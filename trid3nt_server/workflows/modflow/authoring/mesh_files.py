"""The DISV file MODFLOW asks an accepted mesh for: one artifact per mesh and layering.

The bytes are written from the sheet's stated layering over the mesh's own geometry and
uploaded beside the mesh's objects; a second run on the same mesh and layering reads them
back. The run's staged directory receives the file as its input row, never from here.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from .accepted_mesh import mesh_artifact, mesh_missing, mesh_nodes
from .griddata import frame_of, place
from .refinement import refined_idomain
from .stress import hold_lakes, lake_statements
from ..errors import ModflowError

logger = logging.getLogger("trid3nt_server.workflows.modflow.authoring.mesh_files")

__all__ = ["botm_of", "disv_dimensions", "modflow_mesh_files", "top_of", "unstructured"]


def unstructured(sheet: Any) -> bool:
    """Whether the sheet states the run's grid as a DISU, which no accepted-mesh DISV describes."""
    return any(identifier.startswith("DISU_") for identifier in sheet.filled)


def disv_dimensions(sheet: Any, cells: Any) -> dict[str, int]:
    """The grid's ``(nlay, ncpl)`` - the integers flopy shapes every stated array against."""
    return {"nlay": _layers(sheet), "ncpl": len(cells)}


async def modflow_mesh_files(*, sheet: Any, mesh: Mapping[str, Any]) -> dict[str, Any]:
    """The accepted mesh's DISV for this sheet's layering -> its uri under engine_files.

    A record already carrying this mesh and layering's file is not rewritten; any other
    layering on the same mesh writes its own file beside it.
    """
    import asyncio

    return await asyncio.to_thread(_mesh_files, sheet, mesh)


def _mesh_files(sheet: Any, mesh: Mapping[str, Any]) -> dict[str, Any]:
    if unstructured(sheet):
        return {"engine_files": dict(getattr(mesh_artifact(mesh), "engine_files", None) or {}), "disv": None}
    art = mesh_artifact(mesh)
    if art is None:
        raise mesh_missing(
            f"this run was handed no accepted mesh to write a DISV from "
            f"(mesh record: {sorted((mesh or {}))}).")
    points, cells, bed, _lonlat = mesh_nodes(mesh)
    nlay = _layers(sheet)
    top, botm, idomain = _layering(sheet, nlay=nlay, art=art, bed=bed,
                                   points=points, cells=cells)
    if lake_statements(sheet):
        idomain = hold_lakes(lake_statements(sheet), idomain, points=points, cells=cells,
                             utm_epsg=getattr(art, "utm_epsg", None), nlay=nlay,
                             frame=frame_of(sheet), top=top, botm=botm)
    idomain = refined_idomain(sheet, idomain, points=points, cells=cells,
                              utm_epsg=getattr(art, "utm_epsg", None), nlay=nlay)
    key = _layering_key(nlay=nlay, top=top, botm=botm, idomain=idomain)
    files = dict(getattr(art, "engine_files", None) or {})
    if not files.get(key):
        gridprops = _gridprops(points, cells)
        with tempfile.TemporaryDirectory(prefix="trid3nt_disv_") as work:
            local = _write_disv(Path(work), filename=f"{key}.disv", nlay=nlay,
                                top=top, botm=botm, idomain=idomain,
                                gridprops=gridprops, options=_disv_options(sheet))
            files[key] = _stage(art.mesh_id, local)
        _record(art, files)
        logger.info("modflow disv written for %s: %s", art.mesh_id, key)
    return {"engine_files": files, "disv": str(files[key])}


# The DISV's own geometry - the vertices and cell tables and their counts. What this
# step writes from the mesh, so a sheet stating them would hold a second answer.
_DISV_MESHES = ("DISV_NCPL", "DISV_NVERT", "DISV_VERTICES", "DISV_CELL2D")
_DISV_DATA = ("DISV_NLAY", "DISV_TOP", "DISV_BOTM", "DISV_IDOMAIN") + _DISV_MESHES


def _disv_options(sheet: Any) -> dict[str, Any]:
    """The DISV options a sheet states (LENGTH_UNITS, origins...) as flopy keywords."""
    for identifier in _DISV_MESHES:
        if identifier in sheet.filled:
            raise ModflowError(
                f"{identifier} states the mesh's own tables, which no sheet "
                "may state; the DISV's geometry is this step's writing of the "
                "accepted mesh.",
                error_code="MODFLOW_MESH_AUTHORED")
    return {identifier.split("_", 1)[1].lower(): row.value
            for identifier, row in sheet.filled.items()
            if identifier.startswith("DISV_") and identifier not in _DISV_DATA}


def _layers(sheet: Any) -> int:
    """The sheet's stated layer count - the one number every layering here hangs off."""
    stated = sheet.filled.get("DISV_NLAY")
    if stated is None:
        raise _unstated("DISV_NLAY", "a DISV is one mesh under a layer count, and "
                        "this sheet states no layers.")
    nlay = int(stated.value)
    if nlay < 1:
        raise ModflowError(
            f"DISV_NLAY states {nlay}; a simulation solves at least one layer.",
            error_code="MODFLOW_LAYERING_UNSTATED")
    return nlay


def _layering(sheet: Any, *, nlay: int, art: Any, bed: Any, points: Any,
              cells: Any) -> tuple[Any, Any, Any]:
    """Top, bottom and idomain for every cell and layer - stated, or refused by name."""
    run = sheet.run
    utm_epsg = getattr(art, "utm_epsg", None)
    frame = frame_of(sheet)
    top = top_of(sheet, nlay=nlay, art=art, bed=bed, points=points, cells=cells)
    botm = botm_of(sheet, nlay=nlay, art=art, points=points, cells=cells, top=top)
    idomain = None
    stated_idomain = sheet.filled.get("DISV_IDOMAIN")
    if stated_idomain is not None:
        idomain = place(stated_idomain.value, slot=stated_idomain.slot,
                        nlay=nlay, run=run, points=points, cells=cells,
                        utm_epsg=utm_epsg, frame=frame)
    return top, botm, idomain


def botm_of(sheet: Any, *, nlay: int, art: Any, points: Any, cells: Any, top: Any) -> Any:
    """The grid's bottoms as the DISV is written from them: stated, or refused by name."""
    stated_botm = sheet.filled.get("DISV_BOTM")
    if stated_botm is None:
        raise _unstated(
            "DISV_BOTM", "every cell and layer this run solves needs a bottom "
            "elevation the fill states; a thickness under the mesh's bed is the "
            "fill's to choose, never arithmetic made here.")
    return place(stated_botm.value, slot=stated_botm.slot, nlay=nlay, run=sheet.run,
                 points=points, cells=cells, utm_epsg=getattr(art, "utm_epsg", None),
                 frame=frame_of(sheet), top=top)


def top_of(sheet: Any, *, nlay: int, art: Any, bed: Any, points: Any,
           cells: Any) -> Any:
    """The top of the grid the layering and the starting heads lie under: stated, else the mesh's painted bed."""
    stated_top = sheet.filled.get("DISV_TOP")
    if stated_top is None:
        return _bed_top(art, bed, cells)
    return place(stated_top.value, slot=stated_top.slot, nlay=nlay, run=sheet.run,
                 points=points, cells=cells, utm_epsg=getattr(art, "utm_epsg", None),
                 frame=frame_of(sheet))


def _bed_top(art: Any, bed: Any, cells: Any) -> list[float]:
    """One top elevation per cell: the mean of its own painted bed, a derived default."""
    if bed is None or not getattr(art, "has_bathymetry", False):
        raise _unstated(
            "DISV_TOP", "this sheet states no top and the accepted mesh carries "
            "no painted bed to derive one from; state DISV_TOP or accept a mesh "
            "whose recipe paints its bed.")
    tops: list[float] = []
    unmeasured = 0
    for cell in cells:
        mean = sum(float(bed[v]) for v in cell) / len(cell)
        if not math.isfinite(mean):
            unmeasured += 1
            continue
        tops.append(mean)
    if unmeasured:
        raise ModflowError(
            f"DISV_TOP derives from the mesh's bed, and {unmeasured} of "
            f"{len(tops) + unmeasured} cell(s) touch a node the bed does not "
            "paint; a cell solved under a guessed top refuses here by name.",
            error_code="MODFLOW_TOP_UNMEASURED")
    return tops


def _gridprops(points: Any, cells: Any) -> dict[str, Any]:
    """The DISV's vertex and cell2d tables - the mesh's geometry, numbered for mf6.

    mf6 measures a cell's area from its cell2d ring and refuses a negative one, so a
    counterclockwise ring the mesh lists is reversed here on the way in.
    """
    import numpy as np
    from flopy.utils.cvfdutil import get_disv_gridprops

    iverts = []
    for cell in cells:
        ring = [int(v) for v in cell]
        pts = [(float(points[v][0]), float(points[v][1])) for v in ring]
        if _shoelace(pts) > 0.0:
            ring.reverse()
        iverts.append(ring)
    return get_disv_gridprops(np.asarray(points, dtype=float), iverts)


def _shoelace(pts: list[tuple[float, float]]) -> float:
    """Signed area of a closed ring: positive counterclockwise."""
    return sum(pts[i][0] * pts[(i + 1) % len(pts)][1]
               - pts[(i + 1) % len(pts)][0] * pts[i][1]
               for i in range(len(pts))) / 2.0


def _layering_key(*, nlay: int, top: Any, botm: Any, idomain: Any) -> str:
    """The object name a layering's bytes are stored under: the mesh's, by its own digest."""
    body = json.dumps({"nlay": int(nlay), "top": top, "botm": botm,
                       "idomain": idomain},
                      sort_keys=True, separators=(",", ":"))
    return f"disv-{hashlib.sha256(body.encode()).hexdigest()[:16]}"


def _write_disv(work: Path, *, filename: str, nlay: int, top: Any, botm: Any,
                idomain: Any, gridprops: Mapping[str, Any],
                options: Mapping[str, Any]) -> Path:
    """Write just the one DISV file, flopy doing the formatting - never a whole simulation."""
    import flopy

    sim = flopy.mf6.MFSimulation(sim_name="disv", sim_ws=str(work),
                                 verbosity_level=0)
    model = flopy.mf6.ModflowGwf(sim, modelname="disv")
    kwargs: dict[str, Any] = {"nlay": nlay, "top": top, "botm": botm,
                              **dict(gridprops), **dict(options)}
    if idomain is not None:
        kwargs["idomain"] = idomain
    disv = flopy.mf6.ModflowGwfdisv(model, filename=filename, **kwargs)
    disv.write()
    path = work / filename
    if not path.exists():
        raise ModflowError(
            f"flopy wrote no {filename} from this mesh's geometry; the "
            "accepted mesh's cells do not form a grid the engine can solve.",
            error_code="MODFLOW_DISV_UNWRITTEN")
    return path


def _stage(mesh_id: str, local: Path) -> str:
    """Upload one written file beside the mesh's other objects -> its uri.

    With no cache bucket configured it refuses: a temporary-directory path reads nothing.
    """
    from trid3nt_server.store import objects as storage

    bucket = (os.environ.get("TRID3NT_CACHE_BUCKET") or "").strip()
    if not bucket:
        raise mesh_missing(
            f"TRID3NT_CACHE_BUCKET is unset, so {local.name} written for mesh "
            f"{mesh_id} has nowhere to be staged and the run would read nothing.")
    key = f"mesh/{mesh_id}/{local.name}"
    storage.client().put_object(Bucket=bucket, Key=key, Body=local.read_bytes())
    return f"s3://{bucket}/{key}"


def _record(art: Any, files: Mapping[str, str]) -> None:
    """Put what was written onto the mesh's own record, under the engine's names.

    The sidecar is durability, never correctness: a failed write leaves the run its files.
    """
    from trid3nt_server.store import objects as storage
    from trid3nt_server.tools.mesh.artifact import write_mesh_artifact_sidecar

    art.engine_files = dict(files)
    if str(art.display_uri or "").startswith("s3://"):
        write_mesh_artifact_sidecar(art, storage.client())


def _unstated(identifier: str, why: str) -> ModflowError:
    return ModflowError(f"{identifier} states nothing; {why}",
                        error_code="MODFLOW_LAYERING_UNSTATED")
