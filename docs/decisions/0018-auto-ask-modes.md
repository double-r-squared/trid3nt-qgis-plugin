# 0018 - auto and ask modes (accepted, picker to implement)

Context: retrieval/routing ties are a real error species - the model picks a
plausible-but-wrong tool (e.g. a damage tool for "summary statistics of the
building layer") and the turn goes down a sad path the user could have
prevented in one click.
Decision: two modes governing ROUTING VISIBILITY ONLY.
- AUTO: every existing gate still fires (payload warnings, granularity,
  solver confirm, code-exec approval, credential entry, region choice,
  spatial input - the consent surface is NEVER mode-dependent). Tool
  selection is autonomous; no pick cards.
- ASK: auto plus tool selection surfaced as a multiple-choice card
  (retrieval-ranked candidates + a free-text option), staged in waves along
  the natural analysis flow (acquisition -> preprocessing -> analysis ->
  visualization) so the user is never flooded.
Refinement: auto may still ask on a MEASURED ambiguity signal (top-1 vs
top-2 retrieval scores in a near-tie) - error-killing without inundation.
Consequence: gates answer "may I do this"; modes answer "which tool" - the
two layers never mix. The card pattern (gate/code-exec/credential) and the
top-k candidate machinery are already built; the picker is assembly.
