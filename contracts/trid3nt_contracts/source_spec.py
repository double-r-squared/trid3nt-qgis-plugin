"""``SourceSpec`` - the generic data-router source specification. Data, not code.

One ``source.yaml`` per data source, beside that source's own folder. It is the
shape the router loader AND the parity harness both validate against, so the
two cannot drift. INDISTINGUISHABILITY is the bar: a spec-driven source flows
through the identical pipeline and surfaces identically to a coded one.
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



#: The ingestion shape that selects the base executor. A HYBRID spec declares a
#: base shape PLUS a transform block that wraps that executor - a raster source
#: with a mosaic, a vector source with a fan-out.
SourceShape = Literal[
    "raster-cog",
    "vector-fgb",
    "station-timeseries-fgb",
    "record",
    "animation_frames",
]

#: Auth mode. ``none`` = keyless public; ``api_key_env`` = a key from an env var,
#: required or optional; ``cds`` / ``vault`` / ``token`` are reserved.
AuthMode = Literal["none", "api_key_env", "cds", "vault", "token"]

#: Request-param declared types. The compound ones, whose Python shape the name
#: alone does not carry:
#:
#: - ``int_range`` = a 2-element ``[start, end]`` int list.
#: - ``date_compact`` = ``YYYY-MM-DD`` or ``YYYYMMDD``, normalized to ``YYYYMMDD``.
#: - ``point`` = a 2-element ``[lon, lat]`` float list, coerced and finite-checked
#:   here; any CONUS or mutual-exclusion gate belongs to the executor.
#: - ``float_list`` = a scalar float OR a ``list[float]``, checked against
#:   ``values``, sorted and deduped; a scalar becomes a 1-element list.
#: - ``str_list`` = a ``list[str]`` filter set with no allowed-set gate; each
#:   entry stripped, empties dropped, sorted and deduped for cache-key stability.
#: - ``bool`` = a flag, coerced with ``bool(value)``.
#: - ``datetime_range`` = a 2-element ISO ``[start, end]`` list; each entry parses
#:   as a date OR a datetime, coerced to a tuple with ``start <= end`` and echoed
#:   as isoformat strings for cache-key stability. It carries the sub-day window
#:   no ``iso_date`` pair can express.
ParamType = Literal[
    "bbox", "iso_date", "enum", "int", "float", "str", "int_range", "date_compact",
    "point", "float_list", "str_list", "bool", "datetime_range",
]

#: Payload-estimate models.
PayloadModel = Literal["bbox_area", "per_station", "per_feature", "tiled"]




class EndpointSpec(GraceModel):
    """One named endpoint. Exactly one of ``url`` / ``url_template`` is the base;
    ``query`` carries the static params merged onto every request to it."""

    url: str | None = None
    url_template: str | None = None
    query: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _one_url(self) -> "EndpointSpec":
        if not (self.url or self.url_template):
            raise ValueError("endpoint requires one of url / url_template")
        return self


class AuthSpec(GraceModel):
    """Auth mode and the shared User-Agent."""

    mode: AuthMode = "none"
    #: For ``mode="api_key_env"``: which env var, and whether it is required.
    #: ``required: false`` is how a keyless fallback is expressed.
    api_key_env: dict[str, Any] = Field(default_factory=dict)
    user_agent: str = "trid3nt_default"


class ParamSpec(GraceModel):
    """One request-param's validation contract, applied BEFORE any network call.
    ``quantize`` is a bbox directive: ``round_6dp``, or ``res_<m>`` to snap to a
    raster resolution."""

    type: ParamType
    required: bool = False
    default: Any = None
    values: list[Any] | None = None          # enum only
    quantize: str | None = None              # bbox only: round_6dp | res_<m>
    max_range_days: int | None = None        # iso_date pair ceiling (end param)
    #: int/float inclusive range gate. Out of range raises a typed input error
    #: stamped with this param's ``error_suffix``.
    min: float | None = None
    max: float | None = None
    #: iso_date COVERAGE bounds. A violation is a typed ``*_NOT_AVAILABLE``,
    #: which is a different error from the ISO-format and range-days ones.
    min_date: str | None = None              # static ISO lower coverage bound
    max_future_days: int | None = None       # end <= today + N (future ceiling)
    #: Per-param input-error suffix override, for a source that stamps a
    #: different suffix per param. Default (None) = the spec-level suffix.
    error_suffix: str | None = None
    #: Force this None-default param OPTIONAL in the promoted input schema. A
    #: None default alone is marked required-in-schema, which is right for a
    #: param annotated ``T`` and wrong for one annotated ``T | None``; this
    #: expresses the second case. Default False = required-in-schema.
    schema_optional: bool = False
    #: str-param alias table. A value is lower-cased and stripped, then mapped
    #: through this; an UNMAPPED value passes through verbatim rather than
    #: raising. Default (None) = no aliasing.
    aliases: dict[str, str] | None = None
    #: enum only: lower-case and strip BEFORE the allowed-set check, so a
    #: case-insensitive vocabulary echoes the normalized key. Default False is
    #: strict match.
    lowercase: bool = False


class GateSpec(GraceModel):
    """Pre-fetch gates."""

    conus_only: bool = False                 # bbox must intersect CONUS
    #: Per-spec CONUS envelope override ``(west, south, east, north)``. A source
    #: whose served grid reaches past the shared generic envelope states its own
    #: REAL bounds here: borrowing an unrelated source's footprint false-refuses
    #: coverage this source actually has. Absent (None) = the shared envelope.
    conus_bbox: tuple[float, float, float, float] | None = None
    max_bbox_deg2: float | None = None       # hard ceiling, raw degree^2
    #: Hard ceiling on the bbox area in APPROXIMATE km^2, cos-lat scaled.
    #: Distinct from ``max_bbox_deg2``: a degree^2 ceiling is not the same
    #: guardrail at varying latitude. Default (None) = no km^2 ceiling.
    max_bbox_km2: float | None = None
    max_stations: int | None = None          # station-timeseries only
    max_features: int | None = None          # vector only (paging cap)


class NormalizeSpec(GraceModel):
    """Normalization stamps - what makes a spec-driven layer indistinguishable."""

    crs: str = "EPSG:4326"
    units: str | None = None
    datum: str | None = None
    quantity: str | None = None
    orientation: str | None = None           # raster only
    #: Name a request param whose RESOLVED VALUE becomes the emitted units,
    #: instead of the static ``units``. Default (None) = the static stamp.
    units_from_param: str | None = None
    #: Per-param MAPPED units: ``{"param": <name>, "map": {<value>: <units>}}``.
    #: Unlike ``units_from_param``, which stamps the raw value, this maps through
    #: a table, so a value ABSENT from the map stamps NO units at all - which is
    #: what a categorical variable beside a scaled one needs. Overrides the
    #: static ``units``. Default (None) = no per-param mapping.
    units_by_param: dict[str, Any] | None = None


class OutputSpec(GraceModel):
    """The output surface."""

    #: ``record`` means the source returns a bare JSON dict, NOT a renderable
    #: layer - a structured lookup or a summary rather than something to draw.
    #: It pairs with ``shape: record`` and ``ext: json``, and the honesty floor
    #: holds: the record hook raises typed errors, it never fabricates a dict.
    layer_type: Literal["raster", "vector", "record"]
    ext: Literal["tif", "fgb", "json"]
    role: Literal["primary", "context", "input"] = "primary"
    #: HOW THIS DATASET DRAWS ITSELF. A dataset's default rendering is a fact
    #: about the DATA, so it is declared beside the source rather than mapped to
    #: it elsewhere. ``{kind: continuous | classed | reference | mesh}`` picks
    #: one of the four preset shapes and the remaining keys parameterise it
    #: (``ramp`` / ``units`` / ``label`` / ``scale`` / ``classes`` /
    #: ``geometry`` / ``color``). ``by_param`` -
    #: ``{param: <name>, map: {<value>: <partial row>}}`` - overrides those per
    #: param value, which is how one source serving several variables gives each
    #: its own ramp and range. Absent (None) = the kind's bare default.
    style: dict[str, Any] | None = None
    #: Per-param MAPPED role: ``{"param": <name>, "map": {<value>: <role>}}``.
    #: One source can serve both an analytical product and a context basemap; a
    #: value absent from the map falls back to the static ``role``. Default
    #: (None) = the static role.
    role_by_param: dict[str, Any] | None = None
    #: Whether the emitted layer carries the request bbox. Default True.
    emit_bbox: bool = True
    #: The HUMAN-facing layer name, verbatim. The default stamp is a machine
    #: identifier and reads as one in a layer tree. Set it wherever the
    #: product's identity is not obvious from the tool name - a MODELLED product
    #: must say so here, because this is what a person reads off the map.
    #: Default (None) = the machine stamp.
    display_name: str | None = None
    #: Stamp the emitted bbox from the EXTENT OF THE FEATURES rather than the
    #: request bbox: ``{pad: <deg>}``, where ``pad`` widens a degenerate
    #: single-point axis. The extent is read back from the produced file, which
    #: exists on cache hit and miss alike, so the stamp does not depend on the
    #: cache path. Default (None) = ``emit_bbox`` governs the bbox.
    bbox_from_features: dict[str, Any] | None = None
    #: Name a ``LayerURI`` SUBCLASS result model - a key into
    #: ``execution.LAYER_RESULT_MODELS``. The subclass is built from the base
    #: layer plus the ``hooks.envelope`` field dict, so a source returning extra
    #: business fields needs no coded fetcher. Declared TOGETHER with
    #: ``hooks.envelope``; registration validates both. Default (None) = the
    #: plain layer.
    result_model: str | None = None
    #: EMPTINESS-DRIVEN output switch: a hook name ``<source>.<point>``, called
    #: with ``(spec, params)`` when the produced vector file is FEATURE-EMPTY.
    #: Its dict is returned INSTEAD of the layer, so an AOI with nothing in it
    #: is not handed a layer that draws nothing - the honesty gate. A non-empty
    #: fetch is unaffected. Default (None) = the layer is always returned.
    variant_by_emptiness: str | None = None
    #: FETCH-TIME PROVENANCE CHANNEL. True binds a recorder around the fetch so
    #: the executor can record a small typed dict during a NON-cached fetch; the
    #: cache persists it as a sidecar and REPLAYS it on a hit. This is how a
    #: fact that only exists at fetch time - which leg of a composite painted
    #: which part - survives every cache path, since it is unrecoverable from the
    #: final bytes. Requires ``hooks.envelope``, its consumer. Default False.
    provenance: bool = False
    #: Keep attribute-only (NULL-geometry) features instead of dropping them, so
    #: a record whose geometry could not be resolved survives as a row with no
    #: map footprint. Such a file is written with no spatial index, which cannot
    #: be built over NULL geometry. Default False drops them.
    keep_null_geometry: bool = False


class CacheSpec(GraceModel):
    """The cache TTL class."""

    ttl_class: TTLClass


class PayloadEstimateSpec(GraceModel):
    """Payload-MB estimator inputs; the estimator itself is synthesized from
    them. Every coefficient is optional - each ``model`` reads the ones it
    needs and ignores the rest."""

    model: PayloadModel
    floor_mb: float = 0.01
    #: Optional upper clip on the estimate. Default (None) = no ceiling.
    ceil_mb: float | None = None
    # bbox_area / tiled
    mb_per_sq_deg: float | None = None
    #: Per-param MB/deg^2 table for the ``bbox_area`` model:
    #: ``{"param": <name>, "map": {<value>: <coefficient>}, "default": <float>}``.
    #: The same bbox area costs wildly different bytes at different resolutions,
    #: which one scalar cannot hold. Overrides ``mb_per_sq_deg`` for the resolved
    #: value; a value absent from the map falls to ``default``, then to
    #: ``mb_per_sq_deg``, then to 0.01. Default (None) = the scalar.
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
    Each names a REGISTERED PURE FUNCTION, ``<source_key>.<point>``, resolved at
    load. A hook only COMPUTES - transport, caching and gates stay router-owned.
    """

    # A field is added here only when a real source cannot be expressed without
    # it; a speculative point is not added.


    #: ``(spec, params) -> list[RequestPlan]``. Builds the source-specific
    #: request(s) - URL, query, headers, and any pre-fetch validation the
    #: declarative param gates cannot express. Returns 1..N plans: one for a
    #: single GET, several for a static multi-endpoint set the parse hook joins.
    #: For a paged source it is called once per page, with the page injected.
    build_request: str | None = None

    #: ``(spec, params, bodies: list[bytes]) -> list[GeoJSON-feature dict]``.
    #: Decodes the source's payload(s) into features the shared serializer
    #: writes. Raises the typed EMPTY / RESULT_TOO_LARGE / UPSTREAM errors on the
    #: honest-empty, over-cap and bad-body paths rather than returning nothing.
    parse_response: str | None = None

    # Resolve-then-fetch and bounded per-item enrichment: two composable phases,
    # each declared only by a source that needs it. The router owns the
    # orchestration, the transport and the bounded, deduped, best-effort detail
    # loop; these hooks are the PURE compute at each edge.

    #: PHASE R (resolve), PRE-cache-key. ``(spec, params) -> list[RequestPlan]``.
    #: Builds the round-1 request that turns a NAME into an id. Returns ``[]`` to
    #: say the params already select without a round trip. It runs BEFORE the
    #: cache read, so the resolved id enters the cache key and a name query and
    #: its id query collapse to ONE entry.
    resolve_build: str | None = None

    #: PHASE R. ``(spec, params, bodies: list[bytes]) -> dict[str, Any]``.
    #: Decodes the resolution body into a params-MERGE dict folded in before the
    #: main fetch. Raises the typed INPUT error on an unknown or AMBIGUOUS name -
    #: an ambiguous match is refused, never silently resolved to one candidate.
    resolve_parse: str | None = None

    #: MAIN-FETCH offset paging.
    #: ``(spec, params, bodies: list[bytes]) -> RequestPlan | None``. Given every
    #: page so far, returns the NEXT page's request, or ``None`` to STOP - the
    #: offset, short-page and record-cap loop control a declarative page-count
    #: pager cannot express. ``build_request`` still builds page 1, and the
    #: router owns the loop and a hard page ceiling.
    next_page: str | None = None

    #: PHASE E (enrich).
    #: ``(spec, params, features: list[dict]) -> list[tuple[str, RequestPlan]]``.
    #: From the round-1 features, the ORDERED per-item detail requests. The hook
    #: applies its own per-pass cap by emitting only that many; the router dedupes
    #: by ref key, bounds the total, and fetches each BEST-EFFORT - a failed ref
    #: is recorded, never silently dropped.
    enrich_plan: str | None = None

    #: PHASE E. ``(spec, params, features, results: dict[str, DetailResult])
    #: -> list[dict]``. Folds the fetched detail - each result carrying a body OR
    #: a typed error - back into the features. EVERY input feature survives: one
    #: whose refs failed keeps its row with null detail.
    enrich_merge: str | None = None

    #: POST-EMIT ENVELOPE.
    #: ``(spec, params, layer: LayerURI, data: bytes) -> dict[str, Any]``. The
    #: LAST hook called: it gets the assembled base layer plus the produced bytes
    #: and returns the extra business fields for ``output.result_model``, plus
    #: any base-field overrides. PURE - it computes over already-fetched bytes and
    #: performs no transport. The honesty-floor keys (``uri``, ``layer_type``) are
    #: dropped from its return, so a hook can ADD fields but can never flip an
    #: error to a success or re-point the layer. Pairs with
    #: ``output.result_model``, declared together.
    envelope: str | None = None

    #: LIBRARY-DELEGATE call. ``(spec, params, *, timeout_s: float)
    #: -> features | (array, transform, crs)``. The ONE sanctioned impurity: a
    #: source whose maintained LIBRARY owns both discovery and the socket calls
    #: that library here, while params, gates, stamps, cache, publish and typed
    #: errors stay router-owned. The declared timeout is passed in, the call is
    #: marked library-owned in telemetry, and any library exception the hook did
    #: not itself map becomes a retryable upstream error. A vector spec returns
    #: features; a raster spec returns ``(array, transform, crs)``.
    delegate: str | None = None

    #: LIBRARY-DELEGATE pre-cache input validation. ``(spec, params) -> None``.
    #: Runs AFTER type and gate validation and BEFORE the cache read, so a
    #: source-specific input gate the declarative surface cannot express refuses
    #: pre-cache and pre-network - which also makes it testable offline.
    delegate_validate: str | None = None

    #: RECORD-RETURN dict builder.
    #: ``(spec, params, bodies: list[bytes]) -> dict | None``. For a ``record``
    #: source, this PURE hook shapes fetched bodies into the result dict; the
    #: router owns the transport and the cache. ``None`` for a plan's body means
    #: "no usable record here, try the next plan": the plans are walked in order
    #: and the first non-None dict wins. If EVERY plan yields None the router
    #: raises the typed empty error - the hook never fabricates a success dict.
    record: str | None = None

    #: SOCKETED PRE-CACHE-KEY resolve.
    #: ``(spec, params, *, timeout_s: float) -> dict``. The library-socket
    #: sibling of ``resolve_build`` / ``resolve_parse``, which resolve over HTTP.
    #: Runs after ``delegate_validate`` and BEFORE the cache read, under the same
    #: delegate constraints, and its dict MERGES into ``params`` so the resolved
    #: value enters the cache key - without it, a request that asks for "latest"
    #: computes a non-deterministic key. Pairs with ``hooks.delegate``.
    delegate_resolve: str | None = None

    #: GENERIC PRE-CACHE-KEY resolve. ``(spec, params) -> dict``. The MULTI-STEP
    #: HTTP case that neither the single-round ``resolve_build`` / ``resolve_parse``
    #: pair nor the socket ``delegate_resolve`` expresses. Runs after type and
    #: gate validation and BEFORE the cache read; its dict MERGES into ``params``
    #: so the resolved value enters the cache key - otherwise a "latest available"
    #: request keys non-deterministically and serves the first cached day forever.
    pre_resolve: str | None = None

    #: PER-BAND COLORMAP.
    #: ``(spec, params) -> dict[int, tuple[int, int, int, int]]``. For a raster
    #: source whose palette is a PURE function of a request param. The returned
    #: value -> RGBA table is baked into the emitted file's band-1 palette. PURE:
    #: it computes over the params, never reads the fetched array, does no I/O.
    colormap: str | None = None

    #: FRAMES-LIST pre-loop RESOLVE. ``(spec, params) -> list[FramePlan]``. An
    #: ``animation_frames`` source returns an ORDERED list of layers rather than
    #: one; this hook does the timestamp-index fetch, window, subsample and
    #: filter, and returns the per-frame plans - each carrying its cache params,
    #: display name, valid-from/valid-to window, layer id and bbox. Raises the
    #: typed EMPTY error when the window matches no frames.
    frames_plan: str | None = None

    #: FRAMES-LIST per-frame BUILDER.
    #: ``(spec, params, frame: FramePlan) -> bytes``. Builds ONE frame's raster
    #: bytes, and MAY perform that frame's own tile I/O - the second sanctioned
    #: impurity. Raises ``FrameDegraded`` for a graceful per-frame skip, which the
    #: executor RECORDS rather than leaving a silent gap; the typed EMPTY error is
    #: raised only when EVERY frame degrades. Pairs with ``frames_plan``.
    frame_bytes: str | None = None

    #: TRANSPORT-STATUS classification.
    #: ``(spec, status: int | None, body: str | None) -> RouterError | None``.
    #: The shared transport collapses every non-2xx to a retryable UPSTREAM error.
    #: A source that must split the status into distinct typed errors - 401/403 to
    #: a credential-shaped one, 404 to a non-retryable input one - names this PURE
    #: hook, consulted on a transport failure BEFORE the default. ``None`` keeps
    #: the default. No I/O: the status and body are already in hand.
    classify_status: str | None = None


class DispatchSpec(GraceModel):
    """A spec-declared, SINGLE-TARGET pre-flight dispatch to a sibling tool.
    One declared param value serves the request from a NAMED sibling, returning
    that tool's result VERBATIM - its cache prefix, its ids, no double fetch."""

    # The seam is DELIBERATELY NARROW - one sanctioned exception to the rule that
    # a tool does not compose another:
    #   - ONE target per condition: ``to`` is a single string, never a list.
    #   - SPEC-DECLARED only: ``to`` and ``equals_any`` are literals, never
    #     hook-computed.
    #   - NO CHAINS: a dispatched target must not itself declare a dispatch, so
    #     the returned result is always exactly one sibling's verbatim output.
    #   - PRE-FLIGHT: evaluated on the RAW params before validation, gates,
    #     cache or fetch.

    #: The request param whose value triggers the dispatch (``source``).
    param: str = Field(min_length=1)
    #: The normalized param values that MATCH this condition - the alias set.
    equals_any: list[str] = Field(min_length=1)
    #: How to normalize the raw param value before the ``equals_any`` membership
    #: check. ``lower_strip`` strips and lower-cases; ``none`` compares
    #: verbatim.
    normalize: Literal["lower_strip", "none"] = "lower_strip"
    #: The SINGLE sibling registered-tool name to dispatch to.
    to: str = Field(min_length=1)
    #: ``target_arg -> this spec's raw param name`` for the dispatched call. The
    #: RAW value is forwarded; the target validates it under its own contract.
    pass_args: dict[str, str] = Field(default_factory=dict)




class SourceSpec(GraceModel):
    """A single data source's router specification; ``name`` IS its registry key.
    ``ingest`` and ``join`` stay flexible dicts because they vary per shape;
    every other top-level key is strictly typed and an unknown one is a defect."""

    schema_version: Literal["v1"] = "v1"

    # --- identity ---
    name: str = Field(min_length=1)          # the registry key
    source_class: str = Field(min_length=1)  # the cache prefix
    shape: SourceShape
    supports_global_query: bool = False

    #: Register at ``tier="internal"``: the promoted tool stays resolvable for
    #: in-process callers but is EXCLUDED from the declarable pool and the
    #: retrieval index, so it has no model-facing surface. For a seam a public
    #: tool resolves internally. Default False = ordinary registration.
    internal_only: bool = False

    #: Explicit error-code prefix token. ``source_class`` is the CACHE prefix and
    #: some sources stamp their error codes from a different token; one field
    #: cannot carry both, so a spec that needs them to differ sets this. Default
    #: (unset) = ``source_class.upper()``.
    error_prefix: str | None = None

    #: The input-error SUFFIX; ``error_prefix`` fixes only the prefix. A
    #: per-param ``error_suffix`` overrides this for that param.
    input_error_suffix: str = "INPUT_ERROR"

    #: The empty / no-coverage suffix. A source that distinguishes "nothing
    #: covers this extent" from "nothing matched" stamps ``NO_COVERAGE`` here.
    empty_error_suffix: str = "EMPTY"

    #: The LLM-facing tool docstring, verbatim. It is the SOLE source of both the
    #: promoted tool's declaration description and its retrieval-index document
    #: text, so a change here moves the tool in both the selector and the
    #: retriever. ``None`` = synthesize one from the caveats and the corpus.
    docstring: str | None = None

    # --- endpoints + auth ---
    endpoints: dict[str, EndpointSpec] = Field(min_length=1)
    auth: AuthSpec = Field(default_factory=AuthSpec)

    # --- request-param schema, validated BEFORE any network call ---
    params: dict[str, ParamSpec] = Field(default_factory=dict)
    gates: GateSpec = Field(default_factory=GateSpec)

    # --- ingestion: shape-specific, so a flexible dict keyed by shape ---
    ingest: dict[str, Any] = Field(default_factory=dict)

    # --- named pure functions for the ONE irreducible per-source step
    hooks: HookSpec | None = None

    # --- cross-sibling pre-flight dispatch: one param value serves a named
    # --- sibling tool's result verbatim ---
    dispatch: list[DispatchSpec] = Field(default_factory=list)

    # --- named transform: a two-source JOIN on a key ---
    join: dict[str, Any] | None = None

    # --- normalization + output ---
    normalize: NormalizeSpec = Field(default_factory=NormalizeSpec)
    output: OutputSpec

    # --- cache + payload gate ---
    cache: CacheSpec
    payload_estimate: PayloadEstimateSpec

    # --- caveats and the same-data endpoint chain ---
    caveats: list[str] = Field(default_factory=list)
    #: SAME-DATA ENDPOINT MIRRORS ONLY, in order. Every entry names a KEY in this
    #: spec's own ``endpoints`` block - an alternate service publishing the SAME
    #: dataset, which the loudness floor lets walk silently. Registration REFUSES
    #: an entry naming no such key: endpoint resolution indexes ``endpoints`` and
    #: never the tool registry, so a sibling TOOL name here is a promise no code
    #: path can keep. A CROSS-DATASET alternative is not this mechanism - it is a
    #: declared rung on a fallback ladder, gated and stamped.
    endpoint_fallback: list[str] = Field(default_factory=list)

    # A source's NATIVE cell and tier facts live here, beside the source, so a
    # gate card can quote them next to a solver's declared range. Default () is a
    # source with no granularity-bearing param, which is the common case.
    resolution_declarations: tuple[ResolutionSpec, ...] = Field(default=())

    # The vertical reference this source's elevations are counted from. STATED
    # from the dataset's own documentation, never inferred from the bytes: a
    # guess would be indistinguishable from a fact to every reader downstream.
    # A source that cannot state ONE datum carries none, and a consumer refuses
    # on that rather than assuming zero.
    vertical_datum: str | None = None

    # A HEAVY raster fetcher declares its resolution confirm gate here and the
    # canonical fetch gate is synthesized onto the promoted tool. A named
    # template rather than an inline gate keeps the declarations terse and the
    # provider paths single-sourced. ``None`` = an un-gated fetch.
    confirm_gate: Literal["fetch_resolution"] | None = None

    # --- retrieval phrasings ---
    corpus: list[str] = Field(default_factory=list)

    @property
    def error_code_prefix(self) -> str:
        """The token ``error_code`` is stamped from: ``error_prefix`` when the
        spec pins one, else ``source_class`` upper-cased."""
        return self.error_prefix or self.source_class.upper()

    @model_validator(mode="after")
    def _validate_shape_consistency(self) -> "SourceSpec":
        """Cross-field consistency the router relies on at dispatch time."""
        _validate_style_row(self.name, self.output.style)
        # A raster shape emits tif; a vector or station shape emits fgb.
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
        # The record shape and the record layer type imply each other.
        if self.shape == "record" and self.output.layer_type != "record":
            raise ValueError(
                f"shape=record requires output.layer_type=record; "
                f"got {self.output.layer_type!r}"
            )
        if self.output.layer_type == "record" and self.shape != "record":
            raise ValueError(
                f"output.layer_type=record requires shape=record; got {self.shape!r}"
            )
        # An animation returns an ORDERED list of raster layers, so it must
        # declare both the pre-loop plan and the per-frame builder: without them
        # there is nothing to resolve the frame set or build a frame from.
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
        # A join only makes sense over a vector base shape.
        if self.join is not None and self.shape != "vector-fgb":
            raise ValueError(
                f"join transform requires shape=vector-fgb; got {self.shape!r}"
            )
        return self
