# 0064 - remaining ratchets: spend the two hook-ratchet flags + the free cleanups

Context: ADR 0061 flagged four HOOK-RATCHET / mode-promotion candidates; ADR 0063
retired the strongest (the 4x chained-resolution mode) and left three QUEUED, noting
its ``next_page`` hook now provides a reusable offset-paging PRIMITIVE. This wave spends
the two remaining fold-bearing ratchets by folding the two sources that motivated them
(openfema, storm_events) and clears three free hygiene cleanups. NO new mode, NO new
HookSpec field, NO new transport capability was needed - both folds reuse the existing
chained-resolution surface (ADR 0056 / 0063) wholesale.

Decision (2026-07-31):

1. **fetch_openfema_disasters FOLDED - two ratchets spent at once.** The twin
   (bbox|state selector -> per-state OData query with $skip/$top paging -> per-county
   aggregate -> TIGERweb county-polygon FIPS join -> bbox-clip) folds to
   ``source.yaml`` + five ``openfema_disasters`` hooks on the chained-resolution
   executor.
   - **OFFSET PAGING (ratchet 1) - reused, not rebuilt.** ``build_request`` collapses
     the twin's per-state loop into ONE combined OData query (``(state eq 'FL' or state
     eq 'GA') and incidentType eq ... and fyDeclared ge ...``, live-verified) so
     ``next_page`` is the exact ADR 0063 offset-paging primitive (gbif's sibling): next
     ``$skip`` until a short page or the 12000-row cap. The declarative ``$skip``/``$top``
     YAML pager variant was NOT built - the hook primitive serves it, so per "prove;
     extend minimally + no-op if not" the declarative variant stays unbuilt (no-op).
   - **ATTRIBUTE-FEED <- BOUNDARY-SERVICE FIPS JOIN (ratchet 2) - via PHASE E, NOT
     transforms/join.py.** ``parse_response`` aggregates the paged declarations into
     geometry-less per-county features (n_declarations, disaster_numbers, incident_types,
     declaration_types, latest, IA/PA flags; statewide ``fipsCountyCode 000`` excluded);
     ``enrich_plan`` emits ONE TIGERweb county FeatureServer GET per distinct
     state-in-scope (mode dedups by state FIPS); ``enrich_merge`` left-joins each county
     polygon by the 5-digit GEOID, bbox-clips the selector path, drops unmatched, and
     raises OPENFEMA_NO_DECLARATIONS when nothing joins. **The transforms/join.py reuse
     the ledger condition named was REJECTED with evidence** (below); the chained-mode
     enrich IS the general attribute<-geometry join surface.
   - **Live edge-matrix parity PASS vs twin.** RI-full (5/5), VT-flood (14/14),
     DE-since-2017 (3/3), bbox-clip RI (6/6) all county-set + property value-identical
     (n_declarations / disaster_numbers / incident_types / latest / IA-PA / county_name);
     no-declarations scope -> OPENFEMA_NO_DECLARATIONS both; 5/5 error codes identical
     (no-selector / bad-state / bad-incident / bad-year / bbox-outside-US ->
     OPENFEMA_INPUT_ERROR). Docstring carried VERBATIM (5,435 chars); retrieval 8/8
     corpus phrasings rank the tool in the model-free top-8 (index UNSHIFTED).

2. **fetch_storm_events_db FOLDED - bulk-file-behind-an-index via the RESOLVE phase,
   no new machinery.** The bulk-gzip-CSV-behind-an-HTML-directory-index shape (ADR 0061
   ratchet, "IMPURE-ish HTML regex" concern) folds cleanly onto the EXISTING resolve
   phase (ADR 0063): ``resolve_build`` GETs the NCEI directory index (router-owned I/O)
   + does the bespoke input validation; ``resolve_parse`` regex-scrapes the index for the
   window's year(s) and picks the newest processed-date file per year (PURE compute over
   a router-fetched body - exactly like a JSON ``resolve_parse``), merging the resolved
   bulk-CSV URL(s) into ``params``; ``build_request`` GETs each gzip CSV; ``parse_response``
   decompresses + filters (state / event_types / bbox / date-window, client-side) +
   synthesizes points. It routes to the ``http_json`` executor (no ``next_page`` / enrich)
   with ``route()``'s pre-resolve doing the index round trip. **The one-consumer STOP RULE
   (jrc-DSL precedent) did NOT trip**: the index-scrape needs zero new hook points, zero
   new executor, zero new transport capability - the "impurity" was illusory (the fetch is
   the router's, the regex is pure). Live end-to-end PASS: real NCEI index resolve ->
   newest ``d1960_c20260323`` file -> 77 TX-tornado points, every feature EVENT_TYPE=Tornado
   + STATE=TEXAS. Offline parse-parity vs twin over state / state-name / event-type / bbox /
   date-window / empty combos. Docstring VERBATIM (3,441 chars); retrieval 8/8 top-8.

3. **Three free cleanups.**
   - **(a) Cloud Run Jobs submitter binding - REJECTED, not deleted.** The ledger
     candidate assumed GCP-era dead code; a live-use grep REFUTED it:
     ``set_worker_submitter`` / ``_WORKER_SUBMITTER`` in ``meta/passthroughs/passthroughs.py``
     is the LIVE on-box qgis_process substrate seam - ``main.py`` binds
     ``_default_qgis_process_submitter`` (a local docker/subprocess runner, NOT Cloud Run)
     at startup, ``qgis_discovery`` reads it, and test_qgis_discovery + test_main_startup
     exercise it. Only the ``set_worker_submitter`` docstring's "Cloud Run Jobs" / "GCP
     libs" wording was stale/misleading GCP-era prose - CORRECTED to describe the on-box
     lane (comments = constraints). The binding stays.
   - **(b) meta/probe_point.py relocated to cases/.** Deregistered route-server code (not
     an LLM tool) moved via ``git mv`` to ``cases/probe_point.py`` (the 0058 relocation
     posture); ``tool_catalog_http`` + two test files re-pointed; ``agent/tools/meta`` now
     holds no non-@register_tool module (the tools-tree invariant restored).
   - **(c) _strip_query / _unwrap_tile_template hoisted to a shared agent URI util.**
     ``agent/tools/_uri_util.py`` created (self-contained, urllib.parse only); the 3 agent
     importers (query_point_hazard / compose_case_report / publish_layer) re-pointed off
     the ``cases/hydrate_case_layers`` platform import. ``cases/`` KEEPS its own copy
     (cases = platform layer must not import agent/tools - the wrong-direction layering the
     hoist was meant to fix); the small duplication is the correct trade for clean layering.

4. **Metrics.** Coded fetchers 63 -> 61 (-2); coded tools -2 net. Spec-served data sources
   33 -> 35 (+2). Registry total unchanged at 190 (two twins died, two spec-driven surfaces
   took their names). ``test_catalog_surfacing``: spec-served count 33 -> 35, arm2/arm3
   declarable delta -32 -> -34, stratum tool count 32 -> 34 (the expected metric, not a
   regression). Twin py + test LOC removed: 864 + 873 (twins) + 345 + 1,139 (tests) = 3,221;
   value-bearing coverage migrated to ``test_router_chained.py`` (13 new tests: aggregate +
   FIPS join, statewide-exclusion + NO_DECLARATIONS, bbox-clip, offset-paging stop
   conditions, input errors; index resolve newest-pick + missing-year, state/bbox/event-type
   filter, empty, corrupt-gzip, input errors). No consumer re-point (neither twin fed a
   nested workflow - re-verified: openfema/storm do not feed sfincs/flood, so NO flood
   canary needed this wave).

5. **HOOK-RATCHET update.** No NEW 2x+ recurring cross-source shape surfaced. All four
   ADR 0061 ratchets are now retired: chained resolution (ADR 0063), offset paging (this
   wave, via the reused primitive), boundary-service FIPS join (this wave, via PHASE E),
   bulk-file-behind-an-index (this wave, via the resolve phase). The fold's coded-fetcher
   -> zero endgame advances by two.

Non-gating divergences flagged (REPORTED, never fudged):
(a) **transforms/join.py NOT reused for the openfema FIPS join (REJECTED reuse).** The
    ledger condition said "reuse existing join transform". join.py is geometry-FIRST,
    single-value choropleth (fetch geometry -> extract scope -> fetch values-per-scope ->
    left-join one value). openfema is attributes-FIRST, multi-field aggregate (fetch +
    page declarations -> aggregate sets/counts/flags per county -> fetch geometry -> join
    + bbox-clip). Forcing openfema through join.py would invert its control flow AND
    generalize ``join_on_key`` from a single value to an arbitrary aggregate - NOT a no-op
    for the census/lehd/usgs_gw/usgs_wq/volcano priors. The chained-mode enrich (the
    promoted, general mechanism) IS the honest fit; the specific join.py reuse fails the
    no-op-priors bar and is rejected, but the JOIN still happens (via enrich), so the
    ratchet is spent.
(b) **openfema combined-query row cap is TOTAL, not per-state.** The twin caps 12,000 rows
    PER state; the combined OData OR-query caps 12,000 TOTAL. Only a pathological
    all-history multi-state unfiltered query (>12k county-declaration rows) diverges; every
    realistic query (single state, or a bbox spanning 2-3 states, usually with an
    incident/year filter) is well under the cap and value-identical. The aggregate is
    order-independent (max-date + set-union), so ordering within the cap never matters.
(c) **openfema payload estimator capped (ceil_mb=5).** The synthesized per_feature estimate
    cannot see the state_code selector (no bbox), so it would read CONUS-area (~269 MB ->
    a spurious >250 MB BLOCK) on a state query. ``ceil_mb: 5`` caps it to the twin's
    realistic state-overlay size (~4 MB, no warn); no realistic query warns or blocks
    either side. A pathological >5 MB huge-bbox query under-estimates vs the twin's own
    loose guess. Same class as ADR 0063 divergence (d).
(d) **openfema cache-key + resolve semantics.** The router keys on the RAW params
    (state_code / bbox / incident_type / start_year); the twin keyed on the RESOLVED
    (states / clip_bbox / ...). A state_code query and an equivalent bbox query hit
    different cache entries (both fetch the identical declaration set - value-identical).
    Same class as ADR 0063 divergence (a).
(e) **storm_events resolve is pre-cache-key.** The index scrape runs in ``pre_resolve``
    (before read_through), so (i) the index is GET on every call incl. cache hits (one
    small directory listing) and (ii) the resolved processed-date URL enters the cache key,
    so an NCEI REPROCESS busts the cache (arguably MORE correct - fresh data); the twin
    keyed on (year, filters) only and served stale until TTL. Both honest; flagged.
(f) **storm_events payload over-warns on a state-without-bbox query.** The per_feature
    estimate matches the twin on the genuinely-large national query (~40 MB, WARN both),
    but on a state-only query the router (no bbox visible) still reads national-scale ->
    ~40 MB WARN where the twin's state estimate (~1.4 MB) does not. Advisory only (never a
    block); the honest-conservative bound. Same class as ADR 0061 divergence (c).
(g) **LayerURI cosmetics (both).** The router synthesizes layer_id / name from
    source_class where the twins hand-built scope-labelled strings; the layer DATA (the
    FGB) is value-identical (unchanged from every prior fold wave).

Consequence: the last two fold-bearing HOOK-RATCHET flags are retired without adding any
new mode - the offset-paging primitive and the resolve phase (ADR 0063) plus the enrich
phase carry openfema and storm_events, and the "coded fetcher count -> zero" endgame
advances by two. Supersedes nothing; extends the reach of the ADR 0056 / 0061 / 0063 hook
contract to the OData-offset-paged + FIPS-joined and the directory-index-resolved
bulk-file shapes, and records the transforms/join.py-reuse rejection as a bounded,
evidenced no.
