# 0327 - one store, one scheme: a layer reference is an s3:// uri

## What changed

A layer has ONE uri. Products write once into the existing MinIO bucket family,
the reference the map carries is `s3://bucket/key`, and QGIS opens it natively
through GDAL `/vsis3`. The endpoint and credentials are process-wide GDAL
configuration applied once per profile (`render.layers.configure_store_access`,
called at dock construction and after each connect), so pointing at a store on
another host is an endpoint VALUE, not a second code path.

What that removed, in the order it used to run:

* **the publish lifecycle.** `publish_layer` minted or wrote a second face for
  most layers - a browser-readable GeoJSON twin per vector, a QGIS-Server WMS
  GetMap URL behind a never-set env var, a `.qgs` project uri to key it by.
  It is now three steps in one function: **write** (enforce COG overviews,
  writing the tiled+overview sibling into the same bucket when the source has
  none), **register** (bind the handle to that one uri), **notify** (resolve the
  declared style row once and stash the legend the envelope carries).
* **the registry's translation layers.** Half of `uri_registry` mapped a display
  URL back to the data uri it wrapped, and accepted a decommissioned cloud's
  `gs://` scheme alongside `s3://`. A record is now one id bound to one uri.
* **the plugin's streaming client.** It translated every `s3://` uri into a
  MinIO http address and handed GDAL a `/vsicurl/` string, because the buckets
  were anonymous. `s3_to_vsis3` is the whole translation now, and it carries no
  host at all.
* **the anonymous-download bucket policy.** Both buckets are private. A read is
  signed, and an unsigned GET of a published COG returns 403.
* **the local/remote duality.** There is no branch anywhere that asks whether
  the store is local. Rasters and vectors stream ranged; the mesh leg is the
  same scheme plus one measured cache hop, because MDAL has no `/vsi` layer.

## Why

The duality was the cost. Every layer existed twice - once as the object that
holds the numbers and once as the address something could render - and every
seam between the producer and the canvas had to know which face it was holding.
That is where the failure classes lived: a display URL handed to an analytical
tool, a legend stashed under the wrong key, a re-publish appearing as a second
row because two tile templates of one COG differed in their query strings, a
cold case painting rasters but not roads because only one face had been
materialized.

None of those are bugs to fix. They are consequences of a layer having two
names, and they end when it has one.

The second cost was the shortcut that made the first face reachable. `/vsicurl`
needs no credentials, which is exactly why the buckets had to be world-readable
on the trust boundary. Signing the read is not extra machinery - it is GDAL's,
configured once - and it lets the store be private, which it should have been.

## The legacy templates in the live store

199 uris in `projects.json` and 8 in `sessions.json` still carried the
`/cog/tiles/...?url=<encoded s3 uri>` display shape from before QGIS-native
rendering. They were MIGRATED once
(`scripts/migrate_legacy_tile_templates.py --apply`, backups written alongside)
rather than given a lazy upgrade path, because a lazy path is the unwrap code
kept alive forever to serve a set that only shrinks: it would have preserved
`_unwrap_legacy_template` in the plugin, `_unwrap_tile_template` in the tools,
the titiler branches in the registry and the republish branch in `publish_layer`
- roughly the whole second-face surface this change exists to delete.

The rewrite renames a reference; it does not change which bytes a layer is (the
embedded `url=` param IS the object the template always pointed at).
`case_chat_messages.json` (131 occurrences) was deliberately NOT touched: it is
the record of what was said in a turn, not state a reader resolves, and editing
an assistant's own words to make them tidy is a lie about the transcript. A
legacy template surviving there resolves to nothing and fails honestly.

## Proof

`plugin/tests/headless_store_reads_proof.py` drives the real
`LayerMaterializer` under a real `QgsApplication` against a real case's
persisted `loaded_layer_summaries` - a TELEMAC river-dye canary carrying raster,
vector and mesh rows - and checks: every reference is `s3://`; all seven rows
reach the canvas; rasters and vectors stage nothing; the mesh takes the one
cache hop with its cost measured (0.2 MB in 0.00 s for `r2d_river.slf`); and an
unsigned GET of a published COG returns 403 while the signed read of the same
object succeeds.

## Deviations, reported rather than taken

* **The registry did not collapse as far as the spec's row estimates.** The
  ledger row reads "uri_registry translation layers (~1,200 -> thin id-to-URI
  record)". What landed is 1,205 -> 1,035. Everything that TRANSLATES is gone;
  what survives is the layer-handle indirection - the `L<n>` mint the model is
  shown, the fuzzy mangle-match, the placeholder resolution - which is id
  RESOLUTION rather than scheme translation, and cutting it would revive the
  URI-hallucination class the module exists to prevent. Surfaced here rather
  than decided alone.
* **`LoadLayerArgs.wms_url` and `ReferenceLayer.wms_url` still stand** in the
  ws contract. Neither shape is ever emitted (only `zoom-to` map-commands are),
  so they are dead contract classes rather than residue of this change; culling
  them is a map-command sweep, not a transport one.
* **`show_nexrad_radar` returns a raster LayerURI whose uri is an external WMS
  GetMap URL.** The plugin skipped it before this change (no `{z}` placeholder,
  so it never reached the XYZ branch) and skips it now with an honest note. The
  tool is broken at the canvas either way; naming its fix is a display decision,
  not a transport one.
