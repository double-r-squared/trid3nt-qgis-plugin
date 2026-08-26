# 0318 - The reach family migration: the meshed river becomes the visible river

## Context

ADR 0317 migrated four of the seven TELEMAC families out of the container and
left the reach family - `telemac_river_dye` and `telemac_do_sag` - holding SIX
network fetches inside `telemac_river_dye_build.py`: an NLDI `comid/position`
snap, two NHDPlus_HR `/3/query` flowline re-seeds, the NLDI navigate that IS the
model centerline, an NHDArea `/8/query` bank fetch, and a private
Copernicus-STAC -> 3DEP DEM ladder. It named two blockers, both measured rather
than assumed, and deferred rather than guessing.

NATE ruled the first one: **NLDI EVERYWHERE**. The NLDI mainstem centerline
promotes to a server-side router spec like every other fetch and becomes the
DISPLAYED river input layer - the meshed river is the visible river. OSM
waterways demote to map context.

The second blocker was measurable, and measuring it is what this note is mostly
about.

## The Copernicus deferral, closed by fixing the sampling rather than by widening a tolerance

ADR 0317 measured the router's `fetch_dem(source="copernicus")` mosaic against
the worker's own `/vsicurl` read of the same GLO-30 tiles at RMS 3.87 m, max
22.3 m on valley walls. The obvious move was to accept it: `fetch_dem_bed` does
not hand TELEMAC per-node elevations at all. It reduces the DEM to exactly TWO
scalars - a least-squares along-channel slope, then CLIPPED to
`[min_bed_slope, max_bed_slope]`, and a 20th-percentile bed top - and lays a
clean monotonic plane. Measured on the Eel River canary corridor, those two
scalars moved by 2.6e-6 in slope (0.23% relative) and 0.000000 m in bed top: a
9 mm difference in total bed drop over 3.5 km. That would have passed any honest
tolerance.

It was still the wrong answer, because the CAUSE was diagnosable and cheap to
fix. The router's `stac_float` executor always computed its destination grid
from the bbox and `native_cell_m` and bilinear-reprojected onto it - over the
canary window, 142x124 px at 0.000356 x 0.000270 deg, against a source that is
3600x3600 px at exactly 1/3600 deg. The difference was not data; it was a
resample onto a lattice of the router's own.

**The `stac_float` executor gained a declared `px_per_deg` sizing, and the
lattice's PHASE comes from the data.** This is the same shape ADR 0317 gave the
ImageServer executor, with one thing learned the hard way: a global 1-arcsecond
DEM tile is pixel-is-POINT, so its pixel CENTRES sit on the integer arcsecond
and its EDGES sit half a pixel off the integer degree. A lattice snapped to the
prime meridian lands every destination centre exactly BETWEEN two source centres
- the worst alignment available, and measurably worse than the metric grid it
replaced (RMS 1.26 m against 1.03 m). Snapping to the source's own origin, read
off one probed item's header, and resampling NEAREST gives:

    RAW per-point delta vs the worker's own /vsicurl sample:
      RMS = 0.000000 m   mean = 0.00000000 m   max|d| = 0.000000 m
      EXACT per-point parity: True

The staged bed IS the pixels the worker used to fetch. `fetch_copernicus_dem`
declares `px_per_deg` as an OPT-IN request param, so no other consumer's grid
(or cache key) moves; the reach bed producer asks for 3600.

## Decision

**The river is a server-tier fetch, and it is the layer the user sees.**
`steps/reach.resolve_reach_river` resolves the seed, fetches the centerline, the
banks and the bed, and returns the manifest `inputs` rows. The deck writer calls
it - the same seam ADR 0317 used for the open-water beds - and `stage_manifest`
carries the rows the launcher already walked. Three files land in the run
directory: `river_centerline.geojson`, `river_banks.geojson`, `bed_source.tif`.
GeoJSON and not FlatGeobuf because the image carries shapely and no geopandas,
and the staged text is the shape the responses arrived in anyway.

**Two new specs, and one bug fixed in an old one.**
`fetch_nhdplus_hr_flowlines` (NHDPlus HR layer 3, by envelope, optionally by
GNIS name) serves both seed rungs; `fetch_nhd_area_water` (layer 8) serves the
banks. Both reproduce the request rather than approximating it: the server-side
generalization tolerance rides as a static endpoint query param because a
different tolerance finds a different nearest vertex, and the record cap rides as
a declared `max_records` param because the two questions this layer answers cap
at different counts and the cap is part of the request when the envelope holds
more rows than it. Verified feature-for-feature against the worker's own calls:
the named-seed vertex is identical, the bank polygons' exterior-vertex multiset
is identical (6559 values), the navigated flowlines' vertex multiset is identical.

The old bug is in `fetch_nhdplus_nldi_navigate`, which rounded `distance_km` to a
whole kilometre. Below 1 km that is not a rounding: NLDI returns whole reaches
until the cumulative distance is EXCEEDED, so the do_sag canary's 0.5 km ask
rounded to 0 and would have returned ONE flowline where the ask means four - a
2.3 km river instead of a 3.5 km one. The distance travels as the float it was
declared as.

**The seed ladder stops failing open.** `_named_flowline_seed` and
`_mainstem_flowline_seed` degraded to the raw seed on ANY exception, so a slow
NHDPlus query meshed a different reach and nothing recorded which had happened -
the cause of `telemac_do_sag_refined`'s non-determinism. The ladder now
distinguishes two things the old one could not: "the query answered, and the
answer was no improvement" is a DECISION and is recorded as a named rung
(`position-named-flowline-absent`, `position-nearest-flowline`), while a fetch
FAILURE raises. The run records the rung, the navigated COMIDs and a sha256 of
the staged centerline bytes, which makes "the same run twice" checkable rather
than an impression.

**The reach bed COG dies, with the last bespoke surfacing helper.** It existed
only because a container fetch could not reach the emit seam, and it painted the
input as a scatter of node samples rather than as the terrain the run was given -
the third node-dot instance. `products._surface_bed_bathymetry_input` was the
last surviving `_surface_*input*`; the seam is now the only path in the tree.

**`--network none` on the reach spec closes the image.** The reach spec also
serves the rain-on-grid catchment, so one line puts both remaining legs behind
the denied network. Every TELEMAC leg is an engine room.

**The reach run records where it modelled and what it was given.** The solve path
never wrote a bbox (only the mesh preview did), so a packet check had nothing to
test the animation frames' extent against - a frame at the UTM false origin was
indistinguishable from a frame on the water. It now writes the mesh's own 4326
extent, and `bed_source` - the dataset label, which a worker that opens a file
cannot know and the server that fetched it can.

## Consequences

Parity, run for run through the rebuilt no-network image, against the recorded
pins: **`telemac_do_sag_refined` 15/15 metrics IDENTICAL** (DO minimum 9.0081
mg/L at 123.5 m, the full 60-station sag and CBOD curves to the last digit);
**`telemac_river_dye_refined` 15/15 IDENTICAL** (peak 41.38519287109375 mg/L at
40 s, plume reach 645.1 m); **`telemac_rain_on_grid` 27/27 IDENTICAL**;
**`coastal_tidal_surge_refined` 18/18 IDENTICAL** (the flagship packet the
CO-OPS outage blocked last wave re-ran clean); **`artemis_harbor_agitation`,
`artemis_harbor_agitation_refined`, `artemis_harbor_resonance_idealized`,
`telemac3d_stratified_flow` (coarse), `tomawac_wave_field` (coarse),
`coastal_tidal_surge` (coarse), `telemac_do_sag` (coarse) all IDENTICAL.**

The reach pins did NOT need re-pinning. That is the point of fixing the sampling
rather than widening the tolerance.

Determinism, proven rather than asserted: two consecutive
`telemac_do_sag_refined` runs with no code change between them produced
IDENTICAL values on all 15 metrics, including a byte-identical sag curve. This
is the canary that flipped between two runs last wave.

Two REFINED open-water pins moved, and neither is this wave's: their baselines
were last written at `33e879cf`, BEFORE ADR 0317's own bed migration, which
re-ran and re-pinned only the coarse legs of those two families.
`tomawac_wave_field_refined` gained the `target_resolution_m` provenance row that
ADR 0315 landed (every physical metric identical);
`telemac3d_stratified_flow_refined`'s `stratification_dt` moved 3.3786 -> 6.9524
against that stale pin, reproduces exactly across two runs on this build, and
sits on a code path this wave does not touch (its bed is the ImageServer
`fetch_ncei_dem_mosaic`, not `stac_float`). Both are re-pinned here with that
provenance stated.

The delivered layer roster, which is the ruling made visible:

    Input: OSM waterways (map context; the modeled river is the NLDI centerline)
    Release point / Outfall
    Input: the modeled river centerline (nhdplus_nldi)
    Input: the river banks (nhd_area_water)
    Input: river bed elevation (copernicus_dem)
    <the answer layer>

The two seed-ladder queries fetch with `visualize=False`: what they contribute to
the run is ONE VERTEX, and painting the whole named watercourse beside the
centerline cut from it would say the run models both.

Worker LOC: `workers/telemac/` product 9132 -> 8689 (-443), test 2336 -> 1998
(-338). Running net across the two migration waves: product 9318 -> 8689 (-629),
test 2383 -> 1998 (-385).

## Two things this wave found and did not cause

**The proof renderer spoke the wrong colormap dialect.** The style contract's
vocabulary is rio-tiler's, which is lowercase; matplotlib spells every
ColorBrewer ramp in CamelCase. `render_all_layers_proof` handed the legend's name
straight to `imshow`, so any layer whose legend names `rdylbu` - the
dissolved-oxygen field does - raised a hard ValueError and took the whole
delivery packet down, while `viridis` and `gray` happened to spell the same in
both and hid the mismatch. Resolved case-insensitively against matplotlib's own
registry rather than against a second hand-written table that could drift from
the first.

**Five proof folders held two panel generations at once.** The assembler refused
on them, correctly: a layer roster that changes renames the panels, and the
previous generation sits there under names nothing writes any more. The
superseded panels are removed - which is the assembler working, not a folder
being pruned.
