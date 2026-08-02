# 0068 - raster stragglers: the SLR MapServer-export pair folds; the rest STOP with named gaps

Context: the raster-family deferrals accumulated across ADR 0047 (wave-5) and ADR
0059 (wave-11) were re-attempted against the full phase + mode inventory the
intervening waves landed (pre_resolve, next_page, PHASE E, fan_out, multi_url VRT
fan-out, gzip_object, get_zip / fixed_tile_grid, imageserver_export, stac_float,
serialize dtype/nodata directives, tolerate_page_error). Waves 13-17 (ADR
0063-0067) overturned most prior deferrals via composition, so every target was
re-read IN FULL before a verdict, not trusting the prior label. Six targets:
fetch_noaa_slr_confidence + fetch_noaa_slr_marsh, fetch_landcover, fetch_topobathy,
fetch_soilgrids, fetch_noaa_sst, fetch_3dep_extra.

Decision (2026-08-01):

1. **The NOAA SLR raster PAIR FOLDED via ONE new minimal access mode
   (`mapserver_export`) + a strictly-no-op serializer branch.** The two siblings
   share a single data path: an ArcGIS `MapServer/export` returning a
   SERVER-SYMBOLIZED PNG32 (a baked color scheme, not raw values), georeferenced
   client-side into a 4-band RGBA COG so `publish_layer` renders the baked
   symbology directly. The gap (ADR 0059) was that no mode fetched a rendered PNG
   and `array_to_cog_bytes` had no RGBA ColorInterp path.
   - **`mapserver_export` access mode (raster_cog).** Resolves the service name
     from a request param via a declarative `service_by_param` level map (slr_ft ->
     conf_Nft / marsh_NNN, the twin's formula expressed as a static map: 11 conf
     levels, 21 marsh levels), builds `{base}/{service}/MapServer/export`, sizes the
     export from a `res_deg` cell grid (the twin's `grid_size`, clamped 16..2048),
     fetches the PNG through the SHARED transport `get_bytes` (ADR-0044 owns the
     socket), PIL-decodes to RGBA, and georeferences with the request-bbox
     transform. An out-of-set level is a typed INPUT error
     (NOAA_SLR_RASTER_INPUT_INVALID, the twin's per-level validation); an
     undecodable body / HTTP failure is typed UPSTREAM. NO nodata coverage gate: a
     fully-transparent export (no coverage at that level) is a VALID transparent
     overlay, never a typed EMPTY (the twin's honesty floor -- the layer appears
     and renders nothing).
   - **`array_to_cog_bytes` RGBA branch (strictly no-op for all 47 priors).** Two
     additive parameters: `colorinterp="rgba"` tags a 4-band uint8 array
     red/green/blue/alpha; `nodata=None` omits the nodata tag (an RGBA overlay
     carries transparency in the alpha band, not a sentinel). Both default to the
     single-band float32 / nan-nodata behaviour -- proven no-op by the full offline
     suite (incl. test_router_executors) plus an explicit singleband-unchanged test.
   - **Live edge-matrix parity PASS vs twin (8/8).** The twin's shared
     `_noaa_slr_raster.py` export path was restored from git and run against the
     router mode over real NOAA data: conf_3ft / conf_6ft / marsh_300 / marsh_150
     all 4-band RGBA VALUE-IDENTICAL (array, crs, transform, dtype equal reading
     both COGs back); invalid levels (conf 3.5 / conf 11.0 / marsh 0.25) ->
     NOAA_SLR_RASTER_INPUT_INVALID retryable=False identical; transparent-still-valid
     (a synthetic all-transparent PNG through both paths -> a valid 4-band COG, no
     raise). Twins + shared module + `test_fetch_noaa_slr_siblings.py` DELETED;
     value-bearing coverage migrated to `test_router_mapserver_export.py` (17 tests:
     registration/category/corpus, the array_to_cog_bytes RGBA branch + singleband
     no-op, mode PNG->RGBA COG, transparent-still-valid, undecodable/HTTP upstream,
     service-map resolution + export query, invalid level / non-positive res_deg
     INPUT_INVALID, res_deg grid control). Docstrings carried VERBATIM + the sibling
     corpus.yaml files untouched, so the retrieval index is UNSHIFTED: 12/12 corpus
     phrasings rank the tool in the model-free top-8.

2. **The other four STOP with EVIDENCED, NAMED gaps (fold-fewer-fully beats
   force-fitting).** Each was read in full against the current mode inventory; none
   reduces to a no-op extension.
   - **fetch_landcover -- DEFER.** Needs (a) a DICT-return output contract carrying
     SFINCS-consumed sidecar fields (`nlcd_vintage_year`, `effective_resolution_m`,
     `downsampled`) -- `set_sfincs_parameters` validates the Manning's mapping CSV
     against the vintage before HydroMT (Invariant 7); the router emits ONLY a
     LayerURI, there is NO dict path; (b) an unbuilt WCS 1.0.0 GetCoverage access
     mode (via `ogc_adapter.fetch_ogc_layer`, NOT the router transport); (c) a NLCD
     background(0)->nodata PIXEL remap + a palette-preserving two-path
     COG-with-overviews translate; (d) auto-coarsen effective-resolution /
     pixel-budget logic + a landcover-only cache-version salt. The dict-return is
     the structural killer (consumers depend on it); the WCS mode + categorical
     remap are genuine new machinery. Superseded prior: ADR 0059.
   - **fetch_topobathy -- DEFER (heaviest).** A 4-source composite (CUDEM manifest +
     ETOPO formula-grid + NCEI-regional STAC + 3DEP-via-fetch_dem) each reprojected
     into a shared NON-4326 UTM target grid + a vertical-datum gate + an extended
     `TopobathyResult(LayerURI)` result type. DO-NOT-REGRESS flood leg:
     `sfincs/flood/flood.py` imports `fetch_topobathy` + `TopobathyError` DIRECTLY,
     so a folded surface would need a flood-consumer re-point + FLOOD CANARY on top
     of the multi-source composite + UTM-output + datum-gate + extended-result gaps.
     Superseded prior: ADR 0059.
   - **fetch_soilgrids -- DEFER (re-examined vs multi_url per the wave-9 stop).** The
     `multi_url` VRT executor is a SAME-CRS mosaic paster (its output crs = the
     mosaic crs; NO reproject step) -- soilgrids opens a Homolosine VRT and needs a
     PROJECTED-WINDOW branch (transform_bounds 4326->Homolosine densified, native
     window read, `rasterio.warp.reproject` -> EPSG:4326 bilinear) PLUS a
     per-property Int16->physical scale-divisor directive. The `serialize` block
     adds a nodata sentinel but neither a reproject nor a per-param scale. Real new
     machinery; the wave-9 stop holds. Superseded prior: ADR 0047/0055.
   - **fetch_3dep_extra -- STOP / KEEP.** Access is an OPAQUE
     `pfdf.data.usgs.tnm.dem.read` library call that owns its own tile discovery +
     mosaic + socket (violates the transport-owns-the-socket invariant, ADR 0044).
     A fold needs a GENERIC library-delegate executor (the `dataretrieval_delegate`
     precedent, but generalized from the hardwired `dataretrieval` package to an
     arbitrary `module.callable` with arg-mapping) + substring-based error
     classification + a per-resolution payload-coefficient table
     `PayloadEstimateSpec` cannot hold. Not a no-op extension. Superseded prior:
     ADR 0059.

3. **fetch_noaa_sst is HELD / BLOCKED-ON-UPSTREAM (reachability RETESTED).** A plain
   probe: the ERDDAP base `coastwatch.pfeg.noaa.gov/erddap` is HTTP 200 (server up)
   but the twin's dataset `NOAA_DHW` is HTTP 404 (both `.das` and `info/.../index.json`
   -- moved/retired). The griddap bracket-selector fold is `build_request` templating
   + a parse hook (structurally trivial), BUT live byte-parity cannot be proven
   against a dead dataset and the twin itself is broken. HELD stays until the dataset
   id is re-resolved (a separate upstream-tracking task, not a mode gap).

4. **Metrics.** Coded tools -2; coded fetchers -2 (two SLR twins died, two spec-driven
   surfaces took their names). Spec-served data sources 47 -> 49 (+2). Registry total
   UNCHANGED at 190. Twin py + shared-module + test LOC removed = 5,098 + 9,696
   (shared) ... (fetch_noaa_slr_confidence 4,988B + fetch_noaa_slr_marsh 5,112B +
   _noaa_slr_raster 9,696B twins; test_fetch_noaa_slr_siblings.py ~247 lines);
   value-bearing coverage migrated to `test_router_mapserver_export.py` (17 tests).
   `test_catalog_surfacing`: n_specs 47 -> 49, the arm2/arm3 declarable delta -46 ->
   -48, the stratum tool count 46 -> 48 (the expected metric, not a regression).
   Offline suite FAILED set == 9 EXACTLY (the pre-existing test_fetch_resolution_gate
   x4 + test_run_river_dye_scenario x5; no new failure), run in four foreground
   quarters. Daemon import clean; both spec-served + registry-resolvable.

5. **NO consumer re-point + NO flood canary required.** grep-verified: neither SLR
   twin feeds any sfincs/flood/nested-workflow seam -- the only references are
   docstring cross-references, the categories map (name-keyed, unchanged), a
   scenario_reuse name->class map (name-keyed, unchanged), and a doc example in
   `_example_tool_template.py` (re-pointed to `fetch_dem`). The touched
   `raster_cog.py` changes are additive-only (new access string + new default-off
   serializer args), no flood consumer resolves through them, and the full offline
   suite is green -- so no flood canary was run this wave (the ADR 0065/0067
   no-flood-coupling precedent).

Non-gating divergences flagged (REPORTED, never fudged):
(a) **LayerURI cosmetics.** The router synthesizes `layer_id` / `name` from
    `source_class` where the twins hand-built level-labelled strings ("NOAA SLR
    Mapping Confidence (3 ft)"); the layer DATA (the RGBA COG) is value-identical,
    and `output.emit_bbox: true` reproduces the twin's request-bbox stamp. Unchanged
    from every prior fold wave.
(b) **res_deg declared default.** The twin signature is `res_deg: float | None = None`
    (resolved to 0.0005 internally); the spec declares `default: 0.0005` +
    `schema_optional: true` (optional in schema, not required), so the model sees a
    concrete default. Functionally identical (both resolve to 0.0005); the res_deg
    param is model-overridable and its grid effect is preserved.
(c) **Out-of-set level error PATH.** The twin validates slr_ft against a frozenset
    BEFORE any network call; the router validates via the `service_by_param` map miss
    in the mode body (still pre-network, before the fetch). Both stamp
    NOAA_SLR_RASTER_INPUT_INVALID retryable=False -- byte-identical error surface.
(d) **Synthesized payload estimator.** The twin's estimate is grid-size-driven
    (w*h*4*0.25/1e6 clamped [0.1, 60]); the router synthesizes a `bbox_area` model
    (mb_per_sq_deg=8, floor 0.1, ceil 60). Both stay well under the 25 MB warn for
    every realistic coastal-AOI query; same class as every prior fold's estimator
    divergence.
(e) **COG internal blocksize.** The twin's COG driver used its default blocksize;
    the shared `array_to_cog_bytes` writes blocksize=256. An internal COG-layout
    detail only -- the pixel values / bands / crs / transform read back
    value-identical (the ADR 0053/0054 parity standard), proven by the 8/8 harness.

Consequence: the raster stragglers resolve to ONE genuine fold (the SLR
MapServer-export PAIR, a new minimal `mapserver_export` RGBA mode + a no-op
serializer branch, one mode collapsing the two siblings) and four honestly
STOP-ruled / HELD residues, each with a named next-mode gap (dict-output + WCS,
multi-source UTM composite, projected-window reproject, generic library-delegate)
or a blocked-on-upstream dataset (noaa_sst). The "coded fetcher count -> zero"
endgame advances by two. Supersedes the ADR 0059 DEFER verdicts for
fetch_noaa_slr_confidence / fetch_noaa_slr_marsh; restates (with fresh evidence
against the current inventory) the DEFER verdicts for landcover / topobathy /
soilgrids / 3dep_extra and the HELD verdict for noaa_sst; extends the raster
executor with one server-rendered-image access mode.
