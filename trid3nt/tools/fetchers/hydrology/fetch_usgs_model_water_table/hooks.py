"""usgs_model_water_table delegate hook: the head of the model's own water table as an elevation on NAVD88.

The head is the starting head of the cell's topmost active layer from the one archive, in metres; a model
counted from another datum is put onto NAVD88 per cell through PROJ's local VERTCON grid. A cell is kept where the thickness row also
measures it, and the row answers only where it measures every cell of the box."""

from __future__ import annotations

import io
import zipfile
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..._router.errors import router_empty_error, router_upstream_error
from ..._router.hooks import FrameDegraded, register_hook
from ..._router.transport import get_bytes
from .. import _usgs_model_grid as grid

__all__ = ["read_head"]


@register_hook("usgs_model_water_table.read")
def read_head(spec: SourceSpec, params: dict[str, Any], *, timeout_s: float) -> tuple[Any, Any, str]:
    """``(head in m on NAVD88, transform, crs)`` over the AOI from the single grid that holds it; an AOI no
    grid holds whole, or a box with an unmeasured cell, is the row's EMPTY."""
    bbox = tuple(round(float(v), 6) for v in params["bbox"])
    key = grid.covering_archive(spec, bbox)
    arch = grid.ARCHIVES[key]
    blob = grid.archive_bytes(spec, key, get_bytes)
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            head, _thickness, measured = grid.head_column(zf, arch)
    except (zipfile.BadZipFile, KeyError, ValueError, OSError) as exc:
        raise router_upstream_error(
            spec.error_code_prefix, f"{arch['label']}: unreadable archive member ({exc})",
            retryable=False) from exc
    model_datum = str(arch["datum"])
    shift = None if model_datum == grid.TARGET else grid.shift_to_navd88(spec, bbox, model_datum)
    try:
        out, transform = grid.masked_window(arch, head, measured, bbox, "water table", shift, whole=True)
    except FrameDegraded as exc:
        raise router_empty_error(spec.error_code_prefix, str(exc), spec.empty_error_suffix) from exc
    return out, transform, "EPSG:4326"
