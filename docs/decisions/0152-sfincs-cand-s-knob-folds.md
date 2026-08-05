# ADR 0152 - SFINCS CAND-S rows: knob folds, not new templates

Date: 2026-08-05
Status: accepted

## Context

Thirteen SFINCS CAND-S board rows were queued across core numerics, subgrid,
quadtree, SnapWave, infiltration, structures, and precipitation. All are S-tier
hypotheses under the triage-first law. Triage against the installed engine
(SFINCS v2.3.3 solver image `deltares/sfincs-cpu:sfincs-v2.3.3`; deck build via
hydromt_sfincs 1.2.2) established, per row, what the signed SFINCS surface
already supports before any build.

Two facts shaped every disposition:

1. `setup_config` is a fully-open HydroMT passthrough: `SfincsInput.from_dict`
   does a raw `setattr` for every key, so ANY scalar reaches `sfincs.inp`
   verbatim -- even keys hydromt 1.2.2 does not model (e.g. `nuvisc`). What
   gates a fold is whether the SFINCS v2.3.3 BINARY honors the key, not whether
   hydromt knows it.
2. The ACTIVE offline/local path (`run_sfincs_direct` / `model_flood_scenario`)
   builds the deck IN-AGENT via `server/.../sfincs_builder.py` and runs the
   stock `deltares/sfincs-cpu` container as a PURE solver. The vendored
   `services/workers/_sfincs_build/deck.py` (its own `_emit_physics_config`
   twin) is only executed by the ON-HOLD s2z cloud worker. So a deck-build fold
   takes effect on the active path with NO custom-image rebuild; the vendored
   twin is mirrored for parity.

The existing `sfincs_advanced_numerical_physics_knobs` template + the
`physics_registry['sfincs']` table + the `sfincs_flood` composer are the natural
homes for these knobs. The strong prior (the SWAN/OpenQuake pattern) is: fold
single-namelist knobs onto the signed surface; do not mint near-clone templates.

## Decision

Zero new tools, zero new templates. Registry 215 and EXPECTED_TEMPLATES 57
UNCHANGED. Per-row disposition:

FOLD (code):
- Row 4 `horizontal_viscosity_smoothing`: added `viscosity` (0/1) + `nuvisc`
  (m2/s) to `physics_registry['sfincs']` and to both `_emit_physics_config`
  copies; surfaced on the numerical-physics template `_KNOB_KEYS` + params.
- Row 2 `manning_roughness_zonation_mode`: surfaced the already-registered,
  already-emitted `manning_land`/`manning_sea` constants on the template
  `_KNOB_KEYS` + params (per-cell NLCD reclass stays default; land/sea =
  unequal; uniform = equal).
- Row 10 `spatially_uniform_constant_infiltration`: added
  `infiltration_constant_mm_per_hr` to `model_flood_scenario` (+ the
  `assess_flood_impact` wrapper) building `InfiltrationForcing(constant_mm_per_hr=)`,
  mutually exclusive with the gridded GCN250 path (typed
  `INFILTRATION_METHOD_CONFLICT`). Both deck builders already emit `qinf`.

FOLDED-ALREADY (documented, zero code):
- Row 1 `advection_scheme_toggle` -> `advection` (0/1) knob. SFINCS v2.3.3 has no
  separate 'original'-scheme keyword; the 0/1 toggle IS the exposed selector.
- Row 3 `momentum_smoothing_theta_tuning` -> `theta` knob (already first-class).
- Row 7 `grid_type_regular_vs_quadtree` -> `sfincs_flood(quadtree=False|True)`.
- Row 12 `uniform_precip_timeseries_forcing` -> `setup_precip_forcing` uniform
  magnitude is the default pluvial forcing.

STOP (recipe recorded on the board row):
- Row 5 `subgrid_stability_limiter_tuning`: cited knobs (uvmax/hmin_cfl/uvlim/
  slopelim/wiggle) absent from hydromt 1.2.2 setup_subgrid AND from SfincsInput;
  max_gradient/z_minimum exist but are a different concept -- folding them under
  this name would mislabel.
- Row 6 `subgrid_qtable_weighting_method`: NOT blocked (q_table_option +
  weight_option are 1.2.2 setup_subgrid kwargs) but DEFERRED under the lean bar
  -- low-value backward-compat knob, default is already the improved method,
  needs a vendored-cloud-deck mirror + subgrid smoke. Recipe recorded.
- Row 8 `quadtree_mesh_netcdf_qgis_output`: emitting outputformat=1 is trivial
  but the ROW's product value (QGIS-NATIVE mesh layer) needs a new
  publish-side mesh-layer deliverable (postprocess already rasterizes the UGRID
  faces to a COG). HIGH product value for the QGIS-only doctrine; queued as a
  mesh-deliverable job, not an S-tier knob.
- Row 9 `incident_wave_setup_toggle`: SnapWave wave forcing does not ship; the
  toggle would be inert (Invariant 7). Gated behind the SnapWave-deck family.
- Row 11 `weir_discharge_coefficient_tuning`: SFINCS structures (weirs/thin
  dams) do not ship; par1/cd cannot exist before the weir line-element. Gated
  behind the structures family.
- Row 13 `gridded_precip_interpolation_mode`: gridded precip (amprfile) does not
  ship; ampr_block is inert without it. Gated behind the gridded-precip row.

## Consequences

- The numerical-physics template gains four levers (viscosity, nuvisc,
  manning_land, manning_sea); `sfincs_flood`/`assess_flood_impact` gain
  `infiltration_constant_mm_per_hr`. A run with none set is byte-identical.
- WORKER-IMAGE LAW: the active local path does NOT execute the vendored
  `deck.py` (it builds in-agent + runs the stock `deltares/sfincs-cpu` solver),
  so NO custom-image rebuild was needed to land or verify these folds. The
  vendored `_sfincs_build/deck.py` twin was mirrored (viscosity/nuvisc emit) for
  parity; the `trid3nt-local/sfincs:latest` cloud-image rebuild + through-image
  smoke is DEFERRED with the ON-HOLD s2z cloud path.
- Six STOP rows carry precise recipes; four unblock only after a prerequisite
  family lands (SnapWave, structures, gridded precip) -- honest gaps, not silent
  no-ops.

## Evidence

- Offline slice (from repo root, `env -u TRID3NT_CACHE_BUCKET pytest`):
  test_categories + test_template_hygiene + test_catalog_surfacing +
  test_door_dissolution + test_sfincs_numerical_physics +
  test_sfincs_builder_surge_forcing + test_sfincs_archetype_decks +
  test_sfincs_forcing_adapter + test_set_sfincs_parameters = 108 passed.
- FLOOD CANARY (regular-grid pluvial, Chattanooga TN bbox, stock
  `deltares/sfincs-cpu:sfincs-v2.3.3`): status=ok, depth COG published, new
  MinIO run prefix + outputs. run_id 01KZ9TC84D0JDJYHANSEPCSTBK.
- KNOBS SMOKE (same bbox, folds set): SFINCS v2.3.3 solve status=ok + depth COG;
  deck `sfincs.inp` carries `viscosity = 1`, `nuvisc = 0.02`, `qinf = 5.0`
  verbatim; `manning_land`/`manning_sea` flow to `setup_manning_roughness`
  (manifest in the generated `sfincs.man` grid, `manningfile = sfincs.man`).
  run_id 01KZ9TEJ249E0562532RTBXWN2. The knobs deck got a distinct setup_id from
  the canary (advanced_physics is part of the deck cache key -- no stale reuse).
- Proof map: docs/proof/templates/sfincs_advanced_numerical_physics_knobs.png
  (peak flood depth over Esri World Imagery, white AOI box; 6372 wet cells, max
  2.70 m). SFINCS is a regular grid -- no separate mesh-wireframe deliverable.
