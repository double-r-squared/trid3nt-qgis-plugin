# `render/` - the format set a product reaches the map in

Render is the package that turns data into what the user sees. It accepts
exactly what QGIS opens and nothing else:

| kind | what arrives | what the map gets |
|---|---|---|
| raster | a COG in the object store, EPSG:4326 | a raster layer, styled from the declared row resolved against its own band |
| vector | a GeoJSON FeatureCollection in lon/lat | a vector layer written under the run prefix |
| mesh | an MDAL file (SELAFIN, 2dm, UGRID, HEC-RAS 2D HDF) plus the dataset files written beside it | a mesh layer with ONE dataset group selected, under the temporal controller |
| chart | a payload already built | the chart the dock renders, persisted under the run |

An engine whose output is not in the set carries ONE transform module beside its
module surface that normalizes it. An engine whose output is already in the set -
TELEMAC, whose result IS a SELAFIN - carries none.

Nothing under `render/` paints a field or reshapes an engine's read. The values
behind a product are measured where they are read; `dev/lint/format_set.py`
holds the boundary in both directions.

## What a TELEMAC output becomes

| a primitive | the product |
|---|---|
| `field(name, t="every").animate()` | the results mesh layer, painting the dataset group the result file carries for that variable, with the run's own reference time so the temporal controller scrubs the right clock |
| `field(name, t)` | the same mesh, painting a single-step group the module outputs wrote beside the results for that instant |
| `max_over_time(name)`, a plane of a 3D result, a variable the module DERIVES | the same, for a group no result file carries |
| `series`, `profile`, `column` | a chart payload |
| `drogues()`, a series at a station | a GeoJSON vector layer |
| an answer | a measure on the sheet, never a layer |

A derived group is written as the SMS ASCII dataset (`DATASET / BEGSCL / ND /
NC / NAME / TS`) - the least machinery MDAL reads - by
`render/mesh_display.write_ascii_dataset`, uploaded under the run prefix by the
module outputs, and named on the layer's `dataset_uris`. The plugin stages the
mesh and every dataset file, calls `addDatasets` on each, and only then binds
the declared group by name. A node below the read's floor is written as nothing,
so the field draws where it is visible and the basemap shows through where it is
not.

A group the RESULT FILE carries cannot be rewritten that way - the animation
plays from the engine's own SELAFIN - so the floor travels on the layer's style
row instead: the shader ranges FROM the floor and sets the colour ramp's
`clip`, and QGIS leaves every below-floor node unpainted. One field, one absent
region, on the still and on the animation. The packet's own GIF renderer masks
at the same edge, off the same row.

A 3D SELAFIN is NOT an MDAL mesh: MDAL rejects the file. TELEMAC-3D writes the
2D result beside it, and that file is the mesh a plane is drawn onto - the run
states it as `display_basename`.

## Style

A producer declares a style ROW (`kind`, `ramp`, `units`, `label`, and where the
legend is ranged from: a `center` for a diverging ramp, a `floor`, a `p<q>` cap).
The FORMAT decides which of the four preset shapes draws it; the row's range
semantics are how the producer MEASURED the range, and what the layer carries is
the range itself, as a fixed `scale` - plus, on a mesh, the `floor` the PRODUCT
measured, because the mask is still ahead of the renderer. Every product of one
quantity is ranged together, so a still and its animation read on one ramp, and
a legend end rounds AWAY from the field so no value falls outside the range that
clips against it.

There are no preset NAMES: `presets.py` closes a four-kind family (continuous
raster, classed vector-or-raster, reference outline, mesh dataset group) and
writes the `.qml` the map loads. `restyle.py` is the user's edit of a declared
row, journaled with the sentence the legend ends up saying.

## A run's outputs

There is ONE registry. `formats.publish` writes every layer it emitted onto the
run in progress, and `journal.build_record` carries that list into the run's
journal line as its `outputs` field. `render/outputs_seam.run_outputs(run_id)`
and the `list_run_frames` tool read it there and nowhere else - the artifacts a
row points at are delete-on-whim and the session that saw the layers ends, while
the record outlives both.

## Files

Described one by one in `trid3nt_server/render/README.md`. The seam is modeled
in `docs/model/render-seam.sysml`.

## Invariants

- A "modeled" envelope with empty layers NEVER reads status=ok (the honesty floor).
- Input layers surface via `purpose=` on router fetches, never hand-emitted.
- Publication is automatic: a product is published as it is made, and there is no
  verb for showing one. The un-emit is a visibility flip on a row the canvas
  already holds.
- MemoryFile-backed raster reads keep the file alive for the dataset lifetime
  (orphaning is GC-timing corruption).
