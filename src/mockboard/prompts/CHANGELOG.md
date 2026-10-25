# Prompt changelog

One line per prompt version: what changed and what the eval showed. Old versions are never
edited, only superseded by a new file `<id>.v<N+1>.md`.

| Prompt | Version | Date | Change | Eval result |
|---|---|---|---|---|
| shared | 1 | 2026-10-25 | Initial stable prefix (ground rules, data-not-instructions, no accent judgement). | Not yet evaluated (golden set pending). |
| listener | 1 | 2026-10-25 | Initial claim extraction with lakh/crore normalisation. | Not yet evaluated. |
| librarian | 1 | 2026-10-25 | Initial per-claim verdict against cards in the cached prompt. | Not yet evaluated. |
| actor | 1 | 2026-10-25 | Initial nine moves plus opener, 30-word cap, ask-never-assert. | Not yet evaluated. |
| judge | 1 | 2026-10-25 | Rubric = UPSC's seven published qualities (1-5 or insufficient_evidence) + Mock Board practice indicators (factual_accuracy, composure); `report_language` en/hi (ruling 6). | Not yet evaluated. |
| opener | 1 | 2026-10-25 | Application-form-driven open-ended opener; no place facts unless card-backed (ruling 5). | Not yet evaluated. |
| listener | 2 | 2026-10-25 | Adds `concedes_error` (candidate admits their own earlier figure or statement was wrong) for the acknowledgement preface (ruling 11, CTO_NOTES_02 M2). Tool schema requires the field. | Not yet evaluated (golden set pending); fake-LLM board-behaviour invariants pass. |
| actor | 2 | 2026-10-25 | Fact challenge gets structured, pre-rendered fields (`claim_figure`, `card_figure`, `card_year`, `source_short`, `language_mix`) instead of the card text; required three-sentence shape (candidate's figure, card's figure + year + short source, question about their source); both figures in the card's own scale; sentence case; no repeat of the previous line; `preface_already_spoken` for the code-spoken acknowledgement (ruling 11, CTO_NOTES_02 M3). Guard now requires the card's figure and rejects card text. | Not yet evaluated (golden set pending); fake-LLM tests pass. |
