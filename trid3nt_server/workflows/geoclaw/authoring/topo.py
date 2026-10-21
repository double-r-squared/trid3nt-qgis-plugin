"""The topofiles a sheet's Topography composite names, written as topotype 3 by Clawpack.

Each surface is a raster already on the run's vertical frame; it is read in EPSG:4326
(reprojected when it is not) and written with ``Topography.write`` centre-registered, so
the first node stands exactly on the stated lower-left and the header states what the
engine reads. A surface with a node nothing measured refuses: the engine would read that
node as its missing-value sentinel.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Mapping

from ..errors import GeoclawError
from .grid import covered_by

__all__ = ["TOPOFILE", "write_topofiles"]

#: The key a composite's file entry carries its surface under.
TOPOFILE = "topofile"

#: A 4326 raster's two cell edges may differ by this fraction; the writer states one cellsize.
_SQUARE = 1e-6


def _refuse(code: str, message: str) -> None:
    raise GeoclawError(message, error_code=f"GEOCLAW_{code}")


def write_topofiles(sheet: Any, work: Path) -> dict[str, dict[str, Any]]:
    """Write every topofile the sheet's composite named into ``work`` -> file name to its header.

    The header carries ncols, nrows, cellsize, xll, yll and the registration, and the
    set is checked against the sheet's level-1 box by the engine's own coverage rule."""
    headers: dict[str, dict[str, Any]] = {}
    for name, content in sheet.files.items():
        if isinstance(content, Mapping) and TOPOFILE in content:
            headers[name] = _written(str(content[TOPOFILE]), work / name, name)
    if headers:
        covered_by(sheet.filled["CLAW_LOWER"].value, sheet.filled["CLAW_UPPER"].value,
                   headers.values())
    return headers


def _written(uri: str, path: Path, name: str) -> dict[str, Any]:
    import numpy as np
    from clawpack.geoclaw.topotools import Topography

    with tempfile.TemporaryDirectory(prefix="geoclaw-topo-") as scratch:
        values, transform = _lonlat(uri, scratch, name)
    rows, cols = values.shape
    dx, dy = float(transform.a), float(-transform.e)
    if transform.b or transform.d or dx <= 0 or dy <= 0:
        _refuse("TOPO_GRID", f"{name} is not a north-up grid, which topotype 3 states.")
    if abs(dx - dy) > _SQUARE * dx:
        _refuse("TOPO_CELL",
                f"{name} has cells {dx:g} wide and {dy:g} high degrees; topotype 3 "
                "states one cellsize, so grid the surface to square cells first.")
    blank = int(np.isnan(values).sum())
    if blank:
        _refuse("TOPO_NODATA",
                f"{name} holds {blank} of {values.size} nodes nothing measured, which "
                "the engine would read as its missing-value sentinel: fetch the surface "
                "over the ground it measures, so the coarser file under it covers the rest.")
    x = transform.c + dx * (np.arange(cols) + 0.5)
    y = transform.f - dy * (np.arange(rows) + 0.5)
    topo = Topography(topo_type=3)
    topo.set_xyZ(x, y, values.astype("float64"))
    topo.write(str(path), topo_type=3, grid_registration="llcenter")
    return {"ncols": cols, "nrows": rows, "cellsize": dx,
            "xll": float(x[0]), "yll": float(y[-1]), "registration": "centre"}


def _lonlat(uri: str, scratch: str, name: str) -> tuple[Any, Any]:
    """Band 1 as float64 with NaN for nothing measured, and its transform, in EPSG:4326."""
    import numpy as np
    import rasterio
    from rasterio.warp import Resampling, calculate_default_transform, reproject

    from trid3nt_server.tools.derive._hydrology_common import _stage_uri_local

    with rasterio.open(_stage_uri_local(uri, scratch, "topo")) as src:
        data = src.read(1, masked=True).astype("float64").filled(np.nan)
        if src.crs is None:
            _refuse("TOPO_CRS", f"{name} states no coordinate system, so it cannot be put in lon/lat.")
        if src.crs.to_epsg() == 4326:
            return data, src.transform
        transform, width, height = calculate_default_transform(
            src.crs, "EPSG:4326", src.width, src.height, *src.bounds)
        out = np.full((height, width), np.nan, dtype="float64")
        reproject(data, out, src_transform=src.transform, src_crs=src.crs,
                  src_nodata=np.nan, dst_transform=transform, dst_crs="EPSG:4326",
                  dst_nodata=np.nan, resampling=Resampling.bilinear)
        return _inscribed(out, transform)


def _inscribed(values: Any, transform: Any) -> tuple[Any, Any]:
    """The grid trimmed of the edge rows and columns the reprojection left unmeasured at its corners.

    A projected rectangle is a rotated one in lon/lat, so the lon/lat bounds hold wedges
    nothing measured; the edge with the most of them goes until none is left. Measured
    nodes are only dropped, never made."""
    import numpy as np

    top, bottom, left, right = 0, values.shape[0], 0, values.shape[1]
    while top < bottom and left < right:
        view = np.isnan(values[top:bottom, left:right])
        edges = {"top": view[0].sum(), "bottom": view[-1].sum(),
                 "left": view[:, 0].sum(), "right": view[:, -1].sum()}
        worst = max(edges, key=edges.get)
        if not edges[worst]:
            break
        if worst == "top":
            top += 1
        elif worst == "bottom":
            bottom -= 1
        elif worst == "left":
            left += 1
        else:
            right -= 1
    trimmed = values[top:bottom, left:right]
    shifted = transform * transform.translation(left, top)
    return trimmed, shifted
