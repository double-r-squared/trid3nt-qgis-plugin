# 0063 - chained-resolution mode: promote the 4x hook-ratchet into ONE declarative mode

Context: ADR 0061 flagged the CHAINED id/detail resolution shape as a HOOK-RATCHET
MANDATORY-REVIEW (rule 4, seen 4x): a first request resolves an id/handle (name->id)
or a list of item-refs, then a second bounded set of requests fetches the detail per
resolved ref, merged into the output. The two PURE tier-3 hooks (build_request /
parse_response) cannot make that intermediate I/O, so gbif / inaturalist /
nws_alerts_conus / nws_river_forecast were DEFERRED to a mode a later wave scopes.
This wave scopes and lands that mode, and folds all four.

Decision (2026-07-31):

1. **The chained-resolution mode = ONE executor + a pre-cache resolve seam, with
   TWO composable phases; a source declares only the phase(s) it needs.** The
   router owns ALL orchestration (round trips, the offset-paging loop, the
   deduped/bounded/best-effort detail loop, the honest per-ref error aggregation);
   the source-specific PURE compute lives in named hooks at each edge. The mode is
   MINIMAL and GENERAL: it serves all four without overfitting one, and reuses the
   shared transport + the ``vector_fgb`` serializer wholesale.
   - **PHASE R (resolve, name -> id), PRE-cache-key.** ``chained_resolution.pre_resolve``
     runs in ``router.route()`` BEFORE ``read_through``: ``resolve_build`` builds the
     resolution request(s) (or ``[]`` to skip when the id is already canonical),
     the router GETs them, ``resolve_parse`` returns a params-merge dict (the
     resolved id) folded into ``params``. Running pre-cache-key preserves the twin
     contract that a name query and its id query collapse to one cache entry.
   - **MAIN FETCH.** ``build_request`` builds page 1; the optional ``next_page`` hook
     drives offset paging (given the pages so far, return the next page's plan or
     ``None`` to stop -- the pure offset/endOfRecords/total_results loop control the
     declarative ``totalPages`` pager cannot express); ``parse_response`` decodes.
   - **PHASE E (enrich, list -> per-item detail).** ``enrich_plan`` emits the ordered
     ``(ref_key, RequestPlan)`` detail set (already sliced to the source's per-pass
     cap); the router dedupes by ``ref_key``, bounds by ``ingest.chained.max_detail_fetches``,
     and fetches each best-effort into a ``{ref_key: DetailResult}`` map (a failed ref
     records its typed error, never a silent drop, per the never-silent rule);
     ``enrich_merge`` folds the map back into the features -- every feature survives.

2. **Five new PURE HookSpec fields, each justified by a real target, no speculative
   knobs.** ``resolve_build`` / ``resolve_parse`` (gbif, inat), ``next_page`` (gbif,
   inat), ``enrich_plan`` / ``enrich_merge`` (nws_alerts_conus, nws_river_forecast).
   The bespoke per-source gates the declarative surface cannot carry stay in the
   source's own build/parse hooks (gbif's EXACT-match gate; river's bbox-too-large /
   gauge_id-format / no-gauges gates; the NSI-style span caps) -- the mode owns the
   ORCHESTRATION, a genuinely irreducible resolve step stays a pure hook. Two small
   general infra additions (strict no-op for every prior spec): a ``bool`` ParamType
   (river include_thresholds / include_series) and ``output.keep_null_geometry``
   (nws_alerts_conus preserves unresolvable-zone alerts as attribute-only rows,
   written with ``SPATIAL_INDEX=NO``).

3. **All four FOLDED, live edge-matrix parity PASS vs twin (12/12).** gbif (name
   resolve EXACT-gate + taxonKey fast-path + offset paging + bbox-clip; 17/17 &
   300/300 value-identical, empty header-only, unknown+fuzzy name -> byte-identical
   GBIF_INPUT_ERROR), inaturalist (name resolve + page paging; unknown ->
   INAT_INPUT_INVALID), nws_alerts_conus (event filter + zone-polygon enrichment;
   8/8 nationwide flood-warning value-identical incl. zone union, area-scoped +
   empty), nws_river_forecast (gauges list + bounded threshold enrichment; 100/100,
   gauge_id detail mode, empty-bbox -> NWS_RIVER_FORECAST_NO_GAUGES). Twins +
   4 twin test files DELETED (3,686 test LOC), value-bearing coverage migrated to
   ``test_router_chained.py`` (23 tests: mode primitive dedup/cap/best-effort,
   resolve gate, paging stop conditions, zone-union + keep-null, threshold enrich,
   honest errors). Docstrings + corpus carried VERBATIM, so the retrieval index is
   UNSHIFTED: 28/28 corpus phrasings rank the tool in the model-free top-8.

4. **Metrics.** Coded fetchers 67 -> 63 (-4); coded tools -4. Spec-served data
   sources 29 -> 33 (+4). Registry total unchanged at 190 (four twins died, four
   spec-driven surfaces took their names). ``test_catalog_surfacing`` spec-served
   count 29 -> 33, the arm2/arm3 declarable delta -28 -> -32, the stratum tool count
   28 -> 32 (the expected metric, not a regression). One consumer re-point: the
   SFINCS ``model_nws_flood_event_scenario`` composer imported the nws_alerts_conus
   twin's module directly (the tool + two private helpers); re-pointed
   ``fetch_nws_alerts_conus`` to ``TOOL_REGISTRY[...].fn`` and relocated the raw
   FeatureCollection read (``_fetch_nws_conus_geojson``, for warning-polygon
   SELECTION -- a distinct need from the tool's FGB render) into the workflow as a
   Case3Error-raising local; its test re-pointed to Case3Error. FLOOD CANARY green
   (scripts/run_sfincs_direct.py status=ok + depth COG published). Offline suite
   FAILED set == 9 exactly (the pre-existing test_fetch_resolution_gate x4 +
   test_run_river_dye_scenario x5; no new failure). Daemon import clean.

5. **HOOK-RATCHET update.** No NEW 2x+ recurring cross-source shape surfaced this
   wave. The chained mode's ``next_page`` hook now provides a hook-driven offset-paging
   PRIMITIVE the still-QUEUED offset-paging ratchet (openfema $skip/$top) can reuse
   when that source is folded; the declarative $skip/$top variant stays QUEUED
   (openfema not in scope). The remaining ratchets (boundary-service FIPS join,
   bulk-file-behind-an-index) are unchanged.

Non-gating divergences flagged (REPORTED, never fudged):
(a) **Resolve cache-key collapse (gbif/inat).** The twin resolves name->id then keys
    the cache on the resolved id only, so a name query and its id query collapse. The
    router keys on the validated ``species_key`` / ``taxon_id`` param (always present)
    PLUS the resolved id merged by pre_resolve, so a name query and its id query hit
    DIFFERENT cache entries (both fetch the identical occurrence/observation set --
    value-identical output; only a one-time double cache-warm differs). Same class as
    ADR 0061 divergence (d).
(b) **Stringified-int species_key (gbif).** The adapter collapses ``int | str`` to
    ``str``, so a taxonKey arrives as a digit string; the resolve hook treats a
    digit string as a taxonKey fast-path (skip the round trip) -- matching inat's
    ``_coerce_taxon_id`` and the realistic agent surface. The twin's str path would
    have resolved a digit string as a NAME (species/match) -- the router is arguably
    more correct here; flagged, not copied.
(c) **Unknown gauge_id error class (river).** The twin maps a 404 on the single-gauge
    detail to ``NWS_RIVER_FORECAST_NO_GAUGES`` (non-retryable). The shared router
    transport classifies a 404 to a typed transport error -> ``NWS_RIVER_FORECAST_UPSTREAM_ERROR``
    (retryable) BEFORE parse runs, so an unknown gauge_id surfaces UPSTREAM not
    NO_GAUGES. Both honest; the bbox-mode empty-list -> NO_GAUGES path (a 200 with
    zero gauges) IS value-identical, and the agent discovers lids via bbox first.
(d) **Synthesized payload estimator (all four).** The four twins carried no (or a
    custom) ``estimate_payload_mb``; the router synthesizes a per_feature model from
    the spec. On a present bbox the estimate stays well under the 25 MB warn for
    every source's realistic query, so no spurious warn either side. Same class as
    ADR 0061 divergence (c).
(e) **LayerURI cosmetics.** The router synthesizes ``layer_id`` / ``name`` from
    ``source_class`` where the twins hand-built scope-labelled strings; the layer
    DATA (the FGB) is value-identical, only the display label differs (unchanged from
    every prior fold wave).

Consequence: the chained-resolution mode covers the resolve-then-fetch (name->id) and
list-then-per-item-detail shapes as ONE declarative mode; the four coded fetchers become
YAML + pure hooks, and the fold's coded-fetcher-count -> zero endgame advances by four
with the strongest MANDATORY-REVIEW ratchet now retired. Supersedes nothing; extends the
tier-3 hook contract (ADR 0056 / 0061) with the resolve pre-step, offset paging, and the
bounded per-item enrichment loop.
