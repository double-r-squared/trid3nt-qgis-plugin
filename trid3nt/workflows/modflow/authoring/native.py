"""Read and copy the package graph of a native MODFLOW 6 simulation."""

from __future__ import annotations

import shutil
import shlex
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import ModflowError


@dataclass(frozen=True, slots=True)
class NativeSimulation:
    root: Path
    files: tuple[str, ...]
    values: dict[str, dict[str, dict[str, Any]]]
    modules: dict[str, str]

    def copy_to(self, target: Path) -> None:
        """Retain the native directory layout and bytes read by the simulation."""
        for name in self.files:
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.root / name, path)

    def pack(self, target: Path) -> None:
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for name in self.files:
                bundle.write(self.root / name, arcname=name)


def mapping_grid(path: Path) -> dict[str, Any] | None:
    """Read the first flow model's native map faces without changing its grid."""
    import flopy

    sim = flopy.mf6.MFSimulation.load(sim_ws=str(path), strict=True, verbosity_level=0)
    model = next((sim.get_model(name) for name in sim.model_names
                  if sim.get_model(name).model_type == "gwf6"), None)
    if model is None or model.modelgrid.grid_type == "unstructured":
        return None
    grid = model.modelgrid
    if grid.grid_type == "structured":
        faces = (grid.get_cell_vertices(row, col)
                 for row in range(grid.nrow) for col in range(grid.ncol))
    else:
        faces = (grid.get_cell_vertices(cell) for cell in range(grid.ncpl))
    indexed: dict[tuple[float, float], int] = {}
    cells = []
    for face in faces:
        cells.append([indexed.setdefault(tuple(point), len(indexed)) for point in face])
    return {"model": model.name, "points": list(indexed), "cells": cells,
            "top": grid.top.reshape(-1).tolist(),
            "botm": grid.botm.reshape(len(grid.botm), -1).tolist()}


def unpack(source: Path, target: Path) -> NativeSimulation:
    """Unpack only distinct, relative native file names into the staging directory."""
    from trid3nt.inputs.user_layer import MAX_INGEST_BYTES

    try:
        with zipfile.ZipFile(source) as bundle:
            members = bundle.infolist()
            if sum(row.file_size for row in members) > MAX_INGEST_BYTES:
                raise ModflowError("The simulation archive exceeds the file upload budget.",
                                   error_code="MODFLOW_NATIVE_SIMULATION_SIZE")
            names = set()
            for member in members:
                path = Path(member.filename)
                if path.is_absolute() or ".." in path.parts or "\\" in member.filename:
                    raise ModflowError("The simulation archive names a file outside its root.",
                                       error_code="MODFLOW_NATIVE_INPUT_OUTSIDE")
                if member.is_dir():
                    continue
                name = path.as_posix()
                if name in names:
                    raise ModflowError("The simulation archive repeats a package filename.",
                                       error_code="MODFLOW_NATIVE_INPUT_DUPLICATE")
                names.add(name)
                dest = target / path
                dest.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(member) as opened, dest.open("wb") as written:
                    shutil.copyfileobj(opened, written)
    except zipfile.BadZipFile as error:
        raise ModflowError("The simulation input must be a ZIP of native files with mfsim.nam at its root.",
                           error_code="MODFLOW_NATIVE_SIMULATION_UNREAD") from error
    _check_file_links(target, names)
    return load(target / "mfsim.nam")


def _check_link(value: str) -> None:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value or ":" in value:
        raise ModflowError("Native MODFLOW file references must stay inside the simulation directory.",
                           error_code="MODFLOW_NATIVE_INPUT_OUTSIDE")


def _check_file_links(target: Path, names: set[str]) -> None:
    """Check the native file references before FloPy opens any of them."""
    for name in names:
        path = target / name
        with path.open("rb") as handle:
            for line in handle:
                try:
                    text = line.decode("utf-8").strip()
                except UnicodeDecodeError:
                    break
                if not text or text.startswith("#"):
                    continue
                fields = text.upper().split()
                if not {"FILEIN", "FILEOUT", "OPEN/CLOSE"}.intersection(fields) and (
                        path.suffix.lower() != ".nam" or not fields[0].endswith("6")):
                    continue
                if "\\" in text:
                    raise ModflowError("Native MODFLOW file references use relative paths with forward slashes.",
                                       error_code="MODFLOW_NATIVE_INPUT_OUTSIDE")
                try:
                    words = shlex.split(text, comments=True)
                except ValueError as error:
                    raise ModflowError("A native filename could not be parsed.",
                                       error_code="MODFLOW_NATIVE_SIMULATION_UNREAD") from error
                for index, word in enumerate(words[:-1]):
                    if word.upper() in {"FILEIN", "FILEOUT", "OPEN/CLOSE"} or (
                            index == 0 and path.suffix.lower() == ".nam" and word.upper().endswith("6")):
                        _check_link(words[index + 1])


def _value(data: Any) -> Any:
    if isinstance(data, dict):
        return {key: _value(value) for key, value in data.items()}
    if hasattr(data, "tolist"):
        return _value(data.tolist())
    if isinstance(data, (list, tuple)):
        return [_value(value) for value in data]
    return data


def _relative(root: Path, name: Path) -> str:
    """Reject paths that cannot be staged with the same resolution as the original."""
    if ".." in name.parts or not name.is_relative_to(root) or not name.resolve().is_relative_to(root):
        raise ModflowError("The simulation references an input outside its directory.",
                           error_code="MODFLOW_NATIVE_INPUT_OUTSIDE")
    if not name.is_file():
        raise ModflowError(f"The simulation has no staged file for {name.name}.",
                           error_code="MODFLOW_NATIVE_INPUT_MISSING")
    return name.relative_to(root).as_posix()


def load(path: Path) -> NativeSimulation:
    """Read the stated values by model and package file, preserving all grids and exchanges."""
    import flopy
    from flopy.mf6.mfbase import FlopyException

    path = path.expanduser().absolute()
    root = path.parent.resolve()
    if path.name != "mfsim.nam" or not path.is_file():
        raise ModflowError("The simulation must name an existing mfsim.nam.",
                           error_code="MODFLOW_NATIVE_SIMULATION_MISSING")
    try:
        sim = flopy.mf6.MFSimulation.load(sim_ws=str(root), strict=True, verbosity_level=0)
        mfpath = sim.simulation_data.mfpath
        models = {name: sim.get_model(name) for name in sim.model_names}
        packages = {"": [sim.name_file, *sim.sim_package_list],
                    **{name: [model.name_file, *model.packagelist] for name, model in models.items()}}
        values = {
            name: {package.filename: {
                f"{package.package_type.upper()}_{data.path[-1].upper()}": _value(data.get_data())
                for data in package.data_list if data.has_data()}
                for package in group}
            for name, group in packages.items()}
        files = {_relative(root, Path(mfpath.resolve_path(package.filename, name or None)))
                 for name, group in packages.items() for package in group}
        files.update(_relative(root, Path(mfpath.resolve_path(external.file_path, name)))
                     for external in mfpath.existing_file_dict.values() for name in external.model_name)
    except (FlopyException, OSError, ValueError) as error:
        raise ModflowError("FloPy could not read the simulation's package files.",
                           error_code="MODFLOW_NATIVE_SIMULATION_UNREAD") from error
    modules = {name: model.model_type.removesuffix("6") for name, model in models.items()}
    return NativeSimulation(root, tuple(sorted(files)), values, modules)


def author(target: Path, columns: dict[str, Any], coupled: dict[str, dict[str, Any]]) -> NativeSimulation:
    """Write a native MODFLOW simulation from the stated columns and packages."""
    import flopy

    from .simulation import _package

    def grouped(values: dict[str, Any]) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for identifier, value in values.items():
            package, _, key = identifier.partition("_")
            if not key:
                raise ModflowError(f"{identifier} is not a native package keyword.",
                                   error_code="MODFLOW_KEYWORD_SHAPE")
            _check_stated_files(keyword=identifier, value=value)
            result.setdefault(package.lower(), {})[key.lower()] = value
        return result

    groups = grouped(columns)
    sim = flopy.mf6.MFSimulation(sim_ws=str(target), verbosity_level=0, write_headers=False)
    _package(flopy, "ModflowTdis", groups.pop("tdis"), owner=sim, how="TDIS")
    ims = _package(flopy, "ModflowIms", groups.pop("ims"), owner=sim, how="IMS")
    grid = next(kind for kind in ("dis", "disv", "disu") if kind in groups)
    model_specs = {"gwf": groups, **{name: grouped(rows) for name, rows in coupled.items()}}
    for module, packages in model_specs.items():
        prefix = f"Modflow{module.capitalize()}"
        model = _package(flopy, prefix, packages.pop("nam", {}),
                         owner=sim, how=module, modelname=module)
        if module == "prt":
            ems = _package(flopy, "ModflowEms", {}, owner=sim, how="EMS")
            sim.register_solution_package(ems, [module])
        else:
            sim.register_ims_package(ims, [module])
        if module != "gwf":
            packages[grid] = {key: value for key, value in groups[grid].items()
                              if key not in ("grb_filerecord",)}
        for package in sorted(packages, key=lambda name: name != grid):
            _package(flopy, prefix + package, packages[package], owner=model, how=package.upper())
        if module != "gwf":
            _package(flopy, "ModflowGwf" + module, {}, owner=sim, how=f"GWF-{module.upper()} exchange",
                     exgtype=f"GWF6-{module.upper()}6", exgmnamea="gwf", exgmnameb=module)
    sim.write_simulation()
    return load(target / "mfsim.nam")


def override(simulation: NativeSimulation, own: dict[str, Any],
             coupled: dict[str, dict[str, Any]]) -> NativeSimulation:
    """Apply stated edits to existing native package fields and keep the rest of the graph."""
    import flopy
    from flopy.mf6.mfbase import FlopyException, MFDataException

    sim = flopy.mf6.MFSimulation.load(sim_ws=str(simulation.root), strict=True,
                                       verbosity_level=0, write_headers=False)
    models = [sim.get_model(name) for name in sim.model_names]
    package_fields: dict[tuple[str, str], list[tuple[Any, Any]]] = {}
    for module, package in [
        *(("gwf", package) for package in sim.sim_package_list),
        *((model.model_type.removesuffix("6"), package) for model in models for package in model.packagelist),
    ]:
        for column in package.data_list:
            keyword = f"{package.package_type.upper()}_{column.path[-1].upper()}"
            package_fields.setdefault((module, keyword), []).append((package, column))
    changed = {}
    for module, fields in {"gwf": own, **coupled}.items():
        for keyword, value in fields.items():
            _check_stated_files(keyword=keyword, value=value)
            matches = package_fields.get((module, keyword), [])
            if len(matches) != 1:
                raise ModflowError(f"{module}: {keyword} selects {len(matches)} native package fields.",
                                   error_code="MODFLOW_NATIVE_OVERRIDE_FIELD")
            package, column = matches[0]
            try:
                column.set_data(value)
            except (FlopyException, MFDataException, ValueError) as error:
                raise ModflowError(f"The native package refused {keyword}.",
                                   error_code="MODFLOW_PACKAGE_STATE") from error
            changed[package.path] = package
    for package in changed.values():
        package.write()
    return load(simulation.root / "mfsim.nam")


def _check_stated_files(*, keyword: str, value: Any) -> None:
    if isinstance(value, dict):
        if "filename" in value:
            _check_link(str(value["filename"]))
        for item in value.values():
            _check_stated_files(keyword=keyword, value=item)
    elif isinstance(value, (tuple, list)):
        for item in value:
            _check_stated_files(keyword=keyword, value=item)
    elif "FILERECORD" in keyword and isinstance(value, str):
        _check_link(value)
