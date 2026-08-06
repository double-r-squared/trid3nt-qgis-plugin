# ADR 0159 - SFINCS native quadtree mesh output + draw-a-geometry gate supply path

Date: 2026-08-06
Status: accepted

## Context

Two NATE-approved product follow-ons, both queued by prior ADRs:

1. **SFINCS quadtree mesh -> QGIS-native mesh layer** (ADR 0152 Row 8, flagged
   HIGH product value for the QGIS-only doctrine). A SFINCS quadtree solve writes
   `sfincs_map.nc` as a face-indexed UGRID mesh (fields on `nmesh2d_face`). The
   postprocess already RASTERIZES those faces to a depth COG
   (`_rasterize_face_field`), and the quadtree deck emits a vector `mesh.geojson`
   preview -- but neither is the real thing. MDAL reads a UGRID netcdf as a
   NATIVE mesh layer (`layer_type="mesh"` + `crs_authid`), the variable-resolution
   grid the user can actually inspect. The plugin's `_add_mesh` already stages +
   loads such a row (built for exactly this SFINCS quadtree case); what was
   missing was the publish-side emission of the netcdf itself as a mesh row.

2. **Draw-a-geometry gate supply path** (ADR 0148 named it "the follow-on";
   IDEAS "Drawn-geometry supply path for gated spatial knobs"). The geoclaw
   `amr_regions` template rides its refinement windows through the ADR 0107
   input-review gate with `basis="prompt_interpreted"` (model-derived) or
   `"user"` (explicit). Until now only the model could propose a window; a user
   had no way to say "refine HERE" by drawing it. The active-canvas AOI already
   travels per-turn as `aoi_bbox`; a drawn sub-region is the same mechanism for a
   different knob.

## Decision

### ITEM 1 - native quadtree mesh deliverable

- `postprocess_sfincs._maybe_native_mesh_layer(netcdf_path, mesh_uri, run_id)`
  probes the run output; when it is a face-indexed quadtree UGRID
  (`_is_quadtree_output`) it returns ONE `LayerURI(layer_type="mesh",
  style_preset="mesh_grid", role="context", crs_authid=<deck UTM>)` whose `uri`
  is the `sfincs_map.nc` itself. `_native_mesh_source_uri` resolves that uri the
  SAME way `_resolve_run_output_to_local` does (s3 prefix -> `/sfincs_map.nc`;
  local drive -> the netcdf path the plugin reads via `os.path.isfile`).
  `postprocess_flood` appends the row AFTER the peak + frame raster layers.
  Regular-grid output is not face-indexed -> nothing appended -> the regular-grid
  publish path is BYTE-IDENTICAL (test-locked). Never raises: a probe/read
  failure degrades to no mesh row (the depth answer stands).
- `model_flood_scenario` (flood.py) pulls `layer_type=="mesh"` rows OUT of the
  frame set and surfaces them through `publish_input_layer(role="context")`
  (MDAL-native, bbox forced None so it never fights the flood camera) -- NOT
  `publish_layer` (a mesh is not a WMS raster). Emitter-only, mirroring the
  animation frames + the SCHISM mesh preview: the wrapper return + envelope
  `result_layers` keep the raster peak-layer shape.
- **No worker-image change.** A quadtree solve ALREADY writes the UGRID
  `sfincs_map.nc` (that is the only output format for a quadtree grid; the
  postprocess UGRID reader was built against the real cht_sfincs schema). No
  `outputformat` authoring was needed, so WORKER-IMAGE LAW does not bite -- the
  vendored quadtree deck builder is untouched.

### ITEM 2 - draw-a-geometry supply path

- **Contract:** `ws.DrawnGeometry` (`geometry_type: Literal["rectangle"]`,
  `bbox: [4 floats EPSG:4326]`, validated) on
  `UserMessagePayload.drawn_geometry` (`None`/absent = byte-identical to the
  pre-field payload). ONE rectangle; the discriminator leaves room for a polygon
  editor (recorded follow-on).
- **Plugin:** a 'Draw region' checkable `QToolButton` installs a
  `QgsMapToolExtent` (the Set-AOI release-point discipline); the dragged rect
  converts to EPSG:4326, is stashed as the pending `drawn_geometry`, painted as a
  solid amber outline (distinct from the dashed-blue AOI overlay), and rides the
  NEXT `send_chat` as `drawn_geometry`. CLEAR-ON-SEND (one rectangle, one turn)
  + cleared on case switch / disconnect so it never rides the wrong case.
- **Server:** `_set_drawn_geometry_from_payload` stores it on
  `SessionState.drawn_geometry` (same key-present set/clear/ignore-malformed
  semantics as `aoi_bbox`); the turn dispatchers bind it into the per-task
  `_TURN_DRAWN_GEOMETRY` ContextVar (`bind_turn_drawn_geometry` /
  `current_turn_drawn_geometry`, mirroring `bind_turn_case`), so composer gates
  read it WITHOUT a new kwarg threaded down every dispatch path.
- **Gate:** `geoclaw_amr_refinement_regions` reads
  `current_turn_drawn_geometry()`; when present it REPLACES the model's
  `amr_regions` with ONE finest-level window over the drawn bbox for the whole
  sim and forces `window_basis="user"`. The window then rides the ADR 0107 gate
  as an explicit user choice. Absent a drawn region the model proposal +
  `window_basis` flow through unchanged.

## Consequences

- A quadtree flood run now surfaces the real variable-resolution mesh in QGIS
  alongside the depth raster; a regular-grid run is unchanged (no mesh row).
- The draw path is minimal: one rectangle, one gate (geoclaw amr_regions). Other
  gated spatial knobs can adopt `current_turn_drawn_geometry()` the same way.
- No new registered tools; registry + template pins untouched. Contract gains
  `DrawnGeometry` + the `drawn_geometry` field.
- Plugin changes are dev-symlinked: NATE reloads via QGIS > Plugins > Plugin
  Reloader (no sync needed) to see the 'Draw region' button.

## Evidence

- Offline (from repo root, `env -u TRID3NT_CACHE_BUCKET pytest -p
  no:cacheprovider --timeout=300 -q`):
  - `contracts/tests/test_ws.py` (DrawnGeometry round-trip + validators): 66
    passed.
  - `server/tests/test_postprocess_flood_quadtree.py` (native mesh layer helper +
    regular-grid None + unreadable None), `test_geoclaw_amr_regions_gate.py`
    (drawn-geometry OVERRIDES model proposal -> basis=user, one window;
    no-drawn keeps the model proposal), `test_aoi_autofill_adr0017.py`
    (`_set_drawn_geometry_from_payload` set/clear/ignore-malformed),
    `test_ws_bridge_signal_signatures.py`: all passed.
  - Regression: `test_postprocess_flood`, `test_model_flood_scenario`(_v2/
    _coastal), flood animation/frame/duplicate/single-styled, sfincs archetype
    decks, sfincs numerical physics, template hygiene: 151 passed. Geoclaw /
    emission / aoi sweep: 390 passed.
- Plugin: `python -m py_compile` clean on dock.py / ws_bridge.py /
  trid3nt_client.py; `scripts/install_plugin.sh --check` = dev symlink active,
  no sync needed.
- Publish-path smoke (the live cht_sfincs SOLVE is NOT runnable here: only the
  stock `deltares/sfincs-cpu` solver image is present, not the cht_sfincs
  deck-builder worker image, and no MinIO runs bucket is configured). Ran the
  REAL publish code end-to-end against a variable-resolution quadtree UGRID
  `sfincs_map.nc` in the cht schema (mesh2d_node_x/_y + mesh2d_face_nodes 1-based
  + crs var value = EPSG int): `_native_mesh_source_uri` + `_maybe_native_mesh_layer`
  -> `LayerURI(layer_type="mesh", crs_authid="EPSG:32616", role="context",
  style="mesh_grid", name="SFINCS quadtree mesh (139 cells)")`; UGRID validated
  by xarray (556 nodes, 139 faces, 3 distinct cell sizes 209/417/835 m ->
  variable resolution); `_extract_peak_depth_geotiff` rasterized the faces to a
  valid EPSG:32616 depth COG (max 3.14 m, 22115 wet cells). The QGIS/MDAL
  native-mesh VISUAL load + a through-solver quadtree run are NATE's step (need
  the cht worker image + MinIO). Proofs (docs/proof/templates/):
  `sfincs_native_quadtree_mesh_mesh.png` (raw quadtree grid over Esri, density =
  refinement) + `sfincs_native_quadtree_mesh_depth.png` (rasterized peak depth).
