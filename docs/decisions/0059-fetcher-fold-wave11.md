# 0059 - fetcher fold wave-11: copernicus absorption + the tier-2-able sweep (a fold-zero result)

Context: NATE's endgame (ADR 0056/0057, data-router-fold.md) -- every data fetcher
folds into ONE spec-driven engine, coded fetchers -> 0. Wave-11 had two items: (0)
finish the Copernicus DEM absorption NATE verified (same endpoint / same data /
subset-signature of fetch_dem), and (1) sweep the ~15 "tier-2-able" candidates the
data-router-fold.md worklist flagged and fold every one whose parity genuinely
closes on an EXISTING mode with at most a small strictly-no-op declarative
extension. The worklist was explicitly a rough pattern-inferred prior ("NOT a
per-tool audit -- each still gets its own read + two-gate replication before
folding"); waves 5 and 7 already proved audit optimism, so every candidate twin was
read IN FULL before a verdict.

Decision (2026-07-31):

1. **ITEM 0 -- COPERNICUS ABSORBED to an INTERNAL SEAM (LANDED).** A new
   registry tier ``internal`` (``tool_registry.EngineTier``) is added: a
   registry-resolvable tool with NO model-facing surface -- excluded from BOTH the
   default declarable pool AND the retrieval search index (like ``template``, but
   with no door to gate-expand it), reachable ONLY by an in-process
   ``TOOL_REGISTRY[name].fn`` call. ``SourceSpec`` gains ``internal_only: bool``;
   ``registration.register_spec`` maps it to ``tier="internal"`` ahead of the
   catalog-arm branch. The three model-facing-surface producers exclude it:
   ``search_tools._build_index`` (skips ``template``/``internal`` from the index),
   ``tool_retrieval`` fail-open dump, and ``server._default_declarable_registry``.
   ``fetch_copernicus_dem`` sets ``internal_only: true``: it stays THE impl that
   ``fetch_dem`` (source="copernicus" mode + the 3DEP-outage fallback),
   ``_hydrology_common``, ``model_debris_flow`` and ``compute_sediment_yield``
   resolve in-process, but the model never sees it as a tool. Its corpus phrasings
   re-home onto ``fetch_dem`` (corpus.yaml), its sibling ``corpus.yaml`` is deleted,
   and both docstrings lose the dual-state prose (fetch_dem stops naming the folded
   tool; the copernicus spec docstring becomes an internal-seam note). It is removed
   from ``PRIMARY_CATEGORY`` + the cross-list (a categorized internal tool would
   re-leak via ``open_category``, exactly the template exclusion). Verified offline:
   registry 190 unchanged, n_specs 27 unchanged, tier="internal", NOT in the
   declarable pool, NOT in the search index, still resolvable; all six
   copernicus-intent phrasings rank ``fetch_dem`` #1 in the model-free top-8 with
   ``fetch_copernicus_dem`` absent. Net: the model-facing declarable count drops by
   1; coded-tool + coded-fetcher counts are UNCHANGED (copernicus was already
   spec-driven since wave-8, no py twin).

2. **ITEM 1 -- the tier-2-able sweep FOLDS ZERO, per the STOP RULE (evidence
   below).** All ~15 candidates were read in full (three dossier passes, each
   grounded in the twin source + the executor + the installed ``dataretrieval``
   package + fetcher-fold-audit.md). Every one requires either a NEW mode or a NEW
   primitive -- NOT a strictly-no-op declarative extension on an existing mode --
   so "fold-fewer-fully beats force-fitting" resolves the whole sweep to DEFER. The
   worklist's tier-2 label was a shape-family genealogy, not a claim the current
   executors already cover the source; at the code level they do not.

   ArcGIS-class vectors (target: vector-fgb ArcGIS mode):
   - fetch_fema_nfhl_zones: DEFER -- OBJECTID-cursor pagination is a DELIBERATE
     workaround for the NFHL endpoint 500-ing on ``resultOffset>0``; the executor's
     only paging mode IS resultOffset (porting it re-introduces the exact bug the
     twin dodges). Plus a ``zone_filter`` list-of-enum param type that does not exist.
   - fetch_nwi_wetlands: DEFER -- runtime same-URL geojson->esri-json format
     fallback (fallback is endpoint-list-based, cannot swap decode format on one
     URL), a prefix-strip/first-wins property normalizer, and WAF-required
     Accept/Referer headers (AuthSpec carries only user_agent).
   - fetch_epa_frs_facilities: DEFER -- the default program fans out to 5 ArcGIS
     layers and UNIONs the rows (no multi-endpoint-union mode; fallback is
     try-next-on-failure, not fetch-all-and-merge); Superfund Point geometry is
     synthesized from LAT/LON attribute columns, not decoded from geometry.
   - fetch_wdpa_protected_areas: DEFER -- ``designation_filter`` needs an
     alias-normalizer that RAISES on unknown (ParamSpec.aliases passes through), and
     a POST-fetch fail-loud-if-filter-emptied path (the hook set deliberately has no
     post_process). The bbox-only path alone would close, but dropping the param
     breaks signature parity.
   - fetch_usace_dams: DEFER -- credential-gated dual-endpoint selection (async
     secret resolution) with an auth-error that must NOT be masked by the generic
     next-endpoint fallback, plus list-valued ``hazard_potential``/``state`` filters
     needing ``IN (...)`` where-clause construction no param type / where_clauses
     interpolation supports.

   Single-COG rasters (target: raster-cog / stac_float / multi_url / gzip_object):
   - fetch_topobathy: DEFER -- a 4-source composite (CUDEM manifest + ETOPO
     formula-grid + NCEI-regional STAC + 3DEP-via-fetch_dem) each reprojected into a
     shared NON-4326 UTM target grid, a vertical-datum gate, and an extended
     TopobathyResult(LayerURI) result type.
   - fetch_3dep_extra: DEFER -- access is an opaque ``pfdf.data.usgs.tnm.dem.read``
     library call (its own tile discovery/mosaic; no declarable endpoint/STAC/VRT),
     error classification by substring-matching the library's exception text, and a
     per-resolution payload coefficient table PayloadEstimateSpec cannot hold.
   - fetch_landcover: DEFER -- OGC WCS 1.0.0 GetCoverage (unbuilt mode), returns a
     dict not a LayerURI, and a categorical-raster background-transparency pixel
     remap + palette-preserving two-path COG translate.
   - fetch_ghsl_population: DEFER -- needs the ``multi_url`` ``mode: tile_grid`` the
     executor comments name as future/unbuilt (formula-addressed
     ``/vsizip//vsicurl/`` 10-degree tiles), plus a per-tile pixel cap and
     negative-as-nodata threshold masking.
   - fetch_noaa_slr_confidence / fetch_noaa_slr_marsh: DEFER (a pair) -- ArcGIS
     ``MapServer/export`` returning a RENDERED PNG32 georeferenced client-side into a
     4-band RGBA COG; no such mode exists and ``array_to_cog_bytes`` has no RGBA
     ColorInterp path. They would fold together IF a ``mapserver_export_rgba`` mode
     were built.

   dataretrieval / station-timeseries (target: dataretrieval_delegate /
   station_timeseries):
   - fetch_usgs_nwis_gauges: DEFER -- dual state/bbox selector + dual
     latest-vs-hydrograph output schema selected at RUNTIME by input, plus an
     RDB-tab-delimited fallback parser; audit's own table marks it BESPOKE.
   - fetch_usgs_groundwater_levels: DEFER (the sole prior FOLD, refuted on read).
     The twin hits the Water Data OGC API with RAW urllib + bespoke GeoJSON parse /
     left-join / mlid-prefix-strip and appends ``state_code`` to the measurements
     URL (line 320). The delegate mode delegates to the ``dataretrieval`` package -- a
     DIFFERENT client -- and ``get_field_measurements`` has NO ``state_code`` param
     (``utils._with_state`` rejects it), so the twin's primary state selector cannot
     be reproduced and byte-parity on the raw-OGC GeoJSON path is not established. A
     faithful fold needs a NEW vector-fgb OGC-API mode (limit/offset paging + native
     GeoJSON + state_code), not the existing delegate.
   - fetch_snotel_snow: DEFER (closest of the station siblings) -- target is
     station_timeseries snapshot, but needs a registered snapshot.transform selector
     for "latest non-null in a trailing window from a BATCHED multi-station response"
     AND null-tolerant station survival (snapshot path drops a None station); NRCS
     AWDB is outside ``dataretrieval`` entirely. On the ledger's "modes not yet
     built" deferral list.
   - fetch_asos_metar / fetch_raws_weather: DEFER -- IEM per-state multi-endpoint
     discovery routing + one-row-per-OBSERVATION output (station_timeseries emits one
     Point per station); RAWS adds a nested station x day fetch loop with a
     rate-limit throttle. Both on the ledger's named-deferral list.

Registry accounting: registry total UNCHANGED at 190; coded tools 163 -> 163 (0);
coded fetchers 72 -> 72 (0); spec-served data sources 27 -> 27 (0). Model-facing
declarable pool: -1 (fetch_copernicus_dem left the ambient pool + the search index
via tier="internal"). Retrieval index unshifted for every other tool (the 27 specs'
docstrings untouched); the copernicus re-route to fetch_dem verified (6/6 phrasings
fetch_dem top-8, copernicus absent). No consumer re-point needed (fetch_dem +
_hydrology_common + model_debris_flow + compute_sediment_yield still resolve
fetch_copernicus_dem via TOOL_REGISTRY -- the seam is retained). Daemon import clean.

Non-gating divergences flagged: none new. The one prior FOLD candidate
(fetch_usgs_groundwater_levels) is refuted with a concrete, grounded root cause
(client mismatch + unreproducible state_code selector), not fudged.

Consequence: the router gains an ``internal`` tier -- the mechanism for absorbing a
public tool's sibling into an in-process seam without a model-facing surface, the
clean end state for a "same endpoint / same data / subset-signature" duplicate. The
tier-2-able worklist is now READ, not inferred: the honest residue is that every one
of the ~15 needs a scoped NEW mode/primitive (OGC-API vector, list-of-enum +
IN()-where-clause param, batched/fan-out station fetch, WCS raster,
mapserver_export RGBA raster, multi_url tile_grid) -- these are the next MODES the
scoped-job track builds, each collapsing a family once, exactly as
multi_url/gzip_object/hooks did. Supersedes nothing; extends the
fold-some-defer-rest precedent (ADR 0045/0047/0052/0053/0054/0055/0056) with an
honest fold-zero sweep + one new surface-hiding tier.
