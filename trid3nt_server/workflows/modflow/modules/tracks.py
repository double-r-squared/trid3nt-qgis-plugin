"""A PRT run's reads: its CSV track file as pathlines, endpoints and the distribution of travel times.

One record per tracking event; a particle is the pair (release package, release point). The columns and the status
codes are mf6's own (the PRT model's particle track output).
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trid3nt_server.workflows.runtime.primitives import OutputEmpty, Primitive, Series, Track

from ..errors import ModflowError

__all__ = ["Tracked", "read_endpoints", "read_pathlines", "read_travel_time"]

# ISTATUS: where a particle's last record leaves it.
_STATUS = {0: "released", 1: "still being tracked", 2: "reached a boundary face", 3: "stopped in a weak sink cell",
           4: "unused", 5: "stopped in a cell with no exit face", 6: "stopped in a stop zone",
           7: "stopped in an inactive cell", 8: "never released: its cell is inactive",
           9: "stopped in a subcell with no exit face", 10: "reached the stop time or the end of the simulation"}

_COLUMNS = ("iprp", "irpt", "ilay", "icell", "izone", "istatus", "ireason", "trelease", "t", "x", "y", "z")


@dataclass(frozen=True, kw_only=True)
class Tracked(Track):
    """A track read the module table publishes: the caption it goes under."""

    name: str


def _records(solved: Any, token: str) -> dict[tuple[int, int], list[dict[str, float]]]:
    """The CSV track file -> each particle's records in the order mf6 wrote them."""
    refused = solved.unsaved(token)
    if refused is not None:
        raise refused
    from .outputs import filerecord

    path = solved.local(filerecord(solved.stated("OC_TRACKCSV_FILERECORD")))
    try:
        with open(path, newline="") as handle:
            table = list(csv.DictReader(handle))
    finally:
        Path(path).unlink(missing_ok=True)
    if not table:
        raise OutputEmpty("the track file holds no particle record.")
    missing = [name for name in _COLUMNS if name not in table[0]]
    if missing:
        raise ModflowError(f"the track file lacks the column(s) {', '.join(missing)}.", error_code="MODFLOW_OUTPUT_SHAPE")
    particles: dict[tuple[int, int], list[dict[str, float]]] = {}
    for row in table:
        record = {name: float(row[name]) for name in _COLUMNS}
        particles.setdefault((int(record["iprp"]), int(record["irpt"])), []).append(record)
    return particles


def _lonlat(solved: Any, particles: dict[tuple[int, int], list[dict[str, float]]]) -> dict[tuple[int, int], Any]:
    """Each particle's records' positions in lon/lat."""
    import numpy as np
    from pyproj import Transformer

    every = [record for records in particles.values() for record in records]
    lon, lat = Transformer.from_crs(solved.utm_epsg, 4326, always_xy=True).transform(
        np.array([r["x"] for r in every]), np.array([r["y"] for r in every]))
    placed, at = {}, 0
    for key, records in particles.items():
        placed[key] = np.column_stack([lon[at:at + len(records)], lat[at:at + len(records)]])
        at += len(records)
    return placed


def read_pathlines(primitive: Primitive, solved: Any) -> Tracked:
    """``pathlines()``: one line per particle through the places the track file records, [lon, lat, z] with the travel time at each vertex in its properties."""
    import numpy as np

    particles = _records(solved, "PATHLINES")
    where = _lonlat(solved, particles)
    second = float(solved.seconds([1.0])[0])
    features, unmoved = [], 0
    travel = []
    for key, records in particles.items():
        seen: list[tuple[float, float, float, float]] = []
        coordinates, times = [], []
        for rank, record in enumerate(records):
            stamp = (record["t"], record["x"], record["y"], record["z"])
            if seen and stamp == seen[-1]:
                continue
            seen.append(stamp)
            coordinates.append([round(float(where[key][rank][0]), 7), round(float(where[key][rank][1]), 7), record["z"]])
            times.append((record["t"] - record["trelease"]) * second)
        if len(coordinates) < 2:
            unmoved += 1
            continue
        last = records[-1]
        travel.append(times[-1])
        features.append({"type": "Feature", "geometry": {"type": "LineString", "coordinates": coordinates},
                         "properties": {"release_package": key[0], "release_point": key[1],
                                        "released_at_s": last["trelease"] * second,
                                        "travel_time_s": times[-1], "travel_times_s": times,
                                        "end": _STATUS.get(int(last["istatus"]), f"status {int(last['istatus'])}")}})
    if not features:
        raise OutputEmpty("no particle moved from its release point.")
    return Tracked(name="pathlines", features={"type": "FeatureCollection", "features": features},
                   measures={"particles": len(particles), "pathlines": len(features), "unmoved": unmoved,
                             "shortest_s": float(min(travel)), "longest_s": float(max(travel)),
                             "median_s": float(np.median(travel))})


def read_endpoints(primitive: Primitive, solved: Any) -> Tracked:
    """``endpoints()``: a point per particle where its last record leaves it, with how it ended."""
    particles = _records(solved, "ENDPOINTS")
    where = _lonlat(solved, particles)
    second = float(solved.seconds([1.0])[0])
    features, ended = [], {}
    for key, records in particles.items():
        last = records[-1]
        status = _STATUS.get(int(last["istatus"]), f"status {int(last['istatus'])}")
        ended[status] = ended.get(status, 0) + 1
        lon, lat = where[key][-1]
        features.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [round(float(lon), 7), round(float(lat), 7), last["z"]]},
                         "properties": {"release_package": key[0], "release_point": key[1], "end": status,
                                        "layer": int(last["ilay"]), "zone": int(last["izone"]),
                                        "travel_time_s": (last["t"] - last["trelease"]) * second}})
    return Tracked(name="endpoints", features={"type": "FeatureCollection", "features": features},
                   measures={"particles": len(particles), "ended": dict(sorted(ended.items()))})


def read_travel_time(primitive: Primitive, solved: Any) -> Series:
    """``travel_time()``: particles ended by each travel time, over the particles that ended."""
    import numpy as np

    particles = _records(solved, "TRAVEL_TIME")
    second = float(solved.seconds([1.0])[0])
    times = sorted((records[-1]["t"] - records[-1]["trelease"]) * second for records in particles.values()
                   if int(records[-1]["istatus"]) >= 2)
    if not times:
        raise OutputEmpty("no particle ended, so there is no travel time.")
    return Series(name="particles ended", units="particles", times=np.asarray(times), values=np.arange(1, len(times) + 1, dtype=float),
                  at="by travel time", measures={"ended": len(times), "particles": len(particles),
                                                 "shortest_s": times[0], "longest_s": times[-1],
                                                 "median_s": float(np.median(times))})
