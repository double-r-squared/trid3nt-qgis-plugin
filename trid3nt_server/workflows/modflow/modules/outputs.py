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
from ..authoring.observations import observation_files
from ..authoring.stress import point_cell
from ..errors import ModflowError
from .ugrid import group

__all__ = ["PRIMITIVES", "Solved", "TRACKED", "filerecord", "group_stem", "read_extent", "read_field", "read_mass_balance",
           "read_max_over_time", "read_mesh", "read_observations", "read_series"]

# mf6 writes this magnitude for a cell that is dry (HDRY) or inactive (HNOFLO).
_NO_HEAD = 1.0e30

_BUDGET_RECORD = {"SATURATION": ("DATA-SAT", "NPF_SAVE_SATURATION"),
                  "SPECIFIC_DISCHARGE": ("DATA-SPDIS", "NPF_SAVE_SPECIFIC_DISCHARGE")}

# Each variable a model saves to its own binary file: the output control keyword that names the file, and the text the file is written under (None: the head file's own).
_SAVED = {"HEAD": ("OC_HEAD_FILERECORD", None),
          "CONCENTRATION": ("OC_CONCENTRATION_FILERECORD", "CONCENTRATION"),
          "TEMPERATURE": ("OC_TEMPERATURE_FILERECORD", "TEMPERATURE"),
          "COMPACTION": ("CSUB_COMPACTION_FILERECORD", "CSUB-COMPACTION"),
          "SUBSIDENCE": ("CSUB_ZDISPLACEMENT_FILERECORD", "CSUB-ZDISPLACE")}

# The outputs a PRT run reads off its CSV track file, each as the read that makes it and how it is published.
TRACKED = {"PATHLINES": ("pathlines", "layer"), "ENDPOINTS": ("endpoints", "layer"), "TRAVEL_TIME": ("travel_time", "chart")}

# The outputs a package alone writes: a run stating no keyword of it has no such row.
_PACKAGED = {"COMPACTION": "CSUB_", "SUBSIDENCE": "CSUB_"}

# The outputs counted in the model's own length unit.
_LENGTHS = ("HEAD", "COMPACTION", "SUBSIDENCE")


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
    ``filled`` overrides what the sheet states, for a coupled body whose slots its own dictionary filled.
    ``model`` is the name the model is called in the simulation, and ``grid`` the refined grid a refinement's model solves on.
    """

    def __init__(self, run: Mapping[str, Any], body: Any,
                 filled: Mapping[str, Any] | None = None, *, model: str | None = None, grid: Any = None) -> None:
        self.run = run
        self.body = body
        self.run_id = str(run["run_id"])
        self.utm_epsg = int(run["utm_epsg"])
        self.mesh = run["mesh"]
        self.sheet = run["sheet"]
        self._filled = filled
        self.model = model or str(body.MODULE)
        self._grid = grid

    @property
    def qualifier(self) -> str:
        """What a refinement's group and caption carry to tell its model from the carrier's."""
        return "" if self.model == str(self.body.MODULE) else f" ({self.model})"

    @property
    def tag(self) -> str:
        """The qualifier as a dataset group's and a file's name carries it."""
        return "" if self.model == str(self.body.MODULE) else f"_{self.model}"

    @cached_property
    def nodes(self) -> tuple[Any, Any, Any, Any]:
        if self._grid is not None:
            return self._grid.points, self._grid.cells, None, None
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
        if self._grid is not None:
            return len(self._grid.botm)
        return disv_dimensions(self.sheet, self.nodes[1])["nlay"]

    @cached_property
    def layering(self) -> tuple[Any, Any]:
        """The grid's top and per-layer bottoms, placed from the sheet's statements."""
        from ..authoring.mesh_files import _layering

        if self._grid is not None:
            return self._grid.top, self._grid.botm

        points, cells, bed, _ = self.nodes
        top, botm, _ = _layering(self.sheet, nlay=self.nlay,
                                 art=mesh_artifact(self.mesh), bed=bed,
                                 points=points, cells=cells)
        return top, botm

    def stated(self, identifier: str) -> Any:
        """What this read's own body states, else what the run's carrier deck states: the clock and units the
        simulation owns are one for both models, while a body's own keyword - its budget file - is its own."""
        row = None if self._filled is None else self._filled.get(identifier)
        if row is None:
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
        """A head, a compaction and a subsidence are in the model's length unit; any other output in the unit the run stated it in, else its row's."""
        if key in _LENGTHS:
            return str(self.stated("DISV_LENGTH_UNITS") or "")
        return str((self.run.get("published_units") or {}).get(key) or row.unit)

    def frames(self, token: Any, plane: int | None) -> tuple[Any, str, Any, Any, int]:
        """``(row, units, model times, values(nsteps, ncells), layer)``; a dry or inactive cell is NaN."""
        import numpy as np

        key, row = self.output(token)
        layer = self.layer(plane)
        ncpl = len(self.nodes[1])
        if key in _SAVED:
            times, values = self._saved_cells(key, layer)
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

    def carried(self, key: str) -> bool:
        """Whether the run has the output ``key`` at all: its package is written (a row of a package the deck never states is no row)."""
        prefix = _PACKAGED.get(key)
        return prefix is None or any(str(name).startswith(prefix) for name in (
            self.sheet.filled if self._filled is None else self._filled))

    def observed(self) -> list[tuple[str, str, str | None]]:
        """The OBS CSVs this body's observations write: ``(file, label, observation type)``."""
        return observation_files(self.sheet.filled if self._filled is None else self._filled, self.model)

    def unsaved(self, key: str) -> ModflowError | None:
        """Why the run's output control saved nothing of the output ``key``, or None where it saved it."""
        if key in TRACKED:
            if not filerecord(self.stated("OC_TRACKCSV_FILERECORD")):
                return _unsaved("OC_TRACKCSV_FILERECORD", f"the run saved no CSV track file to read the {key.lower()} from")
            return None
        if key in _SAVED:
            identifier = _SAVED[key][0]
            if not filerecord(self.stated(identifier)):
                return _unsaved(
                    identifier, f"the run saved no {key.lower()} file to read")
            return None
        text, keyword = _BUDGET_RECORD[key]
        if self.stated(keyword) is not True:
            return _unsaved(keyword, f"the budget file carries no {text} record")
        if not filerecord(self.stated("OC_BUDGET_FILERECORD")):
            return _unsaved("OC_BUDGET_FILERECORD", "the run saved no budget file to read")
        return None

    def _saved_cells(self, key: str, layer: int) -> tuple[Any, Any]:
        import flopy
        import numpy as np

        identifier, text = _SAVED[key]
        refused = self.unsaved(key)
        if refused is not None:
            raise refused
        path = self.local(filerecord(self.stated(identifier)))
        try:
            heads = (flopy.utils.HeadFile(path) if text is None
                     else flopy.utils.HeadFile(path, text=text))
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
        return str(self.stated("NAM_LIST") or f"{self.model}.lst")


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
    written = group(solved, name, list(zip(times, values)), units=units)
    return Frames(name=f"{row.name}{solved.qualifier}", units=units, values=values, file=written,
                  group=name, epsg=solved.utm_epsg,
                  reference_time=solved.run.get("reference_time"), frames=count,
                  measures=dict(measures))


def read_field(primitive: Primitive, solved: Solved) -> Read:
    """``field(name, t, plane)``: every saved time as one temporal group, or one instant (-1 the last) as a single-step group."""
    import numpy as np

    row, units, model_times, values, layer = solved.frames(primitive.variable, primitive.plane)
    times = solved.seconds(model_times)
    stem = group_stem(row, layer, solved.tag)
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
    return _frames(solved, row, units,
                   group_stem(row, layer, f"{solved.tag}_max"), [0.0],
                   peak[None, :], count=None,
                   measures=_envelope(model_times, values))


def _point(at: Any) -> Any:
    from trid3nt_server.inputs import Point

    if isinstance(at, Point):
        return at
    if isinstance(at, Observation):
        # A stated number stands nowhere: the miss is the reader's to say, so an
        # unmatched read composes nothing and journals instead of refusing the run.
        if at.lon is None or at.lat is None:
            raise OutputEmpty(
                f"the {at.site_id or at.site_name or 'observation'} was stated as "
                "a number, which stands at no site, so there is no site to read it at.")
        return Point(at.lon, at.lat, at.site_name or at.site_id)
    if isinstance(at, Mapping):
        return Point(float(at["lon"]), float(at["lat"]), at.get("name"))
    return Point(*at)


def read_series(primitive: Primitive, solved: Solved) -> Series:
    """``series(name, at, plane)``: the domain maximum per saved time, or the values of the cell containing a Point.

    At a data input's site the read takes the site's own coordinates, lands on the cell
    the run solves it in, and with no site or a site off the grid composes nothing.
    On a multi-layer run a well's published depth picks its layer; a well publishing
    no depth, or one reaching below the model base, is left out and said."""
    import numpy as np

    plane, point, cell = primitive.plane, None, None
    data_site = isinstance(primitive.at, Observation)
    if data_site:
        from trid3nt_server.inputs.point import as_utm, containing_face

        point = _point(primitive.at)
        grid = np.asarray(solved.nodes[0], dtype="float64")
        cell, away = containing_face(as_utm(point, solved.utm_epsg), x=grid[:, 0],
                                     y=grid[:, 1], faces=solved.nodes[1])
        if cell is None:
            label = primitive.at.site_id or point.name or "the site"
            raise OutputEmpty(
                f"the site {label} is outside the mesh, {away:.0f} m from its "
                "nearest cell.")
        if plane is None and solved.nlay > 1:
            plane = _depth_layer(primitive.at, solved, cell)
    row, units, model_times, values, layer = solved.frames(primitive.variable, plane)
    times = solved.seconds(model_times)
    if primitive.at is None:
        highs = np.where(np.isfinite(values).any(axis=1),
                         np.where(np.isfinite(values), values, -np.inf).max(axis=1), np.nan)
        return Series(name=row.name, units=units, times=times, values=highs,
                      at="the domain maximum", measures=_envelope(times, values))
    if not data_site:
        point = _point(primitive.at)
        cell = point_cell(point, label="series point", points=solved.nodes[0],
                          cells=solved.nodes[1], utm_epsg=solved.utm_epsg)
    column = values[:, cell]
    if not data_site and not np.isfinite(column).any():
        raise OutputEmpty(
            f"the cell holding the point is dry or inactive in layer {layer} at every saved time.")
    centre = solved.lonlat[list(solved.nodes[1][cell])].mean(axis=0)
    key = str(primitive.variable).strip().upper()
    measures = (_checked(primitive.at, solved, key=key, units=units, times=times,
                         column=column, cell=cell)
                if isinstance(primitive.at, Observation) and key in _CHECKED
                else _envelope(times, column[:, None]))
    return Series(name=row.name, units=units, times=times, values=column,
                  at=f"at {point.name or 'the point'}", lon=float(centre[0]),
                  lat=float(centre[1]), measures=measures)


def _depth_layer(observed: Observation, solved: Solved, cell: int) -> int:
    """The 1-based layer a well's published depth lands in at its cell; a depth
    reaching below the model base, or a well publishing none, is left out and said."""
    site = observed.site_id or observed.site_name or "an unnamed site"
    if observed.depth is None:
        raise OutputEmpty(
            f"the site {site} "
            f"publishes no well depth, and this run has {solved.nlay} layers; "
            "a multi-layer read needs the depth to pick the layer, so the site "
            "is left out rather than read on a layer nobody measured.")
    from trid3nt_server.inputs.temporal import TemporalUnitsError, convert_units

    length = str(solved.stated("DISV_LENGTH_UNITS") or "unknown").strip().lower()
    if length not in METRES:
        raise ModflowError(
            f"DISV_LENGTH_UNITS is {length!r}, so a well depth has no length to "
            f"place a layer against; state it as {', '.join(METRES)}.",
            error_code="MODFLOW_UNITS_UNSTATED")
    try:
        depth_m = convert_units(float(observed.depth),
                                str(observed.depth_units or ""), "m")
    except TemporalUnitsError as refused:
        raise OutputEmpty(
            f"the site {site}'s well depth {observed.depth} {observed.depth_units or ''} "
            f"is no length this model reads: {refused}") from refused
    depth_model = depth_m / METRES[length]
    top, botm = solved.layering
    surface = float(top) if isinstance(top, (int, float)) else float(top[cell])
    elevation = surface - depth_model
    for rank in range(solved.nlay):
        if elevation >= float(botm[rank][cell]):
            return rank + 1
    base = float(botm[-1][cell])
    raise OutputEmpty(
        f"the site {site}'s well depth {observed.depth} {observed.depth_units or ''} "
        f"reaches {elevation:.2f} {length}, below the model base {base:.2f} at its "
        "cell; the site is left out rather than read on a layer the model does "
        "not solve.")


def _uncomparable(why: str) -> ModflowError:
    return ModflowError(why, error_code="MODFLOW_OBSERVATION_UNCOMPARABLE")


#: The outputs a measured reading at a site is checked against.
_CHECKED = ("HEAD", "CONCENTRATION")


def _checked(observed: Observation, solved: Solved, *, key: str, units: str,
             times: Any, column: Any, cell: int) -> dict[str, Any]:
    """A measured reading against the solved value at its site's cell, in the SAME quantity and the run's own unit.

    A water level is checked as a depth below the land surface or an elevation on the run's frame, a concentration
    in the unit the run states; the saved time compared is the one the reading's window holds, and any other
    quantity, unit or frame refuses by name."""
    import numpy as np

    seen, units = (_level_seen(observed, solved) if key == "HEAD"
                   else (_concentration_seen(observed, units), units))
    step = _sampled_step(observed, solved, times)
    value = float(column[step])
    if not np.isfinite(value):
        raise _uncomparable(
            f"the site's cell is dry or inactive at t = {float(times[step]):g} s, "
            f"so it holds no {key.lower()} to check the reading against.")
    simulated = _level_simulated(observed, solved, value, cell) if key == "HEAD" else value
    compared = ({} if len(times) == 1 else {"compared_at": _saved_at(solved, times[step])})
    return {"site": observed.site_id or observed.site_name or "an unnamed site",
            "sampled": observed.sampled or "an undated moment",
            "quantity": observed.quantity or key.lower(), "units": units,
            "observed": round(seen, 3), "simulated": round(simulated, 3),
            "simulated_minus_observed": round(simulated - seen, 3), **compared}


def _level_seen(observed: Observation, solved: Solved) -> tuple[float, str]:
    """A measured water level in the model's length unit, and that unit."""
    from trid3nt_server.inputs.temporal import TemporalUnitsError, convert_units

    length = str(solved.stated("DISV_LENGTH_UNITS") or "unknown").strip().lower()
    if length not in METRES:
        raise ModflowError(
            f"DISV_LENGTH_UNITS is {length!r}, so a head has no length to check a "
            f"measured water level against; state it as {', '.join(METRES)}.",
            error_code="MODFLOW_UNITS_UNSTATED")
    try:
        return convert_units(observed.value, str(observed.units or ""), "m") / METRES[length], length
    except TemporalUnitsError as refused:
        raise _uncomparable(
            f"the observed water level is in {observed.units!r}, which is no "
            f"length the head can be read in: {refused}") from refused


def _level_simulated(observed: Observation, solved: Solved, head: float, cell: int) -> float:
    """The solved head as the quantity the well reports: a depth below the cell's top, or the head on the run's frame."""
    from trid3nt_server.inputs.vertical_datum import names_frame

    if observed.quantity == "water_table_depth":
        points, cells, bed, _ll = solved.nodes
        top = top_of(solved.sheet, nlay=solved.nlay, art=mesh_artifact(solved.mesh),
                     bed=bed, points=points, cells=cells)
        return (float(top) if isinstance(top, (int, float)) else float(top[cell])) - head
    if observed.quantity == "groundwater_head":
        frame = str(solved.run.get("vertical_frame") or "")
        if not (observed.datum and frame and names_frame(observed.datum, frame)):
            raise _uncomparable(
                f"the well reports an elevation on {observed.datum or 'no stated datum'} "
                f"and this run's heads are on {frame or 'no stated frame'}; a shift "
                "between the two frames is measured, never assumed, so state a "
                "reading on the run's frame or a depth below the land surface.")
        return head
    raise _uncomparable(
        f"the well reports {observed.quantity or 'a value that states no quantity'}, "
        "and a head is checked only against a water_table_depth below the "
        "land surface or a groundwater_head elevation.")


def _concentration_seen(observed: Observation, units: str) -> float:
    """A measured concentration in the unit the run states its own in; a run stating none has nothing to compare in."""
    from trid3nt_server.inputs.temporal import TemporalUnitsError, convert_units

    if not units:
        raise ModflowError(
            "this run states no concentration_units, so a measured concentration "
            "has no unit to be compared in; state the unit the run's "
            "concentrations are counted in.", error_code="MODFLOW_UNITS_UNSTATED")
    try:
        return convert_units(observed.value, str(observed.units or ""), units)
    except TemporalUnitsError as refused:
        raise _uncomparable(
            f"the measured concentration is in {observed.units!r}, which does not "
            f"convert to the run's {units!r}: {refused}") from refused


def _saved_at(solved: Solved, t: float) -> str:
    """A saved time as the UTC instant it stands for on the run's own clock."""
    from datetime import timedelta

    from trid3nt_server.tools.search.match import instant

    return (instant(solved.run.get("reference_time"))
            + timedelta(seconds=float(t))).isoformat()


def _sampled_step(observed: Observation, solved: Solved, times: Any) -> int:
    """The saved time a reading is checked at: the one a steady run saves, else of the saved times the window it was
    asked at holds, the one nearest its sample; with none in that window, the nearest saved time, said on the journal."""
    from datetime import timedelta

    from trid3nt_server.inputs.observation import _WINDOW_DAYS, _in_window
    from trid3nt_server.tools.search.match import instant

    if len(times) == 1:
        return 0
    opened, sampled = instant(solved.run.get("reference_time")), instant(observed.sampled)
    if opened is None or sampled is None:
        raise _uncomparable(
            f"the run saved {len(times)} times and the reading "
            + ("states no instant" if sampled is None else "has no run start to count it from")
            + ", so no saved time is the one it was measured at.")
    asked = instant(observed.asked) or sampled
    saved = [opened + timedelta(seconds=float(t)) for t in times]
    held = [i for i, at in enumerate(saved) if _in_window(at.isoformat(), asked.isoformat())]
    if not held:
        from trid3nt_server.inputs.run_notes import journal_note

        nearest = min(range(len(saved)), key=lambda i: abs(saved[i] - asked))
        journal_note(
            f"the reading is compared at {asked.isoformat()}, and the run saved no "
            f"time within {_WINDOW_DAYS:g} days before it or a day after, so it is "
            f"checked against the nearest saved time, {saved[nearest].isoformat()}, "
            f"{abs(saved[nearest] - asked).days} days away.")
        return nearest
    return min(held, key=lambda i: abs(saved[i] - sampled))


def read_observations(primitive: Primitive, solved: Solved) -> Series:
    """``observations(file)``: an OBS file's CSV as one chart, a line per observation; a dry or inactive value is left out of its line."""
    import csv

    import numpy as np

    file = str(primitive.variable)
    found = {name: (label, kind) for name, label, kind in solved.observed()}
    if file not in found:
        raise ModflowError(f"{file!r} is no OBS file this run states; it writes {sorted(found)}.",
                           error_code="MODFLOW_OUTPUT_UNKNOWN")
    label, kind = found[file]
    path = solved.local(file)
    try:
        with open(path, newline="") as handle:
            table = list(csv.reader(handle))
    finally:
        Path(path).unlink(missing_ok=True)
    if len(table) < 2:
        raise OutputEmpty(f"{file} holds no observed time.")
    times = solved.seconds([float(row[0]) for row in table[1:]])
    lines = []
    for rank, name in enumerate(table[0][1:], start=1):
        values = np.asarray([float(row[rank]) for row in table[1:]], dtype="float64")
        held = np.isfinite(values) & (np.abs(values) < _NO_HEAD)
        if held.any():
            lines.append((name.strip(), times[held], values[held]))
    if not lines:
        raise OutputEmpty(f"not one value {file} holds is a number.")
    every = np.concatenate([values for _name, _t, values in lines])
    (first, t, values), rest = lines[0], lines[1:]
    return Series(name=f"{Path(label).stem if kind is None else label} observations", units=_observed_units(solved, kind),
                  times=t, values=values, at="at the observation points", label=first,
                  lines=tuple(Line(label=name, x=x, values=y) for name, x, y in rest),
                  measures={"observations": len(lines), "max": float(every.max()), "min": float(every.min())})


def _observed_units(solved: Solved, kind: str | None) -> str:
    """A head or drawdown observation is in the model's length unit, a concentration in the unit the run states it in; any other has no unit this run states."""
    if kind in ("head", "drawdown"):
        return str(solved.stated("DISV_LENGTH_UNITS") or "")
    if kind == "concentration":
        return str((solved.run.get("published_units") or {}).get("CONCENTRATION") or "")
    return ""


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


PRIMITIVES = {"field": read_field, "series": read_series, "observations": read_observations,
              "max_over_time": read_max_over_time, "mass_balance": read_mass_balance,
              "mesh": read_mesh, "extent": read_extent}
