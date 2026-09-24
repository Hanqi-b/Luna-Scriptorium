---
name: translate-book
description: Translate a book from verified UTF-8 Markdown with four supervised GPT-6 Luna Max Codex subagents and resumable state. If the original is EPUB, PDF, or another format, first help prepare faithful Markdown or ask the user to provide it; the translation runner accepts Markdown/TXT and outputs Markdown.
---

# Translate a book

`translate_book.py` owns project state, chapter and chunk claims, attempts, atomic checkpoints, and Markdown output. Codex subagents translate. Python never calls a model API. Never create one agent per chunk.

## Stage 1 — prepare the source Markdown

The original document is an input to preparation, not to the translation runner. The root or user must produce a complete UTF-8 Markdown file first. Use available document tools when they can preserve reading order and text; otherwise ask the user for a prepared Markdown file. Do not invent missing pages or silently treat a short excerpt as a whole book. Inspect the result for missing or duplicated chapters, navigation/TOC text, footnotes, quotations, and chapter order. Use one top-level `#` heading per intended chapter; retain lower-level headings, paragraphs, and meaningful Markdown structure. The source stays editable during this stage. No worker claims and no translation project start here.

## Stage 2 — check and lock the complete chunk plan

After the Markdown is ready, run the runner's read-only `plan SOURCE --chunking-version 2`. Inspect its chapter summary, chunk counts and maximum sizes, coverage result, warnings, and source/plan hashes. Resolve structural warnings by correcting the Markdown in Stage 1 and planning again. A real one-chapter book may be accepted after inspection; an accidental one-chapter conversion must be corrected before proceeding. The root performs this check without per-chunk user confirmation. Ask the user only when the source itself is ambiguous or incomplete and the root cannot resolve it.

Run `init` for a new project with `--chunking-version 2`, `--expected-source-sha256`, and `--expected-plan-sha256` from that plan. Set known languages explicitly, for example `--source-language fr --target-language zh-CN`. `init` copies and hashes the agreed Markdown and persists the ordered chunks in SQLite. Treat this as the lock: do not change the source or repartition an initialized project. If the hash comparison fails, return to Stage 1 and plan again. `start` verifies the locked plan before workers are dispatched. Stage 2 contains **all** chapter/chunk decisions; workers never choose their own boundaries.

Run the CLI from the repository root as `python3 translate-book/translate_book.py ...`. The plan is noninteractive; pass its two SHA-256 values to `init` automatically. Version 1 remains available only to reproduce old paragraph-level plans; new projects use version 2.

## Authorization and pool

A request to translate a whole book authorizes the four configured workers and ordinary retries. Do not ask for approval between workers, chunks, or chapters. Ask only for a material translation choice, ambiguous input, or unrecoverable failure.

Require four available subagent slots. Create exactly four named translation agents with `fork_turns: "none"`, `model: "gpt-6-luna"`, and `reasoning_effort: "max"`; omit `agent_type` so a role configuration cannot override the model. Verify each effective model with the host. Never silently substitute another model or effort. Root coordinates; it does not translate or semantically review text. Workers must not spawn descendants.

## Run and dispatch

1. Complete Stages 1 and 2 before starting a new book. For an existing project, inspect `status`; never overwrite or silently reparse it.
2. Before `start`, inspect the prior run and its four expected worker paths from `status`. If the prior root died, run `invalidate-run PROJECT --run-id OLD` first. Interrupt any remaining old workers and confirm their slots are released. Only then run `confirm-cleanup PROJECT --run-id OLD` with one `--released-worker translator_N` for each host-confirmed released identity and `--not-spawned-worker translator_N` only for an identity verified never spawned. All four observations are required. The runner records root's attestation; it cannot independently inspect Codex. If the host cannot inspect or release an old worker, do not attest cleanup or start a new run. A bare `start` must never take over a still-active run.
3. Use this Skill only in a trusted Codex project whose `.codex/hooks.json` `SessionStart`, `Stop`, and `Interrupt` hooks have been reviewed and enabled. A root must first finish one harmless warmup turn in that same session so `SessionStart` and `Stop` both write their markers. A guarded `start` refuses to run without both markers. The markers show those hooks executed earlier in this session; they cannot prove that the host will keep hooks enabled later, so verify hook activation in the target Desktop build. Run `start PROJECT --root-agent-path ROOT_PATH` and retain `run_id` and the four expected worker paths. The runner binds the run to the current `CODEX_THREAD_ID` and registers it with the guard before publishing `RUNNING`. Spawn exactly those four identities once, passing the root path, project path, run ID, and worker ID. Each initial task or `followup_task` processes **at most one chunk**: check root and run, claim once, translate, check again, commit or fail, then end the turn. Dispatch the next chunk to the **same** agent identity only after its prior turn ends. Chapter ownership stays with that worker in SQLite.
4. Workers use the runner CLI for every claim, commit, and failure. A candidate goes in an attempt-specific file; only the runner may mark a chunk `DONE`. Every mutation includes the correct run, worker, and attempt IDs. Workers never edit SQLite directly.
5. Poll both host agent status and runner status. Database `RUNNING` means an attempt was claimed; it does not prove an agent is alive. If `claim` reports an expired existing attempt or a worker disappears, interrupt that worker if still active, verify its old turn is inactive, then call `interrupt-worker` before reusing its identity. An interrupted chunk blocks later chunks in its chapter until supervised recovery. Do not replace a worker until the previous turn is inactive. At most four translation agents may be active.
6. After all chunks are committed, confirm all four agent identities are inactive and their slots released, run `confirm-cleanup PROJECT --run-id RUN` with four `--released-worker` observations, then `build` and report.

## Worker contract

Before claim and before commit, inspect the exact run through `check-run` and inspect the root through `list_agents`. Require `may_claim` before claiming and `may_commit` before committing, plus a running root. If either is invalid or uninspectable, interrupt the current attempt if possible and exit. The runner independently enforces run and attempt fencing inside its write transaction. An attempt deadline is only an auxiliary limit on a stale result; it cannot terminate a model turn.

Use the claimed source and prior committed context. Preserve meaning, paragraph structure, numbers, names, and Markdown markers. Write only the complete candidate, then commit and **end the turn**. Never claim another chunk in that turn. If its deadline expires or commit rejects it while the run remains active, call `interrupt-worker` to release that attempt before exiting. On a tool or translation failure, call `fail` when possible and end the turn. Do not request routine approval from root or user.

## Force-stop-first

On a stop request while root is alive, stop dispatching followups and immediately run `stop PROJECT --run-id RUN` (`force-stop` is an alias). Its **single database transaction** makes the run terminal and interrupts unfinished attempts before any host agent interruption. Current uncommitted chunks may be discarded. Then interrupt every active worker and inspect host status every few seconds until all four identities are inactive and their slots released. If an interrupted identity still occupies a slot, retry interruption once and, if supported, send one cleanup-only followup to that **same** identity. Run `confirm-cleanup` with one explicit observation for each of the four workers only after host observation. The total cleanup wait is at most 60 seconds. If it expires or a second stop ends the wait, leave cleanup unverified and refuse a new run.

The project `Stop` hook records a durable cancellation epoch and synchronously fences every active run mapped to this root before a normal root turn can finish. The `Interrupt` hook records the same epoch and a temporary tombstone, then attempts the fence when the root turn is interrupted. Guarded start compares its entry epoch through commit; claim and commit check the stored root guard directory and epoch. A successful `Stop` clears the tombstone while preserving the epoch so an older, delayed start stays cancelled. If `start` reports an interruption tombstone after Ctrl+C, let a normal root turn finish so `Stop` can fence and clear it before restarting. Root then observes and releases all four workers. Hook fencing does not itself prove host worker termination. `stop-for-root` checks the exact root session and run IDs in one write transaction, so an old hook cannot stop a newer run. Database `running_attempt_count=0` proves data fencing, **not** worker termination. A root killed by SIGKILL cannot execute hooks or force-stop, so recovery must invalidate that old run before starting another pool; workers must check root and run at chunk boundaries. Do not claim instant orphan termination after a desktop root kill.

Committed `DONE` chunks remain `DONE`. Interrupted, uncommitted chunks are eligible for retranslation only after old worker cleanup is verified. An old run or attempt must never overwrite a newer committed result.

## Scope

The runner accepts UTF-8 Markdown and TXT and produces Markdown. Document-to-Markdown preparation is a separate first stage; this Skill does not promise native EPUB/PDF parsing, OCR, layout preservation, EPUB export, semantic review, or final quality review. Keep project paths relative to the project root and preserve the copied source.
