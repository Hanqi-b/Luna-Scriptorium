"""Assemble the current accepted translation and write durable book views."""
from __future__ import annotations

from pathlib import Path
import re
import sqlite3
from typing import Any, Mapping
import uuid

from .common import ProjectError, _atomic_write, _sha256_bytes, _utc_now


def effective_book(connection: sqlite3.Connection) -> tuple[str, int, int]:
    rows = connection.execute(
        """SELECT c.chunk_id, c.state, t.text AS original_text,
                  t.text_hash AS original_hash, e.text AS edited_text,
                  e.text_hash AS edit_hash, e.base_translation_hash AS base_hash
           FROM chunks c
           JOIN chapters h ON h.chapter_id = c.chapter_id
           LEFT JOIN translations t ON t.chunk_id = c.chunk_id
           LEFT JOIN accepted_edits e ON e.chunk_id = c.chunk_id
           ORDER BY h.chapter_number, c.chunk_number"""
    ).fetchall()
    if not rows or any(row["state"] != "DONE" or row["original_text"] is None for row in rows):
        raise ProjectError("cannot assemble until every chunk is DONE")
    parts: list[str] = []
    edits = 0
    for row in rows:
        original = str(row["original_text"])
        if _sha256_bytes(original.encode("utf-8")) != row["original_hash"]:
            raise ProjectError(f"translation integrity check failed: {row['chunk_id']}")
        edited = row["edited_text"]
        if edited is not None:
            if row["base_hash"] != row["original_hash"] or _sha256_bytes(str(edited).encode("utf-8")) != row["edit_hash"]:
                raise ProjectError(f"accepted edit integrity check failed: {row['chunk_id']}")
            edits += 1
        parts.append(str(edited if edited is not None else original).rstrip())
    return "\n\n".join(parts).rstrip() + "\n", len(rows), edits


def write_final(root: Path, connection: sqlite3.Connection, config: Mapping[str, Any]) -> dict[str, Any]:
    content, chunks, edits = effective_book(connection)
    output_dir = root / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(str(config["source_relpath"])).stem
    pattern = re.compile(re.escape(stem) + r"\.v(\d+)\.md")
    versions = [int(match.group(1)) for path in output_dir.iterdir() if (match := pattern.fullmatch(path.name))]
    version = max(versions, default=0) + 1
    while (output_dir / f"{stem}.v{version:03d}.md").exists():
        version += 1
    path = output_dir / f"{stem}.v{version:03d}.md"
    _atomic_write(path, content.encode("utf-8"))
    connection.execute(
        "INSERT INTO builds(build_id,output_path,created_at) VALUES (?,?,?)",
        (uuid.uuid4().hex, str(path.relative_to(root)), _utc_now()),
    )
    return {"command": "build", "built": True, "project": str(root), "output": str(path), "chunks": chunks, "edits_applied": edits}
