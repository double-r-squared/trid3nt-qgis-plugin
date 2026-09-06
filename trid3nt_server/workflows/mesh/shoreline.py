"""The shoreline ladder: what the water is cut from, at the fidelity the ask needs.

A domain cut from the shoreline is only as good as the line it was cut from. A
substrate coarser than the triangles asked for cannot describe the harbour the
ask is about: what it leaves is scraps that touch at points, which is a domain
with no boundary numbering rather than a coarse answer. So the substrate is a
LADDER with a stated fidelity per rung, and an ask no rung resolves is refused by
name rather than served by the nearest thing on the box.

  * ``osm_coastline`` - OpenStreetMap ``natural=coastline``, drawn at street
    resolution and fetched per AOI. The harbour-scale rung. The ways are open
    lines with the LAND ON THE LEFT, so the land polygons the mesher cuts from
    are derived here, by closing the ways against the extent's own box.
  * ``gshhg`` - the machine-local GSHHG L1 polygon shapefile named by
    ``TRID3NT_GSHHG_SHP``. The coarse rung, at the vertex spacing its own
    resolution letter publishes: full 0.1 km, high 0.2 km, intermediate 1 km,
    low 5 km, crude 25 km.

The rung that serves is the COARSEST one that still resolves the ask - a
shoreline far finer than the mesh is detail the triangulator throws away and a
fetch nobody needed - and a rung that yields nothing hands over to the next. The
rung is journaled onto the mesh, because a domain's shape is the first thing an
answer depends on.
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from trid3nt_server.workflows.mesh.meshers import MeshToolError

logger = logging.getLogger("trid3nt_server.workflows.mesh.shoreline")

__all__ = ["Shoreline", "land_polygons", "resolve_shoreline"]

#: The fetcher the harbour-scale rung IS, and the spacing OSM draws a coastline
#: at - metres, the resolution a way surveyed off imagery carries.
_OSM_TOOL = "fetch_osm_coastline"
_OSM_NOMINAL_M = 10.0

#: The vertex spacing each GSHHG resolution letter is published at, from the
#: dataset's own documentation. The letter is in the file's name (GSHHS_i_L1),
#: which is the only place the file states which resolution it is.
_GSHHG_NOMINAL_M = {"f": 100.0, "h": 200.0, "i": 1000.0, "l": 5000.0,
                    "c": 25000.0}

#: How far off a coastline segment the land/water probe stands, as a fraction of
#: the extent's shorter span. Small enough to stay inside the face the segment
#: bounds, large enough to survive the coordinate's own precision.
_PROBE_FRAC = 1.0e-4


@dataclass(frozen=True, slots=True)
class Shoreline:
    """The file the mesher cuts from, the rung it came from, and what to journal."""

    path: Path
    rung: str
    note: str


def resolve_shoreline(bbox: Sequence[float], resolution_m: float,
                      rundir: Path) -> Shoreline:
    """Climb the ladder for this extent and ask -> the shoreline that serves it."""
    asked = float(resolution_m)
    rungs = [_gshhg_rung(), _osm_rung()]
    serving = sorted((r for r in rungs if r.nominal_m <= asked),
                     key=lambda r: -r.nominal_m)
    tried: list[str] = []
    for rung in serving:
        served, why = rung.serve(bbox, rundir)
        if served is not None:
            note = (f"shoreline ladder: {rung.name} served this domain "
                    f"({rung.nominal_m:g} m nominal against a {asked:g} m ask)"
                    + (f"; {'; '.join(tried)}" if tried else ""))
            logger.info("%s", note)
            return Shoreline(path=served, rung=rung.name, note=note)
        tried.append(f"{rung.name} yielded nothing ({why})")
    raise MeshToolError(
        "MESH_SHORELINE_UNAVAILABLE",
        f"no shoreline substrate resolves a {asked:g} m ask over {tuple(bbox)}: "
        + "; ".join(_why(rung, asked, tried) for rung in rungs)
        + ". Set TRID3NT_GSHHG_SHP to a GSHHG L1 polygon shapefile whose "
        "resolution the ask needs, or ask for an edge the substrate can "
        "describe - a shoreline coarser than the triangles cuts scraps rather "
        "than a domain.")


def _why(rung: "_Rung", asked: float, tried: Sequence[str]) -> str:
    """One rung's line in the refusal: what it is, and why it did not serve."""
    for line in tried:
        if line.startswith(rung.name):
            return line
    if rung.nominal_m > asked:
        return (f"{rung.name} resolves {rung.nominal_m:g} m, which is coarser "
                f"than the {asked:g} m asked for")
    return f"{rung.name} was not reached"


# --------------------------------------------------------------------------- #
# The rungs.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class _Rung:
    """One substrate: what it is called, what it resolves, and how it serves."""

    name: str
    nominal_m: float
    serve: Any


def _gshhg_rung() -> _Rung:
    """The machine-local GSHHG shapefile, at the resolution its name states."""
    declared = (os.environ.get("TRID3NT_GSHHG_SHP") or "").strip()
    path = Path(declared) if declared else None
    letter = (path.name.split("_")[1] if path and len(path.name.split("_")) > 2
              else "")
    # A rung that is not on this box states no resolution, so it is ranked LAST
    # rather than given a fidelity it never had: it is still walked, and what it
    # contributes is the name of the variable that would have brought it.
    nominal = (_GSHHG_NOMINAL_M.get(letter.lower(), _GSHHG_NOMINAL_M["c"])
               if path is not None and path.exists() else 0.0)
    name = f"gshhg {path.name}" if path else "gshhg (TRID3NT_GSHHG_SHP unset)"

    def serve(_bbox: Sequence[float], _rundir: Path) -> tuple[Path | None, str]:
        if path is None:
            return None, "TRID3NT_GSHHG_SHP is unset"
        if not path.exists():
            return None, f"TRID3NT_GSHHG_SHP names {path}, which does not exist"
        return path, ""

    return _Rung(name=name, nominal_m=nominal, serve=serve)


def _osm_rung() -> _Rung:
    """OpenStreetMap coastline, fetched for the extent and closed into land."""

    def serve(bbox: Sequence[float], rundir: Path) -> tuple[Path | None, str]:
        from trid3nt_server.tools import TOOL_REGISTRY

        layer = TOOL_REGISTRY[_OSM_TOOL].fn(
            bbox=[float(v) for v in bbox],
            purpose="the shoreline this domain is cut from")
        lines = _coastline_walks(layer)
        if not lines:
            return None, ("OpenStreetMap maps no coastline over this extent - an "
                          "inland water body is natural=water, not a coastline")
        land = land_polygons(lines, tuple(float(v) for v in bbox))
        if not land:
            return None, ("the coastline over this extent bounds no land - the "
                          "whole extent is on one side of it")
        out = rundir / "shoreline"
        out.mkdir(parents=True, exist_ok=True)
        written = out / "osm_coastline_land.geojson"
        written.write_text(json.dumps({
            "type": "FeatureCollection",
            "features": [{"type": "Feature", "properties": {"land": True},
                          "geometry": geom} for geom in land]}))
        return written, ""

    return _Rung(name=_OSM_TOOL, nominal_m=_OSM_NOMINAL_M, serve=serve)


def _coastline_walks(layer: Any) -> list[list[tuple[float, float]]]:
    """The fetched coastline as coordinate walks, in the direction OSM drew them."""
    from trid3nt_server.workflows.mesh.inputs import op_geometry

    doc = op_geometry(layer)
    walks: list[list[tuple[float, float]]] = []

    def walk(geometry: Any) -> None:
        kind = str((geometry or {}).get("type") or "")
        if kind == "LineString":
            walks.append([(float(x), float(y))
                          for x, y in geometry["coordinates"]])
        elif kind == "MultiLineString":
            for part in geometry["coordinates"]:
                walks.append([(float(x), float(y)) for x, y in part])
        elif kind == "GeometryCollection":
            for part in geometry.get("geometries") or ():
                walk(part)

    for feature in (doc.get("features") or ()):
        walk((feature or {}).get("geometry"))
    if not doc.get("features"):
        walk(doc.get("geometry") if "geometry" in doc else doc)
    return [w for w in walks if len(w) >= 2]


# --------------------------------------------------------------------------- #
# Open coastline -> the land it bounds.
# --------------------------------------------------------------------------- #
def land_polygons(walks: Sequence[Sequence[tuple[float, float]]],
                  bbox: tuple[float, float, float, float]) -> list[dict[str, Any]]:
    """Coastline walks + the extent's box -> the LAND polygons, as GeoJSON.

    OSM draws a coastline with the LAND ON THE LEFT of the way's direction, and
    that convention is the whole of the classification: the box and the walks
    split the extent into faces, and each walk says which of the two faces it
    divides is land. A walk that ENDS inside the box divides nothing - both sides
    of it are the same face - and that is refused rather than guessed, because a
    guess there is the sea meshed over the town.
    """
    from shapely.geometry import LineString, Point, box, mapping
    from shapely.ops import polygonize, unary_union

    frame = box(*bbox)
    clipped = [part for w in walks for part in _inside(w, frame)]
    if not clipped:
        return []
    faces = list(polygonize(unary_union(
        [frame.exterior, *(LineString(part) for part in clipped)])))
    if not faces:
        return []
    step = _PROBE_FRAC * min(bbox[2] - bbox[0], bbox[3] - bbox[1])
    sides: tuple[set[int], set[int]] = (set(), set())
    for part in clipped:
        for (x0, y0), (x1, y1) in zip(part[:-1], part[1:]):
            span = math.hypot(x1 - x0, y1 - y0)
            if span == 0.0:
                continue
            mid = (0.5 * (x0 + x1), 0.5 * (y0 + y1))
            left = (-(y1 - y0) / span, (x1 - x0) / span)
            for index, sign in ((0, 1.0), (1, -1.0)):
                probe = Point(mid[0] + sign * step * left[0],
                              mid[1] + sign * step * left[1])
                for face, geom in enumerate(faces):
                    if geom.contains(probe):
                        sides[index].add(face)
                        break
    both = sides[0] & sides[1]
    if both:
        raise MeshToolError(
            "MESH_SHORELINE_DOES_NOT_CLOSE",
            f"{len(both)} of the {len(faces)} faces this extent splits into are "
            "on BOTH sides of the coastline, so the coastline does not divide "
            "the extent into land and water: a way ends inside the box rather "
            "than crossing it. Widen the extent so every coastline way crosses "
            "it, or supply the water body's own polygon as the extent.")
    return [mapping(faces[face]) for face in sorted(sides[0])]


def _inside(walk: Sequence[tuple[float, float]],
            frame: Any) -> list[list[tuple[float, float]]]:
    """The parts of one walk inside the box, each in the walk's own direction."""
    from shapely.geometry import LineString

    if len(walk) < 2:
        return []
    cut = LineString(walk).intersection(frame)
    parts: list[list[tuple[float, float]]] = []

    def take(geom: Any) -> None:
        kind = geom.geom_type
        if kind == "LineString" and len(geom.coords) >= 2:
            parts.append([(float(x), float(y)) for x, y in geom.coords])
        elif kind in ("MultiLineString", "GeometryCollection"):
            for part in geom.geoms:
                take(part)

    if not getattr(cut, "is_empty", True):
        take(cut)
    return parts
