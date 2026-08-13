# ADR 0191 - spot-check fixes: TELEMAC mesh render, SWAN AOI bathymetry void, SCHISM baroclinic shoreline mesh + circulation

Date: 2026-08-08
Status: accepted

## Context

NATE spot-checked the ADR 0189/0190 shortlist proofs and flagged THREE defects.
Each was root-caused at the source FIRST, then fixed. ZERO new tools this wave
(all fixes); registry unchanged (235). No manifest spec-field changes -> no
strict-parser version bumps.

## Defect 1 - TELEMAC river mesh "looks messed up" (rainfall diffmap)

**Root cause (offset-centerline hypothesis REFUTED).** The TELEMAC-2D mesh is
NOT built from a laterally-offset centerline. It follows the REAL NHDPlus HR
flowline (true river planform + bends); the node cloud of the solved mesh traces
the Snake River near Twin Falls exactly. The chaotic triangular "fan" extending
outside the AOI was a PROOF-RENDERING artifact: `proof_telemac_rain.py`
triangulated the scattered mesh nodes with an UNCONSTRAINED
`matplotlib.tri.Triangulation(x, y)` (a Delaunay of the node cloud), which bridges
every river bend with long triangles across the convex hull and fills them, so the
`tricontourf` + `triplot` painted a fan over dry land. (The compare uses
`bank_source="constant_ribbon"` -- a symmetric constant-width channel around the
real flowline -- because this short 3 km demo reach has no NHDArea water-polygon
coverage; the `nhd_area` path raises `TELEMAC_BANKS_UNAVAILABLE` here. Bank
fidelity, variable banks + islands, rides `nhd_area` on reaches with coverage.)

**Fix.** `postprocess_telemac.read_selafin` now returns the REAL element
connectivity `ikle` (0-based, from the SELAFIN IKLE record it previously discarded);
`rainfall_forcing_compare.py` persists it in the arrays npz; the proof triangulates
on the real elements (`Triangulation(mx, my, triangles=ikle)`), so the fill +
wireframe follow the true channel and never leave the water body. No worker-image
rebuild (the change is the server-side postprocess reader + the reference driver).

**Live evidence.** Rain vs no-rain pair re-solved through the run_solver seam +
existing image: base `01KZH53WRW8KB4NYSAK7VDG4NT`, rain
`01KZH561BN64PFA5HWZ8EYEJPM` (70 s / 80 s, both complete). 1500 mm/day over 90 min
raises domain-mean wet depth +8.6 mm (max +21 mm); 1711 nodes, 2786 elements.
Regenerated in place: `telemac_river_dye_rainfall_diffmap.png` (now a clean
channel-following ribbon, dye-rise concentrated upstream) + `_rainfall_timing_chart.png`.

## Defect 2 - SWAN peak-Hs covers only a narrow right-edge band, not the AOI

**Root cause (CGRID/INPGRID-extent hypothesis REFUTED).** The deck's computational
grid CORRECTLY spans the full requested bbox (`_grid_geometry` anchors CGRID +
INPGRID at the SW corner with the full lon/lat spans). The narrow band was a
BATHYMETRY-DATA artifact: the driver AOI was the Big Bend box
`(-84.30, 29.70, -83.90, 30.05)`, whose staged CUDEM topo-bathymetry tile carries a
large interior block of elevation EXACTLY 0.0 -- a source tile void, ~54% of the AOI,
with rectilinear tile-edge boundaries (not natural bathymetry). The SWAN depth
sampler maps elevation 0.0 -> depth 0.0 < DEPMIN (0.05 m) -> DRY, so more than half
the interior meshed dry and SWAN only solved the connected wet strip on the east,
producing the east-edge raster.

**Fix.** Relocate the storm driver AOI to the clean, void-free Mexico Beach / Tyndall
FL shelf box `(-85.55, 29.70, -85.40, 29.85)` -- the box the showcase already targets
(`_APALACHEE`), with continuous CUDEM bathymetry (~86% wet, 0 flat-demo fallback,
coherent land only at the north coast). No SWAN worker-code change -> no image
rebuild. The wave field now fills the AOI (waves build from the south boundary,
shoal + decay northward to the real coastline).

**Live evidence.** 36 h storm re-solved through the native SWAN solver + existing
image: run `01KZH582SZ8M1M9AM7PYPVQTES`, max_hs 6.02 m (matches the 6 m forcing peak
at hour 18), wave_area 197 km2, 19 time-stamped frames (1.0 -> 4.3 -> 6.0 -> 2.7 ->
1.0 m). Regenerated in place: `swan_wave_field_nonstationary_storm_peak_hs.png` (fills
the box) + `_frames.png`. Showcase re-seeded (its `_APALACHEE` box already matches).

**Flagged for NATE (latent, not fixed this wave):** a coastal AOI whose CUDEM tile
has an exact-0.0 interior void still meshes half-dry silently (the existing all-dry /
no-coverage guards do not catch a partial void). A worker-side void guard / gap-fill
is a deferred follow-up; it was NOT added this wave to avoid a cross-cutting SWAN
behavior change + image rebuild for a data-quality issue.

## Defect 3 - SCHISM baroclinic "looks like a normal gradient" + paints land

**Root cause.** (a) The mesh was a regular lon/lat lattice over the RAW bbox with no
coastline clip, and the default Delaware Bay box `(-75.55, 38.85, -75.05, 39.45)` is
mostly LAND (the narrow upper bay / tidal river), so the salinity raster painted a
gradient over land. (b) The showcase ran `sim_days=1.0` and the proof showed the
spun-up field only, which at 1 day from a LINEAR salinity IC still reads as the IC.

**Fix (a) shoreline-following mesh.** `_build_estuary_mesh` now accepts an injected
`water_mask_fn(lon, lat) -> bool` and CLIPS the lattice to water: it keeps the
STRUCTURED lattice triangulation (clean 2-manifold; a re-Delaunay of the water subset
bridged concavities with slivers and broke the boundary walk) and drops only cells
whose CENTROID is land, keeps the largest connected water body, and re-indexes -- a
staircase shoreline the raster is masked by, so no cell paints land. The composer
builds the mask from the real USGS NHDArea WATER polygons (a ~2 s vector query, the
same source the TELEMAC pipeline samples; far cheaper than a full-res CUDEM fetch).
The default estuary is switched to **Galveston Bay, TX**
`(-94.95, 29.35, -94.70, 29.75)` -- a broad open-water bay (79% water, well-covered
by NHDArea; Trinity/San Jacinto river inflow, the Bolivar Roads Gulf mouth at the
south edge) so the clipped mesh is a genuine bay (668 nodes / 1162 elements following
the real shoreline), not a mostly-land box. A latent `schism_gr3.tin_to_hgrid` bug was
fixed at the source: `remove_boundary_pinch_points` can drop cells WITHOUT orphaning a
node, leaving `n_elem` stale -> the element-table loop over-indexed `cells[]` (the
clipped mesh has pinch points; the old rectangle never did). `n_elem` is now resynced
unconditionally after pinch removal. This is used SERVER-SIDE via `load_gr3_bridge`
(the worker entrypoint does not import `schism_gr3`; the image runs pschism on the
pre-authored hgrid), so no image rebuild is required for the fix to take effect.

**Fix (b) demonstrate circulation.** Showcase `sim_days` 1.0 -> 4.0 (river + tide
restructure the field; still coarse-minutes wall). A NEW proof
`schism_baroclinic_circulation_salinity_change.png` renders spun-up surface salinity
MINUS the initial linear gradient (blue = fresher, red = saltier) -- a non-zero field
proves the 3D baroclinic solve MOVED salt (gravitational estuarine circulation +
tidal exchange), not the IC echoed back. The 28-day CORIE hindcast stays NATE-gated
(NOT run).

**Live evidence.** Galveston Bay (default AOI), `sim_days=4.0`, shoreline-clipped
668-node / 1162-element mesh (25 open-boundary nodes at the south Gulf mouth), run
`01KZH7B3DM2ADA4GXAETB4BSBN` through run_solver + the schism image (pschism wall
61 s): surface salinity 0.30-32.71 psu, bottom max 32.72 psu, stratification mean
9.98 / max 18.05 psu -- a strong salt wedge (the 4-day spin-up nearly doubles the
1-day mean 5.19). The change-from-IC map moves -14.2 psu (surface freshening, the
estuarine outflow) to +6.0 psu (tidal salt intrusion) -- the field is NOT the IC
echoed back. Proofs regenerated in place: surface + bottom salinity (triangle-masked
to the real mesh, no salinity on land), the shoreline-clipped mesh (real element
connectivity, no Delaunay fan), the stratification chart; NEW change-from-IC map.
Showcase re-seeded via the product path (`--only schism_baroclinic`): new case
`01KZH7YD7H18MNY6D8S17PPC4X` (3 persisted layers, reconnect-durable). The stale ADR
0189 case `01KZGNQ36SG737MEKVJZCAQMTA` (Delaware Bay, 1-day, unclipped) is superseded
and can be deleted from the dock. The SWAN showcase (`swan_wave_field` nonstationary)
did NOT change -- its `_APALACHEE` box was already the clean -85.5 AOI; only the proof
DRIVER used the void -84.3 box, so no SWAN re-seed was needed.

## Proof-script hygiene (also in this wave)

- ELMFIRE `elmfire_initial_attack_containment_probability_poc_delay_chart.png`: the
  bottom caption was a single over-long line clipped on both sides + bottom. Wrapped
  to three lines with a reserved bottom band (`rect=(0, 0.20, 1, 1)`, `va="bottom"`);
  now fully visible.

## Offline coverage (green, `env -u TRID3NT_CACHE_BUCKET`)

- `server/tests/test_schism_baroclinic.py` (10, +3 NEW): shoreline-clip keeps a
  water-centroid mesh smaller than the lattice with no deep-land node; all-land mask
  falls back to the full rectangle (loud); the authored deck's salt.ic carries no
  deep-land node.
- `server/tests/test_postprocess_telemac.py` (+1 assertion): `read_selafin` returns
  0-based `ikle`.
- `services/workers/schism/test_schism_gr3.py` (6, green): the `tin_to_hgrid` n_elem
  resync does not regress the rectangular path.
- Touched-engine slices all green: schism baroclinic/coupled-waves, telemac
  postprocess/rain, swan storm/deck (91 in the combined slice).

## Consequences

- Coded-tools metric: **+0 tools** (fixes only); registry 235 unchanged,
  EXPECTED_TEMPLATES unchanged.
- Worker images: **none rebuilt.** TELEMAC (postprocess reader + driver), SWAN (driver
  AOI constant only), SCHISM (server-side mesh authoring + composer) -- no worker
  RUNTIME code changed; the SCHISM `schism_gr3` fix is used only by server-side
  authoring (the entrypoint does not import it).
- Board rows for the three shortlist items annotated with the 0191 revision.
- No flood seam touched -> no flood canary mandated.

## Open issues / deferred

1. SWAN CUDEM exact-0.0 interior void: a worker-side void guard / interpolation
   gap-fill so a future void AOI fails loud (or fills) instead of meshing half-dry --
   deferred (data-quality issue; avoided a cross-cutting behavior change this wave).
2. The SCHISM baroclinic mesh + bathymetry remain a COARSE demonstration geometry
   (real shoreline, idealized linearly-deepening bathymetry). The surveyed-bathymetry
   coastal_tin baroclinic deck + the calibrated CORIE 28-day hindcast stay NATE-gated.
3. TELEMAC `nhd_area` (real variable banks) needs NHDArea coverage; short demo reaches
   without it use the constant-width ribbon around the real flowline.
