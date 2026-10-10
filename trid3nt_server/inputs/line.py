"""THE LINE: the polyline a placed read is measured along."""

from __future__ import annotations

import logging
from typing import Any

from .shape import polylines, shape

__all__ = ["line"]

logger = logging.getLogger("trid3nt_server.inputs.line")

_CODE = "LINE_INVALID"


def line(value: Any, *, label: str = "line", code: str = _CODE) -> dict[str, Any] | None:
    """THE ingestion: a drawn polyline, a layer, a geometry or vertices -> one line.

    Several polylines stay several: merging them would decide where a gap closes."""
    if value is None:
        return None
    if isinstance(value, dict) and str(value.get("type") or "") in (
            "LineString", "MultiLineString"):
        return dict(value)
    lines = polylines(shape(value, label=label, code=code), label=label,
                      code=code)
    if len(lines) == 1:
        return {"type": "LineString",
                "coordinates": [[float(x), float(y)] for x, y in lines[0]]}
    return {"type": "MultiLineString",
            "coordinates": [[[float(x), float(y)] for x, y in part]
                            for part in lines]}
