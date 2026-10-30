"""The simulation directory: mfsim.nam, its timing and solution, one file per package.

Every value comes off the sheet and the engine's own flopy does the formatting. The
accepted mesh's DISV is written by no one here - its bytes are staged from this run's
artifact, and this walk constructs a filename-only copy so the model's name file names
it, then writes around it package by package. Never ``write_simulation``: it would
overwrite the staged mesh with a package flopy knows nothing about.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

from . import DISV_FILE
from .accepted_mesh import mesh_artifact, mesh_nodes
from .griddata import frame_of, needs_botm, needs_top, place
from .mesh_files import _layering, botm_of, disv_dimensions, top_of, unstructured
from .movers import grouped, resolved_moves
from .observations import place_observations
from .refinement import Child, built_children, children_of, refined_idomain
from .release import place_release
from .stress import (GIVERS, hold_lakes, lake_statements, layer_rows, maw_statements, place as place_stress, place_interbeds,
                     place_lakes, place_maw, place_streams, place_uzf)

from ..errors import ModflowError
from ...runtime.sheet import Filled, fill

logger = logging.getLogger("trid3nt_server.workflows.modflow.authoring.simulation")

__all__ = ["serialize"]

# The packages mf6 refuses each model without (its own "package not specified" check):
# every one is written, so the sheet states their required keywords or the run refuses.
_REQUIRED_PACKAGES = {"gwf": ("IC", "NPF"), "gwt": ("IC", "MST"),
                      "gwe": ("IC", "EST"), "prt": ("MIP", "PRP")}

# The blocks mf6 refuses a written package without; a required keyword in one has no
# engine value, and flopy's constructor default would write an aquifer nobody measured.
_REQUIRED_BLOCKS = frozenset({"DIMENSIONS", "GRIDDATA", "PERIODDATA"})

_MODEL_KWARGS = "NAM"

_GRID = "DISV"

# A sheet stating DIS would answer the mesh's own question: this run's grid is the
# accepted mesh's DISV, or the DISU the sheet states whole.
_WRONG_GRID = ("DIS",)


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
    if unstructured(sheet):
        staged = sorted(identifier for identifier in sheet.filled if identifier.startswith("DISV_"))
        if staged or children_of(sheet):
            raise ModflowError(
                f"{', '.join(staged) or 'LocalGridRefinement'} states the accepted mesh's grid beside the DISU this run "
                "states; a run solves one grid, so state the DISU alone or the DISV alone.",
                error_code="MODFLOW_WRONG_GRID")
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
            "are gwf, gwt, gwe and prt.",
            error_code="MODFLOW_MODULE_UNKNOWN")
    if unstructured(sheet):
        return _write_unstructured(sheet, work, groups, named)
    points, cells, bed, _lonlat = mesh_nodes(mesh)
    dims = disv_dimensions(sheet, cells)
    utm_epsg = getattr((mesh or {}).get("artifact"), "utm_epsg", None)
    frame = frame_of(sheet)
    refined = bool(children_of(sheet))
    released = _released(sheet)
    if released and refined:
        raise ModflowError(
            "ParticleTracking tracks the carrier's grid, and a LocalGridRefinement moves the flow through its child "
            "model, which the particles never see; state one of them.", error_code="MODFLOW_EXCHANGE_UNKNOWN")
    top = (top_of(sheet, nlay=int(dims["nlay"]), art=mesh_artifact(mesh), bed=bed,
                  points=points, cells=cells)
           if refined or released or any(needs_top(row.value) for rows in groups.values()
                                         for row in rows.values()) else None)
    botm = (botm_of(sheet, nlay=int(dims["nlay"]), art=mesh_artifact(mesh), points=points,
                    cells=cells, top=top)
            if refined or released or any(needs_botm(row.value) for rows in groups.values()
                                          for row in rows.values()) else None)
    placed = dict(dims=dims, points=points, cells=cells, utm_epsg=utm_epsg,
                  sheet=sheet, frame=frame, top=top, botm=botm)
    active = _active(sheet, nlay=int(dims["nlay"]), points=points, cells=cells,
                     utm_epsg=utm_epsg, frame=frame)
    start = _start_head(sheet, nlay=int(dims["nlay"]), points=points, cells=cells,
                        utm_epsg=utm_epsg, frame=frame, top=top)
    grid = (_layering(sheet, nlay=int(dims["nlay"]), art=mesh_artifact(mesh), bed=bed,
                      points=points, cells=cells)[:2]
            if lake_statements(sheet) or maw_statements(sheet) else None)
    derived, tables, series, observed, features = _stressed(
        sheet, groups, nlay=int(dims["nlay"]), points=points, cells=cells, utm_epsg=utm_epsg, frame=frame,
        active=active, start=start, grid=grid)
    flow = _Model(name=module, sheet=sheet, groups=groups, named=named, derived=derived, tables=tables, series=series,
                  observed=observed, features=features, mesh=placed, disv=DISV_FILE, active=active)
    # A refinement's child is a flow model of its own on a grid cut from this one, written beside it.
    models = [flow]
    for child in built_children(sheet, points=points, cells=cells, utm_epsg=utm_epsg, nlay=int(dims["nlay"]),
                                top=top, botm=botm, idomain=active):
        models.append(_child(sheet, child, work, utm_epsg=utm_epsg, frame=frame))
    exchanges = _moved(models)
    for model in models:
        _unstated(model.sheet, set(model.groups) | set(_REQUIRED_PACKAGES[model.sheet.module]) | (
            {"TDIS"} if model is flow else set()), model.derived)
    sim = flopy.mf6.MFSimulation(sim_name="trid3nt", sim_ws=str(work),
                                 verbosity_level=0)
    _package(flopy, "ModflowTdis",
             _placed(groups.pop("TDIS", {}), **placed),
             owner=sim,
             how="TDIS of the simulation")
    carrying = {model.name: _carrying(model) for model in models}
    made = {model.name: _model(flopy, sim, model, solution=model is flow) for model in models}
    movers = []
    for model in models[1:]:
        movers.append(_joined(flopy, sim, made[flow.name], made[model.name], model, exchanges.get(model.name)))
        sim.register_ims_package(sim._solution_files[f"{flow.name}.ims"], [model.name])
    # Each coupled body is a second model in this simulation: its own packages from its slots, a filename-only
    # DISV on the staged mesh, its own solution, and the exchange that joins it to the flow; the transport of a
    # refinement is on the child's grid, in the solution of the run's transport.
    parents: dict[str, _Model] = {}
    for body in sheet.coupled:
        if "refine" in body or "of" in body:
            continue
        coupled = coupled_sheet(sheet, body)
        held = _coupled(flopy, sim, coupled, body, flow=made[flow.name], at=flow, active=active,
                        features=features, carrying=carrying[flow.name], join=None)
        parents[coupled.module] = held
        _exchange(flopy, sim, carrier=flow.name, coupled=held.name)
    for body in sheet.coupled:
        if "of" not in body:
            continue
        home = next(model for model in models if model.name == body["of"])
        if "gwt" not in parents:
            raise ModflowError(
                f"{body['name']} is the transport of the refinement {home.name}, and this run couples no gwt body to "
                "exchange it with; state Transport.", error_code="MODFLOW_EXCHANGE_UNKNOWN")
        held = _coupled(flopy, sim, coupled_sheet(sheet, body), body, flow=made[home.name], at=home,
                        active=home.active, features=home.features, carrying=carrying[home.name], join=parents["gwt"])
        _exchange(flopy, sim, carrier=home.name, coupled=held.name, kinds=("gwf", "gwt"))
        cells = home.child.exchange
        _package(flopy, "ModflowGwtgwt", {**({"auxiliary": ["ANGLDEGX", "CDIST"]} if home.child.auxiliary else {})},
                 owner=sim, exgtype="GWT6-GWT6", gwfmodelname1=flow.name, gwfmodelname2=home.name,
                 exgmnamea=parents["gwt"].name, exgmnameb=held.name, nexg=len(cells), exchangedata=cells,
                 filename=f"{parents['gwt'].name}-{held.name}.exg", how=f"the {parents['gwt'].name}-{held.name} exchange")
    return _write_package_by_package(sim, [mover for mover in movers if mover is not None])


def _released(sheet: Any) -> bool:
    """Whether a PRT body of the sheet states Release statements: their heights read the grid's top and bottoms."""
    return any(isinstance(held := (body.get("slots") or {}).get("PRP_PACKAGEDATA"), Mapping) and "release" in held
               for body in sheet.coupled)


def _carrying(model: _Model) -> list[str]:
    """The prefixes of the flow packages that carry a CONCENTRATION: the water sources a transport model's SSM names."""
    return [prefix for prefix, rows in model.groups.items()
            if "CONCENTRATION" in map(str, getattr(rows.get("auxiliary"), "value", None) or ())]


def _coupled(flopy: Any, sim: Any, coupled: Any, body: Mapping[str, Any], *, flow: Any, at: _Model,
             active: Any, features: list[Mapping[str, Any]], carrying: list[str], join: _Model | None) -> _Model:
    """A transport or energy body placed on the cells of ``at`` and written as a model; ``join`` is the body whose solution it shares."""
    name = str(body.get("name") or coupled.module)
    groups, named = _grouped(coupled)
    if join is not None and "IMS" in groups:
        raise ModflowError(
            f"{name} states IMS keywords, and a refinement's transport solves in the solution of {join.name}; state "
            f"the solver once, on {join.name}.", error_code="MODFLOW_KEYWORD_SHAPE")
    grid = at.mesh
    derived, tables, series, observed, _features = _stressed(
        coupled, groups, nlay=int(grid["dims"]["nlay"]), points=grid["points"], cells=grid["cells"],
        utm_epsg=grid["utm_epsg"], frame=grid["frame"], active=active, features=features, name=name, stem=at.stem,
        grid=(grid["top"], grid["botm"]))
    _unstated(coupled, set(groups) | set(_REQUIRED_PACKAGES[coupled.module]), derived)
    sources = [[flow.get_package(prefix.lower()).package_name, "AUX", "CONCENTRATION"] for prefix in carrying]
    if coupled.module == "gwt" and sources and "sources" not in groups.get("SSM", {}):
        groups.setdefault("SSM", {})
    held = _Model(name=name, sheet=coupled, groups=groups, named=named, derived=derived, tables=tables, series=series,
                  observed=observed, features=[], mesh=grid, disv=at.disv, stem=at.stem)
    _model(flopy, sim, held, solution=join is None, sources=sources if coupled.module == "gwt" else ())
    if join is not None:
        sim.register_ims_package(sim._solution_files[f"{join.name}.ims"], [name])
    return held


# The statements that place on the accepted mesh's cells: a DISU run's cells are node numbers the sheet states.
_PLACING = ("stress", "streams", "lakes", "wells", "zones", "interbeds", "movers", "observations")


def _write_unstructured(sheet: Any, work: Path, groups: dict[str, dict[str, Any]], named: dict[str, Any]) -> list[str]:
    """The simulation of a run whose grid is the DISU its sheet states: every keyword is written as stated."""
    import flopy

    module = str(sheet.module)
    if sheet.coupled:
        raise ModflowError(
            "a run on a DISU couples no other model: its transport, energy and refinement bodies are placed on the "
            "accepted mesh's cells.", error_code="MODFLOW_WRONG_GRID")
    for prefix, rows in groups.items():
        for name, row in rows.items():
            if isinstance(row.value, Mapping) and set(row.value) & set(_PLACING):
                raise ModflowError(
                    f"{prefix}_{name.upper()} states a statement that places on the accepted mesh's cells, and this "
                    "run's grid is the DISU its sheet states; state the cells as node numbers, one integer per cell id.",
                    error_code="MODFLOW_WRONG_GRID")
    _unstated(sheet, set(groups) | set(_REQUIRED_PACKAGES[module]) | {"TDIS"}, set())
    sim = flopy.mf6.MFSimulation(sim_name="trid3nt", sim_ws=str(work), verbosity_level=0)
    _package(flopy, "ModflowTdis", _stated(groups.pop("TDIS", {})), owner=sim, how="TDIS of the simulation")
    _model(flopy, sim, _Model(name=module, sheet=sheet, groups=groups, named=named, derived=set(), tables={}, series={},
                              observed={}, features=[], mesh=None, disv=f"{module}.disu"), solution=True)
    return _write_package_by_package(sim, [])


def _stated(rows: dict[str, Any]) -> dict[str, Any]:
    return {name: row.value for name, row in rows.items()}


@dataclass
class _Model:
    """One model of the simulation, its statements placed: what ``_model`` writes it from."""

    name: str
    sheet: Any
    groups: dict[str, dict[str, Any]]
    named: dict[str, Any]
    derived: set[str]
    tables: dict[str, list]
    series: dict[str, Mapping[str, Any]]
    observed: dict[str, Mapping[str, Any]]
    features: list[Mapping[str, Any]]
    mesh: dict[str, Any] | None
    disv: str
    stem: str = ""
    options: Mapping[str, Any] = field(default_factory=dict)
    child: Any = None
    active: Any = None


def _child(sheet: Any, child: Child, work: Path, *, utm_epsg: int | None, frame: Any) -> _Model:
    """A refinement's model: its DISV written beside the run, its statements placed on its cells."""
    from .mesh_files import _disv_options, _gridprops, _write_disv

    body = next(item for item in children_of(sheet) if item["name"] == child.name)
    coupled = coupled_sheet(sheet, body)
    groups, named = _grouped(coupled)
    if "IMS" in groups:
        raise ModflowError(
            f"{child.name} states IMS keywords, and a refinement solves in the carrier's solution; state the solver "
            "once, on the carrier.", error_code="MODFLOW_KEYWORD_SHAPE")
    nlay = len(child.botm)
    dims = {"nlay": nlay, "ncpl": len(child.cells)}
    _write_disv(work, filename=f"{child.name}.disv", nlay=nlay, top=child.top, botm=child.botm,
                idomain=child.idomain, gridprops=_gridprops(child.points, child.cells),
                options=_disv_options(sheet))
    active = layer_rows(child.idomain, nlay=nlay, ncpl=len(child.cells))
    mesh = dict(dims=dims, points=child.points, cells=child.cells, utm_epsg=utm_epsg, sheet=coupled, frame=frame,
                top=child.top, botm=child.botm)
    start = _start_head(coupled, nlay=nlay, points=child.points, cells=child.cells, utm_epsg=utm_epsg, frame=frame,
                        top=child.top)
    grid = (child.top, child.botm) if lake_statements(coupled) or maw_statements(coupled) else None
    derived, tables, series, observed, features = _stressed(
        coupled, groups, nlay=nlay, points=child.points, cells=child.cells, utm_epsg=utm_epsg, frame=frame,
        active=active, start=start, grid=grid, name=child.name, stem=f"{child.name}.")
    return _Model(name=child.name, sheet=coupled, groups=groups, named=named, derived=derived, tables=tables,
                  series=series, observed=observed, features=features, mesh=mesh, disv=f"{child.name}.disv",
                  stem=f"{child.name}.", options=body["refine"].get("exchange") or {}, child=child, active=active)


def _model(flopy: Any, sim: Any, held: _Model, *, solution: bool, sources: Any = ()) -> Any:
    """One model of this simulation: its name file, its DISV's line, its solution and its packages; the TAB6 files a package reads, its time-series file and its own OBS file come with ``held``."""
    module, name = str(held.sheet.module), held.name
    groups, mesh = held.groups, held.mesh
    placing = (lambda rows: _placed(rows, **mesh)) if mesh else _stated
    cap = module.capitalize()
    model = _package(flopy, f"Modflow{cap}", held.named, owner=sim, modelname=name,
                     model_nam_file=f"{name}.nam",
                     how=f"the {name} model itself")
    # The grid is the staged mesh's: constructed for the name file's line alone and
    # the dimension integers flopy shapes arrays against; mesh_files wrote its bytes,
    # and the DISV rows themselves were read there.
    groups.pop(_GRID, None)
    if mesh is None:
        _package(flopy, f"Modflow{cap}disu", _stated(groups.pop("DISU")), owner=model, filename=held.disv,
                 how=f"the {name} DISU")
    else:
        _package(flopy, f"Modflow{cap}disv", {}, owner=model, filename=held.disv,
                 **mesh["dims"], how=f"the staged {held.disv}")
    if solution and module == "prt":
        ems = _package(flopy, "ModflowEms", {}, owner=sim, filename=f"{name}.ems", how=f"EMS of the {name} solution")
        sim.register_solution_package(ems, [name])
    elif solution:
        ims = _package(flopy, "ModflowIms",
                       placing(groups.pop("IMS", {})) if mesh is None else
                       _placed(groups.pop("IMS", {}), dims=mesh["dims"], points=mesh["points"],
                               cells=mesh["cells"], utm_epsg=mesh["utm_epsg"], sheet=mesh["sheet"],
                               frame=mesh["frame"], top=mesh["top"]),
                       owner=sim, filename=f"{name}.ims",
                       how=f"IMS of the {name} solution")
        sim.register_ims_package(ims, [name])
    for prefix, rows in sorted(groups.items(), key=lambda item: item[0] == "MVR"):
        keywords = placing(rows)
        if prefix == "SSM" and sources:
            keywords = {"sources": list(sources), **keywords}
        if prefix == "MVR":
            keywords = _named_movers(keywords, model)
        package = _package(flopy, "ModflowUtlobs" if prefix == "OBS" else f"Modflow{cap}{prefix.lower()}", keywords,
                           owner=model, how=f"{prefix} of {name}")
        if prefix in held.observed:
            seen = held.observed[prefix]
            package.obs.initialize(filename=f"{name}.{prefix.lower()}.obs", digits=seen.get("digits"),
                                   continuous=seen["continuous"])
        if prefix in held.series:
            seen = held.series[prefix]
            package.ts.initialize(filename=f"{held.stem}{prefix.lower()}.ts", timeseries=seen["rows"],
                                  time_series_namerecord=seen["names"],
                                  interpolation_methodrecord=seen["interpolation"], sfacrecord=seen["sfac"])
        for filename, table in held.tables.get(prefix, ()):
            _package(flopy, f"ModflowUtl{prefix.lower()}tab", {"nrow": len(table), "ncol": len(table[0]),
                                                               "table": table, "filename": filename},
                     owner=package, how=f"the {prefix} table {filename}")
    return model


def _named_movers(keywords: dict[str, Any], model: Any) -> dict[str, Any]:
    """The mover rows with each package prefix replaced by the name the flow model gave the package."""
    names = {prefix: model.get_package(prefix.lower()).package_name for (prefix,) in keywords["packages"]}
    return {**keywords, "packages": [[names[prefix]] for (prefix,) in keywords["packages"]],
            "perioddata": {period: [[names[a], i, names[b], j, kind, value] for a, i, b, j, kind, value in rows]
                           for period, rows in keywords["perioddata"].items()}}


def coupled_sheet(sheet: Any, body: Mapping[str, Any]) -> Any:
    """A coupled body's slots as the sheet its own authoring walks: the shared run, the body's own dictionary."""
    from ..modules import wrapper_for

    return fill(wrapper_for(str(body.get("module") or "")), produced=sheet.run,
                inputs_fill=False, **dict(body.get("slots") or {}))


# The flow model's exchange with each coupled body: the pair to its flopy class and mf6's exchange type.
_EXCHANGES = {("gwf", "gwt"): ("ModflowGwfgwt", "GWF6-GWT6"),
              ("gwf", "gwe"): ("ModflowGwfgwe", "GWF6-GWE6"),
              ("gwf", "prt"): ("ModflowGwfprt", "GWF6-PRT6")}


def _exchange(flopy: Any, sim: Any, *, carrier: str, coupled: str, kinds: tuple[str, str] | None = None) -> None:
    """The exchange joining the flow model to a coupled body, by the pair mf6 names; ``kinds`` are the two modules where the models carry names of their own."""
    pair = kinds or (carrier, coupled)
    if pair not in _EXCHANGES:
        raise ModflowError(
            f"{pair[0]} and {pair[1]} solve no exchange this engine authors; the "
            "coupled pairs are the flow model with its transport and with its energy.",
            error_code="MODFLOW_EXCHANGE_UNKNOWN")
    cls, exgtype = _EXCHANGES[pair]
    _package(flopy, cls, {}, owner=sim, exgtype=exgtype,
             exgmnamea=carrier, exgmnameb=coupled,
             filename=f"{carrier}.{carrier}{coupled}" if kinds is None else f"{carrier}-{coupled}.exg",
             how=f"the {carrier}-{coupled} exchange")


def _start_head(sheet: Any, *, nlay: int, points: Any, cells: Any, utm_epsg: int | None,
                frame: Any, top: Any) -> Any:
    """The run's starting head per layer and cell off a stated IC_STRT, or None; a stress statement may take it as its head."""
    stated = sheet.filled.get("IC_STRT")
    if stated is None:
        return None
    return place(stated.value, slot=stated.slot, nlay=nlay, run=sheet.run, points=points,
                 cells=cells, utm_epsg=utm_epsg, frame=frame, top=top)


def _stressed(sheet: Any, groups: dict[str, dict[str, Any]], *, nlay: int, points: Any,
              cells: Any, utm_epsg: int | None, frame: Any, active: Any,
              start: Any = None, grid: Any = None, features: list[Mapping[str, Any]] | None = None,
              name: str | None = None, stem: str = ""
              ) -> tuple[set[str], dict[str, list], dict[str, Mapping[str, Any]], dict[str, Mapping[str, Any]],
                         list[Mapping[str, Any]]]:
    """Place each stress statement on the cells -> the identifiers it derived (its MAXBOUND), the TAB6 files its packages read, the time series they read, the OBS files packages state and the placed features.

    ``features`` are a flow model's placed features, which a coupled body's observations name; ``name`` is the
    model the observation files are named for and ``stem`` what its table files begin with.

    A statement's row becomes period 1's rows, carried forward by mf6; the bound is the
    placement's own count; ``active`` is the carrier's solved cells - the grid is one."""
    # a lake's water stands where the aquifer's cell is not solved, so no other package places a row there
    lakeless = active
    if grid is not None and (lakes := lake_statements(sheet)):
        active = hold_lakes(lakes, active if active is not None else [[1] * len(cells) for _ in range(nlay)],
                            points=points, cells=cells, utm_epsg=utm_epsg, nlay=nlay, frame=frame,
                            top=grid[0], botm=grid[1])
    derived: set[str] = set()
    features = list(features or [])
    used: dict[str, set[str]] = {}
    for prefix, rows in groups.items():
        row = rows.get("stress_period_data")
        if row is None or not isinstance(row.value, Mapping) \
                or "stress" not in row.value:
            continue
        placed: list[tuple] = []
        for statement in row.value.get("statements", [row.value]):
            found = place_stress(statement, points=points, cells=cells, utm_epsg=utm_epsg, nlay=nlay, frame=frame,
                                 active=active, start=start)
            if prefix in GIVERS:
                features += [{"package": prefix, "name": statement.get("name"), "number": len(placed) + rank,
                              "cells": {item[0][1]}, "layer": item[0][0]} for rank, item in enumerate(found)]
            placed += found
        if (names := {item for line in placed for item in line[1:] if isinstance(item, str)}):
            used[prefix] = names
        rows["stress_period_data"] = replace(row, value={0: placed})
        rows["maxbound"] = Filled(
            slot=sheet.body.MODULE_INPUT[f"{prefix}_MAXBOUND"],
            value=len(placed), provenance=row.provenance)
        derived.add(f"{prefix}_MAXBOUND")
    tables: dict[str, list] = {}
    row = groups.get("SFR", {}).get("packagedata")
    if row is not None and isinstance(row.value, Mapping) and "streams" in row.value:
        found, tables["SFR"] = _streamed(sheet, groups["SFR"], row, stem=stem, nlay=nlay, points=points, cells=cells,
                                         utm_epsg=utm_epsg, frame=frame, active=active)
        features += found
        derived |= {"SFR_NREACHES", "SFR_CONNECTIONDATA"}
    elif isinstance(getattr(groups.get("SFR", {}).get("diversions"), "value", None), Mapping):
        raise ModflowError(
            "Diversions move water between the reaches the Streams lines place; this run states SFR "
            "reaches another way, so state the lines.", error_code="MODFLOW_KEYWORD_SHAPE")
    row = groups.get("LAK", {}).get("packagedata")
    if row is not None and isinstance(row.value, Mapping) and "lakes" in row.value:
        found, tables["LAK"] = _lakened(sheet, groups["LAK"], row, stem=stem, nlay=nlay, points=points, cells=cells,
                                        utm_epsg=utm_epsg, frame=frame, active=lakeless, top=grid[0], botm=grid[1])
        features += found
        derived |= {"LAK_NLAKES", "LAK_NOUTLETS", "LAK_NTABLES", "LAK_CONNECTIONDATA"}
        if not found:
            del groups["LAK"]
    row = groups.get("MAW", {}).get("packagedata")
    if row is not None and isinstance(row.value, Mapping) and "wells" in row.value:
        features += _welled(sheet, groups["MAW"], row, nlay=nlay, points=points, cells=cells,
                            utm_epsg=utm_epsg, active=active, top=grid[0], botm=grid[1])
        derived |= {"MAW_NMAWWELLS", "MAW_CONNECTIONDATA", "MAW_PERIODDATA"}
    row = groups.get("UZF", {}).get("packagedata")
    if row is not None and isinstance(row.value, Mapping) and "zones" in row.value:
        features += _unsaturated(sheet, groups, row, nlay=nlay, points=points, cells=cells, utm_epsg=utm_epsg,
                                 frame=frame, active=active)
        derived |= {"UZF_NUZFCELLS", "UZF_PERIODDATA"}
    row = groups.get("CSUB", {}).get("packagedata")
    if row is not None and isinstance(row.value, Mapping) and "interbeds" in row.value:
        features += _interbedded(sheet, groups["CSUB"], row, nlay=nlay, points=points, cells=cells,
                                 utm_epsg=utm_epsg, frame=frame, active=active)
        derived.add("CSUB_NINTERBEDS")
    row = groups.get("PRP", {}).get("packagedata")
    if row is not None and isinstance(row.value, Mapping) and "release" in row.value:
        found = place_release(row.value["release"], points=points, cells=cells, utm_epsg=utm_epsg, nlay=nlay,
                              top=grid[0], botm=grid[1], active=active, features=features)
        groups["PRP"]["packagedata"] = replace(row, value=found)
        groups["PRP"]["nreleasepts"] = Filled(
            slot=sheet.body.MODULE_INPUT["PRP_NRELEASEPTS"], value=len(found), provenance=row.provenance)
        derived.add("PRP_NRELEASEPTS")
    series: dict[str, Mapping[str, Any]] = {}
    for prefix, rows in groups.items():
        held = rows.pop("ts_filerecord", None)
        if held is None:
            continue
        if not (isinstance(held.value, Mapping) and "series" in held.value):
            raise ModflowError(
                f"{prefix}_TS_FILERECORD names a time-series file this authoring would not write; state the series "
                "through TimeSeries.", error_code="MODFLOW_KEYWORD_SHAPE")
        series[prefix] = held.value["series"]
    _named_series(used, series)
    observed = _observations(sheet, groups, features, module=name or str(sheet.module), nlay=nlay, points=points,
                             cells=cells, utm_epsg=utm_epsg, active=active)
    return derived, {prefix: files for prefix, files in tables.items() if files}, series, observed, features


def _named_series(used: Mapping[str, set[str]], series: Mapping[str, Mapping[str, Any]]) -> None:
    """Refuse a stress statement naming a series its package's TimeSeries does not state."""
    for prefix, names in used.items():
        known = set(series.get(prefix, {}).get("names", ()))
        if names - known:
            raise ModflowError(
                f"{prefix} names the series {', '.join(sorted(names - known))}, and the {prefix} TimeSeries states "
                + (", ".join(sorted(known)) if known else "none") + ".", error_code="MODFLOW_STRESS_UNSTATED")


def _observations(sheet: Any, groups: dict[str, dict[str, Any]], features: list[Mapping[str, Any]], *, module: str,
                  nlay: int, **mesh: Any) -> dict[str, Mapping[str, Any]]:
    """Place every Observations statement: the model's OBS rows are set on its group, a package's returned by prefix for its own OBS file."""
    observed: dict[str, Mapping[str, Any]] = {}
    for prefix, rows in groups.items():
        package = None if prefix == "OBS" else prefix
        row = rows.get("continuous") if package is None else rows.pop("obs_filerecord", None)
        if row is None:
            continue
        stated = row.value if isinstance(row.value, Mapping) else {}
        if "observations" in stated:
            files, digits = place_observations(stated["observations"], module=module, package=package,
                                               features=features, nlay=nlay, **mesh)
            if package is not None:
                observed[prefix] = {"continuous": files, "digits": digits}
                continue
            if digits is not None:
                if "digits" in rows:
                    raise ModflowError(
                        "OBS_DIGITS is stated beside Observations digits; state the option once.",
                        error_code="MODFLOW_KEYWORD_SHAPE")
                rows["digits"] = Filled(slot=sheet.body.MODULE_INPUT["OBS_DIGITS"], value=digits,
                                        provenance=row.provenance)
            rows["continuous"] = replace(row, value=files)
        elif package is None and stated:
            continue
        elif package is not None and "continuous" in stated:
            observed[prefix] = dict(stated)
        else:
            raise ModflowError(
                f"{prefix}_OBS_FILERECORD names an observation file this authoring would not write; state the "
                "observations through Observations, or {\"continuous\": {file: rows}}."
                if package else "OBS_CONTINUOUS is {file: rows} or the Observations statements.",
                error_code="MODFLOW_KEYWORD_SHAPE")
    return observed


def _interbedded(sheet: Any, rows: dict[str, Any], row: Any, **mesh: Any) -> list[Mapping[str, Any]]:
    """Place the Interbeds statements: the package's interbed list and its count; each interbed is a feature."""
    stated = sorted(f"CSUB_{name.upper()}" for name in ("ninterbeds",) if name in rows)
    if stated:
        raise ModflowError(
            f"{', '.join(stated)} is derived from the Interbeds statements; state either the statements or the "
            "CSUB keywords, not both.", error_code="MODFLOW_KEYWORD_SHAPE")
    packagedata = place_interbeds(row.value["interbeds"], boundnames=bool(getattr(rows.get("boundnames"), "value", False)),
                                  **mesh)
    for name, value in (("packagedata", packagedata), ("ninterbeds", len(packagedata))):
        rows[name] = Filled(slot=sheet.body.MODULE_INPUT[f"CSUB_{name.upper()}"], value=value, provenance=row.provenance)
    named = bool(getattr(rows.get("boundnames"), "value", False))
    return [{"package": "CSUB", "name": line[-1] if named else None, "number": line[0], "cells": {line[1][1]}}
            for line in packagedata]


def _moved(models: list[_Model]) -> dict[str, dict[str, Any]]:
    """Resolve every WaterMover statement across the flow models -> the movers each child's exchange carries, by child name.

    A mover whose two ends sit in one model is that model's MVR package (its rows, package list and counts, and the MOVER
    option of each package used); one whose ends sit in the carrier and a child is the exchange's. A statement may be
    stated on any model's deck."""
    stating = [(model, model.groups["MVR"]["perioddata"]) for model in models
               if isinstance(getattr(model.groups.get("MVR", {}).get("perioddata"), "value", None), Mapping)
               and "movers" in model.groups["MVR"]["perioddata"].value]
    if not stating:
        return {}
    for model, _row in stating:
        stated = sorted(f"MVR_{name.upper()}" for name in ("maxmvr", "maxpackages", "packages")
                        if name in model.groups["MVR"])
        if stated:
            raise ModflowError(
                f"{', '.join(stated)} {'is' if len(stated) == 1 else 'are'} derived from the "
                "WaterMover statements; state either the statements or the MVR keywords, not both.",
                error_code="MODFLOW_KEYWORD_SHAPE")
    carrier = models[0]
    flows = {model.name: {"features": model.features, "points": model.mesh["points"], "cells": model.mesh["cells"],
                          "utm_epsg": model.mesh["utm_epsg"]} for model in models}
    moves = resolved_moves([item for _model_, row in stating for item in row.value["movers"]], flows,
                           home=carrier.name)
    by_models = grouped(moves, lambda move: tuple(sorted({move["from"][0], move["to"][0]})))
    provenance = stating[0][1].provenance
    exchanged: dict[str, dict[str, Any]] = {}
    for key, (used, periods) in by_models.items():
        mine = [move for move in moves if tuple(sorted({move["from"][0], move["to"][0]})) == key]
        for move in mine:
            for model_name, package, _number in (move["from"], move["to"]):
                _movable(next(model for model in models if model.name == model_name), package, provenance)
        if len(key) == 2:
            if carrier.name not in key:
                raise ModflowError(
                    f"WaterMover moves water between {key[0]} and {key[1]}, and no exchange joins them; a refinement "
                    f"is exchanged with {carrier.name} alone.", error_code="MODFLOW_STRESS_UNSTATED")
            child = key[0] if key[1] == carrier.name else key[1]
            exchanged[child] = {
                "packages": list(dict.fromkeys((name, package) for move in mine
                                               for name, package, _n in (move["from"], move["to"]))),
                "periods": periods, "maxmvr": len(mine)}
            continue
        model = next(model for model in models if model.name == key[0])
        rows = model.groups.setdefault("MVR", {})
        for name, value in {"perioddata": periods, "maxmvr": len(mine), "maxpackages": len(used),
                            "packages": [[package] for package in used]}.items():
            rows[name] = Filled(slot=model.sheet.body.MODULE_INPUT[f"MVR_{name.upper()}"], value=value,
                                provenance=provenance)
        model.derived |= {"MVR_MAXMVR", "MVR_MAXPACKAGES"}
    inside = {key[0] for key in by_models if len(key) == 1}
    for model, row in stating:
        if model.name in inside:
            continue
        rest = sorted(f"MVR_{name.upper()}" for name in model.groups["MVR"] if name != "perioddata")
        if rest:
            raise ModflowError(
                f"{', '.join(rest)} is stated on {model.name}, and every WaterMover statement of the run moves water "
                f"across its exchange; {model.name} writes no MVR package of its own.",
                error_code="MODFLOW_KEYWORD_SHAPE")
        del model.groups["MVR"]
    return exchanged


def _movable(model: _Model, package: str, provenance: Any) -> None:
    """Set the MOVER option of a package that gives or takes water through the mover."""
    held = model.groups.get(package, {}).get("mover")
    if held is not None and not held.value:
        raise ModflowError(f"{package}_MOVER is stated false, and WaterMover moves water through the {package} package.",
                           error_code="MODFLOW_KEYWORD_SHAPE")
    model.groups[package]["mover"] = Filled(slot=model.sheet.body.MODULE_INPUT[f"{package}_MOVER"], value=True,
                                            provenance=provenance)


def _joined(flopy: Any, sim: Any, parent: Any, child: Any, held: _Model, moved: Mapping[str, Any] | None) -> Any:
    """The GWF-GWF exchange between the carrier and a refinement, its cells from Lgr -> the MVR package of the movers that cross it, or None."""
    cells = held.child.exchange
    options = {**held.options, **({"auxiliary": ["ANGLDEGX", "CDIST"]} if held.child.auxiliary else {})}
    stem = f"{parent.name}-{child.name}"
    exchange = _package(flopy, "ModflowGwfgwf", options, owner=sim, exgtype="GWF6-GWF6", exgmnamea=parent.name,
                        exgmnameb=child.name, nexg=len(cells), exchangedata=cells, filename=f"{stem}.exg",
                        **({"mvr_filerecord": f"{stem}.mvr"} if moved else {}),
                        how=f"the {parent.name}-{child.name} exchange")
    if not moved:
        return None
    made = {parent.name: parent, child.name: child}
    named = lambda model, package: made[model].get_package(package.lower()).package_name
    return _package(flopy, "ModflowMvr", {
        "modelnames": True, "maxmvr": moved["maxmvr"], "maxpackages": len(moved["packages"]),
        "packages": [[model, named(model, package)] for model, package in moved["packages"]],
        "perioddata": {period: [[a, named(a, pa), i, b, named(b, pb), j, kind, value]
                                for a, pa, i, b, pb, j, kind, value in rows]
                       for period, rows in moved["periods"].items()}},
        owner=exchange, filename=f"{stem}.mvr", how=f"the movers across the {parent.name}-{child.name} exchange")


def _streamed(sheet: Any, rows: dict[str, Any], row: Any, *, stem: str = "", **mesh: Any) -> tuple[list[Mapping[str, Any]], list]:
    """Cut the Streams lines into reaches: the package's reach list, its connections, its count, its diversions, its cross-section files and its period settings."""
    stated = sorted(f"SFR_{name.upper()}" for name in ("nreaches", "connectiondata") if name in rows)
    if stated:
        raise ModflowError(
            f"{', '.join(stated)} {'is' if len(stated) == 1 else 'are'} derived from the "
            "Streams lines; state either the lines or the reach keywords, not both.",
            error_code="MODFLOW_KEYWORD_SHAPE")
    diverting = rows.get("diversions")
    diversions = diverting.value["diversions"] if isinstance(getattr(diverting, "value", None), Mapping) else []
    stated = sorted(f"SFR_{name.upper()}" for name, held in (("diversions", None if diversions else diverting),
                                                            ("crosssections", rows.get("crosssections")))
                    if held is not None)
    if stated:
        raise ModflowError(
            f"{', '.join(stated)} {'is' if len(stated) == 1 else 'are'} derived from the Diversions statements and "
            "the Streams cross_section; state those, not the reach keyword.", error_code="MODFLOW_KEYWORD_SHAPE")
    packagedata, connectiondata, perioddata, divisions, features = place_streams(
        row.value["streams"], auxiliary=_declared(rows), diversions=diversions, **mesh)
    if perioddata and "perioddata" in rows:
        raise ModflowError(
            "SFR_PERIODDATA is derived from the period settings the Streams lines state; state "
            "either those or the reach keywords' period rows, not both.", error_code="MODFLOW_KEYWORD_SHAPE")
    files: dict[tuple, str] = {}
    for reach in features:
        if reach["table"] is not None:
            files.setdefault(tuple(map(tuple, reach["table"])), f"{stem}sfr_xs{len(files) + 1}.tab")
    placed = {"packagedata": packagedata, "connectiondata": connectiondata, "nreaches": len(packagedata),
              **({"perioddata": perioddata} if perioddata else {}),
              **({"diversions": divisions} if divisions else {}),
              **({"crosssections": [[reach["number"], files[tuple(map(tuple, reach["table"]))]]
                                    for reach in features if reach["table"] is not None]} if files else {})}
    for name, value in placed.items():
        rows[name] = Filled(slot=sheet.body.MODULE_INPUT[f"SFR_{name.upper()}"],
                            value=value, provenance=row.provenance)
    return features, [(name, [list(line) for line in table]) for table, name in files.items()]


def _declared(rows: dict[str, Any]) -> tuple[str, ...]:
    """The auxiliary names a package's AUXILIARY option states."""
    return tuple(str(name) for name in getattr(rows.get("auxiliary"), "value", None) or ())


def _lakened(sheet: Any, rows: dict[str, Any], row: Any, *, stem: str = "", **mesh: Any) -> tuple[list[Mapping[str, Any]], list]:
    """Place the Lakes polygons: the package's lake list, its connections, its count, its outlets, its table files and the period settings stated."""
    packagedata, connectiondata, perioddata, outlets, features = place_lakes(
        row.value["lakes"], auxiliary=_declared(rows), **mesh)
    tabled = [lake for lake in features if lake["table"] is not None]
    derived = {"nlakes": True, "connectiondata": True, "perioddata": bool(perioddata),
               "noutlets": bool(outlets), "outlets": bool(outlets), "ntables": bool(tabled), "tables": bool(tabled)}
    stated = [f"LAK_{name.upper()}" for name, placed in derived.items() if placed and name in rows]
    if stated:
        raise ModflowError(
            f"{', '.join(stated)} {'is' if len(stated) == 1 else 'are'} derived from the "
            "Lakes polygons; state either the polygons or the lake keywords, not both.",
            error_code="MODFLOW_KEYWORD_SHAPE")
    placed = {"packagedata": packagedata, "connectiondata": connectiondata,
              "nlakes": len(packagedata)}
    if perioddata:
        placed["perioddata"] = perioddata
    if outlets:
        placed.update(outlets=outlets, noutlets=len(outlets))
    if tabled:
        placed.update(tables=[[lake["number"], f"{stem}lak_tab{lake['number'] + 1}.tab"] for lake in tabled],
                      ntables=len(tabled))
    for name, value in placed.items():
        rows[name] = Filled(slot=sheet.body.MODULE_INPUT[f"LAK_{name.upper()}"],
                            value=value, provenance=row.provenance)
    return features, [(f"{stem}lak_tab{lake['number'] + 1}.tab", [list(line) for line in lake["table"]]) for lake in tabled]


def _welled(sheet: Any, rows: dict[str, Any], row: Any, **mesh: Any) -> list[Mapping[str, Any]]:
    """Place the MultiAquiferWells points: the package's well list, its connections, its count and each well's period settings."""
    stated = sorted(f"MAW_{name.upper()}" for name in ("nmawwells", "connectiondata", "perioddata")
                    if name in rows)
    if stated:
        raise ModflowError(
            f"{', '.join(stated)} {'is' if len(stated) == 1 else 'are'} derived from the "
            "MultiAquiferWells points; state either the points or the well keywords, not both.",
            error_code="MODFLOW_KEYWORD_SHAPE")
    packagedata, connectiondata, perioddata, features = place_maw(
        row.value["wells"], auxiliary=_declared(rows), flowing_wells=bool(getattr(rows.get("flowing_wells"), "value", False)),
        **mesh)
    for name, value in (("packagedata", packagedata), ("connectiondata", connectiondata),
                        ("perioddata", perioddata), ("nmawwells", len(packagedata))):
        rows[name] = Filled(slot=sheet.body.MODULE_INPUT[f"MAW_{name.upper()}"],
                            value=value, provenance=row.provenance)
    return features


def _unsaturated(sheet: Any, groups: dict[str, dict[str, Any]], row: Any, **mesh: Any) -> list[Mapping[str, Any]]:
    """Place the UnsaturatedZone polygons: the package's UZF cells, their count and the land cells' period rows."""
    rows = groups["UZF"]
    stated = sorted(f"UZF_{name.upper()}" for name in ("nuzfcells", "perioddata") if name in rows)
    if stated:
        raise ModflowError(
            f"{', '.join(stated)} {'is' if len(stated) == 1 else 'are'} derived from the "
            "UnsaturatedZone polygons; state either the polygons or the UZF keywords, not both.",
            error_code="MODFLOW_KEYWORD_SHAPE")
    options = {name: bool(getattr(rows.get(name), "value", False))
               for name in ("simulate_et", "unsat_etwc", "unsat_etae")}
    packagedata, perioddata, features = place_uzf(row.value["zones"], options=options, auxiliary=_declared(rows), **mesh)
    recharged = {cell[-1] for cell in _recharged(groups) if isinstance(cell, (list, tuple))}
    doubled = sorted(item[1][1] for item in packagedata if item[2] and item[1][1] in recharged)
    if doubled:
        raise ModflowError(
            f"UnsaturatedZone and Recharge both put water on cell {doubled[0]}"
            + (f" and {len(doubled) - 1} more" if len(doubled) > 1 else "")
            + "; the same infiltration counted twice. State the recharge from the UZF infiltration "
            "(finf) or the RCH package over a cell, not both.", error_code="MODFLOW_KEYWORD_SHAPE")
    for name, value in (("packagedata", packagedata), ("perioddata", perioddata),
                        ("nuzfcells", len(packagedata))):
        rows[name] = Filled(slot=sheet.body.MODULE_INPUT[f"UZF_{name.upper()}"],
                            value=value, provenance=row.provenance)
    return features


def _recharged(groups: dict[str, dict[str, Any]]) -> list[Any]:
    """The cell ids of every RCH row the sheet states or the Recharge composite placed."""
    value = getattr(groups.get("RCH", {}).get("stress_period_data"), "value", None)
    if not isinstance(value, Mapping):
        return []
    return [item[0] for period in value.values() if isinstance(period, (list, tuple))
            for item in period if isinstance(item, (list, tuple)) and item]


def _active(sheet: Any, *, nlay: int, points: Any, cells: Any, utm_epsg: int | None,
            frame: Any) -> Any:
    """One row per layer of the cells the run solves, off a stated DISV_IDOMAIN and the cells a refinement hands to its child; neither, None (every cell)."""
    stated = sheet.filled.get("DISV_IDOMAIN")
    rows = None if stated is None else place(stated.value, slot=stated.slot, nlay=nlay, run=sheet.run,
                                             points=points, cells=cells, utm_epsg=utm_epsg, frame=frame)
    if children_of(sheet):
        return refined_idomain(sheet, rows, points=points, cells=cells, utm_epsg=utm_epsg, nlay=nlay)
    return None if rows is None else layer_rows(rows, nlay=nlay, ncpl=len(cells))


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
            top: Any, botm: Any = None) -> dict[str, Any]:
    """Each keyword's constructor value - a cell ARRAY placed against the mesh's cells, the rest (an option's name list included) as stated."""
    return {
        name: (place(row.value, slot=row.slot, nlay=int(dims["nlay"]),
                     run=sheet.run, points=points, cells=cells,
                     utm_epsg=utm_epsg, frame=frame, top=top, botm=botm)
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


def _write_package_by_package(sim: Any, exchanged: list[Any]) -> list[str]:
    """Every file but the discretization - the staged DISV's bytes are the mesh's; ``exchanged`` are the movers an exchange carries."""
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
            if package.package_type in ("dis", "disv"):
                continue
            package.write()
            written.append(Path(package.filename).name)
    for exchange in [*getattr(sim, "exchange_files", ()), *exchanged]:
        exchange.write()
        written.append(Path(exchange.filename).name)
    return sorted(written)
