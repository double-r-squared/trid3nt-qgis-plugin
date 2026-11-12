"""The control file, written in PEST++'s keyword format: every option stated by name, the tables beside it.

An option the control file does not state is PEST++'s own default, so the file states only what the study
and the run stated. Each table is one CSV with the header PEST++ reads it by.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..errors import PestppError
from ..modules import LOCKED, PESTPP

__all__ = ["Observed", "Parameter", "TABLES", "write_control"]

#: The tables the control file names, each written beside it.
TABLES = {"parameter data": "study_parameters.csv", "observation data": "study_observations.csv",
           "model input": "study_templates.csv", "model output": "study_instructions.csv"}


@dataclass(frozen=True)
class Parameter:
    """One adjustable parameter: its starting value, bounds and group, and the scale and offset PEST++
    writes it through (the value written is ``value * scale + offset``)."""

    name: str
    value: float
    lower: float
    upper: float
    group: str
    transform: str = "log"
    scale: float = 1.0
    offset: float = 0.0


@dataclass(frozen=True)
class Observed:
    """One observation: its target value, weight and group."""

    name: str
    value: float
    weight: float
    group: str


def _spelled(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ",".join(_spelled(item) for item in value)
    return repr(value) if isinstance(value, float) else str(value)


def _table(path: Path, header: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)


def write_control(directory: Path, *, name: str, parameters: Sequence[Parameter],
                  observations: Sequence[Observed], templates: Sequence[tuple[str, str]],
                  instructions: Sequence[tuple[str, str]], command: str,
                  stated: Mapping[str, Any] = {}, own: Mapping[str, Any] = {}) -> Path:
    """The control file ``name`` and its tables, written into ``directory`` -> its path.

    ``stated`` are the options the run states, by identifier; ``own`` the options the study's own files
    need, by name. A stated locked option, or one the dictionary does not have, refuses."""
    options: dict[str, Any] = {}
    for identifier, value in stated.items():
        slot = PESTPP.MODULE_INPUT.get(str(identifier).upper())
        if slot is None:
            raise PestppError(f"pestpp has no option {identifier!r}", error_code="PESTPP_OPTION_UNKNOWN")
        if slot.keyword in LOCKED:
            raise PestppError(f"{slot.keyword} is the study's own to state: {LOCKED[slot.keyword]}.",
                              error_code="PESTPP_OPTION_LOCKED")
        options[slot.keyword] = value
    options.update(own)
    if not parameters or not observations:
        raise PestppError("a study adjusts at least one parameter against at least one observation",
                          error_code="PESTPP_STUDY_EMPTY")
    _table(directory / TABLES["parameter data"],
           ("parnme", "partrans", "parchglim", "parval1", "parlbnd", "parubnd", "pargp", "scale", "offset",
            "dercom"),
           [(p.name, p.transform, "factor", repr(p.value), repr(p.lower), repr(p.upper), p.group,
             repr(p.scale), repr(p.offset), 1) for p in parameters])
    _table(directory / TABLES["observation data"], ("obsnme", "obsval", "weight", "obgnme"),
           [(o.name, repr(o.value), repr(o.weight), o.group) for o in observations])
    _table(directory / TABLES["model input"], ("pest_file", "model_file"), templates)
    _table(directory / TABLES["model output"], ("pest_file", "model_file"), instructions)
    lines = ["pcf version=2", "* control data keyword", "pestmode estimation",
             *(f"{keyword} {_spelled(value)}" for keyword, value in options.items()),
             *(f"* {section} external\n{table}" for section, table in TABLES.items()
               if section in ("parameter data", "observation data")),
             "* model command line", command,
             *(f"* {section} external\n{table}" for section, table in TABLES.items()
               if section in ("model input", "model output"))]
    path = directory / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
