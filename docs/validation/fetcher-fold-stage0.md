# Fetcher fold, Stage 0 - THE TRADE, measured

Charter: `docs/validation/fetcher-fold-census.md` section 6 ("Stage 0 - THE TRADE").
Rulings: `docs/IDEAS.md` FETCHER FOLD RULED / STAC FOLD / FETCHER FOLD, SECOND HALF.
Nothing folds in this slice. Every number below is measured in `venvs/agent` on this
machine. The probe scripts and their raw output are session artifacts under
`scratchpad/fetcher-fold/probes/`; the verbatim bodies they produced are reproduced
here so the cell stands on its own.

Environment: Python 3.12.13, rasterio 1.5.0 (GDAL 3.12.1), pyogrio 0.13.0
(GDAL 3.12.4), odc-stac 0.5.3, odc-geo 0.5.3, planetary-computer 1.0.0,
osmnx 2.1.1, pygeohydro 0.19.4, pynhd 0.19.3.
Registered tool count: **161**, unchanged (`tests/test_catalog_surfacing.py`, 9 passed).

## Verdict table

| cell | verdict | measured reason |
|---|---|---|
| **A1** STAC read through `odc.stac.load` under the GDAL retry policy | **PASS** | `cop-dem-glo-30` over a Tampa Bay bbox: 1 item, 360x360 float32, 129,600/129,600 finite, min -2.173 max 17.218, 5.36 s |
| **A2** the retry policy REACHES odc's read | **PASS** | `rasterio.open` spied inside `odc.loader`: at open time `MAX_RETRY=5 RETRY_DELAY=1 RETRY_CODES=429,500,502,503,504`. Unconfigured, odc applies its own `GDAL_CLOUD_DEFAULTS` (10 / 0.5 / unset) |
| **A3** upstream STATUS verbatim | **PASS** | `RasterioIOError: HTTP response code: 404` / `403` / `409`, plus `rasterio._env` DEBUG `VSICURL: GetFileSize(<url>)=0 response_code=404`. The `raster_cog.py:258-261` regression (status discarded by `/vsicurl/`) does **not** reproduce on GDAL 3.12.1 |
| **A4** upstream MESSAGE verbatim (Probe A's stated PASS bar) | **FAIL** | The S3 XML `<Code>NoSuchKey</Code>` body never reaches Python. `CPL_CURL_VERBOSE=YES` + `CPL_VSIL_CURL_USE_HEAD=NO` recovers only Azure's `x-ms-error-code: AuthenticationFailed` **header** (2 hits in 105 log records); zero hits for the S3 body |
| **A5** retry on 429 / 503 | **PASS** | local origin, 2 failures then 200: recovered both times, `HTTP error code: 429 - <url>. Retrying again in 1.0 secs` then `2.4 secs` |
| **A6** Retry-After honored | **FAIL** | measured and source-confirmed - see below |
| **B1** ESRIJSON read, paging, parity | **PASS** | see below |
| **B2** pyogrio config isolation | **CONFIRMED** | inside `rasterio.Env(GDAL_HTTP_MAX_RETRY='99')`: rasterio sees 99, pyogrio sees 4. Two GDALs, two config stores |
| **B3** pyogrio status verbatim | **PASS** | `pyogrio.errors.DataSourceError: HTTP response code: 404` / `403` |
| **B4** pyogrio ESRI error envelope | **FAIL** | a 200 carrying `{"error": {...}}` becomes `Failed to read ESRIJSON data; Invalid FeatureCollection object. Missing 'features' member.` - the upstream message is lost |
| **B5** retry / Retry-After on GDAL 3.12.4 | **PASS / FAIL** | retries yes, identical gaps to 3.12.1; Retry-After not honored |
| **C1** OSMnx 429 path | **PASS** | provoked live against a local Overpass stand-in: `'127.0.0.1' responded 429 Too Many Requests: we'll retry in 55 secs`, recovered, wall 55.1 s |
| **C2** OSMnx Retry-After | **FAIL (accepted, per ruling)** | origin sent `Retry-After: 3`; OSMnx waited **55.0 s** - the hardcoded `error_pause` at `osmnx/_overpass.py:479` |
| **C3** OSMnx mirror chain + verbatim error | **PASS (as shims)** | both written and proven working; **62 LOC** measured (46 non-blank) against the census's ~40 budget |
| **C4** HyRiver: NWIS / STNFloodEventData / NID answer a real query | **PASS** | 3 for 3, cache expiry set and honored |
| **C5** HyRiver: `pynhd.NLDI` answers a real query | **FAIL** | `NLDI()` cannot even be constructed at the pinned version - see below |
| **C6** HyRiver: `status_to_retry` at the call site | **FAIL** | the parameter does not exist on the path those three classes use |
| **C7** HyRiver: verbatim upstream error | **SPLIT** | verbatim when the read raises; **silently returned as data** when the error body is valid JSON |
| **D** OPERA DSWx loaded through `odc.stac.load` | **FAIL (blocked, not refuted)** | collection ids, items, asset keys and scene dates all measured; the asset read needs Earthdata Login, which is an interactive step |

**Consequences under the charter's own gates, read mechanically.**

- **Probe A FAILS its stated bar** ("PASS only if the upstream status AND message are
  readable verbatim from our log"). The status half is green on both builds; the
  message half is not, on either. Under the gate as written, **F1 is held to the
  custom reader driver fallback** (census Q2 option (a): a `ReaderDriverSpec`
  wrapping `open_windowed_cog`), not to option (b). Recorded, not decided: the
  narrower reading - that the regression the census actually named (the lost
  404 -> typed-EMPTY split) does NOT reproduce, so option (b) survives - is a
  judgment beyond the census's recommendations and is raised rather than taken.
- **Probe B PASSES its stated bar** (paging across more than one page proven, feature
  count equal to the hook's, wall time measured). G1 / G2 / G10 are not held to
  `ArcGISRESTful`.
- **OSMnx (G3) is green** on the ruling's own terms: the 429 path recovers, the 55 s
  is documented, both shims are written and measured.
- **The HyRiver leg (G5 / G6 / G7) is red at the pin the ruling names**, in a way the
  census did not anticipate: `pynhd.NLDI` cannot be constructed at all, and the retry
  kwarg the ruling requires does not exist on the paths `NWIS` / `STNFloodEventData` /
  `NID` use. Both are raised, not resolved.
- **OPERA DSWx (Stage 1's proving consumer) is blocked** on an interactive credential
  step, which is NATE's and never an agent's.

---

## Probe A - rasterio's GDAL 3.12.1

`a1_stac_load.py`, `a1b_env_reaches_read.py`, `a2_error_channel.py`,
`a2b_body_recovery.py`, `a4_retry_after.py`.

### A1/A2 - the happy path and the config seam

```
EFFECTIVE_RASTERIO_ENV= {"GDAL_HTTP_MAX_RETRY": 5, "GDAL_HTTP_RETRY_DELAY": 1,
                         "GDAL_HTTP_RETRY_CODES": "429,500,502,503,504"}
SEARCH items=1 t=1.44s
ITEM_0_ID= Copernicus_DSM_COG_10_N27_00_W083_00_DEM
LOAD shape=(360, 360) dtype=float32 t=5.36s min=-2.173 max=17.218 finite=129600/129600
```

The seam is `odc.loader.configure_rio`, not an enclosing `rasterio.Env`: odc opens its
OWN env per read from a module-global config (`odc/loader/_rio.py:201-229`). Proven by
spying on `rasterio.open` inside the loader:

```
OPENS= 1
CONFIG_AT_OPEN= {"GDAL_HTTP_MAX_RETRY": 5, "GDAL_HTTP_RETRY_DELAY": 1,
                 "GDAL_HTTP_RETRY_CODES": "429,500,502,503,504"}
CONFIG_AT_OPEN_UNCONFIGURED= {"GDAL_HTTP_MAX_RETRY": 10, "GDAL_HTTP_RETRY_DELAY": "0.5",
                              "GDAL_HTTP_RETRY_CODES": null}
```

The unconfigured row matters: odc-stac already retries 10 times at 0.5 s **without
`RETRY_CODES`**, i.e. on GDAL's hard-coded default set. Silence is not "no retry".

### A3/A4 - the error channel, three real failures

| case | URL | exception | log |
|---|---|---|---|
| 404 | `noaa-hrrr-bdp-pds.s3.amazonaws.com/definitely-not-a-key.tif` | `RasterioIOError: HTTP response code: 404` | `VSICURL: GetFileSize(...)=0  response_code=404` |
| 403 | the Copernicus DEM blob with a bogus SAS signature | `RasterioIOError: HTTP response code: 403` | `... response_code=403` |
| 409 | the same blob unsigned | `RasterioIOError: HTTP response code: 409` | `... response_code=409` |

Each also emits `rasterio._env INFO: GDAL signalled an error: err_no=11, msg='HTTP
response code: NNN'`.

What the origins actually said, and what GDAL kept:

```
upstream 404 body: <Error><Code>NoSuchKey</Code><Message>The specified key does not exist.</Message>...
upstream 403 body: <Error><Code>AuthenticationFailed</Code><Message>Server failed to authenticate...
                   <AuthenticationErrorDetail>Signature size is invalid</AuthenticationErrorDetail></Error>
GDAL keeps:        the status integer, and nothing else
```

Body-recovery attempts, measured:

```
404_s3     verbose+GET    records= 96 body_token_hits=0
404_s3     verbose+HEAD   records=  9 body_token_hits=0
403_azure  verbose+GET    records=105 body_token_hits=2   -> CURL_INFO_HEADER_IN: x-ms-error-code: AuthenticationFailed
403_azure  verbose+HEAD   records=  9 body_token_hits=0
```

So: **status yes, body no.** `transport/opener.py`'s S3 `<Code>` recovery has no GDAL
equivalent, and buying the Azure header costs a ~100-record-per-read debug firehose.

### A5/A6 - retry and Retry-After

Local origin, `Retry-After: 6` on the wire, `GDAL_HTTP_RETRY_DELAY=1`,
`MAX_RETRY=4`, two failures then success:

| path | arrivals (status) | gaps (s) | Retry-After sent |
|---|---|---|---|
| `/t429.tif` | 429, 429, 200 | 1.00, 2.42 | 6 s |
| `/t503.tif` | 503, 503, 200 | 1.04, 2.39 | 6 s |
| `/t429_nohdr.tif` | 429, 429, 200 | 1.04, 2.46 | none |

The gaps are the same with and without the header, so the header is not read.
The throttle was staged on a local origin rather than provoked out of a real service:
deliberately driving a public endpoint into 429 is the behaviour the upstream-provider
norm exists to prevent. The 404 / 403 / 409 cases above ARE real objects on real
services.
Source-confirmed at `port/cpl_http.cpp` v3.12.4, `CPLHTTPGetNewRetryDelay`:

```c
// Use an exponential backoff factor of 2 plus some random jitter
return dfOldDelay * (2 + rand() * 0.5 / RAND_MAX);
```

There is no `Retry-After` read anywhere in that function or file. **`Retry-After` is
not honored by GDAL's HTTP retry, on either build.** The `Retry-After:` string that
appears in pyogrio's `libgdal` and not in rasterio's is **not** a GDAL feature
difference: pyogrio statically links libcurl into its libgdal (no separate
`libcurl*.so` in `pyogrio.libs/`, one in `rasterio.libs/`), and the string sits
between `Proxy-authenticate:` and `Strict-Transport-Security:` - it is libcurl's own
header table. The census's "documented, not source-verified" cell is now
source-verified, in the negative.

**Consequence for the norm.** `transport/client.py:5-8` honors `Retry-After` at BLOCK
granularity. GDAL cannot, so every folded STAC spec trades a header-driven wait for an
exponential-with-jitter one (1 s -> ~2.4 s -> ~6 s at `RETRY_DELAY=1`). It retries the
right codes and it recovers; it just does not obey the server's number.

---

## Probe B - pyogrio's GDAL 3.12.4

`b1_retry_after.py`, `b2_esrijson.py`, `b3_error_channel.py`.

### B1 - the two config stores

```
CROSS_BUILD_LEAK rasterio_sees= 99  pyogrio_sees= 4
```

Set inside `rasterio.Env(GDAL_HTTP_MAX_RETRY='99')` while pyogrio held 4. The census's
two-GDAL finding is reproduced: policy must be applied through
`pyogrio.set_gdal_config_options` for the vector lens and `odc.loader.configure_rio`
for the raster lens. Retry behaviour itself is identical across the builds
(gaps 1.00/2.42, 1.04/2.39, 1.04/2.46 - the same three rows as A5).

### B2 - ESRIJSON against a live FeatureServer, beside the current hook

Request: NWI Wetlands MapServer layer 0, bbox `(-81.5, 26.0, -81.3, 26.2)` (Naples,
FL). The service reports `count=2683`, `maxRecordCount=1000`.

```
HOOK   features=2683 pages=3 wall=66.20s cols=['acres', 'attribute', 'wetland_type']
DRIVER features=2683           wall=69.31s
COUNT_MATCH=True  ratio_wall=1.05x
AREA_SORTED_MAX_ABS_DIFF= 5.553585992771823e-12
```

Paging proven from GDAL's own requests - four in total:

```
1 .../query?...&f=json
2 .../query?...&f=json&returnCountOnly=true
3 .../query?...&f=json&resultRecordCount=1000&resultOffset=1000
4 .../query?...&f=json&resultRecordCount=1000&resultOffset=2000
Mem: 1000 features read on layer 'ESRIJSON'   (x2)
Mem:  683 features read on layer 'ESRIJSON'
```

**Latency is not the problem the census expected.** It measured 26.5 s for 947
features against a pooled httpx client; here the driver is **1.05x** the hook on the
same request, because both are dominated by the upstream service. The extra
`returnCountOnly` round trip is the driver's whole overhead.

The WAF header trio the hook carries is expressible as GDAL config and was required:

```python
GDAL_HTTP_USERAGENT = "<the hook's browser UA>"
GDAL_HTTP_HEADERS   = "Accept: application/json, text/plain, */*\r\n"
                      "Referer: https://www.fws.gov/program/national-wetlands-inventory"
```

**Field mapping the fold must state.** The driver returns the join's raw column names;
the hook's prefix-strip first-wins normalizer picks these:

| spec column | driver column |
|---|---|
| `attribute` | `NWI_Wetland_Codes.ATTRIBUTE` |
| `wetland_type` | `Wetlands.WETLAND_TYPE` |
| `acres` | `Wetlands.ACRES` |

`f=json` (Esri JSON) is what the ESRIJSON driver wants; the hook asks for
`f=geojson`. Geometry survives the change: sorted polygon areas agree to 5.6e-12.
Ten further columns arrive that the spec drops (`OBJECTID`, `GLOBALID`,
`Shape_Length`, `Shape_Area`, the `NWI_Wetland_Codes.SYSTEM*` set) - the fold keeps
`apply_column_map` as the frame normalizer, exactly as the census projected.

### B3/B4 - the vector error channel

```
404_s3_NoSuchKey:               pyogrio.errors.DataSourceError: HTTP response code: 404
403_azure_AuthenticationFailed: pyogrio.errors.DataSourceError: HTTP response code: 403
esri_error_envelope:            pyogrio.errors.DataSourceError: Failed to read ESRIJSON data;
                                Invalid FeatureCollection object. Missing 'features' member.
```

The third row is the honesty-floor case for G1. An ESRI service answers a bad query
**200 with `{"error": {"code": 400, "message": ...}}`**; the hook raises
`NWI query returned error envelope: {...}` verbatim
(`fetch_nwi_wetlands/hooks.py:76`), the driver produces a generic parse complaint with
the upstream message gone.

---

## Probe C - OSMnx

`c1_osmnx.py`, `c1b_osmnx_shims.py`, `osmnx_shims.py`, `overpass_origin.py` (session artifacts).

### C1/C2 - the 429 path, provoked

```
'127.0.0.1' responded 429 Too Many Requests: we'll retry in 55 secs
C1a 429_RECOVERED rows=2 wall=55.1s arrivals=[429, 200] gap_after_429=[55.0s]
                                          retry_after_header_sent=3s
```

The origin asked for 3 s; OSMnx waited 55.0 s. That is `error_pause = 55` at
`osmnx/_overpass.py:479`, hardcoded, reached only for `{429, 504}`. Recovery works.
Per the ruling this is accepted and documented rather than fixed.

### C3 - the two shims, written and proven

```
C1b MIRROR_CHAIN rows=2 wall=0.16s mirrors_tried=2 first_dead=500
C1b VERBATIM_ERROR status=500 msg='Overpass 500 from http://127.0.0.1:.../api/interpreter
                                   BODY: <html>mirror down</html>'
```

**Measured size: 62 LOC** (46 non-blank) for both together, against the census's
~30 + ~10. The error shim is load-bearing beyond restoring the body: OSMnx's
`_parse_response` (`osmnx/_http.py:291-320`) raises **only when the body fails to
parse as JSON**. A non-OK response carrying valid JSON is returned as data, so an
Overpass JSON error envelope would reach the router as a zero-element result - an
honest-empty layer where an upstream error belongs. The shim's check is
`if not resp.ok and resp.status_code not in (429, 504)`, so it cannot swallow the
429 path OSMnx handles itself.

---

## Probe C (continued) - HyRiver

`c2_hyriver.py`. Cache pinned to
a scratch directory via `HYRIVER_CACHE_NAME` +
`HYRIVER_CACHE_NAME_HTTP`, expiry `HYRIVER_CACHE_EXPIRE=86400` (1 day).

```
CACHE_DIR   <scratch>/hyriver-cache
CACHE_FILES ['aiohttp_cache.sqlite(147456B)']
pygeohydro.NWIS:               PASS rows=3 cols=['USGS-02306774'] first=0.821  wall=1.70s
                               (second run 0.03s - the cache HIT path)
pygeohydro.STNFloodEventData:  PASS rows=22 cols=54 query_param_set=7
pygeohydro.NID:                PASS suggestions=2
                               code_tables_on_the_object=['dam_type','dam_purpose','fields_meta']
pynhd.NLDI:                    FAIL TypeError: string indices must be integers, not 'str'
```

The three PASS rows are the ruling's own deciding reason, confirmed: NID really does
ship `dam_type` / `dam_purpose`, STN really does ship the 7-key `hwms_query_params`
set. Two live SQLite caches exist (`aiohttp_cache.sqlite` for the
`async_retriever` path, `http_cache.sqlite` for `pygeoogc.RetrySession`); only the
first was touched by these four queries.

### C5 - `pynhd.NLDI` is broken at the pinned version

```
File ".../pynhd/nldi.py", line 47, in __init__
    self.valid_chartypes = {r["type"]: r["typeName"] for r in resp[0]}
TypeError: string indices must be integers, not 'str'
```

Root cause, measured against the live service:

```
GET https://api.water.usgs.gov/nldi/linked-data  -> 200
GET https://api.water.usgs.gov/nldi/lookups      -> 404
   {"type":"about:blank","title":"Not Found","status":404,"detail":"Not Found"}
```

`NLDI.__init__` calls `/lookups` unconditionally, gets the 404 problem document back
**as data** (see C7), and iterates the dict - yielding its string keys. Every NLDI
call site fails at construction. pynhd **0.20.0 removed that `/lookups` call**
(its `__init__` keeps only the `/linked-data` request), so the fix exists upstream -
but `pygeohydro==0.19.4` caps `pynhd<0.20,>=0.19.3`, and 0.19.4 is the newest
pygeohydro on PyPI. **The pinned pair the ruling names cannot produce a working NLDI.**

### C6 - `status_to_retry` does not reach the three that matter

```
async_retriever.retrieve_json PARAMS
  ['urls','request_kwds','request_method','limit_per_host','cache_name','timeout',
   'expire_after','ssl','disable','raise_status']
STATUS_TO_RETRY_ACCEPTED_BY_AR   False
RETRYSESSION_DEFAULT_STATUS_TO_RETRY  (500, 502, 504)
EXPIRE_AFTER_DEFAULT                  604800
```

`NWIS`, `STNFloodEventData` and `NID` fetch through `async_retriever.retrieve_json` /
`retrieve_text` (`pygeohydro/nwis.py:154,406,588`, `stnfloodevents.py:281,441`,
`nid.py:67,311`), not through `pygeoogc.RetrySession`. `RetrySession` is used at
exactly five sites across the whole stack, none of them on these paths. And
`async_retriever` **has no retry logic at all**: `grep -n 'retry|backoff|attempt|429|503'`
over `async_retriever/*.py` returns nothing outside the package name. So the ruling's
"`status_to_retry=(429,500,502,503,504)` at every call site" is not expressible at the
call sites it is meant for - there is no retry there to configure.

Two corrections to the census's cells while here: the HyRiver cache default is
`EXPIRE_AFTER = 604800` (7 days), **not** never-expire; and `RetrySession`'s default
`status_to_retry` is `(500, 502, 504)` as documented.

### C7 - the verbatim-error cell is split, and the wrong half is silent

`async_retriever` builds its session with **no `raise_for_status`**
(`async_retriever.py:96-101, 128-133`; only `streaming.py:78` sets it). `raise_status`
fires only on a `ClientResponseError` or a failed body read. Measured:

```python
ar.retrieve_json(['https://api.water.usgs.gov/nldi/lookups'], raise_status=True)
-> [{'type': 'about:blank', 'title': 'Not Found', 'status': 404, 'detail': 'Not Found'}]
```

A 404 returned as data, with `raise_status=True` asked for. When the read DOES raise,
the census's cell is correct and the norm is met -
`ServiceError` is `f"URL: {url}\nERROR: {err}\n"` over `await response.text()`
(`async_retriever/exceptions.py:38-44`), the full upstream body verbatim. The failure
mode is the silent one: any upstream error whose body happens to be valid JSON becomes
a value.

---

## Probe D - OPERA DSWx over Earthdata STAC

`d1_opera_dswx.py`.

**Collection ids.** CMR concept ids and the STAC-API ids differ; the STAC catalog
`https://cmr.earthdata.nasa.gov/stac/POCLOUD` wants the second column:

| product | CMR short name / concept id | STAC collection id |
|---|---|---|
| DSWx-HLS | `OPERA_L3_DSWX-HLS_V1` / `C2617126679-POCLOUD` | `OPERA_L3_DSWX-HLS_V1_1.0` |
| DSWx-S1 | `OPERA_L3_DSWX-S1_V1` / `C2949811996-POCLOUD` | `OPERA_L3_DSWX-S1_V1_1.0` |

**Items over a US basin, measured.**

```
DSWx-HLS  bbox=[-82.2,26.8,-81.8,27.2] (Peace River / Charlotte Harbor, FL)
          window 2026-07-01/2026-09-08 -> items=5, search 1.00 s
  ITEM  OPERA_L3_DSWx-HLS_T17RML_20260701T155821Z_20260720T210147Z_S2C_30_v1.1
  DATE  2026-07-01 16:15:41+00:00
  ASSETS 1_B01_WTR 1_B02_BWTR 1_B03_CONF 1_B04_DIAG 1_B05_WTR-1 1_B06_WTR-2
         1_B07_LAND 1_B08_SHAD 1_B09_CLOUD 1_B10_DEM browse metadata
DSWx-S1   FL bbox -> items=0 in that window; widened to
          bbox=[-98,28,-88,33] (TX-LA gulf) over 2026-06-01/2026-09-08 -> items=3
          (CONUS-wide, same window -> items=3)
  ITEM  OPERA_L3_DSWx-S1_T15RWL_20260602T001755Z_20260602T115028Z_S1A_30_v1.0
  DATE  2026-06-02 00:17:55+00:00
  ASSETS 0_B01_WTR 0_B02_BWTR 0_B03_CONF 0_B04_DIAG browse metadata
         s3_0_B01_WTR ... thumbnail_0 thumbnail_1
```

The water mask is the `*_B01_WTR` asset; DSWx-S1 additionally publishes `s3_*` twins.
Asset key numbering differs between the two products (`1_` vs `0_`), so a spec cannot
hard-code it - the fold selects by the `_WTR` suffix.

**The load fails on auth, not on odc-stac.**

```
DSWx-HLS: LOAD_FAILED RasterioIOError: '/vsicurl/https://archive.podaac.earthdata.nasa.gov/
          podaac-ops-cumulus-protected/.../..._B01_WTR.tif' not recognized
  RAW_GET HTTPError: HTTP Error 401: Unauthorized
```

**The auth mechanism GDAL needs, measured from the challenge.** A HEAD is answered
`303` to a CloudFront signed URL carrying `A-userid=None`; a GET redirects to
`https://urs.earthdata.nasa.gov/oauth/authorize?client_id=...&app_type=401`. So the
protected DAAC does an OAuth round trip that ends in a per-user signed URL:

1. **netrc + cookie jar** - the mechanism this actually needs: `~/.netrc` with
   `machine urs.earthdata.nasa.gov login <user> password <pass>`, `GDAL_HTTP_NETRC=YES`
   (the default), plus `GDAL_HTTP_COOKIEFILE` and `GDAL_HTTP_COOKIEJAR` pointing at one
   writable path so the EDL cookie survives the redirect chain. A bare bearer token in
   `GDAL_HTTP_HEADERS` is the fragile alternative: GDAL forwards headers across the
   redirect and the CloudFront host rejects a stray `Authorization`.
2. **direct S3** - the `s3_*` assets under `s3://podaac-ops-cumulus-protected/`, read
   with temporary credentials from `https://archive.podaac.earthdata.nasa.gov/s3credentials`
   (itself EDL-gated, ~1 h lifetime, `us-west-2` only).

Anonymous access was tested and refused, so there is no unauthenticated route for the
V1 products:

```
https://podaac-ops-cumulus-protected.s3.us-west-2.amazonaws.com/...B01_WTR.tif -> 403
https://podaac-ops-cumulus-public.s3.us-west-2.amazonaws.com/?list-type=2      -> AccessDenied
```

Only the PROVISIONAL_V0 and CALVAL_V1 collections live in the public bucket; the
validated V1 products the ruling names do not.

**Verdict: FAIL, blocked on an interactive step.** Everything odc-stac and the STAC
catalog are responsible for is proven (search, item, assets, geometry, scene date);
the missing piece is an Earthdata Login account and a `~/.netrc` on this machine,
which is NATE's step, never an agent's.

---

## Suite

From the repo root with `venvs/agent`, globs unquoted, `env -u TRID3NT_CACHE_BUCKET`,
`-p no:cacheprovider --timeout=300 -q`, each slice its own invocation.

| slice | result |
|---|---|
| `tests/test_[a-e]*.py` | 1572 passed, 5 skipped (167.45s) |
| `tests/test_[f-o]*.py` | 4228 passed, 1 xfailed (49.44s) |
| `tests/test_[p-r]*.py` | 1660 passed, 1 skipped (196.24s) |
| `tests/test_[s-z]*.py` | 1467 passed, 5 skipped (306.53s) |
| `contracts/tests` | 425 passed (2.83s) |
| `plugin/tests` | 386 passed, 2 failed (21.69s) |

Zero failures across the five server slices and contracts. The two plugin reds are the
documented pre-existing Qt pair, `test_case_bbox.py::TestCaseBboxDock::test_case_bbox_dock_behaviors`
and `test_tool_picker.py::TestToolPickerQt::test_harness_green`, out of this wave's
scope and unchanged by it.

## What this slice changed in the tree

Two commits: the dependency declaration in `pyproject.toml`, and this cell.
The declaration: `odc-stac==0.5.3`, `odc-geo==0.5.3`,
`planetary-computer==1.0.0`, `osmnx==2.1.1`, `pygeohydro==0.19.4`, `pynhd==0.19.3`,
`pyogrio>=0.13,<1`. Installed into `venvs/agent`; `pip check` clean; no installed
package downgraded. New transitives: `odc-loader 0.6.4`, `hydrosignatures 0.19.3`,
`cachetools 7.1.8`, `h5netcdf 1.8.1`, `python-dotenv 1.2.3`. The only binding cap is
`pygeohydro`'s `pynhd<0.20,>=0.19.3`, pinned explicitly on both sides.
Registered tool count unchanged at 161. No fetcher spec, hook or executor touched.
