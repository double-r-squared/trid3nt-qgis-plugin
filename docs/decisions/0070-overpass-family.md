# 0070 - Overpass-family: OSM QL fold via the http_json endpoint_fallback mirror chain

Context: the OSM-backed fetchers (fetch_overpass_pois, fetch_roads_osm,
fetch_buildings, fetch_river_geometry) share ONE bespoke shape -- an Overpass QL
builder (a PURE ``params -> query string`` function) POSTed across public
interpreter mirrors with a fallback chain, whose JSON ``elements`` decode to
geometries. That shape is the textbook tier-3 ``build_request`` + ``parse_response``
pair (ADR 0056) once the router carries the 3-mirror fallback. fetch_river_geometry
was deferred WHOLE to this wave from the ZIP wave (ADR 0067). This wave scopes the
Overpass mode, folds the two textbook members, and characterizes the three that do
NOT fit a single byte-identical surface.

Decision (2026-08-01):

1. **The Overpass mode = the EXISTING http_json executor + one small, general
   transport/executor extension; NO new executor, NO new mode.** Three additive,
   strictly-no-op-for-priors pieces:
   - **``RequestPlan.data``** (a form-body field) + **``transport.post_bytes(data=)``**
     (httpx form-encode). The Overpass interpreter reads its QL from the ``data``
     form field; the twins POSTed ``data={"data": ql}``. This preserves the twins'
     exact transport (POST form, not a GET query-string divergence). No prior hook
     sets ``data`` (every prior POST is NSI's ``json_body``), so it is a no-op.
   - **``ingest.http_source.endpoint_fallback``** -- a first-success-wins mirror
     chain in ``http_json.fetch_bodies``. The build hook returns ONE POST plan per
     declared endpoint (mirror); the executor tries them in order, returns the first
     success, SHORT-CIRCUITS on a non-429 4xx (a bad query will not succeed on
     another mirror), and advances on 5xx/429/timeout -- the data-source fallback
     norm (primary -> fallback -> honest typed error) carried over EXACTLY, and the
     spec fallback-chain the wave-2 architecture named. Default (absent) = the
     existing fetch-all-join behaviour (a static multi-endpoint set), a no-op for
     every prior http_json spec.
   The QL builders + tag/road-class resolution + geometry/clip decode stay PURE
   hooks (``_router/hooks/overpass.py``); transport / retry / cache / FGB serialize
   / LayerURI stay router-owned.

2. **fetch_overpass_pois FOLDED -- vector-fgb + overpass_pois hooks (Points).**
   ``build_request`` resolves the caller's tag inputs (amenity / tag=key=value / a
   bare aliased value / category / value, the twin's exact priority + alias map +
   clean-token gate) to one ``(key, value)`` and builds the ``node/way/relation ...
   out center`` QL; ``parse_response`` projects each element to a Point (node coord
   or way/relation centroid), drops centroids outside the requested bbox, stamps
   ``osm_id/osm_type/name/key/value/tags_json``, and raises a typed
   ``OVERPASS_POIS_NO_FEATURES`` on zero matches (never an empty-success layer).
   ``output.bbox_from_features {pad: 0.02}`` reproduces the twin's single-point
   camera pad. LIVE proof (SF slice, ``amenity=fire_station``): 24 Points (node +
   way-centroid), all key=amenity/value=fire_station, in-bbox; an ocean bbox ->
   OVERPASS_POIS_NO_FEATURES (retryable False).

3. **fetch_roads_osm FOLDED -- vector-fgb + overpass_roads hooks (LineStrings).**
   ``build_request`` validates the ``highway`` class set against the closed
   vocabulary (unknown -> OSM_ROADS_INPUT_INVALID; empty list -> the twin's
   ambiguous-input error; None/absent -> the sorted default tier, spec-default-filled
   so the cache key matches the twin) and builds the ``^(a|b|c)$`` regex QL;
   ``parse_response`` extracts each way and CLIPS its LineString to the exact bbox
   (``shapely.clip_by_rect``, a way crossing the boundary several times yields
   several in-AOI segments sharing the way attributes). LIVE proof (Fort Myers
   slice): the primary mirror 504'd, the fallback chain recovered -> 805 named
   LineStrings (Summerlin Road ...), every vertex in-bbox.

4. **Three members STOP-RULED / characterized (family_read_verdicts), each with a
   concrete unblock path:**
   - **fetch_buildings** -- Overpass-primary polygon source, BUT (a) the ``"msft"``
     Microsoft/Planetary-Computer fallback is DEAD (the public STAC exposes only an
     ``abfs://`` GeoParquet store ``requests.get`` cannot download -- the twin's own
     docstring + the standing decision both say OSM is the reliable source), a
     flag-not-copy, and (b) a click-to-enrich TAGS SIDECAR: a ``{fid -> full tags}``
     JSON WRITTEN to S3 under the same cache key (``buildings_cache_uri`` +
     ``BUILDINGS_TAGS_SIDECAR_EXT``) and consumed cross-module by
     ``tool_catalog_http`` ``/api/building-detail``. The router's read-through-only
     executor has no side-channel-write seam, and the consumer coupling needs a
     re-point. Deferred: needs a sidecar-write executor extension + a consumer
     re-point (a bounded follow-up, not this wave's textbook shape).
   - **fetch_river_geometry** -- its PRIMARY is pure Overpass (waterway QL, foldable
     via THIS mode), but its FALLBACK is a DIFFERENT transport+executor (NHDPlus HR
     HUC4 FileGDB-**zip** via get_zip + a GDB-layer read) that ``select_executor``
     cannot chain AFTER http_json (the router binds ONE executor per spec; no
     cross-executor primary->fallback chain exists). The NHDPlus leg is vestigial (an
     8-envelope bbox heuristic + a ~144 MB download, effectively never reached since
     OSM is the reliable global primary). It is a DO-NOT-REGRESS flood leg
     (``sfincs/flood/flood.py`` imports the twin directly). Folding needs EITHER (a)
     dropping the vestigial NHDPlus leg (a NATE flag-not-copy call, like the
     buildings ``msft`` path) OR (b) a cross-executor fallback capability + a FileGDB
     ``zip_vector`` mode -- PLUS a flood-leg re-point and a MANDATORY flood canary.
     Left ENTIRELY untouched this wave: no flood-consumer seam re-pointed, so no
     flood canary was required (the ADR 0067 posture); the 5 offline river_dye
     baseline failures remain byte-identical in mode (the untouched twin's live-504
     UpstreamAPIError).
   - **fetch_field_boundaries** -- NOT an Overpass source (the wave's
     "characterize its actual source" target): it reads PUBLISHED fiboa / Fields-of-
     The-World GeoParquet from Source Cooperative via fsspec HTTPS with CRS-aware
     row-group bbox PUSHDOWN (GeoParquet 1.1 ``covering`` column + reproject). No
     Overpass, no ArcGIS FeatureServer, no ZIP -- a partial-GeoParquet range-read
     transport no router executor covers. Belongs to a future GeoParquet-pushdown
     executor wave, not this one.

5. **Metrics.** Coded fetchers -2 (roads, pois); coded tools -2. Spec-served data
   sources 50 -> 52 (+2). Registry total unchanged (two twins died, two spec-driven
   surfaces took their names). Twin py removed = 681 + 757; twin test LOC removed =
   878 + 500; value-bearing coverage migrated to ``test_router_overpass.py`` (31
   offline tests: QL build + narrowing, road-class vocab/empty/default, way
   extract/clip/zigzag/50-way-serialize/honest-empty-header, tag-resolution priority/
   alias/unmappable/dirty-token, POI node+centroid+in-bbox filter + NO_FEATURES,
   the mirror endpoint_fallback first-success/all-fail/4xx-short-circuit, and the
   end-to-end LayerURI + cache-key-ordering stability). Docstrings carried VERBATIM
   (``inspect.getdoc`` at fold time) + the sibling corpus.yaml untouched, so the
   retrieval index is UNSHIFTED: 7/7 (roads) + 8/8 (pois) corpus phrasings rank the
   tool in the model-free top-8. ``test_catalog_surfacing`` spec-served count 50 ->
   52, arm2/arm3 declarable delta -49 -> -51, stratum tool count 49 -> 51 (the
   expected metric, not a regression). Offline suite FAILED set == 9 exactly (the
   pre-existing test_fetch_resolution_gate x4 + test_run_river_dye_scenario x5; no
   new failure), run in four foreground quarters (1562 + 1617 + 1317 + 5326 passed).
   Daemon import clean (the twin eager imports in ``agent/tools/__init__.py`` +
   ``main.py`` removed; both spec-served + registry-resolvable).

Non-gating divergences flagged (REPORTED, never fudged):
(a) **roads/buildings gain the pois 3-mirror fallback (STRICTLY more resilient).**
    The roads twin used a SINGLE Overpass endpoint; the folded surface uses the pois
    3-mirror ``endpoint_fallback`` chain (the data-source fallback norm applied
    uniformly). Observable typed error is unchanged (all mirrors exhausted ->
    OSM_ROADS_UPSTREAM_ERROR, retryable), the router just tries harder first --
    value-identical output, more robust. LIVE-proven (roads primary 504 -> chain
    recovered).
(b) **400-bad-QL error class (unreachable path).** The twin mapped a non-429 4xx to a
    NON-retryable OSM_ROADS_INPUT_INVALID; the router's transport classifies a 400 to
    a typed transport error and ``endpoint_fallback`` short-circuits it to
    ``*_UPSTREAM_ERROR`` (retryable). A 400 only occurs on a malformed QL, which the
    validated build hook (closed-vocab tokens, clean-token gate) never produces, so
    the path is unreachable on the realistic agent surface. Same class as prior
    "more-correct / unreachable" flags.
(c) **pois resolve-cache-key collapse (double cache-warm).** The twin resolved the tag
    to ``(key, value)`` BEFORE the cache key, so ``amenity="hospital"`` and
    ``tag="amenity=hospital"`` collapse to one entry. The router keys on the RAW
    agent-facing selectors (bbox + the tag inputs), so two selectors that resolve to
    the same ``(key, value)`` hit DIFFERENT cache entries -- both fetch the identical
    POI set (value-identical output; only a one-time double cache-warm differs).
    EXACTLY the ADR 0063 divergence (a) class (a pure pre-cache-key param-resolution
    hook would remove it, but is speculative infra for one source). roads keys on
    ``(bbox, road_classes-sorted)`` -- byte-identical to the twin.
(d) **road_classes signature default (cosmetic).** The twin annotated
    ``road_classes: list[str] | None = None``; the promoted signature carries the
    sorted 8-class default list as the schema default (needed so an absent param
    fills the twin's default set into the cache key). The param stays optional in the
    inputSchema; only the displayed default value differs.
(e) **Synthesized roads estimator + LayerURI cosmetics.** The roads twin carried NO
    ``estimate_payload_mb``; the router synthesizes a per_feature model (well under
    the 25 MB warn for any realistic bbox). The router synthesizes ``layer_id`` /
    ``name`` from ``source_class`` where the twins hand-built labelled strings; the
    layer DATA (the FGB) + ``style_preset`` (osm_roads / overpass_pois) + role +
    units are value-identical. Unchanged from every prior fold wave.

Consequence: the Overpass family's honest fold is the tier-3 ``build_request`` +
``parse_response`` pair over ONE small http_json extension -- a form-body
``RequestPlan.data`` + a first-success ``endpoint_fallback`` mirror chain -- each a
strict no-op for every prior spec, LIVE-proven end-to-end (roads via a real mirror
504-recovery; pois with a real honest-empty). The two textbook members (pois, roads)
become YAML + pure hooks; buildings (dead msft leg + sidecar-write consumer), river
(cross-executor Overpass+zip fallback + do-not-regress flood leg), and fields
(GeoParquet-pushdown, not Overpass at all) are honestly STOP-RULED with concrete
unblock paths. Supersedes the ADR 0067 river_geometry DEFER only insofar as its
PRIMARY leg is now foldable via this mode; extends the tier-3 hook contract (ADR
0056 / 0061) with the form-body plan field + the mirror endpoint_fallback chain.
