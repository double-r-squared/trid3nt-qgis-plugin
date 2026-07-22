"""Typed AGENT-SIDE mirror of the worker's ``publish_manifest.json`` contract.

The worker writes the manifest as a PLAIN dict (see
``services/workers/_raster_postprocess/manifest.py``) because the CodeBuild
worker context does not ship ``packages/contracts``. This module is the AGENT's
typed READER of that dict - two definitions, ONE ``schema_version`` gate. The
SFINCS raster postprocess offload (Phase 4) lifts the heavy NetCDF/.mat -> COG
conversion into the Batch worker; the worker now writes display-ready
overview-bearing COGs + this manifest, and the agent collapses to: parse this
manifest, build the TiTiler tile URL from each bare ``cog_uri`` + the agent-owned
style registry (keyed on ``style_preset``), register + persist.

Design notes (deliberate divergence from ``GraceModel``):

- These are TOLERANT reader models (``extra="ignore"``), NOT ``GraceModel``
  subclasses (which ``forbid`` extras). A forward-compatible additive key the
  worker grows must NEVER break the agent reader before the agent redeploys
  (data-source-fallback norm). The schema_version gate is the hard compatibility
  contract; unknown keys are ignored, missing optional keys default.

- ``parse_publish_manifest(text)`` mirrors the worker's ``parse_manifest_json``:
  it REJECTS a non-dict body, a missing schema_version, or an unknown
  schema_version (raising ``ValueError``) - that rejection is the agent's
  one-release FALLBACK trigger (the caller then runs the legacy on-box
  postprocess path). A known schema_version validates into ``PublishManifest``.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "MANIFEST_SCHEMA_VERSION",
    "PublishManifestBandStats",
    "PublishManifestLayer",
    "PublishManifest",
    "parse_publish_manifest",
]

#: The ONE schema_version the agent reader understands. MUST stay in lockstep
#: with ``services/workers/_raster_postprocess/manifest.MANIFEST_SCHEMA_VERSION``.
#: A manifest carrying any other value is treated as "unknown" -> the agent
#: falls back to the legacy on-box postprocess path (one-release safety).
MANIFEST_SCHEMA_VERSION: int = 1


class _ReaderModel(BaseModel):
    """Base for the tolerant manifest reader models.

    ``extra="ignore"`` (NOT ``forbid``) so a forward-compatible additive key the
    worker grows is silently dropped rather than crashing the agent before the
    agent redeploys.
    """

    model_config = ConfigDict(extra="ignore")


class PublishManifestBandStats(_ReaderModel):
    """Precomputed band-1 stats - the worker's substitute for the agent's COG
    re-download in ``publish_layer._resolve_titiler_style_params``.

    ``is_categorical`` / ``is_rgba`` short-circuit the categorical-palette and
    RGBA/multiband passthrough guards (the agent returns empty style params for
    those, exactly as the on-box path did). ``p2`` / ``p98`` feed the
    GENERIC-fallback percentile rescale for a single-band continuous preset NOT
    in the agent registry, so the agent never re-reads the COG.
    """

    is_categorical: bool = False
    is_rgba: bool = False
    p2: float | None = None
    p98: float | None = None
    min: float | None = None
    max: float | None = None


class PublishManifestLayer(_ReaderModel):
    """One ``layers[]`` entry - a single display-ready COG the agent registers.

    ``cog_uri`` is a BARE ``s3://`` key (the worker NEVER embeds a tile URL - the
    agent re-templates it onto ``GRACE2_TILE_SERVER_BASE``). ``style_preset`` is a
    KEY into the agent-owned ``_TITILER_STYLE_REGISTRY`` (the preset -> rescale +
    colormap table stays agent-side as the single source of truth). ``name`` MUST
    be the EXACT web grouping token ("Peak flood depth" / "Flood depth step N" /
    the wave equivalents) so the web ``detectSequentialGroups`` scrubber forms.
    """

    layer_id_stem: str
    name: str
    layer_type: str = "raster"
    role: str = "primary"
    style_preset: str
    units: str = ""
    cog_uri: str
    frame_no: int | None = None
    bbox: list[float] | None = None
    has_overviews: bool = True
    band_stats: PublishManifestBandStats = Field(
        default_factory=PublishManifestBandStats
    )
    #: Per-layer metrics. On the PEAK depth layer these are the FloodMetrics
    #: aggregates; on each wave layer they carry the WaveFieldLayerURI narration
    #: scalars (max_hs_m / mean_tp_s / mean_dir_deg / wave_area_km2).
    metrics: dict[str, Any] = Field(default_factory=dict)


class PublishManifest(_ReaderModel):
    """The full worker -> agent publish manifest (gated on ``schema_version``).

    Top-level ``metrics`` carries the run's PEAK aggregates that replace the
    in-process ``postprocess_flood`` return value the agent consumed for
    ``FloodMetrics``.
    """

    schema_version: int
    engine: str = ""
    run_id: str = ""
    status: str = "ok"
    frame_count: int = 0
    metrics: dict[str, Any] = Field(default_factory=dict)
    layers: list[PublishManifestLayer] = Field(default_factory=list)
    error_code: str | None = None


def parse_publish_manifest(text: str | bytes) -> PublishManifest:
    """Parse + schema-gate a ``publish_manifest.json`` body into a typed model.

    Mirrors the worker's ``parse_manifest_json`` rejection rules so the agent's
    one-release fallback trigger is unambiguous:

    Raises ``ValueError`` on a non-dict body, a missing ``schema_version``, or an
    UNKNOWN ``schema_version`` (the caller then runs the legacy on-box
    postprocess path). A known schema_version validates into ``PublishManifest``.
    """
    if isinstance(text, (bytes, bytearray)):
        text = text.decode("utf-8")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("publish_manifest.json must be a JSON object")
    sv = data.get("schema_version")
    if sv is None:
        raise ValueError("publish_manifest.json missing schema_version")
    try:
        sv_int = int(sv)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"publish_manifest schema_version is not an int: {sv!r}"
        ) from exc
    if sv_int != MANIFEST_SCHEMA_VERSION:
        raise ValueError(
            f"unknown publish_manifest schema_version {sv!r} "
            f"(this agent build understands {MANIFEST_SCHEMA_VERSION})"
        )
    return PublishManifest.model_validate(data)
