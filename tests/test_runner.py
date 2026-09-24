"""CLI regressions for locked book units, four claims, fencing, review, and output."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "luna-scriptorium" / "translate_book.py"


class RunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.source = self.base / "book.md"
        self.project = self.base / "project"
        self.source.write_text("# Chapitre 1\n\nL'année 1759.\n\nSuite du récit.\n\n# Chapitre 2\n\nAutre texte.\n", encoding="utf-8")

    def call(self, *args: object, ok: bool = True, env: dict[str, str] | None = None) -> dict:
        child_env = os.environ.copy()
        child_env.pop("CODEX_THREAD_ID", None)
        child_env.pop("CODEX_SESSION_ID", None)
        if env:
            child_env.update(env)
        result = subprocess.run([sys.executable, str(RUNNER), *(str(arg) for arg in args)], cwd=ROOT, env=child_env, text=True, capture_output=True)
        if ok and result.returncode:
            self.fail(f"{args}: {result.stderr}")
        if not ok:
            self.assertNotEqual(result.returncode, 0, result.stdout)
            return json.loads(result.stderr) if result.stderr.startswith("{") else {"error":result.stderr}
        return json.loads(result.stdout)

    def init(self) -> dict:
        plan = self.call("plan", self.source)
        return self.call("init", self.source, "--project", self.project, "--expected-source-sha256",plan["source_sha256"],"--expected-plan-sha256",plan["plan_sha256"])

    def start(self) -> str:
        return self.call("start", self.project)["run_id"]

    def claim(self, run: str, worker: str = "translator_1") -> dict:
        return self.call("claim",self.project,"--run-id",run,"--worker-id",worker)

    def commit(self, run: str, claim: dict, text: str = "# 第一章\n\n译文") -> dict:
        candidate = self.base / f"{claim['attempt_id']}.md"
        candidate.write_text(text,encoding="utf-8")
        return self.call("commit",self.project,"--run-id",run,"--worker-id",claim["worker_id"],"--attempt-id",claim["attempt_id"],"--file",candidate)

    def commit_error(self, run: str, claim: dict, text: str) -> dict:
        candidate = self.base / "stale.md"
        candidate.write_text(text,encoding="utf-8")
        return self.call("commit",self.project,"--run-id",run,"--worker-id",claim["worker_id"],"--attempt-id",claim["attempt_id"],"--file",candidate,ok=False)

    def finish_translation(self) -> str:
        self.init()
        run = self.start()
        while True:
            job = self.claim(run)
            if not job["claimed"]:
                break
            heading = "# 译章" if job["source"].startswith("#") else "译文 1759。"
            self.commit(run,job,heading)
        self.assertEqual(self.call("status",self.project)["chunks"]["states"]["DONE"],2)
        return run

    def review_all(self) -> None:
        status = self.call("status",self.project)
        for stage in ("chapter","consistency"):
            for unit in status["reviews"][stage]["units"]:
                report = self.base / f"{stage}-{unit['unit_id']}.md"
                report.write_text("已与原文核对；无未解决问题。",encoding="utf-8")
                self.call("review-done",self.project,"--stage",stage,"--unit-id",unit["unit_id"],"--file",report)

    def test_plan_read_only_coverage_and_locked_hashes(self):
        plan = self.call("plan",self.source)
        self.assertTrue(plan["coverage_ok"])
        self.assertEqual((plan["chapters"],plan["chunks"]),(2,2))
        self.assertFalse(self.project.exists())
        self.init()
        with sqlite3.connect(self.project/"state.sqlite") as db:
            db.execute("UPDATE chunks SET source='tampered' WHERE chunk_id='ch001_c001'")
        self.assertIn("stored chunk source changed",self.call("start",self.project,ok=False)["error"])

    def test_init_rejects_changed_source_and_existing_project(self):
        plan = self.call("plan",self.source)
        self.source.write_text("# Changed\n\nText",encoding="utf-8")
        err = self.call("init",self.source,"--project",self.project,"--expected-source-sha256",plan["source_sha256"],ok=False)
        self.assertIn("source SHA-256 mismatch",err["error"])
        self.assertFalse(self.project.exists())
        self.init()
        self.assertIn("must be empty",self.call("init",self.source,"--project",self.project,ok=False)["error"])

    def test_long_paragraph_plan_preserves_order_and_size(self):
        paragraph = "Longue phrase française. " * 300
        self.source.write_text(f"# Chapitre 1\n\n{paragraph}\n",encoding="utf-8")
        plan = self.call("plan",self.source)
        self.assertGreater(plan["chunks"],1)
        self.assertLessEqual(plan["max_chunk_chars"],4000)
        self.assertTrue(plan["coverage_ok"])
        self.init()
        with sqlite3.connect(self.project/"state.sqlite") as db:
            pieces = [row[0] for row in db.execute("SELECT source FROM chunks ORDER BY chunk_number")]
        self.assertEqual("".join("".join(piece.split()) for piece in pieces),"".join(self.source.read_text(encoding="utf-8").split()))

    def test_no_hook_markers_reaches_worker_stage(self):
        self.init()
        started = self.call("start",self.project,env={"CODEX_THREAD_ID":"root-test","CODEX_SESSION_ID":"root-test"})
        self.assertEqual(started["status"],"RUNNING")
        self.assertEqual(list(started["worker_paths"]),[f"translator_{n}" for n in range(1,5)])
        self.assertTrue(self.claim(started["run_id"])["claimed"])
        self.assertEqual(self.call("status",self.project)["running_attempt_count"],1)
        self.assertFalse((self.base/".codex").exists())

    def test_four_concurrent_claims_one_chapter_owner(self):
        self.source.write_text("\n\n".join(f"# Chapitre {n}\n\nTexte {n}." for n in range(1,6)),encoding="utf-8")
        self.init(); run = self.start()
        with ThreadPoolExecutor(max_workers=4) as pool:
            jobs = list(pool.map(lambda n: self.claim(run,f"translator_{n}"),range(1,5)))
        self.assertTrue(all(job["claimed"] for job in jobs))
        self.assertEqual(len({job["chapter_id"] for job in jobs}),4)
        self.assertEqual(self.call("status",self.project)["running_attempt_count"],4)
        self.assertIn("already has a RUNNING",self.call("claim",self.project,"--run-id",run,"--worker-id","translator_1",ok=False)["error"])

    def test_four_workers_complete_synthetic_book_and_review(self):
        self.source.write_text("\n\n".join(f"# Chapitre {n}\n\nTexte {n}." for n in range(1,9)),encoding="utf-8")
        self.init(); run = self.start()
        def worker(number: int) -> int:
            count = 0
            while True:
                job = self.claim(run,f"translator_{number}")
                if not job["claimed"]:
                    break
                self.commit(run,job,f"# 译章 {number}\n\n译文。")
                count += 1
            return count
        with ThreadPoolExecutor(max_workers=4) as pool:
            counts = list(pool.map(worker,range(1,5)))
        self.assertEqual(sum(counts),8)
        self.assertEqual(self.call("status",self.project)["chunks"]["states"],{"DONE":8})
        self.review_all()
        built = self.call("build",self.project)
        self.assertEqual(built["chunks"],8)
        self.assertTrue(Path(built["output"]).is_file())

    def test_worker_reuses_chapter_for_next_chunk(self):
        self.source.write_text("# Chapitre 1\n\n"+("Bonjour. "*700)+"\n\n# Chapitre 2\n\nFin.",encoding="utf-8")
        self.init(); run = self.start()
        first = self.claim(run)
        self.commit(run,first,"# 译章\n\n你好。")
        second = self.claim(run)
        self.assertEqual(first["chapter_id"],second["chapter_id"])
        self.assertNotEqual(first["attempt_id"],second["attempt_id"])

    def test_numeric_mismatch_commits_and_flags(self):
        self.init(); run = self.start(); job = self.claim(run)
        result = self.commit(run,job,"# 第一章\n\n正文无年份。")
        self.assertTrue(result["committed"])
        self.assertEqual(result["suspect_missing_numbers"],["1759"])
        status = self.call("status",self.project)
        self.assertEqual(status["chunks"]["states"]["DONE"],1)
        self.assertEqual(status["qa_flags"][0]["code"],"SUSPECT_MISSING_NUMBERS")

    def test_structure_and_empty_candidate_fail_closed(self):
        self.init(); run = self.start(); job = self.claim(run)
        self.assertIn("heading marker",self.commit_error(run,job,"第一章" )["error"])
        self.assertIn("empty",self.commit_error(run,job,"   ")["error"])
        self.assertEqual(self.call("status",self.project)["running_attempt_count"],1)
        self.commit(run,job,"# 第一章")

    def test_missing_candidate_never_marks_done(self):
        self.init(); run = self.start(); job = self.claim(run)
        absent = self.base/"absent.md"
        error = self.call("commit",self.project,"--run-id",run,"--worker-id","translator_1","--attempt-id",job["attempt_id"],"--file",absent,ok=False)
        self.assertIn("file not found",error["error"])
        self.assertEqual(self.call("status",self.project)["chunks"]["states"].get("DONE",0),0)
        self.commit(run,job,"# 第一章")

    def test_wrong_worker_and_attempt_cannot_commit(self):
        self.init(); run = self.start(); job = self.claim(run)
        candidate = self.base/"candidate.md"; candidate.write_text("# 译章",encoding="utf-8")
        err = self.call("commit",self.project,"--run-id",run,"--worker-id","translator_2","--attempt-id",job["attempt_id"],"--file",candidate,ok=False)
        self.assertIn("mismatched attempt",err["error"])
        self.commit(run,job,"# 译章")
        self.assertIn("stale, interrupted, or mismatched",self.commit_error(run,job,"# 再次")["error"])

    def test_stop_fences_attempt_and_resume_keeps_done(self):
        self.source.write_text("# One\n\n"+("A long paragraph. "*250)+"\n\n# Two\n\nMore.",encoding="utf-8")
        self.init(); old = self.start()
        done_job = self.claim(old); self.commit(old,done_job,"# 一\n\n译文")
        active = self.claim(old)
        result = self.call("stop",self.project,"--run-id",old)
        self.assertEqual(result["interrupted"],1)
        status = self.call("status",self.project)
        self.assertIsNone(status["current_run_id"])
        self.assertEqual(status["running_attempt_count"],0)
        self.assertEqual(status["chunks"]["states"]["DONE"],1)
        self.assertIn("no longer accepting",self.commit_error(old,active,"译文")["error"])
        new = self.start()
        self.assertNotEqual(new,old)
        self.assertEqual(self.call("status",self.project)["chunks"]["states"]["DONE"],1)
        self.assertTrue(self.claim(new)["claimed"])
        self.assertIn("no longer accepting",self.commit_error(old,active,"旧结果")["error"])

    def test_stop_and_commit_race_never_loses_done_or_accepts_stale(self):
        self.init(); run = self.start(); job = self.claim(run)
        candidate = self.base/"race.md"; candidate.write_text("# 译章",encoding="utf-8")
        def do_commit():
            return subprocess.run([sys.executable,str(RUNNER),"commit",str(self.project),"--run-id",run,"--worker-id","translator_1","--attempt-id",job["attempt_id"],"--file",str(candidate)],capture_output=True,text=True)
        with ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(do_commit)
            stopped = self.call("stop",self.project,"--run-id",run)
            result = future.result()
        status = self.call("status",self.project)
        if result.returncode == 0:
            self.assertEqual(status["chunks"]["states"].get("DONE",0),1)
        else:
            self.assertEqual(status["chunks"]["states"].get("DONE",0),0)
        self.assertEqual(status["running_attempt_count"],0)
        self.assertIn(stopped["status"],("STOPPED","COMPLETED"))

    def test_duplicate_start_refuses_live_run(self):
        self.init(); run = self.start()
        self.assertIn("still active",self.call("start",self.project,ok=False)["error"])
        self.assertEqual(self.call("status",self.project)["current_run_id"],run)

    def test_interrupted_attempt_reclaims_same_chunk_and_rejects_old_result(self):
        self.init(); run = self.start(); old = self.claim(run)
        self.call("interrupt-worker",self.project,"--run-id",run,"--worker-id","translator_1")
        replacement = self.claim(run)
        self.assertEqual(old["chunk_id"],replacement["chunk_id"])
        self.assertIn("stale",self.commit_error(run,old,"# 旧译")["error"])
        self.commit(run,replacement,"# 新译")

    def test_failed_worker_does_not_stop_other_worker(self):
        self.init(); run = self.start(); first = self.claim(run)
        self.call("fail",self.project,"--run-id",run,"--worker-id","translator_1","--attempt-id",first["attempt_id"],"--error","synthetic failure")
        other = self.claim(run,"translator_2")
        self.assertTrue(other["claimed"])
        self.assertEqual(other["chapter_id"],"ch002")
        self.assertEqual(self.call("check-run",self.project,"--run-id",run)["may_claim"],True)

    def test_exhausted_failure_resets_on_new_run_without_redoing_done(self):
        self.init(); run = self.start()
        first = self.claim(run); self.commit(run,first,"# 第一章")
        for number in range(3):
            job = self.claim(run)
            result = self.call("fail",self.project,"--run-id",run,"--worker-id","translator_1","--attempt-id",job["attempt_id"],"--error",f"failure {number}")
        self.assertFalse(result["retryable"])
        self.assertEqual(self.call("status",self.project)["chunks"]["states"],{"DONE":1,"FAILED":1})
        self.call("stop",self.project,"--run-id",run)
        next_run = self.start()
        self.assertEqual(self.call("status",self.project)["chunks"]["states"],{"DONE":1,"PENDING":1})
        self.assertEqual(self.claim(next_run)["chunk_id"],"ch002_c001")

    def test_review_progress_uses_locked_units_and_build_gated(self):
        self.finish_translation()
        status = self.call("status",self.project)
        self.assertEqual((status["reviews"]["chapter"]["done"],status["reviews"]["chapter"]["total"]),(0,2))
        self.assertEqual((status["reviews"]["consistency"]["done"],status["reviews"]["consistency"]["total"]),(0,1))
        self.assertIn("reviews are DONE",self.call("build",self.project,ok=False)["error"])
        report = self.base/"report.md"; report.write_text("Checked",encoding="utf-8")
        self.assertIn("finish chapter",self.call("review-done",self.project,"--stage","consistency","--unit-id","batch001","--file",report,ok=False)["error"])
        self.review_all()
        done = self.call("status",self.project)["reviews"]
        self.assertEqual((done["chapter"]["done"],done["consistency"]["done"]),(2,1))
        self.assertTrue(self.call("build",self.project)["built"])

    def test_duplicate_review_completion_rejected(self):
        self.finish_translation()
        report = self.base/"report.md"; report.write_text("Checked",encoding="utf-8")
        self.call("review-done",self.project,"--stage","chapter","--unit-id","ch001","--file",report)
        error = self.call("review-done",self.project,"--stage","chapter","--unit-id","ch001","--file",report,ok=False)
        self.assertIn("already DONE",error["error"])
        self.assertEqual(self.call("status",self.project)["reviews"]["chapter"]["done"],1)

    def test_review_edit_is_in_final_output(self):
        self.finish_translation(); self.review_all()
        edit = self.project/"edits"/"ch001_c001.md"; edit.write_text("# 审校后第一章",encoding="utf-8")
        built = self.call("build",self.project)
        output = Path(built["output"]).read_text(encoding="utf-8")
        self.assertIn("审校后第一章",output)
        self.assertIn("译章",output)
        self.assertEqual(built["edits_applied"],1)

    def test_complete_project_does_not_create_new_run(self):
        run = self.finish_translation()
        result = self.call("start",self.project)
        self.assertTrue(result["already_completed"])
        self.assertIsNone(result["run_id"])
        self.assertEqual(self.call("status",self.project)["run"]["run_id"],run)

    def test_source_copy_integrity(self):
        self.init()
        (self.project/"source"/"book.md").write_text("tampered",encoding="utf-8")
        self.assertIn("source integrity",self.call("status",self.project,ok=False)["error"])


if __name__ == "__main__":
    unittest.main()
