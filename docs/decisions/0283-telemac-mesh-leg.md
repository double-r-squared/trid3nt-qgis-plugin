# ADR 0283 -- emit-on-solve: the L-class TELEMAC native-mesh leg

Status: LANDED (offline build half -- schema amendment + seam mesh path + the three
composer producers/forks + the rain_on_grid bespoke-helper deletion + cadence lever
+ the plugin .slf-staging fix, all offline-provable). The live loop (image rebuild +
real solves + the 2-cadence dataset-count proof + the .slf-through-the-dock load)
LANDED as WAVE 4b (EXECUTED 2026-08-17 -- see "4b -- EXECUTED"). Date: 2026-08-17. Builds on ADR 0280 (the seam +
the frozen `outputs.json` schema), ADR 0281 (the S-class composer-fork pattern), and
ADR 0282 (the M-class `frames_only` ruling).

## Context -- NATE RULED FORK A

The recon (`/tasks` stop-clean report) refuted the kickoff's central premise ("no
bespoke mesh animation exists"): `rain_on_grid._publish_full_results_mesh` ALREADY
published `r2d_rog.slf` as a `layer_type="mesh"` `LayerURI` (via
`emission.layer_uri_emit.publish_input_layer`) -- the exact target design, but
hand-wired in the composer (charter law 8 violation). It also surfaced the BLOCKING
fork: `OutputEntry` has NO CRS field, and a SELAFIN mesh sibling carries no CRS of
its own (the plugin's `_add_mesh` sets the mesh CRS only from the row's
`crs_authid`), so routing a mesh through the seam (framework-owned emission)
REQUIRES a schema amendment.

**NATE ruled FORK A** (the kickoff's design): route the mesh via `outputs.json
kind=mesh` + amend the frozen schema with an OPTIONAL `crs_authid` + implement the
seam mesh-kind path + supersede `rain_on_grid._publish_full_results_mesh`.

The three temporal TELEMAC-2D legs are in scope (recon per-leg verdict table):

| Leg | `.slf` uploaded | Prior temporal publication | Verdict this wave |
|---|---|---|---|
| **rain_on_grid** | `r2d_rog.slf` | HAS a bespoke mesh layer (`_publish_full_results_mesh`) | MIGRATE: byte-equivalence on the mesh layer + DELETE the bespoke helper (ledger). |
| **river_dye** | `r2d_river.slf` | peak COG only | ADDITIVE: the mesh layer is new. |
| **coastal_tidal_surge** | `res_coastal.slf` | peak COG only | ADDITIVE: the mesh layer is new. |

telemac3d + the stationary-wave modules (TOMAWAC/ARTEMIS) are correctly OUT (ARTEMIS
is non-temporal = N/A; telemac3d is a later L-class leg).

## Decision -- what LANDED

### 1. Schema amendment (ruled): OPTIONAL `crs_authid`

`OutputEntry` (reader) + `build_entry` (the pure-stdlib contracts writer AND the
verbatim worker mirror `workers/_raster_postprocess/outputs_manifest.py`) gain an
OPTIONAL `crs_authid` (an EPSG authority id string). Omitted (absent, not null) when
unset; tolerant-read both ways. It exists for `kind="mesh"` entries ONLY -- CRS is
per-run (the reach UTM zone), so it cannot live in the `quantity->style` registry;
it rides the entry, exactly as the bespoke composer emit did. This mirrors the
ADR-0280 bbox/band_stats amendment precedent ("authorized with the proving case").
Recorded in `docs/design/outputs-manifest-schema.md` + pinned by
`test_outputs_manifest_schema.py::test_build_entry_carries_optional_crs_authid_on_mesh`.

### 2. Seam mesh path (log-only -> publishing)

`build_layers_from_outputs`' `mesh` branch now builds a `layer_type="mesh"`
`LayerURI` per mesh entry: `uri` = the SELAFIN, `style_preset` via
`resolve_style_preset(quantity)` (a new registry row `model_results -> mesh_grid`),
`role="context"`, `crs_authid` from the entry, `bbox=None` (MDAL derives the extent
-- NOT the composer AOI), deterministic `layer_id = {quantity-base}-mesh-{run_id}`
(idempotent via `observe_published_layer`). `frames_only` generalizes: the mesh IS
the TEMPORAL artifact (MDAL animates every frame from the one file), so it is built
even under `frames_only=True` -- only the standalone peak raster + vectors are
skipped there.

### 3. Producers + composer forks (agent-side)

A shared helper `workflows/telemac/results_mesh_seam.publish_results_mesh_via_seam`
is the producer half: the agent-side postprocess writes `outputs.json` (the peak
entry + the `kind="mesh"` SELAFIN entry, `crs_authid=EPSG:{utm}`) via the host-exec
writer (`outputs_manifest_io.write_outputs_manifest`), then reads it back through the
SEAM (`build_layers_from_outputs(frames_only=True)`) and emits the mesh layer via
`publish_input_layer(role="context")`. The composer keeps its OWN typed peak
(`TelemacDyeLayerURI` / `TelemacCoastalLayerURI` / the WSE `TelemacWseLayerURI`) --
the seam skips the peak entry under `frames_only`, so the same COG is never
registered twice. Best-effort: a write/read/emit miss degrades to peak-only, never
sinks the run. Wired into all three composers (rain_on_grid, river_dye main dye path,
coastal_tidal_surge).

### 4. rain_on_grid migration (byte-equivalence + deletion)

`_publish_full_results_mesh` is DELETED (ledger row). The seam's mesh layer is
byte-equivalent to it field-for-field (name/style/role/crs/uri/bbox); only the
`layer_id` STEM diverges -- the seam mints `model-results-mesh-{run_id}`, the bespoke
helper used `rog-results-{run_id}`. Per the ADR-0281 precedent, `layer_id` is an
idempotence/dedup key and web temporal grouping rides the `name` token
(`detectSequentialGroups`), NOT the layer_id, so the stem swap renders identically.
Captured field-by-field in
`test_outputs_seam.py::test_mesh_entry_publishes_native_mesh_layer`.

Per-leg verdict (coverage law):

| Leg | Migration kind | Bespoke deletion |
|---|---|---|
| rain_on_grid | supersede (byte-equivalent) | `_publish_full_results_mesh` DELETED (ledger) |
| river_dye | additive | none (no prior mesh emit) |
| coastal_tidal_surge | additive | none (no prior mesh emit) |

### 5. Cadence lever: `output_interval_min -> graphic_period`

`output_interval_min` (minutes between GRAPHIC PRINTOUTS, the charter law-8 universal
name) is added as an OPTIONAL tool param on all three composers. `None` = byte-
identical current defaults.

- **river_dye** (DECK-SIDE): threaded into the worker manifest reach dict ONLY when
  set; `ReachConfig` gains an `output_interval_min` field + a `__post_init__` that
  sets `graphic_period = round(output_interval_min*60 / time_step_s)`. Parser bump
  `telemac-reach-9 -> 10`. **INERT until 4b's image rebuild** (the deployed image
  carries parser 9, which hard-errors on the unknown field -- but with the default
  `None` the field is absent, so existing live runs are unaffected).
- **coastal_tidal_surge** (DECK-SIDE): threaded into the manifest coastal dict when
  set; `CoastalConfig` gains the field; the `gp` compute honors it (an explicit
  `graphic_period` still wins; else `output_interval_min`; else the ~40-frame
  default). Parser bump `coastal-tidal-1 -> 2`. **INERT until 4b's rebuild.**
- **rain_on_grid** (AGENT-SIDE): `graphic_period` is computed in the composer (rog's
  `time_step_s` is the composer constant 3.0 s), so `output_interval_min` needs no
  worker change -- `None` keeps the byte-identical default of 200. rog shares the
  telemac-reach parser but adds no field.

The parser bumps are STAGED WITH the worker build changes so 4b's provenance check
has teeth (`test_entrypoint.py` pins `telemac-reach-10`; the coastal build metrics
stamp `coastal-tidal-2`).

### 6. Plugin `.slf`-staging fix

`plugin/render/layers._add_mesh` staged every mesh to a HARDCODED `.nc` filename.
MDAL's driver selection is extension-sensitive, so a SELAFIN staged as `.nc` could be
rejected. FIXED: the staged filename now PRESERVES the source object's extension
(derived from the uri; `.nc` only when the uri has none). Folded under plugin version
0.3.16 (changelog line added; configparser continuation-indentation law respected).
Pinned by `test_raster_render.py::TestMeshStagingExtension` (.slf -> .slf, .nc ->
.nc, extensionless -> .nc).

### 7. Stale-doc cleanup

The dead `_MESH_SIBLING_BY_STYLE_PRESET` symbol (removed system; no definition
anywhere) survived only in stale docstrings -- stripped to present-tense truth in
`postprocess_telemac.py` (module docstring) + `telemac_contracts.py` (6 style-preset
comments). FOLLOW-UP flagged (out of this wave's named scope): residual
`export_case_to_qgis` refs (also an undefined symbol) remain in ~7 other docstrings.

## Consequences

- The three temporal TELEMAC-2D legs publish their result SELAFIN through the
  framework-owned emit-on-solve seam instead of composer-hand-wired emission (law 8).
  rain_on_grid's bespoke `_publish_full_results_mesh` is gone.
- `crs_authid` joins the frozen schema (optional, mesh-only) -- the first non-raster
  render hint the seam threads.
- Zero offline-suite movement expected: baseline EXACTLY 4 fetch_resolution + 2
  river_dye. The worker deck/parser changes are INERT until 4b (offline-green !=
  deploy-green -- the deployed image still runs parser 9 / coastal-tidal-1).

## 4b -- EXECUTED (2026-08-17, the live loop)

Image rebuilt (`scripts/build_telemac_image.sh`, absolute context `workers/telemac`,
3.55 GB). Provenance IN-IMAGE: `entrypoint._PARSER_VERSION == telemac-reach-10`,
`COASTAL_PARSER_VERSION == coastal-tidal-2`, `ReachConfig.output_interval_min` +
the `graphic_period` compute + `CoastalConfig.output_interval_min` all baked.

Three legs solved LIVE through their registered tools (WS `dev-tool-invoke`, the
seed_showcase direct-call path), each emitting the seam mesh layer
(`model-results-mesh-{run_id}`, `mesh_grid`/`context`, crs_authid populated) with
`outputs.json` carrying the `kind="mesh"` entry + the peak raster entry:

| Leg | run_id | mesh crs | frames | seam mesh layer |
|---|---|---|---|---|
| rain_on_grid (cadence A, `output_interval_min=6`) | `01M094YVQMZSH85D5ETMA9744P` | EPSG:32617 | 31 | emitted |
| rain_on_grid (cadence B, `output_interval_min=12`) | `01M0952VY5SXXWEHAKPMS8M761` | EPSG:32617 | 16 | emitted |
| river_dye (`discharge_m3s=250`, `output_interval_min=2`) | `01M0959WWQ1DWBJCKRKJ4PS17E` | EPSG:32611 | 8 | emitted |
| coastal_tidal_surge (Apalachicola, Michael window) | `01M095EXN3JG3PMFCBVQG4AEP1` | EPSG:32616 | 41 | emitted |

**2-cadence proof** (cheapest leg, rain_on_grid, two solves differing ONLY in
`output_interval_min`, same 4986-node mesh): 6 min -> 31 frames, 12 min -> 16
frames. Arithmetic: sim 3 h / dt 3 s = 3600 steps; gp = round(min*60/3): 6->120
(3600/120+1 = 31), 12->240 (3600/240+1 = 16); (31-1)/(16-1) = 2.0 exactly. The
DECK-SIDE lever is separately confirmed live on river_dye: `output_interval_min=2`
was READ by the rebuilt parser-10 (no unknown-field hard-error) and moved the
cadence (reach dt 0.7 s -> gp 171 -> 8 frames).

**Dock-load** (the plugin `.nc`-fix): the real solved `r2d_rog.slf` loads valid
through REAL QGIS/MDAL (QGIS 3.40.6, `QgsMeshLayer(local,name,"mdal")`, 4 dataset
groups) staged under its `.slf` extension -- the fix's intended path works.
HONEST caveat: on MDAL 3.40.6 the SAME bytes staged `.nc` ALSO load (SELAFIN
content-sniffing), so the extension-rejection the fix guards against is NOT
exhibited on this MDAL build; the fix is correct-by-MDAL-driver-selection-spec
(defensive), not demonstrably load-bearing here. Verification path: PyQGIS real
MDAL via the system interpreter (qgis not importable in the agent venv).

**Byte-equivalence** (rain_on_grid): the seam mesh layer name
(`Model results (time series): Otto, North Carolina`) / style (`mesh_grid`) /
role (`context`) / uri (`r2d_rog.slf`) match the deleted `_publish_full_results_mesh`
field-for-field modulo the explained `layer_id` stem; the composer's typed peak is
published once (the seam skips the peak entry under `frames_only`). Ledger row
flipped to DELETED FINAL.

## What 4b must prove (the live loop -- NOT this wave)

1. **Image rebuild** of the telemac worker (absolute `-f`/context paths) so parser
   `telemac-reach-10` + `coastal-tidal-2` + the `output_interval_min` build compute
   are IN the image; provenance-check the new parser stamps in-image.
2. **The results mesh loads through the dock**: a real river_dye / rain_on_grid /
   coastal solve -> `outputs.json` carries the `kind="mesh"` entry (crs_authid set)
   -> the seam publishes the `layer_type="mesh"` layer -> the plugin stages the
   `.slf` under its `.slf` extension and `QgsMeshLayer(...,"mdal")` loads it valid,
   animating the temporal controller (this is what the plugin fix unblocks; the
   recon flagged the `.nc`-staging bug had never been verified live for `.slf`).
3. **The 2-cadence dataset-count proof**: two river_dye (or coastal) solves at two
   different `output_interval_min` values produce SELAFINs with the expected
   different frame/dataset counts (`graphic_period` actually moved the cadence),
   proving the deck-side lever end-to-end through the rebuilt image.
4. **Byte-equivalence live**: for rain_on_grid, the seam mesh layer the plugin
   renders is identical (name/style/role/crs/uri) to what `_publish_full_results_mesh`
   rendered pre-migration -- confirming the deletion changed nothing user-visible.
5. **The flood canary** + `ws_smoke` + daemon restart (already green offline).
