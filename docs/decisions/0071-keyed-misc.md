# 0071 - Keyed + misc leftovers: 5 folds via two no-op enablers; 9 honest STOPs

Context: the largest remaining sweep of ordinary JSON / ArcGIS-class / STAC-raster
fetchers plus the keyed sources (missing-key parity per ADR 0062/0065/0066). Fourteen
twins were read IN FULL and mapped against the full phase/mode inventory (ADR
0063-0070). Five fold onto the router via TWO small, strictly-no-op enablers; nine
STOP with named gaps -- the dominant blocker is a genuinely NEW, systemic one (a
LayerURI-SUBCLASS post-serialize envelope), not a missing declarative knob.

Decision (2026-08-01):

1. **Three additive pieces, each a strict no-op for every prior spec:**
   - **``hooks.classify_status``** -- ``(spec, status, body) -> RouterError | None``.
     ``http_json._get`` collapses every ``TransportError`` to a retryable
     ``*_UPSTREAM_ERROR``, discarding ``TransportError.status``; a keyed source that
     must split the HTTP status into the twin's distinct typed errors names this PURE
     hook, consulted BEFORE the default upstream fallback (401/403 -> a credential-
     shaped ``*_AUTH_ERROR``, 4xx -> ``*_INPUT_ERROR/_INVALID`` for a bad path
     selector, 5xx -> the default upstream). ``is_credential_shaped_error`` already
     recognises the ``_AUTH_ERROR`` suffix, so a rejected key surfaces the credential
     card exactly as the twin did. Returning ``None`` keeps the default -- no prior
     spec declares it.
   - **stac_float ``asset_by_param`` (singular) + ``transform.positive_only``** -- a
     single-param asset map (mobi ``layer`` -> asset key; the existing
     ``asset_by_params`` needs TWO params for modis day/night) and a positive-value
     nodata gate (mobi's importance products are ``>0`` where mapped; the existing
     gate is isfinite-only). Both additive to ``raster_cog._stac_float_to_array``,
     no-op for the modis/esri/chirps priors.
   - **``round_4dp`` quantize directive** -- climate_normals keys + filters on a 4dp
     bbox; a 3-line addition to ``_quantize_bbox`` gives byte-identical cache keys.

2. **fetch_mobi FOLDED -- stac_float raster + the two knobs (keyless).** PC-STAC
   search on the static ``mobi`` collection -> ``asset_by_param[layer]`` -> two-tier
   sign -> windowed bilinear reproject @990 m -> ``positive_only`` nodata gate ->
   float32 COG. ``units_by_param`` reproduces the twin's per-layer units (richness ->
   "imperiled-species count"; RSR variants -> None). LIVE proof (Great Smoky Mtns
   slice, ``species_richness``): twin-vs-router VALUE-IDENTICAL -- shape (34,37),
   dtype float32, EPSG:4326, bounds, valid=1258, vmin/vmax/vmean all equal. The CONUS
   pre-gate is dropped in favour of the STAC-zero-items honest-empty (same MOBI_EMPTY
   one network step later).

3. **fetch_climate_normals FOLDED -- chained_resolution enrich (keyless).**
   ``build_request`` GETs the fixed-width NCEI inventory; ``parse_response`` slices +
   bbox-filters + caps at ``gates.max_stations=120`` -> station Point features (typed
   CLIMATE_NORMALS_EMPTY if none); PHASE E (``enrich_plan`` one access-CSV ref per
   station; ``enrich_merge`` folds the annual normals, DROPS a station with no usable
   normal, raises CLIMATE_NORMALS_EMPTY if none survive). Offline hook parity proven;
   live positive parity is a keyless network gate (a small Tampa-Bay slice fans out
   per-station GETs -- slow but deterministic).

4. **fetch_ebird_observations + fetch_iucn_red_list_range FOLDED -- keyed http_json +
   classify_status.** eBird: ``build_request`` tiles the bbox into 50 km circles
   (multi-GET fan-out), resolves the key (kwarg -> str secret_ref ->
   TRID3NT_EBIRD_API_KEY, credential-shaped EBIRD_MISSING_KEY pre-network);
   ``parse_response`` dedups by ``subId`` across tiles + re-clips to the bbox (empty ->
   honest header-only FGB). IUCN: region-select single-GET (token query param),
   MISSING key -> credential-shaped IUCN_AUTH_ERROR (the twin has no separate
   MISSING_KEY); ``parse_response`` builds one assessment-or-``DD``-placeholder feature
   on a placeholder polygon + raises IUCN_AUTH_ERROR on the 200-OK token-reject
   envelope. Both split status via ``classify_status`` (401/403 -> AUTH, 4xx -> INPUT,
   5xx -> upstream). OFFLINE parity proven byte-identical: MISSING_KEY / AUTH_ERROR /
   INPUT(_INVALID) codes + credential shaping + the dedup/clip, DD-placeholder,
   token-envelope compute. The key is NEVER registered (the keyed parity surface is
   the key-absent + input + status-split typed errors; live feature parity needs a
   key we do not hold).

5. **fetch_usgs_groundwater_levels FOLDED -- enrich + consumer re-point (keyless).**
   ``build_request`` resolves the mutually-exclusive selector (state_code USPS->FIPS,
   or bbox; state wins; neither -> USGS_GROUNDWATER_INPUT_ERROR) and GETs the OGC
   latest-field-measurements; ``parse_response`` decodes the readings (typed
   USGS_GROUNDWATER_NO_WELLS if none); PHASE E (``enrich_plan`` one monitoring-
   locations ref; ``enrich_merge`` joins well name/aquifer/depth by
   ``monitoring_location_id``) is BEST-EFFORT -- a failed/absent locations body leaves
   names blank and NEVER drops a reading. Consumer re-point: ``compute_model_residuals``
   imported the twin's shared core ``_fetch_usgs_groundwater_levels_bytes`` directly ->
   re-pointed to the in-process router seam (get_spec + validate_params + executor, no
   cache/publish -- the admin_boundaries precedent) + its bbox-fetch test re-pointed to
   mock the executor. Offline hook parity proven; live positive parity BLOCKED-ON-
   UPSTREAM -- the USGS OGC endpoint 400s/hangs on the twin's OWN request shape
   identically for twin and router (a current upstream/param issue, not a fold defect;
   the router maps it to USGS_GROUNDWATER_UPSTREAM_ERROR exactly as the twin raised
   GwUpstreamError).

6. **Nine STOP-RULED / characterized (family_read_verdicts), each with a concrete
   unblock path. The dominant blocker is a NEW systemic gap:**
   - **THE LayerURI-SUBCLASS post-serialize envelope** (fetch_high_water_marks,
     fetch_flood_extent_observation, fetch_fault_sources; also topobathy /
     model_debris_flow per ADR 0059/0068). ``router.build_layer_uri`` emits ONLY a
     plain ``LayerURI``; these twins return a SUBCLASS carrying business fields
     computed POST-serialize from the FGB/COG bytes (HWM quality/type/datum breakdown +
     caveats/notes; flood-extent class_breakdown/flood_area_km2/LegendKey; fault
     kinematic ``faults`` list a nested consumer reads). The HookSpec doctrine
     explicitly deferred a post_process hook because the only prior need was
     declarative (``bbox_from_features``); these three are the NEW evidence it is
     genuinely needed. Unblock: a post-emit envelope hook + a spec-declared result
     model. **fetch_fault_sources** adds two more (an ``empty_is_success`` data-dict
     return + a two-tier constant/AOI cache). **fetch_flood_extent_observation** adds a
     categorical-palette tiled MODIS mosaic + a directory-walk date resolve; its V&V
     consumer ``compute_flood_extent_skill`` couples by raster SHAPE in a docstring,
     not by import, so the non-fold does not break it.
   - **fetch_usgs_nwis_gauges** -- STOP (RE-ATTEMPTED with the full inventory; the old
     verdict predated hooks but the gaps are real): (a) DERIVED-mode output selection
     -- the instant-overlay vs discharge-HYDROGRAPH modes carry DIFFERENT columns /
     style_preset / units / layer_id switched on window-PRESENCE (period OR start+end),
     not a single param value ``style_preset_by_param`` can key on; (b) a parse-empty
     CROSS-PARSER fallback (IV WaterML-JSON primary -> Site-service RDB fallback, a
     DIFFERENT decoder) no router mode expresses; (c) it FEEDS sfincs_forcing_autowire
     via a direct twin import. Left ENTIRELY untouched -- no flood-consumer seam
     re-pointed -- so NO flood canary is required (the ADR 0069/0070 posture for an
     untouched flood leg). Unblock: a derived-mode output selector + a
     parse_fallback second-endpoint/decoder mode + a flood-leg re-point + a MANDATORY
     flood canary.
   - **fetch_wfigs_incident** -- returns a bare structured JSON dict (incident_name /
     lat/lon / bbox / size / containment), NEVER a LayerURI. No SourceShape fits
     (``OutputSpec.layer_type`` is raster|vector; ``route()`` always emits a LayerURI).
     Unblock: a ``json-record`` shape + a non-LayerURI emission path.
   - **fetch_statsgo_soils** -- a pfdf LIBRARY delegation (``pfdf.data.usgs.statsgo.read``
     owns its own ScienceBase I/O); ``endpoints`` requires a URL there is none, and the
     hook doctrine forbids I/O in a hook. Unblock: extend the ``ingest.delegate`` /
     ``dataretrieval_delegate`` precedent to pfdf (a generic library-delegate executor);
     consumers model_debris_flow + compute_sediment_yield import it directly and would
     re-point.
   - **fetch_lehd_jobs** -- ``join(two-source)`` for the geometry leg, but the VALUES
     leg is a bulk gzip-CSV whole-object download + block->tract aggregation, not the
     census Data-API query ``join.py`` hardcodes; the ``ingest.values_query`` delegation
     point the join comment implies is DEAD/unimplemented. Unblock: a ``join.values.hook``
     seam (a named pure ``fetch_values`` override, the storm_events gzip-CSV precedent).
   - **fetch_population** -- a runtime shape-SWITCH by the ``dataset`` param prefix
     (worldpop_* -> raster-cog whole-country download-then-window; acs_* -> a half-built
     geometry=None vector, flagged "follow-up" in the twin itself); ``SourceSpec`` is
     validated to ONE fixed shape+ext, and ``select_executor`` never dispatches on a
     param value. PLUS ``compute_exposure_summary`` imports+calls the twin's submodule
     function directly. Unblock: drop the half-built ACS branch + fold ONLY the
     WorldPop raster (the hrsl multi_url precedent) OR add spec-level variant dispatch;
     re-point compute_exposure_summary as a required companion edit.
   - **fetch_movebank_tracks** -- SO-CLOSE: classify_status (built) covers its 401->AUTH
     / 403->LICENSE split, and its CSV parse is trivially pure, BUT (a) ``time_range`` is
     a raw datetime-pair kwarg no ParamType carries (a byte-identical signature needs a
     new ``datetime_range`` type, not a two-``iso_date`` decomposition), (b) composite
     user+pass creds (a Basic-Auth header, loss-lessly a RequestPlan.headers detail),
     and (c) REAL consumer schema coupling -- compute_movement_trajectory +
     compute_home_range_kde read the exact FGB column/geometry_type contract, so the
     parse hook needs a diff-verified schema match, not inspection. Unblock: the
     ``datetime_range`` ParamType + the CSV parse + a consumer-schema diff test.

7. **Metrics.** Coded fetchers -5 (mobi, climate_normals, ebird, iucn, groundwater);
   coded tools -5. Spec-served data sources 52 -> 57 (+5). Registry total unchanged at
   190 (five twins died, five spec-driven surfaces took their names). Twin py removed =
   421 + 627 + 840 + 773 + 870 = 3531 LOC; five twin test files removed; value-bearing
   coverage migrated to ``test_router_keyed_misc.py`` (registration parity, the
   keyed missing-key credential shaping + input + status-split codes, and the hook
   primitives: mobi asset-map/positive-gate + per-param error suffixes; climate
   inventory-filter + drop-and-EMPTY enrich; ebird dedup+bbox-clip; iucn DD-placeholder +
   token-envelope; groundwater selector-gate + NO_WELLS + best-effort join). Docstrings
   carried VERBATIM + the sibling corpus.yaml untouched, so the retrieval index is
   UNSHIFTED. ``test_catalog_surfacing``: n_specs 52 -> 57, arm2/arm3 declarable delta
   -51 -> -56, stratum tool count 51 -> 56 (the expected metric, not a regression).

Non-gating divergences flagged (REPORTED, never fudged):
(a) **Synthesized labels + cache-key-includes-key (keyed).** The router synthesizes
    ``layer_id`` / ``name`` where the twins hand-built labelled strings (mobi
    ``mobi-<layer>-<bbox>``, ebird/iucn/climate/gw seeded ids); the layer DATA + role +
    units + style_preset are value-identical. The keyed specs declare api_key/secret_ref
    as params (the airnow ADR 0065 precedent), so a per-key cache split is possible --
    value-identical output, only a redundant fetch differs (the observations do not vary
    by caller).
(b) **Case-variant selectors -> distinct cache entries (ebird species_code, iucn
    species_name).** The twins lowercased/normalized the selector BEFORE the cache key;
    the router keys on the raw param + the hook normalizes for the URL. Two case-variant
    spellings hit different cache entries -- both fetch the identical result (the ADR
    0063 divergence-(a) class; a pure pre-cache-key resolve hook would remove it).
(c) **Synthesized payload estimators.** ebird/iucn/groundwater/mobi twins carried no
    (or a flat) estimator; the router synthesizes a small per_feature/bbox_area model
    tuned so NEITHER path crosses the 25 MB warn gate for any realistic input (the twins
    never warned either -- value-identical gate behaviour).
(d) **climate_normals 4dp cache key.** Reproduced byte-identically via the new
    ``round_4dp`` directive (no divergence).

Consequence: the keyed + misc leftovers reduce to FIVE honest folds over TWO tiny no-op
enablers (a status-classification hook + two stac_float knobs) -- keyed missing-key +
status parity byte-identical offline, mobi value-identical LIVE, climate/groundwater
enrich offline-proven. The NINE stops are honestly named, and they converge on ONE
systemic gap worth a dedicated wave: the LayerURI-subclass post-serialize envelope
(five twins: high_water_marks / flood_extent / fault_sources / topobathy /
model_debris_flow). ``fetch_usgs_nwis_gauges`` was RE-ATTEMPTED and STOPS on a derived-
mode output selector + a cross-parser fallback (its flood seam left UNTOUCHED -> no
canary required); ``fetch_usgs_groundwater_levels`` was RE-ATTEMPTED and FOLDS cleanly
via the enrich phase, closing what the refuted dataretrieval delegate could not.
Extends the tier-3 hook contract (ADR 0056/0061/0063) with ``classify_status``; extends
stac_float (ADR 0053) with the single-param asset map + positive-only gate.
