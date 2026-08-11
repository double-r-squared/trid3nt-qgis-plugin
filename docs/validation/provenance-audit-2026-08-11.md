# Provenance-transparency audit (ADR 0222) - 2026-08-11

Auditor pass over the tool surface (~244 tools: `server/src/trid3nt_server/agent/workflows/`
+ `tools/` + `tools/fetchers/_router/hooks/` + specs) for the failure classes the
SCHISM surge arc (ADR 0217/0219) exposed. READ-ONLY analysis; no fixes applied.

Audited against the ADR 0219 rulings:
- **R1**: fabricated/synthetic input is NEVER a fallback tier; ladder = retry ->
  coarser REAL source (loudly labeled) -> typed error; synthetic ONLY as an
  explicitly-requested declared mode.
- **R2**: resolution/granularity is a USER lever (autoscaled suggestion + override,
  never a hardcoded cap); oversized requests trip the payload gate.
- **R3**: same-data mirrors may silently fail over; CROSS-DATASET substitution must
  be loud; every degrade labeled in the envelope.

## Headline

The surge-class defect (a SILENT synthetic fallback that misrepresents real
geography) is **not reproduced anywhere else in the sampled surface**. The fetcher
hook layer is clean; the template layer overwhelmingly labels its degrades. There
are **zero CRITICAL findings** - no docstring claims measured/real while a synthetic
path can fire (grep for over-claiming docstrings returned empty). The residual
findings are consistency and completeness gaps, not active lies:

- Two seismic templates (`scenario_gmf`, `psha`) auto-fall-back to a synthetic
  source. Both LABEL it in the envelope, but neither is opt-in-gated the way the
  post-0219 surge now requires - the R1-consistency question NATE should rule on.
- One genuine opaque-ish degrade: SFINCS `setup_mask_active` widens the active-mask
  elevation window on a DEM-range read failure and surfaces it only in an `.inp`
  comment + log, not the user envelope.
- Provenance is present but UNSTRUCTURED (prose caveats vs the structured
  `SyntheticInput` basis=user/derived/default review entries) across the MODFLOW
  archetype family and pelicun.
- A few silent numeric clamps on granularity/domain-extent (guardrails, not caps).

## Summary count by class

| Class | Compliant / exemplary | Findings (gaps) | Critical |
|---|---|---|---|
| 1. Synthetic ingestion | surge (opt-in+banner), swmm depth, secondary_perils CTI, all V&V/mechanism templates | 2 (scenario_gmf, psha: labeled but not opt-in-gated) | 0 |
| 2. Opaque fallbacks | ~60 fetcher hooks (typed router errors), mesh cross-dataset note, topobathy provenance | 3 (sfincs mask, river_seepage requested-DEM, mesh notes=None branch) | 0 |
| 3. Hardcoded granularity | surge + hecras res (user lever + autoscale) | 3 (flood_2d clamp, schism render cap, river_dye clamp - all LOW guardrails) | 0 |
| 4. Provenance completeness | 46 templates emit structured review entries | 2 (MODFLOW archetypes, pelicun: prose not structured) | 0 |

## Ranked findings table

| # | Finding | Class | Sev | File:line | What the user sees today | Fix shape |
|---|---|---|---|---|---|---|
| 1 | SFINCS active-mask window silently widens to `_MASK_FALLBACK_ZMIN/ZMAX` when the DEM elevation range can't be read; the `adaptive=False` flag is emitted only as an `.inp` comment and is discarded at the autoscale call site | 2 | MED | `sfincs/sfincs_builder.py:1170-1205`, comment at `:2274-2278`, flag dropped `:2582` | A flood run with a wider-than-real active mask (may activate cells a real DEM range would exclude), with NO envelope/narration signal - only agent.log + an `.inp` comment | Thread the `adaptive` flag into the run envelope as a labeled degrade (a `fallback_warning` / review entry: "DEM range unreadable, mask bounds are a wide fallback"); stop discarding it at `:2582` |
| 2 | `scenario_gmf` auto-falls-back to a SYNTHETIC demo fault (trace through AOI centre) when no real GEM fault intersects / the fetch fails | 1 | MED | `openquake/scenario_gmf/scenario_gmf.py:383-436` (fallback), labeled `:246-257,293` | Labeled: `synthetic_inputs` entry basis=`default_demo` + note "used a synthetic demo fault". But no opt-in flag and no WARNING banner - the surge now requires both | NATE-judgment: is a generic scenario rupture "fabricated input" under R1, or legitimate methodology? If R1 applies, gate behind an explicit `allow_synthetic_rupture` + WARNING banner (mirror `allow_synthetic_domain`); else record the exemption in the ledger |
| 3 | `psha` auto-falls-back to a SYNTHETIC area source when no mapped fault intersects the AOI | 1 | MED | `openquake/psha/psha.py:561-645` (fallback), surfaced via `source_model_note` `:1580` | Labeled: layer carries `source_model_kind`/`source_model_note` "used the synthetic area source". No opt-in flag / banner | Same NATE-judgment as #2. An area source is a standard PSHA source model, so this is the strongest candidate for a recorded R1 EXEMPTION rather than a fix |
| 4 | MODFLOW archetype family carries provenance as PROSE caveats (`demo_aquifer_caveat`, `aquifer_k_source`) rather than structured `SyntheticInput` basis=user/derived/default review entries | 4 | MED | `modflow/capture_zone/capture_zone.py:711-720`; also river_seepage, contaminant_plume, saltwater_intrusion, managed_recharge, mine_dewatering, regional_water_budget, wetland_hydroperiod, sustainable_yield | A truthful prose caveat, but not machine-readable provenance and not run through `gate_input_review` (no user override surface for the demo defaults) | Add structured `SyntheticInput` review entries (K basis, water-table basis, streambed basis) through `gate_input_review`, matching the surge/hecras/swmm templates; the caveat strings become the entry `note`s |
| 5 | `river_seepage` requested-DEM degrade is log-only when `fetch_dem_for_streambed=True` and the fetch fails; the standing `demo_aquifer_caveat` is generic and does not name the requested-DEM failure | 2 | LOW-MED | `modflow/river_seepage/river_seepage.py:277-278` (log), `:338-340` (generic caveat) | User explicitly asked to sample real streambed from a DEM; on fetch failure gets demo streambed with only a generic always-on caveat, no "your requested DEM was unavailable" signal | On the requested path, promote the degrade to a specific labeled review entry / `fallback_warning`, not just `logger.warning` |
| 6 | `hecras_flood_2d` hard-clamps `resolution_m` to `[20, 200]` m silently; a finer request is clamped up with no envelope note, and no `res_basis` provenance is recorded (unlike the surge) | 3 | LOW | `hecras/flood_2d/flood_2d.py:280-282`, `_DEFAULT_RES_M` `:78` | A user asking for 5 m silently gets 20 m; the run does not report whether resolution was user vs default | Per R2, surface the clamp as a labeled note (or trip the payload gate on oversize) and record a `resolution_m` review entry with basis, mirroring `pahm_surge` |
| 7 | `mesh_acquisition` 3DEP->Copernicus CROSS-DATASET fallback is loud ONLY when `notes is not None`; if a caller passes `notes=None` the canopy-bias substitution is log-only | 2 | LOW | `telemac/rain_on_grid/mesh_acquisition.py:645-657` | Primary caller passes `notes`, so today it is loud (R3-exemplary). A future/other caller with `notes=None` would silently ingest a canopy-inclusive DSM as bed elevation | Make the loud note unconditional (raise/attach regardless of the `notes` sink), so the labeling can't be bypassed by the call shape |
| 8 | pelicun archetype composers lean on `postprocess_pelicun` for synthetic-asset provenance; the composer layer itself emits lighter provenance | 4 | LOW-MED | `tools/simulation/pelicun/postprocess_pelicun/postprocess_pelicun.py:561-597` (the good seam); archetype composers thinner | Provenance exists at postprocess but the MS_BUILDINGS/synthetic-asset default basis is not consistently a structured review entry at the template surface | Fold the postprocess `synthetic_inputs` up into the template's `gate_input_review`, matching class-4 fix #4 |
| 9 | `river_dye` preview builder silently clamps `reach_length_km` `[0.5,8.0]`, `channel_width_m` `[10,1500]`, `sim_duration_s` `[600,14400]` | 3 | LOW | `telemac/river_dye/river_dye.py:2831-2841` | A large reach is silently trimmed (documented gmsh-hang guardrail) with no envelope note | Guardrail is defensible; add a one-line labeled note when a clamp binds, for R2 transparency |
| 10 | `postprocess_schism` output raster capped at `_MAX_PX_PER_SIDE=2500` per side | 3 | LOW | `schism/postprocess_schism.py:68,185-186` | Output COG resolution is capped regardless of AOI; a display/payload decision, not a simulation-granularity one | Acceptable as a render/payload guardrail; if R2 is read strictly for outputs, expose as an optional override. Lowest priority |

## Cross-checked (end-to-end read, not grep-only) - the 3 highest ranks

- **#1 SFINCS mask fallback**: read `_compute_active_mask_bounds` (`:1132-1241`) - it
  returns `(zmin, zmax, adaptive)`; both failure legs (`:1170-1176`, `:1196-1205`)
  return `adaptive=False` with only a `logger.warning`. The consumer at `:2274-2278`
  writes the flag into the generated `.inp` as a trailing comment
  (`# wide fallback (DEM range unreadable)`); the second consumer at `:2582` binds it
  to `_adaptive` and never reads it. Confirmed: the degrade never reaches the run
  envelope/narration the user sees. VERDICT: CONFIRMED opaque-ish degrade.
- **#2 scenario_gmf**: read `resolve_scenario_rupture` (`:341-436`) - synthetic branch
  at `:420-436` sets `kind="synthetic"` + a note; the composer (`:246-257`) builds a
  `SyntheticInput(basis="default_demo", note=rupture.note)` and attaches it via
  `layer.model_copy(update={"synthetic_inputs": ...})` at `:293`. Confirmed: LABELED
  in the envelope, but auto-fires with no opt-in and no WARNING banner. VERDICT:
  CONFIRMED consistency gap (not a silent-fabrication violation).
- **#3 psha**: read `resolve_fault_sources` (`:561-645`) - the empty/failed-fetch legs
  return `([], note)` with a loud note; the caller threads `source_model_note` into
  the layer constructor at `:1580`. Confirmed: LABELED in the layer envelope, auto-
  fires with no opt-in. VERDICT: CONFIRMED consistency gap; strongest exemption
  candidate (area source = standard PSHA methodology, not fabricated data).

## What is already exemplary (the reference patterns, for contrast)

- `schism/pahm_surge/pahm_surge.py:626-660` - synthetic ONLY behind
  `allow_synthetic_domain=True` (declared mode), a `WARNING -- SYNTHETIC BATHYMETRY`
  banner in the note, a `domain_provenance` review entry, and `resolution_m` as a
  user lever with `res_basis` provenance. This is the post-0219 gold standard.
- Fetcher hook layer (`tools/fetchers/_router/hooks/*.py`) - upstream failures raise
  typed `router_upstream_error` / `router_empty_error` / `router_input_error`;
  parse-level `except: continue/return None` only DROP unparseable records (never
  fabricate). Clean across ~60 hooks. `topobathy.py` records `record_provenance` +
  `fallback_warning` for its CUDEM->ETOPO leg degrade.
- `swmm/network_import/network_import.py:408-430` - `_resolve_storm_depth` returns a
  `depth_basis` threaded into a `SyntheticInput` (basis + `real_source_if_any` +
  note) through `gate_input_review`; demo sub-areas labeled `default_demo`.
- `telemac/rain_on_grid/mesh_acquisition.py:645-657` - LOUD cross-dataset note naming
  the canopy bias (R3-exemplary; see finding #7 for its one conditional edge).

## TOP-10 ranked fix list (for NATE's go)

1. **[MED, class 2]** SFINCS `setup_mask_active`: surface the `adaptive=False`
   wide-fallback mask degrade in the run envelope (labeled), stop discarding the flag
   at `sfincs_builder.py:2582`.
2. **[MED, class 1 - NATE-judgment]** `scenario_gmf` synthetic demo fault: rule
   whether R1 requires an opt-in flag + WARNING banner (mirror `allow_synthetic_domain`)
   or a recorded exemption.
3. **[MED, class 1 - NATE-judgment]** `psha` synthetic area source: same ruling;
   strongest candidate for a recorded R1 EXEMPTION (standard PSHA source model).
4. **[MED, class 4]** MODFLOW archetype family: promote prose `demo_aquifer_caveat` /
   `aquifer_k_source` to structured `SyntheticInput` review entries through
   `gate_input_review`.
5. **[LOW-MED, class 2]** `river_seepage`: on the requested-DEM path, promote the
   log-only degrade to a specific labeled review entry naming the DEM failure.
6. **[LOW, class 3]** `hecras_flood_2d`: label the `[20,200]` resolution clamp when it
   binds and record a `resolution_m` basis review entry (surge pattern).
7. **[LOW, class 2]** `mesh_acquisition`: make the 3DEP->Copernicus cross-dataset note
   unconditional (not gated on `notes is not None`).
8. **[LOW-MED, class 4]** pelicun: fold `postprocess_pelicun.synthetic_inputs` up into
   the template's input-review surface.
9. **[LOW, class 3]** `river_dye` preview: emit a labeled note when a domain-extent
   clamp binds.
10. **[LOW, class 3]** `postprocess_schism` `_MAX_PX_PER_SIDE`: optionally expose the
    output-raster cap as an override; acceptable as a payload guardrail (lowest
    priority).

## Method notes

- Grep-driven hunt across the four classes (synthetic/fabricat/idealized/fallback/
  demo/default patterns), then read the except-body of every high-count hook to
  distinguish `raise typed error` (honest) from `swallow -> default` (violation-shaped).
- Fetcher layer triaged by (except-count vs label-count) then body inspection:
  concluded CLEAN (typed router errors on upstream failure; parse-drops never
  fabricate).
- Template layer triaged by default-assignment-after-fetch patterns; each candidate
  read for whether the degrade reaches the ENVELOPE (review entry / note /
  fallback_warning) vs LOG-ONLY.
- CRITICAL class checked by grepping docstrings for unconditional real/measured/observed
  truth-claims against templates with synthetic paths: none found.
- Top-3 verified end-to-end (call site -> composer -> envelope), no grep-only claims
  in the top ranks.
- Coverage caveat: ~103 leaf templates + ~60 hooks; the sweep is systematic by pattern
  but not an exhaustive line-by-line read of every template. Findings are the pattern
  hits that survived body inspection; absence of a finding for a template is "no
  pattern hit", not a proof of compliance.
