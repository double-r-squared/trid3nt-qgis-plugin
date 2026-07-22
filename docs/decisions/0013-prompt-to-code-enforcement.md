# 0013 - rules graduate from prompt to code

Context: the static system prompt reached ~8.4k tokens, much of it
mechanical discipline (publish handles, never-refetch, pre-sim checks).
Decision: three tracks, each gated by the routing bench: (1) code-enforce
mechanical rules at the dispatch/emit seams (they cannot be forgotten at
turn 40); (2) move routing lore into tool docstrings + query corpus so
retrieval pays its cost only when relevant; (3) compress what remains.
Target: ~1.5k always-on tokens - vital for small local models.
