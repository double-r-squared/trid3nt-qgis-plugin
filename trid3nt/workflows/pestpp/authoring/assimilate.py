"""A PEST++ v2 sequential DA control and its three cycle tables over a pinned MODFLOW 6 deck."""

from __future__ import annotations

import csv
import math
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from ...modflow.authoring.observations import (
    _native_obs_path, _native_obs_tokens, _obs_block,
)
from ..errors import PestppError
from ..modules import LOCKED, PESTPP
from .control import _spelled

__all__ = ["DA_OPTIONS", "author_assimilate"]

#: PEST++ DA controls the 5.2.27 binary reads - from DataAssimilator.cpp and the tagged
#: benchmarks - that the generated dictionary does not carry (logged for the orchestrator).
DA_OPTIONS = frozenset({
    "da_num_reals", "da_use_mda", "da_parameter_cycle_table", "da_noptmax_schedule",
    "da_verbose_level", "da_lambda_mults", "da_scale_fac", "da_localizer", "da_hotstart_cycle",
    "da_obs_en", "da_par_en", "da_prior", "da_restart_obs_en", "da_subset_how",
    "da_drop_conflicts", "da_autoadaloc", "da_cov_aad",
})

#: The options the study states itself; a run naming one refuses rather than silently overwriting it.
_OWN = {"da_parameter_cycle_table": "par_cycle_tbl.csv",
        "da_observation_cycle_table": "obs_cycle_tbl.csv",
        "da_weight_cycle_table": "weight_cycle_tbl.csv"}

_TRANSFORMS = frozenset(("log", "none", "fixed"))
_NAME = re.compile(r"[a-zA-Z][a-zA-Z0-9_]{0,39}")


def _error(message: str, code: str) -> PestppError:
    return PestppError(message, error_code=code)


def _section(path: Path, name: str) -> list[list[str]]:
    block = _obs_block(path.read_text(), name)
    return [] if block is None else _native_obs_tokens(block[1])


def _clock(root: Path, model: str) -> tuple[list[float], int]:
    """The TDIS period lengths and period count of ``model``, and the grid's presence."""
    models = [row for row in _section(root / "mfsim.nam", "models") if len(row) == 3]
    found = [row for row in models if row[0].casefold() == "gwf6" and row[2].casefold() == model.casefold()]
    if len(found) != 1:
        raise _error(f"State the GWF model name in mfsim.nam; {model!r} names {len(found)}.",
                     "PESTPP_DA_MODEL")
    packages = _section(_native_obs_path(root, found[0][1]), "packages")
    if not any(row and row[0].casefold().removesuffix("6") in {"dis", "disv", "disu"} for row in packages):
        raise _error(f"The GWF model {model!r} declares no DIS, DISV or DISU grid.", "PESTPP_DA_GRID")
    timing = [row for row in _section(root / "mfsim.nam", "timing")
              if len(row) == 2 and row[0].casefold() == "tdis6"]
    if len(timing) != 1:
        raise _error("State one TDIS clock in mfsim.nam; sequential DA needs a fixed clock.",
                     "PESTPP_DA_CYCLE_CLOCK")
    tdis = _native_obs_path(root, timing[0][1])
    options = _obs_block(tdis.read_text(), "options")
    if options and any(row and row[0].casefold() == "ats6" for row in _native_obs_tokens(options[1])):
        raise _error("Sequential DA needs a fixed TDIS clock, not adaptive time stepping (ATS6).",
                     "PESTPP_DA_CYCLE_CLOCK")
    rows = _section(tdis, "perioddata")
    if not rows:
        raise _error("The TDIS clock states no period data.", "PESTPP_DA_CYCLE_CLOCK")
    try:
        lengths = [float(row[0]) for row in rows]
    except (IndexError, ValueError) as error:
        raise _error("The TDIS period data does not state a period length in its first column.",
                     "PESTPP_DA_CYCLE_CLOCK") from error
    return lengths, len(rows)


def _cycles(stated: Sequence[Mapping[str, Any]], lengths: Sequence[float]) -> list[dict[str, Any]]:
    if not stated:
        raise _error("State at least one cycle window.", "PESTPP_DA_CYCLE_ORDER")
    out, ids, periods = [], [], []
    for row in stated:
        cycle, period = row.get("id"), row.get("period")
        if type(cycle) is not int or cycle < 0:
            raise _error(f"State a zero-based integer cycle id, not {cycle!r}.", "PESTPP_DA_CYCLE_ORDER")
        if type(period) is not int or period < 1:
            raise _error(f"Cycle {cycle} states a one-based stress period, not {period!r}.",
                         "PESTPP_DA_CYCLE_CLOCK")
        length = row.get("length")
        if length is not None and (type(length) not in (int, float) or not math.isfinite(length) or length <= 0):
            raise _error(f"Cycle {cycle} states a positive period length, not {length!r}.",
                         "PESTPP_DA_CYCLE_CLOCK")
        ids.append(cycle)
        periods.append(period)
        out.append({"id": cycle, "period": period, "length": length})
    if ids != sorted(ids) or len(set(ids)) != len(ids):
        raise _error("Cycles are ordered by strictly increasing integer id, each stated once.",
                     "PESTPP_DA_CYCLE_ORDER")
    if periods != sorted(periods) or len(set(periods)) != len(periods):
        raise _error("Cycle windows are ordered and non-overlapping: their stress periods strictly ascend.",
                     "PESTPP_DA_CYCLE_ORDER")
    for row in out:
        if row["period"] > len(lengths):
            raise _error(f"Cycle {row['id']} names stress period {row['period']}, and the TDIS clock holds "
                         f"{len(lengths)}.", "PESTPP_DA_CYCLE_CLOCK")
        if row["length"] is not None and not math.isclose(float(row["length"]),
                                                          float(lengths[row["period"] - 1]),
                                                          rel_tol=1e-9, abs_tol=1e-9):
            raise _error(f"Cycle {row['id']} states length {row['length']!r} days and the TDIS period "
                         f"{row['period']} is {lengths[row['period'] - 1]!r} days.",
                         "PESTPP_DA_CYCLE_CLOCK")
        if row["length"] is None:
            row["length"] = float(lengths[row["period"] - 1])
    return out


def _cycle_values(row: Mapping[str, Any], ids: Sequence[int], label: str, code: str) -> dict[int, float]:
    values: dict[int, float] = {}
    for key, value in dict(row.get(label) or {}).items():
        try:
            cycle = int(key)
        except (TypeError, ValueError) as error:
            raise _error(f"{row.get('name')!r} states cycle {key!r} in its {label} table.",
                         "PESTPP_DA_CYCLE_TABLE") from error
        if cycle not in ids:
            raise _error(f"{row.get('name')!r} states cycle {cycle} in its {label} table, and the study holds "
                         f"{list(ids)}.", "PESTPP_DA_CYCLE_TABLE")
        if type(value) not in (int, float) or not math.isfinite(value):
            raise _error(f"{row.get('name')!r} states a finite value for cycle {cycle} in its {label} table.",
                         code)
        values[cycle] = float(value)
    return values


def _parameter(root: Path, row: Mapping[str, Any], ids: Sequence[int]) -> dict[str, Any]:
    name = row.get("name")
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise _error(f"State a parameter name, got {name!r}.", "PESTPP_DA_PARAMETER_NAME")
    if not isinstance(row.get("source"), str) or not row["source"].strip():
        raise _error(f"State the source for parameter {name}.", "PESTPP_DA_PARAMETER_SOURCE")
    value, lower, upper = row.get("value"), row.get("lower"), row.get("upper")
    if (type(value) not in (int, float) or type(lower) not in (int, float)
            or type(upper) not in (int, float) or not all(math.isfinite(v) for v in (value, lower, upper))
            or not lower < upper or not lower <= value <= upper):
        raise _error(f"State finite ordered bounds containing the starting value of {name}.",
                     "PESTPP_DA_PARAMETER_BOUNDS")
    transform = row.get("transform", "log")
    if transform not in _TRANSFORMS:
        raise _error(f"Parameter {name} takes transform log, none or fixed, not {transform!r}.",
                     "PESTPP_DA_PARAMETER_TRANSFORM")
    if transform == "log" and lower <= 0:
        raise _error(f"Log-transformed parameter {name} needs a positive lower bound.",
                     "PESTPP_DA_PARAMETER_BOUNDS")
    cycle = row.get("cycle", -1)
    if type(cycle) is not int or (cycle != -1 and cycle not in ids):
        raise _error(f"Parameter {name} names cycle {cycle!r}; state -1 or one of {list(ids)}.",
                     "PESTPP_DA_PARAMETER_CYCLE")
    template, model_file = row.get("template"), row.get("model_file")
    if not isinstance(template, str) or not (root / template).is_file():
        raise _error(f"Parameter {name} names template {template!r}, which the study does not hold.",
                     "PESTPP_DA_PARAMETER_TEMPLATE")
    if not isinstance(model_file, str) or not model_file.strip() or not (root / model_file).parent.is_dir():
        raise _error(f"Parameter {name} names model file {model_file!r}, whose folder the study does not hold.",
                     "PESTPP_DA_PARAMETER_TEMPLATE")
    values = _cycle_values(row, ids, "values", "PESTPP_DA_PARAMETER_BOUNDS")
    if values and not (transform == "fixed" and cycle == -1):
        raise _error(f"Parameter {name} states per-cycle values; only a fixed cycle=-1 parameter is a cycle "
                     "table entry.", "PESTPP_DA_CYCLE_TABLE")
    state = row.get("state", False)
    if not isinstance(state, bool):
        raise _error(f"Parameter {name} states state as a boolean, not {state!r}.", "PESTPP_DA_STATE_UNLINKED")
    if state and not (transform == "fixed" and cycle == -1):
        raise _error(f"State parameter {name} is fixed and cycle=-1.", "PESTPP_DA_STATE_UNLINKED")
    return {"name": name, "template": template, "model_file": model_file, "value": float(value),
            "lower": float(lower), "upper": float(upper), "group": str(row.get("group") or "parameters"),
            "transform": transform, "cycle": cycle, "source": row["source"].strip(), "state": state,
            "values": values, "state_par_link": str(row.get("state_par_link") or "")}


def _observation(root: Path, row: Mapping[str, Any], ids: Sequence[int]) -> dict[str, Any]:
    name = row.get("name")
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise _error(f"State an observation name, got {name!r}.", "PESTPP_DA_OBSERVATION_NAME")
    if not isinstance(row.get("source"), str) or not row["source"].strip():
        raise _error(f"State the source for observation {name}.", "PESTPP_DA_OBSERVATION_SOURCE")
    value = row.get("value")
    if type(value) not in (int, float) or not math.isfinite(value):
        raise _error(f"State a finite observed value for {name}.", "PESTPP_DA_OBSERVATION_VALUE")
    weight = row.get("weight")
    if type(weight) not in (int, float) or not math.isfinite(weight) or weight < 0:
        raise _error(f"State a finite non-negative weight for {name}.", "PESTPP_DA_OBSERVATION_WEIGHT")
    cycle = row.get("cycle", -1)
    if type(cycle) is not int or (cycle != -1 and cycle not in ids):
        raise _error(f"Observation {name} names cycle {cycle!r}; state -1 or one of {list(ids)}.",
                     "PESTPP_DA_OBSERVATION_CYCLE")
    instruction, model_file = row.get("instruction"), row.get("model_file")
    per_cycle = row.get("instructions") or {}
    if not isinstance(per_cycle, Mapping):
        raise _error(f"Observation {name} states instructions as a mapping of cycle id to file.",
                     "PESTPP_DA_OBSERVATION_INSTRUCTION")
    instructions: dict[int, str] = {}
    for key, filename in per_cycle.items():
        try:
            at = int(key)
        except (TypeError, ValueError) as error:
            raise _error(f"Observation {name} states instruction cycle {key!r}.",
                         "PESTPP_DA_OBSERVATION_INSTRUCTION") from error
        if at not in ids:
            raise _error(f"Observation {name} states an instruction for cycle {at}, and the study holds "
                         f"{list(ids)}.", "PESTPP_DA_CYCLE_TABLE")
        if not isinstance(filename, str) or not (root / filename).is_file():
            raise _error(f"Observation {name} names instruction {filename!r}, which the study does not hold.",
                         "PESTPP_DA_OBSERVATION_INSTRUCTION")
        instructions[at] = filename
    if instruction is not None and (not isinstance(instruction, str) or not (root / instruction).is_file()):
        raise _error(f"Observation {name} names instruction {instruction!r}, which the study does not hold.",
                     "PESTPP_DA_OBSERVATION_INSTRUCTION")
    if instruction is None and not instructions:
        raise _error(f"Observation {name} names no instruction file.", "PESTPP_DA_OBSERVATION_INSTRUCTION")
    if not isinstance(model_file, str) or not model_file.strip():
        raise _error(f"Observation {name} names no model output.", "PESTPP_DA_OBSERVATION_INSTRUCTION")
    values = _cycle_values(row, ids, "values", "PESTPP_DA_OBSERVATION_VALUE")
    weights = _cycle_values(row, ids, "weights", "PESTPP_DA_OBSERVATION_WEIGHT")
    link = row.get("state_par_link") or ""
    if link and not isinstance(link, str):
        raise _error(f"Observation {name} names a state parameter link as a string.", "PESTPP_DA_STATE_UNLINKED")
    return {"name": name, "instruction": instruction or "", "instructions": instructions,
            "model_file": model_file, "group": str(row.get("group") or "observations"), "value": float(value),
            "weight": float(weight), "cycle": cycle, "source": row["source"].strip(),
            "values": values, "weights": weights, "state_par_link": str(link)}


def _state(parameters: Sequence[Mapping[str, Any]], observations: Sequence[Mapping[str, Any]]
           ) -> list[dict[str, str]]:
    states = {row["name"]: row for row in parameters if row["state"]}
    pairs, linked = [], set()
    for row in observations:
        link = row["state_par_link"] or (row["name"] if row["name"] in states else "")
        if not link:
            continue
        if link not in states:
            raise _error(f"Observation {row['name']} links state parameter {link!r}, which is not a state "
                         "parameter of the study.", "PESTPP_DA_STATE_UNLINKED")
        if link in linked:
            raise _error(f"State parameter {link!r} is linked by more than one observation.",
                         "PESTPP_DA_STATE_UNLINKED")
        linked.add(link)
        pairs.append({"parameter": link, "observation": row["name"]})
    missing = sorted(set(states) - linked)
    if missing:
        raise _error(f"State parameter {missing[0]!r} has no linked end-of-cycle observation; add an "
                     "observation of the same name or a state_par_link.", "PESTPP_DA_STATE_UNLINKED")
    return pairs


def _option(identifier: Any, value: Any) -> tuple[str, Any]:
    key = str(identifier)
    slot = PESTPP.MODULE_INPUT.get(key.upper())
    if slot is not None:
        if slot.keyword in LOCKED:
            raise _error(f"{slot.keyword} is the study's own to state: {LOCKED[slot.keyword]}.",
                         "PESTPP_OPTION_LOCKED")
        if slot.keyword in _OWN:
            raise _error(f"{slot.keyword} is the study's own cycle table; it refuses a stated value.",
                         "PESTPP_OPTION_CONFLICT")
        slot.check(value)
        return slot.keyword, value
    if key.lower() in DA_OPTIONS:
        if key.lower() in _OWN:
            raise _error(f"{key.lower()} is the study's own cycle table; it refuses a stated value.",
                         "PESTPP_OPTION_CONFLICT")
        return key.lower(), value
    raise _error(f"pestpp has no option {identifier!r}", "PESTPP_OPTION_UNKNOWN")


def _table(path: Path, header: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def _cycle_table(path: Path, ids: Sequence[int], rows: Sequence[tuple[str, Mapping[int, float]]]) -> None:
    """The parser's orientation: the first column the quantity name, the remaining headers integer cycle ids."""
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["", *(str(cycle) for cycle in ids)])
        writer.writerows([name, *(f"{float(values[cycle])!r}" if cycle in values else "" for cycle in ids)]
                         for name, values in rows)


def author_assimilate(directory: Path, *, model: str, cycles: Sequence[Mapping[str, Any]],
                      parameters: Sequence[Mapping[str, Any]], observations: Sequence[Mapping[str, Any]],
                      options: Mapping[str, Any] | None = None, command: str = "mf6") -> dict[str, Any]:
    """Write the v2 DA control and its three cycle tables in a working copy -> the bound study."""
    root = directory.resolve()
    lengths, _ = _clock(root, model)
    windows = _cycles(cycles, lengths)
    ids = [row["id"] for row in windows]
    held = [_parameter(root, row, ids) for row in parameters]
    if not held:
        raise _error("State at least one adjustable parameter for the study.", "PESTPP_DA_PARAMETER_NAME")
    if len({row["name"] for row in held}) != len(held):
        raise _error("Parameter names are unique.", "PESTPP_DA_PARAMETER_NAME")
    seen_obs: set[str] = set()
    observed = []
    for row in observations:
        if row.get("name") in seen_obs:
            raise _error(f"Observation {row.get('name')!r} is repeated.", "PESTPP_DA_OBSERVATION_NAME")
        item = _observation(root, row, ids)
        seen_obs.add(item["name"])
        observed.append(item)
    if not observed:
        raise _error("State at least one observation for the study.", "PESTPP_DA_OBSERVATION_NAME")
    pairs = _state(held, observed)

    _table(root / "study_parameters.csv",
           ("parnme", "partrans", "parchglim", "parval1", "parlbnd", "parubnd", "pargp", "scale", "offset",
            "dercom", "cycle"),
           [(row["name"], row["transform"], "factor", repr(row["value"]), repr(row["lower"]),
             repr(row["upper"]), row["group"], "1.0", "0.0", 1, row["cycle"]) for row in held])
    # A shared name tags the pair; the link column is only for pairs whose names differ.
    linked = {row["observation"]: row["parameter"] for row in pairs
              if row["parameter"] != row["observation"]}
    _table(root / "study_observations.csv",
           ("obsnme", "obsval", "weight", "obgnme", "cycle", "state_par_link"),
           [(row["name"], repr(row["value"]), repr(row["weight"]), row["group"], row["cycle"],
             linked.get(row["name"], "")) for row in observed])
    templates = list(dict.fromkeys((row["template"], row["model_file"], row["cycle"]) for row in held))
    _table(root / "study_templates.csv", ("pest_file", "model_file", "cycle"), templates)
    instructions: list[tuple[str, str, int]] = []
    for row in observed:
        if row["instructions"]:
            instructions += [(filename, row["model_file"], cycle)
                             for cycle, filename in sorted(row["instructions"].items())]
        else:
            instructions.append((row["instruction"], row["model_file"], row["cycle"]))
    instructions = list(dict.fromkeys(instructions))
    _table(root / "study_instructions.csv", ("pest_file", "model_file", "cycle"), instructions)

    parameter_rows: list[tuple[str, dict[int, float]]] = []
    if any(row["name"] == "perlen" for row in held):
        parameter_rows.append(("perlen", {row["id"]: float(row["length"]) for row in windows}))
    parameter_rows += [(row["name"], row["values"]) for row in held if row["values"]]
    observation_rows = [(row["name"], row["values"]) for row in observed if row["values"]]
    weight_rows = [(row["name"], row["weights"]) for row in observed if row["weights"]]
    _cycle_table(root / "par_cycle_tbl.csv", ids, parameter_rows)
    _cycle_table(root / "obs_cycle_tbl.csv", ids, observation_rows)
    _cycle_table(root / "weight_cycle_tbl.csv", ids, weight_rows)

    stated = {str(key): value for key, value in dict(options or {}).items()}
    lines: list[str] = []
    for key, value in stated.items():
        keyword, kept = _option(key, value)
        lines.append(f"{keyword} {_spelled(kept)}")
    for keyword, filename in _OWN.items():
        lines.append(f"{keyword} {filename}")
    if not isinstance(command, str) or not command.strip():
        raise _error("State the trial command, mf6 by default.", "PESTPP_DA_COMMAND")
    control = ["pcf version=2", "* control data keyword", "pestmode estimation", *lines,
               "* parameter data external", "study_parameters.csv",
               "* observation data external", "study_observations.csv",
               "* model command line", command.strip(),
               "* model input external", "study_templates.csv",
               "* model output external", "study_instructions.csv"]
    (root / "pestpp.pst").write_text("\n".join(control) + "\n", encoding="utf-8")
    return {"cycles": ids, "windows": windows, "parameters": held, "observations": observed, "state": pairs,
            "templates": [list(row[:2]) for row in templates],
            "readings": {row["name"]: [row["instruction"], row["model_file"]] for row in observed},
            "cycle_tables": {"parameters": _OWN["da_parameter_cycle_table"],
                              "observations": _OWN["da_observation_cycle_table"],
                              "weights": _OWN["da_weight_cycle_table"]}}
