"""usgs_model_layers frames hooks: a plan per layer surface over the covering grid.

The frames are the top of layer 1 and then the bottom of each layer in order,
cropped like ``usgs_model_k``'s K frames and masked by the layers each surface
bounds: the top of layer 1 by layer 1, the bottom of layer n by layer n OR
layer n + 1 (it is also the top of n + 1), the last bottom by the last layer.
A surface is kept wherever any layer it bounds is active, so an inactive layer
stays unmeasured while the layers around it keep both their surfaces. Every frame is
metres on NAVD88: a model counted from another datum is put onto NAVD88 through
NOAA VDatum, refusing where the service has no offset."""

from __future__ import annotations

import io
import logging
import zipfile
from typing import Any

import numpy as np

from trid3nt_contracts.source_spec import SourceSpec

from ..._router.hooks import FrameDegraded, FramePlan, register_hook
from ..._router.transport import get_bytes
from .. import _usgs_model_grid as grid

logger = logging.getLogger(__name__)

__all__ = ["frames_plan", "frame_bytes"]

@register_hook("usgs_model_layers.frames_plan")
def frames_plan(spec: SourceSpec, params: dict[str, Any]) -> list[FramePlan]:
    """The top of layer 1, then the bottom of each layer, of the single grid
    that fully covers the AOI; an AOI outside both grids is a typed refusal
    naming them and their share."""
    q_bbox = tuple(round(float(v), 6) for v in params["bbox"])
    covering = grid.covering_archive(spec, q_bbox)
    arch = grid.ARCHIVES[covering]
    plans = []
    for surface in range(int(arch["layers"]) + 1):
        what = "top of layer 1" if surface == 0 else f"bottom of layer {surface}"
        stem = "top-l1" if surface == 0 else f"bot-l{surface}"
        plans.append(FramePlan(
            cache_params={
                "bbox": list(q_bbox),
                "archive": covering,
                "surface": surface,
                "tool": "fetch_usgs_model_layers",
            },
            name=f"{arch['label']} {what}",
            layer_id=f"model-layers-{covering}-{stem}-{q_bbox[0]:.3f}-{q_bbox[1]:.3f}",
            bbox=q_bbox,  # type: ignore[arg-type]
            fetch_context={"archive": covering, "surface": surface},
        ))
    logger.info("usgs_model_layers: bbox %s -> %s frames=%d",
                list(q_bbox), arch["label"], len(plans))
    return plans


@register_hook("usgs_model_layers.frame_bytes")
def frame_bytes(
    spec: SourceSpec, params: dict[str, Any], frame: FramePlan
) -> bytes:
    """Build ONE surface's EPSG:4326 COG in metres on NAVD88; an unreadable
    member or a surface with no active cells raises ``FrameDegraded`` for the
    executor to record, while the archive download and a missing datum offset
    stay typed errors."""
    key = str(frame.fetch_context["archive"])
    arch = grid.ARCHIVES[key]
    surface = int(frame.fetch_context["surface"])
    layer = max(surface, 1)
    what = "top of layer 1" if surface == 0 else f"bottom of layer {surface}"
    bbox = tuple(float(v) for v in frame.cache_params["bbox"])
    blob = grid.archive_bytes(spec, key, get_bytes)
    member = arch["top_member"] if surface == 0 else arch["bot_member"].format(n=surface)
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            values = grid.read_grid(zf, member, arch)
            mask = grid.read_grid(zf, arch["mask_member"].format(n=layer), arch)
            if 0 < surface < int(arch["layers"]):
                below = grid.read_grid(zf, arch["mask_member"].format(n=surface + 1), arch)
                mask = np.where((mask == 1) | (below == 1), 1, 0)
    except (zipfile.BadZipFile, KeyError, ValueError, OSError) as exc:
        raise FrameDegraded(
            f"{arch['label']} {what}: unreadable archive member ({exc})"
        ) from exc
    frame_of_model = str(arch["datum"])
    shift = None if frame_of_model == grid.TARGET else grid.shift_to_navd88(spec, bbox, frame_of_model)
    return grid.masked_window_cog(
        arch, values * float(arch["factor"]), mask, bbox, what, shift)
