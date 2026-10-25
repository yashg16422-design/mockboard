"""Offline evaluation harness for the text brain (PLAN_v2_DELTA §10, ENGINEERING.md §5).

`schema` defines one scripted item, `loader` reads JSONL sets with dev/held-out splits,
`metrics` computes counts (planted-error recall, false corrections with an exact
Clopper-Pearson upper bound, move-class accuracy, claim F1), and `runner` drives the real
turn-analysis graph with the FakeLLMClient by default.
"""
