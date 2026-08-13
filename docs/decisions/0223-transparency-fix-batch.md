# ADR 0223 - Provenance-transparency fix batch (audit 0222 remediation)

Status: accepted
Date: 2026-08-11

Cross-links: ADR 0222 (the read-only audit + ranked findings this remediates),
ADR 0219 (the R1/R2/R3 rulings), ADR 0224 (the parallel SCHISM-surge + topobathy +
payload-estimator wave; disjoint file set).

## Context

ADR 0222 audited the ~244-tool surface for the surge-class failure (a template that
fetches real data but SILENTLY falls back to fabricated data). It found ZERO
critical lies but a ranked set of consistency + completeness gaps. NATE ruled:

- coarsened REAL data = a legitimate labeled degrade (keep, label);
- MADE-UP data replacing real data = NEVER silent (fail loudly / stall / opt-in);
- the PSHA areal source = a RECORDED EXEMPTION (standard methodology);
- the scenario_gmf demo fault = GATED opt-in.

This ADR records the batch that implements those rulings.

## Decision (per finding)

1. **scenario_gmf synthetic demo fault -> opt-in (R1).**
   `resolve_scenario_rupture` no longer silently fabricates a fault. With no real
   GEM fault and no caller `rupture_trace`, it raises `SCENARIO_NO_REAL_FAULT`
   naming what was searched (`fetch_fault_sources` / GEM Global Active Faults) and
   pointing at the new default-false knob `use_demo_fault`. `use_demo_fault=True`
   runs the labeled synthetic path with a `WARNING -- SYNTHETIC DEMO FAULT` banner
   in the rupture note (envelope + `synthetic_inputs`). A caller `rupture_trace`
   is honoured regardless (user geometry, not fabricated). New tool knob covered by
   `test_gemini_schema_compliance`.

2. **psha synthetic area source -> RECORDED EXEMPTION.** Kept the auto
   areal-source fallback + label (NOT opt-in-gated). Rationale, added to the
   `resolve_fault_sources` + tool docstrings and here: a Gutenberg-Richter AREA
   SOURCE is standard PSHA distributed-seismicity methodology for unmapped-fault
   regions (e.g. USGS NSHM uses gridded/area sources alongside fault sources) -- a
   legitimate source model, not fabricated site data. The transparency obligation
   is met by the (strengthened) `source_model_note` label, which now names the
   area-source methodology explicitly ("not fabricated data, but not
   fault-specific"), not by stopping the run. R1's opt-in requirement is therefore
   exempted for this template only.

3. **SFINCS active-mask wide-fallback -> envelope.** `_compute_active_mask_bounds`
   returns `adaptive=False` when the DEM elevation range is unreadable (wide
   fallback window). `build_sfincs_model` now captures that flag (stops discarding
   it) and threads it into `ModelSetup.parameters["mask_bounds"]` via the new pure
   helper `_mask_bounds_provenance`; the flood composer lifts a non-adaptive mask
   into a labeled warning. Was previously only an `.inp` comment + agent.log.

4. **MODFLOW archetype family -> structured provenance.** New shared helper
   `modflow/_input_review.py` (`aquifer_k_review_entry`,
   `gate_and_stamp_modflow_inputs`, `review_modflow_entries`) promotes the prose
   `demo_aquifer_caveat` / `aquifer_k_source` to structured `SyntheticInput`
   entries routed through `gate_input_review` and stamped onto the layer envelope.
   Wired into capture_zone (exemplar, with a per-tool `input_mode` knob + a
   regional-gradient entry), contaminant_plume (per-tool `input_mode`, multi-layer
   stamp), saltwater_intrusion, managed_recharge, mine_dewatering,
   regional_water_budget (previously had NO structured aquifer provenance),
   wetland_hydroperiod, and sustainable_yield (all three return shapes: drawdown /
   stream-depletion / subsidence). asr already carried the full pattern (internal
   reference). The prose caveats are KEPT on the summary (additive; existing tests
   assert them). Scope note: per-tool `input_mode` is exposed on capture_zone /
   contaminant_plume / asr; the remaining single-purpose archetypes route through
   the gate at mode=None, which honours the session-wide `TRID3NT_INPUT_GATE_MODE`
   lever -- a deliberate simplicity choice, not a gap.

5. **river_seepage requested-DEM failure -> labeled.** When
   `fetch_dem_for_streambed=True` and the DEM fetch fails, the composer now records
   a specific `streambed_elevation` `SyntheticInput` (basis default_demo) naming the
   requested-DEM failure + a `streambed_dem_fallback` summary key, instead of a
   log-only degrade. The always-on aquifer-K entry is stamped alongside.

6. **hecras_flood_2d resolution clamp -> visible.** New pure helper
   `_resolution_with_basis` reports the supported-range `[20, 200]` m clamp (basis
   `default_demo` + a naming note) and the AOI autoscale (basis `derived`) on the
   `resolution_m` review entry; an in-range request stays basis `user` with no note.
   Threaded into both inner composers (inflow + rain-on-grid; the RoG path gained a
   `resolution_m` entry it lacked). NOTE: the DEFAULT-native resolution redesign is
   ADR 0224's scope; this only makes the EXISTING clamp visible.

7. **mesh_acquisition cross-dataset note -> unconditional.** The 3DEP->Copernicus
   (bare-earth -> canopy-inclusive DSM) mesh-bed fallback note is no longer gated on
   `notes is not None`: with a sink it is recorded; WITHOUT one it raises
   `MESH_BED_DEM_CROSS_DATASET_FALLBACK` rather than silently ingesting the DSM, so
   the label cannot be bypassed by the call shape.

8. **pelicun synthetic-asset provenance -> template surface.** The template tool
   (`pelicun_damage_assessment`, sync) now stamps a structured `asset_inventory`
   `SyntheticInput` on the returned layer: AUTO-FETCH mode (bbox, no assets_uri) is
   basis `derived` naming the synthetic building-density inventory with HAZUS
   class-default replacement values (what postprocess flags via `n_default_rv`); an
   explicit inventory is basis `user`. Folds the postprocess provenance up to the
   composer surface.

9. **river_dye domain-extent clamp -> labeled.** New pure helper
   `_clamp_domain_extent` labels the reach_length_km / channel_width_m /
   sim_duration_s guardrails when they bind; a `domain_extent_clamped`
   `SyntheticInput` is appended to the run provenance. Defensible guardrails, now
   visible (R2).

10. **schism postprocess render cap -> documented, NO code change.** The
    `_MAX_PX_PER_SIDE = 2500` output-raster cap in `postprocess_schism` is an
    OUTPUT/payload guardrail (COG display resolution), not a simulation-granularity
    cap. Per the audit it is acceptable as-is; recorded here as a deliberate keep.
    If R2 is ever read strictly for outputs it can be exposed as an optional
    override, but no user-facing simulation fidelity is lost today.

## Consequence

- Every remaining degrade named in the 0222 findings is now envelope-labeled,
  opt-in-gated, or a recorded exemption. No silent fabrication path survives in the
  audited set.
- New shared seams (`modflow/_input_review.py`, `_mask_bounds_provenance`,
  `_resolution_with_basis`, `_clamp_domain_extent`) reduce duplication and are the
  reference for future templates.
- Offline tests accompany each fix (helper-level where a live solve is required):
  scenario_gmf gate, psha strengthened note reaching the layer, SFINCS mask
  provenance, MODFLOW input-review helper + river_seepage requested-DEM label,
  hecras clamp basis, pelicun asset-inventory stamp, mesh cross-dataset raise,
  river_dye clamp.
- Board/velocity ledger left to the orchestrator.

Supersedes nothing.
