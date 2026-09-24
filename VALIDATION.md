# Luna Scriptorium validation

## Current verification

Run `python3 -m unittest discover -s tests -p 'test_runner.py' -q`. The synthetic CLI suite checks locked source/plan integrity, marker-free start, four concurrent claims, chapter ownership, numeric SUSPECT flags, structural rejection, attempt/run fencing, stop/resume without cleanup attestation, worker failure isolation, actual review-unit counts, review gates, edits, and final Markdown build. It does **not** make model calls or test a real book.

The runner stores only durable work state. The root inspects and converts the actual input with reasonably available tools; a project starts only after the extracted source is checked and plan-locked. The host must make all four GPT-6 Luna Max identities available before translation starts. Chapter and consistency review use those same identities. Review progress is the count of persisted completed units, not an estimate.

## Lifecycle boundaries

`stop` atomically makes the run terminal, invalidates current attempts, and returns unfinished chunks to PENDING; DONE chunks remain unchanged. The root then interrupts and observes the four host agents. A later run rejects commits from older run/attempt IDs. A live run is never silently replaced.

SQLite state does not prove host worker termination. After abrupt root death, the old run remains active until the next root fences it; an in-flight old worker may still finish before that fence. The next root must inspect and interrupt old host identities before reuse. The runner does not claim instant cascade cancellation or a measured orphan lifetime. SIGKILL remains a host limitation. The deleted Desktop hooks are not a lifecycle dependency.

## Scope of evidence

CLI tests prove state transitions and output assembly using synthetic inputs. Real EPUB/PDF extraction quality, OCR quality, semantic review quality, effective model selection, and physical host worker cleanup require host-level book runs and are **not** proved by these tests. This repository's previous pilot results are historical; do not reinterpret them as validation of the current simplified flow.
