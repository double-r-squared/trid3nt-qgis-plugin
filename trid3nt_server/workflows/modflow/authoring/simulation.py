"""The simulation directory: mfsim.nam, its timing and solution, one file per package.

Every value comes off the sheet and the engine's own flopy does the formatting. The
accepted mesh's DISV is written by no one here - its bytes are staged from this run's
artifact, and this walk constructs a filename-only copy so the model's name file names
it, then writes around it package by package. Never ``write_simulation``: it would
overwrite the staged mesh with a package flopy knows nothing about.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import DISV_FILE
from .accepted_mesh import mesh_nodes
from .griddata import place
from .mesh_files import disv_dimensions

from ..errors import ModflowError

logger = logging.getLogger("trid3nt_server.workflows.modflow.authoring.simulation")

__all__ = ["serialize"]

# What each module's name file must hold even when the sheet states none of it: the
# engine refuses a flow model without its property and initial-condition packages, and
# an empty output-control options block is a valid statement of "no printouts yet".
_ALWAYS = {"gwf": ("IC", "NPF", "OC"), "gwt": (), "gwe": ()}

_MODEL_KWARGS = "NAM"

_SIM_LEVEL = ("TDIS", "IMS")

_GRID = "DISV"

# A sheet stating these would answer the mesh's own question: this run's grid is the
# accepted mesh, and only its DISV may describe it.
_WRONG_GRID = ("DIS", "DISU")


def serialize(sheet: Any, rundir: Path | str, mesh: Any) -> dict[str, Any]:
    """Write the simulation directory from the sheet and this run's mesh -> what it wrote.

    The run directory is this author's alone: it holds the staged DISV and every input
    row, and this walk adds the steering files and one file per stated package. The mesh
    is read for the grid's dimensions - flopy shapes every stated array against them.
    """
    work = Path(rundir)
    work.mkdir(parents=True, exist_ok=True)
    groups, named = _grouped(sheet)
    written = _write(sheet, work, groups, named, mesh)
    logger.info("modflow simulation written to %s: %s", work, written)
    return {"written": written}


def _refuse(sheet: Any) -> None:
    for identifier in sheet.filled:
        if identifier.split("_", 1)[0] in _WRONG_GRID:
            raise ModflowError(
                f"{identifier} states another discretization, and this run "
                f"solves its accepted mesh as the DISV ({DISV_FILE}); a "
                "different grid is the mesh acceptance to accept, never a "
                "keyword of this run.",
                error_code="MODFLOW_WRONG_GRID")
        if identifier == "NAM_PACKAGES":
            raise ModflowError(
                "NAM_PACKAGES states the model name file's file list, which "
                "this authoring writes itself from what the sheet states; "
                "state each package's own keywords and it appears by name.",
                error_code="MODFLOW_NAM_PACKAGES")


def _grouped(sheet: Any) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """The sheet's filled rows split into per-prefix keyword rows and the model's own."""
    _refuse(sheet)
    groups: dict[str, dict[str, Any]] = {}
    named: dict[str, Any] = {}
    for identifier, row in sheet.filled.items():
        prefix, sep, _rest = identifier.partition("_")
        if not sep:
            raise ModflowError(
                f"{identifier} is a bare name a package keyword cannot be "
                "derived from; a keyword of a package is PREFIX_NAME.",
                error_code="MODFLOW_KEYWORD_SHAPE")
        if prefix == _MODEL_KWARGS:
            named[suffix(prefix, identifier)] = row.value
            continue
        groups.setdefault(prefix, {})[suffix(prefix, identifier)] = row
    return groups, named


def suffix(prefix: str, identifier: str) -> str:
    """The identifier with its prefix removed, at the name flopy's constructor takes."""
    return identifier[len(prefix) + 1:].lower()


def _write(sheet: Any, work: Path, groups: dict[str, dict[str, Any]],
           named: dict[str, Any], mesh: Any) -> list[str]:
    import flopy

    module = str(sheet.module)
    if module not in _ALWAYS:
        raise ModflowError(
            f"{module!r} is no module this authoring solves; its simulators "
            "are gwf, gwt and gwe.",
            error_code="MODFLOW_MODULE_UNKNOWN")
    cap = module.capitalize()
    points, cells, _bed, _lonlat = mesh_nodes(mesh)
    dims = disv_dimensions(sheet, cells)
    utm_epsg = getattr((mesh or {}).get("artifact"), "utm_epsg", None)
    sim = flopy.mf6.MFSimulation(sim_name="trid3nt", sim_ws=str(work),
                                 verbosity_level=0)
    _package(flopy, "ModflowTdis",
             _placed(groups.pop("TDIS", {}), dims=dims, points=points,
                     cells=cells, utm_epsg=utm_epsg, sheet=sheet), owner=sim,
             how="TDIS of the simulation")
    model = _package(flopy, f"Modflow{cap}", named, owner=sim, modelname=module,
                     model_nam_file=f"{module}.nam",
                     how=f"the {module} model itself")
    # The grid is the staged mesh's: constructed for the name file's line alone and
    # the dimension integers flopy shapes arrays against; mesh_files wrote its bytes,
    # and the DISV rows themselves were read there.
    groups.pop(_GRID, None)
    _package(flopy, f"Modflow{cap}disv", {}, owner=model, filename=DISV_FILE,
             **dims, how=f"the staged {DISV_FILE}")
    ims = _package(flopy, "ModflowIms",
                   _placed(groups.pop("IMS", {}), dims=dims, points=points,
                           cells=cells, utm_epsg=utm_epsg, sheet=sheet),
                   owner=sim, filename=f"{module}.ims",
                   how="IMS of the simulation")
    sim.register_ims_package(ims, [module])
    covered = set(groups) | set(_SIM_LEVEL) | {_MODEL_KWARGS, _GRID}
    for prefix, rows in groups.items():
        _package(flopy, f"Modflow{cap}{prefix.lower()}",
                 _placed(rows, dims=dims, points=points, cells=cells,
                         utm_epsg=utm_epsg, sheet=sheet),
                 owner=model, how=f"{prefix} of {module}")
    for prefix in _ALWAYS[module]:
        if prefix not in covered:
            _package(flopy, f"Modflow{cap}{prefix.lower()}", {}, owner=model,
                     how=f"the always-set {prefix} of {module}")
    return _write_package_by_package(sim)


def _placed(rows: dict[str, Any], *, dims: dict[str, int], points: Any,
            cells: Any, utm_epsg: int | None, sheet: Any) -> dict[str, Any]:
    """Each keyword's constructor value - an ARRAY placed against the mesh's cells, the rest as stated."""
    return {
        name: (place(row.value, slot=row.slot, nlay=int(dims["nlay"]),
                     run=sheet.run, points=points, cells=cells,
                     utm_epsg=utm_epsg)
               if row.slot.type == "ARRAY" else row.value)
        for name, row in rows.items()}


def _package(flopy: Any, class_name: str, keywords: dict[str, Any], *,
             owner: Any, how: str, **required: Any) -> Any:
    """Construct one flopy class on its simulation or model, a refusal naming it if refused."""
    cls = getattr(flopy.mf6, class_name, None)
    if cls is None:
        raise ModflowError(
            f"{how}: no {class_name} exists, so what this sheet states has no "
            "home in this simulator.",
            error_code="MODFLOW_PACKAGE_UNKNOWN")
    try:
        return cls(owner, **required, **keywords)
    except Exception as exc:
        raise ModflowError(
            f"{how}: {class_name} refused what this sheet states - "
            f"{type(exc).__name__}: {exc}",
            error_code="MODFLOW_PACKAGE_STATE") from exc


def _write_package_by_package(sim: Any) -> list[str]:
    """Every file but the discretization - the staged DISV's bytes are the mesh's."""
    written: list[str] = []
    sim.name_file.write()
    written.append(sim.name_file.filename)
    sim._tdis_file.write()
    written.append(Path(sim._tdis_file.filename).name)
    for solution in sim._solution_files.values():
        solution.write()
        written.append(Path(solution.filename).name)
    for model in sim._models.values():
        model.name_file.write()
        written.append(model.name_file.filename)
        for package_name in model.get_package_list():
            package = model.get_package(package_name)
            if package.package_type in ("dis", "disv", "disu"):
                continue
            package.write()
            written.append(Path(package.filename).name)
    return sorted(written)
