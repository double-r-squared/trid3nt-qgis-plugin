# ADR 0166 - Capture-zone regional gradient from MEASURED heads

Date: 2026-08-06
Status: accepted

## Context

ADR 0165 oriented the MODFLOW PRT capture zone with a regional gradient derived
from a DEM planar slope -- the shallow water table taken as a subdued replica of
surface topography. That is a SCREENING proxy, honest but coarse: it ignores the
actual measured water table where wells exist. Nebraska (and most of the US via
our fetchers) has dense USGS groundwater coverage, and `fetch_usgs_groundwater_levels`
already returns observed well water levels. This is a FOLD onto the existing
capture-zone surface (no new tool): upgrade the gradient basis to MEASURED heads
when a usable well set exists, with an honest downgrade ladder when it does not.

## Decision

An honest source ladder for the regional gradient, best-basis first, each rung a
LOUD downgrade narrated in the summary `gradient_caveat` (data-source fallback
norm: same-data mirrors silent, cross-dataset substitution loud + user-visible):

1. **MEASURED heads (`gradient_source="measured_heads"`).** Fetch observed USGS
   well water levels over an expanded ~0.1 deg (~11 km) footprint about the well
   (`WELL_SEARCH_HALF_DEG`), reduce each reading to a head ELEVATION in NAVD88
   metres (the datum ladder below), fit a potentiometric plane over the usable
   wells (`_fit_measured_gradient` -> `_fit_plane`), and orient the CHD to that
   vector. The used wells are emitted as a point context layer (head elevation +
   date per well) via `publish_input_layer(role="context")` so the user SEES the
   observed data the gradient came from.

2. **DEM proxy (`gradient_source="dem"`).** The ADR 0165 DEM water-table proxy,
   reached only when measured heads are too few / degenerate. Narrates WHY it
   dropped from measured (the fallback reason).

3. **Demo west->east (`gradient_source="demo_west_east"`).** The last-resort typed
   placeholder (unchanged).

**The datum trap (handled + tested).** NWIS reports depth-to-water BELOW land
surface and/or datum'd groundwater ELEVATIONS. A head elevation requires the two
be co-referenced. Per-reading -> NAVD88 m:

- DEPTH-to-water (pcode 72019 / 61055): head = 3DEP DEM land surface (NAVD88,
  sampled at the well) minus the depth. A non-positive depth (flowing/artesian) is
  EXCLUDED.
- groundwater ELEVATION, NAVD88 (72150 / 62611): head = the value directly.
- groundwater ELEVATION, NGVD29 (62610): head = value + a nominal regional
  NGVD29->NAVD88 shift (`NGVD29_TO_NAVD88_M = -0.20` m). A UNIFORM offset does not
  bias a fitted slope; up to ~2 m of national datum spread only matters where
  NGVD29 and NAVD88 wells are MIXED, so the shift is stated as a screening
  approximation, not a rigorous point transform.
- any other / "Local Assumed" vertical datum on an elevation reading: EXCLUDED
  (not vertically georeferenced).

Readings are filtered to a recency window (`measured_recency_years`, default 10 y;
most-recent per site kept), a parseable value + timestamp, and a non-rejected
approval status. `_usable_well_heads` returns per-basis counts + exclusion reasons
for narration.

**Non-degenerate fit guard.** `_fit_measured_gradient` requires >= 3 wells, a
spatial spread with minor-axis std >= 150 m and extent >= 500 m (collinear /
clustered sets leave the cross-gradient component unconstrained), a finite
gradient at/above the aquifer floor, and a plane residual not exceeding the head
relief. Magnitude is clamped to the same aquifer ceiling as the DEM path
(direction preserved). Any failure -> loud drop to the DEM proxy.

Threading: the composer is authoritative on provenance. The adapter cannot tell a
measured vector from a DEM vector (both are just `(gx, gy)`), so it still labels a
supplied vector "dem"; the composer relabels the returned layer
`gradient_source="measured_heads"` (magnitude/azimuth are already correct,
recomputed by the adapter from the same clamped vector). No MODFLOW-contract or
adapter physics surface changes. Seam-1; in-process mf6 via `asyncio.to_thread`.

## Consequence

`modflow_capture_zone` / `modflow_wellhead_protection` now orient the capture zone
to the REAL measured water table where wells exist, with the observed wells drawn
on the map, and degrade honestly (DEM proxy -> demo) with the basis narrated so
the run's provenance is visible. It stays a SCREENING-tier delineation (100 m grid,
demo K/porosity, nominal datum normalization) -- not a calibrated WHPA.

Live smoke (Platte valley nr Grand Island NE, well 40.905/-98.42,
wellhead_protection [5/10/25] yr, 48 particles): 22 usable USGS wells
(2017-11-01..2026-06-10) across ALL THREE datum-ladder paths (elev_navd88=10,
elev_ngvd29_shifted=7, dem_minus_depth=5; 453 stale readings excluded).
Measured gradient 1.30e-3 m/m, flow azimuth 73.5 deg, plane residual 1.09 m over a
23.9 m head relief. Vs the ADR 0165 DEM proxy (1.35e-3 m/m, 64.7 deg): the
MAGNITUDE agrees within ~3% (validating the subdued-replica assumption for gradient
STRENGTH) while the measured heads rotate the flow azimuth ~9 deg (refining the
direction the pure-DEM proxy could not see). Zones 0.080 / 0.177 / 0.451 km^2
(5/10/25 yr) + 3.06 km^2 envelope, Grubb width 1420 m, stagnation 226 m. Proof:
`docs/proof/templates/modflow_capture_zone_georef{,_chart}.png` (measured basis,
22 wells overlaid).
