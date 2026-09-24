# Phase 1.5 lifecycle acceptance, staged chunking, and small EPUB pilot

Date: 2026-09-24. The lifecycle gate is **PASS for normal Codex Stop and Interrupt events** in the tested Desktop/CLI host. SIGKILL is a separate host capability limit. The EPUB pilot below uses a short excerpt extracted from a real EPUB; native EPUB import/export is not implemented.

## What changed

- A guarded `start` binds each run to the root Codex thread. `SessionStart` and `Stop` readiness markers are optional observations, not authorization or startup requirements. A new project can start with neither marker present.
- The runner publishes the run mapping before it can commit `RUNNING`. A per-session file lock serializes that publication and the SQLite transaction with the hook's scan. One root can have only one active guarded translation run.
- Project `Stop` and `Interrupt` hooks call `stop-for-root`. Its `BEGIN IMMEDIATE` transaction compares the exact current `run_id` and `root_thread_id`, changes the run to `STOPPED`, interrupts unfinished attempts, and clears the current run. Old or mismatched mappings cannot stop a newer run. The hook derives the runner executable from its own trusted path, not from writable mapping data.
- Both `Stop` and `Interrupt` append and sync a durable per-session cancellation epoch before waiting for the start/stop lock. `Interrupt` also publishes a temporary tombstone. Guarded `start` snapshots the epoch at command entry and checks it through transaction commit and after releasing the lock. A late cancellation stops a committed new run before `start` returns. The run stores its root guard directory and epoch, so guarded claim and commit check the same source even from another working directory. A successful `Stop` clears the temporary tombstone; the durable epoch remains to cancel an already-running `start`. This closes the reproduced pre-mapping and lock-timeout races.
- The hooks are project-local in `.codex/hooks.json`. Codex requires their exact definitions to be reviewed and trusted. The trust step was completed in the CLI `/hooks` UI, and a subsequent invocation **without** `--dangerously-bypass-hook-trust` executed `SessionStart` and `Stop`. A separate Desktop task also executed both hooks. A marker proves that a hook ran earlier; it cannot prove that the host will keep that hook enabled later. [Codex hook behavior](https://learn.chatgpt.com/docs/hooks).

## Data fencing and worker termination are different

**Data fencing:** The database rejects a stale run or attempt during the commit transaction. The persisted root guard directory and cancellation epoch also reject old claim, commit, and fail calls if a hook has started but database fencing times out. `DONE` chunks do not roll back. An uncommitted attempt interrupted by Stop is eligible for retranslation only after old-worker cleanup is confirmed.

**Worker termination:** SQLite `running_attempt_count=0` does not prove an agent stopped. The root must observe all four agent identities idle or interrupted and then record four cleanup observations. A normal root final while workers are still busy is fenced by `Stop`, but those workers can continue their current one-chunk turn until they next check root and run state.

## Real host acceptance

| Case | Evidence | Result |
| --- | --- | --- |
| Normal root final with four active workers, Desktop | Disposable Desktop root `01a0d307-f53b-79c2-bdfc-d95517712026` had four `gpt-6-luna` / `max` workers and four RUNNING attempts. It requested final at 10:51:07 UTC. `Stop` fenced run `dc7b5ec30680429cb02e5d687af7b37a` at 10:51:12 UTC, the same second the root turn completed. Status immediately showed STOPPED, current run null, four INTERRUPTED chunks, zero RUNNING attempts. Old commit exited 2. | **PASS: no post-final valid-run window** |
| Worker exit after that normal root final | The four Desktop worker turns became idle at 10:51:54, 10:52:03, 10:52:09, and 10:52:27 UTC. Each checked the stopped run and exited without commit. The maximum observed delay after root completion was **75 seconds**. Cleanup was recorded only after all four host identities were observed idle. | **PASS observed; not instantaneous** |
| Literal Ctrl+C with four active workers, CLI host | A separate root had four existing `gpt-6-luna` / `max` workers active with one attempt each. Ctrl+C invoked `Interrupt`, fenced run `5166c441ec4a44fe8d4b57ef09d2e3d1` at 10:41:37 UTC, and all four host turns finished as `interrupted` at that timestamp. Status showed STOPPED and four INTERRUPTED chunks. Four inactive observations were then recorded. | **PASS in CLI host** |
| Interrupt while guarded start is blocked | A regression test held the root's session lock beyond the 2.3-second Interrupt deadline. The real hook timed out on that lock but had already published the tombstone. When the lock was released, `start` left no current or RUNNING run. A later successful Stop removed the tombstone and a new guarded start succeeded. | **PASS** |
| Interrupt and Stop before start publishes a mapping | A test paused the start subprocess inside project open, then invoked the real Interrupt and Stop hooks. Stop cleared the temporary tombstone without finding a mapping, but the durable epoch still caused the delayed start to reject. No current or RUNNING run remained; a fresh start succeeded. | **PASS** |
| Normal Stop before start publishes a mapping | A test paused guarded start during project open, then invoked the real Stop hook while no mapping existed. Stop advanced the durable epoch, so the delayed start could not create a RUNNING run after the root turn ended. | **PASS** |
| Worker invokes runner from a different directory | With an active guarded run and Interrupt held until its lock timeout, `check-run` reported both decision flags false; claim, commit, and fail from a different cwd rejected using the run's stored guard directory and epoch. The failed call did not increment `failure_count` or mark the chunk FAILED. | **PASS** |
| Reuse and repeated stop | The same four worker thread IDs were reused through `followup_task` for run `b0ac625782b14cd1a69cc6147e49f256`. Each claimed one chunk. Ctrl+C fenced the run at 10:54:46 UTC; all four turns were interrupted and cleanup was confirmed. A third run reused the same four identities and also fenced four active attempts at 10:57:47 UTC. | **PASS** |
| Second Ctrl+C | After the first Ctrl+C, the root process had already exited before a separately sent second keypress. A two-keypress burst produced one observable `Interrupt` hook and a stopped run. The force-stop-first path has no graceful wait to skip. A distinct second live-root callback was **not observable**. | **Safe state observed; second callback N/A** |
| Long-running worker / timeout | In the real four-worker probes, workers were deliberately held in `sleep 120`. Ctrl+C interrupted those turns before sleep completed, none of their attempts became DONE, and all four host turns became inactive. Resume was gated on cleanup observation. | **PASS for force-stop-first** |
| Late old result | A commit for an attempt from stopped run `5166c441ec4a44fe8d4b57ef09d2e3d1` exited 2 with “not the project's current run.” A stopped Desktop run's late commit was also rejected. | **PASS** |
| Stop/resume and DONE monotonicity | After verified cleanup, a new run resumed interrupted work. One chunk was committed DONE; two further stop/resume cycles kept DONE=1. The next chapter claim advanced to `ch001_c002` rather than retranslating DONE `ch001_c001`. | **PASS** |
| One worker failure does not stop others | Covered by the automated runner suite and an earlier Phase 0/1 live worker-failure run. No new four-worker failure injection was needed for the root-hook change. | **PASS, regression evidence** |
| Four-worker / no per-chunk confirmation | Host thread records confirmed four worker identities with model `gpt-6-luna`, effort `max`. The same identities were reused across turns and runs. No per-chunk user confirmation was requested. | **PASS** |

The 10:27 CLI normal-final probe also confirmed that four RUNNING database attempts were fenced by a synchronous `Stop` hook. The Desktop probe above is the stronger host-specific result. Before this change, a separate Desktop normal-final probe left `RUNNING` and `may_commit=true` until an external observer invalidated the run; that defect is now closed when the trusted Stop hook executes.

## Acceptance matrix

| Guarantee | Result |
| --- | --- |
| Normal graceful shutdown leaves 0 active workers | **PASS** when root follows the Skill cleanup sequence; the EPUB pilot ended with four host identities idle and cleanup verified. |
| Forced shutdown leaves 0 observable active workers | **PASS in tested CLI host:** literal Ctrl+C interrupted four Luna Max turns and all were observed inactive. |
| One worker failure does not stop other workers | **PASS** in automated regression and prior Phase 0/1 live run. |
| Root death cannot corrupt translation state | **PASS for trusted Stop/Interrupt events**; SIGKILL is excluded and detailed below. |
| Old run commit is rejected | **PASS**, including real stopped Desktop and CLI runs. |
| Root death orphan lifetime is bounded | **One-chunk work bound** with observed self-exit after normal root final; no hard wall-clock bound if a model turn hangs. |
| Maximum observed orphan lifetime | **75 seconds** after a normal Desktop root final; SIGKILL not observable. |
| Four GPT-6 Luna Max worker constraint preserved | **PASS**, including repeated `followup_task` reuse of the same four thread IDs. |
| No per-chunk user confirmation | **PASS**. |

## SIGKILL: separate host limit

There is no exposed task-scoped SIGKILL operation for a Desktop root. The Desktop tasks share an app-server process; killing it would also remove the observer. No SIGKILL test is claimed as PASS. SIGKILL cannot run `Stop` or `Interrupt`, so the Skill cannot promise immediate fencing or worker termination in that case. Recovery must call `invalidate-run`, observe/release old workers, and record `confirm-cleanup` before starting another pool. Pre-recovery commits from a still-current run remain a known host-loss risk. The one-chunk-per-turn contract limits the amount of ongoing work to at most four chunks, but it does not provide a hard wall-clock termination bound for a stuck model turn.

## Automated verification

`python3 -m unittest discover -s tests -p 'test_runner.py' -q`: **42 passed**. Coverage includes start without either lifecycle marker reaching the four-worker pool, optional and malformed marker behavior, additive migration, exact root/run fencing, Stop and Interrupt hook subprocesses, both Interrupt/start races, the Stop/start race, different-cwd worker fencing for check-run/claim/commit/fail, malformed hook cwd, stale mappings, one active guarded pool per root, mapping tampering, STOP/commit serialization, cleanup gating, attempt deadlines, failure isolation, repeated resume, and the chunk-plan checks. `py_compile` passed for the runner and hook. `quick_validate.py luna-scriptorium` reported `Skill is valid!`.

## Source preparation and deterministic chunking

The Skill now separates source preparation from chunking. The root or user first prepares and inspects a complete UTF-8 Markdown file from the original document. The runner does not convert EPUB/PDF. Only after Markdown is ready does the root run read-only `plan`, inspect chapter counts, chunk sizes, coverage and warnings, then call `init` with the source and plan SHA-256 values. `init` persists a versioned, ordered chunk plan; `start` verifies its hash against database rows before dispatch. Existing initialized projects retain their stored v1 chunks.

New v2 chunking keeps Markdown H1 headings as chapter boundaries, ignores H1-shaped lines inside fenced code, and combines adjacent short blocks within a chapter up to 4,000 characters. Long blocks split at newline, sentence, or whitespace boundaries before a hard character cut. The plan checks that the ordered non-whitespace content of all chunks equals the source and flags absent H1 headings, unclosed code fences, and unavoidable hard cuts. It cannot prove that the converted Markdown omitted no EPUB pages or that chapter headings are semantically correct; Stage 1 inspection remains necessary.

On the real EPUB-derived Markdown excerpt used below, v2 `plan` reported **4 chapters, 4 chunks, full source coverage, and no warnings**; the old paragraph-level rule made 8 chunks. This is a planning check on a small excerpt, not a full-book v2 translation result.

## Small real EPUB input pilot

Source: the user's local English EPUB, `The Burgundians: A Vanished Empire`. Four distinct narrative XHTML spine entries (`021`, `023`, `024`, `026`) were read in order. The first substantial paragraph from each was flattened to text: 444, 495, 326, and 366 characters. The 1,631-character excerpt was stored as Markdown with four chapter headings; this is a deliberately small input adapter test, not native EPUB support.

The trusted Desktop root reused four `gpt-6-luna` / `max` workers. Eight chunks (one heading and one paragraph per chapter) reached DONE with one chunk per worker turn. The root observed all four identities idle, recorded cleanup at 11:05:14 UTC, and built `output/excerpt.v001.md`. Final run status was COMPLETED, DONE=8, RUNNING=0. No per-chunk confirmation was requested.

After the cancellation-epoch and stored-cwd changes, the same Desktop root reused the four worker identities for a **fresh final-code run** `0923ef9267544fa5a1fee06ffbb69220` on the same real EPUB excerpt. It recorded the root guard directory and epoch, completed all 8/8 chunks across four chapters, observed all four worker identities INACTIVE, recorded cleanup at 11:42:10 UTC, and built a new translated output. Final status was COMPLETED, current run null, RUNNING attempts 0. No per-chunk confirmation was requested.

Pilot files (local, ignored by Git): `.artifacts/epub-pilot-2026-09-24/provenance.json` and `.artifacts/epub-pilot-2026-09-24/translation/output/excerpt.v001.md`. Native EPUB parsing, styling, navigation, image handling, and EPUB output remain outside this pilot.

Final-code replay (local, ignored by Git): `.artifacts/epub-pilot-2026-09-24-final/translation/output/excerpt.v001.md`.

**READY FOR REAL BOOK TEST — lifecycle gate only.** Full-book EPUB production still requires a separate EPUB format phase. SIGKILL remains an explicit host limitation, not a passed Skill guarantee.
