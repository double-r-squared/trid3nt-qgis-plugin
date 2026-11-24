"""The OBS utility's lines from the Observations statements: each id resolved by the point contract, each output file named.

A model's OBS file and a package's own OBS file take the same line, ``[name, type, id[, id2]]``; a cell id is a
point in its cell on a layer, a placed feature's id its number (a boundary row's, its cell). A point is never moved.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..errors import ModflowError
from .stress import GIVERS, at_cell, pick

__all__ = ["NativeObservation", "author_native_observations", "observation_files", "place_observations"]

# The packages whose observation id is a placed feature's number; the boundary packages' id is the row's cell.
_NUMBERED = ("SFR", "LAK", "MAW", "UZF", "CSUB")


def _file(module: str, package: str | None, kind: str) -> str:
    return (f"{module}.{package.lower()}.obs.{kind}.csv" if package else f"{module}.obs.{kind}.csv")


def _owner(identifier: str) -> tuple[bool, str | None]:
    """``(is an observation carrier, its package prefix or None for the model's own)``."""
    if identifier == "OBS_CONTINUOUS":
        return True, None
    if identifier.endswith("_OBS_FILERECORD"):
        return True, identifier[:-len("_OBS_FILERECORD")]
    return False, None


def observation_files(filled: Mapping[str, Any], module: str) -> list[tuple[str, str, str | None]]:
    """Every CSV the stated observations write -> ``[(file name, label, observation type)]``, in the order stated.

    ``filled`` maps identifiers to a Filled row or its bare value; the label is the owner and type the
    statements name, while a file stated whole is labelled by its own name and has no type."""
    found: list[tuple[str, str, str | None]] = []
    for identifier, row in filled.items():
        carrier, package = _owner(identifier)
        value = getattr(row, "value", row)
        if not carrier or not isinstance(value, Mapping):
            continue
        if "observations" in value:
            for statement in value["observations"]:
                kind = statement["type"]
                item = (_file(module, package, kind), f"{package} {kind}" if package else kind, kind)
                if item not in found:
                    found.append(item)
            continue
        files = value.get("continuous", {}) if package else value
        found += [(str(name), str(name), None) for name in files]
    return found


def place_observations(statements: list[Mapping[str, Any]], *, module: str, package: str | None,
                       features: list[Mapping[str, Any]], points: Any, cells: Any, utm_epsg: int | None,
                       nlay: int, active: Any = None) -> tuple[dict[str, list[list]], int | None]:
    """Observations statements -> ``({file: rows}, digits)`` for one OBS file; ``features`` are the placed composites' features."""
    mesh = {"points": points, "cells": cells, "utm_epsg": utm_epsg}
    files: dict[str, list[list]] = {}
    digits = {statement.get("digits") for statement in statements}
    if len(digits) > 1:
        raise ModflowError(
            f"Observations states digits {sorted(map(str, digits))} for one OBS file; the option is the file's, "
            "so state it once or the same on each statement.", error_code="MODFLOW_STRESS_UNSTATED")
    for rank, statement in enumerate(statements, start=1):
        role = f"Observations {rank} ({statement['name']})"
        first = _id(statement, package, features, role=role, nlay=nlay, active=active, **mesh)
        second = _second(statement, role=role, nlay=nlay, active=active, **mesh)
        files.setdefault(_file(module, package, statement["type"]), []).append(
            [statement["name"], statement["type"], first, *([second] if second is not None else [])])
    return files, digits.pop()


def _cell(at: Any, layer: Any, *, role: str, nlay: int, active: Any, **mesh: Any) -> tuple[int, int]:
    """The ``(layer - 1, cell)`` a point on a layer names."""
    if at is None or layer is None:
        raise ModflowError(f"{role} names a cell by a point and a layer; state both.",
                           error_code="MODFLOW_STRESS_UNSTATED")
    if layer > nlay:
        raise ModflowError(f"{role} is on layer {layer} and the grid has {nlay} layer(s).",
                           error_code="MODFLOW_STRESS_LAYER")
    cell = at_cell(at, role=role, **mesh)
    if active is not None and active[layer - 1][cell] == 0:
        raise ModflowError(f"{role} lies in a cell the run does not solve (DISV_IDOMAIN 0) on layer {layer}.",
                           error_code="MODFLOW_STRESS_OUTSIDE")
    return layer - 1, cell


def _id(statement: Mapping[str, Any], package: str | None, features: list[Mapping[str, Any]], *, role: str,
        nlay: int, active: Any, **mesh: Any) -> Any:
    on, at, layer = statement.get("on"), statement.get("at"), statement.get("layer")
    if package in _NUMBERED:
        if layer is not None:
            raise ModflowError(f"{role} names a {package} feature, which has no layer; remove layer.",
                               error_code="MODFLOW_STRESS_UNSTATED")
        found = pick(on, at, [item for item in features if item["package"] == package], role=role, **mesh)
        return (found["number"],)
    if on is not None:
        pool = [item for item in features if item["package"] == package] if package else \
               [item for item in features if item["package"] in GIVERS]
        found = pick(on, at, pool, role=role, **mesh)
        if "layer" not in found:
            raise ModflowError(f"{role} names {on!r}, a {found['package']} feature with no cell to observe.",
                               error_code="MODFLOW_STRESS_UNSTATED")
        return found["layer"], next(iter(found["cells"]))
    return _cell(at, layer, role=role, nlay=nlay, active=active, **mesh)


def _second(statement: Mapping[str, Any], *, role: str, nlay: int, active: Any, **mesh: Any) -> Any:
    """The id2 of a statement: a connection number, the second cell, or None."""
    connection, to_at = statement.get("connection"), statement.get("to_at")
    if connection is not None and (to_at is not None or statement.get("to_layer") is not None):
        raise ModflowError(f"{role} states a connection and a second cell; id2 is one of them.",
                           error_code="MODFLOW_STRESS_UNSTATED")
    if connection is not None:
        return (connection - 1,)
    if to_at is None and statement.get("to_layer") is None:
        return None
    return _cell(to_at, statement.get("to_layer"), role=f"{role} to", nlay=nlay, active=active, **mesh)


@dataclass(frozen=True)
class NativeObservation:
    """An MF6 OBS row with native one-based identifiers and an optional package owner."""

    name: str
    kind: str
    first: tuple[int | str, ...]
    second: tuple[int | str, ...] = ()
    package: str | None = None


def _native_obs_error(message: str) -> ModflowError:
    return ModflowError(message, error_code="MODFLOW_OBSERVATION_UNPLACED")


def _native_obs_path(root: Path, filename: str) -> Path:
    path = root / filename
    if Path(filename).is_absolute() or not path.resolve().is_relative_to(root):
        raise _native_obs_error(f"Observation file {filename!r} lies outside the native model directory.")
    return path


def _native_obs_tokens(text: str) -> list[list[str]]:
    return [tokens for line in text.splitlines() if (tokens := shlex.split(line, comments=True))]


def _obs_block(text: str, name: str) -> re.Match[str] | None:
    return re.search(rf"(?ims)^[ \t]*BEGIN[ \t]+{name}\b[^\n]*\n(.*?)(?=^[ \t]*END[ \t]+{name}\b)", text)


def _native_observation_line(row: NativeObservation) -> str:
    if not (re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,39}", row.name)
            and re.fullmatch(r"[a-zA-Z0-9-]+", row.kind) and row.first):
        raise _native_obs_error(f"State an OBS name, type and native ID for {row.name!r}.")
    ids = []
    for value in (*row.first, *row.second):
        if type(value) is int and value > 0:
            ids.append(str(value))
        elif (isinstance(value, str) and value.strip()
              and not any(char in ("'", '"', "#") or ord(char) < 32 for char in value)):
            ids.append(f"'{value}'" if any(char.isspace() for char in value) else value)
        else:
            raise _native_obs_error(f"Observation {row.name} has an invalid native ID token {value!r}.")
    return " ".join((row.name, row.kind, *ids))


def author_native_observations(directory: Path, name_file: str, rows: Sequence[NativeObservation],
                               *, stem: str) -> dict[str, tuple[str, ...]]:
    """Append named OBS CSVs to a native MF6 model, package or exchange and return column order.

    A package can be named by its PNAME, filename, or an unambiguous package type.
    Native MF6 validates which observation types and ID shapes each owner accepts.
    """
    root = directory.resolve()
    if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]*", stem):
        raise _native_obs_error(f"Invalid OBS file stem {stem!r}.")
    name_path = _native_obs_path(root, name_file)
    name_text = name_path.read_text()
    block = _obs_block(name_text, "packages")
    if block is None:
        raise _native_obs_error(f"Cannot place observations in {name_file!r} without its PACKAGES block.")
    packages = _native_obs_tokens(block[1])
    simulation = root / "mfsim.nam"
    if simulation.is_file() and (exchanges := _obs_block(simulation.read_text(), "exchanges")):
        packages.extend(tokens[:2] for tokens in _native_obs_tokens(exchanges[1]) if len(tokens) >= 2)
    groups: dict[Path | None, list[NativeObservation]] = {}
    seen: set[str] = set()
    for row in rows:
        if row.name.lower() in seen:
            raise _native_obs_error(f"Observation {row.name!r} is repeated.")
        seen.add(row.name.lower())
        package = None
        if row.package:
            owner = row.package.casefold()
            owners = [tokens for tokens in packages if len(tokens) >= 2
                      and owner in {tokens[0].casefold().removesuffix("6"),
                                    *(token.casefold() for token in tokens[1:])}]
            if len(owners) != 1 or owners[0][0].casefold() == "obs6":
                raise _native_obs_error(f"Observation {row.name} could not identify package {row.package!r}.")
            package = _native_obs_path(root, owners[0][1])
        groups.setdefault(package, []).append(row)

    files: dict[str, tuple[str, ...]] = {}
    updates: dict[Path, str] = {}
    for rank, (package, observations) in enumerate(groups.items(), 1):
        csv_name, obs_name = f"{stem}_{rank}.csv", f"{stem}_{rank}.obs"
        obs_path = root / obs_name
        if package is None:
            old = [tokens for tokens in packages if tokens[0].casefold() == "obs6"]
            if len(old) > 1:
                raise _native_obs_error(f"{name_file!r} has more than one model OBS package.")
            if old:
                obs_path = _native_obs_path(root, old[0][1])
            else:
                updates[name_path] = name_text[:block.end(1)] + f"  OBS6 {obs_name} {stem}\n" + name_text[block.end(1):]
        else:
            text = updates.get(package) or package.read_text()
            options = _obs_block(text, "options")
            old = [tokens for tokens in _native_obs_tokens(options[1] if options else "")
                   if len(tokens) == 3 and [x.casefold() for x in tokens[:2]] == ["obs6", "filein"]]
            if len(old) > 1:
                raise _native_obs_error(f"{package.name!r} has more than one OBS6 FILEIN declaration.")
            if old:
                obs_path = _native_obs_path(root, old[0][2])
            elif options:
                updates[package] = text[:options.end(1)] + f"  OBS6 FILEIN {obs_name}\n" + text[options.end(1):]
            else:
                updates[package] = f"BEGIN OPTIONS\n  OBS6 FILEIN {obs_name}\nEND OPTIONS\n\n" + text

        if not old and obs_path.exists():
            raise _native_obs_error(f"Observation file {obs_path.name!r} already exists.")
        held = updates.get(obs_path)
        if held is None:
            held = obs_path.read_text() if old else ""
        if (root / csv_name).exists() or any(
            len(tokens) >= 4 and [x.casefold() for x in tokens[:3]] == ["begin", "continuous", "fileout"]
            and tokens[3].casefold() == csv_name.casefold() for tokens in _native_obs_tokens(held)
        ):
            raise _native_obs_error(f"Observation output {csv_name!r} already exists.")
        lines = "\n".join(f"  {_native_observation_line(row)}" for row in observations)
        updates[obs_path] = f"{held.rstrip()}\n\nBEGIN CONTINUOUS FILEOUT {csv_name}\n{lines}\nEND CONTINUOUS\n".lstrip()
        files[csv_name] = tuple(row.name for row in observations)

    for path, text in updates.items():
        path.write_text(text)
    return files
