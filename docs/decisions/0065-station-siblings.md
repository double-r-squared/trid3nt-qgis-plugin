# 0065 - station-siblings: the wave-4 deferred station family folds onto the EXISTING phases

Context: ADR 0045 (wave-4) deferred FIVE station-sibling twins with per-source
STOP-RULE evidence, each judged to need "a wholly new ingestion mode / auth
subsystem": fetch_asos_metar + fetch_raws_weather (multi-state discovery +
station-observations), fetch_snotel_snow (batched-snapshot + two router emission
gaps: station-extent bbox + degrade-to-locations), fetch_airnow_air_quality +
fetch_openaq_measurements (3-path api-key auth the router had no execution path for).
Two waves landed AFTER that deferral changed the calculus: ADR 0063/0064 built the
chained-resolution mode (PHASE R resolve, main fetch, ``next_page`` offset paging,
PHASE E bounded/deduped/best-effort per-item enrichment) and proved phases compose to
absorb new shapes with ~zero new machinery; the credentials wave (ADR 0062) landed the
resolver + env / TOOL_PROVIDER key path so ``api_key_env`` works headless. This wave
re-reads the five twins IN FULL against that surface and folds all five.

Decision (2026-08-01):

1. **All five FOLD via the EXISTING phases; NO new HookSpec field, NO new mode, NO new
   transport capability was needed.** The wave-4 "needs a new mode / auth subsystem"
   verdicts were superseded by the intervening waves, proven per source below. The
   ``output.bbox_from_features`` directive (ADR 0056) already fills wave-4 emission gap
   #1 (station-extent bbox); the chained-mode best-effort per-ref survival IS wave-4
   emission gap #2 (degrade-to-locations); the key resolves in a pure hook exactly as
   the twin resolved it (kwarg -> str secret_ref -> env), so gap #3 (auth) needs no
   router execution path.
   - **fetch_asos_metar** = PHASE R (multi-state per-state ASOS-GeoJSON discovery ->
     station ids, the storm_events resolve sibling) + main fetch (ONE bulk IEM CGI
     comma-CSV for all stations; ``parse_response`` -> one point per observation row).
     LIVE twin-vs-router 11 stations / 179 obs value-identical (station set, tmpf,
     valid). No new machinery.
   - **fetch_raws_weather** = PHASE R (multi-state DCP-GeoJSON RAWS discovery, state
     tagged by body order so obhistory ``network=STATE_DCP`` is correct) + main fetch
     no-op (stations synthesized from the resolved list) + PHASE E (per-station-per-day
     obhistory, best-effort, ``enrich_merge`` EXPANDS station features into one point per
     obs row; a failed station-day contributes no obs, all-empty -> RAWS_WEATHER_EMPTY).
     LIVE 10 stations (CA+NV) / 790 obs value-identical.
   - **fetch_snotel_snow** = main fetch (stations catalog GET, bbox-filter, spatial
     primary -> SNOTEL_NO_STATIONS) + PHASE E (ONE batched data GET for all triplets;
     ``enrich_merge`` folds latest non-null WTEQ/SNWD null-tolerantly; a failed batch
     keeps every station with null readings = the degrade-to-locations fallback) +
     ``output.bbox_from_features`` (station extent). LIVE 18 stations, swe/depth/date +
     station extent value-identical.
   - **fetch_airnow_air_quality** = http_json single bounded-box GET; the key resolves in
     ``build_request`` (kwarg -> str secret_ref -> ``TRID3NT_AIRNOW_API_KEY`` env);
     ``parse_response`` keeps the latest row per (lat, lon, parameter) + derived AQI
     columns, honest header-only FGB on empty.
   - **fetch_openaq_measurements** = offset paging (``/v3/locations`` sweep, ``next_page``
     stop-on-short-page / 2000-station cap) + PHASE E (per-location ``/latest`` fan-out;
     ``enrich_merge`` joins each latest sensor value to its parameter/units via the
     station's sensor map, bbox-hard-filters, EXPANDS to one point per (station, parameter)).

2. **Keyed sources folded under the AUTH RULE: never register a real key; the typed
   missing-key path is the parity surface.** airnow / openaq are NOT in TOOL_PROVIDER
   (the twins were not either), so the hook reading api_key/secret_ref/env is byte-identical
   to the twin's real runtime. Key-ABSENT parity proven byte-identical OFFLINE:
   AIRNOW_MISSING_KEY (``_MISSING_KEY`` suffix) and OPENAQ_KEY_REQUIRED (message-text
   credential detector, exactly as the twin's own OpenAQMissingKeyError message fires)
   are BOTH ``is_credential_shaped_error``=True, retryable=False, same error_code +
   surfacing the same NAME-only generic credential card (derived from the tool name, not
   the message). Input-validation errors (bad bbox, unknown pollutant) byte-identical.
   openaq's paging + enrich + sensor-join proven with synthetic bodies (2 correct
   measurement rows, param/bbox filtered). The LIVE DATA path is unprovable without a
   real key and is honestly BLOCKED-ON-KEY (not blocked-on-mode) -- the machinery
   composes; only the byte-equality of live data cannot be verified without a key.

3. **Metrics.** Coded tools 151 -> 146 (-5); coded fetchers 61 -> 56 (-5). Spec-served
   data sources 35 -> 40 (+5). Registry total unchanged (five twins died, five spec-driven
   surfaces took their names). Twin py + test LOC removed = 7,746; +1,206 hook LOC (+ 5
   ``source.yaml`` + 1 migrated test file ``test_router_stations.py``, 17 tests). Docstrings
   carried VERBATIM (``inspect.getdoc`` at fold time) + corpus.yaml untouched, so the
   retrieval index is UNSHIFTED: 37/38 corpus phrasings rank the tool in the model-free
   top-8 (the one openaq miss is a pre-existing index property of the identical document
   text, not a shift). ``test_catalog_surfacing`` spec-served count 35 -> 40, the arm2/arm3
   declarable delta -34 -> -39, the stratum tool count 34 -> 39 (the expected metric, not a
   regression). One consumer re-point: fetch_high_water_marks imported the asos twin's
   ``_STATE_BBOX`` / ``_bbox_overlaps_state``; re-pointed to a self-contained local table
   (ADR 0064c small-dup-for-clean-layering precedent -- a coded fetcher must not import
   router internals). Offline suite FAILED set == 9 exactly (the pre-existing
   test_fetch_resolution_gate x4 + test_run_river_dye_scenario x5; no new failure). Daemon
   import clean. NO flood coupling (grep-verified: no touched seam feeds sfincs/flood; the
   only match is a docstring cross-reference; router.py + executors untouched -- only hook
   modules ADDED), so NO flood canary run.

4. **HOOK-RATCHET update.** No NEW 2x+ recurring cross-source shape surfaced. Two
   composition patterns recurred and are noted (not yet ratchet-promotable): (a) the
   no-op main fetch (``build_request`` returns ``[]``, ``parse_response`` synthesizes
   features from a resolve-merged list) used by raws to run resolve + enrich without a
   redundant round trip; (b) the EXPANDING ``enrich_merge`` (station -> per-obs rows,
   station -> per-(station, parameter) rows) used by raws + openaq. Both are uses of the
   EXISTING enrich contract (``enrich_merge`` may return more/fewer features), not new
   machinery. All four ADR 0061 ratchets remain retired.

Non-gating divergences flagged (REPORTED, never fudged):
(a) **Keyed bad-key 401 -> UPSTREAM not AUTH (airnow/openaq).** A key that resolves but is
    rejected (HTTP 401) routes through the shared transport, which classifies the status
    BEFORE the parse hook, so it surfaces AIRNOW_UPSTREAM_ERROR / OPENAQ_UPSTREAM_ERROR
    (retryable) where the twin stamped AIRNOW_AUTH_ERROR / OPENAQ_AUTH_ERROR. Same class as
    ADR 0063 divergence (c) (river unknown-gauge 404). The MISSING-key path (the parity
    gate) is raised pre-network in the hook and IS byte-identical; the bad-key path is only
    reachable with a live key (unprovable this wave). For openaq the bad key fails on the
    FIRST locations request (main fetch), so the error surfaces fast, not swallowed.
(b) **Enrich best-effort more robust than twin abort (raws/openaq).** The chained PHASE E
    is best-effort per ref (a failed obhistory day / a failed per-location latest is skipped,
    the owning station survives); the raws twin skips per-station-date too (match), but the
    openaq twin ABORTS the whole fetch on any per-location latest failure. The router is
    arguably more correct (a partial layer over a hard failure); flagged, not copied.
(c) **Resolve/discovery is pre-cache-key (asos/raws).** Multi-state discovery runs in
    ``pre_resolve`` (before read_through), so the discovery GETs fire on every call incl.
    cache hits, and the resolved ``_station_ids`` + resolved window enter the cache key
    (the twin keyed on bbox + window only). Value-identical output; same class as ADR 0064
    divergence (e).
(d) **Keyed cache-key omits the hour window; api_key in key only on explicit kwarg
    (airnow/openaq).** The router computes the current-hour window in the hook (post-cache-key),
    so the cache key omits start/end -- but the ``dynamic-1h`` TTL vintage already buckets
    hourly (the twin's own docstring calls its start/end "belt-and-suspenders"), so caching is
    value-identical. An explicitly-passed api_key kwarg (drivers only) enters the router key
    where the twin excludes it; production (env key) never does.
(e) **Synthesized payload estimator + LayerURI cosmetics (all five).** per_feature estimate
    (bbox-only, cannot see the window/station count) stays well under the 25 MB warn for every
    realistic query; layer_id/name synthesized from source_class where the twins hand-built
    labelled strings (the FGB data is value-identical). Unchanged from every prior fold wave.

Consequence: the station-siblings family -- the wave-4 "needs a new mode / auth subsystem"
deferral -- folds onto the phases ADR 0063/0064 already built, with ZERO new engine
machinery, the THIRD proof that the router's phases compose to absorb new shapes. The
"coded fetcher count -> zero" endgame advances by five; the keyed-source pattern (fold under
the missing-key parity surface, never register a real key) is established. Supersedes the
wave-4 (ADR 0045) DEFER verdicts for these five sources; extends nothing in the hook contract.
