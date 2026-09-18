"""An EXTENT: one lon/lat bounding box with an optional name, from every way a user
names one.

A bbox pick, the canvas AOI, four numbers and a layer's bounds all enter through
``extent``; what reads an Extent after that reads ``.bbox`` ordered west, south,
east, north and never parses again. A bare place NAME refuses: an ingestion
resolves what it was handed and never fetches.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .user_input import PlaceNameError, UserInputError, lonlat_bbox

from .geometry import flatten_geometries, read_geometry_doc

__all__ = ["Extent", "bbox_equivalent", "bbox_overlaps", "extent"]

_CODE = "EXTENT_INVALID"
_LAYER_SCHEMES = ("s3://", "gs://", "file://", "/", "./")
_LAYER_SUFFIXES = (".geojson", ".json", ".fgb", ".gpkg", ".shp", ".parquet")


@dataclass(frozen=True, slots=True)
class Extent:
    """One bounding box in EPSG:4326, and the name the user gave it, if any."""

    bbox: tuple[float, float, float, float]
    name: str | None = None


#: Tolerance (degrees) two boxes agree within to be the SAME extent - about 0.1 m
#: at the equator, so only a re-derived box that rounds differently passes, never
#: a real move of the area.
_SAME_EXTENT_DEG = 1e-6


def bbox_equivalent(a: Any, b: Any, *, tol: float = _SAME_EXTENT_DEG) -> bool:
    """Whether two boxes name the same extent. Either side may be an ``Extent``,
    four numbers, or nothing at all; nothing is equivalent to nothing."""
    left = _as_bbox(a)
    right = _as_bbox(b)
    if left is None or right is None:
        return False
    return all(abs(x - y) <= tol for x, y in zip(left, right))


def bbox_overlaps(a: Any, b: Any) -> bool:
    """Whether two boxes share any ground, touching edges included. Either side may
    be an ``Extent``, four numbers, or nothing at all."""
    from shapely.geometry import box

    left = _as_bbox(a)
    right = _as_bbox(b)
    if left is None or right is None:
        return False
    return box(*left).intersects(box(*right))


def _as_bbox(value: Any) -> tuple[float, float, float, float] | None:
    """Four floats out of an ``Extent`` or any 4-sequence, else ``None``."""
    if isinstance(value, Extent):
        return value.bbox
    if not isinstance(value, Sequence) or isinstance(value, str) or len(value) != 4:
        return None
    try:
        return tuple(float(v) for v in value)  # type: ignore[return-value]
    except (TypeError, ValueError):
        return None


async def extent(value: Any, *, label: str = "extent",
                 code: str = _CODE) -> Extent | None:
    """THE ingestion: a pick, four numbers or a layer -> Extent.

    ``None`` only when nothing came; an unreadable value refuses typed, and a
    place name refuses naming ``geocode_location``."""
    if value is None or isinstance(value, Extent):
        return value
    if isinstance(value, Mapping):
        return _from_mapping(value, label, code)
    if isinstance(value, str):
        return await _from_text(value.strip(), label, code)
    return Extent(lonlat_bbox(value, label=label, code=code))


def _named(value: Mapping[str, Any]) -> str | None:
    name = value.get("name")
    text = str(name).strip() if name is not None else ""
    return text or None


def _from_mapping(value: Mapping[str, Any], label: str, code: str) -> Extent:
    if value.get("type"):
        return Extent(_bounds(value, label, code), _named(value))
    box = value.get("bbox", value.get("coordinates"))
    if box is not None and not isinstance(box, Mapping):
        return Extent(lonlat_bbox(box, label=label, code=code), _named(value))
    raise UserInputError(
        f"{label} {dict(value)!r} carries no box: give it as {{\"bbox\": [west, "
        "south, east, north]}} the way a pick returns it, as four numbers, or as "
        "GeoJSON.", code=code)


async def _from_text(text: str, label: str, code: str) -> Extent:
    if not text:
        raise UserInputError(f"{label} is an empty string.", code=code)
    if text.startswith("{"):
        return _from_mapping(json.loads(text), label, code)
    if text.startswith(_LAYER_SCHEMES) or text.lower().endswith(_LAYER_SUFFIXES):
        doc = await asyncio.to_thread(read_geometry_doc, text)
        return Extent(_bounds(doc, label, code))
    if not any(c.isalpha() for c in text):
        return Extent(lonlat_bbox(text, label=label, code=code))
    raise PlaceNameError(text, label=label, code=code,
                         wants="four numbers - west, south, east, north",
                         retry_as="[west, south, east, north]")


def _bounds(doc: Any, label: str, code: str) -> tuple[float, float, float, float]:
    """The bounds of every geometry in a GeoJSON document."""
    xs: list[float] = []
    ys: list[float] = []

    def walk(coords: Any) -> None:
        if isinstance(coords, Sequence) and coords and \
                isinstance(coords[0], (int, float)):
            xs.append(float(coords[0]))
            ys.append(float(coords[1]))
            return
        for part in coords or ():
            walk(part)

    for geometry in flatten_geometries(doc):
        walk(geometry.get("coordinates"))
    if not xs:
        raise UserInputError(
            f"the geometry supplied as {label} carries no coordinates to take "
            "bounds from.", code=code)
    return lonlat_bbox((min(xs), min(ys), max(xs), max(ys)), label=label, code=code)
