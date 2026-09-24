#!/usr/bin/env python3
"""Locked work units and atomic translation state for four Codex workers.

The root agent prepares book input and supervises the four model workers.
This CLI stores claims, fenced commits, review completion, and Markdown output.
It does not call a model or convert book formats.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Mapping, Sequence


SCHEMA_VERSION = 1
MAX_WORKERS = 4
WORKER_IDS = tuple(f"translator_{number}" for number in range(1, MAX_WORKERS + 1))
WORKER_CANONICAL_PATHS = {worker_id: f"/root/{worker_id}" for worker_id in WORKER_IDS}
MAX_ATTEMPTS = 3
DEFAULT_MAX_CHARS = 4000
DEFAULT_CHUNKING_VERSION = 2
DEFAULT_CONTEXT_CHUNKS = 2
DEFAULT_CONTEXT_CHARS = 2400
SUPPORTED_SUFFIXES = {".md": "markdown", ".markdown": "markdown", ".txt": "text"}


class ProjectError(RuntimeError):
    """An expected, user-facing project or state error."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _dump(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _root_thread_id() -> str | None:
    """Keep the host identity for diagnostics; it never gates startup."""
    return os.environ.get("CODEX_THREAD_ID") or None


def _safe_relative(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise ProjectError("project paths must stay inside the project directory") from exc
    return candidate


def _connect(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(database), isolation_level=None, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    return connection


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Serialize a short, atomic state change."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def _config_bytes(config: Mapping[str, Any]) -> bytes:
    return (json.dumps(config, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _validate_source_suffix(path: Path) -> str:
    suffix = path.suffix.lower()
    try:
        return SUPPORTED_SUFFIXES[suffix]
    except KeyError as exc:
        raise ProjectError("only UTF-8 .md, .markdown, and .txt sources are supported") from exc


def _normalise_text(data: str) -> str:
    return data.replace("\r\n", "\n").replace("\r", "\n")


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


def _ensure_project_layout(root: Path) -> None:
    for name in ("source", "edits", "output", "work/attempts"):
        (root / name).mkdir(parents=True, exist_ok=True)


def _init_database(database: Path, config: Mapping[str, Any], source_hash: str, chunks: Sequence[Mapping[str, Any]]) -> None:
    connection = _connect(database)
    try:
        connection.executescript(
            """
            CREATE TABLE meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                stop_requested INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                ended_at TEXT,
                root_thread_id TEXT
            );
            CREATE TABLE worker_registry (
                worker_id TEXT PRIMARY KEY,
                canonical_path TEXT NOT NULL,
                expected_run_id TEXT
            );
            CREATE TABLE chapters (
                chapter_id TEXT PRIMARY KEY,
                chapter_number INTEGER NOT NULL UNIQUE,
                title TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'PENDING',
                owner_run_id TEXT,
                owner_worker_id TEXT
            );
            CREATE TABLE chunks (
                chunk_id TEXT PRIMARY KEY,
                chapter_id TEXT NOT NULL REFERENCES chapters(chapter_id),
                chunk_number INTEGER NOT NULL,
                source TEXT NOT NULL,
                source_hash TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'PENDING',
                attempts_used INTEGER NOT NULL DEFAULT 0,
                failure_count INTEGER NOT NULL DEFAULT 0,
                current_attempt_id TEXT,
                current_worker_id TEXT,
                UNIQUE(chapter_id, chunk_number)
            );
            CREATE TABLE attempts (
                attempt_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                worker_id TEXT NOT NULL,
                chunk_id TEXT NOT NULL REFERENCES chunks(chunk_id),
                attempt_number INTEGER NOT NULL,
                state TEXT NOT NULL,
                error TEXT,
                started_at TEXT NOT NULL,
                ended_at TEXT
            );
            CREATE INDEX attempts_active ON attempts(run_id, worker_id, state);
            CREATE TABLE translations (
                chunk_id TEXT PRIMARY KEY REFERENCES chunks(chunk_id),
                attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
                text TEXT NOT NULL,
                text_hash TEXT NOT NULL,
                committed_at TEXT NOT NULL
            );
            CREATE TABLE builds (
                build_id TEXT PRIMARY KEY,
                output_path TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );
            CREATE TABLE qa_flags (
                chunk_id TEXT NOT NULL REFERENCES chunks(chunk_id),
                code TEXT NOT NULL,
                details TEXT NOT NULL,
                PRIMARY KEY(chunk_id, code)
            );
            CREATE TABLE reviews (
                stage TEXT NOT NULL,
                unit_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                report TEXT,
                completed_at TEXT,
                PRIMARY KEY(stage, unit_id)
            );
            """
        )
        meta = {
            "schema_version": str(SCHEMA_VERSION),
            "source_hash": source_hash,
            "config_hash": _sha256_bytes(_config_bytes(config)),
            "source_relpath": str(config["source_relpath"]),
            "source_format": str(config["source_format"]),
            "plan_sha256": str(config["plan_sha256"]),
            "current_run_id": "",
        }
        with _transaction(connection):
            connection.executemany("INSERT INTO meta(key, value) VALUES (?, ?)", meta.items())
            chapter_seen: set[str] = set()
            for chunk in chunks:
                chapter_id = str(chunk["chapter_id"])
                if chapter_id not in chapter_seen:
                    connection.execute(
                        "INSERT INTO chapters(chapter_id, chapter_number, title) VALUES (?, ?, ?)",
                        (chapter_id, chunk["chapter_number"], chunk["title"]),
                    )
                    chapter_seen.add(chapter_id)
                connection.execute(
                    """
                    INSERT INTO chunks(chunk_id, chapter_id, chunk_number, source, source_hash)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (chunk["chunk_id"], chapter_id, chunk["chunk_number"], chunk["source"], chunk["source_hash"]),
                )
            chapters = list(dict.fromkeys(str(chunk["chapter_id"]) for chunk in chunks))
            connection.executemany(
                "INSERT INTO reviews(stage, unit_id) VALUES ('chapter', ?)",
                ((chapter_id,) for chapter_id in chapters),
            )
            connection.executemany(
                "INSERT INTO reviews(stage, unit_id) VALUES ('consistency', ?)",
                ((f"batch{index:03d}",) for index in range(1, (len(chapters) + 3) // 4 + 1)),
            )
    finally:
        connection.close()


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


def _command_init(
    source: str,
    project: str,
    target_language: str = "zh-CN",
    source_language: str = "auto",
    chunking_version: int = DEFAULT_CHUNKING_VERSION,
    expected_source_sha256: str | None = None,
    expected_plan_sha256: str | None = None,
) -> dict[str, Any]:
    source_path, source_format, source_bytes, chunks, warnings, plan_sha256 = _source_plan(source, chunking_version)
    source_hash = _sha256_bytes(source_bytes)
    if expected_source_sha256 is not None and expected_source_sha256 != source_hash:
        raise ProjectError("source changed since plan: source SHA-256 mismatch")
    if expected_plan_sha256 is not None and expected_plan_sha256 != plan_sha256:
        raise ProjectError("chunk plan changed since plan: plan SHA-256 mismatch")
    target_language = target_language.strip()
    source_language = source_language.strip()
    if not target_language:
        raise ProjectError("--target-language must not be empty")
    if not source_language:
        raise ProjectError("--source-language must not be empty")
    # Parse before creating any project directory or copying any source data.
    # An invalid/empty source therefore leaves no partial project behind.
    root = Path(project).expanduser().resolve()
    if root.exists():
        if not root.is_dir():
            raise ProjectError("project path exists and is not a directory")
        if any(root.iterdir()):
            raise ProjectError("project directory must be empty; refusing to overwrite existing files")
    root.mkdir(parents=True, exist_ok=True)
    _ensure_project_layout(root)
    source_copy = root / "source" / source_path.name
    if source_copy.resolve() == source_path:
        raise ProjectError("the project source copy cannot be the input file itself")
    _atomic_write(source_copy, source_bytes)
    config: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "source_relpath": f"source/{source_path.name}",
        "source_format": source_format,
        "source_sha256": source_hash,
        "source_language": source_language,
        "target_language": target_language,
        "chunking_version": chunking_version,
        "plan_sha256": plan_sha256,
        "chunking": {
            "max_chars": DEFAULT_MAX_CHARS,
            "context_chunks": DEFAULT_CONTEXT_CHUNKS,
            "context_chars": DEFAULT_CONTEXT_CHARS,
        },
        "retry": {"max_attempts": MAX_ATTEMPTS},
        "workers": {"max": MAX_WORKERS, "ids": list(WORKER_IDS)},
    }
    _atomic_write(root / "config.json", _config_bytes(config))
    _init_database(root / "state.sqlite", config, source_hash, chunks)
    return {
        "command": "init",
        "project": str(root),
        "source_relpath": config["source_relpath"],
        "source_sha256": source_hash,
        "source_format": source_format,
        "source_language": source_language,
        "target_language": target_language,
        "chapters": len({chunk["chapter_id"] for chunk in chunks}),
        "chunks": len(chunks),
        "chunking_version": chunking_version,
        "plan_sha256": plan_sha256,
        "warnings": warnings,
        "workers": list(WORKER_IDS),
    }


def _open_project(project: str) -> tuple[Path, sqlite3.Connection, dict[str, Any]]:
    root = Path(project).expanduser().resolve()
    database = root / "state.sqlite"
    config_path = root / "config.json"
    if not root.is_dir() or not database.is_file() or not config_path.is_file():
        raise ProjectError(f"not an initialized translation project: {project}")
    try:
        config_bytes = config_path.read_bytes()
        config = json.loads(config_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProjectError(f"invalid project config: {config_path}") from exc
    if not isinstance(config, dict):
        raise ProjectError("project config must be a JSON object")
    try:
        source_relpath = str(config["source_relpath"])
        source_format = str(config["source_format"])
        source_hash = str(config["source_sha256"])
    except KeyError as exc:
        raise ProjectError(f"project config is missing {exc.args[0]}") from exc
    source_path = _safe_relative(root, source_relpath)
    if not source_path.is_file():
        raise ProjectError(f"project source copy is missing: {source_relpath}")
    actual_source_hash = _sha256_file(source_path)
    if actual_source_hash != source_hash:
        raise ProjectError("source integrity check failed; the project source copy was modified")
    connection = _connect(database)
    try:
        meta_rows = connection.execute("SELECT key, value FROM meta").fetchall()
        meta = {row["key"]: row["value"] for row in meta_rows}
        expected_config_hash = meta.get("config_hash")
        if expected_config_hash != _sha256_bytes(config_bytes):
            raise ProjectError("config integrity check failed; create a new project for changed settings")
        if meta.get("source_hash") != source_hash or meta.get("source_relpath") != source_relpath:
            raise ProjectError("project metadata does not match config.json")
        if meta.get("source_format") != source_format:
            raise ProjectError("project source format does not match config.json")
        _ensure_schema_compatibility(connection)
    except BaseException:
        connection.close()
        raise
    return root, connection, config


def _config_int(config: Mapping[str, Any], group: str, key: str, default: int) -> int:
    value = config.get(group, {})
    if isinstance(value, Mapping):
        try:
            parsed = int(value.get(key, default))
            return parsed if parsed > 0 else default
        except (TypeError, ValueError):
            return default
    return default


def _worker_paths(root_agent_path: str) -> dict[str, str]:
    base = root_agent_path.strip().rstrip("/")
    if not base:
        raise ProjectError("--root-agent-path must not be empty")
    if not base.startswith("/"):
        raise ProjectError("--root-agent-path must be an absolute path")
    return {
        worker_id: f"{base}/{worker_id}"
        for worker_id in WORKER_IDS
    }


def _ensure_schema_compatibility(connection: sqlite3.Connection) -> None:
    """Open earlier projects without changing their signed config or source."""
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    with _transaction(connection):
        columns = {row[1] for row in connection.execute("PRAGMA table_info(chunks)")}
        if "failure_count" not in columns:
            connection.execute("ALTER TABLE chunks ADD COLUMN failure_count INTEGER NOT NULL DEFAULT 0")
        columns = {row[1] for row in connection.execute("PRAGMA table_info(runs)")}
        if "root_thread_id" not in columns:
            connection.execute("ALTER TABLE runs ADD COLUMN root_thread_id TEXT")
        connection.execute("CREATE TABLE IF NOT EXISTS worker_registry (worker_id TEXT PRIMARY KEY, canonical_path TEXT NOT NULL, expected_run_id TEXT)")
        connection.execute("CREATE TABLE IF NOT EXISTS qa_flags (chunk_id TEXT NOT NULL REFERENCES chunks(chunk_id), code TEXT NOT NULL, details TEXT NOT NULL, PRIMARY KEY(chunk_id, code))")
        connection.execute("CREATE TABLE IF NOT EXISTS reviews (stage TEXT NOT NULL, unit_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING', report TEXT, completed_at TEXT, PRIMARY KEY(stage, unit_id))")
        if "current_run_id" not in {row[0] for row in connection.execute("SELECT key FROM meta")}:
            active = connection.execute("SELECT run_id FROM runs WHERE status IN ('RUNNING','STOPPING') ORDER BY rowid DESC LIMIT 1").fetchone()
            connection.execute("INSERT INTO meta(key,value) VALUES ('current_run_id',?)", ("" if active is None else active[0],))
        for worker_id, canonical_path in WORKER_CANONICAL_PATHS.items():
            connection.execute("INSERT OR IGNORE INTO worker_registry(worker_id,canonical_path) VALUES (?,?)", (worker_id, canonical_path))
        if "reviews" not in tables:
            chapters = [row[0] for row in connection.execute("SELECT chapter_id FROM chapters ORDER BY chapter_number")]
            connection.executemany("INSERT INTO reviews(stage,unit_id) VALUES ('chapter',?)", ((chapter,) for chapter in chapters))
            connection.executemany("INSERT INTO reviews(stage,unit_id) VALUES ('consistency',?)", ((f"batch{n:03d}",) for n in range(1, (len(chapters)+3)//4+1)))


def _ensure_worker(worker_id: str) -> None:
    if worker_id not in WORKER_IDS:
        raise ProjectError(f"worker_id must be one of: {', '.join(WORKER_IDS)}")


def _run_row(connection: sqlite3.Connection, run_id: str) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        raise ProjectError(f"unknown run_id: {run_id}")
    return row


def _verify_stored_plan(connection: sqlite3.Connection, config: Mapping[str, Any]) -> None:
    expected = config.get("plan_sha256")
    if expected is None:
        return
    meta = connection.execute("SELECT value FROM meta WHERE key='plan_sha256'").fetchone()
    if meta is None or meta[0] != expected:
        raise ProjectError("stored chunk plan metadata differs from the locked plan")
    rows = connection.execute("SELECT h.chapter_id,h.chapter_number,h.title,c.chunk_id,c.chunk_number,c.source,c.source_hash FROM chunks c JOIN chapters h ON h.chapter_id=c.chapter_id ORDER BY h.chapter_number,c.chunk_number").fetchall()
    if not rows:
        raise ProjectError("stored chunk plan is empty")
    for row in rows:
        if _sha256_bytes(row["source"].encode("utf-8")) != row["source_hash"]:
            raise ProjectError(f"stored chunk source changed: {row['chunk_id']}")
    if _plan_hash([dict(row) for row in rows]) != expected:
        raise ProjectError("stored chunk plan differs from the locked plan")


def _current_run_id(connection: sqlite3.Connection) -> str | None:
    row = connection.execute("SELECT value FROM meta WHERE key='current_run_id'").fetchone()
    return str(row[0]) if row is not None and row[0] else None


def _set_current_run_id(connection: sqlite3.Connection, run_id: str | None) -> None:
    connection.execute("INSERT INTO meta(key,value) VALUES ('current_run_id',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (run_id or "",))


def _latest_run(connection: sqlite3.Connection) -> sqlite3.Row | None:
    return connection.execute("SELECT * FROM runs ORDER BY rowid DESC LIMIT 1").fetchone()


def _refresh_chapters(connection: sqlite3.Connection) -> None:
    connection.execute("UPDATE chapters SET state='FAILED',owner_run_id=NULL,owner_worker_id=NULL WHERE EXISTS (SELECT 1 FROM chunks c WHERE c.chapter_id=chapters.chapter_id AND c.state='FAILED')")
    connection.execute("UPDATE chapters SET state='DONE',owner_run_id=NULL,owner_worker_id=NULL WHERE NOT EXISTS (SELECT 1 FROM chunks c WHERE c.chapter_id=chapters.chapter_id AND c.state!='DONE')")
    connection.execute("UPDATE chapters SET state='PENDING' WHERE state NOT IN ('DONE','FAILED') AND owner_worker_id IS NULL")


def _refresh_run_after_activity(connection: sqlite3.Connection, run_id: str) -> str:
    incomplete = connection.execute("SELECT COUNT(*) FROM chunks WHERE state!='DONE'").fetchone()[0]
    if incomplete == 0:
        connection.execute("UPDATE runs SET status='COMPLETED',stop_requested=1,ended_at=? WHERE run_id=?", (_utc_now(), run_id))
        if _current_run_id(connection) == run_id:
            _set_current_run_id(connection, None)
        return "COMPLETED"
    return str(_run_row(connection, run_id)["status"])


def _interrupt_run_attempts(connection: sqlite3.Connection, run_id: str, error: str) -> int:
    attempts = connection.execute("SELECT attempt_id,chunk_id FROM attempts WHERE run_id=? AND state='RUNNING'", (run_id,)).fetchall()
    for attempt in attempts:
        connection.execute("UPDATE attempts SET state='INTERRUPTED',ended_at=?,error=? WHERE attempt_id=?", (_utc_now(), error, attempt["attempt_id"]))
        connection.execute("UPDATE chunks SET state='PENDING',current_attempt_id=NULL,current_worker_id=NULL WHERE chunk_id=? AND current_attempt_id=?", (attempt["chunk_id"], attempt["attempt_id"]))
    connection.execute("UPDATE chapters SET owner_run_id=NULL,owner_worker_id=NULL WHERE owner_run_id=?", (run_id,))
    _refresh_chapters(connection)
    return len(attempts)


def _command_start(project: str, root_agent_path: str = "/root") -> dict[str, Any]:
    root, connection, config = _open_project(project)
    worker_paths = _worker_paths(root_agent_path)
    try:
        with _transaction(connection):
            _verify_stored_plan(connection, config)
            active = connection.execute("SELECT run_id FROM runs WHERE status IN ('RUNNING','STOPPING') ORDER BY rowid DESC LIMIT 1").fetchone()
            if active is not None:
                raise ProjectError(f"run {active[0]} is still active; inspect host workers and stop it before starting another run")
            active_attempt = connection.execute("SELECT attempt_id FROM attempts WHERE state='RUNNING' LIMIT 1").fetchone()
            if active_attempt is not None:
                raise ProjectError("active attempt remains; stop the owning run first")
            done = connection.execute("SELECT COUNT(*) FROM chunks WHERE state='DONE'").fetchone()[0]
            total = connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            if done == total:
                return {"command":"start","project":str(root),"status":"COMPLETED","already_completed":True,"run_id":None,"workers":list(WORKER_IDS),"worker_paths":worker_paths}
            connection.execute("UPDATE chunks SET state='PENDING',failure_count=0,current_attempt_id=NULL,current_worker_id=NULL WHERE state!='DONE'")
            connection.execute("UPDATE chapters SET state='PENDING',owner_run_id=NULL,owner_worker_id=NULL WHERE state!='DONE'")
            _refresh_chapters(connection)
            run_id = uuid.uuid4().hex
            connection.execute("INSERT INTO runs(run_id,status,stop_requested,created_at,root_thread_id) VALUES (?,'RUNNING',0,?,?)", (run_id,_utc_now(),_root_thread_id()))
            for worker_id, path in worker_paths.items():
                connection.execute("INSERT INTO worker_registry(worker_id,canonical_path,expected_run_id) VALUES (?,?,?) ON CONFLICT(worker_id) DO UPDATE SET canonical_path=excluded.canonical_path,expected_run_id=excluded.expected_run_id", (worker_id,path,run_id))
            _set_current_run_id(connection, run_id)
        return {"command":"start","project":str(root),"run_id":run_id,"status":"RUNNING","resumed_chunks":total-done,"workers":list(WORKER_IDS),"worker_paths":worker_paths,"source_language":config.get("source_language","auto"),"target_language":config.get("target_language","zh-CN")}
    finally:
        connection.close()


def _current_worker_attempt(connection: sqlite3.Connection, run_id: str, worker_id: str) -> sqlite3.Row | None:
    return connection.execute("SELECT * FROM attempts WHERE run_id=? AND worker_id=? AND state='RUNNING' LIMIT 1", (run_id,worker_id)).fetchone()


def _command_claim(project: str, run_id: str, worker_id: str) -> dict[str, Any]:
    _ensure_worker(worker_id)
    root, connection, config = _open_project(project)
    try:
        with _transaction(connection):
            run = _run_row(connection, run_id)
            if _current_run_id(connection) != run_id or run["status"] != "RUNNING" or run["stop_requested"]:
                return {"command":"claim","claimed":False,"reason":"STALE_RUN","run_id":run_id}
            if _current_worker_attempt(connection, run_id, worker_id) is not None:
                raise ProjectError(f"{worker_id} already has a RUNNING attempt")
            if connection.execute("SELECT COUNT(*) FROM attempts WHERE run_id=? AND state='RUNNING'",(run_id,)).fetchone()[0] >= MAX_WORKERS:
                raise ProjectError("maximum four active workers")
            chapter = connection.execute("SELECT * FROM chapters WHERE owner_run_id=? AND owner_worker_id=? AND state!='FAILED' AND EXISTS (SELECT 1 FROM chunks c WHERE c.chapter_id=chapters.chapter_id AND c.state='PENDING') ORDER BY chapter_number LIMIT 1",(run_id,worker_id)).fetchone()
            if chapter is None:
                chapter = connection.execute("SELECT * FROM chapters WHERE owner_run_id IS NULL AND owner_worker_id IS NULL AND state NOT IN ('DONE','FAILED') AND EXISTS (SELECT 1 FROM chunks c WHERE c.chapter_id=chapters.chapter_id AND c.state='PENDING') ORDER BY chapter_number LIMIT 1").fetchone()
                if chapter is not None:
                    connection.execute("UPDATE chapters SET owner_run_id=?,owner_worker_id=?,state='RUNNING' WHERE chapter_id=?",(run_id,worker_id,chapter["chapter_id"]))
            if chapter is None:
                return {"command":"claim","claimed":False,"reason":"NO_WORK","run_id":run_id}
            chunk = connection.execute("SELECT * FROM chunks WHERE chapter_id=? AND state='PENDING' ORDER BY chunk_number LIMIT 1",(chapter["chapter_id"],)).fetchone()
            if chunk is None:
                return {"command":"claim","claimed":False,"reason":"NO_WORK","run_id":run_id}
            attempt_id = uuid.uuid4().hex
            attempt_number = int(chunk["attempts_used"])+1
            connection.execute("INSERT INTO attempts(attempt_id,run_id,worker_id,chunk_id,attempt_number,state,started_at) VALUES (?,?,?,?,?,'RUNNING',?)",(attempt_id,run_id,worker_id,chunk["chunk_id"],attempt_number,_utc_now()))
            connection.execute("UPDATE chunks SET state='RUNNING',attempts_used=?,current_attempt_id=?,current_worker_id=? WHERE chunk_id=?",(attempt_number,attempt_id,worker_id,chunk["chunk_id"]))
            context_limit = _config_int(config,"chunking","context_chunks",DEFAULT_CONTEXT_CHUNKS)
            context_chars = _config_int(config,"chunking","context_chars",DEFAULT_CONTEXT_CHARS)
            prior = connection.execute("SELECT c.chunk_id,t.text FROM chunks c JOIN translations t ON t.chunk_id=c.chunk_id WHERE c.chapter_id=? AND c.chunk_number<? AND c.state='DONE' ORDER BY c.chunk_number DESC LIMIT ?",(chunk["chapter_id"],chunk["chunk_number"],context_limit)).fetchall()
            context = []
            used = 0
            for row in reversed(prior):
                if used >= context_chars:
                    break
                snippet = str(row["text"])[:context_chars-used]
                context.append({"chunk_id":row["chunk_id"],"text":snippet})
                used += len(snippet)
            return {"command":"claim","claimed":True,"run_id":run_id,"worker_id":worker_id,"attempt_id":attempt_id,"attempt_number":attempt_number,"chapter_id":chunk["chapter_id"],"chunk_id":chunk["chunk_id"],"chunk_number":chunk["chunk_number"],"chapter_title":chapter["title"],"source":chunk["source"],"source_language":config.get("source_language","auto"),"target_language":config.get("target_language","zh-CN"),"context":context}
    finally:
        connection.close()


def _attempt_for_write(connection: sqlite3.Connection, run_id: str, worker_id: str, attempt_id: str) -> sqlite3.Row:
    run = _run_row(connection, run_id)
    if _current_run_id(connection) != run_id or run["status"] != "RUNNING" or run["stop_requested"]:
        raise ProjectError("run is no longer accepting worker results")
    attempt = connection.execute("SELECT * FROM attempts WHERE attempt_id=? AND run_id=? AND worker_id=? AND state='RUNNING'",(attempt_id,run_id,worker_id)).fetchone()
    if attempt is None:
        raise ProjectError("stale, interrupted, or mismatched attempt_id")
    chunk = connection.execute("SELECT * FROM chunks WHERE chunk_id=? AND state='RUNNING' AND current_attempt_id=? AND current_worker_id=?",(attempt["chunk_id"],attempt_id,worker_id)).fetchone()
    if chunk is None:
        raise ProjectError("attempt no longer owns its chunk")
    return chunk


def _read_translation_file(path: str, project_root: Path | None = None) -> str:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute() and project_root is not None:
        candidate = _safe_relative(project_root,str(candidate))
    try:
        text = candidate.read_bytes().decode("utf-8")
    except FileNotFoundError as exc:
        raise ProjectError(f"file not found: {path}") from exc
    except UnicodeDecodeError as exc:
        raise ProjectError("candidate must be UTF-8") from exc
    if not text.strip() or "\x00" in text:
        raise ProjectError("candidate is empty or contains a NUL byte")
    return _normalise_text(text).rstrip()


def _numeric_tokens(text: str) -> list[str]:
    return [re.sub(r"\D","",match) for match in re.findall(r"(?<![0-9])[0-9][0-9,./:%-]*[0-9](?![0-9])",text)]


def _mechanical_check(source: str, translation: str) -> list[str]:
    source_heading = re.match(r"^\s*(#{1,6})(?=\s)",source)
    if source_heading:
        target_heading = re.match(r"^\s*(#{1,6})(?=\s)",translation)
        if not target_heading or target_heading.group(1) != source_heading.group(1):
            raise ProjectError("mechanical QA failed: Markdown heading marker must be preserved on the first line")
    original, translated = Counter(_numeric_tokens(source)), Counter(_numeric_tokens(translation))
    return sorted(token for token,count in original.items() if translated[token] < count)


def _command_commit(project: str, run_id: str, worker_id: str, attempt_id: str, file_path: str) -> dict[str, Any]:
    _ensure_worker(worker_id)
    root, connection, _ = _open_project(project)
    try:
        translation = _read_translation_file(file_path,root)
        with _transaction(connection):
            chunk = _attempt_for_write(connection,run_id,worker_id,attempt_id)
            missing = _mechanical_check(str(chunk["source"]),translation)
            now = _utc_now()
            connection.execute("UPDATE attempts SET state='DONE',ended_at=?,error=NULL WHERE attempt_id=?",(now,attempt_id))
            connection.execute("INSERT INTO translations(chunk_id,attempt_id,text,text_hash,committed_at) VALUES (?,?,?,?,?) ON CONFLICT(chunk_id) DO UPDATE SET attempt_id=excluded.attempt_id,text=excluded.text,text_hash=excluded.text_hash,committed_at=excluded.committed_at",(chunk["chunk_id"],attempt_id,translation,_sha256_bytes(translation.encode("utf-8")),now))
            connection.execute("UPDATE chunks SET state='DONE',current_attempt_id=NULL,current_worker_id=NULL WHERE chunk_id=? AND current_attempt_id=?",(chunk["chunk_id"],attempt_id))
            if missing:
                connection.execute("INSERT INTO qa_flags(chunk_id,code,details) VALUES (?,'SUSPECT_MISSING_NUMBERS',?) ON CONFLICT(chunk_id,code) DO UPDATE SET details=excluded.details",(chunk["chunk_id"],','.join(missing)))
            _refresh_chapters(connection)
            status = _refresh_run_after_activity(connection,run_id)
        return {"command":"commit","committed":True,"project":str(root),"run_id":run_id,"worker_id":worker_id,"attempt_id":attempt_id,"chunk_id":chunk["chunk_id"],"suspect_missing_numbers":missing,"run_status":status}
    finally:
        connection.close()


def _command_fail(project: str, run_id: str, worker_id: str, attempt_id: str, error: str) -> dict[str, Any]:
    _ensure_worker(worker_id)
    if not error.strip():
        raise ProjectError("--error must not be empty")
    root, connection, config = _open_project(project)
    try:
        with _transaction(connection):
            chunk = _attempt_for_write(connection,run_id,worker_id,attempt_id)
            failures = int(chunk["failure_count"])+1
            retryable = failures < _config_int(config,"retry","max_attempts",MAX_ATTEMPTS)
            connection.execute("UPDATE attempts SET state='FAILED',ended_at=?,error=? WHERE attempt_id=?",(_utc_now(),error,attempt_id))
            connection.execute("UPDATE chunks SET state=?,failure_count=?,current_attempt_id=NULL,current_worker_id=NULL WHERE chunk_id=? AND current_attempt_id=?",("PENDING" if retryable else "FAILED",failures,chunk["chunk_id"],attempt_id))
            _refresh_chapters(connection)
            status = _refresh_run_after_activity(connection,run_id)
        return {"command":"fail","failed":True,"project":str(root),"chunk_id":chunk["chunk_id"],"retryable":retryable,"run_status":status}
    finally:
        connection.close()


def _stop_run_in_transaction(connection: sqlite3.Connection, run_id: str, error: str) -> dict[str, Any]:
    run = _run_row(connection,run_id)
    if run["status"] not in ("RUNNING","STOPPING"):
        return {"status":str(run["status"]),"interrupted":0}
    connection.execute("UPDATE runs SET status='STOPPED',stop_requested=1,ended_at=? WHERE run_id=?",(_utc_now(),run_id))
    interrupted = _interrupt_run_attempts(connection,run_id,error)
    if _current_run_id(connection) == run_id:
        _set_current_run_id(connection,None)
    return {"status":"STOPPED","interrupted":interrupted}


def _command_stop(project: str, run_id: str, command: str = "stop") -> dict[str, Any]:
    root, connection, _ = _open_project(project)
    try:
        with _transaction(connection):
            result = _stop_run_in_transaction(connection,run_id,f"{command} requested")
        return {"command":command,"project":str(root),"run_id":run_id,**result}
    finally:
        connection.close()


def _command_interrupt_worker(project: str, run_id: str, worker_id: str) -> dict[str, Any]:
    _ensure_worker(worker_id)
    root, connection, _ = _open_project(project)
    try:
        with _transaction(connection):
            run = _run_row(connection,run_id)
            if _current_run_id(connection) != run_id or run["status"] != "RUNNING":
                return {"command":"interrupt-worker","interrupted":False,"reason":"STALE_RUN"}
            attempt = _current_worker_attempt(connection,run_id,worker_id)
            if attempt is None:
                return {"command":"interrupt-worker","interrupted":False}
            connection.execute("UPDATE attempts SET state='INTERRUPTED',ended_at=?,error='worker interrupted' WHERE attempt_id=?",(_utc_now(),attempt["attempt_id"]))
            connection.execute("UPDATE chunks SET state='PENDING',current_attempt_id=NULL,current_worker_id=NULL WHERE chunk_id=? AND current_attempt_id=?",(attempt["chunk_id"],attempt["attempt_id"]))
            _refresh_chapters(connection)
        return {"command":"interrupt-worker","interrupted":True,"project":str(root),"run_id":run_id,"worker_id":worker_id,"attempt_id":attempt["attempt_id"]}
    finally:
        connection.close()


def _status_payload(root: Path, connection: sqlite3.Connection) -> dict[str, Any]:
    run = _latest_run(connection)
    current = _current_run_id(connection)
    workers = [dict(row) for row in connection.execute("SELECT worker_id,canonical_path,expected_run_id FROM worker_registry ORDER BY worker_id")]
    states = {row["state"]:row["n"] for row in connection.execute("SELECT state,COUNT(*) AS n FROM chunks GROUP BY state")}
    chapters = [dict(row) for row in connection.execute("SELECT chapter_id,chapter_number,title,state,owner_worker_id FROM chapters ORDER BY chapter_number")]
    attempts = [dict(row) for row in connection.execute("SELECT run_id,worker_id,attempt_id,chunk_id,started_at FROM attempts WHERE state='RUNNING' ORDER BY worker_id")]
    reviews = {}
    for stage in ("chapter","consistency"):
        units = [dict(row) for row in connection.execute("SELECT unit_id,status FROM reviews WHERE stage=? ORDER BY unit_id",(stage,))]
        reviews[stage] = {"done":sum(unit["status"]=="DONE" for unit in units),"total":len(units),"units":units}
    flags = [dict(row) for row in connection.execute("SELECT chunk_id,code,details FROM qa_flags ORDER BY chunk_id,code")]
    return {"command":"status","project":str(root),"run":None if run is None else {"run_id":run["run_id"],"status":run["status"],"stop_requested":bool(run["stop_requested"]),"created_at":run["created_at"],"ended_at":run["ended_at"],"current":current==run["run_id"]},"current_run_id":current,"workers":workers,"chunks":{"total":sum(states.values()),"states":states},"chapters":chapters,"running_attempts":attempts,"running_attempt_count":len(attempts),"reviews":reviews,"qa_flags":flags}


def _command_status(project: str) -> dict[str, Any]:
    root, connection, _ = _open_project(project)
    try:
        return _status_payload(root,connection)
    finally:
        connection.close()


def _command_check_run(project: str, run_id: str) -> dict[str, Any]:
    root, connection, _ = _open_project(project)
    try:
        run = _run_row(connection,run_id)
        current = _current_run_id(connection)==run_id
        allowed = current and run["status"]=="RUNNING" and not run["stop_requested"]
        return {"command":"check-run","project":str(root),"run_id":run_id,"status":run["status"],"current":current,"stop_requested":bool(run["stop_requested"]),"may_claim":allowed,"may_commit":allowed}
    finally:
        connection.close()


def _command_review_done(project: str, stage: str, unit_id: str, file_path: str) -> dict[str, Any]:
    if stage not in ("chapter","consistency"):
        raise ProjectError("review stage must be chapter or consistency")
    root, connection, config = _open_project(project)
    try:
        report = _read_translation_file(file_path,root)
        with _transaction(connection):
            _verify_stored_plan(connection,config)
            if connection.execute("SELECT COUNT(*) FROM chunks WHERE state!='DONE'").fetchone()[0]:
                raise ProjectError("finish translation before review")
            if stage=="consistency" and connection.execute("SELECT COUNT(*) FROM reviews WHERE stage='chapter' AND status!='DONE'").fetchone()[0]:
                raise ProjectError("finish chapter review before consistency review")
            unit = connection.execute("SELECT status FROM reviews WHERE stage=? AND unit_id=?",(stage,unit_id)).fetchone()
            if unit is None:
                raise ProjectError(f"unknown review unit: {stage}/{unit_id}")
            if unit[0]=="DONE":
                raise ProjectError("review unit already DONE")
            connection.execute("UPDATE reviews SET status='DONE',report=?,completed_at=? WHERE stage=? AND unit_id=?",(report,_utc_now(),stage,unit_id))
        return {"command":"review-done","project":str(root),"stage":stage,"unit_id":unit_id,"done":True}
    finally:
        connection.close()


def _edit_for(root: Path, chunk_id: str) -> str | None:
    for suffix in (".txt",".md",""):
        path = root/"edits"/f"{chunk_id}{suffix}"
        if path.is_file():
            return _read_translation_file(str(path))
    return None


def _command_build(project: str) -> dict[str, Any]:
    root, connection, config = _open_project(project)
    try:
        with _transaction(connection):
            _verify_stored_plan(connection,config)
            if connection.execute("SELECT COUNT(*) FROM chunks WHERE state!='DONE'").fetchone()[0]:
                raise ProjectError("cannot build until every chunk is DONE")
            if connection.execute("SELECT COUNT(*) FROM reviews WHERE status!='DONE'").fetchone()[0]:
                raise ProjectError("cannot build until chapter and consistency reviews are DONE")
            rows = connection.execute("SELECT c.chunk_id,t.text FROM chunks c JOIN chapters h ON h.chapter_id=c.chapter_id JOIN translations t ON t.chunk_id=c.chunk_id ORDER BY h.chapter_number,c.chunk_number").fetchall()
            if not rows:
                raise ProjectError("no committed translations")
            parts = [(_edit_for(root,row["chunk_id"]) or row["text"]).rstrip() for row in rows]
            content = "\n\n".join(parts).rstrip()+"\n"
            output_dir = root/"output"
            output_dir.mkdir(exist_ok=True)
            stem = Path(str(config["source_relpath"])).stem
            existing = [int(m.group(1)) for file in output_dir.glob(f"{stem}.v*.md") if (m:=re.fullmatch(re.escape(stem)+r"\.v(\d+)\.md",file.name))]
            version = max(existing,default=0)+1
            while True:
                path = output_dir/f"{stem}.v{version:03d}.md"
                if not path.exists():
                    break
                version += 1
            _atomic_write(path,content.encode("utf-8"))
            connection.execute("INSERT INTO builds(build_id,output_path,created_at) VALUES (?,?,?)",(uuid.uuid4().hex,str(path.relative_to(root)),_utc_now()))
        return {"command":"build","built":True,"project":str(root),"output":str(path),"chunks":len(rows),"edits_applied":sum(_edit_for(root,row["chunk_id"]) is not None for row in rows)}
    finally:
        connection.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Book translation state and fenced commits")
    commands = parser.add_subparsers(dest="command",required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("source")
    plan.add_argument("--chunking-version",type=int,choices=(1,2),default=DEFAULT_CHUNKING_VERSION)
    init = commands.add_parser("init")
    init.add_argument("source")
    init.add_argument("--project",required=True)
    init.add_argument("--source-language",default="auto")
    init.add_argument("--target-language",default="zh-CN")
    init.add_argument("--chunking-version",type=int,choices=(1,2),default=DEFAULT_CHUNKING_VERSION)
    init.add_argument("--expected-source-sha256")
    init.add_argument("--expected-plan-sha256")
    start = commands.add_parser("start")
    start.add_argument("project")
    start.add_argument("--root-agent-path",default="/root")
    claim = commands.add_parser("claim")
    claim.add_argument("project")
    claim.add_argument("--run-id",required=True)
    claim.add_argument("--worker-id",required=True)
    commit = commands.add_parser("commit")
    commit.add_argument("project")
    commit.add_argument("--run-id",required=True)
    commit.add_argument("--worker-id",required=True)
    commit.add_argument("--attempt-id",required=True)
    commit.add_argument("--file",required=True)
    fail = commands.add_parser("fail")
    fail.add_argument("project")
    fail.add_argument("--run-id",required=True)
    fail.add_argument("--worker-id",required=True)
    fail.add_argument("--attempt-id",required=True)
    fail.add_argument("--error",required=True)
    for name in ("stop","force-stop"):
        command = commands.add_parser(name)
        command.add_argument("project")
        command.add_argument("--run-id",required=True)
    interrupt = commands.add_parser("interrupt-worker")
    interrupt.add_argument("project")
    interrupt.add_argument("--run-id",required=True)
    interrupt.add_argument("--worker-id",required=True)
    check = commands.add_parser("check-run")
    check.add_argument("project")
    check.add_argument("--run-id",required=True)
    commands.add_parser("status").add_argument("project")
    review = commands.add_parser("review-done")
    review.add_argument("project")
    review.add_argument("--stage",required=True,choices=("chapter","consistency"))
    review.add_argument("--unit-id",required=True)
    review.add_argument("--file",required=True)
    commands.add_parser("build").add_argument("project")
    return parser


def _dispatch(args: argparse.Namespace) -> dict[str, Any]:
    if args.command=="plan": return _command_plan(args.source,args.chunking_version)
    if args.command=="init": return _command_init(args.source,args.project,args.target_language,args.source_language,args.chunking_version,args.expected_source_sha256,args.expected_plan_sha256)
    if args.command=="start": return _command_start(args.project,args.root_agent_path)
    if args.command=="claim": return _command_claim(args.project,args.run_id,args.worker_id)
    if args.command=="commit": return _command_commit(args.project,args.run_id,args.worker_id,args.attempt_id,args.file)
    if args.command=="fail": return _command_fail(args.project,args.run_id,args.worker_id,args.attempt_id,args.error)
    if args.command in ("stop","force-stop"): return _command_stop(args.project,args.run_id,args.command)
    if args.command=="interrupt-worker": return _command_interrupt_worker(args.project,args.run_id,args.worker_id)
    if args.command=="check-run": return _command_check_run(args.project,args.run_id)
    if args.command=="status": return _command_status(args.project)
    if args.command=="review-done": return _command_review_done(args.project,args.stage,args.unit_id,args.file)
    if args.command=="build": return _command_build(args.project)
    raise ProjectError(f"unknown command: {args.command}")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        result = _dispatch(_parser().parse_args(argv))
    except (ProjectError,OSError,sqlite3.Error) as exc:
        print(_dump({"error":str(exc)}),file=sys.stderr)
        return 2
    print(_dump(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
