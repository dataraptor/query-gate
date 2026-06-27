"""QueryGate grounding eval (Tier-2, distributional) — spec §13, Split 09.

Two halves live here:

* :mod:`evals.scoring` — the **pure** scorers (grounded-rate, table-precision,
  answer-correctness, 0-destructive-calls, and the gold-set lint). No API key, no
  provider SDK — unit-testable and CI-safe.
* :mod:`evals.run_eval` — the agent-under-test tool-runner (×N), the distributional
  report, cost accounting, and the ``querygate eval`` entry point. Needs a model key.

The gold question set is ``evals/questions.jsonl`` (Appendix D).
"""
