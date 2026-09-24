"""Small shared primitives for Scriptorium state and planning."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any, Iterator, Mapping

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
    """An expected project or state error."""

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


def _ensure_worker(worker_id: str) -> None:
    if worker_id not in WORKER_IDS:
        raise ProjectError(f"worker_id must be one of: {', '.join(WORKER_IDS)}")
