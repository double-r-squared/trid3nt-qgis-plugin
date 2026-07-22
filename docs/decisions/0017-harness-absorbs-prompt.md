# 0017 - the harness absorbs the prompt (PROPOSAL)

Context: the always-on system prompt is ~8.4k tokens, mostly discipline text
begging the model not to fail in known ways. Prompt adherence degrades in long
contexts; code does not.
Proposal: six mechanisms, each deleting its prompt block on landing, each
gated by the routing bench: (1) layer handles at dispatch (0014); (2) canvas
AOI as a structured field with dispatch auto-fill of missing bboxes; (3)
dispatch guards - idempotent refetch dedupe, result reuse by args-hash,
geocode drift validation, fuzzy enum correction; (4) turn-loop invariants -
no silent turn end after tool results (one structural nudge), continuation
backstop; (5) retrieval-side alias table + engine-routing lore moved to tool
docstrings, ambiguous engine choice via ask-mode decision card; (6) adapter
strips thinking tags. Target: ~600-800 always-on tokens.
