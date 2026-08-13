# ADR 0165 - MODFLOW real-AOI georeferenced capture zone (wellhead protection)

Date: 2026-08-06
Status: accepted

## Context

ADR 0163 landed native mf6 PRT backward tracking as the `capture_zone` /
`wellhead_protection` archetypes. That machinery is already georeferenced: it
transforms the well lat/lon to real UTM, builds a 41x41 x 100 m grid centred on
the AOI, runs a steady GWF solve + PRT backward tracking, and
`postprocess_capture_zone` reprojects the tracks to EPSG:4326 as isochrone
polygons. Two things kept it from answering the wellhead-protection product
question ("what land does my well draw from") on a real site:

1. The regional gradient was a HARDCODED west->east CHD (0.002 m/m). A capture
   zone's orientation is dominated by the flow direction, so a fixed west->east
   boundary points the zone the wrong way at almost every real site.
2. The only emitted geometry was the convex-hull isochrone polygons. The
   backtracked PATHLINE fan - the legibility element that shows which land the
   well draws from - was never emitted.

Triage-first confirmed the fold surface: no new tool is needed. The existing
`modflow_capture_zone` / `modflow_wellhead_protection` registered surface, the
`_build_prt_capture_zone_deck` adapter, and `postprocess_capture_zone` already
carry the georeferenced grid/CRS/PRT plumbing. This is a FOLD onto that surface.

## Decision

Add a GEOREFERENCED gradient mode + pathline emission to the existing capture-zone
surface (no new registered tool):

- **DEM-derived regional gradient (screening water-table proxy).** The composer
  fetches a DEM over the AOI via `TOOL_REGISTRY["fetch_dem"].fn` (Seam-1) and
  fits a plane to the elevation (`_planar_gradient_from_dem` -> `_fit_plane`),
  yielding a gradient vector `(gx, gy)` in local east/north metres. Under the
  shallow-unconfined subdued-replica assumption the water table mimics surface
  topography, so this slope is a SCREENING proxy for the hydraulic gradient - NOT
  a measured potentiometric surface, stated loudly in the docstring, the summary
  `gradient_caveat`, and the proof caption. Magnitude is clamped to a plausible
  aquifer range `[5e-4, 5e-2]` m/m (direction preserved); a below-floor near-flat
  AOI, a DEM fetch failure, or `use_dem_gradient=False` is a LOUD typed fallback
  to the legacy west->east demo gradient (`gradient_source="demo_west_east"`),
  never a silent wrong-direction zone.

- **Directional CHD.** `_build_prt_capture_zone_deck` imposes a PLANAR head field
  on the full perimeter ring - `head(x,y)=TOP + gx*(x-xc) + gy*(y-yc)` - so
  groundwater flows down-gradient `-(gx,gy)` and the capture zone extends
  up-gradient toward recharge. Absent a gradient vector the legacy west+east-only
  CHD is byte-identical (existing tests + manifests unchanged).

- **Pathline emission.** `postprocess_capture_zone` now emits one LineString
  feature per backtracked particle (`feature_type="pathline"`, the geoclaw
  particle-track pattern) into the same EPSG:4326 FlatGeobuf alongside the outer
  envelope + the isochrone travel-time bands. The layer carries `pathline_count`.

- **Grubb screening sanity.** The layer carries the Grubb uniform-flow analytic
  capture width `B = Q/(K*b*i)` and stagnation distance `x0 = Q/(2*pi*K*b*i)` as
  order-of-magnitude ballparks against the PRT envelope (narrated, never a
  calibrated width).

Threading is dedicated MODFLOW-contract fields (`MODFLOWRunArgs.regional_gradient_x
/_y`, `CaptureZoneLayerURI.pathline_count / gradient_source / gradient_magnitude /
gradient_azimuth_deg / stagnation_distance_m / capture_width_m`) so no shared
physics-registry surface is touched. In-process flopy/mf6 (PRT is LOCAL-ONLY); no
image lane.

## Consequence

`modflow_capture_zone` / `modflow_wellhead_protection` now answer the wellhead-
protection product question on a real AOI over satellite imagery: a DEM-oriented
pathline fan + nested time-of-travel bands + the well, showing the contributing
land. The result stays a SCREENING-tier delineation (100 m grid, DEM water-table
proxy, demo K/porosity) - not a legally defensible WHPA. The demo-gradient path
is preserved as a loud fallback. Existing archetype behaviour is byte-identical
when no gradient vector is supplied.

Live smoke (Platte River alluvial valley nr Grand Island NE, well at
40.905, -98.42, wellhead_protection [5/10/25]-yr, 48 particles): DEM gradient
1.35e-3 m/m, flow azimuth 64.7 deg (ENE, capture extends up-gradient WSW), 48
pathlines, zone areas 0.075 / 0.17 / 0.45 km^2 (5/10/25 yr) + 2.79 km^2 envelope,
Grubb capture width 1376 m, stagnation 219 m. Proof:
`docs/proof/templates/modflow_capture_zone_georef{,_chart}.png`.
