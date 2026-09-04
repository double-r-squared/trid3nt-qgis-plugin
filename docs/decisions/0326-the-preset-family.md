# 0326 - the preset family: four kinds, and presentation declared where the data is

## What changed

A preset stopped being a NAME. It is now one of four RENDERER SHAPES -
continuous raster, classed (a vector's graduated symbols or a raster's discrete
bands), reference outline, mesh dataset group - and everything a quantity
contributes to a picture (its ramp, its units, its legend title, the one scale
it is read on) is a PARAMETER of one of those four.

Presentation is declared where the DATA is declared:

* a fetcher carries a `style:` row in its own `source.yaml` (106 specs migrated);
* a solved output derives its row from the `outputs.json` entry's kind,
  quantity and units;
* an engine product contract carries its row beside the quantity it names
  (`TELEMAC_DYE_STYLE` and its siblings);
* a workflow declares NONE of it. `.style()` is gone from the plan vocabulary.

Everything ad hoc happens at runtime on ONE surface: `restyle_layer` re-paints,
retitles, rescales, re-shapes or HIDES a layer already on the map, journaled
with the sentence its legend ends up saying.

## Why

Two failures the old shape made structural.

**A quantity could be unregistered.** `styles.yaml` held a per-quantity preset
zoo and a `quantity -> preset` table; a producer that named a preset the table
did not declare fell to the neutral ramp, whose legend title is the word
"Value". Six live products were in exactly that state, and their names were the
tell: `continuous_wave_agitation`, `continuous_stratified_flow`,
`continuous_significant_wave_height`, `continuous_dissolved_oxygen`,
`continuous_water_surface_elevation`, `continuous_coastal_inundation_depth` -
each a preset NAME an engine invented, none of them a row anyone wrote. One
layer (TELEMAC-3D) carried two names at once: the raster was published under
`continuous_stratified_flow` while the packet's animation resolved
`continuous_temperature_c`, so a mode that rasterizes salinity or velocity was
labelled with a temperature's fixed 0-40 C band.

Under the new shape those cannot recur: there is no table to be missing from.
An agitation coefficient is a continuous raster titled "Agitation coefficient
(Kd)" because its product contract says so, and a TELEMAC-3D field is titled
with the variable the run actually rasterized.

**A style could be guessed from a filename.** `publish.py` tokenised the URI and
the layer id to infer a preset (`dem`/`relief`/`hillshade`/`slope`/`aspect` ->
terrain passthrough). A name is not a measurement. That mapping is deleted; a
DEM is grey and metres because `fetch_dem`'s own row says so.

## The .qml is the preset

The presets are QGIS's own style documents: their format, our writer (a subset
templater in `emission/presets.py`), their validator. The validator is
STRENGTHENED past `loadNamedStyle`'s boolean, which reports well-formedness and
nothing else: `scripts/qml_preset_smoke.py` loads every document the family can
produce into the installed QGIS 3.40.6 and asserts the POST-LOAD state - the
renderer QGIS ends up holding, the stops read back at the values and colours
written, the range the layer reports.

Two facts that gate came back with, both of which a "it returned True" check
would have shipped past:

* a mesh preset must bind its dataset group BY NAME (`name-to-global-index`).
  Without that row QGIS accepts the document, drops the whole
  `mesh-renderer-settings` block, and keeps its default plasma ramp.
* a `renderer-v2` symbol whose class does not match the layer geometry loads
  cleanly and then draws nothing. So the writer emits the symbol for the
  DECLARED geometry, and a row that declares none gets NO document at all -
  QGIS's own default per geometry stands in. A wrong picture is worse than an
  unstyled one.

## What a reader loses, stated

The per-quantity colour choices that lived in `styles.yaml` survive only where a
declaration carries them. A fetcher's row carries its ramp; a product contract's
row carries its ramp; a solved output that declares no ramp takes the continuous
kind's default over the run's own range. Two solved fields on one canvas are
therefore told apart by their titles and units rather than by their hues until
someone restyles one - which is a one-second answer, not a re-solve.

## Measured

| dies | LOC |
|---|---|
| `contracts/trid3nt_contracts/styles.yaml` + `styles.py` (the preset zoo + its loader) | 741 |
| `emission/styles.py` (the name-keyed resolver) | 370 |
| the five test modules that policed the contract, the name inference and the resolver | 941 |
| `_infer_style_preset` + `_TERRAIN_STYLE_TOKENS` + `_SLOPE_ASPECT_PRESET_BY_TOKEN` + `_label_from_style_preset` + `style_preset_for_publish` + `style_params_from_band_stats` + `_parse_style_params` + the `&rescale=..&colormap_name=..` string | in `publish.py` |
| `StyleSpec` + `Step.style()` + `Gate.style()` + the interpreter's style node + `RenderSourceMissingError` | in `runtime/` |

| arrives | LOC |
|---|---|
| `emission/presets.py` (the family, the resolver, the .qml writer) | 603 |
| `emission/restyle.py` (the one presentation surface) | 137 |
| `tests/test_presets.py` + `scripts/qml_preset_smoke.py` | 442 |

## Still standing

`LegendKey` remains the wire form of a resolved style and now carries the
`.qml`. The plugin builds its renderer from the key's ramp and range; loading
the shipped document instead is the plugin leg's work, and the ledger carries
that condition.
