"""A module's outputs: the read of each primitive off a solved GeoClaw run.

fgmax is read by Clawpack's FGmaxGrid, an fgout frame by its FGoutGrid and a gauge by GaugeSolution; each raster is
a lon/lat grid of values the framework writes as COGs, a gauge a series. A product is total depth where the engine calls the cell
wet (GEO_DRY_TOLERANCE) and nothing elsewhere; the overland rows also drop the cells wet in frame 0, the initial sea.
"""

from __future__ import annotations

import shutil
import tempfile
from contextlib import contextmanager
from functools import cached_property
from pathlib import Path
from typing import Any, Iterator, Mapping

from trid3nt_server.workflows.runtime.primitives import OutputEmpty, Primitive, Rasters, Series

from ..errors import GeoclawError

__all__ = ["PRIMITIVES", "Solved", "read_field", "read_max_over_time", "read_series"]

# Each output by token: the primitive that reads it, and the keyword that makes the engine write it.
_READ_BY = {"MAX_DEPTH": "max_over_time", "MAX_SPEED": "max_over_time", "MAX_SURFACE": "max_over_time",
           "ARRIVAL_TIME": "max_over_time", "MAX_OVERLAND_DEPTH": "max_over_time",
           "DEPTH": "field", "SURFACE": "field", "OVERLAND_DEPTH": "field",
           "GAUGE_SURFACE": "series", "GAUGE_DEPTH": "series"}
_KEYWORD = {"max_over_time": "FGMAX_FGMAX_GRIDS", "field": "FGOUT_FGOUT_GRIDS", "series": "GAUGE_GAUGES"}

# The nodes of a lon/lat grid and the nodes of another coincide within this many degrees (about 0.1 m).
_COINCIDE_DEG = 1.0e-6


def _refuse(code: str, message: str) -> GeoclawError:
    return GeoclawError(message, error_code=f"GEOCLAW_{code}")


def _unsaved(keyword: str, why: str) -> GeoclawError:
    return _refuse("OUTPUT_UNSAVED", f"{keyword} is stated by nothing, so {why}; state it and solve again.")


class _Stage:
    """A scratch directory the run's files are downloaded into, where Clawpack's readers open them."""

    def __init__(self, run_id: str, work: Path) -> None:
        self.run_id = run_id
        self.work = work

    def fetch(self, *names: str) -> None:
        from trid3nt_server.workflows.solver.solver import download_result

        for name in names:
            local = Path(download_result(self.run_id, name))
            shutil.move(str(local), str(self.work / name))
            local.parent.rmdir()


class Solved:
    """One solved run: the sheet it solved and the files it left under its run prefix, each fetched where it is read.

    ``run`` is the solve step's handle plus the filled ``sheet``.
    """

    def __init__(self, run: Mapping[str, Any], body: Any) -> None:
        self.run = run
        self.body = body
        self.run_id = str(run["run_id"])
        self.sheet = run["sheet"]

    def stated(self, identifier: str) -> Any:
        row = self.sheet.filled.get(identifier)
        return None if row is None else row.value

    @cached_property
    def dry_tolerance(self) -> float:
        stated = self.stated("GEO_DRY_TOLERANCE")
        return float(self.body.slot("GEO_DRY_TOLERANCE").engine_default if stated is None else stated)

    @cached_property
    def until(self) -> float:
        """The second the run ends at: its stated CLAW_TFINAL, which has no default."""
        stated = self.stated("CLAW_TFINAL")
        if stated is None:
            raise _unsaved("CLAW_TFINAL", "no instant ends the last frame's window")
        return float(stated)

    @contextmanager
    def staged(self) -> Iterator[_Stage]:
        with tempfile.TemporaryDirectory(prefix=f"run-{self.run_id}-") as work:
            yield _Stage(self.run_id, Path(work))

    def output(self, token: Any, kind: str) -> tuple[str, Any]:
        key = str(token).strip().upper()
        row = self.body.MODULE_OUTPUT.get(key)
        if row is None:
            raise _refuse("OUTPUT_UNKNOWN", f"{token!r} is no output of {self.body.MODULE}; the module writes "
                          f"{sorted(self.body.MODULE_OUTPUT)}.")
        if _READ_BY[key] != kind:
            raise _refuse("OUTPUT_READ", f"{key} is read by {_READ_BY[key]}(...), not {kind}(...).")
        return key, row

    def unsaved(self, key: str) -> GeoclawError | None:
        """Why the run wrote nothing of the output ``key``, or None where it wrote it."""
        keyword = _KEYWORD[_READ_BY[key]]
        if not self.stated(keyword):
            return _unsaved(keyword, f"the run wrote no {_READ_BY[key]} product to read for {key}")
        if _READ_BY[key] != "series" and self.stated("GEO_COORDINATE_SYSTEM") != 2:
            return _unsaved("GEO_COORDINATE_SYSTEM", "the grid nodes are not lon/lat and no raster is placed on them")
        if key == "MAX_SPEED" and (self.stated("FGMAX_NUM_FGMAX_VAL") or 1) < 2:
            return _unsaved("FGMAX_NUM_FGMAX_VAL", "fgmax holds no speed (2 or 5 saves it)")
        if key == "MAX_OVERLAND_DEPTH" and not self.stated("FGOUT_FGOUT_GRIDS"):
            return _unsaved("FGOUT_FGOUT_GRIDS", "no frame 0 gives the initial sea to take off the overland depth")
        if _READ_BY[key] == "series" and self.stated("GAUGE_FILE_FORMAT") not in (None, "ascii", 1):
            return _unsaved("GAUGE_FILE_FORMAT", "the gauge files are not the ascii tables GaugeSolution reads")
        return None

    def record(self, key: str) -> list[Any]:
        """The one record of the keyword that makes the output ``key``; a run stating several reads none of them."""
        refused = self.unsaved(key)
        if refused is not None:
            raise refused
        keyword = _KEYWORD[_READ_BY[key]]
        records = self.stated(keyword)
        if len(records) != 1:
            raise _refuse("OUTPUT_AMBIGUOUS", f"{keyword} states {len(records)} records, so {key} has no one grid to be "
                          "read off; a read takes a run that states one.")
        return list(records[0])


def _flat(primitive: Primitive) -> None:
    if primitive.plane is not None:
        raise _refuse("PLANE_UNSTATED", f"plane={primitive.plane!r} names a layer and GeoClaw's grids are one layer.")


def _drawn(values: Any, wet: Any) -> Any:
    import numpy as np

    return np.where(wet, values, np.nan)


def _north_up(array: Any) -> Any:
    """An (x, y) array as raster rows: y runs south to north in the grid, north to south in the raster."""
    return array.T[::-1]


def _envelope(rasters: Any) -> dict[str, Any]:
    import numpy as np

    finite = np.asarray(rasters, dtype="float64")
    finite = finite[np.isfinite(finite)]
    return {"max": float(finite.max()), "min": float(finite.min()), "cells": int(finite.size)}


def _rasters(solved: Solved, row: Any, key: str, rasters: list[Any], bounds: Any, times: list[float]) -> Rasters:
    """The rasters as a read; none holding a number refuses, as the engine drew nothing for it."""
    import numpy as np

    if not any(np.isfinite(r).any() for r in rasters):
        raise OutputEmpty(f"not one cell of {key} is a number the engine called wet or reached.")
    return Rasters(name=row.name, units=row.unit, values=np.stack(rasters), bounds=bounds, times=tuple(times),
                   until=float(solved.until) if times else None,
                   measures={**_envelope(rasters), "frames": len(rasters)})


def _fgmax(solved: Solved, key: str, stage: _Stage) -> Any:
    from clawpack.geoclaw.fgmax_tools import FGmaxGrid

    fgno = int(solved.record(key)[0])
    stage.fetch("fgmax_grids.data", f"fgmax{fgno:04d}.txt")
    grid = FGmaxGrid()
    grid.read_fgmax_grids_data(fgno, data_file=str(stage.work / "fgmax_grids.data"))
    if grid.point_style != 2:
        raise _refuse("FGMAX_POINT_STYLE", f"FGMAX_FGMAX_GRIDS states point_style {grid.point_style}, and a raster is "
                      "placed on a uniform grid (2).")
    grid.read_output(fgno=fgno, outdir=str(stage.work), verbose=False, indexing="xy")
    return grid


def _fgout(solved: Solved, key: str, stage: _Stage) -> Any:
    from clawpack.geoclaw.fgout_tools import FGoutGrid

    fgno = int(solved.record(key)[0])
    stage.fetch("fgout_grids.data")
    grid = FGoutGrid(fgno, str(stage.work))
    grid.read_fgout_grids_data(fgno)
    grid.drytol = solved.dry_tolerance
    return grid


def _frame(grid: Any, stage: _Stage, number: int) -> Any:
    """Frame ``number`` (from 1) through the grid's own reader."""
    suffix = f"{number:04d}"
    stem = f"fgout{grid.fgno:04d}"
    stage.fetch(f"{stem}.q{suffix}", f"{stem}.t{suffix}", *([] if grid.output_format == "ascii" else [f"{stem}.b{suffix}"]))
    return grid.read_frame(number)


def _initial_wet(solved: Solved, grid: Any, stage: _Stage) -> Any:
    """The cells wet at frame 0 - the initial sea; a first frame not written at t = 0 is no initial sea."""
    first = _frame(grid, stage, 1)
    start = float(solved.stated("CLAW_T0") or 0.0)
    if first.t != start:
        raise _refuse("OVERLAND_UNANCHORED", f"the first fgout frame is written at t = {first.t:g} s, so no frame holds "
                      f"the initial sea; state FGOUT_FGOUT_GRIDS tstart {start:g}, the run's CLAW_T0.")
    return first.h > solved.dry_tolerance


def read_max_over_time(primitive: Primitive, solved: Solved) -> Rasters:
    """``max_over_time(name)``: an fgmax product - max depth, speed or surface, or the arrival time - on the fgmax grid.

    Depth, speed and surface are drawn where the maximum depth was wet; arrival time is Clawpack's own and stays
    nothing where no wave was flagged. MAX_OVERLAND_DEPTH also drops the initial sea and needs an fgout grid on the same nodes.
    """
    import numpy as np

    _flat(primitive)
    key, row = solved.output(primitive.variable, "max_over_time")
    with solved.staged() as stage:
        grid = _fgmax(solved, key, stage)
        h = np.ma.filled(grid.h.astype("float64"), np.nan)
        wet = np.isfinite(h) & (h > solved.dry_tolerance)
        if key == "ARRIVAL_TIME":
            field = np.ma.filled(grid.arrival_time.astype("float64"), np.nan)
        elif key == "MAX_SPEED":
            if not hasattr(grid, "s"):
                raise _refuse("OUTPUT_UNSAVED", "fgmax0001.txt carries no speed column; FGMAX_NUM_FGMAX_VAL is stated 2 "
                              "or 5 and the file was written under another.")
            field = _drawn(np.ma.filled(grid.s.astype("float64"), np.nan), wet)
        elif key == "MAX_SURFACE":
            field = _drawn(h + np.ma.filled(grid.B.astype("float64"), np.nan), wet)
        elif key == "MAX_OVERLAND_DEPTH":
            fgout = _fgout(solved, key, stage)
            if fgout.nx != grid.nx or fgout.ny != grid.ny or not (
                    np.allclose(fgout.x, grid.x, rtol=0, atol=_COINCIDE_DEG)
                    and np.allclose(fgout.y, grid.y, rtol=0, atol=_COINCIDE_DEG)):
                raise _refuse("OVERLAND_GRIDS", "the fgout grid is not on the fgmax grid's nodes, so its frame 0 says "
                              "nothing of the initial sea there; state FGOUT_FGOUT_GRIDS on FGMAX_FGMAX_GRIDS's nodes.")
            field = _drawn(h, wet & ~_initial_wet(solved, fgout, stage).T)
        else:
            field = _drawn(h, wet)
        dx, dy = (grid.x[-1] - grid.x[0]) / (grid.nx - 1), (grid.y[-1] - grid.y[0]) / (grid.ny - 1)
        bounds = (float(grid.x[0] - dx / 2), float(grid.y[0] - dy / 2),
                  float(grid.x[-1] + dx / 2), float(grid.y[-1] + dy / 2))
    return _rasters(solved, row, key, [field[::-1]], bounds, [])


def read_field(primitive: Primitive, solved: Solved) -> Rasters:
    """``field(name, t)``: an fgout variable - depth, surface or overland depth - at every frame, or the frame at ``t``.

    ``t`` is ``"every"``, a frame index counted from 0 (-1 the last), or an instant in seconds taking the nearest frame.
    A cell the engine called dry at a frame is nothing there.
    """
    import numpy as np

    _flat(primitive)
    key, row = solved.output(primitive.variable, "field")
    if primitive.t is None:
        raise _refuse("FIELD_TIME_UNSTATED", f"field({primitive.variable}) states no time; state t as 'every', a frame "
                      "index (-1 the last) or an instant in seconds.")
    rasters: list[Any] = []
    with solved.staged() as stage:
        grid = _fgout(solved, key, stage)
        times = np.asarray(grid.times, dtype="float64")
        if primitive.t == "every":
            numbers = list(range(1, times.size + 1))
        else:
            index = (int(primitive.t) if isinstance(primitive.t, int) and not isinstance(primitive.t, bool)
                     else int(np.argmin(np.abs(times - float(primitive.t)))))
            if not -times.size <= index < times.size:
                raise _refuse("FIELD_TIME_UNSTATED", f"frame {index} is no frame of this run; it wrote {times.size}.")
            numbers = [index % times.size + 1]
        sea = _initial_wet(solved, grid, stage) if key == "OVERLAND_DEPTH" else None
        for number in numbers:
            frame = _frame(grid, stage, number)
            h = np.asarray(frame.h, dtype="float64")
            wet = h > solved.dry_tolerance
            if key == "SURFACE":
                values = _drawn(np.asarray(frame.eta, dtype="float64"), wet)
            else:
                values = _drawn(h, wet if sea is None else wet & ~sea)
            rasters.append(_north_up(values))
        west, east, south, north = (float(v) for v in grid.extent_edges)
    instants = [float(times[n - 1]) for n in numbers]
    return _rasters(solved, row, key, rasters, (west, south, east, north), instants)


def _gauge(solved: Solved, at: Any) -> list[Any]:
    """The gauge record a read is at: the sole gauge, a gauge number, or a Point standing exactly on one."""
    gauges = solved.stated("GAUGE_GAUGES")
    if at is None:
        if len(gauges) != 1:
            raise _refuse("GAUGE_UNSTATED", f"the run states {len(gauges)} gauges ({', '.join(str(g[0]) for g in gauges)}); "
                          "state which one the series is read at.")
        return list(gauges[0])
    if isinstance(at, int) and not isinstance(at, bool):
        found = [g for g in gauges if int(g[0]) == at]
        if not found:
            raise _refuse("GAUGE_UNSTATED", f"gauge {at} is none of this run's ({', '.join(str(g[0]) for g in gauges)}).")
        return list(found[0])
    from pyproj import Geod

    from trid3nt_server.inputs import Point
    from trid3nt_server.inputs.point import PointNotViableError

    point = at if isinstance(at, Point) else Point(*at)
    for gauge in gauges:
        if abs(gauge[1] - point.lon) <= _COINCIDE_DEG and abs(gauge[2] - point.lat) <= _COINCIDE_DEG:
            return list(gauge)
    geod = Geod(ellps="WGS84")
    near = min(((geod.inv(point.lon, point.lat, g[1], g[2])[2], g) for g in gauges), key=lambda pair: pair[0])
    raise PointNotViableError(point, label="series point", missed="no gauge of this run, and a gauge reads only where it stands",
                              hint=(near[1][1], near[1][2], near[0]))


def read_series(primitive: Primitive, solved: Solved) -> Series:
    """``series(name, at)``: a gauge's surface elevation or depth at every instant it wrote.

    ``at`` is a gauge number, a Point on a gauge, or nothing where the run states one gauge.
    """
    import numpy as np
    from clawpack.pyclaw.gauges import GaugeSolution

    _flat(primitive)
    key, row = solved.output(primitive.variable, "series")
    refused = solved.unsaved(key)
    if refused is not None:
        raise refused
    gauge = _gauge(solved, primitive.at)
    number, lon, lat = int(gauge[0]), float(gauge[1]), float(gauge[2])
    with solved.staged() as stage:
        stage.fetch(f"gauge{number:05d}.txt")
        solution = GaugeSolution(number, path=str(stage.work))
    if solution.q.shape[0] < 4:
        raise _refuse("OUTPUT_UNSAVED", f"gauge {number} wrote {solution.q.shape[0]} variables and surface elevation is "
                      "the fourth; GAUGE_Q_OUT_FIELDS states fewer.")
    values = np.asarray(solution.q[3 if key == "GAUGE_SURFACE" else 0], dtype="float64")
    times = np.asarray(solution.t, dtype="float64")
    if not values.size:
        raise OutputEmpty(f"gauge {number} wrote no instant.")
    top, bottom = int(np.argmax(values)), int(np.argmin(values))
    return Series(name=row.name, units=row.unit, times=times, values=values, at=f"at gauge {number}", lon=lon, lat=lat,
                  measures={"max": float(values[top]), "t_max": float(times[top]),
                            "min": float(values[bottom]), "t_min": float(times[bottom]), "frames": int(times.size)})


PRIMITIVES = {"field": read_field, "max_over_time": read_max_over_time, "series": read_series}
