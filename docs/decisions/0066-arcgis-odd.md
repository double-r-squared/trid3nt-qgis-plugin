# 0066 - arcgis-odd: the wave-11 ArcGIS deferrals fold onto the EXISTING hooks

Context: ADR 0059 (wave-11) DEFERRED six ArcGIS-family twins with per-source
STOP-RULE evidence, each judged to need a NEW mode/primitive the router did not yet
have: fema_nfhl_zones (OBJECTID-cursor paging vs the resultOffset-only executor +
list-enum zone_filter), nwi_wetlands (same-URL geojson->esri format fallback +
prefix-strip normalizer + WAF headers), epa_frs_facilities (5-layer fan-out UNION +
Superfund point-from-LAT/LON), wdpa_protected_areas (alias-normalizer that RAISES +
post-fetch fail-loud), usace_dams (credential-gated dual-endpoint + non-maskable auth +
list IN() filters), and hifld_critical_infrastructure. This wave re-reads all six IN
FULL against the phase inventory ADR 0063/0064/0065 landed (pre_resolve, next_page
offset paging, PHASE E enrichment, esri_json decode, endpoint_by_param, the keyed
missing-key parity rule) and folds five; hifld was already spec-driven.

Decision (2026-08-01):

1. **All five FOLD via the EXISTING tier-3 hooks (build_request / next_page /
   parse_response); NO new mode, NO new HookSpec field, NO new transport capability.**
   The wave-11 deferrals were vector-fgb-executor-centric ("the executor's only paging
   mode is resultOffset", "AuthSpec carries only user_agent"); the honest reframe is that
   every "bespoke" step is a PURE compute at a hook edge, which is exactly what the tier-3
   hooks are for. The hook executor (http_json / chained_resolution) owns all I/O + the
   paging loop + the FGB serialize; the hooks only compute. This is the fourth proof
   (after ADR 0063/0064/0065) that the phases compose to absorb new shapes with ~zero new
   engine machinery.
   - **fema_nfhl_zones** = build_request (esri envelope + the ``OBJECTID>0`` cursor start,
     sfha_only -> ``SFHA_TF='T'``, zone_filter -> server ``FLD_ZONE IN (...)`` with
     uppercase+VALID-set validation raising FEMA_NFHL_ZONES_INPUT_INVALID) + next_page
     (advance the cursor to the page's max OBJECTID, stop on a short page -- the OBJECTID
     cursor IS a ``next_page`` variant, the pure loop control the declarative pager cannot
     express) + parse_response (project to the 14 regulatory columns, OBJECTID stripped).
     LIVE twin-vs-router value-identical: small no-filter 782, sfha-only 65, zone AE 15,
     empty-ocean 0, a 3-page cursor case 3000 features, bad-zone error code identical.
   - **nwi_wetlands** = build_request (geojson query with the WAF header trio on the
     RequestPlan) + next_page (offset paging, stop on short/exceededTransferLimit) +
     parse_response (prefix-strip first-wins normalizer -> the 3 NWI columns). The
     same-URL esri fallback is NOT reproduced (the live host serves geojson with the WAF
     headers -- probed 200 with the table-prefixed keys -- and the twin's fallback ring
     decode was never byte-parity). LIVE parity BLOCKED-ON-UPSTREAM this wave (the USGS
     wetlands host returned 503 during the run); structurally proven by the earlier 200
     geojson probe + the offline hook unit tests (prefix-strip, WAF headers, paging stop).
   - **wdpa_protected_areas** = build_request (designation alias-normalizer that RAISES on
     an unknown token -- WDPA_DESIGNATION_INVALID) + next_page (offset paging by
     exceededTransferLimit) + parse_response (client-side casefold filter + the fail-loud
     when a non-empty bbox is emptied). Both "hard" steps are pure hook compute; the
     rejected-before post_process hook was NOT needed (parse_response IS the decode+gate
     step, allowed to raise). LIVE value-identical: no-filter 6, National-Park 1; bad-desig
     + fail-loud + bbox (BBOX_INVALID) error codes identical (fail-loud confirmed live in
     isolation, WDPA_DESIGNATION_INVALID with 6 features present).
   - **usace_dams** = build_request (token resolve kwarg->str-secret_ref->env; the bespoke
     hazard controlled-vocab + state USPS/Title-Case normalization + min_height into an
     ``IN (...)`` / ``DAM_HEIGHT >=`` where clause) + next_page (offset paging) +
     parse_response (NID schema project). KEYED under the ADR 0065 rule (never register a
     real key): the KEYLESS path -> the public ESRI Living Atlas mirror is the parity
     surface. LIVE value-identical: bbox 11, High 2, TX>=100 3; bad-hazard
     USACE_DAMS_INPUT_INVALID identical.
   - **epa_frs_facilities** = build_request (facility_program enum expands to the ordered
     layer set -- "frs"=5 point layers, single=1, superfund=1 esri-json -- one plan per
     layer) + parse_response (decode each body IN BUILD ORDER: point layers -> common
     schema, superfund -> point-from-LAT/LON synthesis; stamp program/label; union).
     Multi-plan build_request over the http_json path, no next_page. LIVE value-identical:
     frs union 643, tri 57, superfund 1; bad-program EPA_FRS_INPUT_INVALID identical.
   - **hifld_critical_infrastructure** = ALREADY FOLDED (source.yaml since an early wave;
     no twin py). No-op this wave; verified spec-served + retrieval-stable.

2. **One minimal, opt-in, no-op-for-priors executor extension: ``ingest.chained.
   tolerate_page_error``.** fema_nfhl_zones over the flaky FEMA cluster 500s unpredictably
   on later cursor pages; the twin treats a non-first-page 500 as "cursor exhausted ->
   partial". The chained executor's ``_fetch_main`` now, when a source opts in, catches a
   RouterError on a NON-first page (>=1 body already) and stops with the partial pages
   (the first page always propagates). ~6 lines, opt-in, strict no-op for every prior spec;
   it reproduces the twin's documented resilience for FEMA-class flaky-cursor endpoints, a
   general pattern (not overfit). This is the ONLY code addition beyond the hook modules +
   source.yaml files.

3. **Metrics.** Coded tools 146 -> 141 (-5); coded fetchers 56 -> 51 (-5). Spec-served
   data sources 40 -> 45 (+5). Registry total unchanged at 186 (five twins died, five
   spec-driven surfaces took their names). Twin py + test LOC removed = 4,131 (twins:
   774+580+728+1259+790) + 3,591 (tests: 910+164+757+1347+413); value-bearing coverage
   migrated to ``test_router_arcgis_odd.py`` (25 offline hook unit tests: cursor
   paging+tolerate, sfha/zone/IN() where, prefix-strip, alias-raise, fail-loud, USPS/hazard
   normalization, keyless-mirror endpoint, program-expansion union + point-from-LAT/LON).
   Docstrings carried VERBATIM (``inspect.getdoc`` at fold time) + corpus.yaml untouched, so
   the retrieval index is UNSHIFTED: 39/39 corpus phrasings rank the tool in the model-free
   top-8. ``test_catalog_surfacing`` spec-served count 40 -> 45, the arm2/arm3 declarable
   delta -39 -> -44, the stratum tool count 39 -> 44 (the expected metric, not a
   regression). NO consumer re-point + NO flood canary: a grep of the five names found only
   docstring cross-references + categories.py name-strings + the deleted registration
   imports -- no flood/SFINCS composer imports a twin module or its ``_fetch_*`` internals
   (fema_nfhl_zones / usace_dams sit in the flood_infrastructure CATEGORY, a name-string,
   not a functional coupling). Offline suite FAILED set == 9 exactly (the pre-existing
   test_fetch_resolution_gate x4 + test_run_river_dye_scenario x5; no new failure), run in
   four foreground quarters. Daemon import clean; all five spec-served + registry-resolvable.

4. **HOOK-RATCHET update.** No NEW 2x+ recurring cross-source shape surfaced. Two
   composition patterns recurred and are noted (not yet ratchet-promotable): (a) the
   server-side ``FLD_ZONE IN (...)`` / ``HAZARD_POTENTIAL IN (...)`` where clause built
   inside build_request (nfhl, usace_dams) -- a hook-local string, NOT the declarative
   where_clauses extension the wave-11 note anticipated (proven unnecessary: the hook
   builds it, no-op for priors); (b) the multi-plan build_request + by-order parse union
   (epa_frs, the ADR 0065 raws/openaq enrich siblings), a use of the EXISTING multi-request
   contract. All four ADR 0061 ratchets remain retired.

Non-gating divergences flagged (REPORTED, never fudged):
(a) **NFHL zone_filter server-side vs client-side.** The twin filtered zone_filter
    client-side after fetch; the router applies ``FLD_ZONE IN (...)`` server-side. The
    feature SET is value-identical (LIVE-proven); the cursor + payload differ only in that
    the server pages over the filtered set. Also NFHL zone_filter is a plain ``str_list``
    param, so its cache key sorts the stripped (mixed-case) tokens where the twin keyed the
    uppercased set -- a lowercase spelling double-warms the cache, value-identical output
    (same class as ADR 0065 divergence d).
(b) **NFHL later-page 500 tolerance is best-effort partial, not the twin's exact page.**
    ``tolerate_page_error`` stops with the pages so far on a later-page upstream failure
    (matching the twin's posture); on a flaky run the twin + router may cut at different
    pages, so parity is proven on a CLEAN run. The first-page failure always propagates
    (both).
(c) **NWI same-URL esri fallback dropped + live parity blocked-on-upstream.** The degraded
    esri-json path (naive per-ring MultiPolygon decode, never byte-parity with the server
    geojson) is not reproduced; the geojson primary is the live/tested path. LIVE feature
    parity was blocked this wave by a transient 503 on the USGS wetlands host -- the
    machinery composes; only the byte-equality of live data awaits the host's recovery
    (blocked-on-upstream, not blocked-on-mode).
(d) **usace_dams token/authoritative path is BLOCKED-ON-KEY.** The keyless -> public mirror
    path is byte-parity (LIVE-proven). The AUTHORITATIVE endpoint + the non-maskable
    auth-card + the authoritative->mirror non-auth fallback are only reachable WITH a
    token, which this wave never registers; they are honestly blocked-on-key (same class as
    ADR 0065 divergence a). The Persistence ``secret_ref`` (async) path is likewise
    unexercised; the str-secret_ref + env token paths are covered.
(e) **WDPA full-schema on the non-empty path.** The router carries the 6 declared columns;
    the twin's non-empty ``from_features`` path carried only the server-returned keys (the
    empty path declared all 6). Resolved by passing server props through as-is (the twin's
    behaviour), so both now emit the server's dynamic column set (LIVE value-identical).
(f) **FRS single-page per layer (2000-feature cap) vs the twin's 20000.** No next_page:
    each fanned layer caps at the server maxRecordCount (2000) where the twin paged to
    20000/layer. A realistic small-AOI query is value-identical (LIVE union 643 across 5
    layers, tri 57, superfund 1); only a dense state-scale bbox truncates earlier (the
    advisory payload gate warns first). Flagged, not copied.
(g) **Synthesized payload estimator + LayerURI cosmetics (all five).** per_feature estimate
    + source_class-derived layer_id/name where the twins hand-built labelled strings (FGB
    data value-identical); ``emit_bbox: false`` reproduces the four twins that omit
    LayerURI.bbox (FRS keeps it). Unchanged from every prior fold wave.

Consequence: the wave-11 ArcGIS-odd family -- the "needs a new mode/primitive" deferral --
folds onto the tier-3 hooks the earlier waves already built, with ZERO new mode and one
minimal opt-in resilience flag; the "coded fetcher count -> zero" endgame advances by five,
and the keyed-source missing-key parity rule (ADR 0065) now covers a degrade-to-public-mirror
source (usace_dams: no key is NOT an error). Supersedes the wave-11 (ADR 0059) DEFER verdicts
for these five sources; extends nothing in the hook contract beyond the one opt-in
``tolerate_page_error`` flag.
