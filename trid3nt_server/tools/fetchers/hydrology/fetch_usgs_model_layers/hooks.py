"""usgs_model_layers frames hooks: a plan per layer surface over the covering grid.

The frames are the top of layer 1 and then the bottom of each layer in order,
cropped and masked exactly as ``usgs_model_k`` crops and masks that layer's K
(the top of layer 1 by layer 1's mask, a layer's bottom by that layer's mask),
so frame i + 1 here and frame i there describe the same cells. Every frame is
metres on NAVD88: a model counted from another datum is put onto NAVD88 through
NOAA VDatum, refusing where the service has no offset."""

from __future__ import annotations

import io
import logging
import zipfile
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..._fetch_common import FetchError
from ..._router.errors import router_input_error, router_upstream_error
from ..._router.hooks import FrameDegraded, FramePlan, register_hook
from ..._router.transport import get_bytes
from .. import _usgs_model_grid as grid

logger = logging.getLogger(__name__)

__all__ = ["frames_plan", "frame_bytes"]

#: The frame the row states its elevations on; a model counted from another
#: frame is converted onto it, and one already on it is read as stated.
_TARGET = "NAVD88"


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


def _offset_m(spec: SourceSpec, lon: float, lat: float, frame: str) -> float:
    """What VDatum says a zero on ``frame`` reads at on NAVD88 at this point;
    its refusal is raised as this row's own, naming the point."""
    from trid3nt_server.tools import TOOL_REGISTRY

    try:
        record = TOOL_REGISTRY["fetch_vertical_datum_offset"].fn(
            point=[lon, lat], from_frame=frame.lower(), to_frame=_TARGET.lower())
    except FetchError as exc:
        message = (
            f"the {frame} elevations of the model layers cannot be put on "
            f"{_TARGET}: NOAA VDatum has no offset at ({lon:.6f}, {lat:.6f}) - "
            f"fetch_vertical_datum_offset says ({getattr(exc, 'error_code', '')}): {exc}")
        if getattr(exc, "retryable", False):
            raise router_upstream_error(spec.error_code_prefix, message) from exc
        raise router_input_error(
            spec.error_code_prefix, message, spec.input_error_suffix) from exc
    return float(record["offset_m"])


def _shift(spec: SourceSpec, bbox: tuple[float, ...], frame: str):
    """The offset onto NAVD88 across the AOI: VDatum's offset at each of the
    AOI's four corners, bilinear between them. A datum offset is a smooth
    field, so the corners the caller named bound it without a point chosen
    for them. The offsets are asked when the shift is first applied, so a
    frame with no active cell never asks."""
    west, south, east, north = bbox

    def shift(xs: Any, ys: Any) -> Any:
        sw, se, nw, ne = (
            _offset_m(spec, lon, lat, frame)
            for lon, lat in ((west, south), (east, south), (west, north), (east, north)))
        u = (xs - west) / max(east - west, 1e-12)
        v = (ys - south) / max(north - south, 1e-12)
        return (1 - u) * (1 - v) * sw + u * (1 - v) * se + (1 - u) * v * nw + u * v * ne

    return shift


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
    except (zipfile.BadZipFile, KeyError, ValueError, OSError) as exc:
        raise FrameDegraded(
            f"{arch['label']} {what}: unreadable archive member ({exc})"
        ) from exc
    frame_of_model = str(arch["datum"])
    shift = None if frame_of_model == _TARGET else _shift(spec, bbox, frame_of_model)
    return grid.masked_window_cog(
        arch, values * float(arch["factor"]), mask, bbox, what, shift)
