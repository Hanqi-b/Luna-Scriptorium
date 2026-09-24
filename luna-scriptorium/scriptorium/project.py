"""SQLite project state, fenced attempts, review acceptance, and commands."""
from __future__ import annotations

from pathlib import Path
import json
import sqlite3
import uuid
from typing import Any, Mapping, Sequence
from .common import (ProjectError, SCHEMA_VERSION, MAX_WORKERS, WORKER_IDS, WORKER_CANONICAL_PATHS,
                     MAX_ATTEMPTS, DEFAULT_MAX_CHARS, DEFAULT_CHUNKING_VERSION, DEFAULT_CONTEXT_CHUNKS, DEFAULT_CONTEXT_CHARS,
                     _utc_now, _sha256_bytes, _sha256_file, _atomic_write, _root_thread_id,
                     _safe_relative, _connect, _transaction, _config_bytes, _config_int,
                     _worker_paths, _ensure_worker)
from .chunks import _source_plan, _plan_hash, select_context
from .qa import _read_translation_file, _mechanical_check
from .build import effective_book, write_final

def _ensure_project_layout(root: Path) -> None:
    for name in ("source", "output", "work/attempts"):
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
            CREATE TABLE accepted_edits (
                chunk_id TEXT PRIMARY KEY REFERENCES chunks(chunk_id),
                text TEXT NOT NULL,
                text_hash TEXT NOT NULL,
                base_translation_hash TEXT NOT NULL,
                stage TEXT NOT NULL,
                review_unit_id TEXT NOT NULL,
                source_review_hash TEXT NOT NULL,
                accepted_at TEXT NOT NULL
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


def _command_init(
    source: str,
    project: str,
    target_language: str,
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
        connection.execute("""CREATE TABLE IF NOT EXISTS accepted_edits (
            chunk_id TEXT PRIMARY KEY REFERENCES chunks(chunk_id), text TEXT NOT NULL,
            text_hash TEXT NOT NULL, base_translation_hash TEXT NOT NULL,
            stage TEXT NOT NULL, review_unit_id TEXT NOT NULL,
            source_review_hash TEXT NOT NULL, accepted_at TEXT NOT NULL)""")
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
    claimable = connection.execute(
        """SELECT COUNT(*) FROM chunks c JOIN chapters h ON h.chapter_id=c.chapter_id
           WHERE c.state='PENDING' AND h.state!='FAILED'"""
    ).fetchone()[0]
    active = connection.execute("SELECT COUNT(*) FROM attempts WHERE run_id=? AND state='RUNNING'", (run_id,)).fetchone()[0]
    if not claimable and not active:
        connection.execute("UPDATE runs SET status='FAILED',stop_requested=1,ended_at=? WHERE run_id=?", (_utc_now(), run_id))
        if _current_run_id(connection) == run_id:
            _set_current_run_id(connection, None)
        return "FAILED"
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
            connection.execute("UPDATE chunks SET state='PENDING',current_attempt_id=NULL,current_worker_id=NULL WHERE state IN ('PENDING','RUNNING')")
            connection.execute("UPDATE chapters SET state='PENDING',owner_run_id=NULL,owner_worker_id=NULL WHERE state NOT IN ('DONE','FAILED')")
            _refresh_chapters(connection)
            claimable = connection.execute(
                """SELECT COUNT(*) FROM chunks c JOIN chapters h ON h.chapter_id=c.chapter_id
                   WHERE c.state='PENDING' AND h.state!='FAILED'"""
            ).fetchone()[0]
            if not claimable:
                return {"command":"start","project":str(root),"status":"NEEDS_RETRY","run_id":None,"failed_chunks":total-done}
            run_id = uuid.uuid4().hex
            connection.execute("INSERT INTO runs(run_id,status,stop_requested,created_at,root_thread_id) VALUES (?,'RUNNING',0,?,?)", (run_id,_utc_now(),_root_thread_id()))
            for worker_id, path in worker_paths.items():
                connection.execute("INSERT INTO worker_registry(worker_id,canonical_path,expected_run_id) VALUES (?,?,?) ON CONFLICT(worker_id) DO UPDATE SET canonical_path=excluded.canonical_path,expected_run_id=excluded.expected_run_id", (worker_id,path,run_id))
            _set_current_run_id(connection, run_id)
        return {"command":"start","project":str(root),"run_id":run_id,"status":"RUNNING","remaining_chunks":total-done,"workers":list(WORKER_IDS),"worker_paths":worker_paths,"source_language":config["source_language"],"target_language":config["target_language"]}
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
            context = select_context(prior, context_chars)
            return {"command":"claim","claimed":True,"run_id":run_id,"worker_id":worker_id,"attempt_id":attempt_id,"attempt_number":attempt_number,"chapter_id":chunk["chapter_id"],"chunk_id":chunk["chunk_id"],"chunk_number":chunk["chunk_number"],"chapter_title":chapter["title"],"source":chunk["source"],"source_language":config["source_language"],"target_language":config["target_language"],"context":context}
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


def _replace_qa_flags(connection: sqlite3.Connection, chunk_id: str, missing: list[str]) -> None:
    """Flags describe the effective translation, never the first draft."""
    connection.execute("DELETE FROM qa_flags WHERE chunk_id=?", (chunk_id,))
    if missing:
        connection.execute(
            "INSERT INTO qa_flags(chunk_id,code,details) VALUES (?,'SUSPECT_MISSING_NUMBERS',?)",
            (chunk_id, ",".join(missing)),
        )


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
            _replace_qa_flags(connection, chunk["chunk_id"], missing)
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
    root, connection, config = _open_project(project)
    try:
        payload = _status_payload(root,connection)
        payload["source_language"] = config["source_language"]
        payload["target_language"] = config["target_language"]
        payload["accepted_edit_count"] = connection.execute("SELECT COUNT(*) FROM accepted_edits").fetchone()[0]
        return payload
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
            if stage == "consistency":
                _require_snapshot(root, connection)
            unit = connection.execute("SELECT status FROM reviews WHERE stage=? AND unit_id=?",(stage,unit_id)).fetchone()
            if unit is None:
                raise ProjectError(f"unknown review unit: {stage}/{unit_id}")
            if unit[0]=="DONE":
                raise ProjectError("review unit already DONE")
            report_hash = _sha256_bytes(report.encode("utf-8"))
            edit_hashes = {
                row[0] for row in connection.execute(
                    "SELECT source_review_hash FROM accepted_edits WHERE stage=? AND review_unit_id=?",
                    (stage, unit_id),
                )
            }
            if edit_hashes and edit_hashes != {report_hash}:
                raise ProjectError("review report differs from accepted edit provenance")
            connection.execute("UPDATE reviews SET status='DONE',report=?,completed_at=? WHERE stage=? AND unit_id=?",(report,_utc_now(),stage,unit_id))
        return {"command":"review-done","project":str(root),"stage":stage,"unit_id":unit_id,"done":True}
    finally:
        connection.close()


def _require_snapshot(root: Path, connection: sqlite3.Connection) -> str:
    row = connection.execute("SELECT value FROM meta WHERE key='consistency_snapshot_hash'").fetchone()
    path = root / "work" / "current-book.md"
    if row is None or not path.is_file() or _sha256_file(path) != row[0]:
        raise ProjectError("create an intact current-book snapshot before consistency review")
    return str(path)


def _command_snapshot(project: str) -> dict[str, Any]:
    root, connection, config = _open_project(project)
    try:
        with _transaction(connection):
            _verify_stored_plan(connection, config)
            if connection.execute("SELECT COUNT(*) FROM reviews WHERE stage='chapter' AND status!='DONE'").fetchone()[0]:
                raise ProjectError("finish chapter review before snapshot")
            if connection.execute("SELECT value FROM meta WHERE key='consistency_snapshot_hash'").fetchone():
                path = _require_snapshot(root, connection)
                return {"command": "snapshot", "project": str(root), "output": path, "existing": True}
            content, chunks, edits = effective_book(connection)
            path = root / "work" / "current-book.md"
            _atomic_write(path, content.encode("utf-8"))
            digest = _sha256_bytes(content.encode("utf-8"))
            connection.execute(
                "INSERT INTO meta(key,value) VALUES ('consistency_snapshot_hash',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (digest,)
            )
        return {"command": "snapshot", "project": str(root), "output": str(path), "sha256": digest, "chunks": chunks, "edits_applied": edits}
    finally:
        connection.close()


def _command_apply_edit(
    project: str, stage: str, unit_id: str, chunk_id: str, file_path: str, review_file: str
) -> dict[str, Any]:
    if stage not in ("chapter", "consistency"):
        raise ProjectError("edit stage must be chapter or consistency")
    root, connection, config = _open_project(project)
    try:
        translation = _read_translation_file(file_path, root)
        report = _read_translation_file(review_file, root)
        with _transaction(connection):
            _verify_stored_plan(connection, config)
            if connection.execute("SELECT COUNT(*) FROM chunks WHERE state!='DONE'").fetchone()[0]:
                raise ProjectError("finish translation before applying a review edit")
            unit = connection.execute(
                "SELECT status FROM reviews WHERE stage=? AND unit_id=?", (stage, unit_id)
            ).fetchone()
            if unit is None or unit["status"] != "PENDING":
                raise ProjectError("review unit is missing or already DONE")
            chunk = connection.execute(
                """SELECT c.chunk_id,c.chapter_id,c.source,h.chapter_number,
                          t.text_hash AS base_hash
                   FROM chunks c JOIN chapters h ON h.chapter_id=c.chapter_id
                   JOIN translations t ON t.chunk_id=c.chunk_id
                   WHERE c.chunk_id=? AND c.state='DONE'""", (chunk_id,)
            ).fetchone()
            if chunk is None:
                raise ProjectError("edit requires an existing DONE chunk")
            expected_unit = chunk["chapter_id"] if stage == "chapter" else f"batch{(chunk['chapter_number']-1)//4+1:03d}"
            if unit_id != expected_unit:
                raise ProjectError("edit chunk is outside the assigned review unit")
            if stage == "chapter":
                if connection.execute("SELECT value FROM meta WHERE key='consistency_snapshot_hash'").fetchone():
                    raise ProjectError("chapter edits are closed after the consistency snapshot")
            else:
                if connection.execute("SELECT COUNT(*) FROM reviews WHERE stage='chapter' AND status!='DONE'").fetchone()[0]:
                    raise ProjectError("finish chapter review before consistency edits")
                _require_snapshot(root, connection)
            missing = _mechanical_check(str(chunk["source"]), translation)
            edit_hash = _sha256_bytes(translation.encode("utf-8"))
            report_hash = _sha256_bytes(report.encode("utf-8"))
            connection.execute(
                """INSERT INTO accepted_edits(
                       chunk_id,text,text_hash,base_translation_hash,stage,
                       review_unit_id,source_review_hash,accepted_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(chunk_id) DO UPDATE SET
                       text=excluded.text,text_hash=excluded.text_hash,
                       base_translation_hash=excluded.base_translation_hash,
                       stage=excluded.stage,review_unit_id=excluded.review_unit_id,
                       source_review_hash=excluded.source_review_hash,
                       accepted_at=excluded.accepted_at""",
                (chunk_id,translation,edit_hash,chunk["base_hash"],stage,unit_id,report_hash,_utc_now()),
            )
            _replace_qa_flags(connection, chunk_id, missing)
        return {"command": "apply-edit", "accepted": True, "chunk_id": chunk_id,
                "stage": stage, "unit_id": unit_id, "edit_sha256": edit_hash,
                "source_review_sha256": report_hash, "suspect_missing_numbers": missing}
    finally:
        connection.close()


def _command_retry_failed(project: str, chunk_id: str, reason: str) -> dict[str, Any]:
    if not reason.strip():
        raise ProjectError("--reason must explain the corrected retry")
    root, connection, config = _open_project(project)
    try:
        with _transaction(connection):
            _verify_stored_plan(connection, config)
            if _current_run_id(connection) is not None:
                raise ProjectError("stop or finish the active run before retry-failed")
            chunk = connection.execute(
                "SELECT chapter_id,state FROM chunks WHERE chunk_id=?", (chunk_id,)
            ).fetchone()
            if chunk is None or chunk["state"] != "FAILED":
                raise ProjectError("retry-failed requires a FAILED chunk")
            connection.execute("UPDATE chunks SET state='PENDING',failure_count=0 WHERE chunk_id=?", (chunk_id,))
            connection.execute(
                """UPDATE chapters SET state='PENDING',owner_run_id=NULL,owner_worker_id=NULL
                   WHERE chapter_id=? AND NOT EXISTS
                   (SELECT 1 FROM chunks WHERE chapter_id=? AND state='FAILED')""",
                (chunk["chapter_id"],chunk["chapter_id"]),
            )
        return {"command": "retry-failed", "project": str(root), "chunk_id": chunk_id,
                "reset": True, "reason": reason}
    finally:
        connection.close()


def _command_build(project: str) -> dict[str, Any]:
    root, connection, config = _open_project(project)
    try:
        with _transaction(connection):
            _verify_stored_plan(connection, config)
            if connection.execute("SELECT COUNT(*) FROM reviews WHERE status!='DONE'").fetchone()[0]:
                raise ProjectError("cannot build until chapter and consistency reviews are DONE")
            _require_snapshot(root, connection)
            return write_final(root, connection, config)
    finally:
        connection.close()
