"""The control file, written in PEST++'s keyword format: every option stated by name, the tables beside it.

An option the control file does not state is PEST++'s own default, so the file states only what the study
and the run stated. Each table is one CSV with the header PEST++ reads it by.
"""

from __future__ import annotations

import csv
import math
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..errors import PestppError
from ..modules import LOCKED, PESTPP
from ...runtime.module import UNSET

__all__ = ["Observed", "Parameter", "TABLES", "at_bounds", "prior", "sigma_range", "substitute", "unheld_note", "write_control", "write_opt_control"]

#: The tables the control file names, each written beside it.
TABLES = {"parameter groups": "study_groups.csv", "parameter data": "study_parameters.csv",
          "observation data": "study_observations.csv", "model input": "study_templates.csv",
          "model output": "study_instructions.csv", "phi factors": "study_phi_factors.csv",
          "prior": "study_prior.unc", "prior covariance": "study_prior.cov"}

#: The group table's columns, all of which the binary requires once the table exists.
_GROUP_COLUMNS = ("inctyp", "derinc", "derinclb", "forcen", "derincmul", "dermthd")

#: DERMTHD's value where a study states group variables and not this one: the binary prints none for the
#: group it makes itself, and the table demands the column; the users manual (4.8.2) gives this as its usual value.
_DERMTHD = "parabolic"


@dataclass(frozen=True)
class Parameter:
    """One adjustable parameter: its starting value, bounds and group, and the scale and offset PEST++
    writes it through (the value written is ``value * scale + offset``). A pilot point carries its ``site`` and its
    variogram's range (``correlation``), which correlate it with its group's other points in the prior; a parameter
    whose prior is a stated accuracy carries it as ``sd``, with where it is cited (``sd_source``)."""

    name: str
    value: float
    lower: float
    upper: float
    group: str
    transform: str = "log"
    scale: float = 1.0
    offset: float = 0.0
    site: tuple[float, float] | None = None
    correlation: float | None = None
    sd: float | None = None
    sd_source: str | None = None
    variogram: str = "exponential"
    change_limit: str = "factor"
    tied_to: str = ""
    original_name: str = ""
    contribution: float = 1.0
    nugget: float = 0.0
    anisotropy: float = 1.0
    bearing: float = 0.0
    spatial_group: str = ""


@dataclass(frozen=True)
class Observed:
    """One observation: its target value and group, and its sigma in the run's unit - the weight is one over it -
    with how the sigma was reached and each part of it as ``(member, sigma, cite)``. A target held against a gauge
    zero that is a parameter of its own carries that zero's ``(sd, cite)`` as its ``datum``, outside sigma."""

    name: str
    value: float
    group: str
    sigma: float
    basis: str
    parts: tuple[tuple[str, float, str], ...] = ()
    datum: tuple[float, str] | None = None

    @property
    def weight(self) -> float:
        return 1.0 / self.sigma


def unheld_note(inputs: str) -> str:
    """The study's note for the run's supplied ``inputs`` that carry no accuracy on its record and so are no parameter."""
    return (f"the run's supplied inputs ({inputs}) carry no accuracy on its record, so none is a parameter of the study: "
            "an input becomes one only with a stated accuracy, its prior sd")


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


def sigma_range(stated: Mapping[str, Any]) -> float:
    """How many standard deviations a parameter's bounds span, as PEST++ reads them: par_sigma_range as the run
    states it, the binary's own unstated."""
    value = stated.get("PAR_SIGMA_RANGE")
    return float(PESTPP.MODULE_INPUT["PAR_SIGMA_RANGE"].engine_default if value is None else value)


def _sd(p: Parameter, transform: str, span: float) -> float:
    import math

    if p.sd is not None:
        return p.sd
    return ((math.log10(p.upper) - math.log10(p.lower)) if transform == "log" else (p.upper - p.lower)) / span


def _at(value: float, bound: float, log: bool) -> bool:
    # pestpp-ies clips a log-transformed parameter at log10 of its bound, and 10 ** that is the bound to within an ulp.
    import math

    return value == bound or (log and value == 10.0 ** math.log10(bound))


def complete_values(parameters: Sequence[Parameter], values: Mapping[str, float]) -> dict[str, float]:
    """Supply any fixed or tied values omitted from a PEST++ parameter ensemble."""
    completed = dict(values)
    by_name = {p.name: p for p in parameters}
    for p in parameters:
        if p.name not in completed and p.transform == "fixed":
            completed[p.name] = p.value
        elif p.name not in completed and p.transform == "tied":
            parent = by_name[p.tied_to]
            completed[p.name] = p.value / parent.value * completed[parent.name]
    return completed


def at_bounds(parameters: Sequence[Parameter], values: Mapping[str, float], transform: str = ""
              ) -> dict[str, dict[str, int]]:
    """Each parameter group holding a parameter at or past a bound -> ``{group: {"lower", "upper", "of"}}``: PEST++'s
    own rule (Pest::get_pars_at_near_bounds, tolerance 0), on PEST's value as the study's tool wrote it."""
    held: dict[str, dict[str, int]] = {}
    for p in parameters:
        group = held.setdefault(p.group, {"lower": 0, "upper": 0, "of": 0})
        group["of"] += 1
        value, log = float(values[p.name]), (transform or p.transform) == "log"
        if value <= p.lower or _at(value, p.lower, log):
            group["lower"] += 1
        elif value >= p.upper or _at(value, p.upper, log):
            group["upper"] += 1
    return {group: counts for group, counts in held.items() if counts["lower"] or counts["upper"]}


def prior(parameters: Sequence[Parameter], stated: Mapping[str, Any] = {}) -> dict[str, dict[str, Any]]:
    """Each parameter group's prior as the study states it to PEST++ -> ``{group: prior}``: the mean (the starting
    value) and the bounds in the parameter's own terms (a multiplier's 1, an offset's 0: PEST's value with its offset
    over its scale), the transform, the standard deviation - stated, or the one the bounds imply, of log10 where
    log-transformed - and its source, and, for pilot points, their count and the variogram correlating them."""
    span = sigma_range(stated)
    transform = str(dict(stated).get("PARTRANS") or "")
    held: dict[str, dict[str, Any]] = {}
    for p in parameters:
        kind = transform or p.transform
        group = held.setdefault(p.group, {
            "parameters": 0, "mean": p.value + p.offset / p.scale, "lower": p.lower + p.offset / p.scale,
            "upper": p.upper + p.offset / p.scale, "transform": kind,
            "sd": _sd(p, kind, span), "sd_of": "log10" if kind == "log" else "value",
            "sd_source": p.sd_source or f"the bounds read as {span:g} standard deviations (PEST++ par_sigma_range)"})
        group["parameters"] += 1
        if p.site is not None:
            group["correlated"] = {"variogram": p.variogram, "a": p.correlation, "contribution": p.contribution,
                                   "nugget": p.nugget}
            if p.anisotropy != 1.0 or p.bearing != 0.0:
                group["correlated"].update(anisotropy=p.anisotropy, bearing=p.bearing)
    return held


def _covariance(directory: Path, parameters: Sequence[Parameter], transform: str, span: float) -> str | None:
    """The prior PEST++ draws from, where any parameter is a pilot point or states its own sd: each group of points
    correlated by its variogram, every other parameter alone at its stated sd or the one its bounds imply -> the
    uncertainty file's name, else None."""
    import numpy as np

    from ..helpers.geostat import covariance

    sited = [p for p in parameters if p.site is not None]
    if not sited and all(p.sd is None for p in parameters):
        return None
    matrix = np.zeros((len(sited), len(sited)))
    at = 0
    grouped = {}
    for p in sited:
        grouped.setdefault(p.spatial_group or p.group, []).append(p)
    sited = [p for members in grouped.values() for p in members]
    for members in grouped.values():
        variance = np.array([_sd(p, transform or p.transform, span) ** 2 for p in members])
        matrix[at:at + len(members), at:at + len(members)] = covariance(
            np.array([p.site for p in members]), float(members[0].correlation), variance,
            variogram=members[0].variogram, contribution=members[0].contribution, nugget=members[0].nugget,
            anisotropy=members[0].anisotropy, bearing=members[0].bearing)
        at += len(members)
    if sited:
        (directory / TABLES["prior covariance"]).write_text(
            f"{len(sited)} {len(sited)} 1\n" + "".join(" ".join(repr(float(v)) for v in row) + "\n" for row in matrix)
            + "* row and column names\n" + "".join(f"{p.name}\n" for p in sited))
    alone = [p for p in parameters if p.site is None]
    blocks = ["START STANDARD_DEVIATION", *(f"{p.name} {_sd(p, transform or p.transform, span)!r}" for p in alone),
              "END STANDARD_DEVIATION"] if alone else []
    if sited:
        blocks += ["START COVARIANCE_MATRIX", f"file {TABLES['prior covariance']}", "END COVARIANCE_MATRIX"]
    (directory / TABLES["prior"]).write_text("\n".join(blocks) + "\n")
    return TABLES["prior"]


# PEST++ 5.2.27 manual section 4.9.2 and Pest.cpp's external parameter table.
_COLUMNS = {"parnme": "name", "partrans": "transform", "parchglim": "change_limit",
            "parval1": "value", "parlbnd": "lower", "parubnd": "upper", "pargp": "group",
            "scale": "scale", "offset": "offset", "partied": "tied_to"}
_NUMERIC = {"parval1", "parlbnd", "parubnd", "scale", "offset"}
_WORD = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")


def _parameter_error(sentence: str) -> PestppError:
    return PestppError(sentence, error_code="PESTPP_PARAMETER_DATA_INVALID")


def parameter_rows(stated: Any) -> dict[str, dict[str, Any]]:
    """Normalize native table columns without changing the dictionary's global keyword syntax."""
    if stated is None:
        return {}
    if not isinstance(stated, Mapping):
        raise _parameter_error("parameter_data requires rows keyed by PARNME.")
    rows = {}
    for name, raw in stated.items():
        if not isinstance(name, str) or not isinstance(raw, Mapping) or name.lower() in rows:
            raise _parameter_error("parameter_data requires a unique name and a mapping for every row.")
        row = {}
        for column, value in raw.items():
            column = str(column).lower()
            if column in row or column not in {*_COLUMNS, "dercom"}:
                raise _parameter_error(f"{name} states unsupported or repeated column {column.upper()}.")
            if column in _NUMERIC:
                if type(value) not in (int, float) or not math.isfinite(value):
                    raise _parameter_error(f"{name} {column.upper()} must be a finite number.")
                value = float(value)
            elif column == "dercom":
                if type(value) is not int or value != 1:
                    raise _parameter_error(f"{name} DERCOM must be 1 for the study's single model command.")
            elif not isinstance(value, str) or not _WORD.fullmatch(value):
                raise _parameter_error(f"{name} {column.upper()} requires one PEST++ name or option.")
            else:
                value = value.lower()
            row[column] = value
        rows[name.lower()] = row
    return rows


def apply_parameter_data(directory: Path, parameters: Sequence[Parameter],
                         templates: Sequence[tuple[str, str]], stated: Mapping[str, Any],
                         rows: Mapping[str, Mapping[str, Any]]) -> list[Parameter]:
    """Use the same effective parameter row for control, covariance, and calibrated writes."""
    unknown = rows.keys() - {p.name for p in parameters}
    if unknown:
        raise _parameter_error(f"Unknown PARNME in parameter_data: {', '.join(sorted(unknown))}.")
    adjusted = []
    renames = {}
    for p in parameters:
        row = rows.get(p.name, {})
        effective = replace(p, **{"transform": stated.get("PARTRANS", p.transform),
                                  "change_limit": stated.get("PARCHGLIM", p.change_limit),
                                  **{_COLUMNS[k]: v for k, v in row.items() if k in _COLUMNS}})
        if effective.transform not in {"log", "none", "fixed", "tied"}:
            raise _parameter_error(f"{p.name} PARTRANS takes log, none, fixed or tied.")
        if effective.change_limit not in {"factor", "relative"}:
            raise _parameter_error(f"{p.name} PARCHGLIM takes factor or relative.")
        if effective.transform == "log" and effective.change_limit == "relative":
            raise PestppError(f"{p.name} is log-transformed and takes a factor change limit only.",
                              error_code="PESTPP_OPTION_CONFLICT")
        if row and (not effective.lower <= effective.value <= effective.upper
                    or effective.lower == effective.upper or effective.scale == 0
                    or (effective.transform == "log" and effective.lower <= 0)):
            raise _parameter_error(f"{p.name} has incompatible PARVAL1, bounds, SCALE or PARTRANS.")
        if effective.site is not None and min(
                effective.lower * effective.scale + effective.offset,
                effective.upper * effective.scale + effective.offset) <= 0:
            raise _parameter_error(f"{p.name} requires positive written pilot-point values.")
        if effective.name != p.name:
            renames[p.name] = effective.name
            effective = replace(effective, original_name=p.name)
        adjusted.append(effective)
    by_name = {p.name: p for p in adjusted}
    if len(by_name) != len(adjusted):
        raise _parameter_error("PARNME values must be unique.")
    for p in adjusted:
        parent = by_name.get(p.tied_to)
        if p.transform == "tied" and (parent is None or parent.transform in {"fixed", "tied"} or not parent.value):
            raise _parameter_error(f"{p.name} PARTIED must name an adjustable parameter with a nonzero PARVAL1.")
        if p.transform != "tied" and p.tied_to:
            raise _parameter_error(f"{p.name} PARTIED requires PARTRANS tied.")
    if renames:
        def renamed(match: re.Match) -> str:
            source = match[1].strip().lower()
            return "~" + renames.get(source, source).ljust(len(match[1])) + "~" if source in renames else match[0]

        for template, _ in templates:
            path = directory / template
            header, body = path.read_text().split("\n", 1)
            path.write_text(header + "\n" + re.sub(r"~([^~]+)~", renamed, body))
    return adjusted


def write_control(directory: Path, *, name: str, parameters: Sequence[Parameter],
                  observations: Sequence[Observed], templates: Sequence[tuple[str, str]],
                  instructions: Sequence[tuple[str, str]], command: str,
                  stated: Mapping[str, Any] = {}, own: Mapping[str, Any] = {}, balanced: bool = False,
                  parameter_data: Mapping[str, Any] | None = None) -> Path:
    """The control file ``name`` and its tables, written into ``directory`` -> its path.

    ``stated`` are the options the run states, by identifier; ``own`` the options the study's own files
    need, by name. A stated locked option, or one the dictionary does not have, refuses. A parameter-group
    variable writes the group table (every other column the dictionary's default), a parameter column
    replaces that column for every parameter, and any other option is a keyword line. ``balanced`` gives each
    observation group an equal share of phi through PEST++'s own phi factor file. Where a parameter is a pilot point
    the prior PEST++ draws from is written as its parcov."""
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
    parameters = apply_parameter_data(
        directory, parameters, templates, {k.upper(): v for k, v in changes.items()},
        parameter_rows(parameter_data))
    drawn = _covariance(directory, parameters, "", sigma_range(stated))
    if drawn is not None:
        if "parcov" in options:
            raise PestppError("the study's pilot points state the prior PEST++ draws from (parcov), and the run "
                              "states another", error_code="PESTPP_OPTION_CONFLICT")
        options["parcov"] = drawn
    groups = sectioned["parameter groups"]
    if groups:
        held = [PESTPP.MODULE_INPUT[column.upper()].engine_default for column in _GROUP_COLUMNS]
        _table(directory / TABLES["parameter groups"], ("pargpnme", *_GROUP_COLUMNS),
               [(group, *(groups.get(column, default if default is not UNSET else _DERMTHD)
                          for column, default in zip(_GROUP_COLUMNS, held)))
                for group in dict.fromkeys(p.group for p in parameters)])
    tied = any(p.tied_to for p in parameters)
    _table(directory / TABLES["parameter data"],
           ("parnme", "partrans", "parchglim", "parval1", "parlbnd", "parubnd", "pargp", "scale", "offset",
            "dercom", *(("partied",) if tied else ())),
           [(p.name, p.transform, p.change_limit, repr(p.value), repr(p.lower), repr(p.upper), p.group,
             repr(p.scale), repr(p.offset), 1, *((p.tied_to,) if tied else ())) for p in parameters])
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


_OPT_GROUPS = {"<=": "less_than", ">=": "greater_than"}


def _opt_name(row: Mapping[str, Any]) -> str:
    name = row.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]*", name):
        raise PestppError(f"State a native observation name for {name!r}.", error_code="PESTPP_OPT_NAME")
    if not isinstance(row.get("source"), str) or not row["source"].strip():
        raise PestppError(f"State the source for {name}.", error_code="PESTPP_OPT_SOURCE_UNSTATED")
    return name.lower()


def _finite_number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def write_opt_control(directory: Path, *, parameters: Sequence[Parameter],
                      objective: Mapping[str, Any], constraints: Sequence[Mapping[str, Any]],
                      templates: Sequence[tuple[str, str]], instructions: Sequence[tuple[str, str]],
                      command: str, iterations: int) -> Path:
    """Author OPT's deterministic scalar objective, decision groups and hard constraints."""
    if type(iterations) is not int or iterations <= 0:
        raise PestppError("State positive noptmax for optimization.", error_code="PESTPP_OPT_ITERATIONS")
    groups: list[str] = []
    for parameter in parameters:
        lo, hi, start = parameter.lower, parameter.upper, parameter.value
        if not all(_finite_number(number) for number in (lo, hi, start)) or not lo < hi or not lo <= start <= hi:
            raise PestppError(f"State ordered finite bounds containing the starting value of {parameter.name}.",
                              error_code="PESTPP_OPT_BOUNDS_UNSTATED")
        if parameter.transform != "none":
            raise PestppError(f"Decision {parameter.name} requires an untransformed rate.",
                              error_code="PESTPP_OPT_TRANSFORM")
        if parameter.group not in groups:
            groups.append(parameter.group)

    objective_name = _opt_name(objective)
    direction = objective.get("sense")
    if direction not in ("min", "max"):
        raise PestppError(f"State min or max for {objective_name}.", error_code="PESTPP_OPT_SENSE")
    if not constraints:
        raise PestppError("State at least one simulated-output constraint.",
                          error_code="PESTPP_OPT_CONSTRAINT_UNSTATED")
    # The objective row names a forecast, so its target and weight are unused by the solver.
    observations = [Observed(objective_name, 0.0, "objective", math.inf, objective["source"])]
    seen = {objective_name}
    for row in constraints:
        name = _opt_name(row)
        if name in seen:
            raise PestppError(f"Observation name {name} is repeated.", error_code="PESTPP_OPT_NAME")
        seen.add(name)
        sense, limit = row.get("sense"), row.get("limit")
        if sense not in ("<=", ">="):
            raise PestppError(f"State <= or >= for {name}.", error_code="PESTPP_OPT_SENSE")
        if not _finite_number(limit):
            raise PestppError(f"State a finite limit for {name}.", error_code="PESTPP_OPT_LIMIT_UNSTATED")
        observations.append(Observed(name, float(limit), _OPT_GROUPS[sense], 1.0, row["source"]))
    return write_control(
        directory, name="pestpp.pst", parameters=parameters, observations=observations,
        templates=templates, instructions=instructions, command=command,
        stated={"NOPTMAX": iterations, "OPT_DEC_VAR_GROUPS": groups, "OPT_OBJ_FUNC": objective_name,
                "OPT_DIRECTION": direction, "OPT_RISK": 0.5})


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
