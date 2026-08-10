# ADR 0215 - MODFLOW wellhead reeval: soil-derived K + kriged water table (data seams)

Status: Accepted
Date: 2026-08-10

## Context

The module-coverage board's PRT row carried NATE's 2026-08-06 reeval flag on the
wellhead / capture-zone track: "nothing wrong, CONTINUE DEVELOPING" with four
named candidate directions - transient / multi-well pumping, heterogeneous K from
real data, a kriged potentiometric surface over the plane fit, and an NHD river
boundary condition.

Where the track already was (studied before this ADR):

- `modflow_wellhead_protection` is a thin archetype wrapper over
  `model_capture_zone_scenario` (the `capture_zone` composer); the only difference
  is EPA WHPA framing + default tiers [2, 5, 10] yr vs [1, 5, 10].
- The composer already had a THREE-rung regional-gradient ladder: measured USGS
  well heads (a least-squares potentiometric PLANE) -> DEM topographic proxy ->
  demo west-east placeholder, each a loud downgrade.
- The worker deck (`gwt_adapter._build_prt_capture_zone_deck`) is SINGLE-well,
  STEADY (nper=1), a CHD perimeter ring oriented to the gradient vector, uniform
  initial head, and a DEMO aquifer K (`DEFAULT_AQUIFER_K_MS = 1e-4 m/s`) unless
  the caller overrides it.

So two of the four reeval directions were partly present as a gradient PLANE and
an override-only K; the honest increment is to give both a REAL data basis with
loud provenance, and to design the two directions that need a worker/contract lift.

Local-mode fact that shapes scope: the server imports the worker adapter directly
(`run_modflow_local` + `gwt_adapter`), so worker deck changes are offline-testable
against the local mf6 binary - the image-staleness law bites only the DEPLOYED
container path. The transient / multi-well / NHD lifts are therefore buildable
locally but still need a contract change and a live mf6 solve to land honestly;
they are DESIGNED here, not shipped.

## Decision

### Landed (2 shared data-provenance seams + composer wiring; live at _PLATTE)

Both seams live in `server/src/trid3nt_server/agent/workflows/shared/` - the
cross-engine home - so the Landlab groundwater templates
(`groundwater_water_table` / `groundwater_storm_recession`) can import the SAME
texture->K step without a second implementation. The Landlab templates are NOT
modified this wave; only the seam is made importable.

1. **Soil-derived hydraulic conductivity** (`soil_hydraulics.py`, reeval item 2).
   `ksat_from_texture(sand, clay, om)` implements the published Saxton & Rawls
   (2006) pedotransfer functions (SSSAJ 70:1569-1578: the moisture regressions
   Eq. 1-5 and the Ksat closure Eq. 16, `1930 * (theta_s - theta_33) ** (3 -
   lambda)`), returning a `PedotransferK` carrying `k_m_s`, the named `basis`, the
   texture inputs, an estimated effective porosity, a plausibility-clamp flag, and
   a standing `limitation` string. It is a DERIVED NEAR-SURFACE proxy - labeled
   loudly, never presented as measured aquifer K (a confined aquifer's K can
   differ by orders of magnitude). The composer derives K from SoilGrids texture
   at the well when the caller supplies none (`use_soil_k=True` default); a soil
   fetch/sample failure is a loud fallback to the demo default. Provenance flows
   into `summary["aquifer_k_source"]` + a source-aware `demo_aquifer_caveat`.

2. **Kriged / trend water-table interpolation** (`water_table_interp.py`, reeval
   item 3). `interpolate_water_table(wells)` applies an explicit, evidence-based
   ladder over the usable measured wells: REGRESSION KRIGING (a trend plane plus
   an ordinary-kriged residual field, exponential variogram) when `n >= 8` with
   sufficient 2-D spread and a fittable variogram; a TREND PLANE for `3 <= n < 8`
   or a degenerate variogram; INSUFFICIENT (`None`, loud caller fallback) for
   `n < 3` or a collinear/clustered set. The rule is stated in code and narrated.
   The regional GRADIENT the CHD boundary is oriented to remains the plane fit
   (kriging and the plane agree on the trend); kriging adds the SURFACE curvature
   a single gradient vector cannot carry, exposed via `surface.sample(e, n)` for
   the worker starting-head follow-on. Provenance (method + variogram + well
   count + residual) flows into `summary["water_table_interp_method"]` +
   `derived["water_table_interpolation"]`.

Citations: Cressie (1993) "Statistics for Spatial Data" (ordinary kriging);
Hengl, Heuvelink & Rossiter (2007), Computers & Geosciences 33(10):1301-1315
(regression kriging); Journel & Huijbregts (1978) (exponential variogram).

### Live evidence (real data at the _PLATTE High Plains site, 40.905, -98.42)

- Soil K: real SoilGrids texture sand 37.1% / clay 21.4% (a loam) at 5-15 cm ->
  K = 2.48e-6 m/s (8.92 mm/hr), porosity 0.198, unclamped. ~40x below the demo
  1e-4 m/s default - a material, honest correction.
- Water table: 570 raw USGS readings in the well-search box -> 22 usable wells
  (453 stale-excluded; basis mix NAVD88 x10, NGVD29-shifted x7, DEM-minus-depth
  x5). 22 >= 8 with good spread -> REGRESSION KRIGING selected: |grad| 1.3e-3 m/m,
  flow azimuth 73 deg (ENE), trend residual RMS 1.09 m over a 23.95 m head relief,
  variogram range ~21 km. The real well density confirms _PLATTE as a kriging site.

### NOT landed this wave - designed for the next worker+contract+image wave

3. **Transient + multi-well capture evolution** (reeval item 1). WHPA practice
   (US EPA 440/6-87-010; USGS ex-prt-mp7-p03 for transient PRT) wants a WELLFIELD
   (several wells, individual rates) and TIME-evolving 1/5/10-yr capture zones.
   Concrete seam: `MODFLOWRunArgs` grows a `wells: list[WellSpec]` (each lat/lon +
   rate) alongside the singular `well_location_latlon` (kept for back-compat);
   `_build_prt_capture_zone_deck` emits one WEL record per snapped well cell and,
   for transient, prepends a steady spin-up + `_add_transient_sto_tdis` schedule,
   then reverses each period's budget for the PRT FMI (the existing
   `build_and_run_prt_from_gwf` extends to a per-period reversed budget set). The
   travel-time tiers already ARE the isochrones; transient makes them evolve. This
   is offline-testable against local mf6 but needs a contract field + a live solve
   + the deployed-image rebuild.

4. **NHD boundary conditions** (reeval item 4). Where the AOI warrants it, replace
   the generic CHD perimeter with RIV/CHD cells derived from NHD flowlines
   (`fetch_river_geometry`) and waterbodies (`fetch_nhd_waterbodies`): rasterize
   the reaches onto the grid, assign RIV stage/conductance from the kriged
   water-table surface at each reach cell (seam 2's `sample`), keep the CHD ring
   only where no NHD feature bounds the domain. Concrete seam: a `river_reaches`
   payload on `MODFLOWRunArgs` + a `ModflowGwfriv` block in the deck. Needs the
   same contract+worker+image+live path as item 3.

## Consequences

- Every capture-zone / wellhead run now defaults to a SoilGrids-derived K and a
  kriged/trend measured water table where the data supports them, each labeled as
  a derived/measured basis per the input-review norm - a real fidelity gain over
  the demo default + plane, with no fabrication.
- The two shared seams are pure, offline, and reusable; the Landlab groundwater
  templates gain a texture->K step for free when they choose to import it.
- The full worker realization of items 1 and 4 (and the per-cell kriged IC of
  item 2/3) is a bounded, designed follow-on gated on a contract change + a live
  mf6 solve + a deployed-image rebuild - deliberately not rushed into this data
  wave.

Supersedes nothing. The measured-heads gradient path (ADR 0166) is preserved and
now routes its surface through the shared seam.

## Part 2 outcome (landed) - the designed worker+contract+image wave

The three "NOT landed" designs above are now SHIPPED and live-proven with real
mf6 6.7.0 (both on the local-exec path and THROUGH the rebuilt worker image).

**Contract** (`MODFLOWRunArgs`, additive): a `WellSpec` (lon/lat/rate/name) list
`wells` (the WELLFIELD), a `capture_zone_transient` flag, an NHD `river_reaches`
payload (list of lon/lat polylines), and a `starting_head_by_cell` kriged IC
grid. `CaptureZoneLayerURI` grows `well_capture_allocation`, `transient`, and
`river_cell_count`. Every field defaults to the single-well steady demo (part 1
byte-identical). Parser version `modflow-jobspec-2`; a misnamed variant of any new
field raises a loud TypeError at the deck call (rejection tests added).

**Worker** (`_build_prt_capture_zone_deck` + `build_and_run_prt_from_gwf`):
- ONE MF6 WEL record per snapped well (extraction sign applied); a particle ring
  released around EACH well with a `W{k}_P{n}` boundname so the postprocess
  allocates which well captures which particles.
- Transient = a steady spin-up period 0 (wellfield OFF) + N transient GwfSto
  storage periods (wellfield ON) via the shared `_add_transient_sto_tdis`; the PRT
  TDIS mirrors the REVERSED per-period schedule and the FMI reads the per-period
  reversed budget (the isochrones evolve with the drawdown; USGS ex-prt-mp7-p03).
- NHD `river_reaches` rasterized onto the grid as RIV cells (stage sampled from
  the kriged IC at each reach cell, a documented streambed conductance default);
  the perimeter CHD ring is retained where no reach bounds the domain.
- The uniform IC replaced by the per-cell kriged surface (`starting_head_by_cell`)
  re-referenced about the deck datum so the interior carries the measured
  water-table curvature (item 3). Shared `prt_grid_geometry` lets the composer
  sample the surface at each cell centre before the deck exists.

**Image law**: the worker image was rebuilt with absolute -f/context paths and
BUMPED to mf6 6.7.0 (6.5.0 rejected the PRT deck: "UNKNOWN PRP OPTION:
EXTEND_TRACKING" -- the capture-zone PRT was already 6.7.0-only), matching the
production local-exec binary. SHA-256 pinned from the release asset digest. Docker
history carries zero GRACE-2 refs. Behavior-proving smoke THROUGH the baked image
built a transient 3-well + 57-RIV-cell deck, solved it, ran PRT, and asserted the
1/5/10-yr zones grow (1.03/2.15/2.94 km2) with a nonempty 24/24/24 per-well
allocation.

**Live evidence** (real fetchers + real mf6):
- _PLATTE (40.905/-98.42), TRANSIENT 3-well WHPA: soil K 2.477e-6 m/s (SoilGrids),
  regression-kriging IC from 22 usable USGS wells (570 raw; basis NAVD88 x10 /
  NGVD29-shifted x7 / DEM-minus-depth x5), measured gradient |grad| 1.30e-3 m/m
  azimuth 73.5 deg, trend residual RMS 1.09 m -- all matching part 1. Isochrones
  1.06/2.22/3.00 km2; per-well allocation 3.12/2.11/0.63 km2. `river_cell_count=0`
  is HONEST: the nearest NHD reach is 4.78 km away, outside the 4.1 km domain, so
  the CHD ring alone bounds it (the "where no NHD feature bounds the domain" rule).
- Grand Island reach AOI (40.857/-98.412), the RIV-active companion: ALL FOUR
  seams active -- soil K + regression-kriging IC (26 wells) + transient + NHD RIV
  58 cells; isochrones 0.65/1.40/1.95 km2, per-well allocation 2.39/3.24/1.06 km2,
  head-residual RMS 1.06 m over 26 wells (mean ~0, range -2.71..+3.70 m). This is
  the fresh showcase case (`title_suffix="multi-well transient NHD RIV"`).

Proofs: `docs/proof/templates/modflow_wellhead_protection_multiwell_transient_nhd_riv.png`
(WHPA map: wells, 1/5/10-yr zones, NHD reaches, up-gradient pathline fan) +
`..._chart.png` (computed-vs-measured heads + residual histogram). Offline
matplotlib renders; NATE does the live QGIS/ESRI verification (working agreement).

## Part 2 follow-up (landed) - the ZERO-layers emission bug

Re-seeding the wellhead showcases surfaced a LATENT honesty-floor hole (present
since the archetype composers were written, NOT introduced by part 2): a
`modflow_wellhead_protection` / `modflow_capture_zone` run completed end-to-end
(FGB in the run prefix, `scenario complete` logged) but the Case received ZERO
layers, with no error raised.

**Root cause** (`sustainable_yield.py::_run_archetype`, the seam every MODFLOW
archetype flows through). The archetype run tool returns a typed `LayerURI`, and
the server dispatch's `add_loaded_layer` gate fires ONLY on a bare-`LayerURI`
return; the thin workflow tools serialize the composer's `*Result` to a dict
(`result.model_dump(mode="json")`), which the dispatch does NOT auto-load. The
composer was expected to load the headline layer via
`_maybe_emit(pipeline_emitter, ...)` routing through `emit_tool_call`'s gate --
but the thin tools pass `pipeline_emitter=None`, so `_maybe_emit` ran the solver
directly and NOTHING loaded. Only the `role="context"` wells overlay attached
(it used `pipeline_emitter or current_emitter()`); the primary zone did not. The
whole archetype family (capture zone, wellhead, drawdown, dewatering, subsidence,
...) was affected - `plume` and `seepage` were unaffected because they load
explicitly / return a bare `LayerURI`. No error envelope fired because nothing
FAILED: the tool returned a valid `CaptureZoneResult` dict; the layer simply had
no path onto the map (the second honesty-floor hole, now closed).

**Fix.** `_run_archetype` loads its returned primary layer explicitly
(`current_emitter().add_loaded_layer(emit_layer_uri(result))`) after the typed
validation - the single seam for all archetypes, matching the `plume`/subsidence
pattern and the `run_modflow_archetype_job` docstring contract ("Return it so the
emitter's `add_loaded_layer` gate loads it onto the map"). Dedups by identity, so
a caller that also loads is a no-op.

**Ambient-AWS fix.** `capture_zone.py::_read_wells_features` read the `s3://`
wells FGB with `gpd.read_file(s3_uri)` -> GDAL `/vsis3/`, which ignores the MinIO
`AWS_ENDPOINT_URL` and failed with "AWS Access Key Id ... does not exist" (a
silent degrade to no gradient-wells overlay). It now fetches the bytes through
the boto3 object reader (`run_modflow._read_vector_bytes`) into a temp file and
reads with pyogrio - the no-ambient-AWS norm.

**Regression test.** `server/tests/test_archetype_layer_emission.py` (3 offline,
no mf6/S3): `_run_archetype` loads the typed layer onto `current_emitter()` even
with `pipeline_emitter=None`; an error-dict return raises + loads nothing; and a
composer's dict-shaped return is (correctly) NOT auto-loaded by `emit_tool_call`
(documents the root cause).

**Re-seed (physics-verified GREEN, both cases 2 layers each):**
- single-well `modflow_wellhead_protection` case=`01KZPZHVXFTFCF62NGCNJMAFP3`
  run=`01KZPZKYR5RVJ31PMPX81MVA29`; `!run modflow_wellhead_protection(aoi_latlon=[40.905, -98.42], well_location_latlon=[40.905, -98.42], travel_time_years=[5.0, 10.0, 25.0], n_particles=48)`;
  capture_zone FGB present, tiers [5,10,25], + gradient-wells overlay.
- multi-well transient NHD RIV case=`01KZPZP13CPA0RKV5GP9WQ2QZ5`
  run=`01KZPZPGRZK3TZCP2NSG6H0CP8`; `!run modflow_wellhead_protection(aoi_latlon=[40.857, -98.412], wells=[GI-1 1600, GI-2 1100, GI-3 800], transient=True, sim_years=10.0, n_periods=5, use_nhd_river_boundaries=True, travel_time_years=[1.0, 5.0, 10.0], n_particles=24)`;
  capture_zone FGB present, tiers [1,5,10], `well_capture_allocation` nonempty
  (GI-1 2.394 km2 / 24 particles / 1600 m3/d, GI-2, GI-3). No ambient-AWS warning.

Superseded junk cases (earlier no_result seed attempts on the pre-fix daemon):
`01KZPXHRN8MVCQ2C9GCX0JGJY1` (single-well) and `01KZPXK071BN70K1FQH9CBG1KV`
(multi-well) - both seeded ZERO layers; safe to delete.
