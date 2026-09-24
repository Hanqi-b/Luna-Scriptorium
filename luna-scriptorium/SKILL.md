---
name: luna-scriptorium
description: Translate a whole book into Chinese from a book file or folder. The root prepares readable source text, then supervises four GPT-6 Luna Max Codex workers through translation, chapter review, consistency review, and final Markdown output with resumable SQLite state.
---

# Luna Scriptorium

A request such as `$luna-scriptorium 翻译 <book>` authorizes the complete workflow. `状态` reports current book progress; `停止` fences the current run and interrupts workers. Do not request another confirmation for format conversion, internal markers, worker startup, retries, chapters, chunks, review, or building output. Ask only when the source cannot be read reliably, the translation goal genuinely needs clarification, or the host cannot provide four required workers after reasonable recovery.

## 1. Prepare the book

The root identifies the actual file or folder, then uses tools already available in the environment to extract a faithful, ordered UTF-8 Markdown or TXT source. For EPUB, read OPF/spine/XHTML in reading order; for PDF, inspect the text layer and use available OCR for scans; for DOCX, HTML, TXT, Markdown, or a folder, inspect and extract as appropriate. Remove navigation noise without losing content; check chapters, notes, quotations, images/captions, reading order, and obvious omissions. The root may use preparation agents if useful, but all preparation agents must exit before the translation pool starts. The user need not install tools, convert files, run OCR, or make a chunk plan.

Make a bounded plan before conversion: try the direct extractor and a sensible fallback; use available OCR when the text layer is unusable. Stop once those reasonable methods fail to produce reliable body text, and report the concrete failure. Never invent missing pages. Keep the original input intact. The runner deliberately accepts only stable Markdown/TXT work units; format handling belongs to the root, not a growing adapter framework.

## 2. Lock work units

Run `python3 luna-scriptorium/translate_book.py plan SOURCE` and inspect chapter order, coverage, warnings, chunk counts, and maximum chunk size. Correct extraction problems and plan again. A genuine one-chapter book is valid after inspection. Initialize with `init SOURCE --project PROJECT --expected-source-sha256 HASH --expected-plan-sha256 HASH`, supplying known source/target languages. `init` copies the source and locks its plan. Never silently repartition an existing project. For an existing project, start with `status`; reuse its stored source and plan, including already DONE chunks.

## 3. Start one four-worker pool

Before `start`, inspect host agents and ensure all four `translator_1` through `translator_4` slots can be used; preparation agents must have exited. If an old run is active after root loss, stop that exact run first, interrupt any observable old workers, and confirm those identities are inactive before reusing them. A bare `start` refuses a still-active run. No hook or marker is required.

Run `start PROJECT`. Spawn exactly four fixed Codex subagents with `fork_turns: "none"`, `model: "gpt-6-luna"`, `reasoning_effort: "max"`, and names `translator_1` to `translator_4`; do not set an overriding agent type. Check the host's effective model/effort. If the pool cannot reach four, fence the run and recover or report the host blocker. Never silently substitute a model, create per-chunk agents, or add reviewer/critic agents. The root orchestrates and does not translate or semantically review the book itself.

Each worker turn handles one chunk: `check-run`, `claim`, translate using source and prior chapter context, `check-run` again, write an attempt-specific candidate file, `commit` or `fail`, then end the turn. Use [prompts/translate.md](prompts/translate.md). The root issues the next `followup_task` to the **same identity** only after the previous turn finishes. Chapter affinity is kept by the runner. A failed worker does not stop the other three. If a worker disappears, wait until its old host turn is inactive, call `interrupt-worker` to release its attempt, and resume that same identity. Database attempt state does not prove host activity.

A numeric mismatch is a `SUSPECT_MISSING_NUMBERS` flag on an otherwise committed chunk, particularly important for front matter. It goes to review; it does not discard a whole translated chunk. Empty or structurally broken candidates are rejected. All committed `DONE` chunks survive stop and restart.

If a chunk exhausts its ordinary retries, let the other workers continue. Inspect the failure, stop and resume the same project once if a corrected retry is reasonable; report a persistent blocker instead of looping indefinitely.

## 4. Review with the same workers and build

After translation reaches `DONE / total chunks`, reuse the four worker identities for chapter review. Assign each `chNNN` review unit to an available worker, including flagged numbers, omissions, tone, footnotes, and source fidelity. Use [prompts/review.md](prompts/review.md). Workers return a report and proposed chunk edits to the root; they do not directly alter final `edits/` or review state. The root checks that the worker turn finished, applies verified proposed corrections as `PROJECT/edits/CHUNK_ID.md`, and calls `review-done PROJECT --stage chapter --unit-id chNNN --file REPORT`.

Then use the **same four workers** for whole-book consistency review. `batch001` covers chapters 1–4, `batch002` covers 5–8, etc.; check names, places, recurring terms, register, and cross-chapter consistency. The root applies verified corrections through the same `edits/` files and calls `review-done ... --stage consistency --unit-id batchNNN --file REPORT`. The root coordinates assignments with ordinary followups; there is no second worker registry or claim system. After all review units are DONE, run `build PROJECT`; verify output exists, contains all chapters in order, and reflects edits. For EPUB input, use the preparation-stage spine/asset map to rebuild an EPUB when the structure can be preserved, then inspect reading order, links, and text coverage. Provide the final Markdown, any verified rebuilt book file, and a short QA summary including unresolved issues. Release the four workers and confirm no active turns remain.

## 5. Progress and stops

The root reports concise book-level progress at preparation completion, pool start, roughly 10/25/50/75% of **actual DONE chunks**, chapter/batch completion, review transitions, final build, and periodically during long quiet periods. Calculate translation progress from `status.chunks.states.DONE / status.chunks.total`; chapter and consistency progress come from `status.reviews.<stage>.done / total`. Keep stages separate; never invent a subjective percentage. Explain material anomalies briefly while handling routine retries and SUSPECT flags internally. User-facing updates prioritize book title, chapter, stage counts, active work, and output path, not run IDs or SQLite details.

For a normal stop, stop dispatching, call `stop PROJECT --run-id RUN` **before** interrupting host agents, then interrupt/wait until the four identities are inactive. The stop transaction invalidates active attempts and returns unfinished chunks to PENDING. A later invocation resumes automatically. After abrupt root death, the host may leave a turn running; the next root fences the old run before a new one and an old run/attempt cannot commit into that new run. SQLite fencing protects data after invalidation; it cannot itself terminate a host agent or promise instant zero orphans. Do not describe DB `running_attempt_count=0` as proof of host termination.
