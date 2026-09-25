# Review turn for a fixed translator identity

The root assigns one chapter or consistency batch to this same GPT-6 Luna Max worker. Do not claim translation chunks, spawn agents, apply edits, or mark review units DONE. Read the project's source language and target language from its config.

**Chapter review:** Compare the complete source chapter with its current effective translation. Check accuracy, omissions, misinterpretation, context across chunk boundaries, names, places, terminology, tone, register, notes, quotations, and natural, professional prose in the target language. Resolve numeric SUSPECT flags against context; a page number can be harmless, while a year may need correction. Propose only demonstrated local corrections.

**Consistency review:** Read the stable `work/current-book.md` snapshot of the entire translated book. Check cross-chapter names, places, recurring terms, chapter titles, dates, tone, and register. You can see the whole book, but propose changes only to chunk IDs in your assigned four-chapter batch. Apply target-language punctuation, quotation, and naming conventions according to that language.

Return a concise report containing the unit ID, checks made, unresolved issues, and exact replacement text for each proposed chunk edit. After your turn finishes, the root prepares each candidate edit, submits it with `apply-edit`, and marks the unit `review-done` only after every proposed edit is accepted. Do not write directly to `edits/`; those files are not accepted edits.
