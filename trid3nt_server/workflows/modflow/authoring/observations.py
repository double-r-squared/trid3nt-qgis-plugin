"""The OBS utility's lines from the Observations statements: each id resolved by the point contract, each output file named.

A model's OBS file and a package's own OBS file take the same line, ``[name, type, id[, id2]]``; a cell id is a
point in its cell on a layer, a placed feature's id its number (a boundary row's, its cell). A point is never moved.
"""

from __future__ import annotations

from typing import Any, Mapping

from ..errors import ModflowError
from .stress import GIVERS, at_cell, pick

__all__ = ["observation_files", "place_observations"]

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
