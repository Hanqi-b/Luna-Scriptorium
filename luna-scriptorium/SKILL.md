---
name: luna-scriptorium
description: Multilingual whole-book translation between a root-identified source language and a user-selected target language, using four fixed GPT-6 Luna Max Codex workers with review and resumable Markdown output.
---

# Luna Scriptorium

`$luna-scriptorium 翻译 <book> 到 <target language>` authorizes the complete multilingual whole-book workflow. By default, the root identifies the source language automatically; a user-specified source language takes precedence. If the target is missing and cannot be inferred, ask for that goal; never ask for internal lifecycle, conversion, worker, chunk, review, or build confirmation. `状态` and `停止` are optional user commands.

## Prepare and lock

The root inspects the file or folder and uses reasonably available tools to extract ordered UTF-8 Markdown/TXT. EPUB: OPF/spine/XHTML; PDF: text layer, then available OCR if scanned; DOCX, HTML, TXT, Markdown, or folders: extract their actual body. Check chapter order, notes, quotations, captions, and omissions. Try a direct extractor and sensible fallback, then report a concrete blockage if reliable text remains unavailable. Preserve the original. Preparation agents, if any, must exit before translation starts. The user does not convert or clean files manually.

Run `python3 luna-scriptorium/translate_book.py plan SOURCE`; inspect coverage, chapters, warnings, chunk sizes, and order. Correct extraction problems and plan again. Run `init SOURCE --project PROJECT --target-language TARGET --expected-source-sha256 HASH --expected-plan-sha256 HASH`; pass `--source-language` only when reliably known, otherwise retain `auto`. The root supplies these internal arguments. `init` copies and locks the source/plan. On a repeat invocation, use `status`; keep DONE chunks and never silently repartition the project.

## Translate with four fixed workers

Before `start`, ensure all preparation agents are gone and four host slots can be used. If a prior run remains active after root loss, stop that exact run, interrupt observable old workers, and ensure their turns are inactive before reuse. `start PROJECT` refuses a live run and needs no hook or marker. Spawn exactly `translator_1`–`translator_4` with `fork_turns: "none"`, `model: "gpt-6-luna"`, `reasoning_effort: "max"`; omit `agent_type` and verify the effective host model. If the pool cannot reach four, fence the run and recover or report the host blocker. No planner, critic, reviewer pool, or per-chunk agents. The root coordinates; it does not translate or semantically review text itself.

Each worker follows [prompts/translate.md](prompts/translate.md): `check-run` → one `claim` → translate → `check-run` → one `commit` or `fail` → end turn. The root sends the next `followup_task` to that same agent identity after its prior turn ends. Chapter ownership remains with that worker. If a worker disappears, verify the old host turn is inactive before `interrupt-worker` and reuse. A merely uninspectable root is not proven dead; runner run validity governs claims and commits. SQLite attempts do not prove host activity.

A numeric discrepancy commits with a SUSPECT flag for review. Empty/structurally broken output is rejected. Other workers continue after one worker fails. Ordinary `start` leaves FAILED chunks untouched; if a chunk exhausts retries, inspect and correct the cause, then use `retry-failed PROJECT --chunk-id ID --reason REASON` only for that specific chunk. Stop rather than loop indefinitely on a persistent blocker.

## Review, snapshot, and output

After all chunks are DONE, assign chapter review units to the **same four workers** using [prompts/review.md](prompts/review.md). For each proposed correction, the worker returns exact replacement text and a report; the root prepares a UTF-8 candidate edit file, calls `apply-edit PROJECT --stage chapter --unit-id chNNN --chunk-id CHUNK --file CANDIDATE --review-file REPORT`, and checks that it was accepted. Only after all proposed edits for that chapter succeed does the root call `review-done PROJECT --stage chapter --unit-id chNNN --file REPORT`. If no edit is needed, the root records the completed review directly with `review-done`. The runner checks the DONE chunk, unit scope, structure, review provenance, and current numeric QA; it stores accepted text and hash atomically. A file merely appearing in `edits/` never changes output.

After every chapter unit is DONE, call `snapshot PROJECT`. This creates `work/current-book.md` from the effective translation, including accepted chapter edits. All four consistency workers read this stable full-book view, while each proposes changes only within its assigned batch: `batch001` covers chapters 1–4, `batch002` covers 5–8, etc. For each proposed correction, the root prepares a UTF-8 candidate edit file, calls `apply-edit PROJECT --stage consistency --unit-id batchNNN --chunk-id CHUNK --file CANDIDATE --review-file REPORT`, and checks acceptance. Only then does it call `review-done PROJECT --stage consistency --unit-id batchNNN --file REPORT` with the matching report; if no edit is needed, it calls `review-done` directly. These are ordinary followups to the existing worker identities, not a second scheduler.

When all review units are DONE, run `build PROJECT`. The canonical text output is Markdown assembled from original committed translations plus **runner-accepted** edits. Verify completeness, chapter order, source/target languages, output existence, and unresolved QA flags. For EPUB input, reconstruct an EPUB only when the retained spine/assets can be preserved and the result verified; provide it alongside Markdown. Report the final path and short QA summary. Release the four workers and confirm no active turns remain.

## Progress and stop

Report book title, detected source → target, stage, real unit counts, current chapters, and material anomalies at preparation completion, pool start, roughly 10/25/50/75% of DONE/total chunks, review transitions, final build, and periodically during long quiet periods. Chapter and consistency progress each use their own persisted DONE/total unit counts. Never invent a subjective percentage or make users interpret run IDs, attempts, or SQLite rows.

On stop, stop dispatching and call `stop PROJECT --run-id RUN` **before** interrupting/waiting for host workers. This invalidates current attempts and returns uncommitted chunks to PENDING while preserving DONE. The next invocation resumes without cleanup attestation. After abrupt root death, an old host turn may persist until the next root fences its run. SQLite fencing prevents old commits after invalidation; it does not terminate host agents or promise instant zero orphans. Do not reintroduce heartbeat, leases, hook gates, or cancellation generations.
