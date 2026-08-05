# 0148: GeoClaw knob activation - stale-image rebuild + AMR window governs refinement

Date: 2026-08-05
Status: landed

## Context

The ADR 0144 revision surfaced two deck-level defects on the two GeoClaw SWE+AMR
knob templates (`geoclaw_regional_manning_friction`,
`geoclaw_amr_refinement_regions`), both proven live on Crescent City decks:

- **A - banded Manning inert.** `geo_data.manning_coefficient=[0.015,0.06]` +
  `manning_break=[0.0]` produced BYTE-IDENTICAL output to scalar `0.025`, while
  scalar friction was verified sensitive (control `n=0.001` vs `0.5` shifts peak
  depth 0.59 -> 0.22 m).
- **B - AMR window subsumed.** The AOI default region already forced the finest
  level everywhere in the AOI, so an in-AOI window at that level had no marginal
  effect (inside == outside).

## Root cause

**Defect A was NOT a deck-writer bug.** Introspecting clawpack 5.14.0 inside the
`trid3nt-local/geoclaw` image confirmed the whole chain is correct:
`GeoClawData.write` authors `num_manning` + the coefficient list + the break list
(and raises unless `len(break) == len(coeffs)-1`); `geoclaw_module.f90` allocates
`manning_break(num_manning)`, sets the TOP band to `+inf`, reads the `num_manning-1`
breaks below it; `src2.f90` selects the per-cell coefficient by topography `B`
(`do nman=num_manning,1,-1; if (B < manning_break(nman)) coeff=...`). The
setrun_builder already emitted exactly that form. The defect was a **STALE DEPLOYED
IMAGE**: the baked `geoclaw:latest` predated ADR 0143, so its `parse_build_spec`
had NO `manning_coefficients` / `amr_regions` fields and SILENTLY DROPPED them from
the build_spec, authoring only the scalar `manning_n`. Banded and scalar decks were
therefore identical. (The ADR 0143 smoke used a thin `geoclaw:knobs-test` overlay
that no longer existed; the ADR 0144 re-smoke fell back to the stale `latest`.)

**Defect B had two layers:** the same stale image dropped `amr_regions` entirely;
and even once threaded, `plan_geoclaw_grid` OVERRODE `amr_levels` to the
cost-bounded whole-AOI finest (L4 here), so a user window specified at that level
was subsumed by the AOI default region.

## Decision

- **Rebuild the image with the landed worker** (the standing fix for A, and layer
  one of B). The worker code is BAKED into the image (the build_spec is the only
  server<->worker interface), so a landed setrun_builder change is inert until the
  image is rebuilt. This is now the deploy contract for any GeoClaw deck change.

- **AMR windows GOVERN refinement (Defect B).** When explicit `amr_regions` are
  present:
  - the composer sets the deck's finest to FOLLOW the finest window (honoring the
    user's level), bounded to +1 over `plan_geoclaw_grid`'s cost-bounded whole-AOI
    ceiling - a window is a bounded sub-box, so only its cells reach that finest
    level (budget-safe);
  - the setrun_builder pins the AOI default region ONE LEVEL BELOW the finest
    (`aoi_level = amr_levels-1`) so an in-AOI window is demonstrably finer than its
    surroundings, raises `amr_levels_max` to cover any window beyond `amr_levels`,
    and points fgmax's `min_level_check` + sample spacing at the AOI ambient so the
    whole AOI depth field is still captured;
  - with NO windows both collapse to `amr_levels` - every non-window deck is
    numerically unchanged (byte-identical solve; only emitted deck comments differ).

- **AMR windows ride the ADR 0107 input-review gate.** The windows are the
  consequential, model-invented input (they place WHERE the mesh refines), so the
  template now presents each resolved window through `gate_input_review` with
  `basis=prompt_interpreted` (model-derived box) or `user` (explicit/drawn
  geometry). Auto mode labels them in the assumptions block; user_gated holds the
  run for review before the solve; a headless direct-call fails open. The
  plugin draw-a-rectangle supply path (which would set `basis=user`) is the
  follow-on.

## Consequence

- **Banded Manning now activates.** Live re-smoke, SAME Crescent City deck,
  amr_levels=2: banded [0.015 offshore, 0.06 onshore] vs scalar 0.025 gives gauge
  max|eta diff| 0.0587 m (t=764 s), peak-field max|diff| 0.0521 m over 133 common
  wet cells, and banded floods LESS (0.0739 vs 0.0766 km2 - higher onshore n damps
  overland reach, the correct direction). The scalar path stays byte-identical.

- **AMR window now demonstrable.** Live re-smoke, amr_levels=4 + an L4 window:
  inside the window ALL cells reach L4 (3600 cells), outside the window (+nesting
  buffer) capped at L3 with ZERO L4 leakage. Solve healthy (real ocean column,
  max_depth ~1.0 m). Budget-safe: the AOI-wide finest is L3 (below the plan's L4
  ceiling), only the small window box carries L4.

- **Tests.** `test_setrun_builder.py` gains the banded-Manning fortran-consumer
  contract test + the window-lowers-ambient / window-exceeds-finest / no-window
  planning tests; `test_geoclaw_amr_regions_gate.py` pins the window surfacing in
  the pending-inputs payload with the right basis, the cancel path, and the
  auto-mode stamp. Offline geoclaw slice green.

- **Proofs refreshed** (`docs/proof/templates/`): `regional_manning.png` (banded
  peak-depth over Esri), `regional_manning_chart.png` (banded vs scalar waveforms,
  max|diff| in the caption strip), `amr_regions.png` (Chile-2010-style mid-run
  sea-surface anomaly + black AMR patch outlines per level), `amr_regions_depth.png`
  (peak-depth product), `amr_regions_chart.png` (inside-vs-outside level contrast).
  All follow the NATE norms: no annotation box over the plot area, diagnostics in
  the caption strip, white box = AOI, yellow dashed = user window, red dot = gauge.

- **Standing deploy note:** a GeoClaw setrun_builder / entrypoint change is not
  live until `docker build -f services/workers/geoclaw/Dockerfile -t
  trid3nt-local/geoclaw:latest .` is re-run (layer cache makes it fast). The stale
  `latest` that masked both defects is the cautionary case.
