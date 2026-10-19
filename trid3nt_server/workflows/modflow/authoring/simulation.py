"""The simulation directory: mfsim.nam, its timing and solution, one file per package.

Every value comes off the sheet and the engine's own flopy does the formatting. The
accepted mesh's DISV is written by no one here - its bytes are staged from this run's
artifact, and this walk constructs a filename-only copy so the model's name file names
it, then writes around it package by package. Never ``write_simulation``: it would
overwrite the staged mesh with a package flopy knows nothing about.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from . import DISV_FILE
from .accepted_mesh import mesh_artifact, mesh_nodes
from .griddata import frame_of, needs_top, place
from .mesh_files import disv_dimensions, top_of
from .stress import place as place_stress

from ..errors import ModflowError
from ...runtime.sheet import Filled, fill

logger = logging.getLogger("trid3nt_server.workflows.modflow.authoring.simulation")

__all__ = ["serialize"]

# The packages mf6 refuses each model without (its own "package not specified" check):
# every one is written, so the sheet states their required keywords or the run refuses.
_REQUIRED_PACKAGES = {"gwf": ("IC", "NPF"), "gwt": ("IC", "MST"),
                      "gwe": ("IC", "EST")}

# The blocks mf6 refuses a written package without; a required keyword in one has no
# engine value, and flopy's constructor default would write an aquifer nobody measured.
_REQUIRED_BLOCKS = frozenset({"DIMENSIONS", "GRIDDATA", "PERIODDATA"})

_MODEL_KWARGS = "NAM"

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
    if module not in _REQUIRED_PACKAGES:
        raise ModflowError(
            f"{module!r} is no module this authoring solves; its simulators "
            "are gwf, gwt and gwe.",
            error_code="MODFLOW_MODULE_UNKNOWN")
    points, cells, bed, _lonlat = mesh_nodes(mesh)
    dims = disv_dimensions(sheet, cells)
    utm_epsg = getattr((mesh or {}).get("artifact"), "utm_epsg", None)
    frame = frame_of(sheet)
    top = (top_of(sheet, nlay=int(dims["nlay"]), art=mesh_artifact(mesh), bed=bed,
                  points=points, cells=cells)
           if any(needs_top(row.value) for rows in groups.values()
                  for row in rows.values()) else None)
    placed = dict(dims=dims, points=points, cells=cells, utm_epsg=utm_epsg,
                  sheet=sheet, frame=frame, top=top)
    active = _active(sheet, nlay=int(dims["nlay"]), points=points, cells=cells,
                     utm_epsg=utm_epsg, frame=frame)
    derived = _stressed(sheet, groups, nlay=int(dims["nlay"]), points=points,
                        cells=cells, utm_epsg=utm_epsg, frame=frame, active=active)
    _unstated(sheet, set(groups) | set(_REQUIRED_PACKAGES[module]) | {"TDIS"},
              derived)
    sim = flopy.mf6.MFSimulation(sim_name="trid3nt", sim_ws=str(work),
                                 verbosity_level=0)
    _package(flopy, "ModflowTdis",
             _placed(groups.pop("TDIS", {}), **placed),
             owner=sim,
             how="TDIS of the simulation")
    carrying = [prefix for prefix, rows in groups.items()
                if "CONCENTRATION" in map(str, getattr(rows.get("auxiliary"), "value", None) or ())]
    flow = _model(flopy, sim, module=module, groups=groups, named=named, **placed)
    # The water a flow package carries a CONCENTRATION on is a transport source: SSM names
    # each such package by the name the flow model's name file gives it.
    sources = [[flow.get_package(prefix.lower()).package_name, "AUX", "CONCENTRATION"]
               for prefix in carrying]
    # Each coupled body is a second model in this simulation: its own packages from its
    # slots, a filename-only DISV on the same staged mesh, its own solution, and the
    # exchange that joins it to the flow.
    for body in sheet.coupled:
        coupled = coupled_sheet(sheet, body)
        cgroups, cnamed = _grouped(coupled)
        cderived = _stressed(coupled, cgroups, nlay=int(dims["nlay"]),
                             points=points, cells=cells, utm_epsg=utm_epsg,
                             frame=frame, active=active)
        _unstated(coupled, set(cgroups) | set(_REQUIRED_PACKAGES[coupled.module]),
                  cderived)
        if coupled.module == "gwt" and sources \
                and "sources" not in cgroups.get("SSM", {}):
            cgroups.setdefault("SSM", {})
        _model(flopy, sim, module=coupled.module, groups=cgroups, named=cnamed,
               sources=sources if coupled.module == "gwt" else (), **placed)
        _exchange(flopy, sim, carrier=module, coupled=coupled.module)
    return _write_package_by_package(sim)


def _model(flopy: Any, sim: Any, *, module: str, groups: dict[str, dict[str, Any]],
           named: dict[str, Any], dims: dict[str, int], points: Any, cells: Any,
           utm_epsg: int | None, sheet: Any, frame: Any, top: Any,
           sources: Any = ()) -> Any:
    """One model of this simulation: its name file, the staged DISV's line, its solution and its packages."""
    cap = module.capitalize()
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
                           cells=cells, utm_epsg=utm_epsg, sheet=sheet,
                           frame=frame, top=top),
                   owner=sim, filename=f"{module}.ims",
                   how=f"IMS of the {module} solution")
    sim.register_ims_package(ims, [module])
    for prefix, rows in groups.items():
        keywords = _placed(rows, dims=dims, points=points, cells=cells,
                           utm_epsg=utm_epsg, sheet=sheet, frame=frame, top=top)
        if prefix == "SSM" and sources:
            keywords = {"sources": list(sources), **keywords}
        _package(flopy, f"Modflow{cap}{prefix.lower()}", keywords,
                 owner=model, how=f"{prefix} of {module}")
    return model


def coupled_sheet(sheet: Any, body: Mapping[str, Any]) -> Any:
    """A coupled body's slots as the sheet its own authoring walks: the shared run, the body's own dictionary."""
    from ..modules import wrapper_for

    return fill(wrapper_for(str(body.get("module") or "")), produced=sheet.run,
                inputs_fill=False, **dict(body.get("slots") or {}))


# The flow model's exchange with each coupled body: the pair to its flopy class and mf6's exchange type.
_EXCHANGES = {("gwf", "gwt"): ("ModflowGwfgwt", "GWF6-GWT6"),
              ("gwf", "gwe"): ("ModflowGwfgwe", "GWF6-GWE6")}


def _exchange(flopy: Any, sim: Any, *, carrier: str, coupled: str) -> None:
    """The exchange joining the flow model to a coupled body, by the pair mf6 names."""
    if (carrier, coupled) not in _EXCHANGES:
        raise ModflowError(
            f"{carrier} and {coupled} solve no exchange this engine authors; the "
            "coupled pairs are the flow model with its transport and with its energy.",
            error_code="MODFLOW_EXCHANGE_UNKNOWN")
    cls, exgtype = _EXCHANGES[(carrier, coupled)]
    _package(flopy, cls, {}, owner=sim, exgtype=exgtype,
             exgmnamea=carrier, exgmnameb=coupled,
             filename=f"{carrier}.{carrier}{coupled}",
             how=f"the {carrier}-{coupled} exchange")


def _stressed(sheet: Any, groups: dict[str, dict[str, Any]], *, nlay: int, points: Any,
              cells: Any, utm_epsg: int | None, frame: Any, active: Any) -> set[str]:
    """Place each stress statement on the cells -> the identifiers it derived (its MAXBOUND).

    A statement's row becomes period 1's rows, carried forward by mf6; the bound is the
    placement's own count; ``active`` is the carrier's solved cells - the grid is one."""
    derived: set[str] = set()
    for prefix, rows in groups.items():
        row = rows.get("stress_period_data")
        if row is None or not isinstance(row.value, Mapping) \
                or "stress" not in row.value:
            continue
        placed = [cell for statement in row.value.get("statements", [row.value])
                  for cell in place_stress(statement, points=points, cells=cells,
                                           utm_epsg=utm_epsg, nlay=nlay, frame=frame,
                                           active=active)]
        rows["stress_period_data"] = replace(row, value={0: placed})
        rows["maxbound"] = Filled(
            slot=sheet.body.MODULE_INPUT[f"{prefix}_MAXBOUND"],
            value=len(placed), provenance=row.provenance)
        derived.add(f"{prefix}_MAXBOUND")
    return derived


def _active(sheet: Any, *, nlay: int, points: Any, cells: Any, utm_epsg: int | None,
            frame: Any) -> Any:
    """One row per layer of the cells the run solves, off a stated DISV_IDOMAIN; unstated, None (every cell)."""
    stated = sheet.filled.get("DISV_IDOMAIN")
    if stated is None:
        return None
    rows = place(stated.value, slot=stated.slot, nlay=nlay, run=sheet.run,
                 points=points, cells=cells, utm_epsg=utm_epsg, frame=frame)
    if isinstance(rows, (int, float)):
        return [[rows] * len(cells) for _ in range(nlay)]
    return [row if isinstance(row, (list, tuple)) else [row] * len(cells) for row in rows]


def _unstated(sheet: Any, prefixes: set[str], derived: set[str] = frozenset()) -> None:
    """Refuse every required block keyword of a written package the sheet leaves unstated."""
    missing = sorted(
        identifier for identifier, slot in sheet.body.MODULE_INPUT.items()
        if identifier.split("_", 1)[0] in prefixes - {_GRID}
        and slot.is_required and set(slot.rubrique) & _REQUIRED_BLOCKS
        and identifier not in sheet.filled and identifier not in derived)
    if missing:
        raise ModflowError(
            f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} "
            "required by mf6 and stated by nothing: state each from a "
            "measurement or a named source - the engine has no value for it.",
            error_code="MODFLOW_KEYWORD_UNSTATED")


def _placed(rows: dict[str, Any], *, dims: dict[str, int], points: Any,
            cells: Any, utm_epsg: int | None, sheet: Any, frame: Any,
            top: Any) -> dict[str, Any]:
    """Each keyword's constructor value - a cell ARRAY placed against the mesh's cells, the rest (an option's name list included) as stated."""
    return {
        name: (place(row.value, slot=row.slot, nlay=int(dims["nlay"]),
                     run=sheet.run, points=points, cells=cells,
                     utm_epsg=utm_epsg, frame=frame, top=top)
               if row.slot.type == "ARRAY" and "OPTIONS" not in row.slot.rubrique
               else row.value)
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
    for exchange in getattr(sim, "exchange_files", ()):
        exchange.write()
        written.append(Path(exchange.filename).name)
    return sorted(written)
