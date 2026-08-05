# 0144: GeoClaw depth-COG revision - finest-AMR-wins + overland mask + grid-plan arg fix

Date: 2026-08-05
Status: landed

## Context

NATE flagged the ADR 0143 knob-template proofs: the published GeoClaw depth
COG rendered ONE uniform-value rectangle (0.7566 / 0.7317 m filling rows
165-185 x cols 185-230 of a 256x266 grid near Crescent City harbor) instead of
real coastal inundation. Forensics on the two acceptance runs confirmed every
wet cell held a single constant.

Two independent pre-existing defects produced this:

1. **Coarse-AMR smear in the rasterizer.** `rasterize_frame_to_grid` painted a
   coarse patch cell across its full footprint, and only WET patch cells wrote -
   so a finer patch's DRY cell never erased the coarser patch's wet value.
   Attribution on real fort.q frames: all 966 wet cells that survived the ocean
   mask came from the coarse level-1 patch on cells where the finer level-2
   patch read DRY (0 came from the finest patch). The published "overland
   inundation" was a single coarse cell (an averaging artifact of an L1 cell
   straddling harbor + land) smeared flat, with the harbor sub-cells carved out
   by the ocean mask, leaving a rectangle.

2. **Swapped grid-plan arguments.** `model_geoclaw_inundation` called
   `plan_geoclaw_grid(bbox, domain_bbox, ...)` but the signature is
   `plan_geoclaw_grid(domain_bbox, aoi_bbox, ...)`. The finest-cell budget was
   therefore computed over the whole offshore-extended domain (treated as the
   AOI), blowing the budget at level 3 and capping `amr_levels` at 2. The AOI
   never refined past ~575 m cells, so even with defect 1 fixed the overland
   field was near-uniform (2 unique depths) and the AMR window could not reach
   its requested level.

## Decision

Fix both at the source so every GeoClaw template benefits:

- **Finest-AMR-wins, unconditionally.** `rasterize_frame_to_grid` now tracks a
  per-cell `painted_level`; a patch owns every covered cell whose recorded level
  is `<=` its own and writes its depth when wet, NaN when dry. A finer patch's
  dry cell ERASES a coarser wet value - the depth over any area is the finest
  patch's solution there, never a coarse cell smeared under a finer dry patch.
  Applied identically in the byte-faithful worker port
  (`services/workers/_geoclaw_postprocess/postprocess.py`).

- **Overland mask requires land.** The ocean mask's topo term is now
  `topo <= sea_level_m` (was `topo < 0`) so a cell at or below the still-water
  datum is water, not overland - overland is strictly `topo > sea_level_m`. This
  catches the nearshore sea on ETOPO coasts whose bathymetry reads ~0 m at the
  waterline. `postprocess_geoclaw` gains a `sea_level_m` parameter, threaded from
  `run_args.sea_level_m`. The initial-wet criterion is retained as the robust
  primary term.

- **Correct grid-plan arg order:** `plan_geoclaw_grid(domain_bbox, bbox, ...)`.
  For the Crescent City AOI this yields `amr_levels=4` with a LOWER finest-cell
  estimate (35 250 vs the swapped 94 122) - within budget - and an L1->L4 nest.

Proof deliverables are the as-in-QGIS renders only: maps over Esri World Imagery
+ dock-interpreter charts. NO relief code enters emissions, contracts, or the
composer - the depth-COG fix emits exactly what production emits. (Gradient
colored-relief spot-checks are on-demand debug artifacts, not landing
deliverables.)

## Consequence

- Published depth is now coast-following overland inundation on low coastal land
  (topo 0-9 m), not a uniform rectangle. Re-smoked live (local-docker) on both
  knob decks: peak field went from 2 unique depths to 80+ (min 0.05 / median
  0.21 / max 0.93 m, 141 wet cells hugging the harbor + SE shoreline). Runs stay
  cheap (~60 s). Regression test
  `server/tests/test_geoclaw_postprocess_amr_flatten.py` asserts finest-wins
  flattening, the land mask, and the non-uniform gate on a synthetic multi-level
  fixture.

- **ADR 0123 contamination verdict: YES.** The `geoclaw_tsunami_gauge_timeseries`
  peak-inundation layer is produced by the SAME `model_geoclaw_inundation` ->
  `postprocess_geoclaw` -> `rasterize_frame_to_grid` path, so it carried the
  identical smear. It is fixed by the same change (no separate edit needed).

- Proofs refreshed with corrected visual vocabulary: AOI boundary = white solid
  box, AMR refinement window = yellow dashed box (labeled). Charts differentiated
  per template (AMR level-nesting map vs gauge waveform).

- **Two OPEN follow-ups surfaced (out of this postprocess scope, flagged for
  NATE):**
  - The spatially-varying (list) Manning form is inert: banded
    `[0.015, 0.06]` + `manning_break=[0.0]` produces BYTE-IDENTICAL output to
    scalar `0.025`, while scalar friction is verified sensitive (control
    `n=0.001` vs `0.5` shifts peak depth 0.59 -> 0.22 m). The
    `manning_coefficient` list / `manning_break` is written to the deck but not
    activated in the GeoClaw run - a deck-level variable-friction wiring defect.
  - The explicit AMR window is subsumed by the AOI-default region (both reach L4
    at this AOI size), so the window has no marginal effect. Demonstrating a
    window requires the AOI default to be coarser than the window's level - a
    template-design change.
