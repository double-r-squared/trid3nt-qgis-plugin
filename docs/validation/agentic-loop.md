# The agentic quality loop (THINKING - not approved)

NATE's parallel: agentic coding workflows (research -> plan -> execute ->
adversarial review -> edit loop) applied to simulation quality.

MAPPING
- research: what does modeling THIS scenario require (site, engine, forcing,
  canonical setups) - already a house rule for engine work
- plan: the model conception as a reviewable artifact BEFORE solving: AOI,
  resolution, boundary conditions, parameter sources, duration, and the
  observations that will validate it. Surfaced as a plan card (ask mode
  reviews; auto passes through; the granularity gate is a fragment of this)
- execute: deck build + solve (exists)
- adversarial review: three layers (below)
- edit loop: revise and re-review, BOUNDED (max revisions, cost surfaced via
  the granularity gate), honest non-convergence report if it never gates

ONE AGENT VS SUBAGENTS - the variable is CONTEXT ISOLATION, not processes.
Empirical basis (this project's own build/review logs): same-model
adversarial panels with fresh context out-caught the builder lanes 4/4
(fresh-clone break, path-walk misses, an overstated fix claim, embedding
regressions hidden by an aggregate win). Self-review inside the builder's
context inherits the assumptions that made the mistake.

THE THREE-LAYER REVIEW
1. Deterministic core - the diagnostics parsers, no LLM (mass balance,
   duration sufficiency, stability, parameter-vs-data cross-checks). Catches
   the majority at zero token cost.
2. Fresh-context review call - ONE isolated model call (new conversation,
   refute charter, fed the plan + diagnostics envelope + metrics). The
   "subagent" benefit without subagent infrastructure in the daemon.
3. The human review card - the tier-3 checklist; never auto-passed.

VERDICT ON FOLDING: the loop folds into the one turn-loop agent; the REVIEW
must not fold into the same context. No agent framework needed in the
daemon - one deterministic toolchain, one isolated call, one card.

OPEN (feeds open-questions.md): plan-card contents and when it gates; review
call cost/model tier; revision budget defaults; how the loop interacts with
calibration (is calibration just the edit loop with an optimizer driving?).
