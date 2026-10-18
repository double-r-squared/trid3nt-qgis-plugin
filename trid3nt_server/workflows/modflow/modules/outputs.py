"""A module's outputs: the read of each primitive off a solved MODFLOW run.

Primitives are named from the module's variable vocabulary (``field``, ``series``, ``max_over_time``, ``mass_balance``,
``mesh``, ``extent``); the head and budget files the sheet's output control named and the model listing are opened by flopy.
Every mesh product is a dataset group of the run's one UGRID display file, valued per cell.
"""

from __future__ import annotations

from functools import cached_property
from pathlib import Path
from typing import Any, Mapping

from trid3nt_server.workflows.runtime.primitives import (
    Frames,
    Line,
    OutputEmpty,
    Primitive,
    Read,
    Series,
)

from trid3nt_server.inputs.observation import Observation

from ..authoring.accepted_mesh import mesh_artifact, mesh_nodes
from ..authoring.griddata import METRES, SECONDS
from ..authoring.mesh_files import disv_dimensions, top_of
from ..authoring.stress import point_cell
from ..errors import ModflowError
from .ugrid import group

__all__ = ["PRIMITIVES", "Solved", "filerecord", "group_stem", "read_extent", "read_field", "read_mass_balance",
           "read_max_over_time", "read_mesh", "read_series"]

# mf6 writes this magnitude for a cell that is dry (HDRY) or inactive (HNOFLO).
_NO_HEAD = 1.0e30

_BUDGET_RECORD = {"SATURATION": ("DATA-SAT", "NPF_SAVE_SATURATION"),
                  "SPECIFIC_DISCHARGE": ("DATA-SPDIS", "NPF_SAVE_SPECIFIC_DISCHARGE")}


def _unsaved(keyword: str, why: str) -> ModflowError:
    return ModflowError(
        f"{keyword} is stated by nothing, so {why}; state it and solve again.",
        error_code="MODFLOW_OUTPUT_UNSAVED")


def filerecord(value: Any) -> str | None:
    """The file name a FILERECORD value states: a string, or the first string in the nesting flopy takes."""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        for item in value:
            found = filerecord(item)
            if found:
                return found
    return None


class Solved:
    """One solved run's files, each opened where it is read: the head and budget files, the listing.

    ``run`` is the handle the solve step returned plus the accepted ``mesh`` and the filled ``sheet`` the run solved.
    """

    def __init__(self, run: Mapping[str, Any], body: Any) -> None:
        self.run = run
        self.body = body
        self.run_id = str(run["run_id"])
        self.utm_epsg = int(run["utm_epsg"])
        self.mesh = run["mesh"]
        self.sheet = run["sheet"]

    @cached_property
    def nodes(self) -> tuple[Any, Any, Any, Any]:
        return mesh_nodes(self.mesh)

    @cached_property
    def lonlat(self) -> Any:
        import numpy as np
        from pyproj import Transformer

        points = np.asarray(self.nodes[0], dtype="float64")
        lon, lat = Transformer.from_crs(self.utm_epsg, 4326, always_xy=True
                                        ).transform(points[:, 0], points[:, 1])
        return np.column_stack([lon, lat])

    @cached_property
    def bbox(self) -> tuple[float, float, float, float]:
        """The lon/lat box the solved mesh spans - where the camera flies."""
        ll = self.lonlat
        return (float(ll[:, 0].min()), float(ll[:, 1].min()),
                float(ll[:, 0].max()), float(ll[:, 1].max()))

    @cached_property
    def nlay(self) -> int:
        return disv_dimensions(self.sheet, self.nodes[1])["nlay"]

    def stated(self, identifier: str) -> Any:
        row = self.sheet.filled.get(identifier)
        return None if row is None else row.value

    def seconds(self, times: Any) -> Any:
        """Model times in seconds by the sheet's TDIS_TIME_UNITS, or the refusal that names it."""
        import numpy as np

        units = str(self.stated("TDIS_TIME_UNITS") or "unknown").strip().lower()
        if units not in SECONDS:
            raise ModflowError(
                f"TDIS_TIME_UNITS is {units!r}, so a model time has no length in "
                "seconds; state TDIS_TIME_UNITS as seconds, minutes, hours, days or years.",
                error_code="MODFLOW_CLOCK_UNSTATED")
        return np.asarray(times, dtype="float64") * SECONDS[units]

    def local(self, basename: str) -> str:
        from trid3nt_server.workflows.solver.solver import download_result

        return download_result(self.run_id, basename)

    def layer(self, plane: int | None) -> int:
        """The 1-based layer a field is read on; a multi-layer run states it."""
        if plane is None:
            if self.nlay == 1:
                return 1
            raise ModflowError(
                f"this run has {self.nlay} layers and the read states none; state "
                f"plane= from 1 to {self.nlay}.", error_code="MODFLOW_LAYER_UNSTATED")
        if isinstance(plane, bool) or not isinstance(plane, int) or not 1 <= plane <= self.nlay:
            raise ModflowError(
                f"plane {plane!r} is no layer of this run; it has {self.nlay} "
                f"layer(s), numbered from 1.", error_code="MODFLOW_LAYER_UNSTATED")
        return plane

    def output(self, token: Any) -> tuple[str, Any]:
        key = str(token).strip().upper()
        row = self.body.MODULE_OUTPUT.get(key)
        if row is None:
            raise ModflowError(
                f"{token!r} is no output of {self.body.MODULE}; the module writes "
                f"{sorted(self.body.MODULE_OUTPUT)}.", error_code="MODFLOW_OUTPUT_UNKNOWN")
        return key, row

    def units(self, key: str, row: Any) -> str:
        return str(self.stated("DISV_LENGTH_UNITS") or "") if key == "HEAD" else row.unit

    def frames(self, token: Any, plane: int | None) -> tuple[Any, str, Any, Any, int]:
        """``(row, units, model times, values(nsteps, ncells), layer)``; a dry or inactive cell is NaN."""
        import numpy as np

        key, row = self.output(token)
        layer = self.layer(plane)
        ncpl = len(self.nodes[1])
        if key == "HEAD":
            times, values = self._heads(layer)
        else:
            times, values = self._budget_cells(key, layer)
        values = np.where(np.abs(values) >= _NO_HEAD, np.nan, values)
        if values.shape != (len(times), ncpl):
            raise ModflowError(
                f"{key} came back {values.shape} for {len(times)} saved time(s) over "
                f"{ncpl} cells.", error_code="MODFLOW_OUTPUT_SHAPE")
        if not len(times) or not np.isfinite(values).any():
            raise OutputEmpty(f"not one value the run saved for {key} is a number.")
        return row, self.units(key, row), np.asarray(times, dtype="float64"), values, layer

    def unsaved(self, key: str) -> ModflowError | None:
        """Why the run's output control saved nothing of the output ``key``, or None where it saved it."""
        if key == "HEAD":
            if not filerecord(self.stated("OC_HEAD_FILERECORD")):
                return _unsaved("OC_HEAD_FILERECORD", "the run saved no head file to read")
            return None
        text, keyword = _BUDGET_RECORD[key]
        if self.stated(keyword) is not True:
            return _unsaved(keyword, f"the budget file carries no {text} record")
        if not filerecord(self.stated("OC_BUDGET_FILERECORD")):
            return _unsaved("OC_BUDGET_FILERECORD", "the run saved no budget file to read")
        return None

    def _heads(self, layer: int) -> tuple[Any, Any]:
        import flopy
        import numpy as np

        refused = self.unsaved("HEAD")
        if refused is not None:
            raise refused
        path = self.local(filerecord(self.stated("OC_HEAD_FILERECORD")))
        try:
            heads = flopy.utils.HeadFile(path)
            try:
                times = [float(t) for t in heads.get_times()]
                values = [np.asarray(heads.get_data(totim=t))[layer - 1].ravel() for t in times]
            finally:
                heads.file.close()
        finally:
            Path(path).unlink(missing_ok=True)
        return times, np.asarray(values, dtype="float64")

    def _budget_cells(self, key: str, layer: int) -> tuple[Any, Any]:
        import flopy
        import numpy as np

        text, _keyword = _BUDGET_RECORD[key]
        refused = self.unsaved(key)
        if refused is not None:
            raise refused
        ncpl = len(self.nodes[1])
        path = self.local(filerecord(self.stated("OC_BUDGET_FILERECORD")))
        try:
            budget = flopy.utils.CellBudgetFile(path)
            try:
                times = [float(t) for t in budget.get_times()]
                steps = []
                for t in times:
                    record = budget.get_data(text=text, totim=t)[0]
                    cells = np.full(self.nlay * ncpl, np.nan)
                    cells[record["node"] - 1] = (
                        record["sat"] if key == "SATURATION" else
                        np.sqrt(record["qx"] ** 2 + record["qy"] ** 2 + record["qz"] ** 2))
                    steps.append(cells[(layer - 1) * ncpl:layer * ncpl])
            finally:
                budget.file.close()
        finally:
            Path(path).unlink(missing_ok=True)
        return times, np.asarray(steps, dtype="float64")

    @cached_property
    def listing_name(self) -> str:
        return str(self.stated("NAM_LIST") or f"{self.body.MODULE}.lst")


def _envelope(times: Any, values: Any) -> dict[str, Any]:
    """The extremes of ``values(nsteps, ncells)`` over the cells that hold a number, with their instants."""
    import numpy as np

    high = np.where(np.isfinite(values), values, -np.inf).max(axis=1)
    low = np.where(np.isfinite(values), values, np.inf).min(axis=1)
    live = np.isfinite(values).any(axis=1)
    top, bottom = int(np.argmax(np.where(live, high, -np.inf))), int(np.argmin(np.where(live, low, np.inf)))
    return {"max": float(high[top]), "t_max": float(times[top]),
            "min": float(low[bottom]), "t_min": float(times[bottom]),
            "final": float(np.nanmax(values[-1])) if live[-1] else None,
            "frames": int(len(times))}


def group_stem(row: Any, layer: int, qualifier: str = "") -> str:
    """The dataset group a read of the output ``row`` on ``layer`` is written under."""
    return f"{row.name.strip().replace(' ', '_')}{qualifier}_layer_{layer}"


def _frames(solved: Solved, row: Any, units: str, name: str,
            times: Any, values: Any, *, count: int | None, measures: Mapping[str, Any],
            ) -> Frames:
    written = group(solved, name, list(zip(times, values)))
    return Frames(name=row.name, units=units, values=values, file=written,
                  group=name, epsg=solved.utm_epsg,
                  reference_time=solved.run.get("reference_time"), frames=count,
                  measures=dict(measures))


def read_field(primitive: Primitive, solved: Solved) -> Read:
    """``field(name, t, plane)``: every saved time as one temporal group, or one instant (-1 the last) as a single-step group."""
    import numpy as np

    row, units, model_times, values, layer = solved.frames(primitive.variable, primitive.plane)
    times = solved.seconds(model_times)
    stem = group_stem(row, layer)
    if primitive.t == "every":
        return _frames(solved, row, units, stem, times, values,
                       count=int(times.size), measures=_envelope(times, values))
    if primitive.t is None:
        raise ModflowError(
            f"field({primitive.variable}) states no time; state t as 'every', a "
            "step index (-1 the last) or an instant in seconds.",
            error_code="MODFLOW_FIELD_TIME_UNSTATED")
    index = (int(primitive.t) if isinstance(primitive.t, int)
             else int(np.argmin(np.abs(times - float(primitive.t)))))
    frame = values[index]
    inside = frame[np.isfinite(frame)]
    if not inside.size:
        raise OutputEmpty(f"{primitive.variable} holds no number at t = {times[index]:g} s.")
    t = float(times[index])
    return _frames(
        solved, row, units, f"{stem}_at_t_{int(round(t))}_s", [t], frame[None, :],
        count=None,
        measures={"max": float(inside.max()), "min": float(inside.min()),
                  "mean": float(inside.mean()), "spread": float(inside.max() - inside.min()),
                  "cells": int(inside.size), "t": t, "frames": int(times.size)})


def read_max_over_time(primitive: Primitive, solved: Solved) -> Read:
    """``max_over_time(name, plane)``: the highest value every cell held over the saved times, one single-step group."""
    import numpy as np

    row, units, model_times, values, layer = solved.frames(primitive.variable, primitive.plane)
    held = np.isfinite(values)
    peak = np.where(held.any(axis=0),
                    np.where(held, values, -np.inf).max(axis=0), np.nan)
    return _frames(solved, row, units, group_stem(row, layer, "_max"), [0.0],
                   peak[None, :], count=None,
                   measures=_envelope(model_times, values))


def _point(at: Any) -> Any:
    from trid3nt_server.inputs import Point

    if isinstance(at, Point):
        return at
    if isinstance(at, Observation):
        if at.lon is None or at.lat is None:
            raise ModflowError(
                "the observation was stated as a number, which stands at no "
                "site, so no cell holds the value it is checked against; state "
                "it as a point layer of the well that reported it.",
                error_code="MODFLOW_OBSERVATION_UNPLACED")
        return Point(at.lon, at.lat, at.site_name or at.site_id)
    if isinstance(at, Mapping):
        return Point(float(at["lon"]), float(at["lat"]), at.get("name"))
    return Point(*at)


def read_series(primitive: Primitive, solved: Solved) -> Series:
    """``series(name, at, plane)``: the domain maximum per saved time, or the values of the cell containing a Point."""
    import numpy as np

    row, units, model_times, values, layer = solved.frames(primitive.variable, primitive.plane)
    times = solved.seconds(model_times)
    if primitive.at is None:
        highs = np.where(np.isfinite(values).any(axis=1),
                         np.where(np.isfinite(values), values, -np.inf).max(axis=1), np.nan)
        return Series(name=row.name, units=units, times=times, values=highs,
                      at="the domain maximum", measures=_envelope(times, values))
    point = _point(primitive.at)
    points, cells, _bed, _ll = solved.nodes
    cell = point_cell(point, label="series point", points=points, cells=cells,
                      utm_epsg=solved.utm_epsg)
    column = values[:, cell]
    if not np.isfinite(column).any():
        raise OutputEmpty(
            f"the cell holding the point is dry or inactive in layer {layer} at every saved time.")
    centre = solved.lonlat[list(solved.nodes[1][cell])].mean(axis=0)
    measures = (_checked(primitive.at, solved, times=times, column=column, cell=cell)
                if isinstance(primitive.at, Observation) and str(primitive.variable).upper() == "HEAD"
                else _envelope(times, column[:, None]))
    return Series(name=row.name, units=units, times=times, values=column,
                  at=f"at {point.name or 'the point'}", lon=float(centre[0]),
                  lat=float(centre[1]), measures=measures)


def _uncomparable(why: str) -> ModflowError:
    return ModflowError(why, error_code="MODFLOW_OBSERVATION_UNCOMPARABLE")


def _checked(observed: Observation, solved: Solved, *, times: Any, column: Any,
             cell: int) -> dict[str, Any]:
    """A measured water level against the solved one at its well's cell, in the SAME quantity and the model's length unit.

    A depth below the land surface is checked against the cell's top less its head, an elevation against the head
    on the run's own vertical frame; a reading of any other quantity, unit or frame refuses by name."""
    import numpy as np

    from trid3nt_server.inputs.temporal import TemporalUnitsError, convert_units
    from trid3nt_server.inputs.vertical_datum import names_frame

    length = str(solved.stated("DISV_LENGTH_UNITS") or "unknown").strip().lower()
    if length not in METRES:
        raise ModflowError(
            f"DISV_LENGTH_UNITS is {length!r}, so a head has no length to check a "
            f"measured water level against; state it as {', '.join(METRES)}.",
            error_code="MODFLOW_UNITS_UNSTATED")
    try:
        seen = convert_units(observed.value, str(observed.units or ""), "m") / METRES[length]
    except TemporalUnitsError as refused:
        raise _uncomparable(
            f"the observed water level is in {observed.units!r}, which is no "
            f"length the head can be read in: {refused}") from refused
    step = _sampled_step(observed, solved, times)
    head = float(column[step])
    if not np.isfinite(head):
        raise _uncomparable(
            f"the well's cell is dry or inactive at t = {float(times[step]):g} s, "
            "so it holds no water table to check the reading against.")
    if observed.quantity == "water_table_depth":
        points, cells, bed, _ll = solved.nodes
        top = top_of(solved.sheet, nlay=solved.nlay, art=mesh_artifact(solved.mesh),
                     bed=bed, points=points, cells=cells)
        simulated = (float(top) if isinstance(top, (int, float)) else float(top[cell])) - head
    elif observed.quantity == "groundwater_head":
        frame = str(solved.run.get("vertical_frame") or "")
        if not (observed.datum and frame and names_frame(observed.datum, frame)):
            raise _uncomparable(
                f"the well reports an elevation on {observed.datum or 'no stated datum'} "
                f"and this run's heads are on {frame or 'no stated frame'}; a shift "
                "between the two frames is measured, never assumed, so state a "
                "reading on the run's frame or a depth below the land surface.")
        simulated = head
    else:
        raise _uncomparable(
            f"the well reports {observed.quantity or 'a value that states no quantity'}, "
            "and a head is checked only against a water_table_depth below the "
            "land surface or a groundwater_head elevation.")
    return {"site": observed.site_id or observed.site_name or "an unnamed site",
            "sampled": observed.sampled or "an undated moment",
            "quantity": observed.quantity, "units": length,
            "observed": round(seen, 3), "simulated": round(simulated, 3),
            "simulated_minus_observed": round(simulated - seen, 3)}


def _sampled_step(observed: Observation, solved: Solved, times: Any) -> int:
    """The saved time a reading is checked at: the one a steady run saves, else the one nearest the instant it was sampled."""
    import numpy as np

    from trid3nt_server.tools.search.match import instant

    if len(times) == 1:
        return 0
    opened, sampled = instant(solved.run.get("reference_time")), instant(observed.sampled)
    if opened is None or sampled is None:
        raise _uncomparable(
            f"the run saved {len(times)} times and the reading "
            + ("states no instant" if sampled is None else "has no run start to count it from")
            + ", so no saved time is the one it was measured at.")
    return int(np.argmin(np.abs(np.asarray(times) - (sampled - opened).total_seconds())))


def read_mass_balance(primitive: Primitive, solved: Solved) -> Series:
    """``mass_balance()``: the percent discrepancy the model listing prints, per listed step, with one net line per budget term."""
    import flopy
    import numpy as np

    path = solved.local(solved.listing_name)
    try:
        incremental, _cumulative = flopy.utils.Mf6ListBudget(path).get_dataframes(
            start_datetime=None)
    finally:
        Path(path).unlink(missing_ok=True)
    if not len(incremental):
        raise OutputEmpty(f"{solved.listing_name} prints no volumetric budget.")
    times = solved.seconds(incremental.index.to_numpy())
    columns = set(incremental.columns)
    terms = sorted({c.rsplit("_", 1)[0] for c in columns if c.endswith(("_IN", "_OUT"))}
                   - {"TOTAL"})
    zero = np.zeros(len(incremental))

    def side(term: str, end: str) -> Any:
        column = f"{term}_{end}"
        return incremental[column].to_numpy() if column in columns else zero

    discrepancy = incremental["PERCENT_DISCREPANCY"].to_numpy(dtype="float64")
    return Series(
        name="budget", units="%", times=times, values=discrepancy, at="the whole model",
        lines=tuple(Line(label=term, x=times, values=side(term, "IN") - side(term, "OUT"))
                    for term in terms),
        measures={"percent_discrepancy": float(discrepancy[-1]),
                  "total_in": float(incremental["TOTAL_IN"].iloc[-1]),
                  "total_out": float(incremental["TOTAL_OUT"].iloc[-1]),
                  "steps": int(len(incremental))})


def read_mesh(primitive: Primitive, solved: Solved) -> Read:
    """``mesh()``: the mesh the run solved on - its counts, layers and zone."""
    points, cells, _bed, _ll = solved.nodes
    return Read(measures={"nodes": int(len(points)), "elements": int(len(cells)),
                          "layers": solved.nlay, "epsg": solved.utm_epsg})


def read_extent(primitive: Primitive, solved: Solved) -> Read:
    """``extent()``: the domain's lon/lat bounds and the area its cells cover."""
    import numpy as np

    points, cells, _bed, _ll = solved.nodes
    xy = np.asarray(points, dtype="float64")
    area = 0.0
    for ring in cells:
        x, y = xy[list(ring), 0], xy[list(ring), 1]
        area += abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))) / 2.0
    return Read(measures={"bbox": list(solved.bbox), "area_km2": round(area / 1.0e6, 4)})


PRIMITIVES = {"field": read_field, "series": read_series,
              "max_over_time": read_max_over_time, "mass_balance": read_mass_balance,
              "mesh": read_mesh, "extent": read_extent}
