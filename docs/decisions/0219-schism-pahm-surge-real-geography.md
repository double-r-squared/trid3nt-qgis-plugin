# ADR 0219 - SCHISM PaHM surge on REAL Galveston geography (fixes the 0217 synthetic-shelf showcase)

Date: 2026-08-11

Status: accepted

Cross-links: ADR 0217 (the standalone Holland-1980 sflux surge landing; this ADR
fixes its bathymetry-fallback flag and re-seeds the showcase). Supersedes the 0217
showcase case `01KZRWZK2XRF1ADH68NX6SA602` / run `01KZRWHNM33Q4NP99BD1XBKP22`
(peak 1.15 m piled against the synthetic shelf's west box edge).

## Context: the 0217 showcase ran on a SYNTHETIC shelf

ADR 0217 flagged it: `schism_pahm_surge` fetches its coastal bathymetry via
`_fetch_bathymetry_cog` -> `fetch_topobathy`, and on any fetch failure falls back
to a SYNTHETIC sloping shelf (`_synthetic_shelf_depths`). The seeded Ike showcase
hit that fallback, so the surge piled against the domain's open (model-shoreline)
edge -- NOT real Galveston geography. NATE flagged the surge field for
investigation and asked for a LARGER domain to see the whole footprint.

## Diagnosis: why the bathymetry fell back (three compounding faults)

1. **A ~12000 px composite for a screening domain.** `fetch_topobathy` composites
   at the FINEST source resolution (CUDEM 1/9" ~ 3 m). The 0217 seed AOI
   (-95.05,29.0,-94.55,29.45, ~0.5 deg) demanded a **17395 x 17790 px** (1.2 GB
   float32) grid, capped to a 12000 px warp of several CUDEM `/vsicurl` tiles plus a
   3DEP-land leg fetched at `resolution_m=10` (~5500 px). That heavy warp + the
   intermittent 3DEP timeout is the fallback trigger the 0217 flag named.

2. **A latent CRS bug hidden by the fallback.** `sample_bathymetry_on_nodes`
   sampled the topobathy COG with raw lon/lat, but the COG is **EPSG:32616 (UTM)**,
   not 4326. Every node landed off-grid -> all-NaN -> every depth clamped to the
   0.5 m wet floor = a flat domain. The synthetic-shelf fallback had always masked
   this for surge (the sampler was never reached), so it surfaced only once the
   fetch was made to succeed.

3. **The 3DEP land leg clobbers the offshore bathy.** In the topobathy merge the
   3DEP land DEM is HIGHER precedence than the ETOPO base and fills the nearshore
   ocean with a 0 m sea-level value -- overwriting the real negative bathy and
   re-flattening the domain to ~0 m depth.

## Decision: a coarse ETOPO-shelf screening acquisition + a real-geography default

- **Resolution cap (the 0217 flag's fix).** A new `_SURGE_SCREENING_RES_M = 200`
  is threaded through `_fetch_bathymetry_cog(screening_res_m=, force_bathy_base=)`
  into `fetch_topobathy` as `resolution_m` + `min_pixel_m`. A 30-60 km surge
  screening domain (an internal TIN of ~440 nodes) does not need 1/9" CUDEM; a
  few-hundred-metre grid is right. The enlarged AOI now fetches a **601 x 747 px
  (~0.4 MB)** COG instead of an 8.9 GB native grid.

- **Two new `fetch_topobathy` params (both default false -- no behaviour change for
  existing callers):**
  - `skip_cudem` -- drop the fine CUDEM 1/9" composite and its dozens of per-tile
    network reads over a large domain (wasted at coarse node density AND the
    dominant time/failure cost); forces the ETOPO global shelf base on.
  - `skip_land` -- drop the 3DEP land leg whose 0 m ocean fill clobbers the ETOPO
    negative bathy. ETOPO 2022 is already a COMPLETE topo-bathy (land positive, sea
    negative); a surge mesh clamps its land nodes to min-wet anyway, so ETOPO-only
    gives real negative offshore bathy with no ocean clobber. Verified: the enlarged
    TIN samples a deep Gulf shelf (-34 m offshore, southern row -24..-34 m) grading
    to a shallow bay/land north (260/440 nodes wet, mean depth 10 m).

- **CRS fix.** `sample_bathymetry_on_nodes` now reprojects the query lon/lat into
  the raster CRS before `ds.sample` (a UTM COG sampled with lon/lat lands every node
  off-grid). This also un-breaks the tidal coastal path's node bathymetry.

- **LARGER default AOI.** `_IKE_BBOX` -> greater Galveston
  `(-95.4, 28.6, -94.2, 29.95)`: the bay, Bolivar Peninsula, Galveston Island, and
  the open Gulf shelf seaward (south = the open boundary). Generous on purpose --
  the barotropic screening solve is fast at coarse resolution.

- **Loud synthetic warning.** When the fetch STILL fails and the synthetic shelf
  fires, the envelope note is now prefixed with a `WARNING -- SYNTHETIC BATHYMETRY`
  banner ("the peak piles against the domain edge, NOT real coastal geography --
  treat the surge PATTERN as non-physical"). The showcase no longer rides it.

The mesh route stayed the internal TIN on REAL (capped) bathymetry -- the ADR 0217
mesh gate (`_surge_mesh_gate`) still runs first and consumes a case mesh when one is
loaded, but the fresh showcase case has none, so the strong-path fallback (a
georeferenced internal TIN on real ETOPO bathymetry) carried it. No synthetic shelf.

## Consequence: the Ike showcase now shows REAL right-of-track surge

Re-run (case `01KZS6M3TX4QZSVKN5QC6E6EGA`, run `01KZS6NG40P717B2FGSSKP4P1N`,
`sim_days=1.5` = approach through landfall +12 h):

- **Peak surge 3.18 m at (-94.543, 29.586)** -- on/near **Bolivar Peninsula + the
  upper bay**, the RIGHT-OF-TRACK lobe (the physics expectation, stated up front and
  confirmed). The top-10 peak nodes all sit on the northern right-of-track shore
  (lat 29.53-29.60); NE-quadrant mean peak 1.28 m vs SW offshore/island 0.46 m.
- **Gauge** (station.in at the mesh centroid ~(-94.80, 29.28), the Galveston Bay
  entrance / Bolivar Roads near Pier 21): setdown to -1.45 m as the vortex
  approaches (offshore-directed winds on the left flank), then set-up to +1.21 m at
  landfall (~24 h) -- the classic surge setdown/setup signature.

Honest gap vs observed Ike: real Ike ran ~3-4 m at the open coast and ~4.5 m in the
upper bay (Chambers County). The screening SYMMETRIC Holland vortex (no GAHM
forward-motion asymmetry, no tide co-forcing, Coriolis-off, a rectangular
non-shoreline-clipped TIN) lands at 3.18 m -- in the observed coastal band and
capturing the right-of-track pattern, expected to undershoot the extreme upper-bay
value. The documented refinement path (native GAHM binary + tide co-forcing + a
shoreline-clipped gate mesh) is unchanged from ADR 0217.

Proof: `docs/proof/templates/schism_pahm_surge.png` (+ `_chart.png`) re-rendered from
the new run (ESRI basemap, EPSG:3857 both, track + landfall + gauge marker). Board:
the PaHM rows' LANDED notes updated to the 0219 real-geography numbers.

Superseded by nothing.
