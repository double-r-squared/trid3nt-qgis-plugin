# ADR 0201 - Coastal water-edge narrow-pass connectivity + NHD retry (sandbox polish)

Status: accepted (Tampa Bay re-meshed and verified live)
Date: 2026-08-08
Extends: ADR 0194 (coastal water-edge re-mesh), ADR 0197 (render alignment fix).
Relates: ADR 0193 (custom-SDF meshing pattern).

## Context

ADR 0194's coastal water-edge builder unioned OSM `natural=coastline` water with
NHDPlus HR `NHDArea`/`NHDWaterbody` polygons, but kept a `NHDWaterbody` only when
it EXACTLY touched (`.intersects()`) the already-assembled water. NATE flagged
that Tampa Bay's Boca Ciega Bay -- the back-bay strip behind the Pinellas barrier
islands, reached from the Gulf only through narrow tidal passes (John's Pass,
Blind Pass, Pass-a-Grille Channel) -- was not fully meshed. Root cause confirmed
live: the Boca Ciega Bay `NHDWaterbody` polygon was being dropped by the
exact-touch filter because (a) OSM's mainland-facing bay shoreline is sometimes
undertraced relative to the Gulf-facing shore, so the bay never polygonizes into
one face with the open water, and (b) a several-metre digitization gap between
the independently-sourced OSM coastline and NHDWaterbody polygons at the pass
mouth defeats an exact geometric touch. ADR 0194's open questions also flagged
`hydro.nationalmap.gov` intermittently 500-ing with no retry, degrading silently
to coastline-only water.

## Decision

**Connectivity rule (dilation, not exact-touch).** A water polygon (or NHD
waterbody) is kept as part of the connected system if it lies within a bounded
distance -- `pass_dilation_m = 300.0` -- of the water already known to be
connected, applied transitively (a chain of near-touching parts all connect).
300 m is chosen to comfortably exceed real Florida tidal-pass digitization slop
between independently-sourced polygons (the passes themselves are 100-250 m
wide) while staying comfortably below the coast-to-inland-waterbody distance for
a genuinely isolated pond/lake (observed >1 km in this run; see verification).
This is deliberately NOT a keep-everything relaxation: anything farther than the
dilation from the connected system is dropped.

Two call sites apply the rule:

1. `nhd_water_union` -- a fixed-point loop grows the connected set: each round,
   any remaining `NHDWaterbody` within `pass_dilation_m` of the current
   connected-water frontier is pulled in; the frontier then grows and the next
   round can reach one hop further (this is what lets a chain of small
   waterbodies bridge through a pass). Stops when a round adds nothing.
2. `reconnect_narrow_passes` -- a final pass over the assembled water
   `MultiPolygon`'s parts: union-find over parts whose `pass_dilation_m` buffers
   intersect (via an `STRtree`, transitive), keep everything reachable from the
   largest (main) part, drop the rest, then close the kept union with
   `buffer(+d).buffer(-d)` so the pass itself becomes meshable water instead of a
   phantom land sliver splitting the domain. This catches anything the
   `nhd_water_union` leg didn't fully merge (OSM-side gaps, not just NHD-side).

**NHD retry.** `_arcgis_polys_retry` wraps each ArcGIS layer query (NHDArea
layer 8, NHDWaterbody layer 9) in a bounded exponential-backoff retry (3
attempts, 1s/2s/(capped 8s) waits). On final failure the leg degrades to
coastline-only water, logged loudly (`log.error`, not swallowed) and recorded in
provenance (`error` + `degraded: true`) rather than silently returning an empty
dict as before.

Sanity check on the rule itself (this session): back-bays connected through a
real narrow pass are correctly kept IN (Boca Ciega Bay, see verification); a
genuinely isolated inland water body with no path to the coast within 300 m
stays OUT (33 fragments were dropped this run; the largest, 0.231 km2 at
`(-82.3812, 28.0320)`, sits near the domain's NE corner, well inland of the bay
system with no connecting path within the dilation). The rule is sound.

## Verification (Tampa Bay, live, 2026-08-08)

**Boca Ciega Bay -- IN.** Point-in-polygon test: `(-82.75, 27.7)` and four 0.01
deg offsets around it all `contains=True` on the rebuilt water polygon. The
closeup proof render (`oceanmesh_standalone_tampa_bay_closeup.png`, framing
`(-82.79, 27.66, -82.66, 27.86)`) visually shows the back-bay strip between the
Pinellas barrier islands and the mainland fully meshed (cyan wireframe tracking
the real Intracoastal Waterway shoreline), not the phantom-land gap ADR 0194 v1
had there.

**Area added.** Water area grew from **2261.504 km2** (0194 baseline, exact-touch
filter) to **2500.048 km2** (this run, dilation-based) = **+238.544 km2**
(+10.5%), consistent with Boca Ciega Bay plus other narrow-pass-connected water
(646 NHDWaterbody polygons kept this run vs the 45 kept under the old exact-touch
filter, out of 8408 total candidates in the domain bbox).

**Inland pond spot-check -- OUT.** Isolated `NHDWaterbody` parts that failed the
final `reconnect_narrow_passes` union-find (33 of them) were inspected directly:
the largest, area 0.231 km2, centroids at `(-82.3812, 28.0320)`, `(-82.6806,
27.8685)`, `(-82.6845, 27.8713)`, etc. -- scattered inland, correctly excluded.
None of the kept-connected parts required more than the 300 m dilation to reach
the main body.

**NHD retry -- live endpoint stayed healthy, so exercised via a targeted unit
test instead of a live 500.** `hydro.nationalmap.gov` returned `degraded: false,
error: null` on both live NHD queries this session (no organic failure to
observe). Direct test of `_arcgis_polys_retry`/`nhd_water_union` with a mocked
flaky endpoint: (a) succeeds on the 3rd of 3 attempts with observed 1.0s/2.0s
backoff waits between attempts, total 3.0s; (b) on persistent failure, retries 3
times then raises, and `nhd_water_union` catches it, logs
`NHD water union DEGRADED to coastline-only for bbox=...` loudly, and returns
`(None, {"error": "...", "degraded": True, ...})` -- never a silent empty dict.

**Re-mesh (Tampa Bay, full domain, `build_coastal_water_edge_mesh.py run
("tampa_bay")`, foreground, no image rebuild -- container script mounted per
ADR 0193/0194):**

| | 0194 baseline | 0201 (this run) |
|---|---|---|
| water area | 2261.504 km2 | 2500.048 km2 (+238.5 km2) |
| nodes / elements | 10797 / 18726 | 10557 / 18378 |
| inverted elements | 0 | **0** |
| boundary closed | yes (18 loops) | **yes (17 loops)** |
| min quality qE | 0.4262 | **0.5882** |
| median quality qE | 0.9622 | 0.9664 |
| edge length (min/median/max) | 74-2826 m (256 m) | 86-3198 m (278 m) |

Quality gates pass: 0 inverted elements, boundary closed. Mesh quality improved
(min qE 0.43 -> 0.59) despite the added narrow-pass geometry, because the final
`buffer(+d).buffer(-d)` close in `reconnect_narrow_passes` smooths the
sub-resolution slivers that were previously either absent or a source of
degenerate triangles at the old exact-touch boundary.

**Emitted** (overwritten in place, `docs/proof/templates/oceanmesh_meshes/`):
`tampa_bay.2dm`, `tampa_bay.slf`, `tampa_bay_hgrid.gr3`, `tampa_bay.fort.14`.

**MDAL verify** (`QgsMeshLayer`): `tampa_bay.2dm` valid, 10557 vertices / 18378
faces; `tampa_bay.slf` valid, 10557 vertices / 18378 faces (matches).

**SERAFIN verify** (TELEMAC worker `TelemacFile`, read-only import):
`npoin=10557 nelem=18378 nvar=1 varnames=["BOTTOM"]`, x-range
`[-82.9000, -82.3838]`, y-range `[27.4800, 28.0600]` -- matches the domain bbox.

**Renders**: `oceanmesh_standalone_tampa_bay.png` (full domain) and
`oceanmesh_standalone_tampa_bay_closeup.png` (Pinellas barrier islands + passes,
Boca Ciega Bay framed and visibly meshed) re-rendered in place via the shared
`merc_render` module.

## Consequence

- Changed: `scripts/sandbox/oceanmesh/water_edge.py` only --
  `reconnect_narrow_passes` (new), `_dilate_deg`/`_area_km2` (new helpers),
  `nhd_water_union` (dilation fixed-point loop replaces exact-touch, gains
  `pass_dilation_m` kwarg with a default so the call in
  `build_coastal_water_edge_mesh.py` is unchanged), `build_coastal_water` (gains
  the same default kwarg + calls `reconnect_narrow_passes` as a final step),
  `_arcgis_polys_retry` (new). All existing function signatures are
  source-compatible (new kwargs are defaulted); no caller outside this file
  needed a change.
- Tampa Bay re-meshed and its four formats + two proof renders overwritten in
  place. Delaware Bay, duck_nc, puget_sound, coweeta_river were NOT touched this
  session (out of scope; Delaware Bay does not have Tampa's back-bay/narrow-pass
  topology so it was not expected to need this fix, but it has not been
  re-verified against the new dilation rule).
- No image rebuild (container script mounted, not baked, per ADR 0193/0194). No
  workflow / tool / category / board / server-tree file touched (parallel wave
  owns that surface this session).

## Open questions

- Delaware Bay + duck_nc + puget_sound have not been re-run against the dilation
  connectivity rule; if any has a similar narrow-pass back-bay it would benefit
  from the same re-mesh (not verified either way this session).
- `pass_dilation_m=300.0` is a single fixed constant tuned to Florida Gulf-coast
  tidal passes; a much wider or much narrower real-world pass elsewhere could
  need a different value. Not parameterized per-AOI in `AOIS` yet.
- Local UTM meshing (vs the current EPSG:4326 degree frame) remains ADR 0192 Q1.
- Placement (TELEMAC geometry / SCHISM hgrid / registered tool) stays NATE's.
