"""``SourceSpec`` -- the generic data-router source specification (data, not code).

One ``source.yaml`` per data source, co-located beside the source's existing
tool folder (sibling to ``fetch_X.py`` + ``corpus.yaml``). This module owns the
pydantic v2 shape the router loader AND the parity harness both validate against,
so the two paths cannot drift -- exactly mirroring ``CatalogEntry`` (catalog.py)
for the curated catalog.

Authority: ``docs/specs/router-pilot-contract.md`` sec 1 (SOURCE SPEC SCHEMA)
and ``docs/specs/data-router-fold.md`` (architecture + retention principle).

The spec captures the ~15-25% source-specific body the audit
(``docs/specs/fetcher-fold-audit.md``) found; the shared router provides the
~75-85% boilerplate (typed errors, cache read-through, payload gate, LayerURI)
ONCE. INDISTINGUISHABILITY: a spec-driven source must flow through the identical
pipeline and surface identically to a catalog-native / hand-written one.

Design note (open decision #1, router-pilot-contract sec 6): the ``SourceSpec``
home is ``trid3nt_contracts`` (shared with the harness, mirrors ``CatalogEntry``),
not router-package-local. This pins CONTRACTS for shared validation.

The shape-specific ``ingest`` block and the ``join`` transform block are typed as
flexible ``dict[str, Any]`` sub-blocks (they vary per shape per contract sec 1.2);
every OTHER top-level key is a strict typed sub-model. The top-level model uses
``extra="forbid"`` (via ``GraceModel``) so an unknown top-level key is a defect,
never silently dropped.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from .common import GraceModel
from .tool_registry import ResolutionSpec, TTLClass

__all__ = [
    "SourceShape",
    "AuthMode",
    "ParamType",
    "PayloadModel",
    "EndpointSpec",
    "AuthSpec",
    "ParamSpec",
    "GateSpec",
    "NormalizeSpec",
    "OutputSpec",
    "CacheSpec",
    "PayloadEstimateSpec",
    "HookSpec",
    "DispatchSpec",
    "SourceSpec",
    "STYLE_GEOMETRIES",
    "STYLE_KINDS",
]

#: The preset family, closed. A style row naming anything else is a typo, and a
#: typo that reaches a layer paints it as something it is not.
STYLE_KINDS = ("continuous", "classed", "reference", "mesh")
STYLE_GEOMETRIES = ("point", "line", "polygon")


def _validate_style_row(name: str, row: Any, *, where: str = "output.style") -> None:
    """Reject a style row at REGISTRATION rather than at paint time."""
    if row is None:
        return
    if not isinstance(row, dict):
        raise ValueError(f"{name}: {where} must be a mapping; got {type(row).__name__}")
    kind = row.get("kind", "continuous")
    if kind not in STYLE_KINDS:
        raise ValueError(f"{name}: {where}.kind {kind!r} not in {list(STYLE_KINDS)}")
    geometry = row.get("geometry")
    if geometry is not None and geometry not in STYLE_GEOMETRIES:
        raise ValueError(
            f"{name}: {where}.geometry {geometry!r} not in {list(STYLE_GEOMETRIES)}")
    by_param = row.get("by_param")
    if by_param is None:
        return
    if not isinstance(by_param, dict) or not by_param.get("param"):
        raise ValueError(f"{name}: {where}.by_param needs a param name")
    for value, mapped in (by_param.get("map") or {}).items():
        _validate_style_row(name, mapped, where=f"{where}.by_param.map[{value!r}]")


# --------------------------------------------------------------------------- #
# Enums (contract sec 1.1)
# --------------------------------------------------------------------------- #

#: The ingestion shape that selects the base executor (contract sec 2). The
#: two HYBRID pilots (esri_landcover, census_acs) declare a base shape PLUS a
#: transform block (``ingest.mosaic`` / ``join``) that wraps the base executor.
SourceShape = Literal[
    "raster-cog",
    "vector-fgb",
    "station-timeseries-fgb",
    "record",
    "animation_frames",
]

#: Auth mode (contract sec 1.1 ``auth.mode``). ``none`` = keyless public;
#: ``api_key_env`` = optional/required key from an env var (census keyless
#: fallback); ``cds`` / ``vault`` / ``token`` reserved for later families.
AuthMode = Literal["none", "api_key_env", "cds", "vault", "token"]

#: Request-param declared types (contract sec 1.1 ``params.<name>.type``).
#: ``int_range`` = a 2-element ``[start, end]`` int list (mtbs year_range);
#: ``date_compact`` = ``YYYY-MM-DD`` / ``YYYYMMDD`` normalized to ``YYYYMMDD``
#: (us_drought_monitor date). Both are phase-2 wave-2 ArcGIS-family additions.
#: ``point`` = a 2-element ``[lon, lat]`` float list (nldi seed_point); the
#: router coerces + finite-checks it and leaves the CONUS / mutual-exclusion
#: gate to the delegating executor (phase-2 wave-3).
#: ``float_list`` = a scalar float OR a ``list[float]`` (slr_scenarios
#: scenario_ft) validated against ``values`` (the allowed level set),
#: sorted + deduped -- the fan-out mode's per-value driver (phase-2 wave-6,
#:). A scalar is coerced to a 1-element list.
#: ``str_list`` = a ``list[str]`` free-text filter set (nws_event event_types);
#: each entry stripped, empties dropped, sorted + deduped for cache-key
#: stability -- the string sibling of ``float_list`` with no allowed-set gate
#: (tier-3 hook wave). A scalar string is coerced to a 1-element list.
#: ``bool`` = a truthy flag param (nws_river_forecast include_thresholds /
#: include_series). Coerced with ``bool(value)`` (the twin's ``bool(flag)``
#: contract); the promoted signature annotates it ``bool`` (chained-resolution
#: mode). No prior spec declares it (strict no-op).
#: ``datetime_range`` = a 2-element ``[start, end]`` ISO datetime-pair list
#: (movebank ``time_range``). Each entry parses as an ISO date OR datetime; the
#: router coerces to a ``(datetime, datetime)`` tuple with ``start <= end`` and
#: echoes ``[start.isoformat(), end.isoformat()]`` for cache-key stability -- the
#: datetime sibling of ``int_range`` no ``iso_date`` pair carries (a raw
#: datetime-pair kwarg, LayerURI-envelope wave). No prior spec declares
#: it (strict no-op).
ParamType = Literal[
    "bbox", "iso_date", "enum", "int", "float", "str", "int_range", "date_compact",
    "point", "float_list", "str_list", "bool", "datetime_range",
]

#: Payload-estimate models (contract sec 1.1 ``payload_estimate.model``).
PayloadModel = Literal["bbox_area", "per_station", "per_feature", "tiled"]


# --------------------------------------------------------------------------- #
# Typed sub-blocks
# --------------------------------------------------------------------------- #


class EndpointSpec(GraceModel):
    """One named endpoint (contract sec 1.1 ``endpoints.<name>``).

    Exactly one of ``url`` / ``url_template`` is the base; ``query`` carries the
    static query params merged onto every request to this endpoint.
    """

    url: str | None = None
    url_template: str | None = None
    query: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _one_url(self) -> "EndpointSpec":
        if not (self.url or self.url_template):
            raise ValueError("endpoint requires one of url / url_template")
        return self


class AuthSpec(GraceModel):
    """Auth mode + shared User-Agent (contract sec 1.1 ``auth``)."""

    mode: AuthMode = "none"
    #: For ``mode="api_key_env"``: which env var + whether it is required (a
    #: ``required: false`` var expresses the keyless-fallback pattern, census).
    api_key_env: dict[str, Any] = Field(default_factory=dict)
    user_agent: str = "trid3nt_default"


class ParamSpec(GraceModel):
    """One request-param's validation contract (contract sec 1.1 ``params``).

    Validated BEFORE any network call (audit boilerplate: ``_validate_bbox`` /
    ``_validate_date_range`` / ``_validate_<enum>``). ``quantize`` is a bbox
    directive (``round_6dp`` or ``res_<m>`` for raster resolution snap).
    """

    type: ParamType
    required: bool = False
    default: Any = None
    values: list[Any] | None = None          # enum only
    quantize: str | None = None              # bbox only: round_6dp | res_<m>
    max_range_days: int | None = None        # iso_date pair ceiling (end param)
    #: int/float inclusive range gate (esri year [2017,2023]). Out-of-range ->
    #: a typed input error stamped with this param's ``error_suffix``.
    min: float | None = None
    max: float | None = None
    #: iso_date coverage bounds (gridmet: start >= 1979-01-01, end <= today+1).
    #: A violation is a typed ``*_NOT_AVAILABLE`` (twin GRIDMETNotAvailableError),
    #: distinct from the ISO-format / range-days input errors.
    min_date: str | None = None              # static ISO lower coverage bound
    max_future_days: int | None = None       # end <= today + N (future ceiling)
    #: Per-param A.6 input-error suffix override (VERDICT round-2). Most sources
    #: use the spec-level ``input_error_suffix``; esri splits per-param
    #: (bbox -> BBOX_INVALID, year -> YEAR_INVALID). Default (None) = spec-level.
    error_suffix: str | None = None
    #: Force this None-default param OPTIONAL in the promoted inputSchema
    #: (phase-2 wave-3). The adapter marks a None-default NON-Optional
    #: annotation as required-in-schema (the wave-2 quirk). A twin that annotated
    #: the param ``T | None = None`` is NOT required; setting ``schema_optional:
    #: true`` reproduces that (wqp bbox, nldi seed_point/comid). Default False
    #: preserves the wave-2 required behavior for every prior spec.
    schema_optional: bool = False
    #: str-param alias table (phase-2 wave-3). When set, a str value is
    #: lower-cased + stripped and mapped through this table; an unmapped value
    #: passes through verbatim (stripped) -- exactly the wqp
    #: ``_resolve_characteristic`` alias-or-passthrough contract. Default (None) =
    #: no aliasing (strict no-op for every prior spec).
    aliases: dict[str, str] | None = None
    #: enum only: lower-case + strip the value BEFORE the allowed-set check
    #: (epa_ejscreen ``indicator`` accepts case-insensitive aliases, echoing the
    #: normalized key). Default False = the byte-identical strict-match behaviour
    #: for every prior enum param.
    lowercase: bool = False


class GateSpec(GraceModel):
    """Pre-fetch gates (contract sec 1.1 ``gates``)."""

    conus_only: bool = False                 # bbox-intersects-CONUS (gridmet)
    #: Per-spec CONUS envelope override ``(west, south, east, north)``. Absent
    #: (None) -> the shared gridmet-derived envelope in ``router._CONUS_BBOX``.
    #: A spec whose own staged/served grid extends further than that generic
    #: envelope (groundwater_recharge's Reitz grid reaches south to 24.0625,
    #: past gridmet's 25.05 -- false-refusing Key West) declares its own real
    #: bounds here instead of borrowing an unrelated source's footprint.
    conus_bbox: tuple[float, float, float, float] | None = None
    max_bbox_deg2: float | None = None       # hard ceiling (esri_landcover: 8.0)
    #: Hard ceiling on the bbox area in APPROXIMATE km^2 (WGS84, cos-lat scaled --
    #: the ``_fetch_common._bbox_area_km2`` model). Distinct from ``max_bbox_deg2``
    #: (raw degree^2): fetch_river_geometry's twin enforced a 5000 km^2 guardrail
    #: on the quantized bbox, which a degree^2 ceiling cannot express identically at
    #: varying latitude. Default (None) = no km^2 ceiling (strict no-op for priors).
    max_bbox_km2: float | None = None
    max_stations: int | None = None          # station-timeseries only
    max_features: int | None = None          # vector only (paging cap)


class NormalizeSpec(GraceModel):
    """Normalization stamps (contract sec 1.1 ``normalize``) -- indistinguishability."""

    crs: str = "EPSG:4326"
    units: str | None = None
    datum: str | None = None
    quantity: str | None = None
    orientation: str | None = None           # raster only (gridmet no-sortby lesson)
    #: Emit ``LayerURI.units`` from a request param's resolved value rather than
    #: the static ``units`` (phase-2 wave-3). The wqp twin stamps
    #: ``units=<resolved characteristic>``; setting ``units_from_param:
    #: characteristic`` reproduces it. Default (None) = the static ``units`` stamp.
    units_from_param: str | None = None
    #: Per-param MAPPED units (phase-2 wave-7). Unlike ``units_from_param`` (which
    #: stamps the raw param value), this maps a param value through a table to the
    #: units string -- ``{"param": "layer", "map": {"cbh": "m * 10", ...}}`` -- so a
    #: value ABSENT from the map stamps ``LayerURI.units=None`` (the landfire/usfs
    #: categorical-vs-scaled split). Default (None) = no per-param units mapping
    #: (strict no-op for every prior spec). Overrides the static ``units`` stamp.
    units_by_param: dict[str, Any] | None = None


class OutputSpec(GraceModel):
    """Output surface (contract sec 1.1 ``output``)."""

    #: ``record``: the source returns a bare JSON dict, NOT a renderable
    #: LayerURI. ``route()`` runs the ``hooks.record`` dict builder, caches its JSON
    #: bytes via ``read_through``, and returns the parsed dict envelope (honesty floor
    #: intact: the hook raises typed input/empty/upstream errors; no fabricated
    #: success). Pairs with ``shape: record`` + ``ext: json``. The wfigs/fault/
    #: population/lehd record fetchers whose result is a structured lookup (a point +
    #: bbox discovery, a jobs summary) rather than a map layer. Default raster/vector
    #: (strict no-op for every prior spec).
    layer_type: Literal["raster", "vector", "record"]
    ext: Literal["tif", "fgb", "json"]
    role: Literal["primary", "context", "input"] = "primary"
    #: HOW THIS DATASET DRAWS ITSELF - the ``style:`` row. A dataset's default
    #: rendering is a fact about the DATA, so it is declared here beside the
    #: source rather than mapped to it somewhere else. ``{kind: continuous |
    #: classed | reference | mesh}`` picks one of the four preset shapes and the
    #: remaining keys parameterise it (``ramp`` / ``units`` / ``label`` /
    #: ``scale`` / ``classes`` / ``geometry`` / ``color``). ``by_param`` -
    #: ``{param: <name>, map: {<value>: <partial row>}}`` - overrides those
    #: parameters per param value, which is how one source that serves several
    #: variables gives each its own ramp and range. Absent (None) = the kind's
    #: bare default.
    style: dict[str, Any] | None = None
    #: Per-param MAPPED role (multi-asset RGB composite wave). ``{"param":
    #: "band_combo", "map": {"thermal": "primary"}}`` selects the LayerURI ``role`` by
    #: a param value; a value absent from the map falls back to the static ``role``.
    #: The landsat thermal LST product is the analytical ``primary`` while the RGB
    #: true/false-color composites are ``context`` basemaps -- one style row, split
    #: role. Default (None) = the static ``role`` for every prior spec (strict no-op).
    role_by_param: dict[str, Any] | None = None
    #: Whether the emitted ``LayerURI`` carries the request bbox. Default True
    #: (census/coops/hifld/esri set it); gridmet's twin omits it, so its spec
    #: sets ``emit_bbox: false`` to stay byte-identical (VERDICT round-2 tell).
    emit_bbox: bool = True
    #: The HUMAN-facing layer name, verbatim. Overrides the router's default
    #: ``"<source_class> <variable>"`` stamp on ``LayerURI.name``, which is a
    #: machine identifier and reads as one in the QGIS layer tree. Set it where
    #: the product's identity is not obvious from the tool name -- a MODELLED
    #: product must say so here, because the layer name is what a person reads
    #: off the map. Default (None) = the machine stamp (strict no-op for every
    #: prior spec).
    display_name: str | None = None
    #: Stamp ``LayerURI.bbox`` from the EXTENT of the emitted vector features
    #: rather than the request bbox (tier-3 hook wave). A dict
    #: ``{pad: <deg>}`` -- the point-event fetchers (earthquakes / tsunami /
    #: volcano) auto-zoom the camera to the events' bounds, padding a degenerate
    #: single-point axis by ``pad`` degrees. The router reads the extent back from
    #: the produced FGB (available on both cache hit + miss), so the stamp is
    #: consistent regardless of the cache path. Default (None) = no override
    #: (strict no-op for every prior spec; ``emit_bbox`` governs the bbox).
    bbox_from_features: dict[str, Any] | None = None
    #: Name a ``LayerURI`` SUBCLASS result model (LayerURI-envelope wave, ADR
    #: 0073). A string key into ``trid3nt_contracts.execution.LAYER_RESULT_MODELS``
    #: (e.g. ``HighWaterMarksLayerURI``): the router builds this subclass from the
    #: base LayerURI + the ``hooks.envelope`` field dict, so a source that returns a
    #: business-field-carrying subclass folds without a coded twin. Declared TOGETHER
    #: with ``hooks.envelope`` (registration validates both the name resolves and the
    #: pairing). Default (None) = the plain LayerURI (strict no-op for every prior
    #: spec).
    result_model: str | None = None
    #: EMPTINESS-DRIVEN output switch (finisher-mechanisms wave). A
    #: hook name ``<source>.<point>`` called with ``(spec, params)`` when the
    #: produced vector FGB is FEATURE-EMPTY: its returned dict is what ``route()``
    #: returns INSTEAD of the LayerURI, reproducing a twin whose non-empty path is
    #: a renderable ``LayerURI`` (subclass) but whose empty-AOI degrade is a bare
    #: record dict + typed note (fetch_fault_sources: a zero-fault AOI is NOT given
    #: a layer -- the honesty gate). A non-empty fetch is unaffected (the LayerURI /
    #: envelope path). Validated as a resolvable hook at load. Default (None) = the
    #: LayerURI is always returned (strict no-op for every prior spec).
    variant_by_emptiness: str | None = None
    #: FETCH-TIME PROVENANCE CHANNEL. When True, ``route`` binds a
    #: :class:`ProvenanceRecorder` around the fetch so the delegate/executor can
    #: ``record_provenance({...})`` a small typed dict during a NON-cached fetch;
    #: the cache persists it as a ``<key>.provenance.json`` sibling of the artifact
    #: and REPLAYS it on a cache hit, and the router hands it to the ``hooks.envelope``
    #: hook so a result-model field that is FETCH-TIME provenance (which of a
    #: multi-source composite's legs painted the merge -- unrecoverable from the final
    #: bytes) survives every cache path. Requires ``hooks.envelope`` (the consumer).
    #: Default False = no recorder, no sidecar, byte-identical to before (strict
    #: no-op for every prior spec).
    provenance: bool = False
    #: Keep attribute-only (NULL-geometry) features in the emitted FGB instead of
    #: dropping them (chained-resolution mode). The nws_alerts_conus twin
    #: preserves alerts whose zone references could not be resolved as NULL-geometry
    #: rows (property table survives, no map footprint) and writes with
    #: ``SPATIAL_INDEX=NO`` (pyogrio rejects a spatial index over NULL geometry).
    #: Default False = the byte-identical drop-null behaviour for every prior vector
    #: spec (which never emits a NULL-geometry feature).
    keep_null_geometry: bool = False


class CacheSpec(GraceModel):
    """Cache TTL class (contract sec 1.1 ``cache``)."""

    ttl_class: TTLClass


class PayloadEstimateSpec(GraceModel):
    """Payload-MB estimator inputs (contract sec 1.1 ``payload_estimate``).

    The router SYNTHESIZES ``estimate_payload_mb(**args) -> float`` from these
    fields so server.py's ``tool-payload-warning`` seam reads it identically to
    a hand-written twin's estimator (>25MB warns, >250MB blocks, #154 gate).
    All coefficient fields are optional; each ``model`` reads the ones it needs.
    """

    model: PayloadModel
    floor_mb: float = 0.01
    #: Optional upper clip on the estimate (phase-2 wave-7). The usfs twin clips
    #: its estimate to ``[0.05, 50]``; ``ceil_mb: 50`` reproduces the ceiling.
    #: Default (None) = no ceiling (strict no-op for every prior spec).
    ceil_mb: float | None = None
    # bbox_area / tiled
    mb_per_sq_deg: float | None = None
    #: Per-param MB/deg^2 coefficient table for the ``bbox_area`` model.
    #: ``{"param": "resolution", "map": {"1 arc-second": 5.0, "1 meter": 5000.0, ...},
    #: "default": 50.0}`` -- fetch_3dep_extra's estimate scales the SAME bbox area by a
    #: per-resolution coefficient (5 / 500 / 5000 / 1 / 200 MB/deg^2) the single
    #: ``mb_per_sq_deg`` scalar cannot hold. When set it overrides ``mb_per_sq_deg``
    #: for the resolved param value (a value absent from the map uses ``default`` else
    #: ``mb_per_sq_deg`` else 0.01). Default (None) = the scalar coefficient (strict
    #: no-op for every prior bbox_area spec).
    mb_per_sq_deg_by_param: dict[str, Any] | None = None
    # per_station
    kb_per_station_per_day: float | None = None
    overhead_kb: float | None = None
    stations_per_sq_deg: float | None = None
    # per_feature
    kb_per_feature: float | None = None
    features_per_sq_deg: float | None = None
    # tiled
    mb_per_tile: float | None = None
    tile_deg2: float | None = None


class HookSpec(GraceModel):
    """Named extension points for the ONE irreducible per-source step.

    The tier-3 hook contract: a source whose bespoke-ness is a single clean step
    the declarative modes cannot express references a REGISTERED PURE FUNCTION by
    name here, and the router calls it at that point. Everything else -- transport,
    retry, caching, gates, payload estimate, LayerURI, typed-error machinery --
    stays router-owned; a hook only computes (it performs no I/O).

    The set is MINIMAL, derived from the wave-10 evidence (5-6 bespoke fetchers
    read): the request-construction step and the payload-decode step are the two
    that the declarative param/ingest surface genuinely cannot carry for a
    single-GET / multi-GET / paged JSON point-event API. A post-process point was
    evaluated and NOT added -- the only observed post-serialize need (stamp the
    camera bbox from the feature extent) is declarative via
    ``output.bbox_from_features``, so a post_process hook would be speculative
    infra. A future wave adds a field here only when a real source needs it.

    Hook name convention: ``<source_key>.<point>`` (e.g.
    ``usgs_earthquakes.build_request``). ``registration.register_spec`` validates
    each declared name resolves in ``_router.hooks.HOOK_REGISTRY`` at load.
    """

    #: ``(spec, params) -> list[RequestPlan]``. Constructs the source-specific
    #: request(s) -- URL + query params + headers + any bespoke pre-fetch input
    #: validation the declarative param gates cannot express (FDSN window
    #: resolution + 366-day cap; NCEI year-window). Returns 1..N plans (N=1 single
    #: GET, N>1 a static multi-endpoint set the parse hook joins). For a paged
    #: source the router calls it once per page, injecting the page param.
    build_request: str | None = None

    #: ``(spec, params, bodies: list[bytes]) -> list[GeoJSON-feature dict]``.
    #: Decodes the source-specific payload(s) into GeoJSON-ish point features the
    #: shared ``vector_fgb`` serializer writes. Raises the router's typed
    #: EMPTY / RESULT_TOO_LARGE / UPSTREAM errors (source-stamped via the shared
    #: factories) on the honest-empty / cap / bad-body paths -- the twin's
    #: no-events / too-large gate lives here, the one irreducible decode step.
    parse_response: str | None = None

    # --- chained-resolution mode: resolve-then-fetch / bounded
    # --- per-item detail enrichment. Two composable phases; a source declares ---
    # --- only the phase(s) it needs. The router owns the orchestration + the ---
    # --- transport + the bounded/deduped/best-effort detail loop; these hooks ---
    # --- are the source-specific PURE compute at each edge. -------------------- #

    #: PHASE R (resolve, PRE-cache-key). ``(spec, params) -> list[RequestPlan]``.
    #: Build the round-1 resolution request(s) (name -> id: a ``species/match`` /
    #: ``/v1/taxa`` GET). Returns ``[]`` to signal "params already carry the
    #: canonical id, skip the round trip" (gbif int taxonKey, inat digit string).
    #: Runs in ``route()`` BEFORE ``read_through`` so the resolved id enters the
    #: cache key (a name query and its id query collapse to one cache entry, the
    #: twin's contract).
    resolve_build: str | None = None

    #: PHASE R. ``(spec, params, bodies: list[bytes]) -> dict[str, Any]``. Decode
    #: the resolution body/bodies into a params-MERGE dict (``{"taxon_key": 12345}``)
    #: the router folds into ``params`` before the main fetch. Raises the typed
    #: INPUT (unknown / ambiguous name) / UPSTREAM (bad body) errors -- the twin's
    #: matchType / no-results gate is this one irreducible step.
    resolve_parse: str | None = None

    #: MAIN-FETCH offset paging. ``(spec, params, bodies: list[bytes]) -> RequestPlan
    #: | None``. Given every page fetched so far (``bodies``), return the NEXT page's
    #: request or ``None`` to STOP -- the pure offset/endOfRecords/total_results loop
    #: control the ``totalPages`` declarative pager cannot express (gbif offset+=300
    #: to ``endOfRecords``; inat page+=1 to ``total_results``). ``build_request``
    #: still builds page 1; the router owns the loop + a hard ``max_pages`` ceiling.
    next_page: str | None = None

    #: PHASE E (enrich). ``(spec, params, features: list[dict]) -> list[tuple[str,
    #: RequestPlan]]``. From the round-1 features, emit the ORDERED ``(ref_key,
    #: RequestPlan)`` detail-request set (per-item detail URLs derived from the
    #: parsed fields: alert zone polygons, gauge threshold/stageflow detail). The
    #: hook applies the source's per-pass cap by only emitting that many refs; the
    #: router dedupes by ``ref_key``, bounds by ``ingest.chained.max_detail_fetches``,
    #: and fetches each best-effort (a failed ref is recorded, never silently dropped).
    enrich_plan: str | None = None

    #: PHASE E. ``(spec, params, features: list[dict], results: dict[str, DetailResult])
    #: -> list[dict]``. Fold the fetched detail (keyed by ``ref_key``; each carries a
    #: body OR a typed error) back into the features and return the final feature
    #: list. EVERY input feature survives (a feature whose refs failed keeps its row
    #: with null/None detail -- the never-silent-drop rule), the twin's best-effort
    #: enrichment join.
    enrich_merge: str | None = None

    #: POST-EMIT ENVELOPE (LayerURI-envelope wave).
    #: ``(spec, params, layer: LayerURI, data: bytes) -> dict[str, Any]``. The
    #: last hook the router calls: it receives the ASSEMBLED base ``LayerURI`` +
    #: the produced bytes (FGB/COG, available on cache hit + miss) and returns the
    #: EXTRA business fields for the spec's ``output.result_model`` subclass (HWM
    #: quality/type/datum breakdown + caveats/notes; a flood-extent
    #: class_breakdown/flood_area; a fault kinematic list) plus any base-field
    #: overrides (``name`` / ``units``). PURE: it only computes over already-fetched
    #: bytes (no transport). The router drops the honesty-floor-owned keys (``uri`` /
    #: ``layer_type``) from the returned dict so a hook can ADD fields but NEVER flip
    #: an error to success or re-point the layer. Pairs with ``output.result_model``
    #: (declared together); no prior spec declares it (strict no-op: the router
    #: emits the plain LayerURI unchanged).
    envelope: str | None = None

    #: LIBRARY-DELEGATE call (generalizing the dataretrieval precedent).
    #: ``(spec, params, *, timeout_s: float) -> features | (array, transform, crs)``.
    #: The ONE sanctioned impurity: a source whose maintained LIBRARY owns discovery
    #: + the socket (pfdf, HRRR-Zarr) names a registered hook that CALLS the library
    #: and returns arrays/frames; the router keeps params/gates/stamps/cache/publish/
    #: typed-errors. Constrained by the router: a declared timeout
    #: (``ingest.delegate.timeout_s``) is passed in, the wrapper marks the call
    #: library-owned in telemetry, and any library exception the hook did not itself
    #: map to a typed router error is caught as a retryable upstream error. A vector
    #: spec returns GeoJSON features (serialized by the shared ``vector_fgb`` writer);
    #: a raster spec (``ingest.access: library_delegate``) returns ``(array,
    #: transform, crs)`` (serialized by the shared COG writer). No prior spec declares
    #: it (strict no-op).
    delegate: str | None = None

    #: LIBRARY-DELEGATE pre-cache input validation. ``(spec, params) ->
    #: None``. Runs in ``route()`` AFTER type/gate validation and BEFORE
    #: ``read_through`` -- the source-specific input gate the declarative param/gate
    #: surface cannot express (pfdf statsgo's exact CONUS envelope, 3dep's US bounds)
    #: raised pre-cache / pre-network, byte-identical to the twin (which validates in
    #: its body before read_through) and offline-testable. Generalizes the
    #: dataretrieval ``pre_validate`` step. No prior spec declares it (strict no-op).
    delegate_validate: str | None = None

    #: RECORD-RETURN dict builder. ``(spec, params, bodies: list[bytes])
    #: -> dict | None``. For a ``shape: record`` / ``output.layer_type: record``
    #: source whose result is a bare structured JSON dict (a discovery record, a
    #: summary), NOT a renderable LayerURI. The router owns the transport (fetches the
    #: ``hooks.build_request`` plan(s)) and the cache; this PURE hook shapes the fetched
    #: bodies into the result dict. Returning ``None`` for a given (ordered) plan's body
    #: signals "no usable record in this response, try the next plan" -- the router's
    #: record executor walks the build plans in order and stops at the first non-None
    #: dict (the wfigs Current->YearToDate best-feature short-circuit); if EVERY plan
    #: yields None the router raises the source's typed empty/not-found error (honesty
    #: floor: the hook never fabricates a success dict, and a bad body still raises a
    #: typed upstream error via the shared factories). No prior spec declares it (strict
    #: no-op).
    record: str | None = None

    #: SOCKETED PRE-CACHE-KEY delegate resolve. ``(spec, params, *,
    #: timeout_s: float) -> dict``. The delegate sibling of the chained-resolution
    #: ``resolve_build``/``resolve_parse`` (which resolve over the router's http
    #: transport): a source whose cycle/key resolution walks a LIBRARY socket (HRRR-
    #: Zarr's s3fs ``fs.exists`` backward cycle walk) names this hook. It runs in
    #: ``route()`` AFTER type/gate + ``delegate_validate`` and BEFORE ``read_through``,
    #: under the SAME ``library_delegate`` constraints as ``delegate`` (declared
    #: ``ingest.delegate.timeout_s``, telemetry marks it library-owned, an unmapped
    #: library exception -> retryable upstream). Its dict return MERGES into ``params``
    #: so the resolved cycle enters the cache key (a ``cycle=None`` request would
    #: otherwise compute a non-deterministic key). Pairs with ``hooks.delegate``. No
    #: prior spec declares it (strict no-op).
    delegate_resolve: str | None = None

    #: GENERIC PRE-CACHE-KEY resolve (landcover + flood-extent wave).
    #: ``(spec, params) -> dict``. A source whose cache key depends on a value that
    #: must be resolved from the network BEFORE ``read_through`` -- but over the
    #: shared HTTP transport, NOT a library socket (the ``delegate_resolve`` sibling
    #: for keyless HTTP) -- names this hook. It runs in ``route()`` AFTER type/gate
    #: validation and BEFORE ``read_through``; its returned dict MERGES into
    #: ``params`` so the resolved value enters the cache key (a ``date=None``
    #: latest-available request would otherwise compute a non-deterministic key and
    #: forever serve the first-cached day). Two resolvers exist as socket delegates
    #: (``delegate_resolve``) and single-round HTTP (``resolve_build``/``_parse``);
    #: this is the multi-step HTTP dir-walk case (the LANCE MCDWD year->doy listing
    #: walk) neither expresses. No prior spec declares it (strict no-op).
    pre_resolve: str | None = None

    #: POST-ARRAY PER-BAND COLORMAP (raster-modes wave). ``(spec, params)
    #: -> dict[int, tuple[int, int, int, int]]``. A ``raster-cog`` source whose
    #: rendered palette is a PURE function of a request param (the jrc-gsw per-band
    #: occurrence/recurrence/seasonality/change ramp, computed from ``band`` alone --
    #: never reads the fetched array, does no I/O) names this hook. The
    #: ``stac_continuous_mosaic`` serializer bakes the returned GDAL ``{value:(r,g,b,
    #: a)}`` table into the emitted uint8 COG's band-1 palette via the existing
    #: ``array_to_cog_bytes(colormap=...)`` seam -- NOT a declarative colormap DSL
    #: (the ramp is computed math, one consumer). PURE: it only computes over the
    #: params. No prior spec declares it (strict no-op: the serializer bakes no
    #: palette unless the spec names the hook).
    colormap: str | None = None

    #: FRAMES-LIST pre-loop RESOLVE (animation wave 1). ``(spec, params)
    #: -> list[FramePlan]``. A ``shape: animation_frames`` source (an ordered
    #: per-timestamp animation that returns ``list[LayerURI]``, not one layer) names
    #: this hook: it does the pre-loop timestamp-index fetch + window + subsample +
    #: (optional) filter and returns the ORDERED list of per-frame plans -- each a
    #: :class:`FramePlan` carrying the frame's ``cache_params`` (the read_through
    #: cache key, byte-identical to the twin's per-frame params), its display
    #: ``name``, the ``valid_from``/``valid_to`` window the map stamps as the
    #: frame's temporal range, the ``layer_id``, and the AOI ``bbox``. Raises the source's typed
    #: EMPTY error when the window matches no frames (honesty floor). No prior spec
    #: declares it (strict no-op).
    frames_plan: str | None = None

    #: FRAMES-LIST per-frame COG BUILDER (animation wave 1). ``(spec,
    #: params, frame: FramePlan) -> bytes``. The ``shape: animation_frames``
    #: executor's per-frame ``read_through`` fetch_fn: builds ONE frame's raster
    #: bytes (the CIRA SLIDER tile-stitch mosaic -> EPSG:4326 COG, or a post-stitch
    #: blend). It may perform the frame's own tile I/O (the ``_satellite_slider``
    #: substrate owns the stitch socket, the sanctioned per-frame impurity, like the
    #: library_delegate). Raises :class:`FrameDegraded` to signal a graceful per-frame
    #: skip (a transparent / off-swath / upstream-failed frame the executor records
    #: and drops, never a silent gap); the executor's honesty floor raises the typed
    #: EMPTY error only when EVERY frame degrades. Pairs with ``frames_plan``. No
    #: prior spec declares it (strict no-op).
    frame_bytes: str | None = None

    #: TRANSPORT-STATUS classification (keyed/misc wave).
    #: ``(spec, status: int | None, body: str | None) -> RouterError | None``. The
    #: http_json transport collapses every non-2xx to a retryable UPSTREAM error;
    #: a keyed source that must split the HTTP status into the twin's distinct typed
    #: errors (401/403 -> a credential-shaped ``*_AUTH_ERROR``, 404 -> a
    #: non-retryable ``*_INPUT_ERROR`` for a bad path selector, else the default
    #: retryable upstream) names this PURE hook. ``_get`` consults it on a transport
    #: failure BEFORE its default upstream fallback; returning ``None`` keeps the
    #: default. No I/O (the status/body are already fetched). No prior spec declares
    #: it (strict no-op: the default upstream mapping is unchanged).
    classify_status: str | None = None


class DispatchSpec(GraceModel):
    """A spec-declared, SINGLE-TARGET pre-flight cross-sibling dispatch.

    The FIRST tool-composes-tool seam: for ONE declared param value the router
    SHORT-CIRCUITS ``route()`` and serves the request from a NAMED sibling
    registered tool, returning THAT tool's result VERBATIM (its own
    ``source_class`` cache prefix, its own ``layer_id`` / ``name`` -- NO re-cache
    under this spec, NO double-fetch). The motivating case is
    ``fetch_dem(source="copernicus")`` returning ``fetch_copernicus_dem``'s layer
    byte-for-byte (the twin's ``TOOL_REGISTRY["fetch_copernicus_dem"].fn(bbox=...)``).

    The seam is DELIBERATELY NARROW (the atomic-tools doctrine avoids
    tool-composes-tool; this is the one sanctioned exception, gated to NATE):

    - ONE target per condition (``to`` is a single string, not a list).
    - SPEC-DECLARED only: ``to`` / ``equals_any`` are literals -- never a
      hook-computed target.
    - NO CHAINS: a dispatched target must not itself declare a ``dispatch`` block
      (the router refuses a chain at dispatch time, so the returned result is
      always exactly one sibling's verbatim output).
    - PRE-FLIGHT ONLY: evaluated on the RAW request params BEFORE validation /
      gates / cache / fetch (byte-identical to the twin, which dispatched first
      thing on the raw ``source`` arg with zero prior validation).
    """

    #: The request param whose value triggers the dispatch (``source``).
    param: str = Field(min_length=1)
    #: The param values (post-normalize) that MATCH this condition. The twin's
    #: copernicus alias set: ``[copernicus, cop-dem-glo-30, glo-30, glo30,
    #: copernicus_glo30]``.
    equals_any: list[str] = Field(min_length=1)
    #: How to normalize the raw param value before the ``equals_any`` membership
    #: check. ``lower_strip`` reproduces the twin's ``source.strip().lower()``;
    #: ``none`` compares verbatim.
    normalize: Literal["lower_strip", "none"] = "lower_strip"
    #: The SINGLE sibling registered-tool name to dispatch to (``fetch_copernicus_dem``).
    to: str = Field(min_length=1)
    #: ``target_arg -> this-spec raw param name`` map for the dispatched call. The
    #: twin passes only ``bbox=bbox`` -> ``{bbox: bbox}``. The RAW (unvalidated)
    #: value is forwarded; the target validates it under its own contract.
    pass_args: dict[str, str] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Top-level SourceSpec
# --------------------------------------------------------------------------- #


class SourceSpec(GraceModel):
    """A single data source's router specification (contract sec 1.1).

    ``name`` MUST equal the hand-written twin's registry key so the fold arm can
    substitute one for the other transparently (indistinguishability). The
    router loader (``_router.spec._compose_specs_from_tree``) walks
    ``fetchers/**/source.yaml`` and validates each file into this model.
    """

    schema_version: Literal["v1"] = "v1"

    # --- identity ---
    name: str = Field(min_length=1)          # registry key == twin's name
    source_class: str = Field(min_length=1)  # cache <source-class> prefix
    shape: SourceShape
    supports_global_query: bool = False

    #: Register this spec at ``tier="internal"`` (tool_registry.EngineTier): the
    #: promoted tool stays registry-resolvable for in-process callers
    #: (``TOOL_REGISTRY[name].fn``) but is EXCLUDED from both the default declarable
    #: pool and the retrieval search index -- no model-facing surface. Set for an
    #: absorbed seam a public tool resolves internally (fetch_copernicus_dem, folded
    #: into fetch_dem's source="copernicus" mode). Default False = the ordinary
    #: tier="general" (or arm-flagged tier="catalog") registration.
    internal_only: bool = False

    #: Explicit error-code prefix token (contract sec 0 / VERDICT finding #1).
    #: ``source_class`` doubles as the cache prefix and MUST equal the twin's for
    #: cache indistinguishability, but three twins stamp their A.6 ``error_code``
    #: from a DIFFERENT token than their cache source_class (COOPS_TIDES vs
    #: noaa_coops_tides, ESRI_LANDCOVER vs esri_landcover_10m, HIFLD_INFRA vs
    #: hifld_critical_infrastructure). One field cannot carry both, so a spec sets
    #: ``error_prefix`` to the twin's exact token; the router stamps error codes
    #: from :meth:`error_code_prefix`. Default (unset) = ``source_class.upper()``.
    error_prefix: str | None = None

    #: A.6 input-error suffix (VERDICT round-2: twins split INPUT_ERROR vs
    #: INPUT_INVALID). ``error_prefix`` fixes only the prefix; hifld/census stamp
    #: ``*_INPUT_INVALID`` (a per-param ``error_suffix`` overrides this, e.g. esri
    #: bbox -> BBOX_INVALID / year -> YEAR_INVALID). Default = the byte-identical
    #: ``INPUT_ERROR`` (gridmet/coops).
    input_error_suffix: str = "INPUT_ERROR"

    #: A.6 empty/no-coverage suffix. Default ``EMPTY`` (GRIDMET_EMPTY /
    #: COOPS_TIDES_EMPTY); esri stamps ``NO_COVERAGE`` (ESRI_LANDCOVER_NO_COVERAGE)
    #: when no item covers the extent / the mosaic is entirely no-data.
    empty_error_suffix: str = "EMPTY"

    #: The LLM-facing tool docstring, carried verbatim from the hand-written twin
    #: (dedented via ``inspect.getdoc``). This is the sole source of the promoted
    #: tool's ``FunctionDeclaration`` description AND its retrieval-index document
    #: text -- so a spec-driven source is INDISTINGUISHABLE from its twin to both
    #: the LLM tool-selector and the BM25/dense retriever (the fold's first cut
    #: registers the spec UNDER the twin's name; the docstring must not shift the
    #: index). ``None`` = synthesize a spec-derived doc from caveats + corpus.
    docstring: str | None = None

    # --- endpoints + auth ---
    endpoints: dict[str, EndpointSpec] = Field(min_length=1)
    auth: AuthSpec = Field(default_factory=AuthSpec)

    # --- request-param schema (validate BEFORE any network call) ---
    params: dict[str, ParamSpec] = Field(default_factory=dict)
    gates: GateSpec = Field(default_factory=GateSpec)

    # --- ingestion (shape-specific; flexible dict keyed by shape, sec 1.2) ---
    ingest: dict[str, Any] = Field(default_factory=dict)

    # --- tier-3 hooks: named pure fns for the ONE irreducible step
    hooks: HookSpec | None = None

    # --- cross-sibling pre-flight dispatch: one param value -> serve
    # --- a named sibling tool's result verbatim (fetch_dem source="copernicus"). --
    dispatch: list[DispatchSpec] = Field(default_factory=list)

    # --- named transform: two-source JOIN-on-key (census, sec 2.5) ---
    join: dict[str, Any] | None = None

    # --- normalization + output ---
    normalize: NormalizeSpec = Field(default_factory=NormalizeSpec)
    output: OutputSpec

    # --- cache + payload gate ---
    cache: CacheSpec
    payload_estimate: PayloadEstimateSpec

    # --- honesty: caveats + the same-data endpoint chain ---
    caveats: list[str] = Field(default_factory=list)
    #: SAME-DATA ENDPOINT MIRRORS ONLY, in order. Every entry names a KEY in this
    #: spec's own ``endpoints`` block -- an alternate service publishing the SAME
    #: dataset (USGS NHDPlus HR -> the medium-resolution NHD service), which the
    #: loudness floor lets walk silently. Registration REFUSES an entry that names
    #: no such key, because such an entry cannot execute: ``resolve_endpoints``
    #: indexes ``endpoints``, never the tool registry, so a SIBLING TOOL name here
    #: is a promise printed onto the spec card that no code path can keep.
    #: A CROSS-DATASET alternative is not this mechanism -- it is a declared rung
    #: on a fallback ladder (``trid3nt_server.fallbacks``), gated and stamped.
    endpoint_fallback: list[str] = Field(default_factory=list)

    # --- declared DATA-native resolutions (two-layer truth)
    # A source's native cell / tier facts live HERE (with the fetcher), so the
    # payload/input-review gate card can QUOTE them ("data native 3DEP 10 m") next to
    # a solver's declared range. Synthesized onto the tool's
    # ``AtomicToolMetadata.resolution_specs`` (constraint_source='data'). Default () =
    # a source with no granularity-bearing param (the common case).
    resolution_declarations: tuple[ResolutionSpec, ...] = Field(default=())

    # --- declared confirm gate (the gate-collapse)
    # A HEAVY raster fetcher (fetch_dem/topobathy/landcover) DECLARES its
    # resolution confirm gate here; the router synthesizes the canonical fetch
    # GateSpec onto the tool's ``AtomicToolMetadata.gate_spec`` (kind='fetch',
    # the shared estimate/pin providers). ``None`` (default) = an un-gated fetch.
    # A named template rather than an inline GateSpec keeps the three fetch
    # source.yaml decls terse and the provider dotted paths single-sourced.
    confirm_gate: Literal["fetch_resolution"] | None = None

    # --- retrieval phrasings (verbatim from the twin's corpus.yaml) ---
    corpus: list[str] = Field(default_factory=list)

    @property
    def error_code_prefix(self) -> str:
        """The token the router stamps ``error_code`` from (VERDICT finding #1).

        ``error_prefix`` when the spec pins the twin's exact token, else
        ``source_class.upper()`` (the byte-identical default for the sources whose
        error prefix already equals their cache source_class, e.g. gridmet/census).
        """
        return self.error_prefix or self.source_class.upper()

    @model_validator(mode="after")
    def _validate_shape_consistency(self) -> "SourceSpec":
        """Cross-field consistency the router relies on at dispatch time."""
        _validate_style_row(self.name, self.output.style)
        # raster shapes emit tif; vector/station shapes emit fgb (or json).
        if self.shape == "raster-cog" and self.output.layer_type != "raster":
            raise ValueError(
                f"shape=raster-cog requires output.layer_type=raster; "
                f"got {self.output.layer_type!r}"
            )
        if self.shape in ("vector-fgb", "station-timeseries-fgb") and (
            self.output.layer_type != "vector"
        ):
            raise ValueError(
                f"shape={self.shape} requires output.layer_type=vector; "
                f"got {self.output.layer_type!r}"
            )
        # record shape <-> record layer_type + json ext + a record hook.
        if self.shape == "record" and self.output.layer_type != "record":
            raise ValueError(
                f"shape=record requires output.layer_type=record; "
                f"got {self.output.layer_type!r}"
            )
        if self.output.layer_type == "record" and self.shape != "record":
            raise ValueError(
                f"output.layer_type=record requires shape=record; got {self.shape!r}"
            )
        # animation_frames shape <-> raster frames + the two frames hooks.
        # It returns list[LayerURI] (ordered per-timestamp), so it emits raster tif
        # frames and MUST declare both the pre-loop frames_plan and the per-frame
        # frame_bytes builder (the router has nothing else to resolve the frame set /
        # build a frame).
        if self.shape == "animation_frames":
            if self.output.layer_type != "raster":
                raise ValueError(
                    f"shape=animation_frames requires output.layer_type=raster; "
                    f"got {self.output.layer_type!r}"
                )
            fp = self.hooks.frames_plan if self.hooks is not None else None
            fb = self.hooks.frame_bytes if self.hooks is not None else None
            if not (fp and fb):
                raise ValueError(
                    "shape=animation_frames requires hooks.frames_plan + hooks.frame_bytes"
                )
        # A join transform only makes sense over a vector base shape.
        if self.join is not None and self.shape != "vector-fgb":
            raise ValueError(
                f"join transform requires shape=vector-fgb; got {self.shape!r}"
            )
        return self
