# Conformance: emission-fold rev 1, sections 2-3 (the preset family)

Clause by clause against `docs/specs/emission-fold.html` rev 1 as amended, and
against the four items the slice was handed. Deviations are REPORTED, not
auto-fixed.

## Section 2 - the paradigm

| clause | verdict | evidence |
|---|---|---|
| emission is AUTOMATIC; no `.emit()` exists anywhere | HELD (unchanged) | `grep -rn "\.emit()" trid3nt_server/` returns nothing this wave touched |
| `restyle(...)` is the ENTIRE presentation surface - ramps, titles, presets, visibility | LANDED | `tools/display/restyle_layer` takes `ramp` / `title` / `units` / `kind` / `policy`+range / `transform` / `clip` / `shared_scale` / `hide`; `emission/restyle.apply_style` is the one writer |
| `restyle(hide)` is the un-emit | LANDED | `restyle.set_hidden` -> `PipelineEmitter.set_layer_visible` flips the row and re-emits session-state; the plugin materializer applies a visibility flip to a layer it has ALREADY added (`_apply_visibility`), which is what makes the un-emit reach a canvas rather than only a payload |
| journaled | LANDED | `apply_style` writes `journal_note(f"restyle {layer_id}: {legend_note}")` - the run journal seam, not a second log |
| a user choice always beats a preset (per-case durability) | UNTOUCHED | native QGIS restyles + per-case durability are the plugin leg's; nothing in this slice writes over a user's own styling |

## Section 3 - the uniform family

| clause | verdict | evidence |
|---|---|---|
| presets keyed by DATA KIND, never by quantity; roughly FOUR | LANDED | `presets.KINDS == ("continuous", "classed", "reference", "mesh")` |
| quantity specifics (units, label, the one-scale range) are PARAMETERS | LANDED | `Preset` carries `ramp`/`units`/`label`/`scale`/`classes`/`geometry`/`attribute`/`dataset_group`; `tests/test_presets.py::test_a_quantity_parameterises_the_preset_it_never_mints_one` |
| the per-quantity preset zoo dissolves; `styles.yaml` and its contract retire | LANDED | `styles.yaml` (591), `contracts/styles.py` (150), `emission/styles.py` (370) deleted; ledger row 1 |
| the one-scale-per-quantity law moves into the preset parameters | LANDED | `Scale` lives on `Preset`; `presets.shared_range` is the comparison-set law; `outputs_seam._run_ranges` still computes ONE range per quantity over the whole run |
| every emitted layer ships styled by its kind's default | LANDED | `presets.from_row(None)` is the continuous bare default; `bare_default(kind)` per kind |
| FETCHER STYLE LIVES IN THE FETCHER'S DECLARATION: `source.yaml` gains a `style:` row (kind + parameters) | LANDED | 106/106 specs carry one; 58 reference, 41 continuous, 3 classed |
| emit-on-fetch reads THE ROW, never a Python mapping; the buried mappings die | LANDED | `emit_on_fetch` forwards `layer.style`; `_infer_style_preset`, `_TERRAIN_STYLE_TOKENS`, `_SLOPE_ASPECT_PRESET_BY_TOKEN`, `_label_from_style_preset`, `style_preset_for_publish` deleted; ledger row 2 |
| a spec without a style row gets its kind's bare default | LANDED | `OutputSpec.style` is optional; `from_row(None)` |
| migrate the ~106 specs mechanically from what the buried mappings said | LANDED WITH TWO STATED CORRECTIONS | see "Deviations" |
| sim outputs derive their default from the product contract's kind + quantity metadata | LANDED | `outputs_seam.entry_style(entry)` = kind from `entry.kind`, label from `quantity_label(entry.quantity)`, units from `entry.units`, dataset group from the quantity for a mesh; the TELEMAC product contract carries a row per product |
| the preset set is curated `.qml`; the yaml-to-qml compiler dies | LANDED | `presets.qml()` writes the subset directly; no yaml-to-qml compiler was ever built, so nothing to remove |
| THEIR format, OUR writer, THEIR validator STRENGTHENED - the gate asserts POST-LOAD STATE (renderer type as intended, ramp stops read back exactly, the render changed) | LANDED | `scripts/qml_preset_smoke.py`: 30 checks over 4 kinds + 3 reference geometries + the classed raster/vector split, all three assertions the spec names |
| every generated `.qml` LOAD-VALIDATES through the qgis smoke before shipping | LANDED | smoke `all_passed` on QGIS 3.40.6; plus `scripts/proof_declared_style_live.py`, which load-validates the documents shipped for REAL fetched artifacts |
| presentation never appears in a workflow declaration | LANDED (and enforced by deletion) | `StyleSpec` / `Step.style()` / `Gate.style()` / the interpreter's style node deleted; ledger row 4 |

## The named findings

The prompt names "the S1-S4/A1 preset-mislabel findings from the re-pin
interrogations". I could not locate a document carrying those labels
(`docs/`, `reports/`, git log bodies, the packet folders) - REPORTED, not
guessed. What I did instead is measurable: every preset NAME live products
published under was resolved against the dying contract, and the ones that
resolved to NOTHING are listed below with what they resolve to now. This is the
class the spec names ("the temperature_c-vs-stratified_flow class of finding"),
and its second-to-last row IS that finding.

| product | published under | the contract declared | now |
|---|---|---|---|
| ARTEMIS agitation Kd | `continuous_wave_agitation` | nothing -> untitled viridis | continuous, `Kd`, "Agitation coefficient (Kd)" |
| TOMAWAC Hs | `continuous_significant_wave_height` | nothing (`continuous_wave_height` existed, unused) | continuous, gnbu, `m`, "Significant wave height" |
| WAQTEL dissolved oxygen | `continuous_dissolved_oxygen` | nothing (`continuous_dissolved_oxygen_mgl` existed, unused) | continuous, rdylbu, `mg/L`, "Dissolved oxygen" |
| TELEMAC max free surface | `continuous_water_surface_elevation` | nothing (`continuous_water_level_m` existed, unused) | continuous, cividis, `m`, "Water surface elevation" |
| coastal peak inundation depth | `continuous_coastal_inundation_depth` | nothing (`continuous_flood_depth` existed, unused) | continuous, ylgnbu, `m`, "Peak inundation depth" |
| TELEMAC-3D stratified field | `continuous_stratified_flow` on the raster AND `continuous_temperature_c` in the packet | nothing / a FIXED 0-40 C band | ONE row, titled with the variable the run actually rasterized (temperature C, velocity m/s or salinity psu) |
| LANDFIRE canopy base height / bulk density / cover / height | `continuous_dem` | grey, `m`, "Elevation" | the continuous bare default; units still ride `units_by_param` |
| USFS canopy fuels | `continuous_dem` | grey, `m`, "Elevation" | the continuous bare default |

Verified programmatically: each of the first six now resolves to a real kind,
ramp, units, title and a non-empty `.qml`.

## Deviations, reported

1. **Two rows were NOT migrated mechanically.** `fetch_usfs_canopy_fuels` and
   LANDFIRE's `cbh`/`cbd`/`cc`/`ch` mapped to `continuous_dem`, whose declared
   row is grey/metres/"Elevation". Migrating that faithfully would have
   published a canopy height labelled as terrain elevation - baking in exactly
   the mislabel class this wave removes. They take the bare continuous default
   instead. Their `units_by_param` rows are untouched.

2. **`gridmet`'s templated `gridmet_{variable}` name** resolved at runtime to
   seven declared contract rows. A template is not a row, so the migration
   expanded it into `style.by_param` over `variable`. Faithful to what the
   mapping said, but it is an expansion rather than a copy.

3. **`geometry` is declared, not read off the data.** A `renderer-v2` symbol
   whose class does not match the layer geometry loads cleanly and then draws
   nothing, so the writer needs to know. The 58 vector geometries were read off
   each spec's own documentation. A row that declares NO geometry gets no
   document at all (QGIS's own default per geometry stands in), so a missing
   declaration degrades to unstyled rather than to blank. `fetch_movebank_tracks`
   declares none deliberately - its geometry is a per-call param.

4. **Per-quantity hues survive only where a declaration carries them.** The
   argued-for colour distinctions in `styles.yaml` (depth ylgnbu vs velocity
   plasma, DO rdylbu vs dye reds) ride the fetcher rows and the product-contract
   rows. A SOLVED output that declares no ramp takes the continuous default over
   the run's own range, so two solved fields on one canvas are told apart by
   title and units until someone restyles one. Stated because it is a real
   change in what a reader sees, and it follows from "presets are keyed by kind,
   never by quantity".

5. **`LegendKey` still stands beside the `.qml`.** The server ships the document
   the map should load, and the plugin still rebuilds an equivalent renderer
   from the key's ramp and range - the same decision made twice. Collapsing it
   is spec section 6's plugin row (`render/layers.py` + `render/temporal.py` +
   `ramps` -> `addMapLayer` + `loadNamedStyle`), not this slice; QUEUED as
   ledger row 6 with that condition.

6. **The SysML extension (spec section 7) is not in this slice.** `model_check`
   is green (0 findings) but no `docs/model/emission-seam.sysml` was authored -
   the spec assigns that to the wave, and the seams it models span sections 4-6.

## Gates

| gate | result |
|---|---|
| suite slice 1 `[a-e]` | 1717 passed, 5 skipped |
| suite slice 2 `[f-o]` | 4222 passed, 1 xfailed |
| suite slice 3 `[p-r]` | 1852 passed, 1 skipped |
| suite slice 4 `[s-z]` | 1534 passed, 6 skipped |
| suite slice 5 contracts | 521 passed |
| `scripts/model_check.py` | 0 findings |
| `scripts/qml_preset_smoke.py` (QGIS 3.40.6) | all_passed |
| daemon restart + `scripts/ws_smoke.py` | all_passed=True |
| `scripts/proof_declared_style_live.py` (real 3DEP + real OSM waterways) | all_passed, both phases |
