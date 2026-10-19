"""One GRIDDATA statement as the array the package constructor takes.

A constant passes, one number per layer fits the DFN's own shape, and a string
names a run input whose raster is sampled at the cells - the fill's existing
"string names an input" rule, measured against the accepted mesh. A raster is read
onto the model's own frame: the unit it publishes is converted onto the sheet's
length and time units, and one that publishes none refuses. What the shape cannot
take refuses by name here rather than as an engine error inside mf6.
"""

from __future__ import annotations

import tempfile
from typing import Any, Mapping

from trid3nt_server.inputs.temporal import TemporalUnitsError, convert_units
from trid3nt_server.workflows.runtime.module import Slot

from ..errors import ModflowError

__all__ = ["METRES", "SECONDS", "frame_of", "needs_top", "place", "sample"]

# mf6 reads a year as 365.25 days.
SECONDS = {"seconds": 1.0, "minutes": 60.0, "hours": 3600.0, "days": 86400.0,
           "years": 31557600.0}

# mf6's length words, as the metres one of them is.
METRES = {"meters": 1.0, "feet": 0.3048, "centimeters": 0.01}

# The dimension of each keyword a template samples from a raster; any other keyword
# has none stated and refuses.
_KEYWORD_DIMENSION = {"NPF_K": "L/T", "NPF_K33": "L/T", "IC_STRT": "L",
                      "DISV_TOP": "L", "DISV_BOTM": "L", "STO_SY": "1",
                      "STO_SS": "1/L"}

# The SI unit each dimension is converted through, in the declared unit table.
_SI = {"L": "m", "L/T": "m/s", "L2/T": "m2/s", "L3/T": "m3/s"}

# What a quotient of cells is over: the dimension it yields -> numerator and denominator.
_QUOTIENT = {"L/T": ("L2/T", "L")}


def frame_of(sheet: Any) -> tuple[Any, Any]:
    """The sheet's stated ``(DISV_LENGTH_UNITS, TDIS_TIME_UNITS)`` - the frame a raster is read onto."""
    return tuple(None if sheet.filled.get(key) is None else sheet.filled[key].value
                 for key in ("DISV_LENGTH_UNITS", "TDIS_TIME_UNITS"))


def needs_top(value: Any) -> bool:
    """Whether this value is a cells statement lying below the top."""
    return isinstance(value, Mapping) and value.get("cells") == "below_top"


def place(value: Any, *, slot: Slot, nlay: int, run: Mapping[str, Any],
          points: Any = None, cells: Any = None,
          utm_epsg: int | None = None, frame: Any = None,
          top: Any = None) -> Any:
    """``value`` as this ARRAY keyword's data: a constant, per-layer rows, or sampled cells.

    ``points``/``cells`` are the accepted mesh's nodes and connectivity - needed only
    where the value names a raster or carries one number per cell; ``nlay`` is the
    sheet's layer count a layered value is checked against; ``frame`` is
    ``frame_of(sheet)`` and ``top`` the per-cell top a below-the-top statement lies under."""
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, Mapping):
        return _expression(value, slot=slot, nlay=nlay, points=points, cells=cells,
                           utm_epsg=utm_epsg, frame=frame, top=top)
    if isinstance(value, str):
        with tempfile.TemporaryDirectory(prefix="trid3nt_griddata_") as work:
            sampled = _sample(value, slot=slot, run=run, scratch=work,
                              points=points, cells=cells, utm_epsg=utm_epsg,
                              frame=frame)
        return [list(sampled) for _ in range(nlay)] if slot.layered else sampled
    if isinstance(value, (list, tuple)):
        return _sequence(list(value), slot=slot, nlay=nlay, run=run,
                         points=points, cells=cells, utm_epsg=utm_epsg,
                         frame=frame)
    raise ModflowError(
        f"{slot.keyword} states {value!r}; its value is a constant, one value "
        "per layer, or the name of a run input to sample at the cells.",
        error_code="MODFLOW_GRIDDATA_SHAPE")


def _sequence(items: list[Any], *, slot: Slot, nlay: int,
              run: Mapping[str, Any], points: Any, cells: Any,
              utm_epsg: int | None, frame: Any) -> Any:
    if not items:
        raise ModflowError(
            f"{slot.keyword} states an empty list; state a number for every "
            "layer or cell, or one input name to sample.",
            error_code="MODFLOW_GRIDDATA_SHAPE")
    if all(isinstance(item, str) for item in items):
        if not slot.layered or len(items) != nlay:
            raise _shape_refusal(slot, nlay, cells)
        with tempfile.TemporaryDirectory(prefix="trid3nt_griddata_") as work:
            return [_sample(name, slot=slot, run=run, scratch=work,
                            points=points, cells=cells, utm_epsg=utm_epsg,
                            frame=frame)
                    for name in items]
    if all(isinstance(item, (list, tuple)) for item in items):
        if not slot.layered or len(items) != nlay:
            raise _shape_refusal(slot, nlay, cells)
        rows = [list(row) for row in items]
        _numbers_only(slot, rows)
        _rows_fit_cells(slot, rows, cells)
        return rows
    if not all(isinstance(item, (int, float)) for item in items):
        raise ModflowError(
            f"{slot.keyword} states {items!r}; its list is numbers or, on a "
            "layered array, one input name per layer.",
            error_code="MODFLOW_GRIDDATA_SHAPE")
    if slot.layered:
        if len(items) == nlay:
            return items
        raise _shape_refusal(slot, nlay, cells)
    _fits_cells(slot, items, cells)
    return items


def _sample(name: str, *, slot: Slot, run: Mapping[str, Any], scratch: str,
            points: Any, cells: Any, utm_epsg: int | None,
            frame: Any) -> list[float]:
    """One input's raster read at every cell centre -> the cell values."""
    if name not in run:
        raise ModflowError(
            f"{slot.keyword} names {name!r}, which no input of this run is "
            "called; state an input of this run or a number.",
            error_code="MODFLOW_GRIDDATA_NO_INPUT")
    if points is None or cells is None:
        raise ModflowError(
            f"{slot.keyword} names {name!r} to sample at the cells, and this "
            "run has no accepted mesh to place the cells at.",
            error_code="MODFLOW_GRIDDATA_NO_MESH")
    return sample(run[name], label=slot.keyword, name=name, scratch=scratch,
                  points=points, cells=cells, utm_epsg=utm_epsg,
                  dimension=_dimension(slot), frame=frame)


def _dimension(slot: Slot) -> str:
    try:
        return _KEYWORD_DIMENSION[slot.identifier]
    except KeyError:
        raise ModflowError(
            f"{slot.identifier} is read from a raster, and no dimension is "
            "stated for it; the keywords a raster fills are "
            f"{', '.join(sorted(_KEYWORD_DIMENSION))}.",
            error_code="MODFLOW_UNITS_UNSTATED") from None


def sample(artifact: Any, *, label: str, name: str, scratch: str, points: Any,
           cells: Any, utm_epsg: int | None, dimension: str,
           frame: Any) -> list[float]:
    """A raster artifact read at the centre of every given cell, onto the model's frame; a bed reads its own surface.

    ``dimension`` is L, L/T, L2/T, L3/T, 1/L or 1 (a pure number); the artifact's own
    unit is converted onto the ``frame`` of (length, time) words, and one stating
    none, or a frame lacking what its dimension needs, refuses."""
    from trid3nt_server.inputs.bed import Bed
    from trid3nt_server.tools.derive._raster_layers import read, staged

    if isinstance(artifact, Bed):
        if artifact.is_depth:
            # A bed stated as a depth is a flat surface that many metres below the run's own zero.
            return [-float(artifact.depth_m) * _factor(
                {"units": "m"}, dimension=dimension, frame=frame, label=label,
                name=name)] * len(cells)
        artifact = artifact.source
    factor = _factor(artifact, dimension=dimension, frame=frame, label=label, name=name)
    path = staged(artifact, role=name, scratch=scratch)
    values, _provenance, crs, transform = read(path)
    xs, ys = _centres(points, cells)
    if crs is not None and utm_epsg:
        from rasterio.warp import transform as warp

        xs, ys = warp(f"EPSG:{int(utm_epsg)}", crs, xs, ys)
    found = _at(values, transform=transform, xs=xs, ys=ys, label=label, name=name)
    return found if factor == 1.0 else [item * factor for item in found]


def _factor(artifact: Any, *, dimension: str, frame: Any, label: str, name: str) -> float:
    """What the artifact's published unit is multiplied by to read on the model's frame."""
    if dimension == "1":
        return 1.0
    unit = (artifact.get("units") if isinstance(artifact, Mapping)
            else getattr(artifact, "units", None))
    if not unit or not str(unit).strip():
        raise ModflowError(
            f"{label} reads {name!r}, which publishes no unit; a raster is "
            f"converted onto the model's {dimension} and one stating none is "
            "never assumed.", error_code="MODFLOW_UNITS_UNSTATED")
    if dimension not in _SI and dimension != "1/L":
        raise ModflowError(
            f"{label} states a raster for a {dimension} quantity, and the model's "
            "frame carries no unit for it to convert onto; state a number in the "
            "units this model declares.", error_code="MODFLOW_UNITS_UNSTATED")
    length, time = frame if frame is not None else (None, None)
    metres = _stated(length, METRES, "DISV_LENGTH_UNITS", label, name)
    seconds = (_stated(time, SECONDS, "TDIS_TIME_UNITS", label, name)
               if "T" in dimension else 1.0)
    try:
        if dimension == "1/L":
            if not str(unit).strip().startswith("1/"):
                raise TemporalUnitsError(
                    f"{unit!r} is no per-length unit ('1/m', '1/ft'), so it "
                    "cannot be read as a 1/L quantity.")
            return metres / convert_units(1.0, str(unit).strip()[2:], "m")
        si = convert_units(1.0, str(unit), _SI[dimension])
    except TemporalUnitsError as refused:
        raise ModflowError(
            f"{label} reads {name!r} in {unit!r} for a {dimension} quantity: "
            f"{refused}", error_code="MODFLOW_UNITS_UNSTATED") from refused
    exponent = int(dimension[1]) if dimension[1:2].isdigit() else 1
    return si * seconds / metres ** exponent


def _stated(word: Any, table: Mapping[str, float], keyword: str, label: str,
            name: str) -> float:
    found = str(word or "unknown").strip().lower()
    if found not in table:
        raise ModflowError(
            f"{label} reads {name!r} onto the model's frame, and {keyword} is "
            f"{found!r}; state it as {', '.join(table)}.",
            error_code="MODFLOW_UNITS_UNSTATED")
    return table[found]


def _expression(value: Mapping[str, Any], *, slot: Slot, nlay: int, points: Any,
                cells: Any, utm_epsg: int | None, frame: Any, top: Any) -> Any:
    """A cells statement - a raster, a quotient of two, or a raster below the top - per cell."""
    if points is None or cells is None:
        raise ModflowError(
            f"{slot.identifier} states cells to evaluate, and this run has no "
            "accepted mesh to place the cells at.",
            error_code="MODFLOW_GRIDDATA_NO_MESH")
    operation, of = value.get("cells"), value.get("of")
    dimension = _dimension(slot)

    def read(artifact: Any, kind: str) -> list[float]:
        if isinstance(artifact, (int, float)) and not isinstance(artifact, bool):
            # A stated number is already in the model's own units: it holds at every cell.
            return [float(artifact)] * len(cells)
        with tempfile.TemporaryDirectory(prefix="trid3nt_griddata_") as work:
            return sample(artifact, label=slot.identifier,
                          name=_name_of(artifact), scratch=work, points=points,
                          cells=cells, utm_epsg=utm_epsg, dimension=kind,
                          frame=frame)

    if operation == "raster":
        found = read(of, dimension)
    elif operation == "quotient" and dimension in _QUOTIENT and \
            isinstance(of, (list, tuple)) and len(of) == 2:
        over, under = _QUOTIENT[dimension]
        numerator, denominator = read(of[0], over), read(of[1], under)
        _thickness(slot, denominator)
        found = [a / b for a, b in zip(numerator, denominator)]
    elif operation == "below_top" and dimension == "L":
        # A list of depths lies at their sum: a water-table depth, then the saturated thickness under it.
        parts = [read(item, "L") for item in (of if isinstance(of, (list, tuple)) else [of])]
        if slot.identifier == "DISV_BOTM":
            _thickness(slot, parts[-1])
        found = _below(slot, [sum(column) for column in zip(*parts)], top,
                       nlay=nlay, cells=cells)
    else:
        raise ModflowError(
            f"{slot.identifier} states {dict(value)!r}; a cells statement is "
            "'raster' (of a raster), 'quotient' (of a numerator and a "
            "denominator, on a conductivity) or 'below_top' (of a raster or a list "
            "of rasters whose sum is the depth, on a length).", error_code="MODFLOW_GRIDDATA_SHAPE")
    return [list(found) for _ in range(nlay)] if slot.layered else found


def _below(slot: Slot, depth: list[float], top: Any, *, nlay: int, cells: Any) -> list[float]:
    if nlay != 1:
        raise ModflowError(
            f"{slot.identifier} states cells below the top, which is one "
            f"layer's statement, and the grid has {nlay} layers.",
            error_code="MODFLOW_GRIDDATA_SHAPE")
    if top is None:
        raise ModflowError(
            f"{slot.identifier} states cells below the top, and this run has "
            "no top to lie below.", error_code="MODFLOW_LAYERING_UNSTATED")
    tops = [float(top)] * len(cells) if isinstance(top, (int, float)) else list(top)
    if len(tops) != len(cells):
        raise ModflowError(
            f"{slot.identifier} lies below a top of {len(tops)} value(s) and "
            f"the accepted mesh has {len(cells)} cell(s).",
            error_code="MODFLOW_GRIDDATA_SHAPE")
    return [t - d for t, d in zip(tops, depth)]


def _thickness(slot: Slot, thickness: list[float]) -> None:
    flat = sum(1 for item in thickness if item <= 0.0)
    if flat:
        raise ModflowError(
            f"{slot.identifier} reads a thickness that is not above zero at "
            f"{flat} of {len(thickness)} cell(s); an aquifer of no thickness "
            "has no bottom or conductivity to state.",
            error_code="MODFLOW_AQUIFER_THICKNESS")


def _name_of(artifact: Any) -> str:
    name = (artifact.get("name") if isinstance(artifact, Mapping)
            else getattr(artifact, "name", None))
    return str(name or "").strip() or "input"


def _at(values: Any, *, transform: Any, xs: list[float], ys: list[float],
        label: str, name: str) -> list[float]:
    """The raster's value at each centre; a cell outside it or in its nodata refuses."""
    import numpy as np
    from rasterio.transform import rowcol

    from trid3nt_server.tools.derive._raster_layers import NODATA

    rows, cols = rowcol(transform, xs, ys)
    rows = np.asarray(rows, dtype="int64")
    cols = np.asarray(cols, dtype="int64")
    inside = ((rows >= 0) & (rows < values.shape[0])
              & (cols >= 0) & (cols < values.shape[1]))
    sampled = np.where(
        inside, values[np.clip(rows, 0, values.shape[0] - 1),
                       np.clip(cols, 0, values.shape[1] - 1)], NODATA)
    unmeasured = int(np.count_nonzero(
        ~np.isfinite(np.asarray(sampled, dtype="float64"))))
    if unmeasured:
        raise ModflowError(
            f"{label} reads {name!r} at {unmeasured} of {len(xs)} "
            "cell(s) where the raster measures nothing - outside its extent or "
            "its nodata; a cell solved on a guessed value refuses here by name.",
            error_code="MODFLOW_GRIDDATA_UNMEASURED")
    return [float(item) for item in sampled]


def _centres(points: Any, cells: Any) -> tuple[list[float], list[float]]:
    """Every cell's centre - the mean of its own vertices, over any vertex count."""
    xs: list[float] = []
    ys: list[float] = []
    for cell in cells:
        xs.append(float(sum(float(points[v][0]) for v in cell) / len(cell)))
        ys.append(float(sum(float(points[v][1]) for v in cell) / len(cell)))
    return xs, ys


def _numbers_only(slot: Slot, rows: list[list[Any]]) -> None:
    for row in rows:
        if not all(isinstance(item, (int, float)) for item in row):
            raise ModflowError(
                f"{slot.keyword} states {rows!r}; a layered row of cells is "
                "numbers only.",
                error_code="MODFLOW_GRIDDATA_SHAPE")


def _fits_cells(slot: Slot, items: list[Any], cells: Any) -> None:
    if cells is None:
        raise ModflowError(
            f"{slot.keyword} states one number per cell, and this run has no "
            "accepted mesh to count the cells against.",
            error_code="MODFLOW_GRIDDATA_NO_MESH")
    if len(items) != len(cells):
        raise ModflowError(
            f"{slot.keyword} states {len(items)} value(s) and the accepted mesh "
            f"has {len(cells)} cell(s); one number per cell or one per layer, "
            "never a count the grid does not carry.",
            error_code="MODFLOW_GRIDDATA_SHAPE")


def _rows_fit_cells(slot: Slot, rows: list[list[Any]], cells: Any) -> None:
    if cells is None:
        raise _shape_refusal(slot, len(rows), cells)
    bad = next((row for row in rows if len(row) != len(cells)), None)
    if bad is not None:
        raise ModflowError(
            f"{slot.keyword} states a row of {len(bad)} value(s) and the "
            f"accepted mesh has {len(cells)} cell(s); a layered row is one "
            "value per cell.",
            error_code="MODFLOW_GRIDDATA_SHAPE")


def _shape_refusal(slot: Slot, nlay: int, cells: Any) -> ModflowError:
    return ModflowError(
        f"{slot.keyword} takes one value per layer ({nlay} of them)"
        + ("" if cells is None else f" or one per cell ({len(cells)})")
        + (f", and the DFN layers it: {slot.shape}" if slot.shape else "")
        + "; the stated list is neither.",
        error_code="MODFLOW_GRIDDATA_SHAPE")
