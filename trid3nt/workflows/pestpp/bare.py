"""A native PEST++ control file with a stated, installed trial adapter."""

from __future__ import annotations

import asyncio
import math
import re
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Mapping

from trid3nt_contracts import new_ulid

from ..runtime.bare import BareWorkflow, native_assets, native_files, native_name
from ..runtime.fill import Fill, REJECTED, Verdict, production
from ..runtime.module import keyword_floor
from ..runtime.workflow import Deck, RunResult
from ..solver.staging import discard_run, new_rundir
from .authoring.control import Parameter, write_control
from .errors import PestppError
from .staging import CONTROL_FILENAME, stage_study
from .workflow import ENGINES, piece

_TOOLS = frozenset(("glm", "ies", "sen", "swp", "opt", "mou", "da", "sqp"))
_DISPATCH = "trid3nt.workflows.pestpp.engine.solve_case"


@dataclass(frozen=True)
class _Observation:
    name: str
    value: float
    weight: float
    group: str


def _table(rows: Any, fields: dict[str, type]) -> list[dict[str, Any]]:
    if not isinstance(rows, list) or not rows:
        raise PestppError(f"State a nonempty table with {', '.join(fields)}.",
                          error_code="PESTPP_NATIVE_TABLE")
    taken = []
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != fields.keys():
            raise PestppError(f"Each row must state {', '.join(fields)}.",
                              error_code="PESTPP_NATIVE_TABLE")
        result = {}
        for key, expected in fields.items():
            value = row[key]
            if expected is float and (isinstance(value, bool) or not isinstance(value, (float, int))
                                      or not math.isfinite(value)):
                raise PestppError(f"State a finite number for {key}.", error_code="PESTPP_NATIVE_TABLE")
            if expected is str and (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", value)):
                raise PestppError(f"State a PEST++ identifier for {key}.", error_code="PESTPP_NATIVE_TABLE")
            result[key] = expected(value)
        taken.append(result)
    return taken


def _parameters(rows: Any) -> list[Parameter]:
    states = _table(rows, {"PARNME": str, "PARVAL1": float, "PARLBND": float, "PARUBND": float, "PARGP": str,
                           "PARTRANS": str, "PARCHGLIM": str, "SCALE": float, "OFFSET": float})
    result = []
    for row in states:
        position = row["PARVAL1"]
        transform = row["PARTRANS"]
        limit = row["PARCHGLIM"]
        if not row["PARLBND"] <= position <= row["PARUBND"] or (
                transform == "log" and row["PARLBND"] <= 0):
            raise PestppError("State PARVAL1 within PARLBND and PARUBND, positive for a log transform.",
                              error_code="PESTPP_NATIVE_PARAMETER")
        result.append(Parameter(row["PARNME"], position, row["PARLBND"], row["PARUBND"], row["PARGP"],
                                transform=transform, change_limit=limit, scale=row["SCALE"], offset=row["OFFSET"]))
    return result


def _observations(rows: Any) -> list[_Observation]:
    stated = _table(rows, {"OBSNME": str, "OBSVAL": float, "WEIGHT": float, "OBGNME": str})
    if any(row["WEIGHT"] < 0 for row in stated):
        raise PestppError("State each observation's nonnegative WEIGHT.", error_code="PESTPP_NATIVE_OBSERVATION")
    return [_Observation(row["OBSNME"], row["OBSVAL"], row["WEIGHT"], row["OBGNME"]) for row in stated]


def _pairs(value: Any, name: str) -> list[tuple[str, str]]:
    if not isinstance(value, Mapping) or not value:
        raise PestppError(f"State the file pairs for {name}.", error_code="PESTPP_NATIVE_INTERFACE")
    return [(native_name(pest), native_name(model)) for pest, model in value.items()]


def _single_line(value: Any) -> bool:
    if isinstance(value, (list, tuple)):
        return all(_single_line(item) for item in value)
    return not isinstance(value, str) or not any(char in value for char in "\r\n\0")


class BarePestppWorkflow(BareWorkflow):
    engine = "pestpp"

    def judged(self, state: Fill) -> dict[str, Verdict]:
        refused = super().judged(state)
        for key, allowed, code in (("tool", _TOOLS, "PESTPP_TOOL_UNKNOWN"),
                                   ("trial", ENGINES, "PESTPP_ENGINE_UNCALIBRATED")):
            verdict = state.inputs.get(key)
            if verdict and not (isinstance(verdict.value, str) and verdict.value in allowed):
                refused[key] = Verdict(REJECTED, code=code, reason=f"State {key} as one of {', '.join(sorted(allowed))}.")
        return refused

    async def prepare(self, state: Fill) -> Deck:
        env = production(state)
        run = env.run
        tool, trial = run["tool"], run["trial"]
        parameters, observations = _parameters(run["parameters"]), _observations(run["observations"])
        templates, instructions = _pairs(run["templates"], "templates"), _pairs(run["instructions"], "instructions")
        files = native_files(run["files"])
        options, _ = keyword_floor(self.wrappers(), state.keywords)
        if not all(_single_line(value) for value in options.values()):
            raise PestppError("Each native control option must fit on one line.", error_code="PESTPP_NATIVE_OPTION")
        adapter = piece(trial)
        if trial == "telemac":
            steering = native_name(run.get("trial_steering"))
            if not re.fullmatch(r"[A-Za-z0-9_.-]+\.cas", steering) or steering not in files:
                raise PestppError("State trial_steering as a staged TELEMAC-2D steering filename.",
                                  error_code="PESTPP_NATIVE_STEERING")
            command = adapter.command(SimpleNamespace(module="telemac2d", steering=steering))
        else:
            command = adapter.command(None)
            if "mfsim.nam" not in files:
                raise PestppError("Stage mfsim.nam for the MODFLOW trial.", error_code="PESTPP_NATIVE_STEERING")
        for parameter in parameters:
            self.steering.slot("PARTRANS").check(parameter.transform)
            self.steering.slot("PARCHGLIM").check(parameter.change_limit)
        working_id = new_ulid()
        path = new_rundir(working_id, stem=self.engine)
        try:
            await asyncio.to_thread(native_assets, path, files)
            await asyncio.to_thread(
                write_control, path, name=CONTROL_FILENAME, parameters=parameters,
                observations=observations, templates=templates, instructions=instructions,
                command=command, stated=options)
            staged = await stage_study(path, tool=tool, control=CONTROL_FILENAME, name=self.name, model=trial)
        finally:
            await asyncio.to_thread(discard_run, working_id, self.engine, stem=self.engine)
        try:
            return await self.review_native(Deck(state, run_id=staged["run_id"], prefix=self.engine, staged=staged))
        except BaseException:
            await asyncio.to_thread(discard_run, staged["run_id"], self.engine, stem=self.engine)
            raise

    async def solve(self, deck: Deck) -> RunResult:
        from .engine import solve_case

        return await self._solve_native(deck, solve_case)
