# Conformance: emission-fold rev 1, section 4 (one store, one scheme)

Clause by clause against `docs/specs/emission-fold.html` rev 1 as amended, and
against the eight items the slice was handed. Deviations are REPORTED, not
auto-fixed.

## Section 4 - the transport

| clause | verdict | evidence |
|---|---|---|
| GDAL PAM is DISABLED in the plugin profile - a read must never mutate the store | LANDED | `configure_store_access` sets `GDAL_PAM_ENABLED=NO`; asserted live in `qt_remote_endpoints_harness.py` |
| the vsicurl-era anonymous-download bucket policy retires; signed `/vsis3` is the proven path, 403 control included | LANDED, PROVEN LIVE | `init_minio.sh` now runs `mc anonymous set none` on both buckets; `mc anonymous get` reports `private`; an unsigned GET of a published COG returns **403** while the signed `/vsis3` open of the same object succeeds (`headless_store_reads_proof.py` step 4) |
| products write ONCE into the EXISTING MinIO store; the staging and product stores unify into one bucket family | HELD (already true) | `trid3nt-runs` + `trid3nt-cache` are one MinIO family on one endpoint; nothing wrote a second copy of a raster. What DID write a second copy of a VECTOR - the durable browser-readable GeoJSON twin at `case-data/<case>/<layer>.geojson` - is deleted, so a vector is now written once too |
| QGIS reads `s3://` URIs NATIVELY via GDAL `/vsis3` | LANDED | `s3_to_vsis3("s3://b/k") -> "/vsis3/b/k"`; `QgsRasterLayer(path, name, "gdal")` / `QgsVectorLayer(path, name, "ogr")`; seven real rows of a template run load through it |
| endpoint + credentials configured once per profile, set by the plugin | LANDED | `render.layers.configure_store_access(endpoint, key, secret, region)` called at dock construction and again on each connect; credentials are `PluginSettings.store_access_key` / `store_secret_key` / `store_region`, defaulting to the bundled stack's |
| locally the endpoint is localhost; remotely it is the same URI at another address | LANDED | `qt_remote_endpoints_harness.py` drives the real dock: an advertised `http://100.64.0.5:9000` lands as `AWS_S3_ENDPOINT=100.64.0.5:9000`, and no advertisement falls back to `127.0.0.1:9000` - the layer uri is byte-identical in both |
| remote parity is an ENDPOINT SETTING, never a code path | LANDED | see the duality grep below |

### The dies-table

| row | verdict | measured |
|---|---|---|
| publish lifecycle / presign / session-TTL (~1,800) -> bucket credentials, once | LANDED (partly pre-existing) | `publish.py` 1,523 -> **1,105** (-418): the durable-GeoJSON writer, the QGIS-Server WMS seam, the `.qgs` project-uri machinery and the legacy-template republish branch all gone. `presign`: zero live spellings in code before this slice and after (the class died with the browser viewer; only three UI comments named it, now reworded). Session-TTL staging SURVIVES by the spec's own mesh row - see the deviation below |
| uri_registry translation layers (~1,200 -> thin id-to-URI record) | PARTLY LANDED | 1,205 -> **1,035** (-170). Everything that TRANSLATES is gone; the handle indirection that survives is id resolution, not scheme translation - REPORTED below |
| the plugin's streaming client + vsicurl wrapping -> GDAL native | LANDED | `s3_to_http`, `data_base_override`, `_effective_data_base`, the urllib staging download, `_unwrap_legacy_template`, the XYZ raster branch, `LayerEvent.wms_url` / `.tile_template`: zero live spellings. `layers.py` 1,348 -> **1,259**, `trid3nt_client.py` 2,201 -> **2,188** |
| the local/remote duality -> never exists for rasters and vectors | LANDED | grep proof below |
| the mesh leg is one scheme PLUS a measured cache hop | LANDED, COST STATED | `_stage_s3_to_session` copies through the SAME `/vsis3` GDAL uses for everything else (`VSIFOpenL` + chunked read), so there is no second credential path. Measured on the canary's `r2d_river.slf`: 0.2 MB in 0.00 s |
| stream timeouts / stale presigns / TTL-expiry failures -> the class ceases | LANDED | there is no presign to go stale and no TTL on a read; the only timeout left was the urllib staging download, which is deleted |
| the plugin-era "streaming IS the path" ruling lands vindicated | LANDED | implemented by GDAL + MinIO. Ledger row 3 states it |

Wave total across the six commits: **1,358 insertions, 2,760 deletions** over 61
files - net **-1,402**, of which the two named files carry -588.

### The local/remote duality: the grep

```
$ grep -rn "vsicurl" plugin/ trid3nt_server/emission/     -> (nothing)
$ grep -rn "s3_to_http\|data_base_override" --include=*.py -> (nothing)
```

`plugin/render/layers.py` holds no branch that asks where the store is. Both
`_add_raster` and `_add_vector` are: `s3_to_vsis3(uri)` -> `None` is an honest
skip, else construct the layer.

Two local-path arms survive, and NEITHER is a remote mode - stated rather than
swept under the claim:

* `_add_mesh` accepts an already-local mesh path (`os.path.isfile(uri)`) for the
  headless/scripted drive;
* `publish._read_raster_bytes` / `_write_overview_cog` read and write a local
  file when handed one, which is how the overview-enforcement tests push real
  rasterio bytes through the real translate path.

Both are test-drive affordances on a path `publish_layer` itself refuses
(`LAYER_URI_NOT_FOUND` fires on any non-`s3://` raster before either runs), not
a second transport a product layer can travel.

### The 199 legacy `/cog/tiles` uris: MIGRATED, and why

`scripts/migrate_legacy_tile_templates.py --apply` rewrote **199** uris in
`projects.json` and **8** in `sessions.json` to the `s3://` object each template
already embedded, backing up both files alongside. Verified: `grep -c cog/tiles`
returns 0 on both, and a spot-checked row reads
`s3://trid3nt-runs/01KWT7BTW00N4EKHM6MB0HH5C8/swmm_depth_frame_01.tif`.

Migration rather than a lazy upgrade path, because a lazy path is the unwrap
code kept alive forever to serve a set that only shrinks: it would have
preserved `_unwrap_legacy_template` (plugin), `_unwrap_tile_template` (tools),
the titiler branches in the registry, the republish branch in `publish_layer`
and the `compute_layer_bounds` fallback - roughly the whole second-face surface
this slice exists to delete. One rewrite of a reference to the same bytes buys
all five deletions.

`case_chat_messages.json` (131 occurrences) was deliberately NOT migrated: it is
the record of what was said in a turn, not state a reader resolves. A legacy
template surviving there resolves to nothing and fails honestly.

## Headless proof

`plugin/tests/headless_store_reads_proof.py` (replacing
`headless_remote_streaming_proof.py`) drives the real `LayerMaterializer` under
a real `QgsApplication` against a real case's persisted
`loaded_layer_summaries` - the TELEMAC river-dye canary
`01M1N32ZK0ZBJH4C4QPS9MBQ6Z`, which carries raster, vector AND mesh rows.
**0 failed checks:**

* all 7 references are `s3://`;
* all 7 rows reach the canvas (0.1 s);
* every raster/vector note reads `streamed via /vsis3 (no local copy)`, and the
  staging dir gains exactly one file - the mesh;
* the cache hop is measured;
* the unsigned GET is 403;
* `cleanup_session` removes the staging dir.

## Live gates

| gate | result |
|---|---|
| daemon restart + `scripts/ws_smoke.py` | `all_passed=True` |
| `scripts/proof_auto_emit_seam.py` (fetch_dem then compute_hillshade through the emitter seam, real 3DEP terrain) | OK - two publishes unasked, both rasters on the map as `s3://` uris with their legends, no `publish_layer` in the registry to have called |
| `plugin/tests/headless_store_reads_proof.py` | 0 failed checks (above) |
| GDAL `/vsis3` open of a live published COG | `801 x 617`, 1 band, 1 overview level |
| unsigned HTTP GET of the same object | 403 |

`proof_auto_emit_seam` also carried a defect this slice fixed rather than
stepped around: its last assertion read `has_legend` off the RAW layer row, a
key only the printed report carries, so it failed on a run whose every layer HAD
a legend. It reads `legend` now.

## Suite

| slice | result |
|---|---|
| `tests/test_[a-e]*` | 1713 passed, 5 skipped |
| `tests/test_[f-o]*` | 4220 passed, 1 xfailed |
| `tests/test_[p-r]*` | 1817 passed, 1 skipped |
| `tests/test_[s-z]*` | 1526 passed, 6 skipped |
| `contracts/tests` | 521 passed |
| `plugin/` (its own lane) | 412 run, 2 failed - `test_case_bbox` + `test_tool_picker`, the documented pre-existing pair |

Zero failures in every slice. The final gate was re-run with the daemon UP:
`test_live_run_harness` needs it, and an earlier pass showed its two tests red
purely because the migration had required stopping the stack.

Counts fall where test files died with their subjects:
`test_publish_layer_durable_vector_geojson_165p0.py` (whole file - the durable
GeoJSON twin is gone), the WMS/`.qgs` half of the vector-and-overviews file, the
three legacy-unwrap pins in the envelope file, the two display-face pins in
`test_layer_handles_adr0014.py`, and `TestTitilerTemplateRecovery` +
`test_i4_wms_url_as_hazard` in `test_uri_registry.py`, and the tile-template
recovery pin in `test_compute_layer_bounds.py`.

## Deviations, reported rather than taken

1. **The registry did not reach "thin id-to-URI record".** 1,205 -> 1,035, not
   ~1,200 -> thin. Everything that translates between faces or schemes is gone.
   What survives is the layer-handle indirection: the `L<n>` mint the model is
   shown, `rewrite_result_for_llm`, the fuzzy mangle-match and the placeholder
   resolution. That is id RESOLUTION rather than scheme translation, and cutting
   it would revive the URI-hallucination class the module exists to prevent, so
   it is surfaced rather than decided alone. Registered in `docs/REANALYZE_LEDGER.md`
   with the three triggers that would retire it.

2. **Session-scoped staging survives, and the spec asks for both.** The dies-row
   names "publish lifecycle / presign / session-TTL (~1,800)", but the mesh row
   two lines down keeps "one scheme PLUS a measured cache hop". Those are only
   consistent if session-TTL means presigned-URL expiry (dead) rather than the
   staging dir (kept, and required by MDAL). Read that way and executed that
   way; flagged because the row's wording reads the other way at a glance.

3. **`_layer_uri_is_published` had to change meaning, not just prose.** It
   answered "is this LayerURI already on the map?" with "does its uri start with
   http" - true only while the on-map face was a tile/WMS URL. With one scheme it
   would have said NO for every published layer and invited the model to
   re-publish. It now keys on `s3://`. A behavior change inside the slice's
   blast radius, called out because it is the model-facing half.

4. **`LoadLayerArgs.wms_url` and `ReferenceLayer.wms_url` still stand** in the
   ws contract. Neither shape is ever emitted (only `zoom-to` map-commands are),
   so they are dead contract classes rather than residue of this change; culling
   them is a map-command sweep, not a transport one.

5. **`show_nexrad_radar` returns a raster whose uri is an external WMS GetMap
   URL.** The plugin skipped it before this change (no `{z}`, so it never reached
   the XYZ branch) and skips it now with an honest note. Broken at the canvas
   either way; naming its fix is a display decision, not a transport one.

6. **Store credentials ship as plugin-settings defaults** (`trid3nt` /
   `trid3nt-local-dev`, the values `scripts/start_minio.sh` provisions). The
   spec says "credentials configured once per profile, set by the plugin" and
   the ack's wire-isolation invariant forbids sending them over the handshake,
   so the profile is the only place left. There is no Settings-dialog field -
   consistent with `minio_endpoint`, and the UI pass is NATE's.
