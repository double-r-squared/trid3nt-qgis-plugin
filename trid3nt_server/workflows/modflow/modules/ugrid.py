"""The one transform a MODFLOW run needs: its cell-valued reads written as a CF-UGRID display file.

mf6's binary head and budget files are not in the render format set. Every mesh product of a run is one UGRID file of the
accepted mesh holding one dataset group, valued per cell as mf6 values it, so no cell value is averaged onto a node; a file
per group because each group keeps its own time axis.
"""

from __future__ import annotations

import os
import tempfile
from types import SimpleNamespace
from typing import Any, Sequence

from trid3nt_server.render.mesh_display import write_ugrid
from trid3nt_server.store import objects as storage

__all__ = ["display_file", "group"]


def display_file(name: str) -> str:
    """The run-prefix basename the group ``name`` is written under."""
    return f"modflow_{name}.nc"


def group(solved: Any, name: str, steps: Sequence[tuple[float, Sequence[float]]],
          units: str = "") -> str:
    """Write the dataset group ``name`` (``steps`` = ``(seconds, one value per cell)``) as its own display file -> its basename.

    The seconds count from the run's reference time - its moment, else when its solve began."""
    points, cells, _bed, _lonlat = solved.nodes
    mesh = SimpleNamespace(points=points, cells=cells,
                           crs_authid=f"EPSG:{solved.utm_epsg}", bed=None)
    handle, path = tempfile.mkstemp(suffix=".nc", prefix="trid3nt_modflow_")
    os.close(handle)
    try:
        write_ugrid(mesh, path,
                    datasets={name: [(float(t), list(v)) for t, v in steps]},
                    reference_time=solved.run.get("reference_time"),
                    units={name: units})
        with open(path, "rb") as written:
            body = written.read()
    finally:
        os.unlink(path)
    storage.client().put_object(
        Bucket=storage.runs_bucket(), Key=f"{solved.run_id}/{display_file(name)}",
        Body=body, ContentType="application/x-netcdf")
    return display_file(name)
