"""The worker -> agent ``publish_manifest.json`` contract (plain JSON dict).

Per the spike design (``reports/design/worker_side_postprocess_spike.md`` section
6.1): the manifest schema lives here as a PLAIN dict authored by the worker (NOT
in ``contracts``, which the worker CodeBuild context does not ship). A
typed Pydantic mirror gated on the SAME ``schema_version`` lives agent-side and
is consumed only by the agent reader in the NEXT phase. Two definitions, one
schema_version gate.

The agent reads this manifest and becomes register-only: build the TiTiler URL
from the bare ``cog_uri``, resolve ``style_preset`` -> rescale/colormap via its
own ``_TITILER_STYLE_REGISTRY``, skip ``_ensure_raster_has_overviews``
(``has_overviews: true``), skip the COG re-read for rescale (``band_stats``),
register + persist.
"""

from __future__ import annotations

import json
import logging
from typing import Any

LOG = logging.getLogger("trid3nt.worker.raster_postprocess.manifest")

#: Bumped whenever the manifest shape changes incompatibly. The agent reader
#: gates on this exact value and falls back to the legacy on-box postprocess if
#: the manifest is absent OR the schema_version is unknown.
MANIFEST_SCHEMA_VERSION: int = 1

#: The manifest object key, written alongside completion.json under the run prefix.
MANIFEST_FILENAME: str = "publish_manifest.json"


def build_layer_entry(
    *,
    layer_id_stem: str,
    name: str,
    role: str,
    style_preset: str,
    units: str,
    cog_uri: str,
    frame_no: int | None,
    bbox: list[float] | None,
    band_stats: dict[str, Any],
    metrics: dict[str, Any] | None = None,
    has_overviews: bool = True,
    layer_type: str = "raster",
) -> dict[str, Any]:
    """Build one ``layers[]`` entry. ``cog_uri`` is a BARE s3:// key (NOT a tile
    URL — the agent re-templates) and ``style_preset`` is a KEY only."""
    entry: dict[str, Any] = {
        "layer_id_stem": layer_id_stem,
        "name": name,
        "layer_type": layer_type,
        "role": role,
        "style_preset": style_preset,
        "units": units,
        "cog_uri": cog_uri,
        "frame_no": frame_no,
        "bbox": bbox,
        "has_overviews": has_overviews,
        "band_stats": band_stats,
    }
    if metrics is not None:
        entry["metrics"] = metrics
    return entry


def build_manifest(
    *,
    engine: str,
    run_id: str,
    status: str,
    frame_count: int,
    metrics: dict[str, Any],
    layers: list[dict[str, Any]],
    error_code: str | None = None,
) -> dict[str, Any]:
    """Assemble the full manifest dict (schema_version gated)."""
    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "engine": engine,
        "run_id": run_id,
        "status": status,
        "frame_count": frame_count,
        "metrics": metrics,
        "layers": layers,
    }
    if error_code is not None:
        manifest["error_code"] = error_code
    return manifest


def manifest_to_json(manifest: dict[str, Any]) -> str:
    """Serialize the manifest to a stable, indented JSON string."""
    return json.dumps(manifest, indent=2, sort_keys=False)


def parse_manifest_json(text: str) -> dict[str, Any]:
    """Parse + lightly validate a manifest JSON string (round-trip helper/tests).

    Raises ``ValueError`` on a non-dict body, a missing schema_version, or an
    unknown schema_version (the agent's fallback trigger).
    """
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("publish_manifest.json must be a JSON object")
    sv = data.get("schema_version")
    if sv is None:
        raise ValueError("publish_manifest.json missing schema_version")
    if int(sv) != MANIFEST_SCHEMA_VERSION:
        raise ValueError(
            f"unknown publish_manifest schema_version {sv!r} "
            f"(this build understands {MANIFEST_SCHEMA_VERSION})"
        )
    return data
