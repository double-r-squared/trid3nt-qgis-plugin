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
from .tool_registry import TTLClass

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
    "SourceSpec",
]


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
#: gate to the delegating executor (phase-2 wave-3, ADR 0040).
#: ``float_list`` = a scalar float OR a ``list[float]`` (slr_scenarios
#: scenario_ft) validated against ``values`` (the allowed level set),
#: sorted + deduped -- the fan-out mode's per-value driver (phase-2 wave-6,
#: ADR 0052). A scalar is coerced to a 1-element list.
#: ``str_list`` = a ``list[str]`` free-text filter set (nws_event event_types);
#: each entry stripped, empties dropped, sorted + deduped for cache-key
#: stability -- the string sibling of ``float_list`` with no allowed-set gate
#: (tier-3 hook wave). A scalar string is coerced to a 1-element list.
#: ``bool`` = a truthy flag param (nws_river_forecast include_thresholds /
#: include_series). Coerced with ``bool(value)`` (the twin's ``bool(flag)``
#: contract); the promoted signature annotates it ``bool`` (chained-resolution
#: mode, ADR 0063). No prior spec declares it (strict no-op).
ParamType = Literal[
    "bbox", "iso_date", "enum", "int", "float", "str", "int_range", "date_compact",
    "point", "float_list", "str_list", "bool",
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
    #: (phase-2 wave-3, ADR 0040). The adapter marks a None-default NON-Optional
    #: annotation as required-in-schema (the wave-2 quirk). A twin that annotated
    #: the param ``T | None = None`` is NOT required; setting ``schema_optional:
    #: true`` reproduces that (wqp bbox, nldi seed_point/comid). Default False
    #: preserves the wave-2 required behavior for every prior spec.
    schema_optional: bool = False
    #: str-param alias table (phase-2 wave-3, ADR 0040). When set, a str value is
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
    max_bbox_deg2: float | None = None       # hard ceiling (esri_landcover: 8.0)
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
    #: the static ``units`` (phase-2 wave-3, ADR 0040). The wqp twin stamps
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

    layer_type: Literal["raster", "vector"]
    ext: Literal["tif", "fgb", "json"]
    role: Literal["primary", "context", "input"] = "primary"
    style_preset: str                        # may template on a param
    #: Per-param MAPPED style preset (phase-2 wave-7). ``{"param": "layer", "map":
    #: {"fbfm40": "categorical_landcover", "cbh": "continuous_dem", ...}}`` selects
    #: the preset by a param value; a value absent from the map falls back to the
    #: static ``style_preset``. Default (None) = the static preset for every prior
    #: spec (strict no-op).
    style_preset_by_param: dict[str, Any] | None = None
    #: Whether the emitted ``LayerURI`` carries the request bbox. Default True
    #: (census/coops/hifld/esri set it); gridmet's twin omits it, so its spec
    #: sets ``emit_bbox: false`` to stay byte-identical (VERDICT round-2 tell).
    emit_bbox: bool = True
    #: Stamp ``LayerURI.bbox`` from the EXTENT of the emitted vector features
    #: rather than the request bbox (tier-3 hook wave, ADR 0056). A dict
    #: ``{pad: <deg>}`` -- the point-event fetchers (earthquakes / tsunami /
    #: volcano) auto-zoom the camera to the events' bounds, padding a degenerate
    #: single-point axis by ``pad`` degrees. The router reads the extent back from
    #: the produced FGB (available on both cache hit + miss), so the stamp is
    #: consistent regardless of the cache path. Default (None) = no override
    #: (strict no-op for every prior spec; ``emit_bbox`` governs the bbox).
    bbox_from_features: dict[str, Any] | None = None
    #: Keep attribute-only (NULL-geometry) features in the emitted FGB instead of
    #: dropping them (chained-resolution mode, ADR 0063). The nws_alerts_conus twin
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
    """Named extension points for the ONE irreducible per-source step (ADR 0056).

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

    # --- chained-resolution mode (ADR 0063): resolve-then-fetch / bounded ---
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

    # --- tier-3 hooks: named pure fns for the ONE irreducible step (ADR 0056) ---
    hooks: HookSpec | None = None

    # --- named transform: two-source JOIN-on-key (census, sec 2.5) ---
    join: dict[str, Any] | None = None

    # --- normalization + output ---
    normalize: NormalizeSpec = Field(default_factory=NormalizeSpec)
    output: OutputSpec

    # --- cache + payload gate ---
    cache: CacheSpec
    payload_estimate: PayloadEstimateSpec

    # --- honesty: caveats + fallback chain ---
    caveats: list[str] = Field(default_factory=list)
    fallback: list[str] = Field(default_factory=list)

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
        # A join transform only makes sense over a vector base shape.
        if self.join is not None and self.shape != "vector-fgb":
            raise ValueError(
                f"join transform requires shape=vector-fgb; got {self.shape!r}"
            )
        return self
