"""Typed READER for the worker's ``publish_manifest.json``.

The writer emits a plain dict, so the shape has two definitions held together
by ONE ``schema_version`` gate. The reader models are TOLERANT
(``extra="ignore"``): an additive key the writer grows must never break a
reader that has not redeployed yet.
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

#: The ONE schema_version this reader understands, in lockstep with the
#: writer's own constant. Any other value is UNKNOWN and refused, which is the
#: caller's signal to fall back rather than to guess at the body.
MANIFEST_SCHEMA_VERSION: int = 1


class _ReaderModel(BaseModel):
    """Base for the tolerant manifest reader models.
    ``extra="ignore"``, NOT ``forbid``: an additive key the writer grows is
    dropped rather than crashing a reader that has not redeployed."""

    model_config = ConfigDict(extra="ignore")


class PublishManifestBandStats(_ReaderModel):
    """Precomputed band-1 stats, so style resolution never re-reads the COG.
    ``is_categorical`` and ``is_rgba`` short-circuit to empty style params;
    ``p2`` / ``p98`` drive the generic percentile rescale for everything else."""

    is_categorical: bool = False
    is_rgba: bool = False
    p2: float | None = None
    p98: float | None = None
    min: float | None = None
    max: float | None = None


class PublishManifestLayer(_ReaderModel):
    """One ``layers[]`` entry - a single display-ready COG, ready to register."""

    layer_id_stem: str
    #: The EXACT grouping token layers are collected under.
    name: str
    layer_type: str = "raster"
    role: str = "primary"
    #: The declared style row - kind plus parameters. ``None`` takes the kind's
    #: bare default rather than leaving the layer unstyled.
    style: dict[str, Any] | None = None
    units: str = ""
    #: A BARE object-store key, not a tile URL.
    cog_uri: str
    #: LEGACY temporal marker. Frames ride the outputs manifest now, so a value
    #: here means a pre-collapse run or a producer that has not migrated.
    frame_no: int | None = None
    bbox: list[float] | None = None
    has_overviews: bool = True
    band_stats: PublishManifestBandStats = Field(
        default_factory=PublishManifestBandStats
    )
    #: Per-layer metrics: the aggregates this layer's own narration cites.
    #: The keys are the producing layer type's, not a fixed set.
    metrics: dict[str, Any] = Field(default_factory=dict)


class PublishManifest(_ReaderModel):
    """The full worker -> reader publish manifest, gated on ``schema_version``.
    Top-level ``metrics`` carries the run's PEAK aggregates, distinct from the
    per-layer metrics each entry carries."""

    schema_version: int
    engine: str = ""
    run_id: str = ""
    status: str = "ok"
    frame_count: int = 0
    metrics: dict[str, Any] = Field(default_factory=dict)
    layers: list[PublishManifestLayer] = Field(default_factory=list)
    error_code: str | None = None


def parse_publish_manifest(text: str | bytes) -> PublishManifest:
    """Parse and schema-gate a ``publish_manifest.json`` body into a typed model.
    ``ValueError`` on a non-dict body, a missing ``schema_version`` or an unknown
    one - that refusal is the caller's fallback trigger, never a silent parse."""
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
