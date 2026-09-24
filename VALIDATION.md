# Validation

## Workflow reliability

Run `python3 -m unittest discover -s tests -p 'test_runner.py' -q`. CI runs the same suite on pushes and pull requests. Tests use synthetic translations and never call a model. The golden mini-book fixture covers chapter detection, chunk order, numeric SUSPECT, context selection, accepted edit provenance, QA recomputation, snapshot/build order, and four target/source configurations.

The suite also covers plan/source integrity, four concurrent claims, chapter ownership, atomic complete-candidate commit, stale run/attempt rejection, force-stop resume, FAILED persistence and explicit targeted retry, raw edit-file rejection, stable full-book snapshot, review gates, and final Markdown output. It proves these runner state transitions on test fixtures; it does not prove host agent termination.

| Direction | Synthetic workflow | Semantic translation quality |
| --- | --- | --- |
| FR → ZH | Tested | Not yet validated for this revision |
| EN → ZH | Tested | Not yet validated |
| ZH → EN | Tested | Not yet validated |
| FR → EN | Tested | Not yet validated |

The earlier Candide work is a historical FR→ZH preparation/lifecycle case, not evidence that this revised pipeline produced a reviewed book. A short real-model sample in each direction is the next manual quality check; it should inspect omissions, meaning, register, target-language naturalness, and review corrections. A full real-book run is a separate validation step.

## Lifecycle boundary

`stop` atomically invalidates the run and its unfinished attempts; DONE chunks stay DONE. A later run rejects old results. Ordinary `start` does not reset FAILED chunks. Abrupt root death can leave an old host turn running until the next root fences it. The runner's SQLite state is data fencing, not host-worker termination proof; SIGKILL cascade behavior remains a host limitation. No heartbeat, lease, or Desktop hook marker is required for startup.

## Input/output boundary

The root handles actual book extraction with bounded available tools. The runner accepts Markdown/TXT work units. Canonical output is Markdown; EPUB reconstruction is optional only after structural verification. EPUB/PDF extraction quality, OCR, reconstructed EPUB integrity, semantic review quality, and effective four-worker model selection are not covered by this synthetic suite.
