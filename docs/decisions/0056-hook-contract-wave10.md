# 0056 - fetcher fold wave-10: the tier-3 HOOK CONTRACT + proof by migration

Context: NATE's endgame directive (2026-07-31) -- ALL data fetchers fold into one
spec-driven engine, so "adding a data_fetch method is just adding a YAML entry."
Waves 1-9 folded the SHAPE-declarative sources (raster-cog / vector-fgb / station
-timeseries + the ArcGIS, dataretrieval, stac_float, multi_url, gzip_object modes).
The residue the phase-1 audit flagged as genuinely bespoke needs ONE named
extension point per source. This wave lands that tier-3 contract and PROVES it by
migrating three point-event fetchers, each value-identical against the live twin
before the twin is cut. Every new knob is STRICTLY NO-OP for the 24 prior specs
(gated on ``spec.hooks`` / ``output.bbox_from_features`` / ``ingest.http_source``,
none of which a prior spec sets).

Decision (2026-07-31):

1. **HOOK CONTRACT (design that outlives us).** ``SourceSpec`` gains an optional
   ``hooks`` block (``trid3nt_contracts.source_spec.HookSpec``): named references to
   REGISTERED PURE functions for the one irreducible per-source step. The set is
   MINIMAL, derived from reading 6 bespoke fetchers (earthquakes, tsunami, volcano,
   nws_alerts_conus, high_water_marks, wfigs_incident):
   - ``build_request(spec, params) -> list[RequestPlan]`` -- source-specific request
     construction (URL + query + headers) plus the bespoke pre-fetch input
     validation the declarative param gates cannot express (FDSN relative-window
     resolution + 366-day cap; NCEI year-window). Returns 1..N plans (N=1 single
     GET; N>1 a static multi-endpoint set the parse hook joins).
   - ``parse_response(spec, params, bodies: list[bytes]) -> list[feature]`` -- decode
     the source payload(s) into GeoJSON point features; raise the honest-empty /
     result-too-large / bad-body typed errors (via the shared source-stamped
     ``router_*_error`` factories -- the twin's no-events gate, the one decode step).
   A ``post_process`` point was EVALUATED and deliberately NOT added: the only
   observed post-serialize need (stamp the camera bbox from the feature extent) is
   declarative via ``output.bbox_from_features: {pad}`` -- the router reads the
   extent back from the produced FGB (available on cache hit AND miss). A hook point
   nobody needs is speculative infra, so the set stays two.
   The hooks live in a new package ``_router/hooks/`` -- pure functions (NO I/O:
   transport, caching, gates, stamps, and the typed-error machinery stay router
   -owned), registered by name in ``HOOK_REGISTRY`` via the ``@register_hook``
   decorator, each hook module carrying its own unit tests. A spec references a hook
   by name string; ``registration._validate_hooks`` asserts every declared name
   resolves at load (a typo/deletion fails LOUD, per-spec, without bricking startup).
   NEW ENGINE PIECE: ``executors/http_json.py`` -- selected when a spec declares
   ``hooks.build_request`` (before the shape dispatch, no-op for priors). It owns the
   transport (the shared pooled client + retry authority), the declarative paging
   LOOP (``ingest.http_source.paging`` -- one plan per page, bounded by the response's
   ``totalPages`` probe -> source-stamped RESULT_TOO_LARGE over the cap), and the FGB
   serialize; the two hooks own the source-specific steps. Multi-request and paging
   both funnel a LIST of bodies into the single parse hook -- identical downstream.

2. **fetch_usgs_earthquakes FOLDED via hooks (single-GET FDSN GeoJSON, LANDED).**
   The USGS FDSN Event service is a single windowed GeoJSON GET (not an ArcGIS
   ``/query``): ``usgs_earthquakes.build_request`` resolves the relative window
   (default 30d / one-sided derive / 366-day cap) + validates magnitude and builds
   the FDSN URL; ``usgs_earthquakes.parse_response`` decodes the FeatureCollection
   (id from the feature top level, depth from the geometry Z, epoch-ms times, the
   ``metadata.count`` 20000-cap gate, zero -> NO_EVENTS). Live edge-matrix PASS: three
   fixed-window scopes value-identical (29/29 Ridgecrest, 6/6 N-California, 4/4 global
   M6+), NO_EVENTS + INPUT_ERROR edges byte-identical, docstring verbatim (5,866
   chars), style_preset/units/role identical, camera bbox = events extent (pad 0.1)
   identical.

3. **fetch_tsunami_events FOLDED via hooks + declarative paging (PAGED NCEI JSON,
   LANDED).** ``ncei_tsunami.build_request`` selects the mode endpoint
   (events/runups) + resolves/validates the year window + builds one page's request;
   ``ingest.http_source.paging`` walks ``totalPages`` (25-page cap -> RESULT_TOO_LARGE);
   ``ncei_tsunami.parse_response`` concatenates the paged items and decodes per mode.
   Live edge-matrix PASS: MULTI-page value-identical (1484/1484 global events over
   ~8 pages; 810/810 Hawaii runups over ~5 pages), 5/5 Japan events, NO_EVENTS +
   INPUT_ERROR + the too-large gate byte-identical both sides, docstring verbatim
   (5,816 chars).

4. **fetch_usgs_volcano_alerts FOLDED via hooks (MULTI-GET HANS join, LANDED).**
   ``usgs_volcano.build_request`` returns TWO static endpoints (alert list + geographic
   list); ``usgs_volcano.parse_response`` inner-joins on ``vnum``, filters to the
   request bbox in-process, sorts by severity, zero -> NO_VOLCANOES. Live edge-matrix
   PASS: BYTE-identical FGB on global (68/68, 29288 bytes), Hawaii (4/4), Alaska
   (27/27); NO_VOLCANOES + INPUT_ERROR edges byte-identical, docstring verbatim
   (5,339 chars).

Registry accounting: registry total unchanged at 190 (three twins died, three
spec-driven surfaces took their names under ``_router._promoted``). CODED tools 166
-> 163 (-3); coded fetchers 75 -> 72 (-3); spec-served data sources 24 -> 27 (+3).
Retrieval index unshifted: all three docstrings carried VERBATIM (5,866 / 5,816 /
5,339 chars via ``inspect.getdoc``); each corpus phrasing set ranks its promoted
tool in the model-free top-8 (8/8 all three). Coverage migrated: the three twin test
files (578 + 514 + 419 = 1,511 lines) deleted; the hook contract (registry resolve /
duplicate / spec-load validation), each build_request URL + input-validation, each
parse_response field-extraction + honest-empty / too-large, and the http_json
executor (multi-request join + paging loop) covered in a new ``test_router_hooks.py``
(25 tests). ``test_catalog_surfacing`` spec-served count 24 -> 27 (the expected
metric, not a regression). Twin py removed: 2,633 lines (935 + 905 + 793); the
contract + hooks package + http_json executor + router/registration wiring added
~1,050 lines of NEW capability. No consumer re-point needed (none of the three feeds
a sfincs/flood/fire leg -- verified by grep). Daemon import clean. Offline suite
FAILED set unchanged at the baseline 9 (test_fetch_resolution_gate x4 +
test_run_river_dye_scenario x5).

Non-gating divergences flagged: (a) on a CACHE HIT the router stamps the true
feature-extent bbox (read from the FGB) where the twin fell back to the request bbox
-- the router is strictly more correct; the value-identical parity holds on the
fetch path (the harness forces a fresh fetch both sides). (b) A payload estimate
synthesized from the ``bbox_area`` model diverges from each twin's bespoke estimator,
but all three are tiny point layers far under the 25 MB warn threshold on every
scope, so behavior (no spurious warn) is identical. (c) Passing ``min_magnitude=None``
explicitly stamps the 2.5 default (the router applies the declared param default)
where the twin passed no floor -- the default path (2.5) is identical; only an
explicit None diverges.

Consequence: the router now carries a tier-3 HOOK path -- any single-GET / static
multi-GET-join / paged JSON point-event API folds by adding a ``source.yaml`` + a pair
of pure ``build_request`` / ``parse_response`` hooks with their own tests, no new
router mode. This closes the "single irreducible step" class the audit deferred and
gives the endgame its extension mechanism: the coded fetcher count goes to zero as a
source is either shape-declarative (waves 1-9), one-clean-step (this wave's hooks), or
a genuinely multi-step residual awaiting a scoped mode/job (classified in the
data-router-fold.md campaign ledger). Supersedes nothing; extends the
fold-some-defer-rest precedent (ADR 0045/0047/0052/0053/0054/0055) with the tier-3
tier the fold-doc's Phase 3 anticipated.
