# The activation boundary (THINKING - not approved)

NATE: the validation/verification loop applies to SIMULATION-class work
(things with moving parts), NOT every ask. Running it on a fetch or a
colormap tweak is wasteful.

THE DIVIDING LINE: irreducible correctness burden. A tool triggers the loop
only if it MANUFACTURES A CLAIM ABOUT THE WORLD that can be confidently,
invisibly wrong. Data that is its own ground truth does not.

  fetchers                         NO  - data is the truth; honesty floor suffices
  deterministic processing         NO  - mechanical transform (inputs + known formula)
  spatial_query                    NO  - SQL over visible data; user reviews in real time
  simulations (SFINCS/SWMM/...)    YES - synthesize a physical claim from assembled parts
  calibration                      YES - it IS the loop (edit -> run -> review)

MECHANISM: an activation flag as TOOL METADATA (like supports_global_query /
the solver-confirm markers), not a global turn wrapper. The harness engages
the validation machinery only for flagged results; everything else flows
through at full speed. Extensible for free - any future tool that manufactures
a fallible claim just sets the flag.

TIERS SCALE TO STAKES (not binary):
- Tier-1 diagnostics (mass balance / continuity / duration) are cheap enough
  to run on EVERY flagged solve - it is parsing the log the engine already
  wrote.
- The expensive parts (fresh-context review call, the re-run edit loop) gate
  on "does this run matter enough" (design decision: default? user mode?
  stakes heuristic?) - an OPEN QUESTION for open-questions.md.

So: always the deterministic floor for sims; conditionally the heavier loop.
