"""Subfaults laid on the Slab2 interface from a stated rectangle.

A rectangle (an anchor on the interface, its along-strike length and down-dip width, the subfault size, rake and
slip) becomes a grid of Clawpack subfaults: positions are walked along the interface in half-subfault steps and
each centre reads depth, strike and dip from the slab record at that centre. Every value of the rupture is
stated or read from the record; a place the record does not hold refuses by name.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from ..errors import GeoclawError

__all__ = ["ANCHORS", "slab_subfaults"]

#: Where the stated point sits on the rectangle: the midpoint of its top edge, or its centre.
ANCHORS = ("top", "centre")

_WHOLE = 1e-6


def _refuse(code: str, message: str) -> None:
    raise GeoclawError(message, error_code=f"GEOCLAW_{code}")


def _count(total: float, part: float, what: str) -> int:
    n = total / part
    if abs(n - round(n)) > _WHOLE or round(n) < 1:
        _refuse("SLAB_FAULT_TILING", f"SlabFault {what} {total:g} m is not a whole number of subfaults of {part:g} m "
                f"({n:g}): state a {what} that the subfault size divides.")
    return int(round(n))


def slab_subfaults(slab: Mapping[str, Any], *, longitude: float, latitude: float, anchor: str, length: float,
                   width: float, subfault_length: float, subfault_width: float, rake: float, slip: float
                   ) -> list[dict[str, Any]]:
    """The stated rectangle -> subfault statements in Clawpack's own SubFault names, in metres and degrees.

    Each half-subfault step moves along the interface by the horizontal distance ``s cos(dip)`` toward strike + 90
    degrees (down dip) read where the step starts, or ``s`` along the strike; each centre then reads its own depth,
    strike and dip, so a subfault is planar at its centre's local values."""
    from pyproj import Geod

    from trid3nt_server.tools.fetchers import _slab2_grids as slab2

    if anchor not in ANCHORS:
        _refuse("SLAB_FAULT_ANCHOR", f"SlabFault anchor {anchor!r} is none of {' or '.join(ANCHORS)}.")
    rows, cols = _count(width, subfault_width, "width"), _count(length, subfault_length, "length")
    try:
        zones = slab2.blocks_from_record(slab)
    except slab2.SlabError as exc:
        _refuse("SLAB_FAULT_RECORD", str(exc))
    geod = Geod(ellps="WGS84")

    def read(lon: float, lat: float, what: str) -> dict[str, Any]:
        try:
            return slab2.sample(zones, lon, lat)
        except slab2.SlabError as exc:
            _refuse("SLAB_FAULT_COVERAGE", f"SlabFault {what}: {exc}")

    def step(point: tuple[float, float], metres: float, heading: str, what: str) -> tuple[float, float]:
        at = read(*point, what)
        azimuth = {"down": at["strike"] + 90.0, "up": at["strike"] - 90.0, "strike": at["strike"],
                   "back": at["strike"] + 180.0}[heading]
        horizontal = metres * (math.cos(math.radians(at["dip"])) if heading in ("down", "up") else 1.0)
        lon, lat, _ = geod.fwd(point[0], point[1], azimuth, horizontal)
        return float(lon), float(lat)

    top = (longitude, latitude)
    read(*top, "anchor")
    if anchor == "centre":
        for _ in range(rows):
            top = step(top, subfault_width / 2.0, "up", "walking up dip from the anchor")
    along = {0: top}
    for heading, sign in (("strike", 1), ("back", -1)):
        point = top
        for half in range(1, cols):
            point = step(point, subfault_length / 2.0, heading, "walking along strike from the anchor")
            along[sign * half] = point
    out: list[dict[str, Any]] = []
    for column in range(cols):
        edge = along[2 * column + 1 - cols]
        for row in range(rows):
            centre = step(edge, subfault_width / 2.0, "down", f"walking down dip in column {column + 1}")
            here = read(*centre, f"subfault row {row + 1} column {column + 1}")
            if here["depth_km"] <= 0.0:
                _refuse("SLAB_FAULT_DEPTH", f"SlabFault subfault row {row + 1} column {column + 1} reads depth "
                        f"{here['depth_km']:g} km at ({centre[0]:.4f}, {centre[1]:.4f}), which is no depth below the "
                        "surface an Okada subfault takes.")
            out.append({"longitude": centre[0], "latitude": centre[1], "depth": here["depth_km"] * 1000.0,
                        "strike": here["strike"], "dip": here["dip"], "rake": float(rake), "slip": float(slip),
                        "length": float(subfault_length), "width": float(subfault_width),
                        "coordinate_specification": "centroid"})
            edge = step(centre, subfault_width / 2.0, "down", f"walking down dip in column {column + 1}")
    return out
