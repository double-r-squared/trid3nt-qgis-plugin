"""usgs_model_k frames hooks: one plan per model layer over the covering grid.

The output is an ORDERED list of layer frames of the ONE archive whose grid
fully covers the AOI; the bytes hook reads that layer's K + mask members from
the constant archive, masks to active cells, warps the touched window to an
EPSG:4326 COG cropped to cells whose centers fall in the bbox. An AOI outside
both grids is a typed input refusal naming them; a layer with no active cells
degrades and is recorded, and only EVERY layer degrading raises the EMPTY."""

from __future__ import annotations

import io
import logging
import zipfile
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..._router.hooks import FrameDegraded, FramePlan, register_hook
from ..._router.transport import get_bytes
from .. import _usgs_model_grid as grid
from .._usgs_model_grid import ARCHIVES as _ARCHIVES
from .._usgs_model_grid import ring_lonlat as _ring_lonlat

logger = logging.getLogger(__name__)

__all__ = ["frames_plan", "frame_bytes"]


@register_hook("usgs_model_k.frames_plan")
def frames_plan(spec: SourceSpec, params: dict[str, Any]) -> list[FramePlan]:
    """One FramePlan per layer of the single grid that fully covers the AOI;
    an AOI outside both grids is a typed refusal naming them and their share."""
    q_bbox = tuple(round(float(v), 6) for v in params["bbox"])
    covering = grid.covering_archive(spec, q_bbox)
    arch = _ARCHIVES[covering]
    plans = [
        FramePlan(
            cache_params={
                "bbox": list(q_bbox),
                "archive": covering,
                "layer": n,
                "tool": "fetch_usgs_model_k",
            },
            name=f"{arch['label']} K layer {n}",
            layer_id=(
                f"model-k-{covering}-l{n}-{q_bbox[0]:.3f}-{q_bbox[1]:.3f}"
            ),
            bbox=q_bbox,  # type: ignore[arg-type]
            fetch_context={"archive": covering, "layer": n},
        )
        for n in range(1, int(arch["layers"]) + 1)
    ]
    logger.info(
        "usgs_model_k: bbox %s -> %s frames=%d",
        list(q_bbox),
        arch["label"],
        len(plans),
    )
    return plans


@register_hook("usgs_model_k.frame_bytes")
def frame_bytes(
    spec: SourceSpec, params: dict[str, Any], frame: FramePlan
) -> bytes:
    """Build ONE layer's EPSG:4326 COG; an unreadable member or a layer with
    no active cells raises ``FrameDegraded`` for the executor to record, while
    the archive download itself stays a typed upstream error."""
    ctx = frame.fetch_context
    key = str(ctx["archive"])
    arch = _ARCHIVES[key]
    layer = int(ctx["layer"])
    bbox = tuple(float(v) for v in frame.cache_params["bbox"])
    blob = grid.archive_bytes(spec, key, get_bytes)
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            k = grid.read_grid(zf, arch["k_member"].format(n=layer), arch)
            mask = grid.read_grid(zf, arch["mask_member"].format(n=layer), arch)
    except (zipfile.BadZipFile, KeyError, ValueError, OSError) as exc:
        raise FrameDegraded(
            f"{arch['label']} layer {layer}: unreadable archive member ({exc})"
        ) from exc
    return grid.masked_window_cog(
        arch, k * float(arch["factor"]), mask, bbox, f"layer {layer}"
    )
