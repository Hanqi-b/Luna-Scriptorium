#!/usr/bin/env python3
"""Fence a translation run when its Codex root turn ends or is interrupted."""

from __future__ import annotations

from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from typing import Any


HOOK_VERSION = 1
SESSION_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _log(base: Path, event: str, session_id: str, detail: str) -> None:
    base.mkdir(parents=True, exist_ok=True)
    with (base / "events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({
            "at": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "session_id": session_id,
            "detail": detail,
        }, ensure_ascii=False, sort_keys=True) + "\n")


@contextmanager
def _session_lock(base: Path, session_id: str, deadline: float):
    lock_path = base / "locks" / f"{session_id}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as stream:
        while True:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("timed out waiting for root start/stop lock")
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def main() -> int:
    event: dict[str, Any] = json.load(sys.stdin)
    name = str(event.get("hook_event_name") or "")
    session_id = str(event.get("session_id") or "")
    cwd_text = str(event.get("cwd") or "")
    cwd_path = Path(cwd_text)
    if (
        not SESSION_PATTERN.fullmatch(session_id)
        or not cwd_text
        or not cwd_path.is_absolute()
        or not cwd_path.is_dir()
    ):
        if name == "Stop":
            print(json.dumps({"decision": "block", "reason": "Invalid translation hook session or cwd"}))
        elif name == "Interrupt":
            print(json.dumps({"systemMessage": "Invalid translation hook session or cwd"}))
        else:
            print(json.dumps({"continue": False, "stopReason": "Invalid translation hook session or cwd"}))
        return 0
    cwd = cwd_path.resolve()

    base = cwd / ".codex" / "translate-book-guard"
    if name == "SessionStart":
        _write_json(base / "ready" / f"{session_id}.json", {
            "session_id": session_id,
            "cwd": str(cwd),
            "hook_version": HOOK_VERSION,
        })
        _log(base, name, session_id, "ready")
        print("{}")
        return 0

    if name not in ("Stop", "Interrupt") or event.get("agent_id"):
        print("{}")
        return 0

    errors: list[str] = []
    deadline = time.monotonic() + (2.3 if name == "Interrupt" else 18.0)
    interrupted_path = base / "interrupted" / f"{session_id}.json"
    # Stop and Interrupt must both invalidate a start that began before this
    # hook, even if that start has not published an active mapping yet.
    # Append before acquiring the session lock so a slow start cannot outrun
    # the hook's deadline. The log is never cleared: later starts snapshot
    # its new generation at command entry.
    try:
        epoch_path = base / "interrupt-epochs" / f"{session_id}.log"
        epoch_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(epoch_path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        try:
            os.write(descriptor, b"!")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if name == "Interrupt":
            _write_json(interrupted_path, {
                "session_id": session_id,
                "cwd": str(cwd),
                "hook_version": HOOK_VERSION,
            })
            _log(base, name, session_id, "interrupt marker published")
    except OSError as exc:
        errors.append(f"unable to publish cancellation epoch/marker: {exc}")
    try:
        with _session_lock(base, session_id, deadline):
            mapping_dir = base / "active" / session_id
            mapping_paths = sorted(mapping_dir.glob("*.json")) if mapping_dir.is_dir() else []
            legacy_path = base / "active" / f"{session_id}.json"
            if legacy_path.is_file():
                mapping_paths.append(legacy_path)
            if name == "Interrupt" and len(mapping_paths) > 1:
                with ThreadPoolExecutor(max_workers=min(8, len(mapping_paths))) as pool:
                    jobs = {pool.submit(_fence_mapping, base, cwd, name, session_id, path, deadline): path for path in mapping_paths}
                    for job in as_completed(jobs):
                        path = jobs[job]
                        try:
                            job.result()
                            path.unlink(missing_ok=True)
                        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired, RuntimeError) as exc:
                            _log(base, name, session_id, f"error: {exc}")
                            errors.append(f"{path.name}: {exc}")
            else:
                for mapping_path in mapping_paths:
                    try:
                        _fence_mapping(base, cwd, name, session_id, mapping_path, deadline)
                        mapping_path.unlink(missing_ok=True)
                    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired, RuntimeError) as exc:
                        _log(base, name, session_id, f"error: {exc}")
                        errors.append(f"{mapping_path.name}: {exc}")
            if name == "Stop" and not errors:
                interrupted_path.unlink(missing_ok=True)
                _write_json(base / "stop-ready" / f"{session_id}.json", {
                    "session_id": session_id,
                    "cwd": str(cwd),
                    "hook_version": HOOK_VERSION,
                })
    except (OSError, TimeoutError) as exc:
        _log(base, name, session_id, f"error: {exc}")
        errors.append(str(exc))
    if errors:
        detail = "; ".join(errors)
        if name == "Stop":
            print(json.dumps({
                "decision": "block",
                "reason": f"Translation run could not be fenced: {detail}. Stop it before ending this turn.",
            }))
        else:
            print(json.dumps({"systemMessage": f"Translation run may still be active: {detail}"}))
    else:
        print("{}")
    return 0


def _fence_mapping(base: Path, cwd: Path, name: str, session_id: str, mapping_path: Path, deadline: float) -> None:
    try:
        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
        if mapping.get("session_id") != session_id:
            raise ValueError("active mapping session mismatch")
        command = [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "translate_book.py"),
            "stop-for-root",
            str(mapping["project"]),
            "--run-id",
            str(mapping["run_id"]),
            "--root-thread-id",
            session_id,
        ]
        remaining = deadline - time.monotonic() - 0.15
        if remaining <= 0:
            raise TimeoutError("root guard deadline exceeded before fencing")
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=min(remaining, 1.8 if name == "Interrupt" else 5.0),
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"stop-for-root exited {result.returncode}")
        response = json.loads(result.stdout)
        if response.get("reason") == "ROOT_OR_RUN_MISMATCH":
            if response.get("current_run_id") == mapping["run_id"]:
                raise RuntimeError(f"current run has a mismatched root identity: {response}")
            _log(base, name, session_id, f"stale mapping ignored for {mapping['run_id']}")
            return
        if response.get("reason") not in (None, "ALREADY_TERMINAL") or (
            response.get("reason") is None and not response.get("stopped")
        ):
            raise RuntimeError(f"stop-for-root did not fence the run: {response}")
        _log(base, name, session_id, f"fenced {mapping['run_id']}")
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired, RuntimeError, TimeoutError):
        raise


if __name__ == "__main__":
    raise SystemExit(main())
