# One-chunk translation turn

The root supplies the project path, run ID, and fixed worker ID. Do not spawn agents or process more than one chunk in this turn.

1. Call `check-run`; require `may_claim`. Inspect the root agent in the host. If root is absent or uninspectable, exit.
2. Call `claim` once. If no work or stale run, exit. Translate only the returned source, using its previous chapter context for continuity. Preserve meaning, tone, names, dates, quotations, footnotes, lists, and Markdown structure. Do not copy context into the translation.
3. Write the complete translation to an attempt-specific UTF-8 file. Recheck `check-run` and the root. If valid, call `commit` with the exact run ID, worker ID, attempt ID, and file path. A numeric SUSPECT flag is for later review; do not discard a complete translation because of it.
4. If translation fails, call `fail` with a short reason while the run remains valid. If root or run has ended, exit; the stop transaction fences the attempt. End the turn after one commit or failure. The root will send the next job to this same agent identity.
