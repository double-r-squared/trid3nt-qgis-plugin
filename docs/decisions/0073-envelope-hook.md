# 0073 - LayerURI-envelope wave: the post-emit envelope hook (ADR 0056 reopened)

Context: ADR 0056 EVALUATED a ``post_process`` hook and deliberately REJECTED it as
speculative -- the only post-serialize need then was the camera bbox, already
declarative via ``output.bbox_from_features`` (one consumer, so a hook point nobody
needs is speculative infra). ADR 0071 found the SYSTEMIC recurrence the rejection
anticipated: 5+ sources return a ``LayerURI`` SUBCLASS carrying business fields
computed POST-serialize from the produced bytes that ``router.build_layer_uri``
(plain ``LayerURI`` only) cannot emit -- fetch_high_water_marks (quality/type/datum
breakdown + caveats/notes), fetch_flood_extent_observation
(class_breakdown/flood_area_km2/LegendKey), fetch_fault_sources (a kinematic
``faults`` list a nested consumer reads), plus topobathy + model_debris_flow per the
ledger. The DELETION_LEDGER hook-ratchet rule (3+ recurrence = mandatory promotion)
now DEMANDS the seam. This wave lands it and proves it by migrating one twin.

Decision (2026-08-01):

1. **THE ENVELOPE HOOK (minimal, pure, no-op).** A THIRD hook point --
   ``hooks.envelope`` -- runs LAST in ``router.route()``, after ``read_through`` and
   the ``bbox_from_features`` stamp: ``envelope(spec, params, layer: LayerURI, data:
   bytes) -> dict``. It receives the ASSEMBLED base ``LayerURI`` + the produced bytes
   (available on cache hit AND miss) and returns the EXTRA business fields (plus any
   ``name`` / ``units`` override). PURE: it only computes over already-fetched bytes
   (reading an in-memory FGB/COG is CPU, exactly like ``_extent_from_fgb`` -- the
   transport/cache/gates/typed-error machinery stay router-owned; NO socket). The
   result TYPE is declared DECLARATIVELY: ``output.result_model`` names a
   ``LayerURI`` subclass in a new ``trid3nt_contracts.execution.LAYER_RESULT_MODELS``
   table, so the spec-driven surface has NO coded twin holding the class. The router
   builds ``cls(**{**layer.model_dump(), **extra})``.
   HONESTY FLOOR: the router STRIPS the identity keys ``uri`` / ``layer_type`` from
   the hook's returned dict (``_ENVELOPE_PROTECTED_KEYS``) before constructing the
   model -- a hook may ADD fields but can NEVER re-point the layer or flip an error
   to success (errors already raise before emission; the envelope runs only on a
   produced-bytes success). Registration validates, per-spec fail-loud at load: the
   ``envelope`` hook name resolves in ``HOOK_REGISTRY``, ``result_model`` resolves in
   ``LAYER_RESULT_MODELS``, and the two are declared TOGETHER (each is meaningless
   alone). STRICTLY no-op for all 57 priors -- none set ``hooks.envelope`` or
   ``output.result_model`` (asserted by a test scanning every composed spec).

2. **fetch_high_water_marks FOLDED (proof-by-migration).** The USGS STN HWM twin
   maps cleanly onto the phases: ``resolve_build`` / ``resolve_parse`` resolve a named
   flood EVENT to its ``event_id`` (Events.json substring match; ``[]`` to skip when
   no event -- the gbif resolve pattern); ``build_request`` derives the US STATE(S)
   the bbox overlaps (STN has no server-side bbox filter), raises the US-outside
   HWM_INPUT_ERROR, and builds the FilteredHWMs request (Event-scoped when resolved,
   else State-scoped); ``parse_response`` decodes + CLIPS to the bbox client-side,
   stamps ``quantity="water_surface_elevation"`` per mark, and raises the honest
   HWM_NO_MARKS on an empty AOI (a typed error here, never a fabricated empty layer);
   the ``envelope`` hook reads the produced FGB back into the quality/type/datum
   breakdown + caveats/notes -> ``HighWaterMarksLayerURI``. ``error_prefix: HWM``
   reproduces all four A.6 codes (HWM_INPUT_ERROR / HWM_EVENT_NOT_FOUND /
   HWM_UPSTREAM_ERROR / HWM_NO_MARKS). The twin's redundant event+states fallback is
   DROPPED with proof: after the bbox clip a State filter cannot add an in-AOI mark
   (the bbox is a subset of its overlapping states), so the event-only fetch is the
   byte-identical result. Consumer ``extract_model_at_observations`` reads the FGB
   ``quantity`` column only (no class import), so NO re-point was needed. Offline hook
   parity proven (18 tests); LIVE positive parity is a network gate (STN endpoint),
   deferred to a live-drive session per the offline-first rule.

3. **datetime_range ParamType (movebank rider, no-op enabler).** A 2-element
   ``[start, end]`` ISO datetime-pair type (each entry parses as an ISO date OR
   datetime; ``start <= end``; echoed as ``[start.isoformat(), end.isoformat()]`` for
   cache stability) -- the datetime sibling of ``int_range`` that no ``iso_date`` pair
   carries. It is the byte-identical enabler for movebank's raw ``time_range`` kwarg.
   Added + validated; STRICTLY no-op (no prior spec declares it).

4. **STOP-RULED (the envelope seam closes the subclass fields but NOT these):**
   - **fetch_flood_extent_observation** -- the envelope closes
     class_breakdown/flood_area_km2/LegendKey, BUT the fetch needs a NEW categorical
     tiled-mosaic raster access mode (per-10-degree-tile GeoTIFF download ->
     nearest-resample window -> FIRST-VALID-wins uint8 mosaic -> embedded palette +
     ColorInterp.palette) the existing ``fixed_tile_grid`` (continuous NaN-merge) does
     not express, PLUS a LANCE directory-walk date resolve (latest year/doy). Its V&V
     consumer ``compute_flood_extent_skill`` couples by raster SHAPE in a DOCSTRING,
     not by import, and neither is on the sfincs/flood run path (verified by grep), so
     the non-fold breaks nothing and NO flood canary is required. Unblock: a
     categorical tiled-mosaic raster mode + the dir-walk resolve.
   - **fetch_fault_sources** -- the envelope closes catalog/fault_count/faults/source,
     BUT (a) EMPTY-IS-SUCCESS returns a plain NON-LayerURI dict (the honesty gate
     against hallucinating "fault lines displayed" when zero faults intersect) and the
     router's ``route()`` ALWAYS emits a LayerURI (a vector empty -> header-only FGB
     would FABRICATE a renderable empty layer -- the wfigs "json-record shape" gap);
     (b) a TWO-TIER cache (whole-world GeoJSON under constant params, then an
     AOI-scoped vector as a second entry) the read-through single-key flow does not
     express -- the router would re-download the 10.6 MB file per distinct AOI; (c) a
     HARD import coupling -- ``resolve_fault_sources`` imports ``fetch_fault_sources``
     + ``FaultSourcesError`` + reads ``.faults`` directly, needing a re-point. Unblock:
     a non-LayerURI emission path (json-record shape) + a two-tier cache directive +
     the consumer re-point.

5. **Metrics.** Coded fetchers -1 (high_water_marks); coded tools -1. Spec-served
   data sources 57 -> 58 (+1). Registry total UNCHANGED at 190 (one twin died, one
   spec-driven surface took its name). Twin py removed = 738 LOC; the twin test file
   removed; value-bearing coverage migrated to ``test_router_envelope.py`` (18 tests:
   the seam registration/validation/protected-key strip/no-op + the HWM resolve /
   states-gate / bbox-clip+NO_MARKS / quantity stamp / envelope breakdowns). Docstring
   carried VERBATIM (2,869 chars via ``inspect.getdoc``) + the sibling corpus.yaml
   untouched, so the retrieval index is UNSHIFTED. ``test_catalog_surfacing``: n_specs
   57 -> 58, arm2/arm3 declarable delta -56 -> -57, stratum tool count 56 -> 57 (the
   expected metric, not a regression).

Non-gating divergences flagged (REPORTED, never fudged):
(a) **Synthesized labels.** The router synthesizes ``layer_id`` (
    ``usgs_stn_hwm-usgs_stn_hwm``) where the twin hand-built ``usgs-hwm-<seed-hash>``;
    the envelope hook overrides ``name`` to the twin's ``"USGS high-water marks (n)"``.
    The layer DATA + role + style_preset + units + the camera bbox (extent, pad 0.02
    via ``bbox_from_features``) are value-identical.
(b) **Synthesized payload estimator.** The twin's ``400 if event else 150`` (or
    bbox-area) estimator is reproduced by a ``per_feature`` model tuned so neither
    path crosses the 25 MB warn gate for any realistic AOI (the twin never warned
    either -- value-identical gate behaviour).
(c) **Cache key shape.** The twin keyed on ``{bbox, event_id, states}``; the router
    keys on ``{bbox, event}`` with the resolved ``event_id`` merged pre-cache-key. The
    twin is deleted, so cross-twin cache parity is moot; the key is internally stable.

Consequence: the router now carries the tier-3 ENVELOPE path -- a source whose result
is a business-field-carrying ``LayerURI`` subclass folds by naming a
``LAYER_RESULT_MODELS`` type + a pure ``envelope`` hook, no coded twin. This reopens
and closes the ADR 0056 ``post_process`` rejection on the new recurrence evidence:
the hook the doctrine deferred as speculative is now the systemically-needed one, and
it stays PURE/MINIMAL/REGISTERED/no-op exactly as the doctrine demands. The two
remaining envelope-subclass sources (flood_extent, fault_sources) STOP on
orthogonal gaps (a categorical tiled raster mode; a non-LayerURI emission path +
two-tier cache) named for their own waves. Supersedes nothing; extends the hook
contract (ADR 0056/0063/0071) with the post-emit ``envelope`` point + the declarative
``output.result_model`` result-type naming.
