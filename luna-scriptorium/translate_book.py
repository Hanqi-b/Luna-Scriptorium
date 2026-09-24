#!/usr/bin/env python3
"""Command-line entry point for Luna Scriptorium's book state runner."""
from __future__ import annotations

import argparse
import sqlite3
import sys
from typing import Any, Sequence
from scriptorium.common import ProjectError, DEFAULT_CHUNKING_VERSION, _dump
from scriptorium.chunks import _command_plan
from scriptorium.project import (
    _command_init, _command_start, _command_claim, _command_commit, _command_fail,
    _command_stop, _command_interrupt_worker, _command_check_run, _command_status,
    _command_review_done, _command_apply_edit, _command_retry_failed,
    _command_snapshot, _command_build,
)

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
    init.add_argument("--target-language",required=True)
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
    edit = commands.add_parser("apply-edit")
    edit.add_argument("project")
    edit.add_argument("--stage",required=True,choices=("chapter","consistency"))
    edit.add_argument("--unit-id",required=True)
    edit.add_argument("--chunk-id",required=True)
    edit.add_argument("--file",required=True)
    edit.add_argument("--review-file",required=True)
    retry = commands.add_parser("retry-failed")
    retry.add_argument("project")
    retry.add_argument("--chunk-id",required=True)
    retry.add_argument("--reason",required=True)
    commands.add_parser("snapshot").add_argument("project")
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
    if args.command=="apply-edit": return _command_apply_edit(args.project,args.stage,args.unit_id,args.chunk_id,args.file,args.review_file)
    if args.command=="retry-failed": return _command_retry_failed(args.project,args.chunk_id,args.reason)
    if args.command=="snapshot": return _command_snapshot(args.project)
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
