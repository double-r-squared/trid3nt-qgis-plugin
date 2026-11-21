"""The MODFLOW particle-tracking wrapper: its dictionary, the release statement and what a solved run writes."""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping

from ...runtime.module import MOMENT, Output
from ..errors import ModflowError
from .module import ModflowModule, _start_date_time, periods
from .outputs import read_extent, read_mesh
from .tracks import read_endpoints, read_pathlines, read_travel_time

__all__ = ["MODULE_OUTPUT", "PRT"]

# What the module writes, read off the CSV track file. A pathline is drawn, an endpoint is a point, the travel times are a chart.
MODULE_OUTPUT: Mapping[str, Output] = MappingProxyType({
    "PATHLINES": Output("pathlines", "", style={"kind": "reference", "geometry": "line"}, varies=False),
    "ENDPOINTS": Output("endpoints", "", style={"kind": "reference", "geometry": "point"}, varies=False),
    "TRAVEL_TIME": Output("particles ended", "particles", varies=False),
})

_RELEASE = ("geometry", "on", "layer", "z", "depth", "spacing", "count")


def _number(value: Any, name: str, *, low: float, high: float | None = None, whole: bool = False) -> None:
    if value is None:
        return
    top = f" to {high:g}" if high is not None else ""
    if isinstance(value, bool) or not isinstance(value, int if whole else (int, float)) or value < low \
            or (high is not None and value > high):
        raise ModflowError(f"Release {name} is {'an integer' if whole else 'a number'} from {low:g}{top}; got {value!r}.",
                           error_code="MODFLOW_STRESS_UNSTATED")


def _release(value: Any) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    if isinstance(value, (list, tuple)) and value:
        stated = [item for each in value if (item := _release(each)[0])]
        return ({"PRP_PACKAGEDATA": {"release": [one for item in stated for one in item["PRP_PACKAGEDATA"]["release"]]}}
                if stated else {}), {}
    if not isinstance(value, Mapping):
        raise ModflowError(f"Release takes a mapping of {', '.join(_RELEASE)}, or a list of them; got {value!r}.",
                           error_code="MODFLOW_STRESS_UNSTATED")
    unknown = sorted(set(value) - set(_RELEASE))
    if unknown:
        raise ModflowError(f"Release takes {', '.join(_RELEASE)}; {', '.join(unknown)} is none of them.",
                           error_code="MODFLOW_STRESS_UNSTATED")
    if value.get("geometry") is None and value.get("on") is None:
        return {}, {}
    if value.get("geometry") is not None and value.get("on") is not None:
        raise ModflowError("Release states a geometry and a feature it is on; a release is at one of them.",
                           error_code="MODFLOW_STRESS_UNSTATED")
    if value.get("on") is not None and (not isinstance(value["on"], str) or not value["on"].strip()):
        raise ModflowError(f"Release on is the name a feature was given; got {value['on']!r}.",
                           error_code="MODFLOW_STRESS_UNSTATED")
    _number(value.get("layer"), "layer", low=1, whole=True)
    _number(value.get("z"), "z", low=0, high=1)
    _number(value.get("depth"), "depth", low=0)
    _number(value.get("spacing"), "spacing", low=0)
    _number(value.get("count"), "count", low=1, whole=True)
    if value.get("spacing") == 0:
        raise ModflowError("Release spacing is a distance above zero.", error_code="MODFLOW_STRESS_UNSTATED")
    if value.get("depth") is not None:
        if value.get("z") is not None:
            raise ModflowError("Release states a depth and a z; the height is one of them.",
                               error_code="MODFLOW_STRESS_UNSTATED")
    elif value.get("z") is None:
        raise ModflowError("Release states no z and no depth; the engine has no value for the height of a release, so "
                           "state z (0 the layer's bottom, 1 its top) with a layer, or a depth below the grid's top.",
                           error_code="MODFLOW_STRESS_UNSTATED")
    elif value.get("layer") is None and value.get("on") is None:
        raise ModflowError("Release states z and no layer; state the layer it is the fraction of.",
                           error_code="MODFLOW_STRESS_UNSTATED")
    return {"PRP_PACKAGEDATA": {"release": [{key: held for key, held in value.items() if held is not None}]}}, {}


_release.__doc__ = (
    "Particles released from the map: Release(geometry | on, layer, z | depth, spacing | count), or a list of them.\n\n"
    "    geometry is a point (one particle at it), a line (a particle every spacing metres from its start, or count\n"
    "    from end to end) or a polygon (spacing: a lattice of squares that wide anchored at its bounding box, a\n"
    "    particle at the centre of each square it holds; count n: n by n particles per cell side in every rectangular\n"
    "    cell whose centre it holds). on names a placed feature of the flow model (a well, a boundary), and count n\n"
    "    releases n by n particles in each of its cells. The height is a layer (from 1) with z, the fraction of the\n"
    "    layer's thickness above its bottom (0 the bottom, 1 the top), or a depth below the grid's top at the cell, in\n"
    "    the model's length unit. A point is never moved; one outside the grid refuses with the nearest cell. The\n"
    "    cells are computed; release times and the package's options are PRP keywords stated by name.")


PRT = ModflowModule("prt")
PRT.MODULE_OUTPUT = MODULE_OUTPUT
# The simulation's own clock on every body: Periods spells the marching, the moment the instant it opens at.
PRT.composites(reads={"Periods": ("until", "steps")}, Periods=periods)
PRT.composites(reads={"Release": ("geometry", "layer", "z", "depth", "spacing", "count")}, Release=_release)
PRT.FILLED_BY = MappingProxyType({MOMENT: {"TDIS_START_DATE_TIME": _start_date_time}})
# The read of what a solved run writes: the track file's three reads, and the mesh's own two.
PRT.reads(pathlines=read_pathlines, endpoints=read_endpoints, travel_time=read_travel_time, mesh=read_mesh,
          extent=read_extent)
