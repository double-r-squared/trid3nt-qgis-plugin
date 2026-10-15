"""One GRIDDATA statement as the array the package constructor takes.

A constant passes, one number per layer fits the DFN's own shape, and a string
names a run input whose raster is sampled at the cells - the fill's existing
"string names an input" rule, measured against the accepted mesh. What the shape
cannot take refuses by name here rather than as an engine error inside mf6.
"""

from __future__ import annotations

import tempfile
from typing import Any, Mapping

from trid3nt_server.workflows.runtime.module import Slot

from ..errors import ModflowError

__all__ = ["place"]


def place(value: Any, *, slot: Slot, nlay: int, run: Mapping[str, Any],
          points: Any = None, cells: Any = None,
          utm_epsg: int | None = None) -> Any:
    """``value`` as this ARRAY keyword's data: a constant, per-layer rows, or sampled cells.

    ``points``/``cells`` are the accepted mesh's nodes and connectivity - needed only
    where the value names a raster or carries one number per cell; ``nlay`` is the
    sheet's layer count a layered value is checked against."""
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        with tempfile.TemporaryDirectory(prefix="trid3nt_griddata_") as work:
            sampled = _sample(value, slot=slot, run=run, scratch=work,
                              points=points, cells=cells, utm_epsg=utm_epsg)
        return [list(sampled) for _ in range(nlay)] if slot.layered else sampled
    if isinstance(value, (list, tuple)):
        return _sequence(list(value), slot=slot, nlay=nlay, run=run,
                         points=points, cells=cells, utm_epsg=utm_epsg)
    raise ModflowError(
        f"{slot.keyword} states {value!r}; its value is a constant, one value "
        "per layer, or the name of a run input to sample at the cells.",
        error_code="MODFLOW_GRIDDATA_SHAPE")


def _sequence(items: list[Any], *, slot: Slot, nlay: int,
              run: Mapping[str, Any], points: Any, cells: Any,
              utm_epsg: int | None) -> Any:
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
                            points=points, cells=cells, utm_epsg=utm_epsg)
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
            points: Any, cells: Any, utm_epsg: int | None) -> list[float]:
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
    from trid3nt_server.tools.derive._raster_layers import read, staged

    path = staged(run[name], role=name, scratch=scratch)
    values, _provenance, crs, transform = read(path)
    xs, ys = _centres(points, cells)
    if crs is not None and utm_epsg:
        from rasterio.warp import transform as warp

        xs, ys = warp(f"EPSG:{int(utm_epsg)}", crs, xs, ys)
    return _at(values, transform=transform, xs=xs, ys=ys, slot=slot, name=name)


def _at(values: Any, *, transform: Any, xs: list[float], ys: list[float],
        slot: Slot, name: str) -> list[float]:
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
            f"{slot.keyword} reads {name!r} at {unmeasured} of {len(xs)} "
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
