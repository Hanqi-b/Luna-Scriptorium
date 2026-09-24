"""CLI-level state invariant tests for the Phase 1 translation runner."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import hashlib
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "translate-book" / "translate_book.py"
ROOT_GUARD_HOOK = ROOT / "translate-book" / "hooks" / "root_guard.py"


class RunnerCLITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.temp_root = Path(self.temp.name)
        self.project = self.temp_root / "project"
        self.source = self.temp_root / "book.md"

    def invoke(
        self,
        *args: object,
        cwd: Path = ROOT,
        env_overrides: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        child_env = os.environ.copy()
        child_env.pop("CODEX_THREAD_ID", None)
        child_env.pop("CODEX_SESSION_ID", None)
        if env_overrides:
            child_env.update(env_overrides)
        return subprocess.run(
            [sys.executable, str(RUNNER), *(str(arg) for arg in args)],
            cwd=cwd,
            env=child_env,
            text=True,
            capture_output=True,
            check=False,
        )

    def command(
        self,
        *args: object,
        cwd: Path = ROOT,
        env_overrides: dict[str, str] | None = None,
    ) -> dict[str, object]:
        result = self.invoke(*args, cwd=cwd, env_overrides=env_overrides)
        if result.returncode != 0:
            raise AssertionError(
                f"command failed ({result.returncode}): {args!r}\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}"
            )
        return json.loads(result.stdout)

    def expect_error(
        self,
        text: str,
        *args: object,
        cwd: Path = ROOT,
        env_overrides: dict[str, str] | None = None,
    ) -> None:
        result = self.invoke(*args, cwd=cwd, env_overrides=env_overrides)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(text, result.stderr)

    def init_markdown(self, *bodies: str) -> dict[str, object]:
        content = "\n\n".join(
            f"# Chapter {number}\n\n{body}" for number, body in enumerate(bodies, 1)
        )
        self.source.write_text(content + "\n", encoding="utf-8")
        return self.command(
            "init",
            self.source,
            "--project",
            self.project,
            "--chunking-version",
            "1",
        )

    def init_text(self, content: str) -> dict[str, object]:
        self.source = self.temp_root / "book.txt"
        self.source.write_text(content, encoding="utf-8")
        return self.command(
            "init",
            self.source,
            "--project",
            self.project,
            "--chunking-version",
            "1",
        )

    def plan(self, source: Path | None = None) -> dict[str, object]:
        return self.command("plan", source or self.source)

    def test_v2_plan_is_read_only_and_locks_grouped_chunks(self) -> None:
        self.source.write_text(
            "# Chapitre 1\n\nPremier paragraphe.\n\nDeuxième paragraphe.\n\n"
            "```md\n# Titre dans un exemple\n```python\n# Encore dans le code\n```\n\n"
            "# Chapitre 2\n\nTroisième paragraphe.\n",
            encoding="utf-8",
        )
        first = self.plan()
        second = self.plan()
        self.assertEqual(first, second)
        self.assertEqual(first["chunking_version"], 2)
        self.assertEqual(first["chapters"], 2)
        self.assertEqual(first["chunks"], 2)
        self.assertTrue(first["coverage_ok"])
        self.assertFalse(self.project.exists(), "plan must not initialize a project")
        initialized = self.command(
            "init", self.source, "--project", self.project,
            "--expected-source-sha256", first["source_sha256"],
            "--expected-plan-sha256", first["plan_sha256"],
        )
        self.assertEqual(initialized["plan_sha256"], first["plan_sha256"])
        with sqlite3.connect(self.project / "state.sqlite") as connection:
            rows = connection.execute(
                "SELECT c.source FROM chunks c JOIN chapters h ON h.chapter_id = c.chapter_id "
                "ORDER BY h.chapter_number, c.chunk_number"
            ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertIn("Premier paragraphe.\n\nDeuxième paragraphe.", rows[0][0])
        self.assertIn("# Titre dans un exemple", rows[0][0])
        self.assertIn("# Encore dans le code", rows[0][0])

    def test_v2_plan_warns_about_unclosed_code_fence(self) -> None:
        self.source.write_text("# Chapitre 1\n\n```md\nTexte\n# Pas un chapitre\n", encoding="utf-8")
        planned = self.plan()
        self.assertEqual(planned["chapters"], 1)
        self.assertIn("UNCLOSED_CODE_FENCE", planned["warnings"])

    def test_v2_plan_rejects_changed_source_and_tampered_locked_plan(self) -> None:
        self.source.write_text("# Chapitre 1\n\nTexte d'origine.\n", encoding="utf-8")
        planned = self.plan()
        self.source.write_text("# Chapitre 1\n\nTexte modifié.\n", encoding="utf-8")
        self.expect_error(
            "source SHA-256 mismatch", "init", self.source, "--project", self.project,
            "--expected-source-sha256", planned["source_sha256"],
            "--expected-plan-sha256", planned["plan_sha256"],
        )
        self.assertFalse(self.project.exists())
        revised = self.plan()
        self.command(
            "init", self.source, "--project", self.project,
            "--expected-source-sha256", revised["source_sha256"],
            "--expected-plan-sha256", revised["plan_sha256"],
        )
        replacement = "# Texte altéré.\n\nTexte modifié."
        with sqlite3.connect(self.project / "state.sqlite") as connection:
            connection.execute(
                "UPDATE chunks SET source = ?, source_hash = ? WHERE chunk_id = 'ch001_c001'",
                (replacement, hashlib.sha256(replacement.encode("utf-8")).hexdigest()),
            )
        self.expect_error("stored chunk plan differs", "start", self.project)

    def test_v2_long_french_text_splits_on_whitespace_with_full_coverage(self) -> None:
        paragraph = "La phrase française est longue, mais reste lisible. " * 130
        self.source.write_text(f"# Chapitre 1\n\n{paragraph}\n", encoding="utf-8")
        planned = self.plan()
        self.assertGreater(planned["chunks"], 1)
        self.assertLessEqual(planned["max_chunk_chars"], 4000)
        self.assertTrue(planned["coverage_ok"])
        self.assertFalse(any(str(w).startswith("HARD_CHARACTER_CUTS") for w in planned["warnings"]))
        self.command("init", self.source, "--project", self.project)
        with sqlite3.connect(self.project / "state.sqlite") as connection:
            pieces = [row[0] for row in connection.execute("SELECT source FROM chunks ORDER BY chunk_number")]
        self.assertEqual(
            "".join("".join(piece.split()) for piece in pieces),
            "".join(self.source.read_text(encoding="utf-8").split()),
        )
        self.assertTrue(all(len(piece) <= 4000 for piece in pieces))

    def test_v2_grouped_chapters_translate_and_build(self) -> None:
        self.source.write_text(
            "# Chapitre 1\n\nBonjour.\n\nLe monde change.\n\n"
            "# Chapitre 2\n\nUne autre histoire.\n",
            encoding="utf-8",
        )
        planned = self.plan()
        self.assertEqual((planned["chapters"], planned["chunks"]), (2, 2))
        self.command(
            "init", self.source, "--project", self.project,
            "--source-language", "fr", "--target-language", "zh-CN",
            "--expected-source-sha256", planned["source_sha256"],
            "--expected-plan-sha256", planned["plan_sha256"],
        )
        run_id = self.start()
        first = self.claim(run_id, "translator_1")
        second = self.claim(run_id, "translator_2")
        self.assertNotEqual(first["chapter_id"], second["chapter_id"])
        self.commit(run_id, first, "# 第一章\n\n你好。\n\n世界正在改变。")
        self.commit(run_id, second, "# 第二章\n\n另一个故事。")
        self.assertEqual(self.command("status", self.project)["chunks"]["states"], {"DONE": 2})
        built = self.command("build", self.project)
        output = Path(str(built["output"])).read_text(encoding="utf-8")
        self.assertIn("# 第一章\n\n你好。\n\n世界正在改变。", output)
        self.assertIn("# 第二章\n\n另一个故事。", output)

    def confirm_cleanup(self, run_id: str) -> dict[str, object]:
        arguments: list[str] = ["confirm-cleanup", str(self.project), "--run-id", run_id]
        for worker_number in range(1, 5):
            arguments.extend(("--not-spawned-worker", f"translator_{worker_number}"))
        return self.command(*arguments)

    def invalidate_run(self, run_id: str) -> dict[str, object]:
        return self.command("invalidate-run", self.project, "--run-id", run_id)

    def root_env(self, thread_id: str, session_id: str) -> dict[str, str]:
        return {
            "CODEX_THREAD_ID": thread_id,
            "CODEX_SESSION_ID": session_id,
        }

    def write_ready_marker(
        self,
        thread_id: str,
        session_id: str,
        *,
        cwd: Path | None = None,
        hook_version: int = 1,
        stop_ready: bool = True,
    ) -> Path:
        root_cwd = (cwd or self.temp_root).resolve()
        marker = (
            root_cwd
            / ".codex"
            / "translate-book-guard"
            / "ready"
            / f"{thread_id}.json"
        )
        marker.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "session_id": session_id,
            "cwd": str(root_cwd),
            "hook_version": hook_version,
        }
        marker.write_text(json.dumps(payload), encoding="utf-8")
        if stop_ready:
            stop_marker = (
                root_cwd
                / ".codex"
                / "translate-book-guard"
                / "stop-ready"
                / f"{thread_id}.json"
            )
            stop_marker.parent.mkdir(parents=True, exist_ok=True)
            stop_marker.write_text(json.dumps(payload), encoding="utf-8")
        return marker

    def active_mapping_path(self, thread_id: str, run_id: str) -> Path:
        return (
            self.temp_root
            / ".codex"
            / "translate-book-guard"
            / "active"
            / thread_id
            / f"{run_id}.json"
        )

    def invoke_root_hook(
        self, event: dict[str, object], *, cwd: Path
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(ROOT_GUARD_HOOK)],
            cwd=cwd,
            input=json.dumps(event),
            text=True,
            capture_output=True,
            check=False,
        )

    def launch_guarded_start_paused_in_open_project(
        self, root_id: str
    ) -> tuple[subprocess.Popen[str], Path, Path]:
        gate_dir = self.temp_root / f"start-gate-{root_id}"
        gate_dir.mkdir()
        entered_path = gate_dir / "entered"
        release_path = gate_dir / "release"
        sitecustomize = gate_dir / "sitecustomize.py"
        sitecustomize.write_text(
            "import os\n"
            "import sqlite3\n"
            "import time\n"
            "from pathlib import Path\n"
            "_connect = sqlite3.connect\n"
            "_gate = os.environ.get('TRANSLATE_BOOK_TEST_START_GATE')\n"
            "if _gate:\n"
            "    _entered = Path(os.environ['TRANSLATE_BOOK_TEST_START_ENTERED'])\n"
            "    _release = Path(os.environ['TRANSLATE_BOOK_TEST_START_RELEASE'])\n"
            "    def _delayed_connect(*args, **kwargs):\n"
            "        _entered.write_text('entered', encoding='utf-8')\n"
            "        deadline = time.monotonic() + 30\n"
            "        while not _release.exists():\n"
            "            if time.monotonic() >= deadline:\n"
            "                raise TimeoutError('test start gate was not released')\n"
            "            time.sleep(0.01)\n"
            "        return _connect(*args, **kwargs)\n"
            "    sqlite3.connect = _delayed_connect\n",
            encoding="utf-8",
        )

        child_env = os.environ.copy()
        child_env.pop("CODEX_THREAD_ID", None)
        child_env.pop("CODEX_SESSION_ID", None)
        child_env.update(self.root_env(root_id, root_id))
        child_env["TRANSLATE_BOOK_TEST_START_GATE"] = "enabled"
        child_env["TRANSLATE_BOOK_TEST_START_ENTERED"] = str(entered_path)
        child_env["TRANSLATE_BOOK_TEST_START_RELEASE"] = str(release_path)
        existing_pythonpath = child_env.get("PYTHONPATH")
        child_env["PYTHONPATH"] = (
            str(gate_dir)
            if not existing_pythonpath
            else os.pathsep.join((str(gate_dir), existing_pythonpath))
        )
        process = subprocess.Popen(
            [sys.executable, str(RUNNER), "start", str(self.project)],
            cwd=self.temp_root,
            env=child_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return process, entered_path, release_path

    def wait_for_path(
        self,
        path: Path,
        process: subprocess.Popen[str],
        *,
        timeout_seconds: float = 10,
    ) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline and not path.exists():
            if process.poll() is not None:
                break
            time.sleep(0.01)
        return path.exists()

    def start(self) -> str:
        result = self.command("start", self.project)
        return str(result["run_id"])

    def claim(self, run_id: str, worker_id: str) -> dict[str, object]:
        return self.command(
            "claim", self.project, "--run-id", run_id, "--worker-id", worker_id
        )

    def commit(self, run_id: str, claim: dict[str, object], text: str) -> dict[str, object]:
        attempt_id = str(claim["attempt_id"])
        candidate = self.temp_root / f"translation-{attempt_id}.txt"
        candidate.write_text(text, encoding="utf-8")
        return self.command(
            "commit",
            self.project,
            "--run-id",
            run_id,
            "--worker-id",
            claim["worker_id"],
            "--attempt-id",
            attempt_id,
            "--file",
            candidate,
        )

    def test_four_concurrent_claims_own_distinct_chapters(self) -> None:
        self.init_markdown("Body one.", "Body two.", "Body three.", "Body four.")
        run_id = self.start()
        barrier = threading.Barrier(4)

        def concurrent_claim(worker_id: str) -> dict[str, object]:
            barrier.wait(timeout=10)
            return self.claim(run_id, worker_id)

        workers = [f"translator_{number}" for number in range(1, 5)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            claims = list(pool.map(concurrent_claim, workers))

        self.assertTrue(all(claim["claimed"] for claim in claims), claims)
        self.assertEqual({claim["worker_id"] for claim in claims}, set(workers))
        self.assertEqual(len({claim["chapter_id"] for claim in claims}), 4, claims)
        self.assertEqual(self.command("status", self.project)["running_attempt_count"], 4)

    def test_chapter_stays_with_owner_and_chunks_are_claimed_in_order(self) -> None:
        self.init_text("First paragraph.\n\nSecond paragraph.")
        run_id = self.start()
        first = self.claim(run_id, "translator_1")
        other = self.claim(run_id, "translator_2")
        self.assertTrue(first["claimed"])
        self.assertFalse(other["claimed"])
        self.assertEqual(other["reason"], "NO_WORK")

        self.commit(run_id, first, "# Translated heading.")
        second = self.claim(run_id, "translator_1")
        self.assertTrue(second["claimed"])
        self.assertEqual(second["chapter_id"], first["chapter_id"])
        self.assertGreater(second["chunk_number"], first["chunk_number"])
        still_owned = self.claim(run_id, "translator_2")
        self.assertFalse(still_owned["claimed"])
        self.assertEqual(still_owned["reason"], "NO_WORK")

    def test_commit_is_atomic_and_done_chunk_cannot_be_claimed_again(self) -> None:
        self.init_text("Only source paragraph.")
        run_id = self.start()
        claim = self.claim(run_id, "translator_1")
        attempt_id = str(claim["attempt_id"])
        candidate = self.temp_root / "candidate.txt"
        candidate.write_text("Committed translation.", encoding="utf-8")

        # Force the second write in commit to fail. The earlier attempt-state
        # update must roll back with the translation insert.
        with sqlite3.connect(self.project / "state.sqlite") as connection:
            connection.execute(
                """
                CREATE TRIGGER reject_translation BEFORE INSERT ON translations
                BEGIN SELECT RAISE(ABORT, 'injected commit failure'); END
                """
            )
        self.expect_error(
            "injected commit failure",
            "commit",
            self.project,
            "--run-id",
            run_id,
            "--worker-id",
            "translator_1",
            "--attempt-id",
            attempt_id,
            "--file",
            candidate,
        )
        with sqlite3.connect(self.project / "state.sqlite") as connection:
            chunk_state = connection.execute("SELECT state FROM chunks").fetchone()[0]
            attempt_state = connection.execute(
                "SELECT state FROM attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()[0]
            translation_count = connection.execute(
                "SELECT COUNT(*) FROM translations"
            ).fetchone()[0]
            connection.execute("DROP TRIGGER reject_translation")
        self.assertEqual((chunk_state, attempt_state, translation_count), ("RUNNING", "RUNNING", 0))

        self.command(
            "commit",
            self.project,
            "--run-id",
            run_id,
            "--worker-id",
            "translator_1",
            "--attempt-id",
            attempt_id,
            "--file",
            candidate,
        )
        status = self.command("status", self.project)
        self.assertEqual(status["chunks"]["states"], {"DONE": 1})
        self.assertEqual(status["running_attempt_count"], 0)
        duplicate = self.claim(run_id, "translator_2")
        self.assertFalse(duplicate["claimed"])
        self.assertIn(duplicate["reason"], {"NO_WORK", "STOP_REQUESTED", "STALE_RUN"})
        with sqlite3.connect(self.project / "state.sqlite") as connection:
            row = connection.execute(
                "SELECT c.state, t.text, a.state FROM chunks c "
                "JOIN translations t ON t.chunk_id = c.chunk_id "
                "JOIN attempts a ON a.attempt_id = t.attempt_id"
            ).fetchone()
        self.assertEqual(row, ("DONE", "Committed translation.", "DONE"))

    def test_failed_chunk_retries_without_affecting_another_workers_chapter(self) -> None:
        self.init_markdown("First chapter body.", "Second chapter body.")
        run_id = self.start()
        first = self.claim(run_id, "translator_1")
        second = self.claim(run_id, "translator_2")

        failure = self.command(
            "fail",
            self.project,
            "--run-id",
            run_id,
            "--worker-id",
            "translator_1",
            "--attempt-id",
            first["attempt_id"],
            "--error",
            "transient model error",
        )
        self.assertTrue(failure["retryable"])
        self.commit(run_id, second, "# Second chapter heading.")
        second_next = self.claim(run_id, "translator_2")
        self.assertEqual(second_next["chapter_id"], second["chapter_id"])
        self.commit(run_id, second_next, "Second chapter translated.")

        retry = self.claim(run_id, "translator_1")
        self.assertEqual(retry["chunk_id"], first["chunk_id"])
        self.assertEqual(retry["attempt_number"], 2)
        self.commit(run_id, retry, "# First chapter heading translated.")
        first_next = self.claim(run_id, "translator_1")
        self.assertEqual(first_next["chapter_id"], first["chapter_id"])
        self.commit(run_id, first_next, "First chapter translated.")

        states = self.command("status", self.project)["chunks"]["states"]
        self.assertEqual(states, {"DONE": 4})

    def test_force_stop_rejects_the_worker_attempt(self) -> None:
        self.init_text("One source paragraph.")
        run_id = self.start()
        claim = self.claim(run_id, "translator_1")
        stopped = self.command("force-stop", self.project, "--run-id", run_id)
        self.assertEqual(stopped["status"], "STOPPED")
        self.assertEqual(stopped["interrupted"], 1)

        candidate = self.temp_root / "late-result.txt"
        candidate.write_text("Late translation.", encoding="utf-8")
        self.expect_error(
            "not accepting worker results",
            "commit",
            self.project,
            "--run-id",
            run_id,
            "--worker-id",
            "translator_1",
            "--attempt-id",
            claim["attempt_id"],
            "--file",
            candidate,
        )
        status = self.command("status", self.project)
        self.assertEqual(status["running_attempt_count"], 0)
        self.assertEqual(status["chunks"]["states"], {"INTERRUPTED": 1})

    def test_stop_atomically_fences_active_attempt_and_resume_requires_cleanup_ack(self) -> None:
        self.init_text("First paragraph.\n\nSecond paragraph.")
        old_run_id = self.start()

        completed = self.claim(old_run_id, "translator_1")
        self.commit(old_run_id, completed, "First translation.")
        active = self.claim(old_run_id, "translator_1")
        self.assertNotEqual(completed["chunk_id"], active["chunk_id"])

        stopped = self.command("stop", self.project, "--run-id", old_run_id)
        self.assertEqual(stopped["status"], "STOPPED")
        self.assertEqual(stopped["interrupted"], 1)

        with sqlite3.connect(self.project / "state.sqlite") as connection:
            attempts = connection.execute(
                "SELECT attempt_id, state FROM attempts ORDER BY started_at, attempt_id"
            ).fetchall()
            chunks = connection.execute(
                "SELECT chunk_id, state, current_attempt_id FROM chunks ORDER BY chunk_number"
            ).fetchall()
            translations = connection.execute(
                "SELECT chunk_id, text FROM translations"
            ).fetchall()
        self.assertEqual(
            {(str(attempt_id), str(state)) for attempt_id, state in attempts},
            {(str(completed["attempt_id"]), "DONE"), (str(active["attempt_id"]), "INTERRUPTED")},
        )
        self.assertEqual(
            [(str(row[0]), str(row[1]), row[2]) for row in chunks],
            [(str(completed["chunk_id"]), "DONE", None), (str(active["chunk_id"]), "INTERRUPTED", None)],
        )
        self.assertEqual(translations, [(completed["chunk_id"], "First translation.")])

        late_file = self.temp_root / "late-stop-result.txt"
        late_file.write_text("Must never overwrite.", encoding="utf-8")
        self.expect_error(
            "not accepting worker results",
            "commit",
            self.project,
            "--run-id",
            old_run_id,
            "--worker-id",
            "translator_1",
            "--attempt-id",
            active["attempt_id"],
            "--file",
            late_file,
        )

        self.expect_error("confirm-cleanup", "start", self.project)
        self.confirm_cleanup(old_run_id)
        resumed_run_id = self.start()
        self.assertNotEqual(resumed_run_id, old_run_id)
        resumed = self.claim(resumed_run_id, "translator_1")
        self.assertTrue(resumed["claimed"], resumed)
        self.assertEqual(resumed["chunk_id"], active["chunk_id"])
        self.assertEqual(resumed["attempt_number"], 2)
        self.commit(resumed_run_id, resumed, "Second translation.")

        no_more_work = self.claim(resumed_run_id, "translator_2")
        self.assertFalse(no_more_work["claimed"], no_more_work)
        status = self.command("status", self.project)
        self.assertEqual(status["chunks"]["states"], {"DONE": 2})
        with sqlite3.connect(self.project / "state.sqlite") as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM translations").fetchone()[0], 2
            )

    def test_stop_rolls_back_all_fencing_writes_if_chunk_invalidation_fails(self) -> None:
        self.init_text("The stop transaction must not expose a partial state.")
        run_id = self.start()
        claim = self.claim(run_id, "translator_1")

        with sqlite3.connect(self.project / "state.sqlite") as connection:
            connection.execute(
                """
                CREATE TRIGGER reject_stop_chunk BEFORE UPDATE ON chunks
                WHEN NEW.state = 'INTERRUPTED'
                BEGIN SELECT RAISE(ABORT, 'injected stop failure'); END
                """
            )
        self.expect_error("injected stop failure", "stop", self.project, "--run-id", run_id)
        with sqlite3.connect(self.project / "state.sqlite") as connection:
            run_state = connection.execute(
                "SELECT status FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
            attempt_state = connection.execute(
                "SELECT state FROM attempts WHERE attempt_id = ?", (claim["attempt_id"],)
            ).fetchone()[0]
            chunk_state = connection.execute(
                "SELECT state FROM chunks WHERE chunk_id = ?", (claim["chunk_id"],)
            ).fetchone()[0]
            connection.execute("DROP TRIGGER reject_stop_chunk")
        self.assertEqual((run_state, attempt_state, chunk_state), ("RUNNING", "RUNNING", "RUNNING"))

    def test_start_does_not_invalidate_a_live_run_without_explicit_recovery(self) -> None:
        self.init_text("An active root still owns this run.")
        old_run_id = self.start()
        active = self.claim(old_run_id, "translator_1")

        self.expect_error("invalidate-run", "start", self.project)
        status = self.command("status", self.project)
        self.assertEqual(status["run"]["run_id"], old_run_id)
        self.assertEqual(status["run"]["status"], "RUNNING")
        self.assertEqual(status["running_attempt_count"], 1)
        with sqlite3.connect(self.project / "state.sqlite") as connection:
            states = connection.execute(
                "SELECT a.state, c.state FROM attempts a JOIN chunks c ON c.chunk_id = a.chunk_id "
                "WHERE a.attempt_id = ?",
                (active["attempt_id"],),
            ).fetchone()
        self.assertEqual(states, ("RUNNING", "RUNNING"))

        invalidated = self.command("invalidate-run", self.project, "--run-id", old_run_id)
        self.assertEqual(invalidated["status"], "INTERRUPTED")
        self.assertEqual(invalidated["interrupted"], 1)
        self.confirm_cleanup(old_run_id)
        new_run_id = self.start()
        self.assertNotEqual(new_run_id, old_run_id)
        resumed = self.claim(new_run_id, "translator_2")
        self.assertTrue(resumed["claimed"], resumed)
        self.assertEqual(resumed["chunk_id"], active["chunk_id"])
        self.assertEqual(resumed["attempt_number"], 2)

    def test_completed_run_still_requires_host_cleanup_attestation_before_start(self) -> None:
        self.init_text("A fully committed book still has worker identities to reap.")
        completed_run_id = self.start()
        claim = self.claim(completed_run_id, "translator_1")
        self.commit(completed_run_id, claim, "Completed translation.")

        status = self.command("status", self.project)
        self.assertEqual(status["run"]["status"], "COMPLETED")
        with sqlite3.connect(self.project / "state.sqlite") as connection:
            cleanup_verified_at = connection.execute(
                "SELECT cleanup_verified_at FROM runs WHERE run_id = ?",
                (completed_run_id,),
            ).fetchone()[0]
        self.assertIsNone(cleanup_verified_at)

        self.expect_error("confirm-cleanup", "start", self.project)
        self.confirm_cleanup(completed_run_id)
        self.start()
        final_status = self.command("status", self.project)
        self.assertEqual(final_status["chunks"]["states"], {"DONE": 1})
        self.assertEqual(final_status["running_attempt_count"], 0)

    def test_start_registers_four_worker_identities_and_cleanup_needs_observations(self) -> None:
        self.init_text("Registry and cleanup observations are part of run state.")
        started = self.command(
            "start", self.project, "--root-agent-path", "/root/lifecycle_test"
        )
        run_id = str(started["run_id"])
        with sqlite3.connect(self.project / "state.sqlite") as connection:
            workers = connection.execute(
                "SELECT worker_id, canonical_path, expected_run_id, observed_state, observed_at "
                "FROM worker_registry ORDER BY worker_id"
            ).fetchall()
        self.assertEqual(len(workers), 4, workers)
        self.assertEqual(
            [(row[0], row[1]) for row in workers],
            [
                ("translator_1", "/root/lifecycle_test/translator_1"),
                ("translator_2", "/root/lifecycle_test/translator_2"),
                ("translator_3", "/root/lifecycle_test/translator_3"),
                ("translator_4", "/root/lifecycle_test/translator_4"),
            ],
        )
        self.assertTrue(all(row[2] == run_id and row[3] is None and row[4] is None for row in workers))

        self.command("stop", self.project, "--run-id", run_id)
        unobserved_cleanup = self.invoke(
            "confirm-cleanup", self.project, "--run-id", run_id
        )
        self.assertNotEqual(unobserved_cleanup.returncode, 0, unobserved_cleanup.stdout)

        active_observation = self.invoke(
            "confirm-cleanup",
            self.project,
            "--run-id",
            run_id,
            "--worker-state",
            "translator_1=active",
            "--not-spawned-worker",
            "translator_2",
            "--not-spawned-worker",
            "translator_3",
            "--not-spawned-worker",
            "translator_4",
        )
        self.assertNotEqual(active_observation.returncode, 0, active_observation.stdout)
        self.assertIn("inactive or not-spawned", active_observation.stderr)

        confirmed = self.command(
            "confirm-cleanup",
            self.project,
            "--run-id",
            run_id,
            "--not-spawned-worker",
            "translator_1",
            "--not-spawned-worker",
            "translator_2",
            "--not-spawned-worker",
            "translator_3",
            "--not-spawned-worker",
            "translator_4",
        )
        self.assertTrue(confirmed["cleanup_verified"])
        self.assertEqual(set(confirmed["worker_observations"].values()), {"NOT_SPAWNED"})

    def test_repeated_stop_resume_cycles_keep_done_chunks_and_attempt_history(self) -> None:
        self.init_text("First paragraph.\n\nSecond paragraph.\n\nThird paragraph.")
        run_id = self.start()

        first = self.claim(run_id, "translator_1")
        self.command("stop", self.project, "--run-id", run_id)
        self.confirm_cleanup(run_id)
        run_id = self.start()

        resumed_first = self.claim(run_id, "translator_1")
        self.assertEqual(resumed_first["chunk_id"], first["chunk_id"])
        self.commit(run_id, resumed_first, "First translation.")

        second = self.claim(run_id, "translator_1")
        self.command("stop", self.project, "--run-id", run_id)
        self.confirm_cleanup(run_id)
        run_id = self.start()

        resumed_second = self.claim(run_id, "translator_1")
        self.assertEqual(resumed_second["chunk_id"], second["chunk_id"])
        self.commit(run_id, resumed_second, "Second translation.")
        third = self.claim(run_id, "translator_1")
        self.assertNotIn(third["chunk_id"], {first["chunk_id"], second["chunk_id"]})
        self.commit(run_id, third, "Third translation.")

        status = self.command("status", self.project)
        self.assertEqual(status["chunks"]["states"], {"DONE": 3})
        with sqlite3.connect(self.project / "state.sqlite") as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM attempts WHERE state = 'INTERRUPTED'"
                ).fetchone()[0],
                2,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM translations").fetchone()[0], 3
            )

    def test_attempt_deadline_rejects_commit_without_writing_translation(self) -> None:
        self.init_text("A late model response must not commit.")
        run_id = self.start()
        claim = self.claim(run_id, "translator_1")
        late_file = self.temp_root / "past-deadline.txt"
        late_file.write_text("Too late.", encoding="utf-8")

        with sqlite3.connect(self.project / "state.sqlite") as connection:
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(attempts)")
            }
            self.assertIn("deadline_at", columns)
            connection.execute(
                "UPDATE attempts SET deadline_at = ? WHERE attempt_id = ?",
                (time.time() - 1, claim["attempt_id"]),
            )

        rejected = self.invoke(
            "commit",
            self.project,
            "--run-id",
            run_id,
            "--worker-id",
            "translator_1",
            "--attempt-id",
            claim["attempt_id"],
            "--file",
            late_file,
        )
        self.assertNotEqual(rejected.returncode, 0, rejected.stdout)
        self.assertIn("deadline", rejected.stderr.lower())
        with sqlite3.connect(self.project / "state.sqlite") as connection:
            chunk_state = connection.execute("SELECT state FROM chunks").fetchone()[0]
            attempt_state = connection.execute(
                "SELECT state FROM attempts WHERE attempt_id = ?", (claim["attempt_id"],)
            ).fetchone()[0]
            translation_count = connection.execute(
                "SELECT COUNT(*) FROM translations"
            ).fetchone()[0]
        self.assertNotEqual(chunk_state, "DONE")
        self.assertNotEqual(attempt_state, "DONE")
        self.assertEqual(translation_count, 0)

    def test_repeated_force_stops_and_recovery_do_not_spend_failure_budget(self) -> None:
        self.init_text("Still available after interruptions.")
        run_id = self.start()

        # Mix explicit force stops and abandoned-run recovery. These interrupt
        # attempts but must not count as translation failures.
        for interruption in ("force", "recover", "force"):
            claim = self.claim(run_id, "translator_1")
            self.assertTrue(claim["claimed"], interruption)
            if interruption == "force":
                stopped = self.command("force-stop", self.project, "--run-id", run_id)
                self.assertEqual(stopped["interrupted"], 1)
                self.confirm_cleanup(run_id)
                run_id = self.start()
            else:
                self.expect_error("invalidate-run", "start", self.project)
                self.invalidate_run(run_id)
                self.confirm_cleanup(run_id)
                run_id = self.start()
            status = self.command("status", self.project)
            self.assertNotIn("FAILED", status["chunks"]["states"], status)

        fourth_attempt = self.claim(run_id, "translator_2")
        self.assertTrue(fourth_attempt["claimed"], fourth_attempt)
        self.assertEqual(fourth_attempt["chunk_id"], claim["chunk_id"])

    def test_retry_failed_does_not_reactivate_the_stopped_run_id(self) -> None:
        self.init_text("A chunk that will fail three times.")
        old_run_id = self.start()
        for attempt_number in range(1, 4):
            claim = self.claim(old_run_id, "translator_1")
            self.assertTrue(claim["claimed"], claim)
            failure = self.command(
                "fail",
                self.project,
                "--run-id",
                old_run_id,
                "--worker-id",
                "translator_1",
                "--attempt-id",
                claim["attempt_id"],
                "--error",
                f"deliberate failure {attempt_number}",
            )
        self.assertFalse(failure["retryable"])
        self.command("force-stop", self.project, "--run-id", old_run_id)
        self.confirm_cleanup(old_run_id)

        retried = self.command("retry-failed", self.project)
        retry_run_id = str(retried["run_id"])
        if retry_run_id == old_run_id:
            self.assertNotEqual(
                self.command("status", self.project)["run"]["status"], "RUNNING"
            )
            retry_run_id = self.start()
        self.assertNotEqual(retry_run_id, old_run_id)

        old_claim = self.invoke(
            "claim",
            self.project,
            "--run-id",
            old_run_id,
            "--worker-id",
            "translator_2",
        )
        if old_claim.returncode == 0:
            self.assertFalse(json.loads(old_claim.stdout)["claimed"])
        else:
            self.assertIn("error", old_claim.stderr)
        new_claim = self.claim(retry_run_id, "translator_2")
        self.assertTrue(new_claim["claimed"], new_claim)

    def test_init_preserves_nonempty_project_and_allows_retry_after_invalid_source(self) -> None:
        incoming = self.temp_root / "same-name.txt"
        incoming.write_text("Incoming source.", encoding="utf-8")
        (self.project / "source").mkdir(parents=True)
        sentinel = self.project / "source" / incoming.name
        sentinel.write_text("Existing user data.", encoding="utf-8")

        result = self.invoke("init", incoming, "--project", self.project)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "Existing user data.")

        # A failed parse must not strand config/state files that block a later
        # corrected init at the same project path.
        self.project = self.temp_root / "retryable-project"
        invalid_source = self.temp_root / "empty.txt"
        invalid_source.write_text("", encoding="utf-8")
        failed_init = self.invoke("init", invalid_source, "--project", self.project)
        self.assertNotEqual(failed_init.returncode, 0, failed_init.stdout)
        invalid_source.write_text("Corrected source paragraph.", encoding="utf-8")
        successful_init = self.invoke("init", invalid_source, "--project", self.project)
        self.assertEqual(
            successful_init.returncode,
            0,
            f"stdout: {successful_init.stdout}\nstderr: {successful_init.stderr}",
        )

    def test_commit_rejects_missing_markdown_heading_and_numeric_content(self) -> None:
        self.source.write_text(
            "# Chapter 7\n\nIn 1799, 42 readers gathered.\n", encoding="utf-8"
        )
        self.command(
            "init",
            self.source,
            "--project",
            self.project,
            "--chunking-version",
            "1",
        )
        run_id = self.start()
        heading = self.claim(run_id, "translator_1")
        self.assertTrue(heading["claimed"])

        bad_heading = self.temp_root / "bad-heading.txt"
        bad_heading.write_text("Chapitre 7", encoding="utf-8")
        rejected_heading = self.invoke(
            "commit",
            self.project,
            "--run-id",
            run_id,
            "--worker-id",
            "translator_1",
            "--attempt-id",
            heading["attempt_id"],
            "--file",
            bad_heading,
        )
        self.assertNotEqual(rejected_heading.returncode, 0, rejected_heading.stdout)
        self.commit(run_id, heading, "# Chapitre 7")

        body = self.claim(run_id, "translator_1")
        bad_numbers = self.temp_root / "missing-number.txt"
        bad_numbers.write_text("42 lecteurs se sont réunis.", encoding="utf-8")
        rejected_numbers = self.invoke(
            "commit",
            self.project,
            "--run-id",
            run_id,
            "--worker-id",
            "translator_1",
            "--attempt-id",
            body["attempt_id"],
            "--file",
            bad_numbers,
        )
        self.assertNotEqual(rejected_numbers.returncode, 0, rejected_numbers.stdout)
        self.commit(run_id, body, "1799年，有42位读者聚集。")
        self.assertEqual(
            self.command("status", self.project)["chunks"]["states"], {"DONE": 2}
        )

    def test_interrupted_worker_keeps_chapter_assignment(self) -> None:
        self.init_markdown("First body.", "Second body.")
        run_id = self.start()
        first = self.claim(run_id, "translator_1")
        second = self.claim(run_id, "translator_2")
        self.assertNotEqual(first["chapter_id"], second["chapter_id"])

        interrupted = self.command(
            "interrupt-worker",
            self.project,
            "--run-id",
            run_id,
            "--worker-id",
            "translator_1",
        )
        self.assertTrue(interrupted["interrupted"])
        status = self.command("status", self.project)
        first_chapter = next(
            chapter
            for chapter in status["chapters"]
            if chapter["chapter_id"] == first["chapter_id"]
        )
        self.assertEqual(first_chapter["owner_worker_id"], "translator_1")
        self.assertEqual(status["running_attempt_count"], 1)

    def test_interrupted_chunk_keeps_later_chunk_reserved_for_same_worker(self) -> None:
        self.init_text("First paragraph.\n\nSecond paragraph.")
        run_id = self.start()
        first = self.claim(run_id, "translator_1")
        self.assertEqual(first["chunk_id"], "ch001_c001")
        self.assertEqual(first["chunk_number"], 1)

        interrupted = self.command(
            "interrupt-worker",
            self.project,
            "--run-id",
            run_id,
            "--worker-id",
            "translator_1",
        )
        self.assertTrue(interrupted["interrupted"], interrupted)

        with sqlite3.connect(self.project / "state.sqlite") as connection:
            chunk_states = connection.execute(
                "SELECT chunk_id, state FROM chunks ORDER BY chunk_number"
            ).fetchall()
            chapter_owner = connection.execute(
                "SELECT state, owner_run_id, owner_worker_id FROM chapters "
                "WHERE chapter_id = 'ch001'"
            ).fetchone()
        self.assertEqual(chunk_states[0], ("ch001_c001", "INTERRUPTED"))
        self.assertEqual(chunk_states[1], ("ch001_c002", "PENDING"))
        self.assertEqual(chapter_owner[1:], (run_id, "translator_1"))

        other_worker = self.claim(run_id, "translator_2")
        self.assertFalse(other_worker["claimed"], other_worker)
        self.assertEqual(other_worker["reason"], "NO_WORK")

    def test_concurrent_builds_create_distinct_files_without_overwrite(self) -> None:
        self.init_text("Source paragraph for concurrent builds.")
        run_id = self.start()
        claim = self.claim(run_id, "translator_1")
        self.commit(run_id, claim, "Translated paragraph.")

        build_count = 6
        barrier = threading.Barrier(build_count)

        def concurrent_build() -> subprocess.CompletedProcess[str]:
            barrier.wait(timeout=10)
            return self.invoke("build", self.project)

        with ThreadPoolExecutor(max_workers=build_count) as pool:
            results = list(pool.map(lambda _index: concurrent_build(), range(build_count)))
        for result in results:
            self.assertEqual(
                result.returncode,
                0,
                f"stdout: {result.stdout}\nstderr: {result.stderr}",
            )
        outputs = [json.loads(result.stdout)["output"] for result in results]
        self.assertEqual(len(set(outputs)), build_count, outputs)
        self.assertTrue(all(Path(path).read_text(encoding="utf-8") == "Translated paragraph.\n" for path in outputs))

    def test_recovery_requires_workers_cleared_ack_and_fences_old_attempt(self) -> None:
        self.init_text("Recover this paragraph.")
        old_run_id = self.start()
        old_claim = self.claim(old_run_id, "translator_1")
        self.expect_error("invalidate-run", "start", self.project)
        untouched = self.command("status", self.project)
        self.assertEqual(untouched["run"]["status"], "RUNNING")
        self.assertEqual(untouched["running_attempt_count"], 1)
        self.invalidate_run(old_run_id)
        self.confirm_cleanup(old_run_id)
        new_run_id = self.start()
        status = self.command("status", self.project)
        self.assertNotEqual(new_run_id, old_run_id)
        self.assertEqual(status["running_attempt_count"], 0)
        self.assertEqual(status["chunks"]["states"], {"PENDING": 1})

        candidate = self.temp_root / "old-result.txt"
        candidate.write_text("Stale translation.", encoding="utf-8")
        self.expect_error(
            "not accepting worker results",
            "commit",
            self.project,
            "--run-id",
            old_run_id,
            "--worker-id",
            "translator_1",
            "--attempt-id",
            old_claim["attempt_id"],
            "--file",
            candidate,
        )
        current = self.claim(new_run_id, "translator_2")
        self.assertTrue(current["claimed"])
        self.assertEqual(current["attempt_number"], 2)
        self.commit(new_run_id, current, "Recovered translation.")

    def test_status_selects_new_run_when_restart_timestamps_tie(self) -> None:
        self.init_text("A run that can be restarted immediately.")
        old_run_id = self.start()
        self.command("force-stop", self.project, "--run-id", old_run_id)
        self.confirm_cleanup(old_run_id)
        new_run_id = self.start()
        self.assertNotEqual(old_run_id, new_run_id)

        # created_at is second-resolution. Force the same timestamp and make
        # the stopped row sort after the new id lexically, so a timestamp/id
        # tie-break cannot accidentally pass this regression test.
        with sqlite3.connect(self.project / "state.sqlite") as connection:
            old_row_id = old_run_id
            if old_row_id <= new_run_id:
                old_row_id = "f" * 64
                connection.execute(
                    "UPDATE runs SET run_id = ? WHERE run_id = ?",
                    (old_row_id, old_run_id),
                )
            same_second = "2026-09-24T12:34:56+00:00"
            connection.execute(
                "UPDATE runs SET created_at = ? WHERE run_id IN (?, ?)",
                (same_second, old_row_id, new_run_id),
            )
            old_and_new = connection.execute(
                "SELECT run_id, created_at FROM runs WHERE run_id IN (?, ?)",
                (old_row_id, new_run_id),
            ).fetchall()
        self.assertEqual(len(old_and_new), 2)
        self.assertEqual(old_and_new[0][1], old_and_new[1][1])
        self.assertGreater(old_row_id, new_run_id)

        status = self.command("status", self.project)
        self.assertEqual(status["run"]["run_id"], new_run_id)
        self.assertEqual(status["run"]["status"], "RUNNING")

    def test_legacy_active_run_is_invalidated_before_cleanup_confirmation(self) -> None:
        self.init_text("Legacy project paragraph.")
        old_run_id = self.start()
        old_claim = self.claim(old_run_id, "translator_1")

        # Recreate the Phase 1 runs table without the lifecycle attestation,
        # retaining its run and the active attempt that references it.
        with sqlite3.connect(self.project / "state.sqlite") as connection:
            connection.execute(
                """
                CREATE TABLE runs_legacy (
                    run_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    stop_requested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    ended_at TEXT
                )
                """
            )
            connection.execute(
                """
                INSERT INTO runs_legacy(run_id, status, stop_requested, created_at, ended_at)
                SELECT run_id, status, stop_requested, created_at, ended_at FROM runs
                """
            )
            connection.execute("DROP TABLE runs")
            connection.execute("ALTER TABLE runs_legacy RENAME TO runs")

        # Migration keeps the active run visible. A fresh root start must
        # first atomically invalidate the old attempt, then wait for explicit
        # host cleanup confirmation.
        status = self.command("status", self.project)
        self.assertEqual(status["running_attempt_count"], 1)
        self.assertEqual(status["run"]["run_id"], old_run_id)
        self.assertEqual(old_claim["run_id"], old_run_id)
        with sqlite3.connect(self.project / "state.sqlite") as connection:
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(runs)")
            }
        self.assertIn("cleanup_verified_at", columns)
        self.assertIn("root_thread_id", columns)

        self.expect_error("invalidate-run", "start", self.project)
        self.invalidate_run(old_run_id)
        interrupted = self.command("status", self.project)
        self.assertEqual(interrupted["run"]["status"], "INTERRUPTED")
        self.assertEqual(interrupted["running_attempt_count"], 0)
        self.assertEqual(interrupted["chunks"]["states"], {"INTERRUPTED": 1})

        stale_candidate = self.temp_root / "legacy-active-result.txt"
        stale_candidate.write_text("Must not commit after recovery invalidation.", encoding="utf-8")
        self.expect_error(
            "not accepting worker results",
            "commit",
            self.project,
            "--run-id",
            old_run_id,
            "--worker-id",
            "translator_1",
            "--attempt-id",
            old_claim["attempt_id"],
            "--file",
            stale_candidate,
        )

        self.confirm_cleanup(old_run_id)
        new_run_id = self.start()
        self.assertNotEqual(new_run_id, old_run_id)
        new_status = self.command("status", self.project)
        self.assertEqual(new_status["run"]["run_id"], new_run_id)
        self.assertEqual(new_status["running_attempt_count"], 0)
        self.assertEqual(new_status["chunks"]["states"], {"PENDING": 1})
        resumed = self.claim(new_run_id, "translator_2")
        self.assertTrue(resumed["claimed"], resumed)
        self.assertEqual(resumed["attempt_number"], 2)

    def test_guarded_start_requires_ready_marker_for_current_root(self) -> None:
        self.init_text("Guarded start requires the installed Stop hook to be ready.")
        root_id = "root-ready-required"

        result = self.invoke(
            "start",
            self.project,
            cwd=self.temp_root,
            env_overrides=self.root_env(root_id, root_id),
        )

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("ready", result.stderr.lower())
        with sqlite3.connect(self.project / "state.sqlite") as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)

    def test_guarded_start_rejects_ready_marker_without_prior_stop_canary(self) -> None:
        self.init_text("A ready marker alone does not prove that Stop ran.")
        root_id = "root-stop-canary-required"
        self.write_ready_marker(root_id, root_id, stop_ready=False)

        result = self.invoke(
            "start",
            self.project,
            cwd=self.temp_root,
            env_overrides=self.root_env(root_id, root_id),
        )

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("stop-ready", result.stderr.lower())
        with sqlite3.connect(self.project / "state.sqlite") as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)

    def test_guarded_start_records_root_identity_and_active_hook_mapping(self) -> None:
        self.init_text("A guarded run belongs to this root thread.")
        root_id = "root-mapped-run"
        marker = self.write_ready_marker(root_id, root_id)
        self.assertTrue(marker.is_file())

        started = self.command(
            "start",
            self.project,
            cwd=self.temp_root,
            env_overrides=self.root_env(root_id, root_id),
        )
        run_id = str(started["run_id"])

        with sqlite3.connect(self.project / "state.sqlite") as connection:
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(runs)")
            }
            self.assertIn("root_thread_id", columns)
            root_thread_id = connection.execute(
                "SELECT root_thread_id FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
        self.assertEqual(root_thread_id, root_id)

        active_path = self.active_mapping_path(root_id, run_id)
        mapping = json.loads(active_path.read_text(encoding="utf-8"))
        self.assertEqual(mapping["session_id"], root_id)
        self.assertEqual(mapping["run_id"], run_id)
        self.assertEqual(Path(mapping["project"]).resolve(), self.project.resolve())
        self.assertNotIn("runner_path", mapping)

    def test_stop_for_root_fences_exact_run_and_rejects_late_commit(self) -> None:
        self.init_text("A stopped root run must reject its worker's late answer.")
        root_id = "root-stop-exact"
        self.write_ready_marker(root_id, root_id)
        started = self.command(
            "start",
            self.project,
            cwd=self.temp_root,
            env_overrides=self.root_env(root_id, root_id),
        )
        run_id = str(started["run_id"])
        claim = self.claim(run_id, "translator_1")
        candidate = self.temp_root / "root-late-result.txt"
        candidate.write_text("Late root response.", encoding="utf-8")

        stopped = self.command(
            "stop-for-root",
            self.project,
            "--run-id",
            run_id,
            "--root-thread-id",
            root_id,
        )
        self.assertTrue(stopped["stopped"], stopped)
        self.expect_error(
            "not accepting worker results",
            "commit",
            self.project,
            "--run-id",
            run_id,
            "--worker-id",
            "translator_1",
            "--attempt-id",
            claim["attempt_id"],
            "--file",
            candidate,
        )
        status = self.command("status", self.project)
        self.assertEqual(status["run"]["status"], "STOPPED")
        self.assertEqual(status["running_attempt_count"], 0)

    def test_stale_or_mismatched_root_stop_cannot_stop_the_new_run(self) -> None:
        self.init_text("A new root run must survive a stale hook event.")
        old_root = "root-old-hook"
        self.write_ready_marker(old_root, old_root)
        old_started = self.command(
            "start",
            self.project,
            cwd=self.temp_root,
            env_overrides=self.root_env(old_root, old_root),
        )
        old_run_id = str(old_started["run_id"])
        old_mapping_path = self.active_mapping_path(old_root, old_run_id)
        old_mapping = json.loads(old_mapping_path.read_text(encoding="utf-8"))
        self.command(
            "stop-for-root",
            self.project,
            "--run-id",
            old_run_id,
            "--root-thread-id",
            old_root,
        )
        self.confirm_cleanup(old_run_id)

        new_root = "root-current-hook"
        self.write_ready_marker(new_root, new_root)
        new_started = self.command(
            "start",
            self.project,
            cwd=self.temp_root,
            env_overrides=self.root_env(new_root, new_root),
        )
        new_run_id = str(new_started["run_id"])
        new_claim = self.claim(new_run_id, "translator_1")

        # Reinstall the old file to simulate a delayed/stale Stop event after
        # the new root has already started. The hook must fence only the run
        # named in this mapping and treat the old run as a harmless no-op.
        old_mapping_path.parent.mkdir(parents=True, exist_ok=True)
        old_mapping_path.write_text(json.dumps(old_mapping), encoding="utf-8")
        stale_hook = self.invoke_root_hook(
            {
                "hook_event_name": "Stop",
                "session_id": old_root,
                "cwd": str(self.temp_root.resolve()),
            },
            cwd=self.temp_root,
        )
        self.assertEqual(stale_hook.returncode, 0, stale_hook.stderr)
        self.assertEqual(json.loads(stale_hook.stdout), {})

        stale_run_result = self.invoke(
            "stop-for-root",
            self.project,
            "--run-id",
            old_run_id,
            "--root-thread-id",
            old_root,
        )
        self.assertEqual(stale_run_result.returncode, 0, stale_run_result.stderr)
        stale_run = json.loads(stale_run_result.stdout)
        self.assertFalse(stale_run["stopped"])
        self.assertEqual(stale_run["reason"], "ROOT_OR_RUN_MISMATCH")

        mismatched_root_result = self.invoke(
            "stop-for-root",
            self.project,
            "--run-id",
            new_run_id,
            "--root-thread-id",
            old_root,
        )
        self.assertEqual(mismatched_root_result.returncode, 0, mismatched_root_result.stderr)
        mismatched_root = json.loads(mismatched_root_result.stdout)
        self.assertFalse(mismatched_root["stopped"])
        self.assertEqual(mismatched_root["reason"], "ROOT_OR_RUN_MISMATCH")

        status = self.command("status", self.project)
        self.assertEqual(status["run"]["run_id"], new_run_id)
        self.assertEqual(status["run"]["status"], "RUNNING")
        self.assertEqual(status["running_attempt_count"], 1)
        with sqlite3.connect(self.project / "state.sqlite") as connection:
            attempt_state = connection.execute(
                "SELECT state FROM attempts WHERE attempt_id = ?",
                (new_claim["attempt_id"],),
            ).fetchone()[0]
        self.assertEqual(attempt_state, "RUNNING")

    def test_stop_hook_blocks_when_cwd_is_missing(self) -> None:
        result = self.invoke_root_hook(
            {
                "hook_event_name": "Stop",
                "session_id": "root-missing-cwd",
            },
            cwd=self.temp_root,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["decision"], "block")
        self.assertIn("cwd", payload["reason"].lower())

    def test_stop_hook_subprocess_fences_the_mapped_root_run(self) -> None:
        self.init_text("The actual Stop hook should invoke stop-for-root.")
        root_id = "root-hook-subprocess"
        start_hook = self.invoke_root_hook(
            {
                "hook_event_name": "SessionStart",
                "session_id": root_id,
                "cwd": str(self.temp_root.resolve()),
            },
            cwd=self.temp_root,
        )
        self.assertEqual(start_hook.returncode, 0, start_hook.stderr)
        self.assertEqual(json.loads(start_hook.stdout), {})
        stop_warmup = self.invoke_root_hook(
            {
                "hook_event_name": "Stop",
                "session_id": root_id,
                "cwd": str(self.temp_root.resolve()),
            },
            cwd=self.temp_root,
        )
        self.assertEqual(stop_warmup.returncode, 0, stop_warmup.stderr)
        self.assertEqual(json.loads(stop_warmup.stdout), {})

        started = self.command(
            "start",
            self.project,
            cwd=self.temp_root,
            env_overrides=self.root_env(root_id, root_id),
        )
        run_id = str(started["run_id"])
        claim = self.claim(run_id, "translator_1")
        stop_hook = self.invoke_root_hook(
            {
                "hook_event_name": "Stop",
                "session_id": root_id,
                "cwd": str(self.temp_root.resolve()),
            },
            cwd=self.temp_root,
        )
        self.assertEqual(stop_hook.returncode, 0, stop_hook.stderr)
        self.assertEqual(json.loads(stop_hook.stdout), {})

        status = self.command("status", self.project)
        self.assertEqual(status["run"]["status"], "STOPPED")
        self.assertEqual(status["running_attempt_count"], 0)
        candidate = self.temp_root / "hook-late-result.txt"
        candidate.write_text("Late Stop-hook response.", encoding="utf-8")
        self.expect_error(
            "interrupt epoch advanced for run",
            "commit",
            self.project,
            "--run-id",
            run_id,
            "--worker-id",
            "translator_1",
            "--attempt-id",
            claim["attempt_id"],
            "--file",
            candidate,
        )

    def test_interrupt_hook_subprocess_fences_the_mapped_root_run(self) -> None:
        self.init_text("A root interruption must fence the current attempt.")
        root_id = "root-interrupt-subprocess"
        self.write_ready_marker(root_id, root_id)
        started = self.command(
            "start",
            self.project,
            cwd=self.temp_root,
            env_overrides=self.root_env(root_id, root_id),
        )
        run_id = str(started["run_id"])
        claim = self.claim(run_id, "translator_1")

        interrupt_hook = self.invoke_root_hook(
            {
                "hook_event_name": "Interrupt",
                "session_id": root_id,
                "cwd": str(self.temp_root.resolve()),
            },
            cwd=self.temp_root,
        )
        self.assertEqual(interrupt_hook.returncode, 0, interrupt_hook.stderr)
        self.assertEqual(json.loads(interrupt_hook.stdout), {})
        status = self.command("status", self.project)
        self.assertEqual(status["run"]["status"], "STOPPED")
        self.assertEqual(status["running_attempt_count"], 0)
        candidate = self.temp_root / "interrupt-late-result.txt"
        candidate.write_text("Late result.", encoding="utf-8")
        self.expect_error(
            "interruption tombstone",
            "commit",
            self.project,
            "--run-id",
            run_id,
            "--worker-id",
            "translator_1",
            "--attempt-id",
            claim["attempt_id"],
            "--file",
            candidate,
        )

    def test_interrupt_timeout_during_guarded_start_requires_stop_before_restart(self) -> None:
        self.init_text("An Interrupt racing a guarded start must leave no active run.")
        root_id = "root-interrupt-start-race"
        self.write_ready_marker(root_id, root_id)
        root_env = self.root_env(root_id, root_id)
        base = self.temp_root / ".codex" / "translate-book-guard"
        lock_path = base / "locks" / f"{root_id}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        interrupted_marker = base / "interrupted" / f"{root_id}.json"

        child_env = os.environ.copy()
        child_env.pop("CODEX_THREAD_ID", None)
        child_env.pop("CODEX_SESSION_ID", None)
        child_env.update(root_env)
        start_process: subprocess.Popen[str] | None = None
        start_was_waiting_on_lock = False
        interrupt_result: subprocess.CompletedProcess[str] | None = None
        lock_held_seconds = 0.0

        with lock_path.open("a+b") as session_lock:
            fcntl.flock(session_lock.fileno(), fcntl.LOCK_EX)
            lock_acquired_at = time.monotonic()
            try:
                start_process = subprocess.Popen(
                    [sys.executable, str(RUNNER), "start", str(self.project)],
                    cwd=self.temp_root,
                    env=child_env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                time.sleep(0.15)
                start_was_waiting_on_lock = start_process.poll() is None

                interrupt_result = self.invoke_root_hook(
                    {
                        "hook_event_name": "Interrupt",
                        "session_id": root_id,
                        "cwd": str(self.temp_root.resolve()),
                    },
                    cwd=self.temp_root,
                )
                lock_held_seconds = time.monotonic() - lock_acquired_at
            finally:
                fcntl.flock(session_lock.fileno(), fcntl.LOCK_UN)

        self.assertGreaterEqual(lock_held_seconds, 2.3)
        self.assertTrue(start_was_waiting_on_lock, "guarded start did not wait for the session lock")
        self.assertIsNotNone(interrupt_result)
        assert interrupt_result is not None
        self.assertEqual(interrupt_result.returncode, 0, interrupt_result.stderr)
        interrupt_payload = json.loads(interrupt_result.stdout)
        self.assertIn("systemMessage", interrupt_payload)
        self.assertTrue(interrupted_marker.is_file())

        assert start_process is not None
        start_stdout, start_stderr = start_process.communicate(timeout=10)
        status = self.command("status", self.project)
        self.assertIsNone(status["current_run_id"], status)
        self.assertEqual(status["running_attempt_count"], 0, status)
        self.assertTrue(
            status["run"] is None or status["run"]["status"] != "RUNNING",
            f"guarded start left a running run: {status}",
        )
        self.assertTrue(
            start_process.returncode == 0 or "interrupt" in start_stderr.lower(),
            f"start failed for an unrelated reason: {start_stdout}\n{start_stderr}",
        )

        stop_hook = self.invoke_root_hook(
            {
                "hook_event_name": "Stop",
                "session_id": root_id,
                "cwd": str(self.temp_root.resolve()),
            },
            cwd=self.temp_root,
        )
        self.assertEqual(stop_hook.returncode, 0, stop_hook.stderr)
        self.assertEqual(json.loads(stop_hook.stdout), {})
        self.assertFalse(interrupted_marker.exists())

        restarted = self.command(
            "start",
            self.project,
            cwd=self.temp_root,
            env_overrides=root_env,
        )
        run_id = str(restarted["run_id"])
        self.assertEqual(self.command("status", self.project)["current_run_id"], run_id)

        final_stop = self.invoke_root_hook(
            {
                "hook_event_name": "Stop",
                "session_id": root_id,
                "cwd": str(self.temp_root.resolve()),
            },
            cwd=self.temp_root,
        )
        self.assertEqual(final_stop.returncode, 0, final_stop.stderr)
        self.assertEqual(json.loads(final_stop.stdout), {})

    def test_interrupt_then_stop_cannot_resurrect_delayed_open_project_start(self) -> None:
        self.init_text("A delayed start must observe an Interrupt even after Stop cleanup.")
        root_id = "root-delayed-open-race"
        self.write_ready_marker(root_id, root_id)
        start_process, entered_path, release_path = (
            self.launch_guarded_start_paused_in_open_project(root_id)
        )
        interrupt_result: subprocess.CompletedProcess[str] | None = None
        stop_result: subprocess.CompletedProcess[str] | None = None
        start_reached_open_project = False
        try:
            start_reached_open_project = self.wait_for_path(entered_path, start_process)

            if start_reached_open_project:
                interrupt_result = self.invoke_root_hook(
                    {
                        "hook_event_name": "Interrupt",
                        "session_id": root_id,
                        "cwd": str(self.temp_root.resolve()),
                    },
                    cwd=self.temp_root,
                )
                stop_result = self.invoke_root_hook(
                    {
                        "hook_event_name": "Stop",
                        "session_id": root_id,
                        "cwd": str(self.temp_root.resolve()),
                    },
                    cwd=self.temp_root,
                )
        finally:
            release_path.touch()

        start_stdout, start_stderr = start_process.communicate(timeout=15)
        self.assertTrue(start_reached_open_project, f"start did not reach _open_project: {start_stderr}")
        self.assertIsNotNone(interrupt_result)
        self.assertIsNotNone(stop_result)
        assert interrupt_result is not None
        assert stop_result is not None
        self.assertEqual(interrupt_result.returncode, 0, interrupt_result.stderr)
        self.assertEqual(json.loads(interrupt_result.stdout), {})
        self.assertEqual(stop_result.returncode, 0, stop_result.stderr)
        self.assertEqual(json.loads(stop_result.stdout), {})

        guard_dir = self.temp_root / ".codex" / "translate-book-guard"
        interrupted_marker = guard_dir / "interrupted" / f"{root_id}.json"
        epoch_path = guard_dir / "interrupt-epochs" / f"{root_id}.log"
        self.assertFalse(interrupted_marker.exists(), "Stop did not clear the Interrupt tombstone")
        self.assertTrue(epoch_path.is_file())
        self.assertGreaterEqual(epoch_path.stat().st_size, 1)

        status = self.command("status", self.project)
        self.assertIsNone(status["current_run_id"], status)
        self.assertEqual(status["running_attempt_count"], 0, status)
        self.assertTrue(
            status["run"] is None or status["run"]["status"] != "RUNNING",
            f"delayed start resurrected a running run: {status}; stdout={start_stdout}; stderr={start_stderr}",
        )
        mapping_dir = guard_dir / "active" / root_id
        self.assertEqual(list(mapping_dir.glob("*.json")) if mapping_dir.exists() else [], [])

        restarted = self.command(
            "start",
            self.project,
            cwd=self.temp_root,
            env_overrides=self.root_env(root_id, root_id),
        )
        run_id = str(restarted["run_id"])
        self.assertEqual(self.command("status", self.project)["current_run_id"], run_id)

        cleanup = self.invoke_root_hook(
            {
                "hook_event_name": "Stop",
                "session_id": root_id,
                "cwd": str(self.temp_root.resolve()),
            },
            cwd=self.temp_root,
        )
        self.assertEqual(cleanup.returncode, 0, cleanup.stderr)
        self.assertEqual(json.loads(cleanup.stdout), {})

    def test_stop_alone_cannot_resurrect_delayed_open_project_start(self) -> None:
        self.init_text("A delayed start must observe Stop even without a prior Interrupt.")
        root_id = "root-stop-delayed-open-race"
        self.write_ready_marker(root_id, root_id)
        epoch_path = (
            self.temp_root
            / ".codex"
            / "translate-book-guard"
            / "interrupt-epochs"
            / f"{root_id}.log"
        )
        epoch_size_before = epoch_path.stat().st_size if epoch_path.exists() else 0
        start_process, entered_path, release_path = (
            self.launch_guarded_start_paused_in_open_project(root_id)
        )
        stop_result: subprocess.CompletedProcess[str] | None = None
        start_reached_open_project = False
        try:
            start_reached_open_project = self.wait_for_path(entered_path, start_process)
            if start_reached_open_project:
                stop_result = self.invoke_root_hook(
                    {
                        "hook_event_name": "Stop",
                        "session_id": root_id,
                        "cwd": str(self.temp_root.resolve()),
                    },
                    cwd=self.temp_root,
                )
        finally:
            release_path.touch()

        start_stdout, start_stderr = start_process.communicate(timeout=15)
        self.assertTrue(
            start_reached_open_project,
            f"start did not reach _open_project: {start_stderr}",
        )
        self.assertIsNotNone(stop_result)
        assert stop_result is not None
        self.assertEqual(stop_result.returncode, 0, stop_result.stderr)
        self.assertEqual(json.loads(stop_result.stdout), {})
        self.assertTrue(epoch_path.is_file(), "Stop did not append the root epoch")
        self.assertGreater(epoch_path.stat().st_size, epoch_size_before)

        status = self.command("status", self.project)
        self.assertIsNone(status["current_run_id"], status)
        self.assertEqual(status["running_attempt_count"], 0, status)
        self.assertTrue(
            status["run"] is None or status["run"]["status"] != "RUNNING",
            f"Stop alone let delayed start resurrect a run: {status}; "
            f"stdout={start_stdout}; stderr={start_stderr}",
        )
        mapping_dir = (
            self.temp_root
            / ".codex"
            / "translate-book-guard"
            / "active"
            / root_id
        )
        self.assertEqual(list(mapping_dir.glob("*.json")) if mapping_dir.exists() else [], [])

        restarted = self.command(
            "start",
            self.project,
            cwd=self.temp_root,
            env_overrides=self.root_env(root_id, root_id),
        )
        run_id = str(restarted["run_id"])
        self.assertEqual(self.command("status", self.project)["current_run_id"], run_id)
        cleanup = self.invoke_root_hook(
            {
                "hook_event_name": "Stop",
                "session_id": root_id,
                "cwd": str(self.temp_root.resolve()),
            },
            cwd=self.temp_root,
        )
        self.assertEqual(cleanup.returncode, 0, cleanup.stderr)
        self.assertEqual(json.loads(cleanup.stdout), {})

    def test_worker_commands_from_other_cwd_respect_root_interrupt_tombstone(self) -> None:
        self.init_markdown("First chapter body.", "Second chapter body.")
        root_id = "root-cross-cwd-interrupt"
        self.write_ready_marker(root_id, root_id)
        started = self.command(
            "start",
            self.project,
            cwd=self.temp_root,
            env_overrides=self.root_env(root_id, root_id),
        )
        run_id = str(started["run_id"])
        active_claim = self.command(
            "claim",
            self.project,
            "--run-id",
            run_id,
            "--worker-id",
            "translator_1",
            cwd=self.temp_root,
        )
        self.assertTrue(active_claim["claimed"], active_claim)

        guard_dir = self.temp_root / ".codex" / "translate-book-guard"
        lock_path = guard_dir / "locks" / f"{root_id}.lock"
        interrupted_marker = guard_dir / "interrupted" / f"{root_id}.json"
        other_cwd = self.temp_root / "another-working-directory"
        other_cwd.mkdir()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as session_lock:
            fcntl.flock(session_lock.fileno(), fcntl.LOCK_EX)
            try:
                interrupt_result = self.invoke_root_hook(
                    {
                        "hook_event_name": "Interrupt",
                        "session_id": root_id,
                        "cwd": str(self.temp_root.resolve()),
                    },
                    cwd=self.temp_root,
                )
                self.assertEqual(interrupt_result.returncode, 0, interrupt_result.stderr)
                self.assertIn("systemMessage", json.loads(interrupt_result.stdout))
                self.assertTrue(interrupted_marker.is_file())

                root_status = self.command("status", self.project)
                self.assertEqual(root_status["run"]["status"], "RUNNING")
                self.assertEqual(root_status["current_run_id"], run_id)
                self.assertEqual(root_status["running_attempt_count"], 1)

                check_run = self.command(
                    "check-run",
                    self.project,
                    "--run-id",
                    run_id,
                    cwd=other_cwd,
                )
                self.assertFalse(check_run["may_claim"], check_run)
                self.assertFalse(check_run["may_commit"], check_run)

                rejected_claim = self.invoke(
                    "claim",
                    self.project,
                    "--run-id",
                    run_id,
                    "--worker-id",
                    "translator_2",
                    cwd=other_cwd,
                )
                self.assertNotEqual(rejected_claim.returncode, 0, rejected_claim.stdout)
                self.assertIn("interrupt", rejected_claim.stderr.lower())

                candidate = self.temp_root / "cross-cwd-late-result.txt"
                candidate.write_text("The root was interrupted.", encoding="utf-8")
                rejected_commit = self.invoke(
                    "commit",
                    self.project,
                    "--run-id",
                    run_id,
                    "--worker-id",
                    "translator_1",
                    "--attempt-id",
                    active_claim["attempt_id"],
                    "--file",
                    candidate,
                    cwd=other_cwd,
                )
                self.assertNotEqual(rejected_commit.returncode, 0, rejected_commit.stdout)
                self.assertIn("interrupt", rejected_commit.stderr.lower())

                rejected_fail = self.invoke(
                    "fail",
                    self.project,
                    "--run-id",
                    run_id,
                    "--worker-id",
                    "translator_1",
                    "--attempt-id",
                    active_claim["attempt_id"],
                    "--error",
                    "late worker failure after root interrupt",
                    cwd=other_cwd,
                )
                self.assertNotEqual(rejected_fail.returncode, 0, rejected_fail.stdout)
                self.assertIn("interrupt", rejected_fail.stderr.lower())

                with sqlite3.connect(self.project / "state.sqlite") as connection:
                    chunk_state, failure_count = connection.execute(
                        "SELECT state, failure_count FROM chunks WHERE chunk_id = ?",
                        (active_claim["chunk_id"],),
                    ).fetchone()
                    attempt_state = connection.execute(
                        "SELECT state FROM attempts WHERE attempt_id = ?",
                        (active_claim["attempt_id"],),
                    ).fetchone()[0]
                self.assertEqual(failure_count, 0)
                self.assertNotEqual(chunk_state, "FAILED")
                self.assertEqual(attempt_state, "RUNNING")
            finally:
                fcntl.flock(session_lock.fileno(), fcntl.LOCK_UN)
        still_running = self.command("status", self.project)
        self.assertEqual(still_running["run"]["status"], "RUNNING")
        self.assertEqual(still_running["current_run_id"], run_id)
        self.assertEqual(still_running["running_attempt_count"], 1)

        stop_result = self.invoke_root_hook(
            {
                "hook_event_name": "Stop",
                "session_id": root_id,
                "cwd": str(self.temp_root.resolve()),
            },
            cwd=self.temp_root,
        )
        self.assertEqual(stop_result.returncode, 0, stop_result.stderr)
        self.assertEqual(json.loads(stop_result.stdout), {})
        self.assertFalse(interrupted_marker.exists())
        stopped = self.command("status", self.project)
        self.assertEqual(stopped["run"]["status"], "STOPPED")
        self.assertIsNone(stopped["current_run_id"])

    def test_stop_hook_ignores_tampered_mapping_runner_path(self) -> None:
        self.init_text("An active mapping cannot choose the Stop executable.")
        root_id = "root-untrusted-mapping"
        self.write_ready_marker(root_id, root_id)
        started = self.command(
            "start",
            self.project,
            cwd=self.temp_root,
            env_overrides=self.root_env(root_id, root_id),
        )
        run_id = str(started["run_id"])
        claim = self.claim(run_id, "translator_1")

        marker_path = self.temp_root / "untrusted-runner-was-used"
        injected_runner = self.temp_root / "untrusted-runner.py"
        injected_runner.write_text(
            "from pathlib import Path\n"
            f"Path({str(marker_path)!r}).write_text('used', encoding='utf-8')\n"
            "print('{\"stopped\": true}')\n",
            encoding="utf-8",
        )
        mapping_path = self.active_mapping_path(root_id, run_id)
        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
        mapping["runner_path"] = str(injected_runner)
        mapping_path.write_text(json.dumps(mapping), encoding="utf-8")

        hook_result = self.invoke_root_hook(
            {
                "hook_event_name": "Stop",
                "session_id": root_id,
                "cwd": str(self.temp_root.resolve()),
            },
            cwd=self.temp_root,
        )

        self.assertEqual(hook_result.returncode, 0, hook_result.stderr)
        self.assertEqual(json.loads(hook_result.stdout), {})
        self.assertFalse(marker_path.exists(), "hook executed the untrusted runner path")
        status = self.command("status", self.project)
        self.assertEqual(status["run"]["status"], "STOPPED")
        candidate = self.temp_root / "tampered-mapping-late.txt"
        candidate.write_text("Late response after trusted Stop.", encoding="utf-8")
        self.expect_error(
            "interrupt epoch advanced for run",
            "commit",
            self.project,
            "--run-id",
            run_id,
            "--worker-id",
            "translator_1",
            "--attempt-id",
            claim["attempt_id"],
            "--file",
            candidate,
        )

    def test_one_root_run_at_a_time_and_next_project_starts_after_stop(self) -> None:
        self.init_text("First project root-owned paragraph.")
        second_project = self.temp_root / "second-project"
        second_source = self.temp_root / "second-book.txt"
        second_source.write_text("Second project root-owned paragraph.", encoding="utf-8")
        self.command("init", second_source, "--project", second_project)

        root_id = "root-multiple-projects"
        hook_cwd = str(self.temp_root.resolve())
        start_hook = self.invoke_root_hook(
            {
                "hook_event_name": "SessionStart",
                "session_id": root_id,
                "cwd": hook_cwd,
            },
            cwd=self.temp_root,
        )
        self.assertEqual(start_hook.returncode, 0, start_hook.stderr)
        self.assertEqual(json.loads(start_hook.stdout), {})
        stop_warmup = self.invoke_root_hook(
            {
                "hook_event_name": "Stop",
                "session_id": root_id,
                "cwd": hook_cwd,
            },
            cwd=self.temp_root,
        )
        self.assertEqual(stop_warmup.returncode, 0, stop_warmup.stderr)
        self.assertEqual(json.loads(stop_warmup.stdout), {})

        root_env = self.root_env(root_id, root_id)
        first_started = self.command(
            "start", self.project, cwd=self.temp_root, env_overrides=root_env
        )
        first_run_id = str(first_started["run_id"])
        first_claim = self.claim(first_run_id, "translator_1")

        rejected_second_start = self.invoke(
            "start",
            second_project,
            cwd=self.temp_root,
            env_overrides=root_env,
        )
        self.assertNotEqual(rejected_second_start.returncode, 0, rejected_second_start.stdout)

        mapping_dir = (
            self.temp_root
            / ".codex"
            / "translate-book-guard"
            / "active"
            / root_id
        )
        self.assertEqual(
            {path.name for path in mapping_dir.glob("*.json")},
            {f"{first_run_id}.json"},
        )
        with sqlite3.connect(second_project / "state.sqlite") as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)

        first_stop_hook = self.invoke_root_hook(
            {
                "hook_event_name": "Stop",
                "session_id": root_id,
                "cwd": hook_cwd,
            },
            cwd=self.temp_root,
        )
        self.assertEqual(first_stop_hook.returncode, 0, first_stop_hook.stderr)
        self.assertEqual(json.loads(first_stop_hook.stdout), {})

        first_status = self.command("status", self.project)
        self.assertEqual(first_status["run"]["run_id"], first_run_id)
        self.assertEqual(first_status["run"]["status"], "STOPPED")
        self.assertEqual(first_status["running_attempt_count"], 0)
        candidate = self.temp_root / "first-project-late.txt"
        candidate.write_text("Late answer after root Stop.", encoding="utf-8")
        self.expect_error(
            "interrupt epoch advanced for run",
            "commit",
            self.project,
            "--run-id",
            first_run_id,
            "--worker-id",
            "translator_1",
            "--attempt-id",
            first_claim["attempt_id"],
            "--file",
            candidate,
        )
        self.assertEqual(list(mapping_dir.glob("*.json")), [])

        # Once Stop has removed the first run's active mapping, the same root
        # may safely begin work in another project.
        second_started = self.command(
            "start", second_project, cwd=self.temp_root, env_overrides=root_env
        )
        second_run_id = str(second_started["run_id"])
        second_claim = self.command(
            "claim",
            second_project,
            "--run-id",
            second_run_id,
            "--worker-id",
            "translator_1",
        )
        self.assertTrue(second_claim["claimed"], second_claim)

        second_stop_hook = self.invoke_root_hook(
            {
                "hook_event_name": "Stop",
                "session_id": root_id,
                "cwd": hook_cwd,
            },
            cwd=self.temp_root,
        )
        self.assertEqual(second_stop_hook.returncode, 0, second_stop_hook.stderr)
        self.assertEqual(json.loads(second_stop_hook.stdout), {})
        second_status = self.command("status", second_project)
        self.assertEqual(second_status["run"]["run_id"], second_run_id)
        self.assertEqual(second_status["run"]["status"], "STOPPED")
        self.assertEqual(second_status["running_attempt_count"], 0)
        second_candidate = self.temp_root / "second-project-late.txt"
        second_candidate.write_text("Late answer after second root Stop.", encoding="utf-8")
        self.expect_error(
            "interrupt epoch advanced for run",
            "commit",
            second_project,
            "--run-id",
            second_run_id,
            "--worker-id",
            "translator_1",
            "--attempt-id",
            second_claim["attempt_id"],
            "--file",
            second_candidate,
        )


if __name__ == "__main__":
    unittest.main()
