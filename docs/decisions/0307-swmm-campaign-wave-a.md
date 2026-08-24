
## Post-review corrections (verify lens, 2026-08-25)

- LOC staleness: rdii_rtk/steps.py is 443 (the final efaa46da fix added
  16 lines after the last count) -> RDII total 753, everything-touched
  3,324, family total 9,144, delta vs baseline +1,421.
- The SWMM template count is 14, not 15 (the group table itself sums to
  14): "3 of 14" migrated.
- Row 23b softened: the variable-step chart fix removes a LATENT
  assumption - these decks pin REPORT_STEP=dt so the engine clock was
  uniform and nothing previously plotted was wrong. The residual
  same-class pattern in snowmelt_metrics (cross-run clock indexing,
  currently harmless at 167/167 steps) is recorded for the family
  waves, as is the missing offline coverage for swmm_times_hr /
  flow_routing_error_pct (queued with wave B).
- Wave-B scope corrected: mesh/swmm_deck_runner (625) is imported by
  the C family too and cannot retire at wave B - the group-boundary
  argument strengthens; the wave-B sentence was wrong.
- Board nit: the snow_removal knobs line updated with the row.
