# Branch review findings (NATE, refactor/engine-doors)

Accumulating during NATE's review; fixed as ONE identity-gated batch on his go.

1. (2026-07-28) SIMULATION TREE NOT NORMALIZED: engine-owned internals sit at
   agent/tools/simulation/ root instead of their engine dirs -
   run_river_seepage_tool (MODFLOW-family; NATE's catch), run_modflow_archetype_tool.py,
   run_modflow_multi_species_tool.py, run_geoclaw/landlab/openquake/swan/swmm/
   telemac_tool, model_fire_spread (elmfire), postprocess_pelicun +
   run_pelicun_damage_assessment (pelicun). PLUS duplicated setters: set_sfincs/
   swmm/telemac_parameters at BOTH root and engine dirs (one twin dead each).
   Fix: determine live-vs-dead per item (registry import chain), move engine-owned
   into engine dirs, delete dead twins, import sweep, identity gate.
   model_debris_flow stays root (general, no door - NATE decision).
   STATUS: FIXED (2026-07-28). 4 real moves (river_seepage, 2 modflow internals,
   postprocess_pelicun), 4 dead removals (3 root setter twins + run_swmm_tool),
   7 suspects were already-gutted untracked pycache shells (removed). Registry
   202 byte-identical (1 legit module-path change), suite exact 10-baseline,
   canary green.

2. (2026-07-28, self-found) REMAINING HYGIENE DEBT, deferred honestly by the
   mop-up: (a) 166 comment-line marker hits across 69 server/tests files
   (needs per-line judgment, separate pass); (b) ~45 in-code STRING literals
   (log/error messages) citing job-/OQ-/Wave- markers - these surface in
   envelopes/logs, worth a judgment pass; (c) test FILE NAMES carrying job
   numbers (test_case_binding_job0268.py etc.) - rename candidates.
