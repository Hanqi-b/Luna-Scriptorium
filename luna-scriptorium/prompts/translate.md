# One-chunk translation turn

The root supplies the project path, run ID, and fixed worker ID. Handle at most one chunk; never spawn another agent.

1. Call `check-run` and require `may_claim`; then call `claim` once. If the host explicitly confirms the root is absent or terminated, exit. If root status is merely uninspectable, do not infer death: use `check-run` as the validity check.
2. Translate the returned source completely and faithfully into its `target_language`. Detect the source language from the text when `source_language` is `auto`. Use the provided prior context for continuity without repeating it. **Do not summarize, omit, silently compress, explain what the author left unexplained, or expand or rewrite the argument.** Preserve meaningful names, dates, quotations, notes, headings, paragraph/list structure, and Markdown markers. Produce natural, publication-quality prose in the target language while preserving the source faithfully.
3. Write the complete candidate to an attempt-specific UTF-8 file. Recheck `check-run`; if the host explicitly reports the root terminated, exit. If the root is merely uninspectable but the run is valid, commit with the exact run ID, worker ID, attempt ID, and file path. A numeric SUSPECT flag goes to review; it does not discard a complete translation.
4. On translation failure, call `fail` while the run remains valid. If the run is terminal, exit; its stop transaction fenced the attempt. End this turn after one commit or failure. The root sends the next job to this same identity.
