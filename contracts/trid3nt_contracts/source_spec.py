"""``SourceSpec`` - the generic data-router source specification. Data, not code.

One ``source.yaml`` per data source, beside that source's own folder. It is the
shape the router loader AND the parity harness both validate against, so the
two cannot drift. INDISTINGUISHABILITY is the bar: a row-driven source flows
through the identical pipeline and surfaces identically to a coded one.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from .common import ContractModel
from .coverage import PER_RECORD, Coverage
from .tool_registry import TTLClass

__all__ = [
    "SourceShape",
    "AuthMode",
    "ParamType",
    "PayloadModel",
    "EndpointSpec",
    "AuthSpec",
    "CredentialSpec",
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

#: The preset family, closed: a typo here would paint a layer as something it is not.
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



#: The base executor shape. A hybrid row declares a base shape plus a transform block wrapping that executor.
SourceShape = Literal[
    "raster-cog",
    "vector-fgb",
    "station-timeseries-fgb",
    "record",
    "animation_frames",
]

#: ``cds`` = the Copernicus client's own config; ``vault`` / ``token`` are reserved.
AuthMode = Literal["none", "api_key_env", "cds", "vault", "token"]

#: Compound param types: int_range [start, end]; date_compact YYYY-MM-DD or YYYYMMDD normalized to YYYYMMDD;
#: point [lon, lat], finite-checked here (a CONUS or exclusion gate belongs to the executor); float_list a
#: scalar or list checked against ``values``, sorted and deduped; str_list stripped, sorted and deduped for
#: cache-key stability; bool coerced with ``bool(value)``; datetime_range ISO [start, end] with start <= end,
#: echoed as isoformat strings for cache-key stability - the sub-day window no ``iso_date`` pair can express.
ParamType = Literal[
    "bbox", "iso_date", "enum", "int", "float", "str", "int_range", "date_compact",
    "point", "float_list", "str_list", "bool", "datetime_range",
]

PayloadModel = Literal["bbox_area", "per_station", "per_feature", "tiled"]




class EndpointSpec(ContractModel):
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


class CredentialSpec(ContractModel):
    """The key a source needs, named where the source is declared.

    ``name`` is the scope a stored key lands under, so two rows served by ONE
    upstream account state the same name and one entered key serves both."""

    name: str = Field(min_length=1, max_length=120)
    label: str = Field(min_length=1, max_length=120)
    #: None when no public self-serve signup exists; a fabricated URL would pass for a real one.
    signup_url: str | None = Field(default=None, max_length=512)
    #: Env var read when no session key was pushed; the same name the source's own hook reads.
    env_var: str = Field(min_length=1, max_length=200)


class AuthSpec(ContractModel):
    """Auth mode, the shared User-Agent, and the key this source needs."""

    mode: AuthMode = "none"
    #: A keyed source's credential; absent is a public source, which never refuses for a key.
    credential: CredentialSpec | None = None
    user_agent: str = "trid3nt_default"


class ParamSpec(ContractModel):
    """One request-param's validation contract, applied BEFORE any network call.
    ``quantize`` is a bbox directive: ``round_6dp``, or ``res_<m>`` to snap to a
    raster resolution."""

    type: ParamType
    required: bool = False
    default: Any = None
    values: list[Any] | None = None          # enum only
    quantize: str | None = None              # bbox only: round_6dp | res_<m>
    max_range_days: int | None = None        # iso_date pair ceiling (end param)
    #: Inclusive range gate; out of range raises a typed input error stamped with ``error_suffix``.
    min: float | None = None
    max: float | None = None
    #: iso_date coverage bounds; a violation is a typed ``*_NOT_AVAILABLE``.
    min_date: str | None = None              # static ISO lower coverage bound
    max_future_days: int | None = None       # end <= today + N (future ceiling)
    #: Per-param input-error suffix; None uses the row-level suffix.
    error_suffix: str | None = None
    #: Force a None-default param optional in the promoted schema (right for ``T | None``, wrong for ``T``).
    schema_optional: bool = False
    #: str alias table applied after lower-case and strip; an unmapped value passes through verbatim.
    aliases: dict[str, str] | None = None
    #: enum only: lower-case and strip before the allowed-set check (default is a strict match).
    lowercase: bool = False


class GateSpec(ContractModel):
    """Pre-fetch gates."""

    conus_only: bool = False                 # bbox must intersect CONUS
    #: CONUS envelope override (west, south, east, north) for a grid reaching past the shared one;
    #: borrowing another source's footprint false-refuses real coverage.
    conus_bbox: tuple[float, float, float, float] | None = None
    max_bbox_deg2: float | None = None       # hard ceiling, raw degree^2
    #: Ceiling on bbox area in approximate km^2 (cos-lat scaled); a deg^2 ceiling is not the same guardrail across latitude.
    max_bbox_km2: float | None = None
    max_stations: int | None = None          # station-timeseries only
    max_features: int | None = None          # vector only (paging cap)


class NormalizeSpec(ContractModel):
    """Normalization stamps - what makes a row-driven layer indistinguishable."""

    crs: str = "EPSG:4326"
    units: str | None = None
    datum: str | None = None
    quantity: str | None = None
    orientation: str | None = None           # raster only
    #: Request param whose resolved value becomes the emitted units instead of ``units``.
    units_from_param: str | None = None
    #: ``{param, map}`` maps the param value to units; a value absent from the map stamps no units. Overrides ``units``.
    units_by_param: dict[str, Any] | None = None


class OutputSpec(ContractModel):
    """The output surface."""

    #: ``record`` returns a bare JSON dict, not a renderable layer; pairs with ``shape: record`` and
    #: ``ext: json``, and the record hook raises typed errors, never fabricates a dict.
    layer_type: Literal["raster", "vector", "record"]
    ext: Literal["tif", "fgb", "json"]
    role: Literal["primary", "context", "input"] = "primary"
    #: How this dataset draws itself: ``{kind: continuous | classed | reference | mesh}`` plus that kind's keys
    #: (``ramp`` / ``units`` / ``label`` / ``scale`` / ``classes`` / ``geometry`` / ``color``).
    #: ``by_param`` (``{param, map: {value: partial row}}``) overrides them per param value.
    style: dict[str, Any] | None = None
    #: ``{param, map: {value: role}}``; a value absent from the map falls back to ``role``.
    role_by_param: dict[str, Any] | None = None
    emit_bbox: bool = True
    #: The human-facing layer name, verbatim; a modelled product must say so here.
    display_name: str | None = None
    #: Stamp the emitted bbox from the features' extent (``{pad: <deg>}`` widens a single-point axis),
    #: read back from the file so cache hit and miss agree.
    bbox_from_features: dict[str, Any] | None = None
    #: A key into ``execution.LAYER_RESULT_MODELS``, built from the base layer plus the ``hooks.envelope``
    #: dict; declared together with it.
    result_model: str | None = None
    #: Hook ``<source>.<point>(spec, params)`` whose dict is returned instead of the layer when the vector file is feature-empty.
    variant_by_emptiness: str | None = None
    #: True binds a fetch-time recorder whose dict the cache persists as a sidecar and replays on a hit;
    #: needs ``hooks.envelope``.
    provenance: bool = False
    #: Keep NULL-geometry features as attribute-only rows; such a file is written without a spatial index.
    keep_null_geometry: bool = False


class CacheSpec(ContractModel):
    """The cache TTL class."""

    ttl_class: TTLClass


class PayloadEstimateSpec(ContractModel):
    """Payload-MB estimator inputs; the estimator itself is synthesized from
    them. Every coefficient is optional - each ``model`` reads the ones it
    needs and ignores the rest."""

    model: PayloadModel
    floor_mb: float = 0.01
    ceil_mb: float | None = None
    mb_per_sq_deg: float | None = None
    #: ``bbox_area`` MB/deg^2 table ``{param, map, default}``; overrides ``mb_per_sq_deg``, an absent value
    #: falls to ``default``, then the scalar, then 0.01.
    mb_per_sq_deg_by_param: dict[str, Any] | None = None
    kb_per_station_per_day: float | None = None
    overhead_kb: float | None = None
    stations_per_sq_deg: float | None = None
    kb_per_feature: float | None = None
    features_per_sq_deg: float | None = None
    mb_per_tile: float | None = None
    tile_deg2: float | None = None


class HookSpec(ContractModel):
    """Named extension points for the ONE irreducible per-source step.
    Each names a REGISTERED PURE FUNCTION, ``<source_key>.<point>``, resolved at
    load. A hook only COMPUTES - transport, caching and gates stay router-owned.
    """

    # A field is added only when a real source cannot be expressed without it.


    #: ``(spec, params) -> list[RequestPlan]``; a paged source is called once per page.
    build_request: str | None = None

    #: ``(spec, params, bodies) -> list[GeoJSON feature dict]``; raises the typed EMPTY / RESULT_TOO_LARGE /
    #: UPSTREAM errors rather than returning nothing.
    parse_response: str | None = None

    # Resolve and enrich phases are declared only by a source that needs them; the router owns orchestration.

    #: Phase R, before the cache key: ``(spec, params) -> list[RequestPlan]`` turns a name into an id;
    #: ``[]`` means no round trip. The resolved id enters the cache key.
    resolve_build: str | None = None

    #: Phase R: ``(spec, params, bodies) -> dict`` merged into params; raises the typed input error on an unknown or ambiguous name.
    resolve_parse: str | None = None

    #: Offset paging: ``(spec, params, bodies) -> RequestPlan | None`` returns the next page or None to stop;
    #: ``build_request`` builds page 1.
    next_page: str | None = None

    #: Phase E: ``(spec, params, features) -> list[(ref, RequestPlan)]``, the ordered per-item detail requests;
    #: the hook applies its own per-pass cap and a failed ref is recorded, never dropped.
    enrich_plan: str | None = None

    #: Phase E: ``(row, params, features, results) -> list[dict]`` folds the detail back in; every input
    #: feature survives, one whose refs failed with null detail.
    enrich_merge: str | None = None

    #: Last hook: ``(spec, params, layer, data) -> dict`` of extra fields for ``output.result_model`` (declared
    #: together) and base overrides; pure, and ``uri`` / ``layer_type`` are dropped from its return.
    envelope: str | None = None

    #: The one sanctioned impurity: ``(row, params, *, timeout_s) -> features | (array, transform, crs)`` for a
    #: source whose library owns discovery and sockets; an unmapped library exception becomes a retryable upstream error.
    delegate: str | None = None

    #: ``(spec, params) -> None``, after type and gate validation and before the cache read, so the refusal is pre-network.
    delegate_validate: str | None = None

    #: ``(spec, params, bodies) -> dict | None`` for a ``record`` source; the first non-None plan result wins
    #: and all-None raises the typed empty error.
    record: str | None = None

    #: Library-socket sibling of ``resolve_build`` / ``resolve_parse``: ``(spec, params, *, timeout_s) -> dict``
    #: merged into params before the cache key, so "latest" keys deterministically. Pairs with ``hooks.delegate``.
    delegate_resolve: str | None = None

    #: Multi-step HTTP pre-cache-key resolve ``(spec, params) -> dict``, merged into params so a "latest
    #: available" request does not key non-deterministically.
    pre_resolve: str | None = None

    #: ``(spec, params) -> dict[int, RGBA]``, a pure palette baked into band 1 of a raster source.
    colormap: str | None = None

    #: ``animation_frames`` pre-loop plan ``(spec, params) -> list[FramePlan]``; raises the typed EMPTY error
    #: when the window matches no frames.
    frames_plan: str | None = None

    #: ``(spec, params, frame) -> bytes`` builds one frame and may do its own tile I/O; ``FrameDegraded`` skips
    #: a frame and is recorded, the EMPTY error only when every frame degrades.
    frame_bytes: str | None = None

    #: ``(spec, status, body) -> RouterError | None`` splits non-2xx into typed errors (401/403 credential,
    #: 404 non-retryable input) before the default; pure.
    classify_status: str | None = None


class DispatchSpec(ContractModel):
    """A row-declared, SINGLE-TARGET pre-flight dispatch to a sibling tool.
    One declared param value serves the request from a NAMED sibling, returning
    that tool's result VERBATIM - its cache prefix, its ids, no double fetch."""

    # One source per row; this is the only field naming a sibling. Narrow by design: one target string,
    # row-declared literals, no chains (a target must not itself dispatch), evaluated on the raw params
    # before validation, gates, cache or fetch.

    param: str = Field(min_length=1)
    equals_any: list[str] = Field(min_length=1)
    #: ``lower_strip`` normalizes before the ``equals_any`` check; ``none`` compares verbatim.
    normalize: Literal["lower_strip", "none"] = "lower_strip"
    to: str = Field(min_length=1)
    #: target_arg -> this spec's raw param name; the target validates it under its own contract.
    pass_args: dict[str, str] = Field(default_factory=dict)




class SourceSpec(ContractModel):
    """A single data source's router specification; ``name`` IS its registry key.
    ``ingest`` and ``join`` stay flexible dicts because they vary per shape;
    every other top-level key is strictly typed and an unknown one is a defect."""

    schema_version: Literal["v1"] = "v1"

    name: str = Field(min_length=1)          # the registry key
    source_class: str = Field(min_length=1)  # the cache prefix
    shape: SourceShape
    supports_global_query: bool = False

    #: Register at ``tier="internal"``: resolvable in-process but excluded from the declarable pool and the retrieval index.
    internal_only: bool = False

    #: ``source_class`` is the cache prefix; set this when error codes stamp from a different token (default ``source_class.upper()``).
    error_prefix: str | None = None

    #: Input-error suffix; a per-param ``error_suffix`` overrides it.
    input_error_suffix: str = "INPUT_ERROR"

    #: Empty / no-coverage suffix; ``NO_COVERAGE`` separates nothing-covers from nothing-matched.
    empty_error_suffix: str = "EMPTY"

    #: The LLM-facing tool docstring, verbatim: the sole source of the tool description and its retrieval text. None synthesizes one.
    docstring: str | None = None

    endpoints: dict[str, EndpointSpec] = Field(min_length=1)
    auth: AuthSpec = Field(default_factory=AuthSpec)

    params: dict[str, ParamSpec] = Field(default_factory=dict)
    gates: GateSpec = Field(default_factory=GateSpec)

    ingest: dict[str, Any] = Field(default_factory=dict)

    hooks: HookSpec | None = None


    dispatch: list[DispatchSpec] = Field(default_factory=list)

    join: dict[str, Any] | None = None

    normalize: NormalizeSpec = Field(default_factory=NormalizeSpec)
    output: OutputSpec

    cache: CacheSpec
    payload_estimate: PayloadEstimateSpec

    caveats: list[str] = Field(default_factory=list)
    #: Same-data endpoint mirrors only: each entry names a key in this row's ``endpoints``, never a tool, and
    #: registration refuses otherwise. A cross-dataset alternative is a gated fallback-ladder rung.
    endpoint_fallback: list[str] = Field(default_factory=list)

    # What this source covers, as the match reads it: one row per data class served (class, where, time, cell,
    # zero, value units). The only statement of coverage; a source with no row is never matched but stays model-callable.
    coverage: list[Coverage] = Field(default_factory=list)

    # Vertical reference stated from the dataset's own documentation, never inferred; a source with no single
    # datum carries none and a consumer refuses rather than assuming zero.
    vertical_datum: str | None = None

    # A heavy raster fetcher declares its resolution confirm gate by named template; None is un-gated.
    confirm_gate: Literal["fetch_resolution"] | None = None

    corpus: list[str] = Field(default_factory=list)

    @property
    def error_code_prefix(self) -> str:
        """The token ``error_code`` is stamped from: ``error_prefix`` when the
        row pins one, else ``source_class`` upper-cased."""
        return self.error_prefix or self.source_class.upper()

    @model_validator(mode="after")
    def _validate_one_row_per_class(self) -> "SourceSpec":
        """A source states each class once per kind: two rows of one class and kind are two answers to one question."""
        seen = [(row.data_class, row.kind) for row in self.coverage]
        twice = sorted({f"{name} ({kind})" for name, kind in seen
                        if seen.count((name, kind)) > 1})
        if twice:
            raise ValueError(
                f"{self.name} carries more than one row of "
                f"{', '.join(twice)}: one row per data class and kind")
        return self

    @model_validator(mode="after")
    def _adopt_the_coverage_datum(self) -> "SourceSpec":
        """The zero a layer carries is the one its row states."""
        if self.vertical_datum is None:
            # A per-feature zero is read off the feature, never adopted here.
            stated = {row.datum for row in self.coverage
                      if row.datum and row.datum != PER_RECORD}
            if len(stated) == 1:
                self.vertical_datum = stated.pop()
        return self

    @model_validator(mode="after")
    def _validate_shape_consistency(self) -> "SourceSpec":
        """Cross-field consistency the router relies on at dispatch time."""
        _validate_style_row(self.name, self.output.style)
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
        if self.shape == "record" and self.output.layer_type != "record":
            raise ValueError(
                f"shape=record requires output.layer_type=record; "
                f"got {self.output.layer_type!r}"
            )
        if self.output.layer_type == "record" and self.shape != "record":
            raise ValueError(
                f"output.layer_type=record requires shape=record; got {self.shape!r}"
            )
        # An animation needs both the pre-loop plan and the per-frame builder.
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
        if self.join is not None and self.shape != "vector-fgb":
            raise ValueError(
                f"join transform requires shape=vector-fgb; got {self.shape!r}"
            )
        return self
