# GDAL native-collapse verdict -- FOR NATE

Question (NATE): several hand-built router modes may reimplement native GDAL
capabilities. If a cheap GUARD (pre-flight envelope validation via OUR transport,
then GDAL executes verified work) preserves the typed-error contract (status int +
verbatim body + never-silent + retry authority), those modes collapse to spec
declarations. Four candidates investigated across four evidence lanes (PROBE,
AUDIT, GUARD, BENCH). This is a research verdict -- NO code changes. Any COLLAPSE
executes later through the standard two-gate parity recipe.

Env (all lanes, read-only, no venv mutation): rasterio 1.5.0 / bundled GDAL 3.12.1
(raster paths); pyogrio 0.13.0 / bundled GDAL 3.12.4 (vector paths); system
ogr2ogr/gdalinfo GDAL 3.10.3/3.9.2. Version-drift within the venv noted, out of
scope. Scratch scripts: scratchpad/gdal-collapse/{probe1-4,bench_vector,bench_raster,parse_verbose}.py.

Source grounded this session: raster_cog.py = 1130 LOC (multi_url L319-497,
gzip L509-631); vector_fgb.py = 672 LOC; transport/ = 630 LOC
(opener 115 + range_file 198 + client 194 + errors 123).

---

## Per-mode verdict

### 1. multi_url VRT fan-out -- KEEP-OURS
raster_cog.py L319-497 (~190 LOC: `_VrtSource` + `_parse_vrt` +
`_resolve_multi_url_members` + `_multi_url_to_array`). Candidate: GDAL VRT driver
over `/vsicurl/`.

Unpreservable property: **never-silent, on a mid-mosaic member 404.** This is THE
deciding finding and the lanes CONFLICT on it -- flagged, not smoothed:

- PROBE/SYNTHESIS: `rasterio.open('/vsicurl/'+vrt_url)` RAISED `RasterioIOError`
  when a member was 404'd -- honest failure, did NOT return nodata.
- GUARD: GDAL's `VRTSourcedRasterBand` is DESIGNED to tolerate missing sources and
  fills NODATA silently; the mode's "ANY member fail -> typed UPSTREAM" cannot
  survive structurally.
- AUDIT: native collapse reopens the exact 404->UPSTREAM status-blindness bug that
  ADR-0044 / opener.py exist to fix; per-member typed EMPTY/UPSTREAM is lost.

Resolution: the behaviors are NOT contradictory once you separate open-time from
read-time. A member unreachable at VRT-open can raise; a member reachable at open
whose byte-range read fails mid-read gets nodata-filled per VRT design. The probe
hit the raising path; the honesty contract requires the guaranteed path. A mode
whose error behavior is timing- and version-dependent cannot preserve
never-silent, so it stays hand-built. BENCH is the tempting counter-argument
(native won all 3 metrics; full-array SHA-256 BIT-IDENTICAL:
b308cdb0...4ce9; 1 fewer request, ~13% fewer bytes) -- but bench parity measures
the happy path only. Cost profiles are near-identical, so there is no efficiency
prize to offset the honesty loss. Also: opener.py's own docstring records that
GDAL ReadMultiRange hangs through our opener at this GDAL version.

Real collapse = **0 LOC.** `ingest.multi_url` spec field stays as-is.

### 2. gzip_object -- KEEP-OURS
raster_cog.py L509-631 (~132 LOC). Candidate: `/vsigzip//vsicurl/` chain.

Losing bench + lost property. PROBE (real CHIRPS monthly .tif.gz, 14.7MB
compressed): a tiny 8x5px windowed read via `/vsigzip//vsicurl/` still pulled
36.4MB RX -- EXCEEDS the whole compressed object. gzip's non-seekable stream forces
a near-full/duplicate transfer; **zero windowing benefit.** The existing
whole-object-fetch-then-window-in-memory design is the CORRECT choice, not
over-conservative. Additionally retry-authority is lost: GDAL #901 (nested-vsi
ignores `HTTP_MAX_RETRY`) and verbatim body is lost. Even the marginal ~20-30 LOC
that `/vsigzip/` could replace (get_bytes + gzip.decompress + MemoryFile-open)
is net-negative once retry/backoff authority goes; the ~100 LOC of date-templating
/ sentinel logic is business logic that stays regardless of transport.
404/future-date already raise cleanly (verified vs a real HTTP 404 HEAD).
Real collapse = **0 LOC.**

### 3. vsizip_member (DEFLATE zip entry) -- KEEP-OURS
Same physics as gzip. PROBE (GHSL tile zip, DEFLATE entry compress_size=26.6MB,
ground-truthed via zipfile central directory): a 480x300px window via
`/vsizip//vsicurl/` pulled 34.3MB RX (~full entry). DEFLATE zip streams aren't
randomly seekable, forcing near-total sequential decompress regardless of window.
No candidate native path is cheaper; nothing to collapse to. (Note for ADR-0052:
the inner TIFF's own band compression is LZW, distinct from and irrelevant to the
zip-entry seek problem.) Real collapse = **0 LOC.**

### 4. vector_fgb ArcGIS paging -- PARTIAL (low-value; gated on an accepted degrade)
vector_fgb.py ~164-197 LOC of paging (build_where L57, resolve_endpoints L480,
build_query_params L514, `_fetch_one_page` L571, `_fetch_from_endpoint` L616,
fetch_features L640) + ~34 LOC esri ring-decode (`_esri_geometry_to_geojson` L444).

Here the lanes genuinely disagree; both are right about different collapses:

- AUDIT: **NOT collapsible** as a PURE native collapse. Every ArcGIS 200-wrapped
  error envelope tested (malformed where, bad field, bad layer index, and a LIVE
  429 hit mid-probe) collapsed to the IDENTICAL generic `Missing features member`
  DataSourceError -- status/body/retryable all destroyed. Violates never-silent.
- GUARD/BENCH: a HYBRID collapse preserves the contract on page 1. Fetch page-1
  (resultOffset=0) through OUR transport (retry authority + Retry-After);
  `classify_response` gates it (200-wrapped `{error}` -> router_upstream verbatim;
  empty -> honest header-only FGB). SHORT-CIRCUIT: if page-1 < page_size, serialize
  it directly, driver never invoked -- the common small-bbox case pays ZERO extra
  cost. Only when page-1 is saturated hand OGR a clean `ESRIJSON:` URL for native
  auto-paging. BENCH parity (HIFLD transmission lines, TX bbox): counts identical
  6923==6923, OBJECTID set 100% overlap, total polyline length bit-for-bit equal
  (974.5244414454263 deg), and native is 67% cheaper on the wire (esri-json more
  compact than geojson).

What hybridizes vs what does not:
- PRESERVED (page-1 + recovery re-fetch through transport): status-int,
  verbatim-body, never-silent, retry-authority, Retry-After.
- **Residual DEGRADE, must be accepted to collapse:** a 200-wrapped ArcGIS error on
  an INTERIOR driver page (N>1) is invisible to OGR and indistinguishable from an
  honest end-of-features by a count check. GDAL emits a CPLError string, not
  int+body+Retry-After, on interior pages. A pre-flight guard structurally cannot
  reach GDAL-issued interior requests -- it only sees request #1.

Scope reality that caps the prize: only **1 of 24 promoted specs** (epa_ejscreen,
the sole `esri_json:true` source) benefits. OGR's GeoJSON driver does NOT auto-page
ArcGIS `f=geojson` responses (verified: resultRecordCount=5 stayed 5 rows), so the
other 10 ArcGIS-paging specs gain nothing. Separately, `_fetch_one_page` (L584)
today runs its OWN bare `httpx.Client(timeout=60)` with zero retry authority,
bypassing transport -- a pre-existing gap independent of the GDAL question, worth
fixing regardless.

Verdict: PARTIAL. The guard is ~40-50 LOC replacing ~164 LOC of hand paging for
that one spec (net ~-115 LOC) IF NATE accepts the interior-page degrade. Under a
strict never-silent reading, do NOT collapse. The esri ring-decode (~90 LOC) is a
separate, larger prize but carries an indistinguishability caveat (OGR native
ring->polygon decode may not be byte-identical to our winding-repair /
degenerate-ring-drop; verify feature bytes first).

### transport/ (opener, range_file, client, errors) -- NOT A CANDIDATE
630 LOC, 0% deletable even if all three raster/vector modes collapsed. Every
function is shared by direct_window, stac_search/stac_float, imageserver_export,
and PC-sign -- none of which are collapse candidates. GUARD dealbreaker: collapsing
the coalescing opener to `/vsicurl/` chunk-merging loses all four contract
properties (TransportTruncatedError length-assert has no vsicurl equivalent;
GDAL #12933 ReadMultiRange-no-retry still OPEN; #11552 / #12426 only partially
fixed in 3.12.0; vsicurl yields CPLError strings, not int+body+Retry-After).
This opener IS the contract anchor keeping GDAL off the socket.

---

## Conflict resolution: when OGR ArcGIS paging works (NATE's earlier FAIL vs SUCCESS)

Root cause: OGR's FeatureServer auto-pagination lives entirely in OGR's native
HTTP datasource path. A bare `https://` string passed to `pyogrio.read_dataframe`
is silently prefixed with `/vsicurl/` (generic byte-range file access) and opens
the ESRIJSON driver in static-file mode with ZERO pagination awareness -- one
query, silently capped at server maxRecordCount (2000), no `exceededTransferLimit`
surfaced, no error. Auto-paging fires reliably IFF all of:
- (a) recognized as ESRIJSON -- `ESRIJSON:` prefix (or `-if ESRIJSON`); `f=json`,
  NOT `f=geojson` (geojson routes to the GeoJSON driver = no paging);
- (b) URL carries NO explicit `resultOffset` (its presence flips auto-paging OFF
  unless `FEATURE_SERVER_PAGING=YES`, which #10094/PR#10095 showed was itself buggy
  on ArcGIS Online -- rely on the no-resultOffset default);
- (c) `orderByFields=OBJECTID ASC` for stable ordering. Without it BENCH measured
  6923 rows but only 5505 unique OBJECTIDs -- order-unstable cross-page dupes/skips.
  A native-ESRIJSON lint/wrapper MUST propagate each spec's `ingest.query_template.
  order_by`, not just the prefix;
- (d) server >= 10.3, supportsPagination=true.

So NATE's earlier FAIL carried the hand-loop's resultOffset/resultRecordCount (or
f=geojson, or no orderByFields) -> single page. The later SUCCESS used a clean
auto-page URL -> full count. NIFC's still-earlier "success" was luck: live count
(233) < server page cap, so a single non-paginated pull matched full count whether
or not pagination logic ran. GDAL version is NOT the cause -- 3.12.4 and 3.10.3 both
show the identical prefix-dependent split.

---

## Net collapse estimate (LOC, honest sign)

- multi_url VRT: **0** (KEEP -- never-silent unpreservable)
- gzip_object: **0** (KEEP -- zero windowing gain + retry lost)
- vsizip_member: **0** (KEEP -- zero windowing gain, DEFLATE non-seekable)
- vector_fgb: **0** under strict never-silent; **~-115** for the single
  epa_ejscreen spec ONLY if the interior-page 200-error degrade is accepted.

Net: **~0 LOC under the strict contract; at most ~-115 LOC** (one spec, one
accepted degrade). The guard/bench "collapse looks cheap" headline is real only
for the happy path and one of 24 specs -- the transport contract is the reason
three of four candidates stay.

---

## What this does NOT change

- transport/ for API fetchers -- the httpx opener (ADR-0044), retry authority,
  Retry-After, TransportTruncatedError length-assert. Shared, un-collapsed.
- stamps, gates, publish_layer, the honesty floor.
- The spec-interpreter architecture -- modes stay as declarations dispatched by
  the router; no mode becomes a "just a spec field" that hands raw URLs to GDAL.
- The offline/monkeypatched tests that exercise exactly the GDAL-can't-reproduce
  behaviors (test_multi_url_mosaic_pastes_members, ..._member_read_failure_is_upstream,
  ..._all_nodata_window_is_empty, gzip 404/sentinel/all-nodata cases,
  test_esri_json_geometry_envelope_is_json, etc.) -- none deleted, since the modes
  they cover are KEEP/PARTIAL, not COLLAPSE.

Any future COLLAPSE (only vector_fgb is even eligible, and only as a degrade-
accepting hybrid) runs the standard two-gate parity recipe before landing.
