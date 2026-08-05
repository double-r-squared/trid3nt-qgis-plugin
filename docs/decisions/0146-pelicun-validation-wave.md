# 0146: Pelicun validation wave - 4 templates folding 7 rows + 2 test-only + 2 STOPs

Date: 2026-08-05
Status: landed

## Context

Fourth per-engine grind batch: the eleven Pelicun CAND-S rows. Triage
overturned the board's pessimism in the opposite direction from prior
waves: the existing pelicun_damage_assessment template hand-rolls a
HAZUS-flood Monte-Carlo and never touches pelicun's real
assessment.Assessment API, while the installed pelicun 3.9.0 bundles
everything the rows need (validation suites, HAZUS-EQ v5.1+v6.1 data,
FloodRulesets, estimate_RID).

## Decision

Land four chart-led templates on a shared spine (_validation_common.py,
asyncio.to_thread for all pelicun compute, loud typed
PelicunValidationError):
- pelicun_closed_form_validation (folds rows 1+7 via a check knob):
  MC damage-state sampling vs analytic lognormal (max delta 0.00079 at
  n=200k, tol 0.01) + loss-function identity (exact).
- pelicun_mixed_fragility_loss_assessment (folds 2+8): fragility +
  direct loss-function components in one assessment; correlation knob
  (perfect-vs-independent spread ratio 1.026, perfect wider as theory
  demands).
- pelicun_replacement_threshold_override_sweep (folds 9+10): RID
  inference from PID (estimate_RID) + irreparable-override threshold
  sweep (frac_replaced monotone 0.486 -> 0.352).
- pelicun_flood_foundation_depth_damage_sweep (row 5): HAZUS v6.1 RES1
  depth-damage by foundation type (4 distinct curves; basement damage
  begins below grade).

Rows 11 and 6 land as regression tests, not templates: save/reload
byte-identity, and the discovery that pelicun ships ONE flood-building
alias so standalone flood and the coupled surge branch resolve to the
identical dataset by construction. Rows 3+4 STOP with recipes
(DL_calculation needs chdir + tempdir handling; no v5.1 buildings alias
exists - compare by direct csv path).

## Consequence

Registry 204 -> 208; templates 50. 97 offline gates green, no
regression in the existing pelicun surface. Follow-on recorded: port
pelicun_damage_assessment itself onto the real Assessment API.
