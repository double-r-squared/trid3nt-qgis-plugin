"""THE LINE: the polyline a placed read is measured along.

One slot over every way a line reaches a run: the centerline a domain's producer
measured beside its polygon, a polyline the user draws on the canvas, their own
line layer, or a typed list of vertices. What reads it afterwards reads ONE
GeoJSON geometry and never parses again, so a profile along a reach's centerline
and a profile along a line somebody drew across a lake are the same read.
"""

from __future__ import annotations

import logging
from typing import Any

from .shape import polylines, shape

__all__ = ["line"]

logger = logging.getLogger("trid3nt_server.inputs.line")

_CODE = "LINE_INVALID"


def line(value: Any, *, label: str = "line", code: str = _CODE) -> dict[str, Any] | None:
    """THE ingestion: a drawn polyline, a layer, a geometry or vertices -> one line.

    ``None`` only when nothing came. Several polylines stay several - a reach
    mapped in two pieces is one line with a gap in it, and a reader that merges
    them decides where the gap closes."""
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
