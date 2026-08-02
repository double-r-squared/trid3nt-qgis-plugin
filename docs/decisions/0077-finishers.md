# 0077 - Per-source finishers: movebank folded (keyed CSV + composite Basic-Auth); fault/landcover/flood_extent re-STOP with sharpened residuals

Context: ADRs 0068-0076 built the record shape, the post-emit envelope hook, the
generic library-delegate, and `delegate_resolve`, then STOP-RULEd four per-source
residuals waiting on their own small mechanisms: fetch_movebank_tracks (composite
Basic-Auth + a CSV parse hook + a consumer-schema diff), fetch_fault_sources (an
emptiness-driven output switch + a two-tier cache + a result-model migration),
fetch_landcover (a WCS GetCoverage access mode + a dict sidecar + palette COG +
auto-coarsen), and fetch_flood_extent_observation (a categorical tiled-mosaic raster
mode + a LANCE dir-walk resolve). This finishers wave reads every twin end-to-end,
FOLDS the one that fits the EXISTING keyed-http_json machinery with only pure hooks,
and re-STOPs the other three with residuals sharpened by the twin code -- each is a
wave-sized new-router-mechanism build whose value-identical parity cannot be proven in
this pass without either that mechanism or a live/flood-canary gate.

Decision (2026-08-01):

## FOLDED: fetch_movebank_tracks (keyed CSV http_json + composite Basic-Auth)

The Movebank direct-read twin maps cleanly onto the EXISTING keyed http_json path
(the ebird/iucn precedent, ADR 0071) with three PURE hooks and ZERO new router
machinery:

1. `movebank_tracks.build_request` -- resolves a (username, password) pair via the
   RESOLVER BLOB PATH (explicit kwargs -> a `user:pass` secret_ref blob ->
   `TRID3NT_MOVEBANK_USER` + `TRID3NT_MOVEBANK_PASSWORD` env), emits ONE GET carrying
   a computed `Authorization: Basic <b64(user:pass)>` header (composite Basic-Auth, the
   Movebank-specific credential surface httpx's `auth=(u,p)` gave the twin) + the
   `entity_type=event` / `study_id` / `attributes` / optional `sensor_type_id` /
   `timestamp_start`/`timestamp_end` query. The key is NEVER registered: a missing pair
   raises a credential-shaped `MOVEBANK_INPUT_ERROR` PRE-NETWORK (the keyed-fold parity
   surface, byte-identical to the twin's `MovebankInputError`).
2. `movebank_tracks.parse_response` -- the body is CSV (not JSON), so this parses the
   direct-read CSV into per-fix records (hyphen/underscore column-variant tolerant),
   applies the `max_records` cap, and shapes features by `geometry_type`: `point` = one
   Point per fix (individual_id/timestamp/sensor_type_id/study_id); `linestring` = one
   timestamp-ordered LineString per individual (n_points/first_timestamp/last_timestamp/
   study_id) dropping any individual whose track is not ENTIRELY in-bbox (the twin's
   conservative clip). A 200 licence-terms HTML body raises `MOVEBANK_LICENSE_ERROR`.
3. `movebank_tracks.classify_status` -- splits the transport status the executor would
   collapse: 401 -> `MOVEBANK_AUTH_ERROR`, 403 -> `MOVEBANK_LICENSE_ERROR`, other 4xx ->
   `MOVEBANK_INPUT_ERROR`, 5xx -> the default retryable upstream.

The per-`geometry_type` OUTPUT SCHEMA (incl. the honest-empty header-only FGB) is carried
by `ingest.properties_by_param` keyed on `geometry_type` (the existing usace_levees
precedent) -- no new machinery. `datetime_range` (ADR 0073 rider) carries `time_range`.

CONSUMER-SCHEMA DIFF (the ADR 0071/0073 residual): the two consumers
`compute_movement_trajectory` + `compute_home_range_kde` do NOT import the twin -- each
takes a `points_uri` string and reads the FGB by alias-picked columns (`individual_id`,
`timestamp`). The fold emits BYTE-IDENTICAL column schemas (verified by a twin-vs-router
parity harness), so NO consumer re-point was needed. Offline parity PROVEN: a synthetic
CSV run gives value-identical columns + geometry + properties on BOTH geometry types, an
identical empty-schema header, and identical `MOVEBANK_INPUT_ERROR` on missing creds. The
LIVE positive path is BLOCKED-ON-KEY (Movebank 401 without a registered account -- the
keyed-fold posture, ADR 0065). Docstring carried VERBATIM (4,593 chars); corpus untouched;
retrieval unshifted (7/7 corpus phrasings, model-free `retrieve_visible_tools` top-8).
Value coverage -> `test_router_movebank.py` (15 tests). Twin py + `test_fetch_movebank_tracks.py`
DELETED. `credential_registry` movebank provider (name-keyed) UNCHANGED. Catalog `n_specs`
62 -> 63; registry total UNCHANGED at 190 (one coded twin died, one spec-driven surface
took its name).

Non-gating divergences (movebank): (a) the twin required `isinstance(study_id, int)`
strictly; the router `int` ParamType coerces a numeric string (value-identical for a real
int study_id). (b) the router cache key includes the validated `username`/`password`
params when passed as kwargs (the twin keyed on `username` only); in the deployed agent
creds resolve from env (not kwargs), and the twin is deleted so cross-twin cache parity is
moot. (c) synthesized `layer_id`/`name` (the LayerURI-cosmetics divergence class).

## RE-STOP-RULED (each a wave-sized mechanism, sharpened by the twin read)

### fetch_fault_sources -- emptiness-switch + two-tier cache (both genuinely unbuilt)

Confirmed by reading the twin: the NON-empty path returns a `FaultSourcesResult`
(a LayerURI subclass with a categorical `LegendKey`, role=context) built from an
AOI-keyed vector `read_through`; the EMPTY path returns a bare dict. That is an
EMPTINESS-DRIVEN RUNTIME output switch, but `route()` chooses record-vs-LayerURI
STATICALLY (`output.layer_type == "record"`) -- no runtime switch exists. WORSE, the
twin does TWO `read_through`s: an inner CONSTANT-key fetch of the whole-world 10.6 MB GEM
GAF GeoJSON (downloaded once, filtered in-process per AOI) + an outer AOI-keyed vector
entry. The router's single-`read_through` `route()` + PURE (no-I/O) hooks cannot fetch a
constant-key source file inside the executor, so a naive fold would re-download 10.6 MB
per distinct AOI -- a caching REGRESSION, not a value-identical fold. Unblock (a wave):
a `output.variant_by_emptiness` (or an envelope-hook return convention that flips to the
record dict when the FGB is feature-empty) + a router-owned `ingest.constant_cache` tier
(an inner constant-key `read_through` of the source file feeding a pure AOI-filter hook)
+ `FaultSourcesResult` (with its legend) into `execution.py`'s `LAYER_RESULT_MODELS`.
`resolve_fault_sources` already tolerates dict-or-object (`.faults`/`.note` off either),
so its registry-seam re-point is trivial ONCE folded. GEM GAF is keyless + live-reachable
(HTTP 200), so the fold is live-provable the moment the mechanisms exist.

### fetch_landcover -- WCS GetCoverage + palette COG + a dict sidecar + a FLOOD CANARY

Confirmed (ADR 0068 residuals hold): needs (1) a WCS 1.0.0 `GetCoverage` templated-GET
raster access mode (a new `build_request` raster path via the ogc adapter, not the current
COG-window transport); (2) a `LayerURI`-plus-sidecar output carrying the SFINCS-consumed
`nlcd_vintage_year` / `effective_resolution_m` / `downsampled` fields -- the envelope hook
can now express extra fields, but the SFINCS consumer reads a specific attribute contract
that must be matched EXACTLY + a consumer re-point; (3) a NLCD background(0)->nodata pixel
remap + a palette-preserving COG serialize/parse; (4) an auto-coarsen effective-resolution
/ pixel-budget step + a `_LANDCOVER_CACHE_VERSION` salt. Folding TOUCHES the SFINCS seam,
so a fold is FLOOD-CANARY-GATED (run_sfincs_direct status=ok + depth COG). Heaviest +
flood-seam-coupled -> deferred whole (the SFINCS seam is left UNTOUCHED this pass, so NO
canary is owed). Unblock: the WCS mode + palette-COG serialize + the sidecar-envelope +
the SFINCS re-point + the mandatory canary (its own wave).

### fetch_flood_extent_observation -- categorical tiled-mosaic mode + LANCE dir-walk resolve

Confirmed (ADR 0073 residuals hold): the envelope closes class_breakdown/flood_area_km2/
LegendKey, but the fetch needs a NEW categorical tiled-mosaic raster access mode
(per-10-deg-tile GeoTIFF -> nearest-window -> FIRST-VALID-wins uint8 mosaic -> embedded
palette) that `fixed_tile_grid` (continuous NaN-merge) does not express -- a minimal
first-valid categorical merge VARIANT of `transforms/tiled_mosaic.py` -- PLUS a LANCE
directory-walk date resolve (latest year/doy) as a pre-resolve hook. V&V consumer
`compute_flood_extent_skill` couples by raster SHAPE in a docstring (no import) and neither
is on the sfincs/flood run path (grep-verified) -> non-fold breaks nothing, NO canary.
Unblock: the categorical tiled-mosaic mode + the dir-walk resolve (its own wave).

## Metrics

Coded fetchers -1 (movebank); coded tools -1. Spec-served data sources 62 -> 63 (+1).
Registry total UNCHANGED at 190. `test_catalog_surfacing`: n_specs 62 -> 63, arm2/arm3
declarable delta -61 -> -62, stratum tool count 61 -> 62 (the expected promote metrics,
not regressions). Offline baseline UNCHANGED at exactly 9 failures (test_fetch_resolution_gate
x4 + test_run_river_dye_scenario x5). No new router mechanism was added (movebank folded on
the existing keyed http_json + `properties_by_param` + `datetime_range` seams), so the fold
is STRICTLY no-op for the 62 priors (router hooks/executors/catalog suites green).

Consequence: the keyed http_json path now covers a COMPOSITE Basic-Auth source (username +
password resolved in-hook to an `Authorization: Basic` header via the resolver blob path,
the missing-creds typed error the parity surface) with a CSV parse hook + a per-param output
schema -- movebank folds with no coded twin. The remaining three finishers STOP on genuine,
now-precisely-named new-router-mechanism builds (an emptiness-switch + a constant-cache
tier; a WCS mode + palette COG + a flood-canary-gated SFINCS re-point; a categorical
tiled-mosaic mode + a dir-walk resolve), each its own wave. Extends the keyed-http_json
contract (ADR 0071) with the composite Basic-Auth resolver-blob path; supersedes the ADR
0071/0073 fetch_movebank_tracks STOP-RULE.
