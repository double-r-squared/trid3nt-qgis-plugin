# pynhd.flowline_xsection + py3dep.elevation_profile, beside what we have - measured

Asked by the HyRiver hydro stage: `docs/IDEAS.md` "HYRIVER, THE WIDER MAP" (c).
**Nothing folds.** This is the measurement, and the verdict is that neither library
function is an equivalent of the code it was proposed against.

## The two subjects on our side

| our code | LOC | what it is |
|---|---:|---|
| `tools/processing/section/section.py` | 455 | the `between` cut: a polygon sliced by the two lines PERPENDICULAR TO THE CHORD at each end, measured in the reach's own UTM zone; `_band` 16, `_end_face` 36, `_cut_between` 62, `_cut_within` 15, `section` 116 |
| `tools/processing/compute_cross_section/compute_cross_section.py` | 743 | elevation-along-a-line: N stations at equal arc length, cumulative GEODESIC distance, one vectorized `src.sample` per layer, N layers on one shared axis, a Vega-Lite chart envelope; `_resolve_line_coords` 94, `_interpolate_stations` 34, `_sample_layer` 73, `compute_cross_section` 163, `_build_profile_spec` 106 |

## pynhd.flowline_xsection - 60 LOC, and it answers a different question

```
flowline_xsection(flw: GeoDataFrame, distance: float, width: float,
                  id_col="comid", smoothing=None) -> GeoDataFrame
```

It takes an NHDPlus FLOWLINE FRAME in a projected CRS, splines each merged
LineString, and emits a cross-section LINE of fixed `width` every `distance`
metres along it (`_xs_planar` 47 LOC, plus `flowline_resample` 45 and
`network_xsection` 42 beside it).

**What it would replace: nothing we have.** Our `between` cut takes TWO NAMED POINTS
and a POLYGON somebody mapped, and returns the POLYGON SUBSET between them plus the
two transects the cuts left (`face_start` / `face_end`, which is what a solver
prescribes an inflow across). `flowline_xsection` takes no points, no polygon, and
returns neither a subset nor a face - it returns a regular sampling of section LINES
along a network. The chord-perpendicular UTM cut, the end-face measurement off the
section's own boundary vertices (`_end_face`, and the typed
`SECTION_END_FACE_UNMEASURED` when an end lands on a bank rather than across the
reach), the parts-kept/parts-dropped accounting and the refusal on a polygon-less
input are all outside its signature.

**What it does that we do not:** a spline-smoothed, regularly-spaced section SET over
a whole river network, keyed by `comid`. That is a useful thing and we have no
equivalent - but it is a new capability, not a replacement, and it needs an NHDPlus
flowline frame with a `levelpathi` column that our chain does not carry.

## py3dep.elevation_profile - a partial overlap, on one raster only

```
elevation_profile(lines: LineString | MultiLineString, spacing: float,
                  crs=4326) -> xr.DataArray   # dims z; coords x, y, distance
```

It splines the line, samples at uniform `spacing` in metres, and returns elevation
with a `distance` coordinate - which is `_interpolate_stations` (34) plus the
geodesic distance axis, for ONE dataset.

**What it would replace:** roughly 34 of `compute_cross_section`'s 743 lines, and
only when the profile wanted is 3DEP elevation.

**What it does not replace, measured against the tool's own contract:**

- **N layers on one axis.** The tool's stated differentiator is the OVERLAY - ground
  vs water surface, DEM vs bathymetry, head vs land surface. `elevation_profile` is
  3DEP and nothing else; it cannot sample a layer the Case already holds.
- **Any raster but 3DEP.** `_sample_layer` (73) opens an arbitrary `layer_uri`,
  including the `s3://` + `MemoryFile` staging path, reprojects the stations into
  that raster's CRS and reads them in one vectorized call.
- **The honest gap.** Nodata / out-of-bounds stations come back `None` and the line
  breaks there. A spline through a 3DEP request has no notion of a station the
  caller's own layer does not cover.
- **The chart envelope.** `_build_profile_spec` (106) + `_build_caption` (23) emit
  the Vega-Lite chart-emission payload the plugin renders; the library returns a
  DataArray.
- **The line resolve.** `_resolve_line_coords` (94) accepts a GeoJSON LineString, a
  vertex list, or the FeatureCollection `request_spatial_input` hands back.

It also SPLINES the line before sampling, where the tool samples the line as drawn.
For a profile that must correspond to a drawn transect, a smoothed path is a
different transect.

## Verdict

| candidate | replaces | LOC it would remove | recommendation |
|---|---|---:|---|
| `pynhd.flowline_xsection` | nothing in the tree | 0 | **do not fold.** A candidate NEW capability (regular section sets over an NHDPlus network) if that question is ever asked |
| `py3dep.elevation_profile` | the station interpolation, for 3DEP only | ~34 of 743 | **do not fold.** It would trade a raster-agnostic multi-layer sampler for a single-dataset one, and splines a line the caller drew |

Reported, not folded, per the ruling.
