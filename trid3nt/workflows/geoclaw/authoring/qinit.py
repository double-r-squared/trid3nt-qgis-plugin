"""The qinit file a sheet's InitialSurface composite names, written as the engine reads it.

The engine reads one file of ``x y z`` lines from the north-west corner, a row of increasing x at
a time and the rows running south; ``z`` is the surface elevation the water stands at (qinit type
4). The raster is read in EPSG:4326 as the topofiles are, and a node nothing measured refuses.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Mapping

from ..errors import GeoclawError
from .topo import _lonlat

__all__ = ["QINIT", "write_qinitfiles"]

#: The key a composite's file entry carries its surface under.
QINIT = "qinit"


def write_qinitfiles(sheet: Any, work: Path) -> list[str]:
    """Write every qinit file the sheet's composite named into ``work`` -> their names."""
    written: list[str] = []
    for name, content in sheet.files.items():
        if isinstance(content, Mapping) and QINIT in content:
            _written(str(content[QINIT]), work / name, name)
            written.append(name)
    return written


def _written(uri: str, path: Path, name: str) -> None:
    import numpy as np

    with tempfile.TemporaryDirectory(prefix="geoclaw-qinit-") as scratch:
        values, transform = _lonlat(uri, scratch, name)
    rows, cols = values.shape
    dx, dy = float(transform.a), float(-transform.e)
    if transform.b or transform.d or dx <= 0 or dy <= 0:
        raise GeoclawError(f"{name} is not a north-up grid, which a qinit file states.",
                           error_code="GEOCLAW_QINIT_GRID")
    blank = int(np.isnan(values).sum())
    if blank:
        raise GeoclawError(f"{name} holds {blank} of {values.size} nodes nothing measured, which a qinit "
                           "file cannot state: hand it a surface over the whole ground it covers.",
                           error_code="GEOCLAW_QINIT_NODATA")
    x = transform.c + dx * (np.arange(cols) + 0.5)
    y = transform.f - dy * (np.arange(rows) + 0.5)
    xx, yy = np.meshgrid(x, y)
    np.savetxt(path, np.column_stack([xx.ravel(), yy.ravel(), values.ravel()]), fmt="%.12f %.12f %.12e")
