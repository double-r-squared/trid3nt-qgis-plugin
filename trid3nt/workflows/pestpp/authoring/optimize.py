"""Deterministic optimization over a supplied native MODFLOW 6 deck."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from ...modflow.authoring.observations import (
    NativeObservation, _native_obs_path, _native_obs_tokens, _obs_block, author_native_observations,
)
from ..errors import PestppError
from .control import Parameter, write_opt_control
from .modflow import command, observe_csv

_PERIOD = re.compile(r"(?ims)^[ \t]*BEGIN[ \t]+PERIOD[ \t]+(\d+)\b[^\n]*\n(.*?)^[ \t]*END[ \t]+PERIOD\b[^\n]*")
_GRID_IDS = {"dis6": 3, "disv6": 2, "disu6": 1}


def _section(path: Path, name: str) -> list[list[str]]:
    block = _obs_block(path.read_text(), name)
    if not block:
        raise PestppError(f"{path.name} does not declare {name}.", error_code="PESTPP_OPT_DECK")
    return _native_obs_tokens(block[1])


def _one(rows: Sequence[list[str]], label: str) -> list[str]:
    if len(rows) != 1:
        raise PestppError(f"Expected one native match for {label}, found {len(rows)}.",
                          error_code="PESTPP_OPT_UNPLACED")
    return rows[0]


def _ids(value: Any) -> tuple:
    if value is None:
        return ()
    return tuple(value) if isinstance(value, (list, tuple)) else (value,)


def _integer(value: Any, label: str, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise PestppError(f"State {label} from 1 to {maximum}.", error_code="PESTPP_OPT_TIME")
    return value


def _metric(row: Mapping[str, Any], steps: Sequence[int]) -> tuple[NativeObservation, int]:
    name = row.get("name")
    if not isinstance(name, str) or not isinstance(row.get("type"), str):
        raise PestppError("State the name and native type of the forecast.", error_code="PESTPP_OPT_UNPLACED")
    if not isinstance(row.get("units"), str) or not row["units"].strip():
        raise PestppError(f"State units for {name}.", error_code="PESTPP_OPT_UNITS")
    owner = row.get("package")
    if owner is not None and (not isinstance(owner, str) or not owner.strip()):
        raise PestppError(f"State a native OBS owner for {name}.", error_code="PESTPP_OPT_UNPLACED")
    period = _integer(row.get("period"), f"{name} period", len(steps))
    step = _integer(row.get("step"), f"{name} step", steps[period - 1])
    forecast = NativeObservation(name, row["type"], _ids(row.get("id")), _ids(row.get("id2")), owner)
    return forecast, sum(steps[:period - 1]) + step


def _wells(root: Path, packages: Sequence[list[str]], decisions: Sequence[Mapping[str, Any]], periods: int
           ) -> tuple[list[Parameter], list[tuple[str, str]]]:
    grid = _one([pkg for pkg in packages if pkg[0].casefold() in _GRID_IDS], "model grid")
    width = _GRID_IDS[grid[0].casefold()]
    bindings: dict[Path, dict[int, dict[tuple[str, ...], Mapping[str, Any]]]] = {}
    names: set[str] = set()
    for row in decisions:
        name, owner = row.get("name"), row.get("package")
        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,29}", name):
            raise PestppError(f"State a decision name, got {name!r}.", error_code="PESTPP_OPT_NAME")
        if name.casefold() in names:
            raise PestppError(f"Decision {name!r} is repeated.", error_code="PESTPP_OPT_NAME")
        names.add(name.casefold())
        if not isinstance(row.get("source"), str) or not row["source"].strip():
            raise PestppError(f"State the bounds source for {name}.", error_code="PESTPP_OPT_SOURCE_UNSTATED")
        if not isinstance(owner, str):
            raise PestppError(f"State the WEL package for {name}.", error_code="PESTPP_OPT_UNPLACED")
        package = _one([pkg for pkg in packages if len(pkg) >= 2 and pkg[0].casefold() == "wel6"
                        and owner.casefold() in {token.casefold() for token in pkg[1:]}], f"{name} package {owner}")
        period = _integer(row.get("period"), f"{name} period", periods)
        cellid = _ids(row.get("cellid"))
        if len(cellid) != width or any(type(index) is not int or index < 1 for index in cellid):
            raise PestppError(f"State the {width}-component native cell ID for {name}.",
                              error_code="PESTPP_OPT_UNPLACED")
        cell = tuple(str(x) for x in cellid)
        selected = bindings.setdefault(_native_obs_path(root, package[1]), {}).setdefault(period, {})
        if cell in selected:
            raise PestppError(f"Decision {name} repeats a WEL cell in period {period}.",
                              error_code="PESTPP_OPT_UNPLACED")
        selected[cell] = row

    parameters, templates = [], []
    for path, by_period in bindings.items():
        text = path.read_text()
        blocks = {int(match[1]): match for match in _PERIOD.finditer(text)}
        replacements: dict[int, str] = {}
        for period, cells in sorted(by_period.items()):
            source = max((n for n in blocks if n <= period), default=None)
            if source is None:
                raise PestppError(f"{path.name} has no WEL rows for period {period}.",
                                  error_code="PESTPP_OPT_UNPLACED")
            lines = blocks[source][2].splitlines()
            for cell, row in cells.items():
                hits = []
                for index, line in enumerate(lines):
                    fields = list(re.finditer(r"[^\s#]+", line.split("#", 1)[0]))
                    if len(fields) > width and tuple(m[0] for m in fields[:width]) == cell:
                        hits.append((index, fields[width]))
                if len(hits) != 1:
                    raise PestppError(f"{row['name']} needs one inline WEL row for cell {cell} in period {period}.",
                                      error_code="PESTPP_OPT_UNPLACED")
                index, rate = hits[0]
                try:
                    initial = float(rate[0].lower().replace("d", "e"))
                except ValueError as exc:
                    raise PestppError(f"{row['name']} needs a numeric native starting rate.",
                                      error_code="PESTPP_OPT_UNPLACED") from exc
                parameter = Parameter(row["name"].lower(), initial, row.get("lower"), row.get("upper"),
                                      "well_rates", transform="none", change_limit="relative")
                parameters.append(parameter)
                lines[index] = (lines[index][:rate.start()] + f"~{parameter.name:<28}~" + lines[index][rate.end():])
            replacements[period] = "\n".join(lines) + "\n"
            if period < periods and period + 1 not in blocks and period + 1 not in by_period:
                replacements[period + 1] = blocks[source][2]
        for period, contents in reversed(sorted(replacements.items())):
            if period in blocks:
                match = blocks[period]
                text = text[:match.start(2)] + contents + text[match.end(2):]
                continue
            previous = blocks[max(n for n in blocks if n < period)]
            text = (text[:previous.end()] + f"\n\nBEGIN PERIOD {period}\n{contents}END PERIOD\n"
                    + text[previous.end():])
        relative = path.relative_to(root).as_posix()
        template = f"{relative}.tpl"
        if (root / template).exists():
            raise PestppError(f"Decision template {template} already exists.", error_code="PESTPP_OPT_DECK")
        (root / template).write_text("ptf ~\n" + text)
        templates.append((template, relative))
    return parameters, templates


def author_optimize(directory: Path, *, model: str, iterations: int, decisions: Sequence[Mapping[str, Any]],
                    objective: Mapping[str, Any], constraints: Sequence[Mapping[str, Any]]
                    ) -> tuple[Path, dict[str, Any]]:
    """Author the native OBS interface and deterministic PEST++ deck in a working copy."""
    root = directory.resolve()
    simulation = root / "mfsim.nam"
    models = [row for row in _section(simulation, "models") if len(row) == 3]
    gwf = _one([row for row in models if row[0].casefold() == "gwf6"
                and row[2].casefold() == model.casefold()], f"GWF model {model}")
    name_file = gwf[1]
    timing = _one([row for row in _section(simulation, "timing") if len(row) == 2
                   and row[0].casefold() == "tdis6"], "TDIS")
    tdis = _native_obs_path(root, timing[1])
    options = _obs_block(tdis.read_text(), "options")
    if options and any(row[0].casefold() == "ats6" for row in _native_obs_tokens(options[1])):
        raise PestppError("Native OPT observation rows require a fixed TDIS clock.", error_code="PESTPP_OPT_TIME")
    steps = [int(row[1]) for row in _section(tdis, "perioddata")]
    forecasts: dict[str, list[NativeObservation]] = {}
    times: dict[str, int] = {}
    names: set[str] = set()
    for row in (objective, *constraints):
        metric, ordinal = _metric(row, steps)
        if metric.name.casefold() in names:
            raise PestppError(f"Observation {metric.name} is repeated.", error_code="PESTPP_OPT_NAME")
        names.add(metric.name.casefold())
        owner = row.get("model", model)
        if not isinstance(owner, str):
            raise PestppError(f"State a model name for {metric.name}.", error_code="PESTPP_OPT_UNPLACED")
        owner_file = _one([item for item in models if item[2].casefold() == owner.casefold()],
                          f"{metric.name} model {owner}")[1]
        forecasts.setdefault(owner_file, []).append(metric)
        times[metric.name] = ordinal
    files = {}
    for index, (owner_file, rows) in enumerate(forecasts.items(), 1):
        stem = "opt_observations" if index == 1 else f"opt_observations_model{index}"
        files.update(author_native_observations(root, owner_file, rows, stem=stem))
    instructions = [observe_csv(root, filename, columns, {name: (name, times[name]) for name in columns})
                    for filename, columns in files.items()]
    parameters, templates = _wells(root, _section(_native_obs_path(root, name_file), "packages"), decisions, len(steps))
    control = write_opt_control(root, parameters=parameters, objective=objective, constraints=constraints,
                                templates=templates, instructions=instructions, command=command(None),
                                iterations=iterations)
    inputs = {row["name"].lower(): row for row in decisions}
    bound = {"decisions": [{**inputs[p.name], "start": p.value} for p in parameters],
             "objective": dict(objective), "constraints": list(constraints),
             "templates": templates, "readings": {name.casefold(): (file, times[name])
                                                  for file, names in files.items() for name in names}}
    return control, bound
