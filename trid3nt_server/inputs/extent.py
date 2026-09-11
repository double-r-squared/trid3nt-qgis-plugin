"""An EXTENT: one lon/lat bounding box with an optional name, from every way a user
names one.

A bbox pick, the canvas AOI, a place name and a layer's bounds all enter through
``extent``; what reads an Extent after that reads ``.bbox`` ordered west, south,
east, north and never parses again.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .user_input import UserInputError, lonlat_bbox

from .geometry import flatten_geometries, read_geometry_doc

__all__ = ["Extent", "extent"]

_CODE = "EXTENT_INVALID"
_LAYER_SCHEMES = ("s3://", "gs://", "file://", "/", "./")
_LAYER_SUFFIXES = (".geojson", ".json", ".fgb", ".gpkg", ".shp", ".parquet")


@dataclass(frozen=True, slots=True)
class Extent:
    """One bounding box in EPSG:4326, and the name the user gave it, if any."""

    bbox: tuple[float, float, float, float]
    name: str | None = None


async def extent(value: Any, *, label: str = "extent", code: str = _CODE,
                 half_deg: float = 0.06) -> Extent | None:
    """THE ingestion: a pick, four numbers, a place or a layer -> Extent.

    A place becomes the box ``half_deg`` either side of its geocoded centre;
    ``None`` only when nothing came, and an unreadable value refuses typed."""
    if value is None or isinstance(value, Extent):
        return value
    if isinstance(value, Mapping):
        return _from_mapping(value, label, code)
    if isinstance(value, str):
        return await _from_text(value.strip(), label, code, half_deg)
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


async def _from_text(text: str, label: str, code: str, half_deg: float) -> Extent:
    if not text:
        raise UserInputError(f"{label} is an empty string.", code=code)
    if text.startswith("{"):
        return _from_mapping(json.loads(text), label, code)
    if text.startswith(_LAYER_SCHEMES) or text.lower().endswith(_LAYER_SUFFIXES):
        doc = await asyncio.to_thread(read_geometry_doc, text)
        return Extent(_bounds(doc, label, code))
    if not any(c.isalpha() for c in text):
        return Extent(lonlat_bbox(text, label=label, code=code))
    from .aoi import geocode_place

    found = await geocode_place(text)
    if found is None:
        raise UserInputError(
            f"{label} {text!r} could not be geocoded. Give a place the geocoder "
            "knows, four numbers, or pick the box on the canvas.", code=code)
    lon, lat = found
    d = float(half_deg)
    return Extent((round(lon - d, 4), round(lat - d, 4),
                   round(lon + d, 4), round(lat + d, 4)), text)


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
