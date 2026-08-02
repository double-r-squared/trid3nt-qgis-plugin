# 0061 - tier-3 hook wave: extend the fold across auth-free JSON/REST point-obs

Context: ADR 0056 landed the tier-3 HOOK CONTRACT (``build_request`` /
``parse_response`` pure hooks + the ``http_json`` executor + declarative paging)
and proved it by migrating three USGS/NCEI point-event fetchers. It flagged a
remaining ~14-source hook-needing bucket. This wave reads the ten auth-free
JSON/REST candidates the fold-doc named, folds the ones whose bespoke-ness is
1-2 clean irreducible steps, and DEFERS the rest with a precise per-source
reason. Two candidates fold; each needs ONE small, reusable, minimal transport /
param extension (justified below); the other eight defer to a NEW mode a later
wave scopes (they are NOT hook-shaped).

Decision (2026-07-31):

1. **fetch_nws_event FOLDED via hooks (single-GET NWS /alerts/active GeoJSON,
   LANDED).** ``nws_event.build_request`` canonicalizes the ``area`` string (a
   2-letter state/marine code, a 5-digit county FIPS, or a full state name ->
   its code) and builds the ``?area=&status=&message_type=&event=...`` query;
   ``nws_event.parse_response`` decodes the FeatureCollection, projects each
   alert to the twin's preserved NWS property set, JSON-coerces nested props, and
   drops the geometry-less zone-only alerts pyogrio cannot write. Alerts are
   POLYGONS (the serializer is geometry-agnostic; "point features" in 0056 was
   the exemplar, not a limit). Empty result -> a header-only FGB (the twin's
   behaviour), never an honest-empty typed error. Live edge-matrix PASS: FL
   (5/5) + Florida-name (5/5) value-identical, a TX event_types filter applied
   identically (0/0), empty FeatureCollection header-only both sides, bad-status
   / bad-area INPUT_INVALID + non-FeatureCollection UPSTREAM byte-identical
   codes, docstring verbatim (2,749 chars). NEEDED EXTENSION: a ``str_list``
   param type (``event_types`` is a ``list[str]`` filter the declarative param
   surface had no type for) -- added as the string sibling of the existing
   ``float_list`` (strip / drop-empty / sort / dedupe, no allowed-set gate),
   strict no-op for every prior spec.

2. **fetch_usace_nsi FOLDED via hooks (single-POST NSI structures GeoJSON points,
   LANDED).** ``usace_nsi.build_request`` applies the NSI per-axis 1-degree span
   cap and builds the POST plan (the query is a JSON body -- a FeatureCollection
   wrapping the bbox polygon -- NSI has no query-string bbox);
   ``usace_nsi.parse_response`` decodes the FeatureCollection, projects the
   preserved NSI props, and derives the two Pelicun-consumer columns
   (``component_type`` <- ``occtype``, ``replacement_value`` <- ``val_struct``).
   Live edge-matrix PASS: Fort-Myers-Beach (2402/2402) + a Miami tile (502/502)
   value-identical, empty FeatureCollection header-only both sides, oversized
   bbox INPUT_INVALID + ``{"message":...}`` UPSTREAM byte-identical codes, POST
   body byte-identical to the twin's, docstring verbatim (6,417 chars). NEEDED
   EXTENSION: a POST transport path -- ``RequestPlan`` gains ``method`` (default
   ``"GET"``) + ``json_body``, ``transport.post_bytes`` mirrors ``get_bytes``
   under the SAME retry authority (429/5xx/timeout backoff + Retry-After; the
   endpoint is a pure cacheable query, so the retry set is unchanged), and
   ``http_json._get`` dispatches GET vs POST. The hook stays PURE (it DESCRIBES
   the request; the router owns the socket). Strict no-op for every prior hook
   (all default ``method="GET"``). This is a transport capability, not a hook
   point -- the ``build_request`` / ``parse_response`` set stays at two (a
   ``post_process`` hook was NOT added; ADR 0056's evaluation stands).

3. **EIGHT candidates DEFER, each to a NEW mode a later wave scopes (NOT
   hook-shaped, per-source reason).** All are auth-free (none blocks on a
   credential); the blocker is always a structural mode gap, so each is a
   directive/mode-promotion candidate the deletion ledger now tracks:
   - ``fetch_gbif_occurrences`` / ``fetch_inaturalist_observations`` --
     DEFER-mode: a CHAINED taxon-name -> id resolution GET (a full extra HTTP
     round-trip to a ``species/match`` / ``/v1/taxa`` endpoint) must complete
     before the main paged fetch; the two PURE hooks cannot make that I/O.
   - ``fetch_nws_alerts_conus`` / ``fetch_nws_river_forecast`` -- DEFER-mode: the
     same CHAINED-ENRICHMENT shape (fetch a list, then chase a bounded/deduped/
     best-effort set of per-item detail URLs derived from the first response's
     parsed fields -- zone polygons / threshold+stageflow series). nws_alerts_conus
     additionally feeds the SFINCS ``model_nws_flood_event_scenario`` workflow.
   - ``fetch_openfema_disasters`` -- DEFER-mode: OFFSET paging (``$skip``/``$top``,
     stop-on-short-page, not the ``totalPages`` mode) PLUS a second-service
     (TIGERweb) attribute<-boundary FIPS join.
   - ``fetch_storm_events_db`` -- DEFER-mode: a bulk gzip-CSV behind an
     HTML-directory-index scrape (an IMPURE URL-resolution pre-step the pure hook
     contract forbids) + CSV-column -> point synthesis; not a JSON API.
   - ``fetch_noaa_nwm_streamflow`` -- DEFER-mode: gridded netCDF (S3 listing +
     ~14MB download) joined to NLDI-discovered reach geometry over ~25 chained
     calls; not a JSON point API. FEEDS the SFINCS fluvial-forcing autowire
     (``sfincs_forcing_autowire._autowire_river_discharge_forcing``) -- left on
     the coded path, so its flood consumer is untouched.
   - ``fetch_cama_flood_discharge`` -- DEFER-mode: candidate-filename-probe netCDF
     -> raster/COG (closest existing shape is ``raster-cog``, but the multi-URL
     probe + HTML-sentinel byte-sniff is bespoke); gridded output, not points.

4. **Metrics.** Coded fetchers 69 -> 67 (-2); coded tools -2 net; spec-served
   data sources 27 -> 29 (+2). Registry total unchanged (two twins died, two
   spec-driven surfaces took their names). Docstrings carried VERBATIM
   (2,749 / 6,417 via ``inspect.getdoc``), so the retrieval index is UNSHIFTED:
   every corpus phrasing for both promoted tools ranks the tool in the model-free
   top-8 (6/6 + 8/8). Coverage migrated: the two twin test files (887 + 636 =
   1,523 lines) deleted; each build_request URL/body + input validation, each
   parse_response field-extraction + null-geometry drop + honest header-only +
   bad-body UPSTREAM, and the POST executor end-to-end covered in
   ``test_router_hooks.py`` (12 new tests). Twin py removed: 1,221 lines
   (590 + 631). ``test_catalog_surfacing`` spec-served count 27 -> 29 and the
   stratum/pool deltas updated (the expected metric, not a regression). One
   consumer re-point (``compute_flood_depth_damage`` resolved the NSI twin by
   direct module import -> now ``TOOL_REGISTRY["fetch_usace_nsi"].fn``, matching
   ``compute_impact_envelope``'s existing registry lookup). Daemon import clean.

5. **HOOK-RATCHET flags (deletion-ledger standing rule 4).** The wave surfaced
   recurring build/parse shapes across 2+ deferred sources -- flagged as
   directive/mode-promotion candidates, NOT built this wave:
   - CHAINED id/detail resolution (name->id, or list->per-item-detail) recurs
     across gbif + inaturalist + nws_alerts_conus + nws_river_forecast (4x, well
     past the 2x flag / 3x mandatory-review threshold): the strongest promotion
     candidate -- a declarative "resolve-then-fetch" / bounded-enrichment mode.
   - OFFSET paging (``$skip``/``$top``, stop-on-short-page) as a sibling to the
     existing ``totalPages`` paging mode (openfema; likely other OData feeds).
   - Attribute-feed <- boundary-service FIPS join (openfema<-TIGERweb) reusing the
     existing ``_router/transforms/join.py`` machinery.
   - Bulk-file-behind-an-index (regex-over-directory -> newest -> GET + CSV->point)
     as a distinct bulk mode (storm_events; likely other NCEI archives).

Non-gating divergences flagged (REPORTED, never fudged):
(a) fetch_nws_event ``?area=<5-digit FIPS>`` is a TWIN DEFECT -- the live NWS API
    rejects a raw FIPS with HTTP 400 (``area`` accepts only state/marine codes).
    The fold builds the byte-identical URL and raises the byte-identical
    ``NWS_EVENT_UPSTREAM_ERROR``, so it is value-identical to the twin; the twin's
    "NWS treats FIPS the same as state code" comment was always wrong. Flagged for
    a future fix (county alerts need a UGC zone code, not a FIPS), NOT silently
    corrected (do-not-copy-a-defect: the fold matches the twin, the twin is wrong).
(b) The twin's ``area`` accepted a bbox tuple (converted to a point center), but
    the adapter collapses the ``str | tuple`` annotation to ``str`` -- so the
    bbox-tuple path was ALREADY unreachable from the agent. The fold carries the
    STRING canonicalization only; the fold is agent-surface-identical, and only a
    direct-Python bbox-tuple call diverges (now an INPUT_INVALID instead of a
    point query).
(c) The router synthesizes a payload estimator where BOTH twins had NONE (the
    registry default). For NSI the synthesized ``bbox_area`` model is byte-identical
    on a present bbox (span_deg2 * 50); for nws_event (no bbox param) it clips to
    <=5 MB (far under the 25 MB warn), so no spurious warn either side -- identical
    warn behaviour (same class as ADR 0056 divergence b).
(d) An explicit empty ``event_types=[]`` yields ``[]`` (router str_list) where the
    twin passed ``None``; the URL + result are identical, only the cache key of the
    degenerate empty-list input differs.

Consequence: the tier-3 hook path now covers single-GET AND single-POST
JSON/REST point-obs APIs, and the declarative param surface carries a ``str_list``
filter. Two more coded fetchers become YAML + a pure hook pair. The remaining
auth-free residual is classified: every deferral is a named mode (chained
resolution / offset paging / boundary join / bulk-file) the ledger now tracks for
promotion, so the fold's "coded fetcher count -> zero" endgame has an explicit
next-mode roadmap. Supersedes nothing; extends ADR 0056 with the POST transport
capability + the str_list param type + the eight-source deferral classification.
