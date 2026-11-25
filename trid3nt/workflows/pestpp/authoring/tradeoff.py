"""Several objectives with their senses over a supplied native MODFLOW 6 deck."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from ...modflow.authoring.observations import (
    NativeObservation, _native_obs_path, _obs_block, _native_obs_tokens, author_native_observations,
)
from ..errors import PestppError
from .control import write_tradeoff_control
from .modflow import command, observe_csv
from .optimize import _metric, _section, _wells


def author_tradeoff(directory: Path, *, model: str, generations: int, population: int, generator: str,
                    seed: int, decisions: Sequence[Mapping[str, Any]],
                    objectives: Sequence[Mapping[str, Any]], constraints: Sequence[Mapping[str, Any]]
                    ) -> tuple[Path, dict[str, Any]]:
    """Author the native OBS interface and the deterministic PESTPP-MOU deck in a working copy."""
    root = directory.resolve()
    simulation = root / "mfsim.nam"
    models = [row for row in _section(simulation, "models") if len(row) == 3]
    gwf = [row for row in models if row[0].casefold() == "gwf6" and row[2].casefold() == model.casefold()]
    if len(gwf) != 1:
        raise PestppError(f"Expected one GWF model {model}, found {len(gwf)}.", error_code="PESTPP_OPT_UNPLACED")
    name_file = gwf[0][1]
    timing = [row for row in _section(simulation, "timing") if len(row) == 2 and row[0].casefold() == "tdis6"]
    if len(timing) != 1:
        raise PestppError("Expected one TDIS clock.", error_code="PESTPP_OPT_TIME")
    tdis = _native_obs_path(root, timing[0][1])
    options = _obs_block(tdis.read_text(), "options")
    if options and any(row[0].casefold() == "ats6" for row in _native_obs_tokens(options[1])):
        raise PestppError("Native trade-off observation rows require a fixed TDIS clock.",
                          error_code="PESTPP_MOU_TIME")
    steps = [int(row[1]) for row in _section(tdis, "perioddata")]
    forecasts: dict[str, list[NativeObservation]] = {}
    times: dict[str, int] = {}
    names: set[str] = set()
    for row in (*objectives, *constraints):
        metric, ordinal = _metric(row, steps)
        if metric.name.casefold() in names:
            raise PestppError(f"Observation {metric.name} is repeated.", error_code="PESTPP_MOU_NAME")
        names.add(metric.name.casefold())
        owner = row.get("model", model)
        if not isinstance(owner, str):
            raise PestppError(f"State a model name for {metric.name}.", error_code="PESTPP_OPT_UNPLACED")
        owners = [item for item in models if item[2].casefold() == owner.casefold()]
        if len(owners) != 1:
            raise PestppError(f"Expected one model {owner} for {metric.name}, found {len(owners)}.",
                              error_code="PESTPP_OPT_UNPLACED")
        forecasts.setdefault(owners[0][1], []).append(metric)
        times[metric.name] = ordinal
    files: dict[str, tuple[str, ...]] = {}
    for index, (owner_file, rows) in enumerate(forecasts.items(), 1):
        stem = "tradeoff_observations" if index == 1 else f"tradeoff_observations_model{index}"
        files.update(author_native_observations(root, owner_file, rows, stem=stem))
    instructions = [observe_csv(root, filename, columns, {name: (name, times[name]) for name in columns})
                    for filename, columns in files.items()]
    parameters, templates = _wells(root, _section(_native_obs_path(root, name_file), "packages"),
                                   decisions, len(steps))
    control = write_tradeoff_control(
        root, parameters=parameters, objectives=objectives, constraints=constraints, templates=templates,
        instructions=instructions, command=command(None), generations=generations, population=population,
        generator=generator, seed=seed)
    inputs = {row["name"].lower(): row for row in decisions}
    bound = {"decisions": [{**inputs[p.name], "start": p.value} for p in parameters],
             "objectives": [dict(row) for row in objectives],
             "constraints": [dict(row) for row in constraints],
             "templates": templates,
             "readings": {name.casefold(): (file, times[name]) for file, names_ in files.items() for name in names_}}
    return control, bound
