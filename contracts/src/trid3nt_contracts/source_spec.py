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
ParamType = Literal["bbox", "iso_date", "enum", "int", "float", "str"]

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


class OutputSpec(GraceModel):
    """Output surface (contract sec 1.1 ``output``)."""

    layer_type: Literal["raster", "vector"]
    ext: Literal["tif", "fgb", "json"]
    role: Literal["primary", "context", "input"] = "primary"
    style_preset: str                        # may template on a param


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

    #: Explicit error-code prefix token (contract sec 0 / VERDICT finding #1).
    #: ``source_class`` doubles as the cache prefix and MUST equal the twin's for
    #: cache indistinguishability, but three twins stamp their A.6 ``error_code``
    #: from a DIFFERENT token than their cache source_class (COOPS_TIDES vs
    #: noaa_coops_tides, ESRI_LANDCOVER vs esri_landcover_10m, HIFLD_INFRA vs
    #: hifld_critical_infrastructure). One field cannot carry both, so a spec sets
    #: ``error_prefix`` to the twin's exact token; the router stamps error codes
    #: from :meth:`error_code_prefix`. Default (unset) = ``source_class.upper()``.
    error_prefix: str | None = None

    # --- endpoints + auth ---
    endpoints: dict[str, EndpointSpec] = Field(min_length=1)
    auth: AuthSpec = Field(default_factory=AuthSpec)

    # --- request-param schema (validate BEFORE any network call) ---
    params: dict[str, ParamSpec] = Field(default_factory=dict)
    gates: GateSpec = Field(default_factory=GateSpec)

    # --- ingestion (shape-specific; flexible dict keyed by shape, sec 1.2) ---
    ingest: dict[str, Any] = Field(default_factory=dict)

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
