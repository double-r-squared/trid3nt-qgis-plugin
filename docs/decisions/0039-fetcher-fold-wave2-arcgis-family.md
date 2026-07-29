# 0039 - fetcher-fold phase-2 wave-2 (ArcGIS FeatureServer/MapServer vector family)

Context: with the 5 pilots promoted (ADR 0038), phase-2 fans the fold out
family-by-family. The audit (`docs/specs/fetcher-fold-audit.md`) names the ArcGIS
FeatureServer/MapServer `/query` vector-fgb pattern as the single highest-leverage
mode (~25 fetchers, proven by the fetch_hifld pilot). This wave migrates that
family's next tranche of SPEC-EXPRESSIBLE members and builds the declarative
router modes they share, so the whole family becomes spec-driven.

Decision (NATE authority via the wave-2 kickoff; both gates held per source):

1. FAMILY PICK + SCOPE (6): fetch_nifc_fire_perimeters, fetch_hifld_transmission_lines,
   fetch_mtbs_burn_severity, fetch_cdc_svi, fetch_nhd_waterbodies,
   fetch_us_drought_monitor. All are audit-SPEC-EXPRESSIBLE ArcGIS `/query` sources
   whose only gaps were ROUTER-capability gaps (not bespoke logic), closable by one
   cohesive declarative surface. DEFERRED (each needs a wholly new ingestion MODE,
   its own wave, per the STOP RULE): fetch_epa_ejscreen (esri-json `f=json`
   rings->GeoJSON geometry parser + JSON-envelope geometry + dual-range clamps +
   indicator field-select), fetch_noaa_slr_scenarios (multi-query per `scenario_ft`
   with a per-value service-name URL + column merge), fetch_usace_levees
   (multi-service sub-layer routing 16/14/10 with per-layer geom types + property
   allowlists).

2. ROUTER EXTENSIONS (all declarative `ingest.*` / new param types; strict no-op
   when absent so the 5 pilots stay byte-identical):
   - `ingest.where_clauses` -- AND-joined `{template, require:[params]}` WHERE
     builder (transmission VOLTAGE floor, mtbs YEAR range, drought period=).
   - `ingest.column_map` (+ `column_map_ci`) -- the vector response normalizer:
     rename / int / float / str / lookup / epoch_ms_iso, with `null_below`
     sentinel, `on_error: skip_feature`, `key_from`, `default` / `default_template`
     (cdc -999 sentinel, nhd case-insensitive HR/medium fields + ftype_label lookup,
     drought dm->label + epoch-ms ddate->ISO valid_date). Projects to exactly the
     mapped + derived columns (the honest-empty header schema too).
   - Param types `int_range` (mtbs year_range) and `date_compact` (drought date,
     YYYY-MM-DD | YYYYMMDD -> YYYYMMDD) in `SourceSpec`/`validate_params`, with
     `_annotation_for` int_range->list[int] so the promoted inputSchema matches the
     twin byte-for-byte.
   - `spec.fallback` primary->fallback endpoint chain in the vector fetch (nhd HR ->
     medium-res, the data-source-fallback norm).
   - `ingest.endpoint_select {param, absent, present}` param-conditioned endpoint
     (drought current layer /3 vs archive /2).
   - bbox-optional global query (omit the geometry envelope when bbox is None, for
     supports_global_query sources: nifc national sweep).
   - `orderByFields` made opt-in (`query_template.order_by`) -- some hosted services
     (CDC onemap) reject an unsupported orderByFields; the twins that need stable
     paging set it, the CDC SVI twin omits it. Found by the live proof.

3. INDISTINGUISHABILITY preserved: docstring carried VERBATIM (injected from each
   twin's `inspect.getdoc`, asserted equal); promoted inputSchema (properties +
   required) reproduces the twin's -- incl. the adapter's None-default-is-required
   quirk (a None-default param is required-in-schema; a real default stays optional).
   error_prefix pins the twins whose A.6 token differs from cache source_class
   (NIFC_FIRE, HIFLD_TRANSMISSION, MTBS); per-param suffixes (mtbs bbox->BBOX_INVALID,
   year_range->YEAR_RANGE_INVALID). Nested consumer re-pointed: model_debris_flow
   resolves fetch_mtbs_burn_severity via the registry seam and catches the shared
   FetchError base (was the twin's MTBSError).

Consequence: the ArcGIS vector family's declarative normalization + routing layer
now exists; the 6 twins are gone as code (net ~-5306 LOC: ~6.4k twin py+tests
removed, ~2.0k router/spec/test added). Gates: replication-parity 6/6 across the
full contract-4.2 edge matrix (twin-vs-router, `experiments/fetcher_fold_replication`);
router unit suites + promotion suite green (99 tests); registry stays 200 (196
local) with all 6 names spec-served; retrieval index unshifted (all 6 corpus
phrasings still route in top-8, docstrings verbatim); daemon import clean; every
source live-proven with a sane real-endpoint envelope (nifc national sweep n=221,
transmission VOLTAGE>=345 n=37 units=kV, mtbs YEAR-range n=32, cdc n=267 renamed +
-999->null, nhd n=252 ftype_label, drought n=4 dm/label/valid_date). No flood
canary: none of the 6 feed sfincs/flood consumers. Phase-2 continues family-by-
family; the deferred esri-json / multi-query / multi-service modes are the next
waves. Related: 0036 (router core), 0037 (parity closure), 0038 (pilot promotion).
