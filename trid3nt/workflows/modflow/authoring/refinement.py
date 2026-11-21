"""The local grid refinement: a child grid refining a polygon of a rectilinear parent mesh, and the exchange cells between them.

The parent is the accepted mesh read as a lattice (every cell one rectangle of one row and one column); flopy's Lgr
cuts the child out of it and lists the parent-child connections, and this module numbers both grids as DISV cells.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ..errors import ModflowError

__all__ = ["Child", "Lattice", "built_children", "children_of", "covered_by", "lattice", "refined_grids", "refined_idomain"]

# Digits the lattice's vertex coordinates are compared at; a mesher writes the same corner as the same float.
_ROUND = 6


@dataclass(frozen=True)
class Lattice:
    """The parent mesh as a regular grid: row 0 is the top (north) row, ``cell_at[row][col]`` the mesh's cell number."""

    delr: Any
    delc: Any
    xll: float
    yll: float
    cell_at: Any
    row_col: Any

    @property
    def nrow(self) -> int:
        return len(self.delc)

    @property
    def ncol(self) -> int:
        return len(self.delr)


@dataclass(frozen=True)
class Child:
    """One refined model's grid as the DISV is written from it, and the parent-child connections in DISV cell ids."""

    name: str
    points: Any
    cells: Any
    top: Any
    botm: Any
    idomain: Any
    exchange: list
    auxiliary: list


def children_of(sheet: Any) -> list[Mapping[str, Any]]:
    """The coupled bodies that are a refinement of the carrier's grid, in the order stated."""
    return [body for body in sheet.coupled if "refine" in body]


def lattice(points: Any, cells: Any) -> Lattice:
    """The mesh as a lattice, or the refusal naming what the refinement needs."""
    import numpy as np

    pts = np.asarray(points, dtype="float64")
    if any(len(ring) != 4 for ring in cells):
        raise _refused("a refinement is cut out of a rectilinear parent, and this mesh has cells that are not four-cornered")
    xs = np.unique(np.round(pts[:, 0], _ROUND))
    ys = np.unique(np.round(pts[:, 1], _ROUND))
    ncol, nrow = len(xs) - 1, len(ys) - 1
    if ncol * nrow != len(cells):
        raise _refused(f"a refinement is cut out of a rectilinear parent; this mesh has {len(cells)} cells and its "
                       f"corners span a {nrow} by {ncol} lattice")
    cell_at = np.full((nrow, ncol), -1, dtype=int)
    row_col = []
    for icell, ring in enumerate(cells):
        x = np.round(pts[list(ring), 0], _ROUND)
        y = np.round(pts[list(ring), 1], _ROUND)
        col, row = int(np.searchsorted(xs, x.min())), int(np.searchsorted(ys, y.min()))
        if int(np.searchsorted(xs, x.max())) != col + 1 or int(np.searchsorted(ys, y.max())) != row + 1 \
                or len({*x.tolist(), }) != 2 or len({*y.tolist()}) != 2:
            raise _refused(f"a refinement is cut out of a rectilinear parent, and cell {icell} of this mesh is not one "
                           "rectangle of the lattice its corners span")
        r = nrow - 1 - row
        if cell_at[r, col] != -1:
            raise _refused(f"cells {cell_at[r, col]} and {icell} of this mesh lie on the same lattice rectangle")
        cell_at[r, col] = icell
        row_col.append((r, col))
    return Lattice(delr=np.diff(xs), delc=np.diff(ys)[::-1], xll=float(xs[0]), yll=float(ys[0]),
                   cell_at=cell_at, row_col=row_col)


def _refused(message: str) -> ModflowError:
    return ModflowError(message, error_code="MODFLOW_REFINE_PARENT")


def _layers(body: Mapping[str, Any], nlay: int) -> list[int]:
    """The child layers per parent layer: stated, else one per parent layer; the refined layers start at the top and run unbroken."""
    stated = body["refine"].get("layers")
    layers = [1] * nlay if stated is None else [int(item) for item in stated]
    name = body["name"]
    if len(layers) != nlay:
        raise ModflowError(
            f"{name} states layers {layers}, one number per parent layer, and the parent has {nlay} layer(s).",
            error_code="MODFLOW_REFINE_LAYERS")
    refined = sum(1 for item in layers if item > 0)
    if layers[0] < 1 or any(item > 0 for item in layers[refined:]) or any(item < 0 for item in layers):
        raise ModflowError(
            f"{name} states layers {layers}; the refined parent layers start at layer 1 and run unbroken, "
            "each with its child layers, the layers below them 0.", error_code="MODFLOW_REFINE_LAYERS")
    return layers


def covered_by(body: Mapping[str, Any], grid: Lattice, *, points: Any, cells: Any, utm_epsg: int | None,
               active: Any = None) -> Any:
    """The parent cells (row, column) a refinement's polygon holds by their centres, as a boolean array over the lattice."""
    import numpy as np

    from .stress import cells_of

    if not utm_epsg:
        raise ModflowError(f"{body['name']} places by a polygon, and this run has no accepted mesh with a projected zone.",
                           error_code="MODFLOW_REFINE_PARENT")
    found = cells_of(body["refine"]["geometry"], name=body["name"], points=points, cells=cells,
                     utm_epsg=int(utm_epsg), active=active)
    mask = np.zeros((grid.nrow, grid.ncol), dtype=bool)
    for icell in found:
        r, c = grid.row_col[icell]
        mask[r, c] = True
    return mask


def refined_idomain(sheet: Any, idomain: Any, *, points: Any, cells: Any, utm_epsg: int | None, nlay: int) -> Any:
    """``idomain`` as one row per layer with the cells every refinement covers zero in the layers it refines: the child solves there."""
    from .stress import layer_rows

    bodies = children_of(sheet)
    if not bodies:
        return idomain
    rows = layer_rows(idomain, nlay=nlay, ncpl=len(cells))
    grid = lattice(points, cells)
    for body in bodies:
        layers = _layers(body, nlay)
        mask = covered_by(body, grid, points=points, cells=cells, utm_epsg=utm_epsg)
        for r, c in zip(*mask.nonzero()):
            icell = int(grid.cell_at[r, c])
            for layer in range(sum(1 for item in layers if item > 0)):
                if rows[layer][icell] == 0:
                    raise ModflowError(
                        f"{body['name']} covers parent cell {icell + 1} on layer {layer + 1}, which the run does not "
                        "solve (DISV_IDOMAIN 0); a refinement replaces a solved cell.",
                        error_code="MODFLOW_REFINE_PARENT")
                rows[layer][icell] = 0
    return rows


def built_children(sheet: Any, *, points: Any, cells: Any, utm_epsg: int | None, nlay: int, top: Any, botm: Any,
                   idomain: Any) -> list[Child]:
    """Each refinement's child grid and exchange cells, cut from the parent's stated top and bottoms and its zeros."""
    import numpy as np
    from flopy.utils.lgrutil import Lgr

    from .stress import layer_rows

    bodies = children_of(sheet)
    if not bodies:
        return []
    grid = lattice(points, cells)
    shape = (grid.nrow, grid.ncol)
    rows = layer_rows(idomain, nlay=nlay, ncpl=len(cells))
    parent_top = _lattice_array(top, grid)
    parent_botm = np.stack([_lattice_array(_row(botm, k, len(cells)), grid) for k in range(nlay)])
    built = []
    for body in bodies:
        layers = _layers(body, nlay)
        refined = sum(1 for item in layers if item > 0)
        mask = covered_by(body, grid, points=points, cells=cells, utm_epsg=utm_epsg)
        keep = np.ones((nlay, *shape), dtype=int)
        keep[:refined, mask] = 0
        ncpp = int(body["refine"]["refinement"])
        lgr = Lgr(nlay, grid.nrow, grid.ncol, grid.delr, grid.delc, parent_top, parent_botm, keep, ncpp=ncpp,
                  ncppl=layers, xllp=grid.xll, yllp=grid.yll)
        built.append(_child(body, lgr, grid, rows, transport=any(
            other.get("of") == body["name"] for other in sheet.coupled)))
    return built


def refined_grids(sheet: Any, mesh: Mapping[str, Any]) -> dict[str, Child]:
    """Each refinement's child grid by model name, cut from the accepted mesh the run solved on."""
    from .accepted_mesh import mesh_artifact, mesh_nodes
    from .mesh_files import _layering, disv_dimensions

    points, cells, bed, _lonlat = mesh_nodes(mesh)
    art = mesh_artifact(mesh)
    nlay = disv_dimensions(sheet, cells)["nlay"]
    top, botm, _idomain = _layering(sheet, nlay=nlay, art=art, bed=bed, points=points, cells=cells)
    return {child.name: child for child in built_children(
        sheet, points=points, cells=cells, utm_epsg=getattr(art, "utm_epsg", None), nlay=nlay, top=top, botm=botm,
        idomain=None)}


def _row(botm: Any, layer: int, ncpl: int) -> Any:
    """The bottoms of one layer from the placed bottoms: a number, one number per layer, or rows."""
    if isinstance(botm, (int, float)):
        return [botm] * ncpl
    held = botm[layer]
    return [held] * ncpl if isinstance(held, (int, float)) else held


def _lattice_array(values: Any, grid: Lattice) -> Any:
    import numpy as np

    if isinstance(values, (int, float)):
        return np.full((grid.nrow, grid.ncol), float(values))
    out = np.empty((grid.nrow, grid.ncol))
    for icell, (r, c) in enumerate(grid.row_col):
        out[r, c] = float(values[icell])
    return out


def _child(body: Mapping[str, Any], lgr: Any, grid: Lattice, rows: Sequence[Sequence[Any]], transport: bool) -> Child:
    import numpy as np

    delr, delc = lgr.get_delr_delc()
    top, botm = lgr.get_top_botm()
    idomain = lgr.get_idomain()
    nlay, nrow, ncol = lgr.get_shape()
    xll, yll = lgr.get_lower_left()
    xs = xll + np.concatenate([[0.0], np.cumsum(delr)])
    ys = yll + float(delc.sum()) - np.concatenate([[0.0], np.cumsum(delc)])
    index = np.arange((nrow + 1) * (ncol + 1)).reshape(nrow + 1, ncol + 1)
    points = [(float(xs[j]), float(ys[i])) for i in range(nrow + 1) for j in range(ncol + 1)]
    cells = [[int(index[i + 1, j]), int(index[i + 1, j + 1]), int(index[i, j + 1]), int(index[i, j])]
             for i in range(nrow) for j in range(ncol)]
    # mf6 reads ANGLDEGX and CDIST on an xt3d exchange and on every GWT-GWT exchange.
    skewed = bool(body["refine"].get("exchange", {}).get("xt3d")) or transport
    data = lgr.get_exchange_data(angldegx=skewed, cdist=skewed)
    exchange = []
    for row in data:
        (kp, ip, jp), (kc, ic, jc), *rest = row
        exchange.append([(int(kp), int(grid.cell_at[ip, jp])), (int(kc), int(ic * ncol + jc)), *rest])
    inactive = [line for line in exchange if rows[line[0][0]][line[0][1]] == 0]
    if inactive:
        raise ModflowError(
            f"{body['name']} connects to parent cell {inactive[0][0][1] + 1} on layer {inactive[0][0][0] + 1}, "
            "which the run does not solve (DISV_IDOMAIN 0).", error_code="MODFLOW_REFINE_PARENT")
    return Child(name=body["name"], points=points, cells=cells, top=[float(v) for v in top.ravel()],
                 botm=[[float(v) for v in layer.ravel()] for layer in botm],
                 idomain=[[int(v) for v in layer.ravel()] for layer in idomain], exchange=exchange,
                 auxiliary=["angldegx", "cdist"] if skewed else [])
