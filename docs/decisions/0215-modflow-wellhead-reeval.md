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
