# Lean sweep inventory

Synthesis of five measured lenses (ledger, test-only, web-era, library, processing) over
`/home/nate/Documents/trid3nt-local` at git HEAD `f8e5266` (2026-09-07,
"plugin: 0.3.19"). Read-only inventory. Every LOC is `wc -l` or an explicit line range;
every "no importer" claim is `grep` plus a loader-path check (`source.yaml` specs,
`@register_tool`, `_INCONTAINER_SCRIPT` drivers, `tool()` workflow rows) before anything
is called an orphan. Nothing here is committed and nothing is deleted; this is the map.

Baselines (`wc -l` on `*.py`, excluding `tests/` and `venvs/`):

| surface | LOC |
|---|---:|
| `trid3nt_server/` | 141,637 |
| `plugin/` | 29,692 |
| `contracts/` | 17,589 |
| `scripts/` | 15,992 |
| `workers/` | 988 |
| **product total** | **205,898** |

---

## 1. Headline: candidates by fate

Deltas are product `.py` LOC. Test files that ride along are listed in section 2 and are
NOT counted in these totals. The "projected server LOC" column is cumulative on
`trid3nt_server/` only, in the order the fates are listed.

| fate | items | server LOC | contracts LOC | scripts LOC | plugin LOC | total delta | projected server LOC |
|---|---:|---:|---:|---:|---:|---:|---:|
| baseline | | | | | | | **141,637** |
| **DELETE now** | 10 | -2,532 | -1,354 | -141 | 0 | **-4,027** | **139,105** |
| **ATTIC** | 9 | -1,128 | -373 | -3,175 | -105 | **-4,781** | **137,977** |
| **REPLACE WITH LIBRARY** | 14 | -1,645 | 0 | 0 | -350 | **-1,995** | **136,332** |
| **DECLARATIVE SPEC** | 16 tools + 19 hoists | -8,675 (+~2,200 yaml) | 0 | 0 | 0 | **-8,675** | **127,657** |
| **KEEP** | everything else | | | | | | |
| **after all four** | | | | | | **-19,478** | **127,657 py (+~2,200 yaml)** |

Product total after all four fates: **186,420 py (+~2,200 yaml)**, from 205,898.
That is a 9.5% product cut, of which **45% of it is one subsystem** (`tools/processing/`,
23,551 -> ~12,900 py + ~2,200 yaml).

Honest caveats on the projection:
- The DECLARATIVE SPEC row is the only one that is not a straight subtraction: it deletes
  7,631 LOC of tool code and 2,084 LOC of boilerplate from the 19 survivors, and adds back
  ~850 LOC of `processing/_ops/` engine and ~190 LOC of carve-out hooks. `-8,675` is net.
- The REPLACE WITH LIBRARY row's own source table sums to **1,995**, not the "~1,570" its
  lens summary states; the lens's stated total does not reconcile with its own column. The
  summed value is used here.
- Two rows in DELETE now (`compute_model_residuals`, `combine`) are folds/conditionals,
  not pure staleness, and appear as design questions 4 and 5.

---

## 2. DELETE now, ordered by LOC

Deletion posture: `workflows/` rows need parity plus a condition; everything else needs
staleness evidence only (AGGRESSIVE). **No row below is in `workflows/`.**

| # | target | LOC | staleness evidence (one line) |
|---|---|---:|---|
| D1 | 12 dead engine chart builders + 4 dead helpers in `trid3nt_server/tools/processing/charts_common.py` (`:391-473, :474-532, :533-611, :687-739, :740-795, :796-875, :876-935, :936-1007, :1008-1080, :1081-1240, :1241-1304, :1305-1374`, plus `_now_iso :299`, `axis_title :307`, `_numeric_columns :1514`, `_pick_property :1521`) | 962 | Builders for OpenQuake / MODFLOW / SWMM / SEAWAT; `ls workers/` and `ls trid3nt_server/workflows/` return `mesh` + `telemac` only. Each symbol greps to exactly 2-3 hits (its `def`, its `__all__` entry, and for one a docs mention) - zero call sites in `trid3nt_server/`, `tests/`, `scripts/`. `docs/decisions/0043-processing-wave.md` exempted them while those engines were in the tree. |
| D2 | `trid3nt_server/tools/processing/compute_model_residuals/compute_model_residuals.py` | 856 | Duplicates the pairing of `extract_model_at_observations` and the stats of `compute_skill_metrics`; zero live LLM calls in `data/telemetry/tool_calls.jsonl` (3,012 records), zero workflow `tool()` row, zero product import. **Fold, not a bare cut** - see question 4. |
| D3 | `contracts/trid3nt_contracts/output_quantities.py` (whole file) | 457 | Sole importer repo-wide is `contracts/tests/test_engine_run_args_mixin.py:15`. `OUTPUT_QUANTITIES` keys are `sfincs` (`:199`, empty), `swmm` (`:200`), `modflow` (`:252`), `geoclaw` (`:354`, empty), `landlab` (`:355`), `openquake` (`:414`), `swan` (`:446`, empty) - **no `telemac` key exists**. Its docstring at `:36` points at `workers/_raster_postprocess/output_quantities.py`, which does not exist (`find . -name output_quantities.py` returns this file only). `EngineRunArgsMixin` lives at `contracts/trid3nt_contracts/common.py:201` and is unaffected. |
| D4 | 373 LOC of dead message kinds in `contracts/trid3nt_contracts/ws.py` (`:312-320, :445-469, :499-556, :826-852, :888-909, :964-987, :988-1024, :1025-1129`, and map-command args other than `zoom-to` at `:725-764, :770-795`) | 373 | Zero producer: `grep` for `confirmation-request` / `disambiguation-request` / `clarification-request` / `recovery-choice` / `location-resolved` across `trid3nt_server/` returns only a docstring at `geocode_location.py:767` and the `testing/` harness. Zero consumer: `plugin/net/trid3nt_client.py:1864-2028` dispatches 21 types and none of these. The one apparent producer, `server/protocol/loop.py:700-707`, is a comment-labelled noop: *"Scaffolding only -- no triggers yet. Log and acknowledge without acting."* |
| D5 | `contracts/trid3nt_contracts/event.py` | 332 | `EventMetadata` types `extract_event_metadata` / `model_news_event`; neither symbol exists anywhere in the tree except this file's own docstring. Zero importers in `trid3nt_server/`, `plugin/`, `scripts/`. Cut with `collections.py:276-281,452,458` (events collection + vector index). |
| D6 | `trid3nt_server/workflows/shared/tide_series.py` | 276 | `resolve_tide_series` has no caller outside `tests/test_tide_series_datum.py`; `grep -rn tide_series` finds only the file's own logger name, its `__all__`, and that test. No `__main__`, no registry decorator, no `source.yaml`. (In `workflows/` by path but not a workflow row - it is an unwired helper, so staleness evidence suffices.) |
| D7 | `contracts/trid3nt_contracts/case_results.py` | 192 | `CaseOneResult`; importers are `contracts/__init__.py` (re-export) and `contracts/tests/test_case_results.py`. The Case-workflow composer it typed does not exist. |
| D8 | `trid3nt_server/tools/processing/combine/combine.py` | 202 | Authored 2026-08-30 for the mesh wave; `CombinedGeometryLayerURI` greps to 5 hits, all inside its own module. `grep -rn combine workflows/mesh/` returns nothing. Its own docstring: *"Nothing is computed: no union, no clip, no buffer, no reprojection."* **Conditional** - see question 5. |
| D9 | `trid3nt_server/tools/vector_tiles.py:351-499` (`vector_tiles_enabled :351`, `build_pmtiles :368`, `write_pmtiles_to_object_store :470`) | 149 | Only `densify_if_needed` and the two constants are imported (`emission/pipeline_emitter.py:756,817`). The three tile-server symbols and both env vars (`TRID3NT_VECTOR_TILES_ENABLED`, `TRID3NT_VECTOR_TILES_BASE_URL`) grep to the module itself plus `tests/test_vector_tiles_f94.py` and nothing else. This is the MapLibre vector-tile pipeline for the retired web map. |
| D10 | `scripts/sandbox/oceanmesh/merc_render.py` | 141 | Under `scripts/sandbox/` (exploratory by convention) with no `__main__` guard, unlike every live sandbox driver beside it (`schism_gr3.py`, `mesh_formats.py`, `pysheds_watershed/proof_watershed.py`). Sole importer is `tests/test_proof_basemap_credit.py`. |
| D11 | `trid3nt_server/tools/search/search_tools/search_tools.py:527-567` (`_try_vertex_backend`) | 45 | Gated on `GOOGLE_GENAI_USE_VERTEXAI`; `vertexai` is not an installed or declared dependency, so the branch can never fire. |
| D12 | `trid3nt_server/credentials/secrets_handler.py` | 42 | `grep -rn "secrets_handler\|SecretsHandler"` across the whole tree returns **0** hits outside the file itself. The only true product orphan in `docs/validation/code-graph/orphans.md:16`. `docs/DELETION_LEDGER.md:105` records the vault chop that stranded it. |
| | **total** | **4,027** | |

Test files that ride along (removed with their subject, not counted above):
`contracts/tests/test_engine_run_args_mixin.py` (148), `tests/test_compute_model_residuals.py`
(438), `tests/test_tide_series_datum.py` (102), `contracts/tests/test_case_results.py` (83),
`tests/test_proof_basemap_credit.py` (70), `tests/test_vector_tiles_f94.py` (361, the
PMTiles half only), `tests/test_geometry_composition_tools.py` (136, the `combine` half only),
plus the `recovery-choice` roundtrip in `contracts/tests/test_catalog.py:245-265`.

Cosmetic riders, zero behavioural impact, fold into the doc wave rather than here:
web-era prose in 8 processing modules (`compute_layer_bounds.py:116` cites `Map.tsx` and
GCS; `extract_timeseries_at_point.py:118,189` cite `LayerPanel`/`parseFrameToken`;
`compute_slope.py`/`compute_aspect.py` say "the Orchestrator wires the frontend legend"),
the ~40 Gemini comments in `tools/tool_arg_normalizer.py`, the web-client cross-references
at `plugin/net/ws_bridge.py:14`, `plugin/net/trid3nt_client.py:16,872,890`,
`plugin/ui/gate.py:20`, `contracts/errors.py:117`, the banned "North Star" at
`contracts/trid3nt_contracts/telemac_contracts.py:1`, and the foreign scratchpad path
hardcoded at `scripts/sandbox/telemac/render_erodible_scour_proof.py:32`.

---

## 3. REPLACE WITH LIBRARY, ordered by LOC saved

| # | site | LOC now | LOC saved | exact API | risk |
|---|---|---:|---:|---|---|
| L1 | `server/protocol/catalog_http.py:1887-2512` - hand-assembled HTTP/1.1 (`_format_response :1887` with a 10-code reason-phrase table, `_handle_http :1931` reading the request line off a raw `StreamReader`, a 10-comparison if-chain router, `serve_catalog_http :2489`). 59 `_format_response` call sites, 58 `writer.write` + drain + close triples. | 626 | 420 | `aiohttp.web.Application()` + `app.router.add_get(...)`; `web.json_response(obj)` / `web.Response(status=...)`; `await request.read()`; `client_max_size=MAX_INGEST_BYTES`; mount with `web.AppRunner` + `web.TCPSite` on the existing loop. | MED - keep-alive vs the current unconditional `Connection: close`; route order must register `/plugin-repo/trid3nt.zip` (`:2459`) before the generic prefix. `aiohttp 3.14.1` is installed in `venvs/agent` transitively but **undeclared** in `pyproject.toml`; pin it. |
| L2 | google-genai schema round trip: `adapters/adapter.py:912-1233` (`_simplify_annotation`, `_normalize_callable_for_gemini`, `build_tool_declarations`), `bedrock_adapter.py:515-570`, `openai_adapter.py:331-395` (its own comment at `:228` says it mirrors bedrock's), `anthropic_adapter.py:56` importing bedrock's private converter | 443 | 380 | `pydantic.create_model(fn.__name__, **fields).model_json_schema()` over `inspect.signature(fn)`; `not n.startswith("_")` replaces `_strip_private_params`. pydantic 2.13.4 is a declared hard dep. | MED - `from_callable_with_api_option` parses the docstring `Args:` section into per-parameter `description`, which routing depends on (`_router/registration.py:18`); a ~40-LOC docstring parser or `griffe` must fill that gap. pydantic emits `$ref` for nested models, which OpenAI strict mode rejects - inline via `ref_template` or keep the flattened annotations. |
| L3 | Three COG encoders: `emission/cog.py` (whole file) + `emission/publish.py:362-580` (`_raster_has_overviews :362`, `_read_band1_colormap :394`, `_apply_band1_colormap :414`, `_build_cog_with_overviews :442`, `_build_cog_with_overviews_rasterio :490`, `_overview_factors :557`) | 306 | 250 | `rio_cogeo.cogeo.cog_translate(src, dst, cog_profiles.get("deflate"), use_cog_driver=True, forward_band_tags=True, overview_resampling=..., in_memory=True)`; `rio_cogeo.cogeo.cog_validate(path)` replaces `_raster_has_overviews`. | LOW-MED, but note the **dead branch**: `rio-cogeo` is NOT installed in `venvs/agent`, so the `from rio_cogeo.cogeo import cog_translate` at `publish.py:507` has never executed on this box - path 3 is always live today. The colormap forwarding the comment at `:495-499` calls "version-dependent" must be proven on a real NLCD raster; `_overview_factors`' deliberate always-emit-factor-2 (`:557-568`) is product behavior and survives as an explicit `overview_level=`. |
| L4 | `plugin/net/trid3nt_client.py:994-1255` - RFC6455 client by hand (`connect :1041` builds the `Sec-WebSocket-Key` over a raw socket/ssl pair, `_send_frame :1115` masks the payload in a Python comprehension, `_recv_frame :1197`, `_read_exact :1221`) | 262 | 200 | `qgis.PyQt.QtWebSockets.QWebSocket` - `open(QUrl)` / `sendTextMessage()` / `textMessageReceived`, `connected`, `disconnected`, `errorOccurred`. Ships with QGIS, no install, so it clears the pure-stdlib constraint in `plugin/install_dependencies.py:1-30`. | MED - threading model changes: `plugin/net/ws_bridge.py` (571 LOC) drives a blocking `recv(timeout=)` from a worker thread; `QWebSocket` is signal-driven on the Qt loop, so ws_bridge changes shape (a second edit, not free). Probe `from qgis.PyQt.QtWebSockets import QWebSocket` on macOS QGIS 4.0.3 first. |
| L5 | `workflows/shared/cog_io.py:259-517` - `write_cog_4326_from_grid :259` and `reproject_cog_file_to_4326 :419` stage through a temp GTiff on disk, hand-build both profile dicts, reproject band-by-band, unlink in five places | 259 | 200 | rioxarray (declared, 0.22.0 installed): `xr.DataArray(...).rio.write_crs(...).rio.write_transform(...).rio.reproject("EPSG:4326", resampling=...).rio.to_raster(dst, driver="COG")`. No temp source file. | LOW - assert `dst.width/height` on one fixture (rioxarray calls the same `calculate_default_transform`). `_run_crs_roundtrip_guard :218` is product behavior and stays. |
| L6 | `tools/fetchers/_router/transport/client.py:73-239` - the same `for attempt in range(MAX_RETRIES + 1)` loop written verbatim four times (`head :88`, `get_bytes :128`, `post_bytes :173`, `range_get :210`), plus a 5th/6th/7th in `bedrock_adapter.py:962-990`, `anthropic_adapter.py:531+`, `openai_adapter.py` | 167 | 110 | `tenacity` (9.1.4 installed): `@retry(stop=stop_after_attempt(...), wait=wait_exponential_jitter(...), retry=retry_if_exception_type(...), reraise=True)` on one private `_request`. Adapters: `openai.AsyncOpenAI(max_retries=N)`, `anthropic.AsyncAnthropic(max_retries=N)`, `botocore.config.Config(retries={"mode": "adaptive"})` - all three SDKs are declared deps and honor `retry-after`. | LOW for transport. For the adapters the standing norm is the constraint: SDK-internal retries emit no per-attempt log line, so the verbatim-upstream-error obligation moves to an httpx/botocore event hook. `Retry-After` stays honored via a custom `wait=` callable. |
| L7 | `tools/processing/extract_model_at_observations/extract_model_at_observations.py:1124-1273` (`_pair_timeseries`) - planar `np.hypot` in degrees + `np.argmin` per observation (`:1183-1215`), then a Python `min(..., key=lambda)` linear scan per timestamp, with its own `_parse_iso :1174` | 150 | 110 | `gpd.sjoin_nearest(obs, model, max_distance=tol_m, distance_col="dist_m")` after `.to_crs(estimate_utm_crs())`; `pd.merge_asof(..., direction="nearest", tolerance=pd.Timedelta(...))`; `pd.to_datetime(s, utc=True, format="ISO8601")`. | MED - and a **correctness win**: today's `tol_deg = tol_m / 111320.0` is wrong by `1/cos(lat)` on the longitude axis (41% at 45N). `merge_asof` requires sorted keys and breaks ties backward where `min()` keeps first-seen; the `outside_footprint`/`no_time_match`/`nodata_sample` bookkeeping feeding `_reason_summary :1303` must be rebuilt from the left join's NaN rows - that is the actual work. |
| L8 | Plugin urllib HTTP, 6 near-identical ~25-LOC blocks: `trid3nt_client.py:421,482,539`, `render/probe.py:104`, `case/push_layer.py:90,139` | 150 | 100 | `qgis.core.QgsBlockingNetworkRequest` + `QNetworkRequest`; ships with QGIS. | LOW, and more than LOC: it routes through `QgsNetworkAccessManager`, inheriting the user's QGIS proxy settings, SSL exceptions and auth configurations. Today a user behind a QGIS-configured corporate proxy gets an opaque "agent unreachable" from `probe.py:117`. |
| L9 | `tools/processing/charts_common.py:176-251` `_summarize_raster` (reads the full band to float64 at `:194`, hand-builds the valid mask at `:204-208`) and `:252-291` `_summarize_vector` (per-column dtype loop at `:270-286`) | 116 | 70 | `src.read(1, masked=True)` then `.min()/.max()/.mean()/.sum()` + `np.histogram(arr.compressed(), bins=10)`; or `src.statistics(1, approx=False)`. Vector: `gdf.drop(columns="geometry").select_dtypes("number").agg([...]).to_dict()`. | LOW - the masked read correctly excludes alpha/mask-band pixels the `data != nodata` test at `:205` counts, so some `count` values change. That is a fix but a visible one. |
| L10 | `plugin/case/aoi.py:30-100` - `merc_to_lonlat` + `extent_to_bbox4326` implement the EPSG:3857 inverse by hand | 71 | 50 | `QgsCoordinateTransform(src, QgsCoordinateReferenceSystem("EPSG:4326"), QgsProject.instance()).transformBoundingBox(rect)` - the fallback the docstring at `:64-70` concedes the caller already has in `dock.py`. | LOW - but `plugin/tests/test_milestone2.py:209` tests the pure-math path without QGIS; moving pushes that test into the Qt harness. That is the real trade. |
| L11 | Hand-rolled geodesy constants: 52 lines across 27 files (`_fetch_common.py:136-142,158-164`, `raster_cog.py:1359-1362,1564-1566`, `hooks/topobathy.py:325-329`, `hooks/dem_3dep.py:452-455`, `hooks/landcover.py:91-94`, `_pc_stac.py:256-258`, `compute_layer_bounds.py:325-345`, `compute_exposure_summary.py:239`, `compute_sediment_yield.py:417`, `delineate_watershed.py:164`, `compute_flood_extent_skill.py:329-331`, `extract_model_at_observations.py:635-648,1183`, and `tools/_example_tool_template.py:130-135`) | 52 | 40 | `pyproj.Geod(ellps="WGS84").geometry_area_perimeter(shapely.box(*bbox))[0]`; `Geod.fwd/inv` for metre pads and distances; `pyproj.CRS(crs).axis_info[0].unit_conversion_factor` for `_meters_per_unit`. pyproj 3.7.2 installed, already imported by 17 server modules. | LOW mechanically; the point is **accuracy**. The flat 111,320 m/deg-lat is 110,574 at the equator and 111,694 at the pole, and several sites floor `cos(lat)` at 0.05 or 0.15, distorting high-latitude AOIs. Golden-value fixtures need rebaselining. **Fix `_example_tool_template.py:130-135` first** - it is the file new tools are copied from, so the pattern reproduces itself. |
| L12 | `extract_model_at_observations.py:664-701` (`_nearest_wet_sample`) - a `(2r+1)^2` Python double loop per observation | 38 | 30 | `scipy.ndimage.distance_transform_edt(~np.isfinite(band), return_indices=True)` once for the whole raster, then index per observation; or `scipy.spatial.cKDTree(np.argwhere(finite))`. scipy 1.15.3 installed and this file already imports `scipy.ndimage.map_coordinates` at `:651`. | LOW - EDT is a whole-array pass, cheaper above a handful of observations and more expensive for one. Gate on count. |
| L13 | Five divergent bbox-overlap helpers: `raster_cog.py:2478`, `hooks/field_boundaries.py:49`, `hooks/dem_3dep.py:222`, `search_data_catalog.py:116`, `server/dispatch/aoi.py:186` | 35 | 25 | `shapely.box(*a).intersects(shapely.box(*b))`. | LOW mechanically, but they **do not agree**: raster_cog and field_boundaries use strict `<`/`>`, `aoi.py` uses `<=` and documents "touching-edge counts as overlap", and only two guard malformed input. One call site changes behavior at an exact edge touch - decide which semantics is correct rather than picking by majority. |
| L14 | `tools/tool_arg_normalizer.py:512-522` (`snake_case`) | 11 | 10 | `pydantic.alias_generators.to_snake` (and `to_camel` for its sibling). | LOW. |
| | **total (excludes the two rows below)** | | **1,995** | | |

Two rows measured and deliberately **not** taken:

- `tools/fetchers/_router/executors/raster_cog.py:383-716` (334 LOC): `_parse_vrt :395` is
  an `xml.etree` reimplementation of GDAL's VRT reader, `_multi_url_to_array :474` a manual
  windowed-read mosaic. `rasterio.open(vrt)` / `WarpedVRT` / `rasterio.merge` is the nominal
  replacement, but `transport/client.py:1-9` states the architecture: one pooled `httpx.Client`,
  one retry authority, "GDAL-side retries stay off everywhere - reads never touch `/vsicurl/`".
  Handing the read to GDAL surrenders `Retry-After`, the verbatim-upstream-error norm and
  connection pooling. **Do not chop without first replacing the retry story.**
- `plugin/net/trid3nt_client.py:202-810` (609 LOC): the plugin re-derives the envelope shapes
  that `contracts/trid3nt_contracts/ws.py` already models in pydantic, and `plugin/ui/gate.py`
  cites those files as "Contract source of truth" in ten comments (`:10,167,466,543,635,847,1018,1187,1281,1354`).
  **BLOCKED**: `plugin/install_dependencies.py:1-30` rules out pydantic in QGIS's interpreter
  (QGIS 4 on macOS has no pip). The only shapes this could take are a build-time stdlib-dataclass
  mirror generated from `model_fields`, or vendoring pydantic-core wheels. Realistic saving today: 0.

**Dependency hygiene found while measuring.** `httpx` (15 server importers, the fetcher
transport layer) and `PyYAML` (7 importers) are imported by product code but absent from
`pyproject.toml` - present only transitively via openai / anthropic / dataretrieval /
google-genai. `tenacity` and `aiohttp` are in the same position and become load-bearing under
L1 and L6. L2 removes google-genai, which is the supplier of both `httpx` and `tenacity`.
**Pin all four before relying on any of them.**

---

## 4. The processing verdict

`trid3nt_server/tools/processing/` measured: **36 tool packages, 23,551 LOC total**
(20,971 in tool modules, 2,188 in shared helpers, 392 in `corpus.yaml`). By line class:
4,761 docstring (23%), 1,884 comment (9%), 3,154 blank (15%), **11,172 actual code (53%)**.

**The governing ratio: 36 of the server's 56 hand-written `@register_tool` decorators live
here.** The fetchers already folded - 108 declarative `source.yaml` specs, 0 coded fetcher
twins. Processing is the last large block of hand-written tool code in the product.

### The numbers

| shape | tools | LOC | share of 20,971 |
|---|---:|---:|---:|
| thin wrapper over one library/CLI call (declarative-spec shaped) | 16 | 7,631 | 36% |
| genuine domain computation (keep as code) | 19 | 12,484 | 60% |
| pure duplicate of another tool (`compute_model_residuals`) | 1 | 856 | 4% |
| **unused by anything but its own test** | **26** | **14,238** | **68%** |
| ever invoked by a live LLM turn | 3 | 1,465 | 7% |
| called from a workflow template | 4 | 1,353 | 6% |
| imported by product code (`cases/probe_point.py:47-48`) | 2 | 773 | 4% |

Live evidence, five channels: `grep -rn 'tool(\s*"' trid3nt_server/workflows/` returns 12 call
sites naming 4 tools (`section` x3, `endpoints`, `delineate_watershed`, `compute_layer_bounds`);
`data/telemetry/tool_calls.jsonl` holds 3,012 `source="llm"` records from 2026-07 to today, in
which exactly **three** processing tools ever appear - `compute_slope` (42 calls),
`compute_colored_relief` (37), `clip_raster_to_polygon` (18); all other 33 are zero. Processing
is 3.2% of recorded live tool calls; `fetch_dem` alone is 1,591. Honest caveat: that JSONL is a
partial capture (default sink `/tmp/...`, 29 distinct names) and does not record workflow-interpreter
`tool()` calls - so it does not condemn the 4 workflow tools, but it does condemn the other 29.

The boilerplate that makes this a spec problem, measured across all 36 packages: 90 bespoke
`*Error` classes (35/36 packages, max 6 in one file), `**_extra_ignored` (36/36), a temp-file
dance (30/36), a hand-built `read_through` cache-key dict (16/36), hand-constructed `LayerURI`
(27/36), inline `style={...}` (16/36). Cross-tool duplication by 5-line shingle: `compute_ndvi`
<-> `digitize_water_body` share 52 identical shingles, `compute_aspect` <-> `compute_slope` 43,
`compute_hillshade` <-> `compute_slope` 23. Retrieval collides too: 338 corpus phrasings where
"terrain" routes to 8 tools and "flood" to 14, and `compute_colored_relief/corpus.yaml:4` is
literally `- make a shaded relief map here` against `compute_hillshade/corpus.yaml:3`
`- I want a shaded relief map of the Cascade Range`.

### The three-fate proposal

**(a) DECLARATIVE SPEC - `processing/<name>/op.yaml`, the fetcher pattern.**
16 tools, 7,631 LOC of Python -> ~1 engine + 16 spec files. Four spec families cover all 16:

| family | `op.kind` | tools | LOC absorbed |
|---|---|---|---:|
| gdal_cli | resolve binary, build argv from a flag table, run, COG-encode | `compute_slope` 292, `compute_aspect` 298, `compute_hillshade` 588, `compute_colored_relief` 449, `compute_contours` 564 | 2,191 |
| raster_expr | open 1..N rasters, align to a reference grid, evaluate a numpy expression / reclass table, write | `extract_landcover_class` 525, `compute_impervious_surface` 563, `compute_blended_composite` 582 | 1,670 |
| stac_index | search least-cloudy STAC item, sign hrefs, window-read a band pair, normalized difference, optional threshold + polygonize | `compute_ndvi` 495, `digitize_water_body` 623, `compute_change_detection` 788 | 1,906 |
| vector_op | read geometry doc, apply one shapely/pyogrio op, write FGB | `clip_raster_to_polygon` 724, `combine` 202, `endpoints` 219, `compute_layer_bounds` 477, `generate_chart` 242 | 1,864 |

The engine already exists in pieces - `_gdal_runner.run_gdal`/`read_raster_bytes` (144 LOC),
`tools.cache.read_through`, `emission.cog.translate_to_cog`, `_geometry_common.read_geometry_doc`
- plus a param validator and a `LayerURI` builder: ~850 LOC to write against 7,631 deleted.
Five carve-outs survive as engine hooks (the fetcher `hooks:` precedent, ~190 LOC total):
`compute_hillshade`'s `swiss_double` two-hillshade multiply, `compute_contours`' nice-number
auto-interval, `compute_layer_bounds`' `map-command(zoom-to)` emission, `endpoints`'
one-continuous-line refusal, `compute_change_detection`'s per-class count/area side-channel.
`compute_change_detection.py:174-177` already carries `{"ndvi": ("B08","B04"), "ndwi": ("B03","B08")}`
- the band pair is data, which is the proof the stac_index generalization is real.

**Blocked-dep check**: `docs/specs/gdal-leverage-audit.md` S1 proves `osgeo.gdal` is not importable
(no wheel, ABI clash with the libgdal rasterio/pyogrio link) and that rasterio has no
`gdaldem`/`gdal_contour` equivalent. This fate does **not** need it - `gdal_cli` keeps the existing
subprocess path through `_gdal_runner`; it moves the declaration to YAML, not the execution to a
new binding. **Registered tool count is unchanged at 16** - the LLM-visible catalogue does not
shrink; the code that has to be style-swept and legend-swept does, by ~6,591 py LOC.

**(b) CODE SUBSYSTEM - 19 tools, 12,484 LOC stay Python.** model-vs-obs (2,849: datum, quantity
and time reconciliation with per-item drop reasons - no library does this and the honesty floor is
the point), domain models (2,824: Staley2017/Gartner2014/Cannon2010, RUSLE, HAZUS - published
equations with cited constants), hydrology (1,037: pysheds D8, the only pair with live workflow
use), geometry (`section` 455: the chord-perpendicular UTM cut has no library equivalent),
product surface (2,408: case-layer enumeration, frame-token classification, the DuckDB SQL seam),
movement ecology (1,511), imagery (1,787). Even here the fate-(a) engine pays: all 19 drop their
`read_through` dict, their `**_extra_ignored`, their inline `style={}`, their `LayerURI`
construction and most of their 90 error classes into the engine declaration - a measured
15-20% shrink (~2,084 LOC) with zero algorithm change.

**(c) DELETE - 2,020 LOC outright.** `charts_common` dead builders 962 (D1), `compute_model_residuals`
856 (D2), `combine` 202 (D8). Plus ~1,350 more absorbed through the C4 (stac_index) and C5
(gdal_cli) folds, which are counted inside fate (a).

### Bottom line for the subsystem

| | LOC now | LOC after | delta |
|---|---:|---:|---:|
| 16 thin wrappers -> specs (+ engine + hooks) | 7,631 | ~1,040 py + ~2,200 yaml | -6,591 py |
| 19 genuine computations, boilerplate hoisted | 12,484 | ~10,400 | -2,084 |
| `compute_model_residuals` folded | 856 | 0 | -856 |
| `charts_common` dead builders | 962 | 0 | -962 |
| `combine` (conditional) | 202 | 0 | -202 |
| **`processing/` total** | **23,551** | **~12,900 py + ~2,200 yaml** | **-10,695 py (-45%)** |

Two structural moves that cost nothing and should ride along:
- move `charts_common.py` (582 LOC after D1) out of `tools/processing/` - its live consumers
  are `emission/pipeline_emitter.py`, `server/turn/stream.py` and 8 telemac templates. It is an
  emission seam, not a processing tool. Only 4 of its symbols are live: `build_chart_payload`
  (31 refs), `is_chart_emission_result` (21), `build_hydrograph_overlay_chart` (6),
  `build_budget_partition_chart` (2).
- move `_geometry_common.py` (113 LOC) out of `tools/processing/` - 9 of its 13 importers are
  `workflows/mesh/**` and `workflows/telemac/**` (`mesh/inputs.py:51,83`, `mesh/recipe.py:60`,
  `mesh/shared/nodes.py:18,208`, `mesh/shared/primitives.py:206`, `telemac/authoring/assembler.py`,
  `telemac/helpers/reach.py`, `telemac/helpers/release_point.py`).

**Before any fold executes**, re-run the fetcher-fold methodology for processing:
`experiments/fetcher_fold_routing/` (baseline routing -> fold -> replicate -> compare,
`results/comparison.json`, 2026-09-04) is the template and it is three days old.

---

## 5. Ledger status

`docs/DELETION_LEDGER.md`: 3,372 lines, 441 data rows, 311 already `DELETED`. Of the 130
non-DELETED rows, 48 are stale fetcher-fold snapshots (batch verdict below) and 71 were
individually re-measured at HEAD.

### Conditions MET - flip to DELETED with zero further work (7 rows, all verified independently)

| line | candidate | verification at HEAD |
|---|---|---|
| L166 / L228 / L274 | `fetch_nexrad_reflectivity` fold, then PERMANENT-BESPOKE, then RECLASSIFIED | `grep -rln fetch_nexrad_reflectivity --include=*.py` = **0**. The reclassify landed: `trid3nt_server/tools/display/show_nexrad_radar/` is registered (`tools/__init__.py`, `main.py:108`) and tested (`tests/test_show_nexrad_radar.py`). Collapse all three rows to one DELETED line. |
| L270 | `TEMPLATE_CARD` exports (20 modules) + `_template_card.py` (10 files) | `grep -rl TEMPLATE_CARD --include=*.py .` = **0**. Already gone. |
| L322 | `CaseManifest` + `CaseManifestLayer` contract types | `grep -rln CaseManifest --include=*.py .` = **0**, contracts included. |
| L337 | `test_mongo_mcp_wiring.py` filename | `find . -iname 'test_mongo_mcp_wiring*'` = **0**. Already renamed. |
| L487 | `case_lifecycle.ensure_case_qgs` + the `CaseLifecycleError` qgs branch | `grep -rln case_lifecycle --include=*.py .` = **0**; L508 shows the whole module deleted in wave A. Earlier duplicate row never flipped. |
| L488 | `emission/quantity_styles.py` hand-mirrored `_QGIS_STYLE_REGISTRY` | `grep -rln quantity_styles --include=*.py .` = **0**; `ls trid3nt_server/emission/` returns cog, layer_uri_emit, mesh_display, outputs_seam, pipeline_emitter, presets, publish, restyle, uri_registry - no quantity_styles. L74 shows the file deleted, folded into `contracts/.../styles.yaml` + `emission/styles.py`. |
| L489 | Two lazy `emission.publish` -> `tools.processing` imports | Both spellings grep to 0 inside `emission/*.py`; the only `compute_hillshade` reference left there is a name literal at `uri_registry.py:173`. |

### Conditions PARTIALLY met - re-scope the row, do not close it

- **L356** ~55 matplotlib proof/montage scripts: down to **8** files under `scripts/*.py`
  importing matplotlib directly. Real progress; chart-figure (non-raster-montage) scripts are
  explicitly meant to outlive the row. Re-scope the count.
- **L534** four private `_publish_peak_layer` defs: **1** remains
  (`workflows/telemac/products/products.py:157`, one call site, one test patch). 3 of 4 migrated.

### Plausibly MET, needs a live run rather than a grep - do not cut blind

- **L284** MODFLOW gridgen binary absent: `./bin/gridgen` exists at HEAD, so the binary-absent
  blocker looks resolved, but the row's condition also names `build()`-path wiring. Needs a live
  DISV run.
- **L311** HEC-RAS 6.x worker -> 2025 native-Linux: the row says the 2025 beta is win-x64-only,
  while the standing project memory note says the 2025 managed engine prepares and solves
  all-Linux. **These two records conflict** - reconcile against the current HEC-RAS release
  before either is trusted.
- **L315** artemis/tomawac in-worker bathymetry: status is `RESOLVED (commit-pending)`. Trending
  MET; re-check once the commit lands.

### STALE rows - subject already gone, status never flipped (hygiene only, zero code action)

- **48 fetcher-fold campaign snapshots** at lines 154, 158, 161-163, 165, 168-169, 172-175,
  181-189, 191-194, 196, 199-207, 209-214, 216, 220-224, 227, 229-232, 236, 238, 240, 243-246,
  250-251, 256, 260, 264. Each is a `fetch_<name>` (or a mode enabler like "griddap raster access
  mode", "RECORD-RETURN output shape") for which a LATER row carries a real `DELETED (ADR ...)`
  with a fold writeup - e.g. `fetch_landcover` QUEUED at L158/L192, DELETED at L247+L354;
  `fetch_hrrr_forecast` QUEUED across L165-L209, DELETED at L241+L354; `fetch_topobathy` DELETED
  at L159+L355. The router-fold campaign finished; these are dated progress snapshots the ledger
  never collapsed.
- **L110** `fetch_copernicus_dem ambient declaration` - `CONDITION-MET`, superseded.
- **L51** prefix-keyed snapshot cache for realized meshes - CHOPPED and never built; zero
  snapshot-cache code exists, so there is nothing to delete.
- **L303** ImpactPanel WS wire - the row's condition names a WEB-SIDE cull wave that can never
  run: the web client is not a product surface. Re-home this row onto the plugin (see question 3).
- **L487 / L488** - listed under MET above; they are simultaneously stale duplicates of L508/L74.

### Rows that are decision records, not open candidates (verified still correct)

L48 (`refine_region` kept as a primitive), L114 (env-var credentials DEMOTED, not deleted),
L123 (gzip/vsizip GDAL collapse - REJECTED, empirically refuted 2026-07-31), L124 (jrc colormap
DSL - REJECTED, one consumer), L150 (ZIP-member range-read - superseded by `transport.get_zip`),
L334 (`init_persistence_from_env` - live at `server/session/persistence_ref.py:39`, called from
`run_server`, exported at `server/__init__.py:80`), L335 (TiTiler unwrap - KEEP, but the row's
own spellings `_titiler_cog_uri`/`_normalize_layer_uri` grep to 0 and need a rename-only touch-up),
L565 (5 non-demoted `compute_*` tools - all 5 still present, decision holds).

**L336 is the one decision record this sweep overturns.** It marks
`confirm-response`/`disambiguation-response`/`clarification-response` KEEP, "confirmed live+wired",
citing `server/protocol/loop.py:701-703`. The loader path says otherwise: that branch reads
*"Scaffolding only -- no triggers yet. Log and acknowledge without acting."* and there is no
producer anywhere. See D4.

The remaining ~55 measured rows are genuinely NOT MET at HEAD and stand unchanged.

### DELETED-row spot check

15 sampled (L280, 120, 340, 45, 60, 508, 74, 318, 560, 52, 483, 157, 40, 69, 367). All clean:
`_SESSION_ANON_ID`, `build_case_view_snapshot`, `OutputQuantitySpec.style_preset`,
`_check_revisable_branches`, `GATE_NOT_YET_SUPPORTED`, `model_urban_flood_swmm` all grep to 0;
`fetch_airnow_air_quality` / `fetch_noaa_slr_marsh` hits are the live router-folded specs, not
resurrections of the deleted bespoke twins; `fit_downstream_bed` / `_RIM_TOLERANCE` hits are
`assert "X" not in ...` negative assertions in `tests/test_mesh_om2d.py` proving absence.
**No resurrected spellings found.**

---

## 6. DESIGN questions NATE must rule

Each of these has two defensible fates. Everything not on this list is mechanical.

1. **Bedrock Converse path (~800 LOC in `adapters/bedrock_adapter.py`: `:274-390`, `:414-482`,
   `:546-1235`).** DELETE it (AWS is decommissioned; the live provider is `openai` per
   `Makefile:29`, `scripts/use_openrouter.sh:67`, `scripts/start_agent.sh:67`) or KEEP it as a
   dormant pluggable-LLM seam the way `anthropic_adapter.py` is kept. Complication: the module
   named for the dead provider holds six helpers the live path imports - `model_provider()`
   (`:244`, used by `plugin_repo.py:642`, `model_discovery.py:27`), `resolve_selected_model()`
   (`:192`), `bedrock_model_id()` (`:255`), `bedrock_context_window()`/`model_supports_cache()`
   (`:153,:176`, used by `server/protocol/loop.py:288,833`), and `_genai_schema_to_json_schema()`
   (`:515`, imported across module boundaries by `anthropic_adapter.py:56`).
   **Recommendation: lift the six helpers into a provider-neutral module first, then delete the
   Converse machinery.** Do not do it in the same pass as the mechanical D-rows.

2. **`tools/fetchers/_router/stratified.py` (328 LOC) and the arm-3 experiment.** DELETE (nothing
   imports it; `registration.py:325` only branches tier=catalog/general and never dispatches into
   it; the two importers are `tests/test_catalog_surfacing.py:286` and
   `tests/test_fallback_sweep_guard.py:90`) or KEEP HELD (ledger L111/L112 park the
   `TRID3NT_CATALOG_ARM` rollout on a capable-model re-run that has not happened). Note the arm
   gating itself is live elsewhere - `search_data_catalog.py:219` reads arm "1",
   `fetch_from_catalog.py:43` reads "1" or "3" - so deleting this file kills Design 3 only.
   **Recommendation: ATTIC with the spec (`docs/specs/stratified-pools.md`), so the arm-3 decision
   stays available without the module sitting in the live tree.**

3. **`contracts/trid3nt_contracts/impact_envelope.py` (373) plus its plugin renderer
   (`plugin/ui/gate.py:1351-1441`, 91 LOC; `plugin/ui/dock.py:2066-2072`;
   `plugin/net/trid3nt_client.py:2016-2021`).** DELETE both ends (the producer was Pelicun, which
   is not in the tree; `grep -rn impact-envelope trid3nt_server/` = 0) or KEEP the plugin end as a
   renderer awaiting an unbuilt producer. Ledger L303 conditions this on a "web-side cull wave"
   that can never run.
   **Recommendation: DELETE both ends and re-home L303 onto the plugin; a one-way seam with no
   producer teaches a wrong model of the protocol.**

4. **`compute_model_residuals` (856 LOC).** DELETE-by-fold into `extract_model_at_observations`
   (pairing) + `compute_skill_metrics` (stats), or KEEP because it is the only tool that returns a
   residual **point layer** from a single call - the survivors return a paired table and a metrics
   dict respectively.
   **Recommendation: fold, with the standard two-gate recipe - live replication of RMSE/ME and the
   residual point layer on the same inputs, then re-home its 8 corpus phrasings onto the
   survivors. If the point layer cannot be reproduced in one call, keep it and delete nothing.**

5. **`combine` (202 LOC).** DELETE (authored 2026-08-30, no caller anywhere, its own docstring says
   it computes nothing) or KEEP as mesh-wave surface that has not been consumed yet.
   **Recommendation: ledger it with the CONDITION "delete unless a mesh recipe lands that reads a
   `CombinedGeometryLayerURI`", and cut it at the next mesh-wave close if still unconsumed.**

6. **The declarative processing fold (16 tools, -6,591 py, +~2,200 yaml, +~850 engine).** Do it, or
   leave processing as hand-written Python. It is the largest single item in this sweep and a real
   architectural commitment: it trades Python for a second declarative dialect beside
   `source.yaml`, and the fetcher fold is the only evidence it works.
   **Recommendation: do it, but re-run `experiments/fetcher_fold_routing/` for processing FIRST -
   baseline routing, fold, replicate, compare - because the retrieval collisions (338 phrasings,
   "terrain" -> 8 tools, two tools competing for "shaded relief map") mean the fold could move
   routing accuracy in either direction and that must be measured, not assumed.**

7. **Movement ecology: `compute_movement_trajectory` (773) + `compute_home_range_kde` (738).**
   KEEP as genuine domain code, THIN-WRAP on `movingpandas`, or CUT. Both are genuine computations
   with zero live use, and this is a scope question, not a sweep finding: is animal telemetry
   inside the geospatial-intelligence identity?
   **Recommendation: rule the scope question first. If in scope, keep as code (a thin wrap saves
   little and adds a dep). If out of scope, ATTIC both.**

8. **`contracts/trid3nt_contracts/ws.py` dead kinds (373 LOC, 28% of the file).** DELETE (no
   producer, no consumer, and the apparent handler at `server/protocol/loop.py:700-707` is a
   comment-labelled noop) or KEEP as declared protocol vocabulary for gates that may return.
   The live gates already emit a *different* vocabulary from `gates/confirm.py` -
   `tool-payload-warning` (`:488`), `code-exec-request` (`:615`), `credential-request` (`:874`),
   `region-choice-request` (`:927`), `spatial-input-request` (`:1092`) - typed in
   `payload_warning.py`, `sandbox_contracts.py`, `secrets.py`, `region_choice.py`. So `ws.py`
   currently holds the OLD web-client vocabulary sitting beside the new one.
   **Recommendation: DELETE. Two competing protocol vocabularies in one contracts package is the
   drift risk, not the 373 lines.**

9. **New runtime dependencies.** L1 (aiohttp) and L6 (tenacity) make two currently-transitive
   packages load-bearing, and L3 requires declaring `rio-cogeo`, which is **not installed** in
   `venvs/agent` at all. Separately, `httpx` (15 importers) and `PyYAML` (7) are already
   load-bearing and undeclared, and L2 removes `google-genai`, which is what supplies them.
   **Recommendation: pin `httpx`, `PyYAML`, `tenacity` and `aiohttp` in `pyproject.toml` as a
   standalone hygiene commit BEFORE any library-replacement row lands, so a dependency change and
   a behavior change are never in the same commit.**

10. **Plugin-side library rows (L4 QWebSocket 200, L8 QgsBlockingNetworkRequest 100, L10
    QgsCoordinateTransform 50).** All three clear the pure-stdlib constraint in
    `plugin/install_dependencies.py:1-30` because Qt and QGIS ship them, but they are not equal.
    **Recommendation: take L8 now (low risk, and it wins the user's QGIS proxy/SSL/auth settings,
    which urllib silently ignores today); defer L4 until `ws_bridge.py`'s thread-and-queue model
    is redesigned, since the signal-driven `QWebSocket` changes its shape; take L10 only if you
    accept moving `plugin/tests/test_milestone2.py:209` into the Qt harness.**
