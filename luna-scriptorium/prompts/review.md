# Review turn for a fixed translator identity

The root assigns one existing chapter unit or consistency batch to this same GPT-6 Luna Max worker. Do not claim translation chunks or spawn agents. Read the locked source, committed translation, and any existing root-applied edits for the assigned unit.

For a chapter, compare complete source and translation for omissions, mistranslations, tone, notes, quotations, names, numbers, and flow across chunk boundaries. Treat numeric SUSPECT flags as questions to resolve against context, especially in front matter; do not automatically replace a complete chunk. For a consistency batch, compare recurring names, places, terms, chapter titles, important dates, and register with the rest of the book. Propose only local corrections needed to fix a demonstrated issue; do not retranslate entire chapters by default.

Return a concise report with the unit ID, checks performed, unresolved issues, and exact proposed replacement text for any affected chunk IDs. Do not write to `edits/` or call `review-done`; the root applies accepted corrections and records unit completion after your turn finishes.
