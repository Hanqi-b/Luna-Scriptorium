# Luna Scriptorium

Luna Scriptorium is a resumable whole-book translation workflow. A Codex root agent prepares the input, then supervises four fixed GPT-6 Luna Max workers through translation, chapter review, whole-book consistency review, and final output. The source and target may be different languages; the runner has no Chinese-only path.

## Quick start

In Codex, invoke:

```text
$luna-scriptorium 翻译 <book> 到 <target language>
```

The user provides the book and target language once. The root detects the source language, prepares and checks the text, locks chapters/chunks, starts the four-worker pool, manages retries and review, and returns the output path. `状态` asks for progress; `停止` fences the current run. Normal internal stages need no further confirmation. See [SKILL.md](luna-scriptorium/SKILL.md) for the operational contract.

## Input and languages

Give the root a file or folder: EPUB, PDF, DOCX, HTML, TXT, Markdown, and other reasonably readable books are handled using tools available in the environment. The root makes a bounded extraction/OCR attempt and reports the exact blockage if it cannot reliably obtain the body text. The state runner takes stable UTF-8 Markdown/TXT; it is deliberately not a format-adapter framework.

`source_language` can be detected by the root or left as `auto`; `target_language` is set from the user's request. The same runner accepts FR→ZH, EN→ZH, ZH→EN, FR→EN, and other directions without separate language-pair code. This is a workflow capability, not a claim of publication-quality translation for every pair.

## Workflow and output

1. Inspect input and prepare faithful ordered source text; lock a verified chapter/chunk plan.
2. Start exactly four GPT-6 Luna Max workers. Each turn translates one chunk; DONE chunks survive stop and resume. Old run/attempt results cannot overwrite later state.
3. Reuse the same workers for chapter review. Workers propose corrections; the root prepares candidate files, submits each through `apply-edit`, then marks the chapter `review-done` after acceptance. Direct files in `edits/` never enter the final book.
4. Create `work/current-book.md` from accepted chapter edits. Reuse the same workers for consistency review with a stable full-book view and local batch responsibility. Consistency corrections follow the same candidate → `apply-edit` → `review-done` order.
5. Build the canonical translated **Markdown** file in `output/`. A reconstructed EPUB is optional when the root can preserve and verify its structure.

Progress reports use actual DONE/total chunks, chapter review units, and consistency units. Numeric differences are flagged for human-style model review; they do not discard a complete chunk. A FAILED chunk remains FAILED across ordinary starts until the root diagnoses it and explicitly retries that chunk.

## Limits and validation

SQLite fencing protects committed state, but it cannot itself terminate a host worker after abrupt root death. The next root must fence the old run and clear observable old turns before reuse. Extraction, OCR, EPUB reconstruction, effective host model selection, and translation quality need real host validation. [VALIDATION.md](VALIDATION.md) separates tested workflow mechanics from unverified language-pair quality.

Run the synthetic suite locally:

```bash
python3 -m unittest discover -s tests -p 'test_runner.py' -q
```

GitHub Actions runs the same tests on pushes and pull requests. The license is [MIT](LICENSE).

## Local Skill discovery

The repository's `luna-scriptorium/` directory is the Skill. Link it into the personal discovery directory if needed:

```bash
mkdir -p "$HOME/.agents/skills"
ln -s "$(pwd)/luna-scriptorium" "$HOME/.agents/skills/luna-scriptorium"
```
