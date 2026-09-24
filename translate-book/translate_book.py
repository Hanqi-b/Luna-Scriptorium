#!/usr/bin/env python3
"""Small, recoverable state engine for the four-worker book translation pilot.

This module deliberately does not call a model and does not create child
processes.  The Codex root agent (and its four fixed translator identities)
drives it through the JSON CLI:

    python translate_book.py init book.md --project book.translation
    python translate_book.py start book.translation
    python translate_book.py claim book.translation --run-id RUN --worker-id translator_1
    python translate_book.py commit book.translation --run-id RUN \
        --worker-id translator_1 --attempt-id ATTEMPT --file work/attempts/ATTEMPT.txt
    python translate_book.py status book.translation
    python translate_book.py build book.translation

The input adapter is intentionally limited to UTF-8 Markdown/TXT.  Recovery
of an abandoned run requires an explicit host cleanup acknowledgement.  The
CLI is a state store and fencing boundary, not a background service or a
translation API.
"""

from __future__ import annotations

import argparse
from collections import Counter
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import tempfile
import time
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
DEFAULT_ATTEMPT_DEADLINE_SECONDS = 600
SUPPORTED_SUFFIXES = {".md": "markdown", ".markdown": "markdown", ".txt": "text"}
ROOT_GUARD_HOOK_VERSION = 1
ROOT_GUARD_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


class ProjectError(RuntimeError):
    """An expected, user-facing project or state error."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _epoch_now() -> float:
    return time.time()


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


def _root_guard_interrupted_path(cwd: Path, thread_id: str) -> Path:
    return cwd / ".codex" / "translate-book-guard" / "interrupted" / f"{thread_id}.json"


def _root_guard_interrupted(cwd: Path, thread_id: str) -> bool:
    path = _root_guard_interrupted_path(cwd, thread_id)
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ProjectError(f"unable to inspect translation root interruption tombstone: {path}") from exc
    return True


def _root_guard_require_not_interrupted(cwd: Path, thread_id: str) -> None:
    if _root_guard_interrupted(cwd, thread_id):
        raise ProjectError(
            f"translation root interruption tombstone is present; refusing start: "
            f"{_root_guard_interrupted_path(cwd, thread_id)}"
        )


def _root_guard_interrupt_epoch_path(cwd: Path, thread_id: str) -> Path:
    return cwd / ".codex" / "translate-book-guard" / "interrupt-epochs" / f"{thread_id}.log"


def _root_guard_interrupt_epoch(cwd: Path, thread_id: str) -> tuple[int, int, int, int] | None:
    path = _root_guard_interrupt_epoch_path(cwd, thread_id)
    try:
        metadata = os.stat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ProjectError(f"unable to inspect translation root interrupt epoch: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise ProjectError(f"translation root interrupt epoch is not a regular file: {path}")
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
    )


def _root_guard_require_interrupt_epoch(
    cwd: Path,
    thread_id: str,
    snapshot: tuple[int, int, int, int] | None,
) -> None:
    current = _root_guard_interrupt_epoch(cwd, thread_id)
    if current != snapshot:
        raise ProjectError(
            f"translation root cancellation epoch advanced during start "
            f"(interrupt epoch advanced during start): "
            f"{_root_guard_interrupt_epoch_path(cwd, thread_id)}"
        )


def _root_guard_interrupt_epoch_size(cwd: Path, thread_id: str) -> int:
    """Return the durable append-only cancellation generation for a root."""
    current = _root_guard_interrupt_epoch(cwd, thread_id)
    return 0 if current is None else current[2]


def _root_guard_require_run_epoch(cwd: Path, thread_id: str, baseline: int) -> None:
    """Fence a persisted run when its root has interrupted since it started."""
    if _root_guard_interrupt_epoch_size(cwd, thread_id) != baseline:
        raise ProjectError(
            f"translation root cancellation epoch advanced for run "
            f"(interrupt epoch advanced for run): "
            f"{_root_guard_interrupt_epoch_path(cwd, thread_id)}"
        )


def _root_guard_context() -> tuple[str, str, Path] | None:
    """Return the guarded root identity, or None for legacy CLI starts.

    A Codex thread is only allowed to start a guarded run after the project's
    SessionStart and an earlier Stop hook have published markers for this
    exact session and working directory.  The environment is intentionally
    fail-closed: a present but malformed or partial Codex identity, missing
    marker, or mismatched marker prevents the database start.
    """
    thread_id = os.environ.get("CODEX_THREAD_ID")
    session_id = os.environ.get("CODEX_SESSION_ID")
    if thread_id is None and session_id is None:
        return None
    if thread_id is None:
        raise ProjectError("CODEX_THREAD_ID is required for a guarded start")
    if not ROOT_GUARD_ID_PATTERN.fullmatch(thread_id):
        raise ProjectError("CODEX_THREAD_ID is invalid")
    if session_id is None or not ROOT_GUARD_ID_PATTERN.fullmatch(session_id):
        raise ProjectError("CODEX_SESSION_ID is required for a guarded start")
    if thread_id != session_id:
        raise ProjectError("CODEX_THREAD_ID and CODEX_SESSION_ID must match for a guarded start")
    try:
        cwd = Path.cwd().resolve()
    except OSError as exc:
        raise ProjectError("unable to resolve the guarded start working directory") from exc
    expected = {
        "session_id": session_id,
        "cwd": str(cwd),
        "hook_version": ROOT_GUARD_HOOK_VERSION,
    }
    for marker_kind in ("ready", "stop-ready"):
        marker_path = cwd / ".codex" / "translate-book-guard" / marker_kind / f"{thread_id}.json"
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ProjectError(f"translation root guard {marker_kind} marker is missing: {marker_path}") from exc
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProjectError(f"invalid translation root guard {marker_kind} marker: {marker_path}") from exc
        if not isinstance(marker, dict) or type(marker.get("hook_version")) is not int or marker != expected:
            raise ProjectError(f"translation root guard {marker_kind} marker mismatch: {marker_path}")
    _root_guard_require_not_interrupted(cwd, thread_id)
    return thread_id, session_id, cwd


def _root_guard_active_marker(
    cwd: Path,
    thread_id: str,
    session_id: str,
    project: Path,
    run_id: str,
) -> Path:
    """Publish the active run mapping before its DB transaction commits."""
    payload = {
        "session_id": session_id,
        "project": str(project),
        "run_id": run_id,
    }
    active_path = _root_guard_active_marker_path(cwd, thread_id, run_id)
    _atomic_write(active_path, (_dump(payload) + "\n").encode("utf-8"))
    return active_path


def _root_guard_active_marker_path(cwd: Path, thread_id: str, run_id: str) -> Path:
    return cwd / ".codex" / "translate-book-guard" / "active" / thread_id / f"{run_id}.json"


def _root_guard_check_active_mappings(cwd: Path, thread_id: str, run_id: str) -> None:
    """Reject a second active book mapping for the same root thread."""
    active_dir = cwd / ".codex" / "translate-book-guard" / "active" / thread_id
    if active_dir.exists() and not active_dir.is_dir():
        raise ProjectError(f"translation root active mapping directory is not a directory: {active_dir}")
    try:
        mappings = [
            candidate
            for candidate in active_dir.iterdir()
            if candidate.name.endswith(".json") and candidate.name != f"{run_id}.json"
        ]
    except FileNotFoundError:
        mappings = []
    except OSError as exc:
        raise ProjectError(f"unable to inspect translation root active mappings: {active_dir}") from exc
    if mappings:
        raise ProjectError(
            f"translation root already owns an active mapping for this thread: {mappings[0]}"
        )


def _root_guard_remove_active_marker(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ProjectError(f"unable to remove failed translation root active marker: {path}") from exc


def _root_guard_start_cancellation(
    cwd: Path,
    thread_id: str,
    interrupt_epoch: tuple[int, int, int, int] | None,
) -> tuple[bool, BaseException | None]:
    """Check both the clearable tombstone and durable cancellation epoch."""
    try:
        if _root_guard_interrupted(cwd, thread_id):
            return True, None
        _root_guard_require_interrupt_epoch(cwd, thread_id, interrupt_epoch)
    except BaseException as exc:
        return True, exc
    return False, None


def _root_guard_stop_cancelled_start(
    connection: sqlite3.Connection,
    run_id: str,
    marker_path: Path,
    cancellation_error: BaseException | None,
) -> None:
    """Stop a run fenced after its start transaction and retain its marker on failure."""
    try:
        with _transaction(connection):
            _stop_run_in_transaction(connection, run_id, "root interrupted during start")
    except BaseException as exc:
        raise ProjectError(
            f"translation start was interrupted but its run could not be stopped; "
            f"active marker preserved for recovery: {exc}"
        ) from exc
    _root_guard_remove_active_marker(marker_path)
    if cancellation_error is not None:
        raise ProjectError(
            f"translation start cancellation could not be verified; run was stopped: "
            f"{cancellation_error}"
        ) from cancellation_error
    raise ProjectError("translation start cancelled by the root interruption tombstone")


@contextmanager
def _root_guard_start_lock(cwd: Path, thread_id: str) -> Iterator[None]:
    """Serialize guarded starts with the Stop hook's scan and fencing."""
    lock_path = cwd / ".codex" / "translate-book-guard" / "locks" / f"{thread_id}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor: int | None = None
    try:
        descriptor = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    except OSError as exc:
        raise ProjectError(f"unable to acquire translation root guard lock: {lock_path}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


@contextmanager
def _start_transaction(
    connection: sqlite3.Connection,
    guard_context: tuple[str, str, Path] | None,
    project: Path,
    run_id: str,
    interrupt_epoch: tuple[int, int, int, int] | None,
) -> Iterator[sqlite3.Connection]:
    """Publish a guarded mapping and start its DB transaction under one lock."""
    if guard_context is None:
        with _transaction(connection) as active_connection:
            yield active_connection
        return
    thread_id, session_id, cwd = guard_context
    marker_path = _root_guard_active_marker_path(cwd, thread_id, run_id)
    with _root_guard_start_lock(cwd, thread_id):
        _root_guard_require_not_interrupted(cwd, thread_id)
        _root_guard_require_interrupt_epoch(cwd, thread_id, interrupt_epoch)
        _root_guard_check_active_mappings(cwd, thread_id, run_id)
        marker_path = _root_guard_active_marker(cwd, thread_id, session_id, project, run_id)
        try:
            with _transaction(
                connection,
                thread_id,
                cwd,
                interrupt_epoch,
                check_interrupt_epoch=True,
            ) as active_connection:
                yield active_connection
        except BaseException as exc:
            try:
                _root_guard_remove_active_marker(marker_path)
            except BaseException as cleanup_exc:
                raise ProjectError(
                    f"translation start failed: {exc}; active marker cleanup failed: {cleanup_exc}"
                ) from exc
            raise
        cancelled, cancellation_error = _root_guard_start_cancellation(cwd, thread_id, interrupt_epoch)
        if cancelled:
            _root_guard_stop_cancelled_start(connection, run_id, marker_path, cancellation_error)

    # The hook appends its durable epoch before waiting for this lock.  Check
    # once more after releasing it so a late interrupt cannot make this call
    # return a RUNNING result while the hook is timing out on the lock.
    cancelled, cancellation_error = _root_guard_start_cancellation(cwd, thread_id, interrupt_epoch)
    if cancelled:
        _root_guard_stop_cancelled_start(connection, run_id, marker_path, cancellation_error)


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
def _transaction(
    connection: sqlite3.Connection,
    guarded_root_thread_id: str | None = None,
    guarded_root_cwd: Path | None = None,
    guarded_interrupt_epoch: tuple[int, int, int, int] | None = None,
    check_interrupt_epoch: bool = False,
    guarded_run_epoch: int | None = None,
) -> Iterator[sqlite3.Connection]:
    """Start a short serialized write transaction."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        if guarded_root_thread_id is not None:
            guard_cwd = Path.cwd().resolve() if guarded_root_cwd is None else guarded_root_cwd
            _root_guard_require_not_interrupted(guard_cwd, guarded_root_thread_id)
            if check_interrupt_epoch:
                if guarded_run_epoch is None:
                    _root_guard_require_interrupt_epoch(
                        guard_cwd, guarded_root_thread_id, guarded_interrupt_epoch
                    )
                else:
                    _root_guard_require_run_epoch(guard_cwd, guarded_root_thread_id, guarded_run_epoch)
        yield connection
        if guarded_root_thread_id is not None:
            guard_cwd = Path.cwd().resolve() if guarded_root_cwd is None else guarded_root_cwd
            _root_guard_require_not_interrupted(guard_cwd, guarded_root_thread_id)
            if check_interrupt_epoch:
                if guarded_run_epoch is None:
                    _root_guard_require_interrupt_epoch(
                        guard_cwd, guarded_root_thread_id, guarded_interrupt_epoch
                    )
                else:
                    _root_guard_require_run_epoch(guard_cwd, guarded_root_thread_id, guarded_run_epoch)
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
                started_at REAL,
                last_heartbeat REAL,
                heartbeat_token_hash TEXT,
                cleanup_verified_at TEXT,
                root_thread_id TEXT,
                root_guard_cwd TEXT,
                root_guard_epoch INTEGER
            );
            CREATE TABLE worker_registry (
                worker_id TEXT PRIMARY KEY,
                canonical_path TEXT NOT NULL,
                expected_run_id TEXT,
                observed_state TEXT,
                observed_at TEXT
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
                ended_at TEXT,
                deadline_at REAL
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
        "shutdown": {"attempt_deadline_seconds": DEFAULT_ATTEMPT_DEADLINE_SECONDS},
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
    """Apply additive migrations without changing the config hash.

    The heartbeat columns came from the earlier prototype and are retained so
    existing databases can still be opened.  They are intentionally inert in
    the current lifecycle: claim and commit use terminal run fencing and the
    non-renewing attempt deadline instead.  New lifecycle columns are added
    rather than rewriting an existing project's config or state rows.
    """
    chunk_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(chunks)").fetchall()}
    run_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(runs)").fetchall()}
    attempt_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(attempts)").fetchall()}
    table_names = {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    registry_columns = (
        {str(row[1]) for row in connection.execute("PRAGMA table_info(worker_registry)").fetchall()}
        if "worker_registry" in table_names
        else set()
    )
    meta_keys = {str(row[0]) for row in connection.execute("SELECT key FROM meta").fetchall()}
    missing = (
        "failure_count" not in chunk_columns
        or "started_at" not in run_columns
        or "last_heartbeat" not in run_columns
        or "heartbeat_token_hash" not in run_columns
        or "cleanup_verified_at" not in run_columns
        or "root_thread_id" not in run_columns
        or "root_guard_cwd" not in run_columns
        or "root_guard_epoch" not in run_columns
        or "deadline_at" not in attempt_columns
        or "current_run_id" not in meta_keys
        or not {"worker_id", "canonical_path", "expected_run_id", "observed_state", "observed_at"}.issubset(registry_columns)
    )
    if not missing:
        return

    def legacy_epoch(value: object) -> float:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except (TypeError, ValueError, OverflowError):
            # An unparseable legacy timestamp cannot prove that the old root
            # is alive.  Keep the migrated run fenced until an explicit
            # recovery starts a new supervised run.
            return 0.0

    def attempt_deadline(value: object) -> float:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp() + DEFAULT_ATTEMPT_DEADLINE_SECONDS
        except (TypeError, ValueError, OverflowError):
            # A malformed historical timestamp cannot prove that the attempt
            # is still within its window.  Failing it closed is safer than
            # allowing an unbounded old result to commit.
            return 0.0

    with _transaction(connection):
        # Re-read under the writer lock so concurrent first opens do not race
        # on ALTER TABLE after another process has completed the migration.
        chunk_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(chunks)").fetchall()}
        run_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(runs)").fetchall()}
        if "failure_count" not in chunk_columns:
            connection.execute("ALTER TABLE chunks ADD COLUMN failure_count INTEGER NOT NULL DEFAULT 0")
            failed_rows = connection.execute(
                "SELECT chunk_id, COUNT(*) AS count FROM attempts WHERE state = 'FAILED' GROUP BY chunk_id"
            ).fetchall()
            for row in failed_rows:
                connection.execute(
                    "UPDATE chunks SET failure_count = ? WHERE chunk_id = ?",
                    (int(row["count"]), row["chunk_id"]),
                )
        if "started_at" not in run_columns:
            connection.execute("ALTER TABLE runs ADD COLUMN started_at REAL")
        if "last_heartbeat" not in run_columns:
            connection.execute("ALTER TABLE runs ADD COLUMN last_heartbeat REAL")
        if "heartbeat_token_hash" not in run_columns:
            connection.execute("ALTER TABLE runs ADD COLUMN heartbeat_token_hash TEXT")
        if "cleanup_verified_at" not in run_columns:
            connection.execute("ALTER TABLE runs ADD COLUMN cleanup_verified_at TEXT")
        if "root_thread_id" not in run_columns:
            connection.execute("ALTER TABLE runs ADD COLUMN root_thread_id TEXT")
        if "root_guard_cwd" not in run_columns:
            connection.execute("ALTER TABLE runs ADD COLUMN root_guard_cwd TEXT")
        if "root_guard_epoch" not in run_columns:
            connection.execute("ALTER TABLE runs ADD COLUMN root_guard_epoch INTEGER")
        if "deadline_at" not in attempt_columns:
            connection.execute("ALTER TABLE attempts ADD COLUMN deadline_at REAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS worker_registry (
                worker_id TEXT PRIMARY KEY,
                canonical_path TEXT NOT NULL,
                expected_run_id TEXT,
                observed_state TEXT,
                observed_at TEXT
            )
            """
        )
        registry_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(worker_registry)").fetchall()}
        if "canonical_path" not in registry_columns:
            connection.execute("ALTER TABLE worker_registry ADD COLUMN canonical_path TEXT")
        if "expected_run_id" not in registry_columns:
            connection.execute("ALTER TABLE worker_registry ADD COLUMN expected_run_id TEXT")
        if "observed_state" not in registry_columns:
            connection.execute("ALTER TABLE worker_registry ADD COLUMN observed_state TEXT")
        if "observed_at" not in registry_columns:
            connection.execute("ALTER TABLE worker_registry ADD COLUMN observed_at TEXT")
        rows = connection.execute(
            "SELECT run_id, status, created_at, started_at, last_heartbeat, heartbeat_token_hash, cleanup_verified_at FROM runs"
        ).fetchall()
        for row in rows:
            started_at = row["started_at"]
            last_heartbeat = row["last_heartbeat"]
            fallback = legacy_epoch(row["created_at"])
            if started_at is None:
                started_at = fallback
            if row["heartbeat_token_hash"] is None or last_heartbeat is None:
                last_heartbeat = 0.0
            connection.execute(
                """
                UPDATE runs
                SET started_at = ?, last_heartbeat = ?
                WHERE run_id = ?
                """,
                (float(started_at), float(last_heartbeat), row["run_id"]),
            )
        attempts = connection.execute(
            "SELECT attempt_id, started_at, deadline_at FROM attempts WHERE deadline_at IS NULL"
        ).fetchall()
        for attempt in attempts:
            connection.execute(
                "UPDATE attempts SET deadline_at = ? WHERE attempt_id = ?",
                (attempt_deadline(attempt["started_at"]), attempt["attempt_id"]),
            )
        if "current_run_id" not in meta_keys:
            active = connection.execute(
                "SELECT run_id FROM runs WHERE status IN ('RUNNING', 'STOPPING') ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            connection.execute(
                "INSERT INTO meta(key, value) VALUES ('current_run_id', ?)",
                ("" if active is None else str(active["run_id"]),),
            )
        current = connection.execute("SELECT value FROM meta WHERE key = 'current_run_id'").fetchone()
        current_id = str(current[0]) if current is not None and str(current[0]) else None
        if current_id is None:
            latest = connection.execute(
                "SELECT run_id FROM runs ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            current_id = None if latest is None else str(latest["run_id"])
        for worker_id, canonical_path in WORKER_CANONICAL_PATHS.items():
            connection.execute(
                """
                INSERT OR IGNORE INTO worker_registry(
                    worker_id, canonical_path, expected_run_id, observed_state, observed_at
                ) VALUES (?, ?, ?, NULL, NULL)
                """,
                (worker_id, canonical_path, current_id),
            )


def _ensure_worker(worker_id: str) -> None:
    if worker_id not in WORKER_IDS:
        raise ProjectError(f"worker_id must be one of: {', '.join(WORKER_IDS)}")


def _run_row(connection: sqlite3.Connection, run_id: str) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        raise ProjectError(f"unknown run_id: {run_id}")
    return row


def _verify_stored_plan(connection: sqlite3.Connection, config: Mapping[str, Any]) -> None:
    expected = config.get("plan_sha256")
    if expected is None:
        return  # A project initialized before plan locking keeps its stored chunks.
    meta = connection.execute("SELECT value FROM meta WHERE key = 'plan_sha256'").fetchone()
    if meta is None or str(meta[0]) != expected:
        raise ProjectError("stored chunk plan metadata differs from the locked plan")
    rows = connection.execute(
        """
        SELECT h.chapter_id, h.chapter_number, h.title,
               c.chunk_id, c.chunk_number, c.source, c.source_hash
        FROM chunks c JOIN chapters h ON h.chapter_id = c.chapter_id
        ORDER BY h.chapter_number, c.chunk_number
        """
    ).fetchall()
    if not rows:
        raise ProjectError("stored chunk plan is empty")
    chunks: list[dict[str, Any]] = []
    for row in rows:
        if _sha256_bytes(str(row["source"]).encode("utf-8")) != row["source_hash"]:
            raise ProjectError(f"stored chunk source changed: {row['chunk_id']}")
        chunks.append(dict(row))
    if _plan_hash(chunks) != expected:
        raise ProjectError("stored chunk plan differs from the locked plan")


def _run_guard_context(
    connection: sqlite3.Connection,
    run_id: str,
) -> tuple[str | None, Path | None, int | None]:
    """Load the immutable root fencing context persisted with a run."""
    row = connection.execute(
        "SELECT root_thread_id, root_guard_cwd, root_guard_epoch FROM runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if row is None or row["root_thread_id"] is None:
        return None, None, None
    root_thread_id = str(row["root_thread_id"])
    raw_cwd = row["root_guard_cwd"]
    raw_epoch = row["root_guard_epoch"]
    if raw_cwd is None or not str(raw_cwd):
        raise ProjectError(f"guarded run {run_id} is missing its root guard cwd")
    guarded_cwd = Path(str(raw_cwd))
    if not guarded_cwd.is_absolute():
        raise ProjectError(f"guarded run {run_id} has a non-absolute root guard cwd")
    if raw_epoch is None:
        raise ProjectError(f"guarded run {run_id} is missing its root guard epoch")
    try:
        guarded_epoch = int(raw_epoch)
    except (TypeError, ValueError) as exc:
        raise ProjectError(f"guarded run {run_id} has an invalid root guard epoch") from exc
    if guarded_epoch < 0:
        raise ProjectError(f"guarded run {run_id} has an invalid root guard epoch")
    return root_thread_id, guarded_cwd, guarded_epoch


def _attempt_deadline_seconds(config: Mapping[str, Any]) -> int:
    return _config_int(
        config,
        "shutdown",
        "attempt_deadline_seconds",
        DEFAULT_ATTEMPT_DEADLINE_SECONDS,
    )


def _current_run_id(connection: sqlite3.Connection) -> str | None:
    row = connection.execute("SELECT value FROM meta WHERE key = 'current_run_id'").fetchone()
    if row is None or not str(row[0]):
        return None
    return str(row[0])


def _set_current_run_id(connection: sqlite3.Connection, run_id: str | None) -> None:
    value = "" if run_id is None else run_id
    connection.execute(
        "INSERT INTO meta(key, value) VALUES ('current_run_id', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (value,),
    )


def _run_is_current(connection: sqlite3.Connection, run_id: str) -> bool:
    return _current_run_id(connection) == run_id


def _deadline_fresh(attempt: sqlite3.Row, now: float | None = None) -> bool:
    raw_deadline = attempt["deadline_at"]
    try:
        deadline = float(raw_deadline) if raw_deadline is not None else 0.0
    except (TypeError, ValueError):
        deadline = 0.0
    return (_epoch_now() if now is None else now) <= deadline


def _latest_run(connection: sqlite3.Connection) -> sqlite3.Row | None:
    # created_at is intentionally human-readable and second-resolution.  A
    # rapid stop/restart can therefore create multiple runs with the same
    # timestamp; rowid preserves their serialized insertion order.
    return connection.execute("SELECT * FROM runs ORDER BY rowid DESC LIMIT 1").fetchone()


def _refresh_chapters(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        UPDATE chapters
        SET state = 'FAILED', owner_run_id = NULL, owner_worker_id = NULL
        WHERE EXISTS (
            SELECT 1 FROM chunks WHERE chunks.chapter_id = chapters.chapter_id AND chunks.state = 'FAILED'
        )
        """
    )
    connection.execute(
        """
        UPDATE chapters
        SET state = 'DONE', owner_run_id = NULL, owner_worker_id = NULL
        WHERE NOT EXISTS (
            SELECT 1 FROM chunks WHERE chunks.chapter_id = chapters.chapter_id AND chunks.state != 'DONE'
        )
        """
    )
    connection.execute(
        """
        UPDATE chapters
        SET state = CASE WHEN state = 'DONE' THEN 'DONE' ELSE 'PENDING' END
        WHERE state NOT IN ('DONE', 'FAILED')
          AND NOT EXISTS (
              SELECT 1 FROM chunks WHERE chunks.chapter_id = chapters.chapter_id AND chunks.state = 'RUNNING'
          )
          AND owner_worker_id IS NULL
        """
    )


def _refresh_run_after_activity(connection: sqlite3.Connection, run_id: str) -> str:
    row = _run_row(connection, run_id)
    incomplete = connection.execute("SELECT COUNT(*) FROM chunks WHERE state != 'DONE'").fetchone()[0]
    if incomplete == 0:
        now = _utc_now()
        connection.execute(
            """
            UPDATE runs
            SET status = 'COMPLETED', stop_requested = 1, ended_at = ?
            WHERE run_id = ?
            """,
            (now, run_id),
        )
        if _run_is_current(connection, run_id):
            _set_current_run_id(connection, None)
        return "COMPLETED"
    return str(row["status"])


def _interrupt_run_attempts(
    connection: sqlite3.Connection,
    run_id: str,
    error: str,
) -> int:
    """Fence a run's active attempts and retain their chunks as INTERRUPTED."""
    running_attempts = connection.execute(
        "SELECT attempt_id, chunk_id FROM attempts WHERE state = 'RUNNING'"
        " AND run_id = ?",
        (run_id,),
    ).fetchall()
    recovered = len(running_attempts)
    now = _utc_now()
    for attempt in running_attempts:
        connection.execute(
            "UPDATE attempts SET state = 'INTERRUPTED', ended_at = ?, error = ? WHERE attempt_id = ?",
            (now, error, attempt["attempt_id"]),
        )
        connection.execute(
            """
            UPDATE chunks SET state = 'INTERRUPTED', current_attempt_id = NULL, current_worker_id = NULL
            WHERE chunk_id = ? AND state = 'RUNNING' AND current_attempt_id = ?
            """,
            (attempt["chunk_id"], attempt["attempt_id"]),
        )
    connection.execute(
        """
        UPDATE chunks SET state = 'INTERRUPTED', current_attempt_id = NULL, current_worker_id = NULL
        WHERE state = 'RUNNING'
          AND (
              EXISTS (SELECT 1 FROM attempts a WHERE a.chunk_id = chunks.chunk_id AND a.run_id = ?)
              OR EXISTS (SELECT 1 FROM chapters h WHERE h.chapter_id = chunks.chapter_id AND h.owner_run_id = ?)
          )
        """
        , (run_id, run_id)
    )
    connection.execute(
        "UPDATE chapters SET owner_run_id = NULL, owner_worker_id = NULL WHERE owner_run_id = ?",
        (run_id,),
    )
    _refresh_chapters(connection)
    return recovered


def _invalidate_run_in_transaction(connection: sqlite3.Connection, run_id: str) -> int:
    """Fence one run after the caller has established that its root is gone."""
    run = _run_row(connection, run_id)
    if run["status"] not in ("RUNNING", "STOPPING"):
        return 0
    now = _utc_now()
    connection.execute(
        """
        UPDATE runs
        SET status = 'INTERRUPTED', stop_requested = 1, ended_at = ?, cleanup_verified_at = NULL
        WHERE run_id = ? AND status IN ('RUNNING', 'STOPPING')
        """,
        (now, run_id),
    )
    interrupted = _interrupt_run_attempts(connection, run_id, "run invalidated after root loss")
    connection.execute(
        "UPDATE chapters SET owner_run_id = NULL, owner_worker_id = NULL WHERE owner_run_id = ?",
        (run_id,),
    )
    _refresh_chapters(connection)
    if _run_is_current(connection, run_id):
        _set_current_run_id(connection, None)
    return interrupted


def _command_start(project: str, root_agent_path: str = "/root") -> dict[str, Any]:
    # Snapshot the durable root cancellation generation before marker reads or
    # project I/O.  A Stop hook can append and clear its tombstone while this
    # command is opening the project; the generation must still fence it.
    initial_thread_id = os.environ.get("CODEX_THREAD_ID")
    initial_session_id = os.environ.get("CODEX_SESSION_ID")
    initial_guard_cwd: Path | None = None
    interrupt_epoch: tuple[int, int, int, int] | None = None
    if (
        initial_thread_id is not None
        and initial_session_id is not None
        and initial_thread_id == initial_session_id
        and ROOT_GUARD_ID_PATTERN.fullmatch(initial_thread_id)
    ):
        try:
            initial_guard_cwd = Path.cwd().resolve()
        except OSError as exc:
            raise ProjectError("unable to resolve the guarded start working directory") from exc
        interrupt_epoch = _root_guard_interrupt_epoch(initial_guard_cwd, initial_thread_id)
    guard_context = _root_guard_context()
    root_thread_id = None if guard_context is None else guard_context[0]
    root_guard_cwd: str | None = None
    root_guard_epoch: int | None = None
    if guard_context is not None:
        _guard_thread_id, _session_id, guarded_cwd = guard_context
        if initial_guard_cwd != guarded_cwd or initial_thread_id != _guard_thread_id:
            raise ProjectError("guarded start identity changed while establishing its cancellation epoch")
        root_guard_cwd = str(guarded_cwd)
        root_guard_epoch = 0 if interrupt_epoch is None else interrupt_epoch[2]
    root, connection, config = _open_project(project)
    worker_paths = _worker_paths(root_agent_path)
    run_id = uuid.uuid4().hex
    try:
        with _start_transaction(connection, guard_context, root, run_id, interrupt_epoch):
            _verify_stored_plan(connection, config)
            active = connection.execute(
                """
                SELECT run_id FROM runs
                WHERE status IN ('RUNNING', 'STOPPING')
                ORDER BY rowid DESC LIMIT 1
                """
            ).fetchone()
            if active is not None:
                raise ProjectError(
                    f"run {active['run_id']} is still active; stop it or explicitly invalidate-run after root loss"
                )
            active_attempt = connection.execute(
                "SELECT run_id FROM attempts WHERE state = 'RUNNING' ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            if active_attempt is not None:
                raise ProjectError(
                    f"run {active_attempt['run_id']} still has a RUNNING attempt; "
                    "clean up that run before start"
                )
            unverified = connection.execute(
                """
                SELECT run_id FROM runs
                WHERE status IN ('STOPPED', 'INTERRUPTED', 'COMPLETED') AND cleanup_verified_at IS NULL
                ORDER BY rowid DESC LIMIT 1
                """
            ).fetchone()
            if unverified is not None:
                raise ProjectError(
                    f"workers for run {unverified['run_id']} must be confirmed cleared; "
                    f"run confirm-cleanup before start"
                )
            interrupted = connection.execute(
                "SELECT COUNT(*) FROM chunks WHERE state = 'INTERRUPTED'"
            ).fetchone()[0]
            connection.execute(
                """
                UPDATE chunks
                SET state = 'PENDING', current_attempt_id = NULL, current_worker_id = NULL
                WHERE state = 'INTERRUPTED'
                """
            )
            connection.execute(
                """
                UPDATE chapters SET owner_run_id = NULL, owner_worker_id = NULL
                WHERE state NOT IN ('DONE', 'FAILED')
                """
            )
            _refresh_chapters(connection)
            incomplete = connection.execute("SELECT COUNT(*) FROM chunks WHERE state != 'DONE'").fetchone()[0]
            status = "RUNNING" if incomplete else "COMPLETED"
            created_at = _utc_now()
            started_at = _epoch_now()
            # DB completion does not prove that the host has released all
            # worker identities.  The root must call confirm-cleanup after it
            # observes the host state.
            cleanup_verified_at = None
            connection.execute(
                """
                INSERT INTO runs(
                    run_id, status, stop_requested, created_at, ended_at,
                    started_at, last_heartbeat, heartbeat_token_hash,
                    cleanup_verified_at, root_thread_id, root_guard_cwd, root_guard_epoch
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    status,
                    int(status == "COMPLETED"),
                    created_at,
                    created_at if status == "COMPLETED" else None,
                    started_at,
                    cleanup_verified_at,
                    root_thread_id,
                    root_guard_cwd,
                    root_guard_epoch,
                ),
            )
            for worker_id, canonical_path in worker_paths.items():
                connection.execute(
                    """
                    INSERT INTO worker_registry(
                        worker_id, canonical_path, expected_run_id, observed_state, observed_at
                    ) VALUES (?, ?, ?, NULL, NULL)
                    ON CONFLICT(worker_id) DO UPDATE SET
                        canonical_path = excluded.canonical_path,
                        expected_run_id = excluded.expected_run_id,
                        observed_state = NULL,
                        observed_at = NULL
                    """,
                    (worker_id, canonical_path, run_id),
                )
            _set_current_run_id(connection, run_id if status == "RUNNING" else None)
        if guard_context is not None:
            cancelled, cancellation_error = _root_guard_start_cancellation(
                guard_context[2], guard_context[0], interrupt_epoch
            )
            if cancelled:
                _root_guard_stop_cancelled_start(
                    connection,
                    run_id,
                    _root_guard_active_marker_path(guard_context[2], guard_context[0], run_id),
                    cancellation_error,
                )
        result = {
            "command": "start",
            "project": str(root),
            "run_id": str(run_id),
            "root_thread_id": root_thread_id,
            "root_guard_cwd": root_guard_cwd,
            "root_guard_epoch": root_guard_epoch,
            "status": status,
            "resumed_chunks": int(interrupted),
            "workers": list(WORKER_IDS),
            "worker_paths": worker_paths,
            "source_language": config.get("source_language", "auto"),
            "target_language": config.get("target_language", "zh-CN"),
            "config_schema_version": config.get("schema_version"),
        }
    finally:
        connection.close()
    return result


def _current_worker_attempt(connection: sqlite3.Connection, run_id: str, worker_id: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM attempts WHERE run_id = ? AND worker_id = ? AND state = 'RUNNING' LIMIT 1",
        (run_id, worker_id),
    ).fetchone()


def _command_claim(project: str, run_id: str, worker_id: str) -> dict[str, Any]:
    _ensure_worker(worker_id)
    root, connection, config = _open_project(project)
    try:
        guarded_root_thread_id, guarded_root_cwd, guarded_run_epoch = _run_guard_context(connection, run_id)
        with _transaction(
            connection,
            guarded_root_thread_id,
            guarded_root_cwd,
            check_interrupt_epoch=True,
            guarded_run_epoch=guarded_run_epoch,
        ):
            run = _run_row(connection, run_id)
            if not _run_is_current(connection, run_id):
                return {"command": "claim", "claimed": False, "reason": "STALE_RUN", "run_id": run_id}
            if run["status"] != "RUNNING" or run["stop_requested"]:
                return {"command": "claim", "claimed": False, "reason": "STOP_REQUESTED", "run_id": run_id}
            current_attempt = _current_worker_attempt(connection, run_id, worker_id)
            if current_attempt is not None and not _deadline_fresh(current_attempt):
                return {
                    "command": "claim",
                    "claimed": False,
                    "reason": "ATTEMPT_DEADLINE_EXPIRED",
                    "run_id": run_id,
                    "attempt_id": current_attempt["attempt_id"],
                }
            if current_attempt is not None:
                raise ProjectError(f"{worker_id} already has a RUNNING attempt; commit, fail, or interrupt it first")
            active_count = connection.execute(
                "SELECT COUNT(*) FROM attempts WHERE run_id = ? AND state = 'RUNNING'", (run_id,)
            ).fetchone()[0]
            if active_count >= MAX_WORKERS:
                raise ProjectError("the run already has the maximum four active workers")
            max_attempts = _config_int(config, "retry", "max_attempts", MAX_ATTEMPTS)
            connection.execute(
                "UPDATE chunks SET state = 'FAILED' WHERE state = 'PENDING' AND failure_count >= ?", (max_attempts,)
            )
            _refresh_chapters(connection)
            owned = connection.execute(
                """
                SELECT * FROM chapters
                WHERE owner_run_id = ? AND owner_worker_id = ? AND state NOT IN ('DONE', 'FAILED')
                  AND EXISTS (
                      SELECT 1 FROM chunks
                      WHERE chunks.chapter_id = chapters.chapter_id AND chunks.state = 'PENDING'
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM chunks
                      WHERE chunks.chapter_id = chapters.chapter_id AND chunks.state = 'INTERRUPTED'
                  )
                ORDER BY chapter_number LIMIT 1
                """,
                (run_id, worker_id),
            ).fetchone()
            chapter = owned
            if chapter is None:
                chapter = connection.execute(
                    """
                    SELECT * FROM chapters
                    WHERE owner_run_id IS NULL AND owner_worker_id IS NULL AND state NOT IN ('DONE', 'FAILED')
                      AND EXISTS (
                          SELECT 1 FROM chunks
                          WHERE chunks.chapter_id = chapters.chapter_id AND chunks.state = 'PENDING'
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM chunks
                          WHERE chunks.chapter_id = chapters.chapter_id AND chunks.state = 'INTERRUPTED'
                      )
                    ORDER BY chapter_number LIMIT 1
                    """
                ).fetchone()
                if chapter is not None:
                    connection.execute(
                        """
                        UPDATE chapters SET owner_run_id = ?, owner_worker_id = ?, state = 'RUNNING'
                        WHERE chapter_id = ? AND owner_run_id IS NULL AND owner_worker_id IS NULL
                        """,
                        (run_id, worker_id, chapter["chapter_id"]),
                    )
            if chapter is None:
                return {"command": "claim", "claimed": False, "reason": "NO_WORK", "run_id": run_id}
            chunk = connection.execute(
                """
                SELECT * FROM chunks
                WHERE chapter_id = ? AND state = 'PENDING'
                ORDER BY chunk_number LIMIT 1
                """,
                (chapter["chapter_id"],),
            ).fetchone()
            if chunk is None:
                _refresh_chapters(connection)
                return {"command": "claim", "claimed": False, "reason": "NO_WORK", "run_id": run_id}
            attempt_number = int(chunk["attempts_used"]) + 1
            attempt_id = uuid.uuid4().hex
            now = _utc_now()
            deadline_at = _epoch_now() + _attempt_deadline_seconds(config)
            connection.execute(
                """
                INSERT INTO attempts(
                    attempt_id, run_id, worker_id, chunk_id, attempt_number, state, started_at, deadline_at
                ) VALUES (?, ?, ?, ?, ?, 'RUNNING', ?, ?)
                """,
                (attempt_id, run_id, worker_id, chunk["chunk_id"], attempt_number, now, deadline_at),
            )
            connection.execute(
                """
                UPDATE chunks
                SET state = 'RUNNING', attempts_used = ?, current_attempt_id = ?, current_worker_id = ?
                WHERE chunk_id = ? AND state = 'PENDING'
                """,
                (attempt_number, attempt_id, worker_id, chunk["chunk_id"]),
            )
            context_limit = _config_int(config, "chunking", "context_chunks", DEFAULT_CONTEXT_CHUNKS)
            context_chars = _config_int(config, "chunking", "context_chars", DEFAULT_CONTEXT_CHARS)
            previous = connection.execute(
                """
                SELECT c.chunk_id, t.text
                FROM chunks c JOIN translations t ON t.chunk_id = c.chunk_id
                WHERE c.chapter_id = ? AND c.chunk_number < ? AND c.state = 'DONE'
                ORDER BY c.chunk_number DESC LIMIT ?
                """,
                (chunk["chapter_id"], chunk["chunk_number"], context_limit),
            ).fetchall()
            context: list[dict[str, str]] = []
            used_chars = 0
            for previous_row in reversed(previous):
                text = str(previous_row["text"])
                remaining = context_chars - used_chars
                if remaining <= 0:
                    break
                text = text[:remaining]
                context.append({"chunk_id": str(previous_row["chunk_id"]), "text": text})
                used_chars += len(text)
            return {
                "command": "claim",
                "claimed": True,
                "run_id": run_id,
                "worker_id": worker_id,
                "attempt_id": attempt_id,
                "attempt_number": attempt_number,
                "deadline_at": deadline_at,
                "attempt_deadline_seconds": _attempt_deadline_seconds(config),
                "chapter_id": chunk["chapter_id"],
                "chunk_id": chunk["chunk_id"],
                "chunk_number": chunk["chunk_number"],
                "chapter_title": chapter["title"],
                "source": chunk["source"],
                "source_language": config.get("source_language", "auto"),
                "target_language": config.get("target_language", "zh-CN"),
                "context": context,
                "previous_context": context,
                "context_text": "\n\n".join(item["text"] for item in context),
            }
    finally:
        connection.close()


def _attempt_for_write(
    connection: sqlite3.Connection,
    run_id: str,
    worker_id: str,
    attempt_id: str,
    config: Mapping[str, Any],
) -> tuple[sqlite3.Row, sqlite3.Row]:
    run = _run_row(connection, run_id)
    if not _run_is_current(connection, run_id):
        raise ProjectError(f"run {run_id} is not accepting worker results (not the project's current run)")
    if run["status"] != "RUNNING":
        raise ProjectError(f"run {run_id} is not accepting worker results ({run['status']})")
    attempt = connection.execute(
        "SELECT * FROM attempts WHERE attempt_id = ? AND run_id = ? AND worker_id = ? AND state = 'RUNNING'",
        (attempt_id, run_id, worker_id),
    ).fetchone()
    if attempt is None:
        raise ProjectError("stale, interrupted, or mismatched attempt_id")
    if not _deadline_fresh(attempt):
        raise ProjectError("attempt deadline expired; result is stale")
    chunk = connection.execute(
        "SELECT * FROM chunks WHERE chunk_id = ? AND state = 'RUNNING' AND current_attempt_id = ? AND current_worker_id = ?",
        (attempt["chunk_id"], attempt_id, worker_id),
    ).fetchone()
    if chunk is None:
        raise ProjectError("attempt no longer owns its chunk")
    return run, chunk


def _read_translation_file(path: str, project_root: Path | None = None) -> str:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute() and project_root is not None:
        candidate = _safe_relative(project_root, str(candidate))
    try:
        data = candidate.read_bytes()
        text = data.decode("utf-8")
    except FileNotFoundError as exc:
        raise ProjectError(f"translation file not found: {path}") from exc
    except UnicodeDecodeError as exc:
        raise ProjectError("translation candidate must be UTF-8") from exc
    if not text.strip():
        raise ProjectError("translation candidate is empty")
    if "\x00" in text:
        raise ProjectError("translation candidate contains a NUL byte")
    return _normalise_text(text).rstrip()


def _numeric_tokens(text: str) -> list[str]:
    """Return canonical multi-digit tokens whose loss is usually an error."""
    # Unicode `\w` includes Han characters, so `1492年` must still count.
    matches = re.findall(r"(?<![0-9])[0-9][0-9,./:%-]*[0-9](?![0-9])", text)
    return [re.sub(r"\D", "", match) for match in matches]


def _mechanical_check(source: str, translation: str) -> None:
    source_tokens = Counter(_numeric_tokens(source))
    translation_tokens = Counter(_numeric_tokens(translation))
    missing = [token for token, count in source_tokens.items() if translation_tokens[token] < count]
    if missing:
        raise ProjectError(
            "mechanical QA failed: translation is missing source digit token(s): "
            + ", ".join(sorted(missing))
        )
    source_heading = re.match(r"^\s*(#{1,6})(?=\s)", source)
    if source_heading:
        translation_heading = re.match(r"^\s*(#{1,6})(?=\s)", translation)
        if not translation_heading or translation_heading.group(1) != source_heading.group(1):
            raise ProjectError(
                "mechanical QA failed: Markdown heading marker must be preserved on the first line"
            )


def _command_commit(project: str, run_id: str, worker_id: str, attempt_id: str, file_path: str) -> dict[str, Any]:
    _ensure_worker(worker_id)
    root, connection, _config = _open_project(project)
    try:
        guarded_root_thread_id, guarded_root_cwd, guarded_run_epoch = _run_guard_context(connection, run_id)
        if guarded_root_thread_id is not None:
            if guarded_root_cwd is None or guarded_run_epoch is None:
                raise ProjectError(f"guarded run {run_id} is missing its root fencing context")
            _root_guard_require_not_interrupted(guarded_root_cwd, guarded_root_thread_id)
            _root_guard_require_run_epoch(guarded_root_cwd, guarded_root_thread_id, guarded_run_epoch)
        # Relative candidate paths are resolved against the project first,
        # while absolute paths remain useful for a root-managed temp file.
        translation = _read_translation_file(file_path, root)
        with _transaction(
            connection,
            guarded_root_thread_id,
            guarded_root_cwd,
            check_interrupt_epoch=True,
            guarded_run_epoch=guarded_run_epoch,
        ):
            run, chunk = _attempt_for_write(connection, run_id, worker_id, attempt_id, _config)
            _mechanical_check(str(chunk["source"]), translation)
            now = _utc_now()
            connection.execute(
                "UPDATE attempts SET state = 'DONE', ended_at = ?, error = NULL WHERE attempt_id = ?",
                (now, attempt_id),
            )
            connection.execute(
                """
                INSERT INTO translations(chunk_id, attempt_id, text, text_hash, committed_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chunk_id) DO UPDATE SET
                    attempt_id = excluded.attempt_id,
                    text = excluded.text,
                    text_hash = excluded.text_hash,
                    committed_at = excluded.committed_at
                """,
                (chunk["chunk_id"], attempt_id, translation, _sha256_bytes(translation.encode("utf-8")), now),
            )
            connection.execute(
                """
                UPDATE chunks SET state = 'DONE', current_attempt_id = NULL, current_worker_id = NULL
                WHERE chunk_id = ? AND state = 'RUNNING' AND current_attempt_id = ?
                """,
                (chunk["chunk_id"], attempt_id),
            )
            _refresh_chapters(connection)
            status = _refresh_run_after_activity(connection, run_id)
            chapter = connection.execute(
                "SELECT state FROM chapters WHERE chapter_id = ?", (chunk["chapter_id"],)
            ).fetchone()[0]
        return {
            "command": "commit",
            "committed": True,
            "project": str(root),
            "run_id": run_id,
            "worker_id": worker_id,
            "attempt_id": attempt_id,
            "chunk_id": chunk["chunk_id"],
            "chapter_state": chapter,
            "run_status": status,
        }
    finally:
        connection.close()


def _command_fail(
    project: str, run_id: str, worker_id: str, attempt_id: str, error: str
) -> dict[str, Any]:
    _ensure_worker(worker_id)
    if not error.strip():
        raise ProjectError("--error must not be empty")
    root, connection, config = _open_project(project)
    try:
        guarded_root_thread_id, guarded_root_cwd, guarded_run_epoch = _run_guard_context(connection, run_id)
        if guarded_root_thread_id is not None:
            if guarded_root_cwd is None or guarded_run_epoch is None:
                raise ProjectError(f"guarded run {run_id} is missing its root fencing context")
            _root_guard_require_not_interrupted(guarded_root_cwd, guarded_root_thread_id)
            _root_guard_require_run_epoch(guarded_root_cwd, guarded_root_thread_id, guarded_run_epoch)
        with _transaction(
            connection,
            guarded_root_thread_id,
            guarded_root_cwd,
            check_interrupt_epoch=True,
            guarded_run_epoch=guarded_run_epoch,
        ):
            _run, chunk = _attempt_for_write(connection, run_id, worker_id, attempt_id, config)
            max_attempts = _config_int(config, "retry", "max_attempts", MAX_ATTEMPTS)
            failure_count = int(chunk["failure_count"]) + 1
            exhausted = failure_count >= max_attempts
            now = _utc_now()
            connection.execute(
                "UPDATE attempts SET state = 'FAILED', ended_at = ?, error = ? WHERE attempt_id = ?",
                (now, error, attempt_id),
            )
            connection.execute(
                """
                UPDATE chunks
                SET state = ?, failure_count = ?, current_attempt_id = NULL, current_worker_id = NULL
                WHERE chunk_id = ? AND current_attempt_id = ?
                """,
                ("FAILED" if exhausted else "PENDING", failure_count, chunk["chunk_id"], attempt_id),
            )
            _refresh_chapters(connection)
            status = _refresh_run_after_activity(connection, run_id)
        return {
            "command": "fail",
            "failed": True,
            "project": str(root),
            "run_id": run_id,
            "worker_id": worker_id,
            "attempt_id": attempt_id,
            "chunk_id": chunk["chunk_id"],
            "retryable": not exhausted,
            "run_status": status,
        }
    finally:
        connection.close()


def _stop_run_in_transaction(connection: sqlite3.Connection, run_id: str, error: str) -> dict[str, Any]:
    run = _run_row(connection, run_id)
    if run["status"] in ("COMPLETED", "STOPPED", "INTERRUPTED"):
        return {
            "status": str(run["status"]),
            "interrupted": 0,
            "cleanup_verified": run["cleanup_verified_at"] is not None,
        }
    now = _utc_now()
    # The terminal run write is in the same SQLite transaction as attempt
    # invalidation.  It happens first so no observer can see a live run after
    # its cleanup transaction commits.
    connection.execute(
        """
        UPDATE runs
        SET status = 'STOPPED', stop_requested = 1, ended_at = ?, cleanup_verified_at = NULL
        WHERE run_id = ? AND status IN ('RUNNING', 'STOPPING')
        """,
        (now, run_id),
    )
    interrupted = _interrupt_run_attempts(connection, run_id, error)
    connection.execute(
        "UPDATE chapters SET owner_run_id = NULL, owner_worker_id = NULL WHERE owner_run_id = ?",
        (run_id,),
    )
    _refresh_chapters(connection)
    if _run_is_current(connection, run_id):
        _set_current_run_id(connection, None)
    return {"status": "STOPPED", "interrupted": interrupted, "cleanup_verified": False}


def _command_stop(project: str, run_id: str, command: str = "stop") -> dict[str, Any]:
    root, connection, _config = _open_project(project)
    try:
        with _transaction(connection):
            result = _stop_run_in_transaction(connection, run_id, f"{command} requested")
        return {"command": command, "project": str(root), "run_id": run_id, **result}
    finally:
        connection.close()


def _command_stop_for_root(project: str, run_id: str, root_thread_id: str) -> dict[str, Any]:
    """Fence a run only when the active DB owner matches the root mapping."""
    root, connection, _config = _open_project(project)
    try:
        with _transaction(connection):
            current_run_id = _current_run_id(connection)
            run = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if (
                run is None
                or current_run_id != run_id
                or run["root_thread_id"] != root_thread_id
            ):
                return {
                    "command": "stop-for-root",
                    "project": str(root),
                    "run_id": run_id,
                    "root_thread_id": root_thread_id,
                    "stopped": False,
                    "reason": "ROOT_OR_RUN_MISMATCH",
                    "status": None if run is None else str(run["status"]),
                    "current_run_id": current_run_id,
                    "interrupted": 0,
                }
            result = _stop_run_in_transaction(connection, run_id, "root stop requested")
            stopped = str(run["status"]) in ("RUNNING", "STOPPING") and result["status"] == "STOPPED"
            return {
                "command": "stop-for-root",
                "project": str(root),
                "run_id": run_id,
                "root_thread_id": root_thread_id,
                "stopped": stopped,
                "reason": None if stopped else "ALREADY_TERMINAL",
                "current_run_id": _current_run_id(connection),
                **result,
            }
    finally:
        connection.close()


def _parse_worker_observations(
    released_workers: Sequence[str] | None,
    not_spawned_workers: Sequence[str] | None,
    worker_states: Sequence[str] | None,
) -> dict[str, str]:
    observations: dict[str, str] = {}

    def add(worker_id: str, state: str) -> None:
        _ensure_worker(worker_id)
        if worker_id in observations:
            raise ProjectError(f"duplicate cleanup observation for {worker_id}")
        observations[worker_id] = state

    for worker_id in released_workers or ():
        add(str(worker_id), "INACTIVE")
    for worker_id in not_spawned_workers or ():
        add(str(worker_id), "NOT_SPAWNED")
    allowed = {
        "inactive": "INACTIVE",
        "released": "INACTIVE",
        "active": "ACTIVE",
        "unknown": "UNKNOWN",
        "not_spawned": "NOT_SPAWNED",
        "not-spawned": "NOT_SPAWNED",
    }
    for item in worker_states or ():
        if "=" not in item:
            raise ProjectError("--worker-state must use WORKER_ID=inactive|active|unknown")
        worker_id, raw_state = item.split("=", 1)
        state = allowed.get(raw_state.strip().lower())
        if state is None:
            raise ProjectError(
                f"unknown worker state {raw_state!r}; use inactive, active, unknown, or not-spawned"
            )
        add(worker_id.strip(), state)
    return observations


def _command_confirm_cleanup(
    project: str,
    run_id: str,
    released_workers: Sequence[str] | None = None,
    not_spawned_workers: Sequence[str] | None = None,
    worker_states: Sequence[str] | None = None,
) -> dict[str, Any]:
    observations = _parse_worker_observations(released_workers, not_spawned_workers, worker_states)
    root, connection, _config = _open_project(project)
    try:
        failure: str | None = None
        verified_at: str | None = None
        observed_payload: dict[str, str] = {}
        with _transaction(connection):
            run = _run_row(connection, run_id)
            if run["status"] not in ("STOPPED", "INTERRUPTED", "COMPLETED"):
                raise ProjectError(
                    f"run {run_id} is not terminal; stop or invalidate it before confirming cleanup"
                )
            registry = connection.execute(
                "SELECT worker_id, canonical_path, expected_run_id, observed_state, observed_at "
                "FROM worker_registry ORDER BY worker_id"
            ).fetchall()
            if len(registry) != MAX_WORKERS or {str(row["worker_id"]) for row in registry} != set(WORKER_IDS):
                raise ProjectError("worker registry is incomplete; cannot confirm cleanup")
            mismatched = [
                str(row["worker_id"])
                for row in registry
                if row["expected_run_id"] not in (None, run_id)
            ]
            if mismatched:
                raise ProjectError(
                    "worker registry is bound to another run: " + ", ".join(sorted(mismatched))
                )
            observed_at = _utc_now()
            for worker_id, state in observations.items():
                connection.execute(
                    """
                    UPDATE worker_registry
                    SET expected_run_id = ?, observed_state = ?, observed_at = ?
                    WHERE worker_id = ?
                    """,
                    (run_id, state, observed_at, worker_id),
                )
            active = connection.execute(
                "SELECT COUNT(*) FROM attempts WHERE run_id = ? AND state = 'RUNNING'",
                (run_id,),
            ).fetchone()[0]
            registry = connection.execute(
                "SELECT worker_id, observed_state FROM worker_registry ORDER BY worker_id"
            ).fetchall()
            observed_payload = {str(row["worker_id"]): str(row["observed_state"] or "") for row in registry}
            missing = [worker_id for worker_id in WORKER_IDS if not observed_payload.get(worker_id)]
            unsafe = [
                worker_id
                for worker_id, state in observed_payload.items()
                if state not in ("INACTIVE", "NOT_SPAWNED")
            ]
            if active:
                failure = "cannot confirm cleanup while database attempts are RUNNING"
            elif missing:
                failure = "cleanup requires an explicit observation for every worker: " + ", ".join(missing)
            elif unsafe:
                failure = "cleanup requires all workers to be observed inactive or not-spawned: " + ", ".join(unsafe)
            else:
                verified_at = str(run["cleanup_verified_at"] or _utc_now())
                connection.execute(
                    "UPDATE runs SET cleanup_verified_at = ? WHERE run_id = ?",
                    (verified_at, run_id),
                )
        if failure is not None:
            raise ProjectError(failure)
        return {
            "command": "confirm-cleanup",
            "project": str(root),
            "run_id": run_id,
            "status": str(run["status"]),
            "cleanup_verified": True,
            "cleanup_verified_at": verified_at,
            "worker_observations": observed_payload,
        }
    finally:
        connection.close()


def _command_force_stop(project: str, run_id: str) -> dict[str, Any]:
    return _command_stop(project, run_id, command="force-stop")


def _command_invalidate_run(project: str, run_id: str) -> dict[str, Any]:
    root, connection, _config = _open_project(project)
    try:
        with _transaction(connection):
            run = _run_row(connection, run_id)
            if run["status"] not in ("RUNNING", "STOPPING"):
                return {
                    "command": "invalidate-run",
                    "project": str(root),
                    "run_id": run_id,
                    "status": str(run["status"]),
                    "interrupted": 0,
                    "cleanup_verified": run["cleanup_verified_at"] is not None,
                }
            interrupted = _invalidate_run_in_transaction(connection, run_id)
        return {
            "command": "invalidate-run",
            "project": str(root),
            "run_id": run_id,
            "status": "INTERRUPTED",
            "interrupted": interrupted,
            "cleanup_verified": False,
        }
    finally:
        connection.close()


def _command_interrupt_worker(project: str, run_id: str, worker_id: str) -> dict[str, Any]:
    _ensure_worker(worker_id)
    root, connection, _config = _open_project(project)
    try:
        with _transaction(connection):
            run = _run_row(connection, run_id)
            if not _run_is_current(connection, run_id) or run["status"] != "RUNNING":
                return {
                    "command": "interrupt-worker",
                    "interrupted": False,
                    "run_id": run_id,
                    "worker_id": worker_id,
                    "reason": "STALE_RUN",
                }
            attempt = _current_worker_attempt(connection, run_id, worker_id)
            if attempt is None:
                return {"command": "interrupt-worker", "interrupted": False, "run_id": run_id, "worker_id": worker_id}
            chunk = connection.execute(
                "SELECT chapter_id FROM chunks WHERE chunk_id = ?", (attempt["chunk_id"],)
            ).fetchone()
            if chunk is None:
                raise ProjectError("attempt references a missing chunk")
            now = _utc_now()
            connection.execute(
                "UPDATE attempts SET state = 'INTERRUPTED', ended_at = ?, error = ? WHERE attempt_id = ?",
                (now, "worker interrupted", attempt["attempt_id"]),
            )
            connection.execute(
                """
                UPDATE chunks SET state = 'INTERRUPTED', current_attempt_id = NULL, current_worker_id = NULL
                WHERE chunk_id = ? AND current_attempt_id = ?
                """,
                (attempt["chunk_id"], attempt["attempt_id"]),
            )
            # Keep chapter affinity for this run.  The interrupted chunk
            # blocks every further claim in the chapter until a supervised
            # new run normalizes INTERRUPTED chunks back to PENDING.
            _refresh_chapters(connection)
            status = _refresh_run_after_activity(connection, run_id)
        return {
            "command": "interrupt-worker",
            "interrupted": True,
            "project": str(root),
            "run_id": run_id,
            "worker_id": worker_id,
            "attempt_id": attempt["attempt_id"],
            "status": status,
        }
    finally:
        connection.close()


def _run_payload(
    run: sqlite3.Row | None,
    config: Mapping[str, Any],
    current_run_id: str | None = None,
) -> dict[str, Any] | None:
    if run is None:
        return None
    return {
        "run_id": run["run_id"],
        "status": run["status"],
        "stop_requested": bool(run["stop_requested"]),
        "created_at": run["created_at"],
        "ended_at": run["ended_at"],
        "started_at": run["started_at"],
        "root_thread_id": run["root_thread_id"],
        "root_guard_cwd": run["root_guard_cwd"],
        "root_guard_epoch": run["root_guard_epoch"],
        "cleanup_verified": run["cleanup_verified_at"] is not None,
        "cleanup_verified_at": run["cleanup_verified_at"],
        "current": current_run_id == run["run_id"],
        "attempt_deadline_seconds": _attempt_deadline_seconds(config),
    }


def _status_payload(root: Path, connection: sqlite3.Connection, config: Mapping[str, Any]) -> dict[str, Any]:
    run = _latest_run(connection)
    current_run_id = _current_run_id(connection)
    worker_rows = connection.execute(
        """
        SELECT worker_id, canonical_path, expected_run_id, observed_state, observed_at
        FROM worker_registry ORDER BY worker_id
        """
    ).fetchall()
    state_rows = connection.execute("SELECT state, COUNT(*) AS count FROM chunks GROUP BY state").fetchall()
    chapter_rows = connection.execute(
        "SELECT chapter_id, chapter_number, title, state, owner_worker_id FROM chapters ORDER BY chapter_number"
    ).fetchall()
    active_rows = connection.execute(
        "SELECT run_id, worker_id, attempt_id, chunk_id, started_at, deadline_at "
        "FROM attempts WHERE state = 'RUNNING' ORDER BY worker_id"
    ).fetchall()
    return {
        "command": "status",
        "project": str(root),
        "run": _run_payload(run, config, current_run_id),
        "root_thread_id": None if run is None else run["root_thread_id"],
        "root_guard_cwd": None if run is None else run["root_guard_cwd"],
        "root_guard_epoch": None if run is None else run["root_guard_epoch"],
        "current_run_id": current_run_id,
        "workers": [
            {
                "worker_id": row["worker_id"],
                "canonical_path": row["canonical_path"],
                "expected_run_id": row["expected_run_id"],
                "observed_state": row["observed_state"],
                "observed_at": row["observed_at"],
            }
            for row in worker_rows
        ],
        "chunks": {
            "total": sum(int(row["count"]) for row in state_rows),
            "states": {str(row["state"]): int(row["count"]) for row in state_rows},
        },
        "chapters": [
            {
                "chapter_id": row["chapter_id"],
                "chapter_number": row["chapter_number"],
                "title": row["title"],
                "state": row["state"],
                "owner_worker_id": row["owner_worker_id"],
            }
            for row in chapter_rows
        ],
        "running_attempts": [
            {
                "run_id": row["run_id"],
                "worker_id": row["worker_id"],
                "attempt_id": row["attempt_id"],
                "chunk_id": row["chunk_id"],
                "started_at": row["started_at"],
                "deadline_at": row["deadline_at"],
            }
            for row in active_rows
        ],
        "running_attempt_count": len(active_rows),
    }


def _command_status(project: str) -> dict[str, Any]:
    root, connection, config = _open_project(project)
    try:
        return _status_payload(root, connection, config)
    finally:
        connection.close()


def _command_check_run(project: str, run_id: str) -> dict[str, Any]:
    root, connection, config = _open_project(project)
    try:
        run = _run_row(connection, run_id)
        current_run_id = _current_run_id(connection)
        payload = _run_payload(run, config, current_run_id)
        assert payload is not None
        current = current_run_id == run_id
        guard_clear = True
        guarded_root_thread_id, guarded_root_cwd, guarded_run_epoch = _run_guard_context(connection, run_id)
        if guarded_root_thread_id is not None:
            if guarded_root_cwd is None or guarded_run_epoch is None:
                raise ProjectError(f"guarded run {run_id} is missing its root fencing context")
            try:
                _root_guard_require_not_interrupted(guarded_root_cwd, guarded_root_thread_id)
                _root_guard_require_run_epoch(guarded_root_cwd, guarded_root_thread_id, guarded_run_epoch)
            except ProjectError:
                guard_clear = False
        may_claim = bool(
            guard_clear and current and run["status"] == "RUNNING" and not run["stop_requested"]
        )
        may_commit = bool(guard_clear and current and run["status"] == "RUNNING")
        return {
            "command": "check-run",
            "project": str(root),
            "run": payload,
            # Keep lifecycle decision fields at the top level so a worker can
            # consume this command without depending on the larger status
            # envelope.
            "run_id": payload["run_id"],
            "root_thread_id": payload["root_thread_id"],
            "root_guard_cwd": payload["root_guard_cwd"],
            "root_guard_epoch": payload["root_guard_epoch"],
            "status": payload["status"],
            "stop_requested": payload["stop_requested"],
            "current": current,
            "cleanup_verified": payload["cleanup_verified"],
            "cleanup_verified_at": payload["cleanup_verified_at"],
            "may_claim": may_claim,
            "may_commit": may_commit,
            "attempt_deadline_seconds": payload["attempt_deadline_seconds"],
        }
    finally:
        connection.close()


def _edit_for(root: Path, chunk_id: str) -> str | None:
    edits = root / "edits"
    for suffix in (".txt", ".md", ""):
        candidate = edits / f"{chunk_id}{suffix}"
        if candidate.is_file():
            try:
                text = _normalise_text(candidate.read_bytes().decode("utf-8")).rstrip()
            except (OSError, UnicodeDecodeError) as exc:
                raise ProjectError(f"invalid edit file: {candidate}") from exc
            if not text:
                raise ProjectError(f"edit file is empty: {candidate}")
            return text
    return None


def _reserve_output_path(
    connection: sqlite3.Connection, output_dir: Path, root: Path, stem: str
) -> tuple[str, Path]:
    """Reserve the next version while holding the SQLite writer lock."""
    pattern = re.compile(rf"^{re.escape(stem)}\.v(\d+)\.md$")
    versions = []
    for candidate in output_dir.iterdir():
        match = pattern.match(candidate.name)
        if match:
            versions.append(int(match.group(1)))
    reserved = {
        str(row["output_path"])
        for row in connection.execute("SELECT output_path FROM builds").fetchall()
    }
    version = max(versions, default=0) + 1
    while True:
        output_path = output_dir / f"{stem}.v{version:03d}.md"
        relative_path = str(output_path.relative_to(root))
        if not output_path.exists() and relative_path not in reserved:
            build_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO builds(build_id, output_path, created_at) VALUES (?, ?, ?)",
                (build_id, relative_path, _utc_now()),
            )
            return build_id, output_path
        version += 1


def _command_build(project: str) -> dict[str, Any]:
    root, connection, config = _open_project(project)
    try:
        rows = connection.execute(
            """
            SELECT c.chunk_id, c.source, c.state, t.text
            FROM chunks c
            JOIN chapters h ON h.chapter_id = c.chapter_id
            LEFT JOIN translations t ON t.chunk_id = c.chunk_id
            ORDER BY h.chapter_number, c.chunk_number
            """
        ).fetchall()
        if not rows or any(row["state"] != "DONE" or row["text"] is None for row in rows):
            raise ProjectError("cannot build until every chunk is DONE")
        parts: list[str] = []
        for row in rows:
            text = _edit_for(root, str(row["chunk_id"])) or str(row["text"])
            if text.strip():
                parts.append(text.rstrip())
        content = "\n\n".join(parts).rstrip() + "\n"
        source_name = Path(str(config["source_relpath"])).name
        stem = Path(source_name).stem
        output_dir = root / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        with _transaction(connection):
            build_id, output_path = _reserve_output_path(connection, output_dir, root, stem)
        try:
            _atomic_write(output_path, content.encode("utf-8"))
        except BaseException:
            # A failed write must not permanently consume a version when the
            # process is still alive to clean up.  A process crash leaves the
            # reservation as a harmless skipped version.
            with _transaction(connection):
                connection.execute("DELETE FROM builds WHERE build_id = ?", (build_id,))
            raise
        return {
            "command": "build",
            "built": True,
            "project": str(root),
            "build_id": build_id,
            "output_relpath": str(output_path.relative_to(root)),
            "output": str(output_path),
            "chunks": len(rows),
            "edits_applied": sum(1 for row in rows if _edit_for(root, str(row["chunk_id"])) is not None),
        }
    finally:
        connection.close()


def _command_retry_failed(project: str) -> dict[str, Any]:
    root, connection, config = _open_project(project)
    try:
        with _transaction(connection):
            run = _latest_run(connection)
            if run is None:
                raise ProjectError("start the project before retrying failed chunks")
            active = connection.execute("SELECT COUNT(*) FROM attempts WHERE state = 'RUNNING'").fetchone()[0]
            if active:
                raise ProjectError("stop or interrupt all active workers before retry-failed")
            if run["status"] in ("STOPPED", "INTERRUPTED") and run["cleanup_verified_at"] is None:
                raise ProjectError("confirm cleanup before retry-failed can reset chunks")
            rows = connection.execute("SELECT chunk_id FROM chunks WHERE state = 'FAILED'").fetchall()
            if rows and run["status"] == "RUNNING":
                _stop_run_in_transaction(connection, str(run["run_id"]), "retry-failed requested")
            for row in rows:
                connection.execute(
                    """
                    UPDATE chunks SET state = 'PENDING', attempts_used = 0, failure_count = 0,
                        current_attempt_id = NULL, current_worker_id = NULL
                    WHERE chunk_id = ?
                    """,
                    (row["chunk_id"],),
                )
            if rows:
                connection.execute(
                    "UPDATE chapters SET state = 'PENDING', owner_run_id = NULL, owner_worker_id = NULL WHERE state = 'FAILED'"
                )
                connection.execute(
                    """
                    UPDATE runs SET status = 'STOPPED', stop_requested = 1, ended_at = ?, cleanup_verified_at = NULL
                    WHERE run_id = ? AND status IN ('RUNNING', 'STOPPING')
                    """,
                    (_utc_now(), run["run_id"]),
                )
                if _run_is_current(connection, str(run["run_id"])):
                    _set_current_run_id(connection, None)
            _refresh_chapters(connection)
            current = _run_row(connection, run["run_id"])
            status = str(current["status"])
        return {
            "command": "retry-failed",
            "project": str(root),
            "run_id": run["run_id"],
            "retried": len(rows),
            "status": status,
            "requires_new_run": bool(rows),
            "max_attempts": _config_int(config, "retry", "max_attempts", MAX_ATTEMPTS),
        }
    finally:
        connection.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SQLite state engine for a four-worker book translation pilot")
    commands = parser.add_subparsers(dest="command", required=True)

    plan = commands.add_parser("plan")
    plan.add_argument("source")
    plan.add_argument("--chunking-version", type=int, choices=(1, 2), default=DEFAULT_CHUNKING_VERSION)

    init = commands.add_parser("init")
    init.add_argument("source")
    init.add_argument("--project", required=True)
    init.add_argument("--source-language", default="auto")
    init.add_argument("--target-language", default="zh-CN")
    init.add_argument("--chunking-version", type=int, choices=(1, 2), default=DEFAULT_CHUNKING_VERSION)
    init.add_argument("--expected-source-sha256")
    init.add_argument("--expected-plan-sha256")

    start = commands.add_parser("start")
    start.add_argument("project")
    start.add_argument(
        "--root-agent-path",
        default="/root",
        help="parent path used to form the four expected worker identities (default: /root)",
    )

    claim = commands.add_parser("claim")
    claim.add_argument("project")
    claim.add_argument("--run-id", required=True)
    claim.add_argument("--worker-id", required=True)

    commit = commands.add_parser("commit")
    commit.add_argument("project")
    commit.add_argument("--run-id", required=True)
    commit.add_argument("--worker-id", required=True)
    commit.add_argument("--attempt-id", required=True)
    commit.add_argument("--file", required=True)

    fail = commands.add_parser("fail")
    fail.add_argument("project")
    fail.add_argument("--run-id", required=True)
    fail.add_argument("--worker-id", required=True)
    fail.add_argument("--attempt-id", required=True)
    fail.add_argument("--error", required=True)

    for name in ("stop", "force-stop"):
        command = commands.add_parser(name)
        command.add_argument("project")
        command.add_argument("--run-id", required=True)

    stop_for_root = commands.add_parser("stop-for-root")
    stop_for_root.add_argument("project")
    stop_for_root.add_argument("--run-id", required=True)
    stop_for_root.add_argument("--root-thread-id", required=True)

    cleanup = commands.add_parser("confirm-cleanup")
    cleanup.add_argument("project")
    cleanup.add_argument("--run-id", required=True)
    cleanup.add_argument("--released-worker", action="append", default=[])
    cleanup.add_argument("--not-spawned-worker", action="append", default=[])
    cleanup.add_argument("--worker-state", action="append", default=[])

    invalidate = commands.add_parser("invalidate-run")
    invalidate.add_argument("project")
    invalidate.add_argument("--run-id", required=True)

    check_run = commands.add_parser("check-run")
    check_run.add_argument("project")
    check_run.add_argument("--run-id", required=True)

    interrupt = commands.add_parser("interrupt-worker")
    interrupt.add_argument("project")
    interrupt.add_argument("--run-id", required=True)
    interrupt.add_argument("--worker-id", required=True)

    status = commands.add_parser("status")
    status.add_argument("project")

    build = commands.add_parser("build")
    build.add_argument("project")

    retry = commands.add_parser("retry-failed")
    retry.add_argument("project")
    return parser


def _dispatch(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "plan":
        return _command_plan(args.source, args.chunking_version)
    if args.command == "init":
        return _command_init(
            args.source, args.project, args.target_language, args.source_language,
            args.chunking_version, args.expected_source_sha256, args.expected_plan_sha256,
        )
    if args.command == "start":
        return _command_start(args.project, args.root_agent_path)
    if args.command == "claim":
        return _command_claim(args.project, args.run_id, args.worker_id)
    if args.command == "commit":
        return _command_commit(args.project, args.run_id, args.worker_id, args.attempt_id, args.file)
    if args.command == "fail":
        return _command_fail(args.project, args.run_id, args.worker_id, args.attempt_id, args.error)
    if args.command == "stop":
        return _command_stop(args.project, args.run_id)
    if args.command == "force-stop":
        return _command_force_stop(args.project, args.run_id)
    if args.command == "stop-for-root":
        return _command_stop_for_root(args.project, args.run_id, args.root_thread_id)
    if args.command == "confirm-cleanup":
        return _command_confirm_cleanup(
            args.project,
            args.run_id,
            args.released_worker,
            args.not_spawned_worker,
            args.worker_state,
        )
    if args.command == "invalidate-run":
        return _command_invalidate_run(args.project, args.run_id)
    if args.command == "check-run":
        return _command_check_run(args.project, args.run_id)
    if args.command == "interrupt-worker":
        return _command_interrupt_worker(args.project, args.run_id, args.worker_id)
    if args.command == "status":
        return _command_status(args.project)
    if args.command == "build":
        return _command_build(args.project)
    if args.command == "retry-failed":
        return _command_retry_failed(args.project)
    raise ProjectError(f"unknown command: {args.command}")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        result = _dispatch(args)
    except (ProjectError, OSError, sqlite3.Error) as exc:
        print(_dump({"error": str(exc)}), file=sys.stderr)
        return 2
    print(_dump(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
