"""Deterministic chapter and chunk planning, with nearest-first context."""
from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Mapping, Sequence
from .common import (ProjectError, DEFAULT_MAX_CHARS, DEFAULT_CHUNKING_VERSION,
                     _sha256_bytes, _dump, _normalise_text, _validate_source_suffix)

def _split_long(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    pieces: list[str] = []
    remaining = text.strip()
    while len(remaining) > max_chars:
        boundary = remaining.rfind("\n", 0, max_chars + 1)
        if boundary < max_chars // 2:
            sentence_boundaries = [m.end() for m in re.finditer(r"(?<=[.!?。！？])\s+", remaining[: max_chars + 1])]
            boundary = sentence_boundaries[-1] if sentence_boundaries else max_chars
        piece = remaining[:boundary].strip()
        if not piece:
            boundary = max_chars
            piece = remaining[:boundary].strip()
        pieces.append(piece)
        remaining = remaining[boundary:].strip()
    if remaining:
        pieces.append(remaining)
    return pieces


def _paragraphs(block: str, max_chars: int) -> list[str]:
    cleaned = block.strip()
    if not cleaned:
        return []
    lines = cleaned.splitlines()
    units: list[str] = []
    # Keep a Markdown chapter heading as a separate unit.  That makes the
    # assembled result useful even when a worker translates each unit alone.
    if lines and re.match(r"^\s{0,3}#{1,6}\s+\S", lines[0]):
        units.append(lines[0].strip())
        cleaned = "\n".join(lines[1:]).strip()
    if cleaned:
        units.extend(piece.strip() for piece in re.split(r"\n\s*\n", cleaned) if piece.strip())
    split_units: list[str] = []
    for unit in units:
        split_units.extend(_split_long(unit, max_chars))
    return split_units


def _chapter_blocks(text: str, source_format: str) -> list[tuple[str, str]]:
    """Return (title, block) pairs using stable top-level Markdown headings."""
    if source_format == "markdown":
        lines = text.splitlines()
        starts = [
            index
            for index, line in enumerate(lines)
            if re.match(r"^\s{0,3}#(?!#)\s+\S", line)
        ]
        if not starts:
            return [("Chapter 1", text)]
        blocks: list[tuple[str, str]] = []
        if starts[0] > 0 and "\n".join(lines[: starts[0]]).strip():
            blocks.append(("Preamble", "\n".join(lines[: starts[0]])))
        for position, start in enumerate(starts):
            end = starts[position + 1] if position + 1 < len(starts) else len(lines)
            block = "\n".join(lines[start:end]).strip()
            heading = lines[start].strip()
            title = re.sub(r"^#+\s*", "", heading).strip() or f"Chapter {len(blocks) + 1}"
            blocks.append((title, block))
        return blocks
    form_feed_blocks = [part for part in text.split("\f") if part.strip()]
    if len(form_feed_blocks) > 1:
        return [(f"Chapter {number}", block) for number, block in enumerate(form_feed_blocks, 1)]
    return [("Chapter 1", text)]


def _make_chunks(text: str, source_format: str, max_chars: int) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    chapter_blocks = _chapter_blocks(text, source_format)
    for chapter_number, (title, block) in enumerate(chapter_blocks, 1):
        chapter_id = f"ch{chapter_number:03d}"
        units = _paragraphs(block, max_chars)
        if not units:
            continue
        for chunk_number, source in enumerate(units, 1):
            chunk_id = f"{chapter_id}_c{chunk_number:03d}"
            chunks.append(
                {
                    "chapter_id": chapter_id,
                    "chapter_number": chapter_number,
                    "title": title,
                    "chunk_id": chunk_id,
                    "chunk_number": chunk_number,
                    "source": source,
                    "source_hash": _sha256_bytes(source.encode("utf-8")),
                }
            )
    if not chunks:
        raise ProjectError("the source contains no translatable text")
    return chunks


def _chapter_blocks_v2(text: str, source_format: str) -> tuple[list[tuple[str, str]], int, bool]:
    """Find Markdown H1 headings outside fenced code blocks."""
    if source_format != "markdown":
        return _chapter_blocks(text, source_format), 0, False
    lines = text.splitlines()
    starts: list[int] = []
    fence_char: str | None = None
    fence_width = 0
    for index, line in enumerate(lines):
        fence = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if fence_char is None and fence:
            marker = fence.group(1)
            fence_char, fence_width = marker[0], len(marker)
            continue
        if fence_char is not None:
            closer = re.match(rf"^ {{0,3}}({re.escape(fence_char)}{{{fence_width},}})[ \t]*$", line)
            if closer:
                fence_char, fence_width = None, 0
            continue
        if fence_char is None and re.match(r"^ {0,3}#(?!#)\s+\S", line):
            starts.append(index)
    if not starts:
        return [("Chapter 1", text)], 0, fence_char is not None
    blocks: list[tuple[str, str]] = []
    if starts[0] > 0 and "\n".join(lines[: starts[0]]).strip():
        blocks.append(("Preamble", "\n".join(lines[: starts[0]])))
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        heading = lines[start].strip()
        blocks.append((heading.lstrip("# ").strip() or f"Chapter {len(blocks) + 1}", "\n".join(lines[start:end]).strip()))
    return blocks, len(starts), fence_char is not None


def _split_long_v2(text: str, max_chars: int) -> tuple[list[str], int]:
    """Split a long Markdown block at a text boundary whenever possible."""
    pieces: list[str] = []
    hard_cuts = 0
    remaining = text.strip()
    while len(remaining) > max_chars:
        prefix = remaining[: max_chars + 1]
        candidates = [match.end() for match in re.finditer(r"\s+", prefix) if match.end() <= max_chars]
        sentence = [
            match.end()
            for match in re.finditer(r"[.!?。！？][»”\"']{0,2}\s+", prefix)
            if match.end() <= max_chars
        ]
        newlines = [match.end() for match in re.finditer(r"\n+", prefix) if match.end() <= max_chars]
        late = lambda values: [value for value in values if value >= max_chars // 2]
        boundary = next((values[-1] for values in (late(newlines), late(sentence), late(candidates)) if values), None)
        if boundary is None:
            boundary = candidates[-1] if candidates else max_chars
            if not candidates:
                hard_cuts += 1
        piece = remaining[:boundary].strip()
        if not piece:
            raise ProjectError("chunking produced an empty long-block segment")
        pieces.append(piece)
        remaining = remaining[boundary:].strip()
    if remaining:
        pieces.append(remaining)
    return pieces, hard_cuts


def _make_chunks_v2(text: str, source_format: str, max_chars: int) -> tuple[list[dict[str, Any]], list[str]]:
    normalized = _normalise_text(text)
    chapter_blocks, h1_count, unclosed_fence = _chapter_blocks_v2(normalized, source_format)
    warnings: list[str] = []
    if source_format == "markdown" and h1_count == 0:
        warnings.append("NO_H1_CHAPTER_HEADINGS")
    if unclosed_fence:
        warnings.append("UNCLOSED_CODE_FENCE")
    chunks: list[dict[str, Any]] = []
    hard_cuts = 0
    for chapter_number, (title, block) in enumerate(chapter_blocks, 1):
        chapter_id = f"ch{chapter_number:03d}"
        units = [part.strip() for part in re.split(r"\n\s*\n", block.strip()) if part.strip()]
        grouped: list[str] = []
        current = ""
        for unit in units:
            if len(unit) > max_chars:
                if current:
                    grouped.append(current)
                    current = ""
                parts, cuts = _split_long_v2(unit, max_chars)
                grouped.extend(parts)
                hard_cuts += cuts
                continue
            if current and len(current) + 2 + len(unit) > max_chars:
                grouped.append(current)
                current = unit
            else:
                current = f"{current}\n\n{unit}" if current else unit
        if current:
            grouped.append(current)
        for chunk_number, source in enumerate(grouped, 1):
            chunks.append({
                "chapter_id": chapter_id,
                "chapter_number": chapter_number,
                "title": title,
                "chunk_id": f"{chapter_id}_c{chunk_number:03d}",
                "chunk_number": chunk_number,
                "source": source,
                "source_hash": _sha256_bytes(source.encode("utf-8")),
            })
    if not chunks:
        raise ProjectError("the source contains no translatable text")
    if any(len(chunk["source"]) > max_chars for chunk in chunks):
        raise ProjectError("chunk plan exceeds max_chars")
    source_content = re.sub(r"\s+", "", normalized)
    chunk_content = "".join(re.sub(r"\s+", "", str(chunk["source"])) for chunk in chunks)
    if source_content != chunk_content:
        raise ProjectError("chunk plan does not cover the source text in order")
    if hard_cuts:
        warnings.append(f"HARD_CHARACTER_CUTS:{hard_cuts}")
    return chunks, warnings


def _plan_hash(chunks: Sequence[Mapping[str, Any]]) -> str:
    canonical = [
        [chunk["chapter_id"], chunk["chapter_number"], chunk["title"],
         chunk["chunk_id"], chunk["chunk_number"], chunk["source_hash"]]
        for chunk in chunks
    ]
    return _sha256_bytes(_dump({"chunks": canonical}).encode("utf-8"))


def _source_plan(source: str, chunking_version: int) -> tuple[Path, str, bytes, list[dict[str, Any]], list[str], str]:
    if chunking_version not in (1, 2):
        raise ProjectError("--chunking-version must be 1 or 2")
    source_path = Path(source).expanduser().resolve()
    if not source_path.is_file():
        raise ProjectError(f"source file not found: {source}")
    source_format = _validate_source_suffix(source_path)
    try:
        source_bytes = source_path.read_bytes()
        source_text = source_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProjectError("source must be UTF-8 text") from exc
    if chunking_version == 2:
        chunks, warnings = _make_chunks_v2(source_text, source_format, DEFAULT_MAX_CHARS)
    else:
        chunks, warnings = _make_chunks(source_text, source_format, DEFAULT_MAX_CHARS), []
    return source_path, source_format, source_bytes, chunks, warnings, _plan_hash(chunks)


def _command_plan(source: str, chunking_version: int = DEFAULT_CHUNKING_VERSION) -> dict[str, Any]:
    source_path, source_format, source_bytes, chunks, warnings, plan_sha256 = _source_plan(source, chunking_version)
    chapter_summary: list[dict[str, Any]] = []
    for chapter_id in dict.fromkeys(str(chunk["chapter_id"]) for chunk in chunks):
        members = [chunk for chunk in chunks if chunk["chapter_id"] == chapter_id]
        chapter_summary.append({
            "chapter_id": chapter_id,
            "chapter_number": members[0]["chapter_number"],
            "title": members[0]["title"],
            "chunks": len(members),
            "chars": sum(len(str(chunk["source"])) for chunk in members),
            "max_chunk_chars": max(len(str(chunk["source"])) for chunk in members),
        })
    return {
        "command": "plan",
        "source": str(source_path),
        "source_format": source_format,
        "source_sha256": _sha256_bytes(source_bytes),
        "plan_sha256": plan_sha256,
        "chunking_version": chunking_version,
        "chapters": len(chapter_summary),
        "chapter_summary": chapter_summary,
        "chunks": len(chunks),
        "max_chunk_chars": max(len(str(chunk["source"])) for chunk in chunks),
        "coverage_ok": True,
        "warnings": warnings,
    }


def select_context(newest_first: Sequence[Mapping[str, Any]], max_chars: int) -> list[dict[str, str]]:
    """Spend the context budget on the closest prior chunk first.

    The caller queries DESC. Only after selection do we restore book order for
    the prompt. A partial older chunk never displaces a complete newer one.
    """
    selected: list[dict[str, str]] = []
    remaining = max(0, max_chars)
    for row in newest_first:
        if remaining == 0:
            break
        text = str(row["text"])
        selected.append({"chunk_id": str(row["chunk_id"]), "text": text[:remaining]})
        remaining -= min(len(text), remaining)
    selected.reverse()
    return selected
