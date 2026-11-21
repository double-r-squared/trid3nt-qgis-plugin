"""The control file, written in PEST++'s keyword format: every option stated by name, the tables beside it.

An option the control file does not state is PEST++'s own default, so the file states only what the study
and the run stated. Each table is one CSV with the header PEST++ reads it by.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..errors import PestppError
from ..modules import LOCKED, PESTPP
from ...runtime.module import UNSET

__all__ = ["Observed", "Parameter", "TABLES", "substitute", "write_control"]

#: The tables the control file names, each written beside it.
TABLES = {"parameter groups": "study_groups.csv", "parameter data": "study_parameters.csv",
          "observation data": "study_observations.csv", "model input": "study_templates.csv",
          "model output": "study_instructions.csv", "phi factors": "study_phi_factors.csv"}

#: The group table's columns, all of which the binary requires once the table exists.
_GROUP_COLUMNS = ("inctyp", "derinc", "derinclb", "forcen", "derincmul", "dermthd")

#: DERMTHD's value where a study states group variables and not this one: the binary prints none for the
#: group it makes itself, and the table demands the column; the users manual (4.8.2) gives this as its usual value.
_DERMTHD = "parabolic"


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
    """One observation: its target value and group, and its sigma in the run's unit - the weight is one over it -
    with how the sigma was reached and each part of it as ``(member, sigma, cite)``."""

    name: str
    value: float
    group: str
    sigma: float
    basis: str
    parts: tuple[tuple[str, float, str], ...] = ()

    @property
    def weight(self) -> float:
        return 1.0 / self.sigma


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
                  stated: Mapping[str, Any] = {}, own: Mapping[str, Any] = {}, balanced: bool = False) -> Path:
    """The control file ``name`` and its tables, written into ``directory`` -> its path.

    ``stated`` are the options the run states, by identifier; ``own`` the options the study's own files
    need, by name. A stated locked option, or one the dictionary does not have, refuses. A parameter-group
    variable writes the group table (every other column the dictionary's default), a parameter column
    replaces that column for every parameter, and any other option is a keyword line. ``balanced`` gives each
    observation group an equal share of phi through PEST++'s own phi factor file."""
    options: dict[str, Any] = {}
    sectioned: dict[str, dict[str, Any]] = {"parameter groups": {}, "parameter data": {}}
    for identifier, value in stated.items():
        slot = PESTPP.MODULE_INPUT.get(str(identifier).upper())
        if slot is None:
            raise PestppError(f"pestpp has no option {identifier!r}", error_code="PESTPP_OPTION_UNKNOWN")
        if slot.keyword in LOCKED:
            raise PestppError(f"{slot.keyword} is the study's own to state: {LOCKED[slot.keyword]}.",
                              error_code="PESTPP_OPTION_LOCKED")
        sectioned.get(slot.rubrique[0], options)[slot.keyword] = value
    options.update(own)
    if balanced:
        shares = list(dict.fromkeys(o.group for o in observations))
        # The file has no header: a tag, then the group's share of phi.
        (directory / TABLES["phi factors"]).write_text("".join(f"{group},{1.0 / len(shares)!r}\n" for group in shares))
        options["ies_phi_factor_file"] = TABLES["phi factors"]
    if not parameters or not observations:
        raise PestppError("a study adjusts at least one parameter against at least one observation",
                          error_code="PESTPP_STUDY_EMPTY")
    changes = sectioned["parameter data"]
    for p in parameters:
        if changes.get("partrans", p.transform) == "log" and changes.get("parchglim") == "relative":
            raise PestppError(f"{p.name} is log-transformed and a log-transformed parameter takes a factor change "
                              "limit only", error_code="PESTPP_OPTION_CONFLICT")
    groups = sectioned["parameter groups"]
    if groups:
        held = [PESTPP.MODULE_INPUT[column.upper()].engine_default for column in _GROUP_COLUMNS]
        _table(directory / TABLES["parameter groups"], ("pargpnme", *_GROUP_COLUMNS),
               [(group, *(groups.get(column, default if default is not UNSET else _DERMTHD)
                          for column, default in zip(_GROUP_COLUMNS, held)))
                for group in dict.fromkeys(p.group for p in parameters)])
    _table(directory / TABLES["parameter data"],
           ("parnme", "partrans", "parchglim", "parval1", "parlbnd", "parubnd", "pargp", "scale", "offset",
            "dercom"),
           [(p.name, changes.get("partrans", p.transform), changes.get("parchglim", "factor"), repr(p.value),
             repr(p.lower), repr(p.upper), p.group, repr(p.scale), repr(p.offset), 1) for p in parameters])
    _table(directory / TABLES["observation data"], ("obsnme", "obsval", "weight", "obgnme"),
           [(o.name, repr(o.value), repr(o.weight), o.group) for o in observations])
    _table(directory / TABLES["model input"], ("pest_file", "model_file"), templates)
    _table(directory / TABLES["model output"], ("pest_file", "model_file"), instructions)
    lines = ["pcf version=2", "* control data keyword", "pestmode estimation",
             *(f"{keyword} {_spelled(value)}" for keyword, value in options.items()),
             *(f"* {section} external\n{table}" for section, table in TABLES.items()
               if section in ("parameter data", "observation data") or (section == "parameter groups" and groups)),
             "* model command line", command,
             *(f"* {section} external\n{table}" for section, table in TABLES.items()
               if section in ("model input", "model output"))]
    path = directory / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def substitute(directory: Path, templates: Sequence[tuple[str, str]],
               parameters: Sequence[Parameter], values: Mapping[str, float]) -> None:
    """Each template written to its model file with ``values`` through each parameter's scale and offset,
    as PEST writes it; a parameter without a value refuses."""
    held = {p.name: p for p in parameters}
    for template, model_file in templates:
        text = (directory / template).read_text().split("\n", 1)[1]

        def written(match: re.Match) -> str:
            name = match.group(1).strip().lower()
            if name not in values:
                raise PestppError(f"{name} has no value to write", error_code="PESTPP_PARAMETER_UNPLACED")
            return f"{values[name] * held[name].scale + held[name].offset:.12G}"

        (directory / model_file).write_text(re.sub(r"~([^~]+)~", written, text))
