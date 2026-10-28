# Debate builder work log

One entry per committed chunk of CTO_NOTES_04 §7 (DB0-DB7). Rulings 19, 21 and 22 apply.

- **DB0 (Oct 29):** ADR-009, mode registry (`src/mockboard/modes/`), `upsc_interview` profile wrapping today's rules, fallback and cap unchanged; `mode` on the session, `SessionState` and `StrategyContext`; `--mode` CLI flag. UPSC `make cli-fake` transcript byte-identical to HEAD c818ead (timings aside), now locked by `tests/golden/upsc_cli_fake.txt`.
- **DB1 (Oct 29):** 13 curated motions with hand-written, figure-free case files for both sides (`data/motions/curated.json`, validated by `modes/debate/motions.py`; only motions 5 and 6 cite cards). Custom-motion screen (`modes/debate/screen.py`: rules, then `motion_screen.v1.md` classifier, fail-closed) and custom case file (`case_file.v1.md`, generic fallback), both behind `FEATURE_CUSTOM_MOTIONS` (default off). `make eval-motions`: 42/42 correct, 0 of 16 partisan motions passed on the rules + fake-classifier path; 20 decisions came from the fake classifier and need a real-Nova run. The UPSC trace gains only `custom_motions: false` in `feature_flags`.
