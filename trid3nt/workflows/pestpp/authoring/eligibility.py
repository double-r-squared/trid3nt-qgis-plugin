"""What the MODFLOW 6 dictionary says a study may tune: an array's declared element type and a list package's columns.

The dictionary carries every package's keywords; its ``dtype`` on an ARRAY row and its ``columns`` table are the
engine's own declaration of what a value is - a double-precision array, an integer flag, a string, or a list
package's recarray column. Nothing here names a package or a variable: a target is eligible when the DFN declares
it REAL, and everything else refuses by name.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from ..errors import PestppError

__all__ = ["Target", "array_dtype", "columns", "real", "resolve"]

#: The DFN element types a calibration may tune with a multiplier: a double-precision array or list column.
_REAL = "REAL"


@dataclass(frozen=True)
class Target:
    """One identifier resolved against the run's name file and the MODFLOW 6 dictionary.

    ``instance`` is the package name the name file gives it, ``kind`` the DFN package, ``body`` the model body;
    ``variable`` is the DFN entry name (an array) and ``column``/``recarray``/``block`` the list column and where it
    sits; ``dtype`` is the DFN's declared element type; ``layer`` is the one layer a selector names, else None."""

    instance: str
    kind: str
    body: str
    file: str
    variable: str
    dtype: str
    column: str | None = None
    recarray: str | None = None
    block: str | None = None
    layer: int | None = None

    @property
    def is_column(self) -> bool:
        return self.column is not None

    @property
    def identifier(self) -> str:
        base = f"{self.kind}_{self.column or self.variable}".upper()
        return f"{base}@{self.layer}" if self.layer is not None else base


def _dictionary_dir() -> Path:
    from ...modflow.modules.module import module_input_dir

    return module_input_dir()


@lru_cache(maxsize=None)
def _keywords(body: str) -> Mapping[str, dict[str, Any]]:
    path = _dictionary_dir() / f"{body}.json"
    return {row["identifier"]: row for row in json.loads(path.read_text())["keywords"]}


@lru_cache(maxsize=None)
def _column_rows(body: str) -> tuple[dict[str, Any], ...]:
    path = _dictionary_dir() / "columns" / f"{body}.json"
    return tuple(json.loads(path.read_text())["columns"])


def array_dtype(body: str, kind: str, variable: str) -> str:
    """The DFN element type of the array ``variable`` of package ``kind`` in ``body``; an array the dictionary does
    not declare refuses."""
    row = _keywords(body).get(f"{kind}_{variable}".upper())
    if row is None or row.get("type") != "ARRAY":
        raise PestppError(f"{kind}_{variable.upper()} is no array the {body} dictionary declares",
                          error_code="PESTPP_PARAMETER_NOT_REAL")
    if "dtype" not in row:
        raise PestppError(f"{kind}_{variable.upper()} is an array whose dictionary row states no element type",
                          error_code="PESTPP_PARAMETER_NOT_REAL")
    return str(row["dtype"])


def columns(body: str, kind: str, *, recarray: str | None = None) -> tuple[dict[str, Any], ...]:
    """The list columns the dictionary declares for package ``kind`` of ``body``, in the recarray's own order."""
    return tuple(row for row in _column_rows(body)
                 if row["package"] == kind and (recarray is None or row["recarray"] == recarray))


def _packages(the_deck: Any, body: str) -> list[tuple[str, str, str]]:
    """The run's package instances of ``body`` -> ``[(instance, kind, file)]``."""
    return [(instance, kind, file) for instance, (held, kind, file) in the_deck.instances.items() if held == body]


def _lookup(identifier: str, the_deck: Any) -> tuple[str, str, str, str] | None:
    """The longest package prefix under which ``identifier`` names a dictionary variable or column.

    An instance name wins over its kind where both fit, so a repeated package is named by the name file's own
    name; the suffix is the array's DFN name or a list column's keyword."""
    wanted = identifier.upper()
    best: tuple[str, str, str, str] | None = None
    for body in ("gwf", "gwt", "gwe", "prt"):
        for instance, kind, file in _packages(the_deck, body):
            for prefix in dict.fromkeys((instance.upper(), kind.upper())):
                if not wanted.startswith(prefix + "_"):
                    continue
                suffix = wanted[len(prefix) + 1:]
                arrays = _keywords(body)
                is_array = f"{kind}_{suffix}" in arrays and arrays[f"{kind}_{suffix}"].get("type") == "ARRAY"
                is_column = any(row["keyword"] == suffix for row in columns(body, kind))
                if not (is_array or is_column):
                    continue
                candidate = (prefix, suffix, body, kind)
                if best is None or len(prefix) > len(best[0]):
                    best = candidate
    return best


def _layer(identifier: str) -> tuple[str, int | None]:
    base, _, selector = identifier.partition("@")
    if not selector:
        return base, None
    if not selector.isdigit() or int(selector) < 1:
        raise PestppError(f"{identifier} names layer {selector!r}; a layer selector is an integer from 1",
                          error_code="PESTPP_PARAMETER_UNPLACED")
    return base, int(selector)


def resolve(identifier: str, the_deck: Any) -> Target:
    """``identifier`` as a tunable target of the run: ``PKG_VAR``, ``INSTANCE_VAR``, or either with ``@N`` for one
    layer. A name no package of the run states refuses naming what it has."""
    base, layer = _layer(identifier)
    found = _lookup(base, the_deck)
    if found is None:
        packages = sorted({kind for body in ("gwf", "gwt", "gwe", "prt")
                           for _instance, kind, _file in _packages(the_deck, body)})
        raise PestppError(f"{identifier} names no package or variable of the run's MODFLOW models; their package "
                          f"kinds are {', '.join(packages) or 'none'}", error_code="PESTPP_PARAMETER_UNPLACED")
    prefix, suffix, body, kind = found
    instances = [(instance, file) for instance, kind_held, file in _packages(the_deck, body) if kind_held == kind]
    named = [file for instance, file in instances if instance.upper() == prefix]
    if named:
        file = named[0]
        instance = next(instance for instance, held in instances if held == file)
    else:
        if len(instances) != 1:
            raise PestppError(f"{identifier} names the {kind} package kind, and the run states {len(instances)} of "
                              f"them ({', '.join(sorted(i for i, _ in instances))}); name the one it tunes",
                              error_code="PESTPP_PARAMETER_UNPLACED")
        instance, file = instances[0]
    rows = _keywords(body)
    array = rows.get(f"{kind}_{suffix}")
    if array is not None and array.get("type") == "ARRAY":
        if layer is None and array.get("layered") is not True and int(the_deck.nlay) > 1:
            raise PestppError(f"{identifier} is one array of a {int(the_deck.nlay)}-layer run and is not layered; it "
                              "has no layer to select", error_code="PESTPP_PARAMETER_UNPLACED")
        return Target(instance=instance, kind=kind, body=body, file=file, variable=suffix.lower(),
                      dtype=array_dtype(body, kind, suffix), layer=layer)
    matches = [row for row in columns(body, kind) if row["keyword"] == suffix]
    if not matches:
        raise PestppError(f"{identifier} is neither an array nor a list column of {kind} in the {body} dictionary",
                          error_code="PESTPP_PARAMETER_UNPLACED")
    recarrays = {row["recarray"] for row in matches}
    if len(recarrays) != 1:
        raise PestppError(f"{identifier} names column {suffix} of {len(recarrays)} {kind} recarrays; it states "
                          f"{', '.join(sorted(recarrays))}", error_code="PESTPP_PARAMETER_UNPLACED")
    row = matches[0]
    if layer is not None:
        raise PestppError(f"{identifier} is a list column, which has no layer to select", error_code="PESTPP_PARAMETER_UNPLACED")
    return Target(instance=instance, kind=kind, body=body, file=file, variable=suffix.lower(), dtype=row["type"],
                  column=row["keyword"], recarray=row["recarray"], block=row["rubrique"][0])


def real(target: Target) -> Target:
    """``target`` if its DFN type is a double-precision array or list column; every other type refuses by name."""
    if target.dtype != _REAL:
        what = "list column" if target.is_column else "array"
        raise PestppError(f"{target.identifier} is the {target.kind} {what} of the dictionary's type {target.dtype}, "
                          "and a calibration tunes a REAL one; an integer flag, a string or a keyword is no "
                          "parameter", error_code="PESTPP_PARAMETER_NOT_REAL")
    return target
