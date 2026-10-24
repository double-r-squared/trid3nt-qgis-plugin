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
from .griddata import frame_of, needs_botm, needs_top, place
from .mesh_files import _layering, botm_of, disv_dimensions, top_of
from .movers import mover_rows
from .stress import (GIVERS, lake_statements, layer_rows, maw_statements, place as place_stress, place_interbeds,
                     place_lakes, place_maw, place_streams, place_uzf)

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
    botm = (botm_of(sheet, nlay=int(dims["nlay"]), art=mesh_artifact(mesh), points=points,
                    cells=cells, top=top)
            if any(needs_botm(row.value) for rows in groups.values()
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
    derived, tables, series = _stressed(sheet, groups, nlay=int(dims["nlay"]), points=points,
                                        cells=cells, utm_epsg=utm_epsg, frame=frame, active=active,
                                        start=start, grid=grid)
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
    flow = _model(flopy, sim, module=module, groups=groups, named=named, tables=tables, series=series, **placed)
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
        cderived, _tables, _series = _stressed(coupled, cgroups, nlay=int(dims["nlay"]),
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
           botm: Any = None, sources: Any = (), tables: Mapping[str, list] = {},
           series: Mapping[str, Mapping[str, Any]] = {}) -> Any:
    """One model of this simulation: its name file, the staged DISV's line, its solution and its packages; ``tables`` are the TAB6 files a package reads and ``series`` its time-series file."""
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
    for prefix, rows in sorted(groups.items(), key=lambda item: item[0] == "MVR"):
        keywords = _placed(rows, dims=dims, points=points, cells=cells,
                           utm_epsg=utm_epsg, sheet=sheet, frame=frame, top=top,
                           botm=botm)
        if prefix == "SSM" and sources:
            keywords = {"sources": list(sources), **keywords}
        if prefix == "MVR":
            keywords = _named_movers(keywords, model)
        package = _package(flopy, f"Modflow{cap}{prefix.lower()}", keywords,
                           owner=model, how=f"{prefix} of {module}")
        if prefix in series:
            held = series[prefix]
            package.ts.initialize(filename=f"{prefix.lower()}.ts", timeseries=held["rows"],
                                  time_series_namerecord=held["names"],
                                  interpolation_methodrecord=held["interpolation"], sfacrecord=held["sfac"])
        for filename, table in tables.get(prefix, ()):
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
              start: Any = None, grid: Any = None
              ) -> tuple[set[str], dict[str, list], dict[str, Mapping[str, Any]]]:
    """Place each stress statement on the cells -> the identifiers it derived (its MAXBOUND), the TAB6 files its packages read and the time series they read.

    A statement's row becomes period 1's rows, carried forward by mf6; the bound is the
    placement's own count; ``active`` is the carrier's solved cells - the grid is one."""
    derived: set[str] = set()
    features: list[Mapping[str, Any]] = []
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
                              "cells": {item[0][1]}} for rank, item in enumerate(found)]
            placed += found
        rows["stress_period_data"] = replace(row, value={0: placed})
        rows["maxbound"] = Filled(
            slot=sheet.body.MODULE_INPUT[f"{prefix}_MAXBOUND"],
            value=len(placed), provenance=row.provenance)
        derived.add(f"{prefix}_MAXBOUND")
    tables: dict[str, list] = {}
    row = groups.get("SFR", {}).get("packagedata")
    if row is not None and isinstance(row.value, Mapping) and "streams" in row.value:
        found, tables["SFR"] = _streamed(sheet, groups["SFR"], row, nlay=nlay, points=points, cells=cells,
                                         utm_epsg=utm_epsg, frame=frame, active=active)
        features += found
        derived |= {"SFR_NREACHES", "SFR_CONNECTIONDATA"}
    elif isinstance(getattr(groups.get("SFR", {}).get("diversions"), "value", None), Mapping):
        raise ModflowError(
            "Diversions move water between the reaches the Streams lines place; this run states SFR "
            "reaches another way, so state the lines.", error_code="MODFLOW_KEYWORD_SHAPE")
    row = groups.get("LAK", {}).get("packagedata")
    if row is not None and isinstance(row.value, Mapping) and "lakes" in row.value:
        found, tables["LAK"] = _lakened(sheet, groups["LAK"], row, nlay=nlay, points=points, cells=cells,
                                        utm_epsg=utm_epsg, frame=frame, active=active, top=grid[0], botm=grid[1])
        features += found
        derived |= {"LAK_NLAKES", "LAK_NOUTLETS", "LAK_NTABLES", "LAK_CONNECTIONDATA"}
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
        _interbedded(sheet, groups["CSUB"], row, nlay=nlay, points=points, cells=cells, utm_epsg=utm_epsg,
                     frame=frame, active=active)
        derived.add("CSUB_NINTERBEDS")
    row = groups.get("MVR", {}).get("perioddata")
    if row is not None and isinstance(row.value, Mapping) and "movers" in row.value:
        _movered(sheet, groups, row, features, points=points, cells=cells, utm_epsg=utm_epsg)
        derived |= {"MVR_MAXMVR", "MVR_MAXPACKAGES"}
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
    return derived, {prefix: files for prefix, files in tables.items() if files}, series


def _interbedded(sheet: Any, rows: dict[str, Any], row: Any, **mesh: Any) -> None:
    """Place the Interbeds statements: the package's interbed list and its count."""
    stated = sorted(f"CSUB_{name.upper()}" for name in ("ninterbeds",) if name in rows)
    if stated:
        raise ModflowError(
            f"{', '.join(stated)} is derived from the Interbeds statements; state either the statements or the "
            "CSUB keywords, not both.", error_code="MODFLOW_KEYWORD_SHAPE")
    packagedata = place_interbeds(row.value["interbeds"], boundnames=bool(getattr(rows.get("boundnames"), "value", False)),
                                  **mesh)
    for name, value in (("packagedata", packagedata), ("ninterbeds", len(packagedata))):
        rows[name] = Filled(slot=sheet.body.MODULE_INPUT[f"CSUB_{name.upper()}"], value=value, provenance=row.provenance)


def _movered(sheet: Any, groups: dict[str, dict[str, Any]], row: Any, features: list[Mapping[str, Any]],
             **mesh: Any) -> None:
    """Resolve the WaterMover statements: the package's mover list, its package list and counts, and the MOVER option of each package used."""
    rows = groups["MVR"]
    stated = sorted(f"MVR_{name.upper()}" for name in ("maxmvr", "maxpackages", "packages") if name in rows)
    if stated:
        raise ModflowError(
            f"{', '.join(stated)} {'is' if len(stated) == 1 else 'are'} derived from the "
            "WaterMover statements; state either the statements or the MVR keywords, not both.",
            error_code="MODFLOW_KEYWORD_SHAPE")
    used, periods = mover_rows(row.value["movers"], features, **mesh)
    for package in used:
        mover = groups.get(package, {}).get("mover")
        if mover is not None and not mover.value:
            raise ModflowError(
                f"{package}_MOVER is stated false, and WaterMover moves water through the {package} package.",
                error_code="MODFLOW_KEYWORD_SHAPE")
        groups[package]["mover"] = Filled(slot=sheet.body.MODULE_INPUT[f"{package}_MOVER"], value=True,
                                          provenance=row.provenance)
    placed = {"perioddata": periods, "maxmvr": len(row.value["movers"]), "maxpackages": len(used),
              "packages": [[package] for package in used]}
    for name, value in placed.items():
        rows[name] = Filled(slot=sheet.body.MODULE_INPUT[f"MVR_{name.upper()}"], value=value,
                            provenance=row.provenance)


def _streamed(sheet: Any, rows: dict[str, Any], row: Any, **mesh: Any) -> tuple[list[Mapping[str, Any]], list]:
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
            files.setdefault(tuple(map(tuple, reach["table"])), f"sfr_xs{len(files) + 1}.tab")
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


def _lakened(sheet: Any, rows: dict[str, Any], row: Any, **mesh: Any) -> tuple[list[Mapping[str, Any]], list]:
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
        placed.update(tables=[[lake["number"], f"lak_tab{lake['number'] + 1}.tab"] for lake in tabled],
                      ntables=len(tabled))
    for name, value in placed.items():
        rows[name] = Filled(slot=sheet.body.MODULE_INPUT[f"LAK_{name.upper()}"],
                            value=value, provenance=row.provenance)
    return features, [(f"lak_tab{lake['number'] + 1}.tab", [list(line) for line in lake["table"]]) for lake in tabled]


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
    """One row per layer of the cells the run solves, off a stated DISV_IDOMAIN; unstated, None (every cell)."""
    stated = sheet.filled.get("DISV_IDOMAIN")
    if stated is None:
        return None
    rows = place(stated.value, slot=stated.slot, nlay=nlay, run=sheet.run,
                 points=points, cells=cells, utm_epsg=utm_epsg, frame=frame)
    return layer_rows(rows, nlay=nlay, ncpl=len(cells))


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
