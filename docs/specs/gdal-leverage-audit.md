# GDAL leverage audit -- FOR NATE REVIEW

Question (NATE): "Leverage as much GDAL as possible where appropriate -- it is a
universal translator for nearly all the data we use -- will its use help us
abstract more boilerplate?"

Synthesis of three read-only lanes (PROBE = live probes, SWEEP = LOC census,
RISK = error-taxonomy). Claims cross-checked against source; where a live PROBE
result contradicts a SWEEP assumption, the probe wins and the conflict is flagged
inline. No code changed; this doc is the only file written.

## 1. Verdict

Yes, but narrowly and unevenly -- and the LOC math is dominated by one BLOCKED
chunk. GDAL is a universal translator for FORMATS and TRANSPORT (`/vsicurl/`
windowed reads, `/vsizip/` remote-zip members, driver decode), NOT for error
SEMANTICS. Our non-negotiable hard rule ("never silent dead-end") lives in error
semantics, so GDAL must not own the fetch boundary for HTTP-JSON-REST sources.
Concretely:
- Where GDAL is safe it is ALREADY landed: `raster_cog.py` direct-window is
  `/vsicurl/`-windowed today (verified L211-232) -- zero download-then-open
  boilerplate remains. Not a gap.
- The one clean, today-actionable NEW win is `/vsizip//vsicurl/` remote-zip
  members (~105 LOC, low risk, PROBE-validated live).
- The single biggest boilerplate chunk (`processing/` subprocess CLI, ~1300-1400
  LOC) is absorbable ONLY by adding the `osgeo.gdal` in-process binding, which
  PROBE proves is not importable and carries real ABI risk -- a dep decision, not
  a free win.
- The highest-fetcher-count vector family (ArcGIS) is OFF-LIMITS to GDAL
  delegation: OGR cannot see ArcGIS HTTP-200 `{error:{}}` envelopes.
- Honest safe-absorbable total TODAY: ~120-155 LOC (zip members + dead-code
  delete). SWEEP's ~1600-1700 headline is real only if the dep gate opens.

## 2. Leverage strata (ranked by opportunity size, risk annotated)

### S1. processing/ subprocess CLI -> osgeo.gdal in-process  [~1300-1400 LOC; BLOCKED-on-dep]
- Files (LOC verified): compute_hillshade 734, compute_contours 609,
  clip_raster_to_bbox 600, compute_colored_relief 490, compute_aspect 370,
  compute_slope 364 = 3167. Each does shutil.which + subprocess.run + gdaldem /
  gdal_translate / gdalwarp / gdal_contour (grep-confirmed); NONE import osgeo.
  ~40-50% of each file is binary-resolution + PROJ-env + subprocess.run +
  returncode->error mapping.
- Mechanism: osgeo.gdal in-process API (DEMProcessing / Warp / Translate /
  ContourGenerate) removes PATH search, env dict, and returncode plumbing;
  errors become exceptions.
- Lands as: executor rewrite + a DEP ADD (osgeo.gdal binding).
- CONTRADICTION (SWEEP vs PROBE, decisive): PROBE proves `osgeo.gdal` is NOT
  importable (ModuleNotFoundError); PyPI gdal==3.12.1 has no prebuilt wheel; the
  sdist needs gdal-config/native headers (absent) and risks an ABI clash with the
  bundled libgdal that rasterio/pyogrio already link. rasterio CANNOT substitute
  (it has WarpedVRT/COG but NO gdaldem hillshade/slope/aspect/roughness/TRI/TPI
  and no gdal_contour). So the largest prize is the most build-risky and is
  currently gated. The subprocess CLI works today.

### S2. remote-zip members via /vsizip//vsicurl/  [~105 LOC; LOW risk; DO NOW]
- Members: administrative_boundaries (~40), river_geometry HUC4 GDB zip (~40),
  storm_tracks (~20). Excludes gtsm_tide_surge -- see contradiction below.
- Mechanism: GDAL opens a shapefile/GDB member inside a remote zip directly, no
  local download+extract. PROBE validated live: `/vsizip//vsicurl/` on a Census
  TIGER zip read 5 features successfully.
- Lands as: a declarative router ingest mode ("remote_zip_member" open path),
  gated by both fold parity gates (sec 4).
- CONTRADICTION (SWEEP vs PROBE): SWEEP lists gtsm_tide_surge (netCDF-in-zip) in
  this stratum. PROBE proves the netCDF driver is ABSENT from both pyogrio and
  fiona (same bundled libgdal). So the netCDF member is NOT openable via the
  vector GDAL stack -> gtsm stays out of S2 (open question 2).

### S3. raster_cog direct-window error-taxonomy tightening  [~0 LOC absorbed; PRECISION rider]
- GDAL already landed here (SWEEP + PROBE + source agree: "0 gap"). This is NOT a
  boilerplate-absorption stratum; it is a correctness fix riding on the existing
  GDAL path. Verified L228-229: any exception -> router_upstream_error
  (retryable=True), so a 404/out-of-coverage collapses to UPSTREAM and LOSES the
  EMPTY / NOT_AVAILABLE (retryable=False) distinction the twin carries.
- Lands as: an executor patch (CPLGetLastErrorNo regex, see sec 3) that is
  actually a parity-IMPROVING change.

### S4. ArcGIS vector wire-protocol -> OGR ESRIJSON  [~164 LOC; BLOCKED; DO NOT PURSUE]
- vector_fgb.py ArcGIS protocol (~164 of 536 LOC: build_where, build_query_params,
  _fetch_one_page, paging-loop, fallback-chain, resolve_endpoints). SWEEP proposes
  collapsing it to one OGR GDALOpenEx/ogr2ogr call on the bare FeatureServer URL
  (auto-paginates resultOffset, decodes esri-json).
- CONTRADICTION (SWEEP vs RISK, PROBE-adjudicated -> RISK wins): ArcGIS signals
  errors as HTTP 200 + JSON `{error:{}}`. The httpx path catches this explicitly
  (verified vector_fgb.py L462-474: status>=400 raise, non-JSON raise, `"error"`
  envelope raise). OGR cannot: PROBE proved the bare FeatureServer/0 URL (no
  /query) FAILS outright ("DataSourceError JSON parsing error"), and a 200-with-
  error-JSON yields only a generic "not recognized as supported file format" with
  the ArcGIS error text SWALLOWED. So OGR either misclassifies (loses error_code +
  retryable) or, on a valid-but-empty envelope, emits an honest-empty FGB for a
  REAL failure -- a silent dead-end. Either way it breaks the hard rule.
- Verdict: keep httpx for the ArcGIS family. GDAL is unsafe as the FETCHER here.

### S5. fetch_topobathy._gdal_bin dead code  [~18 LOC; trivial cleanup]
- Verified: `_gdal_bin` (L731) has ZERO call sites (grep across server/src).
  Merge already runs in-process via rasterio.merge. Stale subprocess-CLI cruft;
  delete it. Not a delegation target.

## 3. Error-taxonomy preservation plan (per stratum; hard rule non-negotiable)

The router's dynamic error_code + retryable (errors.py `_stamp`) must stay
byte-identical to each twin. Plan:
- S1 processing/: local-only DEMProcessing, NO upstream -> always-internal error;
  returncode->exception is MORE faithful, not less. PRESERVABLE (once dep lands).
- S2 zip-members: reads are file-format opens over /vsicurl/. Wrap in
  gdal.UseExceptions() + a CPLSetErrorHandler capturing CPLGetLastErrorNo/Msg;
  regex `HTTP response code: NNN` to re-stamp router_upstream/empty with the
  twin's code + retryable. GUARD the fetch_dem masking bug: never blanket-set
  GDAL_DISABLE_READDIR_ON_OPEN / CPL_VSIL_CURL_ALLOWED_EXTENSIONS on an error-
  critical read without a recovery probe (verified fetch_dem.py L205-245 scopes
  AWS_NO_SIGN_REQUEST + readdir hints via rasterio.Env to THAT read only, because
  public-bucket signing failures otherwise surface as "does not exist" masking a
  real 403/404). PRESERVABLE.
- S3 raster_cog: split CPL error -> 404/403 => EMPTY / NOT_AVAILABLE
  (retryable=False) vs 429/5xx/timeout => UPSTREAM (retryable=True). Also honor
  429 Retry-After / exponential backoff in OUR wrapper (GDAL_HTTP_RETRY_DELAY is a
  fixed 30s, ignores Retry-After). PRESERVABLE -- and an upgrade over today.
- S4 ArcGIS-OGR: NOT PRESERVABLE. BLOCKED. Only unblock = keep httpx for the fetch
  and use GDAL/OGR SOLELY to decode already-fetched bytes (esri-json ring parser)
  -- GDAL-as-PARSER, not GDAL-as-FETCHER. That is a different, narrower use and
  does not need OGR at all (a pure-python ring decoder suffices).

## 4. Campaign fit (two-gate parity doctrine)

GDAL absorption is ORTHOGONAL to the fetcher-fold SPEC/HYBRID/BESPOKE classes
(SWEEP router_overlap: it is executor-implementation quality inside
vector_fgb/raster_cog, not which/how-many fetchers fold; no double-count with
fetcher-fold-audit.md). Both gates -- routing parity + replication parity (the
contract-4.2 twin-vs-router edge matrix) -- still apply to any router mode.
- S2 zip-member: RIDES the fold waves that migrate administrative_boundaries /
  river_geometry / storm_tracks; lands as a router ingest mode, gated by both.
  Not standalone.
- S1 processing/: STANDALONE. processing/ tools are not fetchers and are not in
  the fold classes; the rewrite is gated by the tool's own acceptance + a
  canonical-render V&V + the dep decision, NOT the two-gate fetcher parity.
- S3 raster_cog: a parity-improving rider; must pass the forced-404/429 edge
  matrix (which it would FAIL today). Land in any raster-mode wave.
- S4 ArcGIS-OGR: does NOT unblock the ADR 0039 wave-2 deferred trio.
  CONTRADICTION (SWEEP vs RISK/ADR 0039): SWEEP hoped OGR ESRIJSON closes
  epa_ejscreen "w/o a new mode." RISK's dealbreaker overrides -- epa_ejscreen is
  an ArcGIS esri-json source and inherits the 200-OK envelope hazard. ADR 0039
  defers the trio for router fan-out / multi-service / esri-json-ring-parser
  reasons, none of which GDAL supplies. epa_ejscreen CAN still fold, but as
  "keep-httpx + in-process ring decoder" (sec 3, S4), so GDAL does NOT change the
  wave-2 plan.

## 5. Honest NOT-GDAL boundary

GDAL owns nothing here: auth handshakes (CDS async-poll, FIRMS MAP_KEY, NID token,
vault Basic-Auth); station-timeseries REST loops (COOPS/ASOS/RAWS/SNOTEL);
Overpass QL + 3-mirror fallback; netCDF/xarray OPeNDAP time-reduce
(CDS/THREDDS/ERDDAP) -- and the bundled libgdal appears to LACK the netCDF driver
entirely (PROBE); 2-source JOIN business logic (openfema, census); name/entity
resolution (gbif/inat taxon); S3 cycle-key walkback; fire-detection/band-math;
AND the HTTP-JSON-REST error-envelope decision layer (the httpx status + body +
`{error}` checks GDAL cannot reproduce). GDAL is a format/transport translator,
not a business-logic or error-semantics layer.

## 6. Open questions for NATE

1. Add the osgeo.gdal binding (system libgdal + gdal==3.12.1 build) to unlock the
   ~1350-LOC processing/ in-process rewrite, accepting ABI-clash risk vs the
   bundled libgdal that rasterio/pyogrio already link -- or keep the working
   subprocess CLI and only trim its wiring? (PROBE: no wheel, sdist needs headers.)
2. Does the bundled libgdal ship ANY netCDF driver (raster/multidim)? PROBE only
   enumerated the VECTOR driver lists (netCDF absent there). If absent everywhere,
   gtsm_tide_surge's netCDF-in-zip member stays NOT-GDAL and gtsm stays xarray.
3. Is OpenFileGDB in the 64-driver set? S2 for administrative_boundaries +
   river_geometry (Esri FileGDB-in-zip) depends on it; PROBE named only 8 of 64.
4. Confirm the epa_ejscreen framing: keep httpx + in-process esri-json ring
   decoder (GDAL-as-parser), preserving never-silent -- i.e. GDAL does not alter
   the ADR 0039 wave-2 plan.
5. Land the S3 raster_cog 404->EMPTY/NOT_AVAILABLE precision fix now (it currently
   fails the forced-404 edge matrix) or defer to the next raster wave?
