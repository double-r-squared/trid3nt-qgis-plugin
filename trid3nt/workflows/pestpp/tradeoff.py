"""A reviewed deterministic MODFLOW trade-off study: several objectives, a feasible non-dominated set."""

from __future__ import annotations

import asyncio
import csv
import json
import math
import shutil
import tempfile
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import Field
from trid3nt_contracts.payload_warning import ParamSheet, ParamSheetRow

from trid3nt.errors import DeclarationError
from trid3nt.inputs.review import gate_input_review

from ..runtime.bare import BareWorkflow, NativeResult, native_assets, native_files
from ..runtime.fill import production
from ..runtime.review import _entries
from ..runtime.sheet import run as solve_staged
from ..runtime.workflow import Deck, RunResult
from ..solver.staging import discard_run
from ..solver.stop import SolveStopped
from .errors import PestppError
from .optimization import _constraint

_DISPATCH = "trid3nt.workflows.pestpp.engine.solve_case"

#: The pinned files a finished trade-off is read by: the archive's summary and its two populations.
_ARCHIVE = "pestpp.pareto.archive.summary.csv"
_DECISIONS = "pestpp.archive.dv_pop.csv"
_OBSERVED = "pestpp.archive.obs_pop.csv"
_REQUIRED = (_ARCHIVE, _DECISIONS, _OBSERVED)


class TradeoffResult(NativeResult):
    """The feasible non-dominated set, each candidate's decisions and objectives, and its constraint slack."""

    outcome: Literal["feasible", "empty"]
    manifest_uri: str
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    objectives: list[dict[str, Any]] = Field(default_factory=list)
    constraints: list[dict[str, Any]] = Field(default_factory=list)
    bindings: dict[str, Any]


def _number(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise PestppError(f"The trade-off archive holds no numeric value for {name}.",
                          error_code="PESTPP_MOU_ARCHIVE") from error
    if not math.isfinite(number) or number == 3.0e30:
        raise PestppError(f"The trade-off archive holds no simulated value for {name}.",
                          error_code="PESTPP_MOU_OUTPUT")
    return number


def _rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise PestppError(f"{path.name} is not among the study's files, so its trade-off cannot be read.",
                          error_code="PESTPP_MOU_OUTPUT")
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _population(path: Path) -> dict[str, dict[str, float]]:
    """A PEST++ ensemble CSV, its first column the member -> ``{member: {name: value}}``."""
    rows = _rows(path)
    if not rows:
        raise PestppError(f"{path.name} holds no members.", error_code="PESTPP_MOU_ARCHIVE")
    member_field = next(iter(rows[0]))
    population: dict[str, dict[str, float]] = {}
    for row in rows:
        member = str(row.pop(member_field, "")).strip()
        if not member:
            raise PestppError(f"{path.name} holds a row without a member name.", error_code="PESTPP_MOU_ARCHIVE")
        key = member.casefold()
        if key in population:
            raise PestppError(f"{path.name} repeats member {member}.", error_code="PESTPP_MOU_ARCHIVE")
        population[key] = {str(name).casefold(): _number(value, str(name))
                           for name, value in row.items() if value not in (None, "")}
    return population


def _feasible(row: Mapping[str, Any]) -> bool:
    return str(row.get("is_feasible", "")).strip().lower() in {"1", "1.0", "true"}


def _dominates(first: Mapping[str, Any], second: Mapping[str, Any],
               objectives: list[Mapping[str, Any]]) -> bool:
    better = False
    for objective in objectives:
        name, sense = str(objective["name"]).casefold(), objective["sense"]
        left, right = first["objectives"][name], second["objectives"][name]
        if sense == "min":
            if left > right:
                return False
            better |= left < right
        else:
            if left < right:
                return False
            better |= left > right
    return better


def _candidates(directory: Path, bound: Mapping[str, Any]
                ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The final archive's feasible members, joined to their decisions and observations."""
    rows = _rows(directory / _ARCHIVE)
    if not rows:
        raise PestppError(f"{_ARCHIVE} holds no generation.", error_code="PESTPP_MOU_ARCHIVE")
    try:
        generation = max(int(float(row["generation"])) for row in rows)
        latest: dict[str, dict[str, str]] = {}
        seen_latest: set[str] = set()
        for row in rows:
            member = str(row.get("member", "")).strip()
            if not member:
                raise PestppError(f"{_ARCHIVE} holds a row without a member name.",
                                  error_code="PESTPP_MOU_ARCHIVE")
            at = int(float(row["generation"]))
            key = member.casefold()
            if at == generation:
                if key in seen_latest:
                    raise PestppError(f"{_ARCHIVE} repeats member {member} in generation {generation}.",
                                      error_code="PESTPP_MOU_ARCHIVE")
                seen_latest.add(key)
            if key not in latest or at >= int(float(latest[key]["generation"])):
                latest[key] = row
    except (KeyError, TypeError, ValueError) as error:
        raise PestppError(f"{_ARCHIVE} does not state its generations and members.",
                          error_code="PESTPP_MOU_ARCHIVE") from error
    decisions = _population(directory / _DECISIONS)
    observed = _population(directory / _OBSERVED)
    if choices := (decisions.keys() ^ observed.keys()):
        member = next(iter(choices))
        raise PestppError(f"The archive states member {member} in one population and not the other.",
                          error_code="PESTPP_MOU_ARCHIVE")
    objectives = [dict(row) for row in bound["objectives"]]
    constraints = [dict(row) for row in bound["constraints"]]
    objective_names = [str(row["name"]).casefold() for row in objectives]
    candidates = []
    for key, chosen in decisions.items():
        row = latest.get(key)
        if row is None:
            raise PestppError(f"{key} is in the archive population and not in its summary.",
                              error_code="PESTPP_MOU_ARCHIVE")
        member = str(row["member"]).strip()
        values = {}
        for name in objective_names:
            if name not in observed[key]:
                raise PestppError(f"{member} states no value for objective {name}.",
                                  error_code="PESTPP_MOU_ARCHIVE")
            values[name] = _number(observed[key][name], name)
        decided = {}
        for decision in bound["decisions"]:
            name = str(decision["name"]).casefold()
            if name not in chosen:
                raise PestppError(f"{member} states no value for decision {decision['name']}.",
                                  error_code="PESTPP_MOU_ARCHIVE")
            value = chosen[name]
            if not float(decision["lower"]) <= value <= float(decision["upper"]):
                raise PestppError(f"The trade-off candidate {member} holds {decision['name']} outside its bounds.",
                                  error_code="PESTPP_MOU_OUTPUT")
            decided[name] = value
        slacks = []
        for constraint in constraints:
            name = str(constraint["name"]).casefold()
            if name not in observed[key]:
                raise PestppError(f"{member} states no value for constraint {constraint['name']}.",
                                  error_code="PESTPP_MOU_ARCHIVE")
            simulated = observed[key][name]
            limit = float(constraint["limit"])
            slacks.append({**constraint, "simulated": simulated,
                           "slack": limit - simulated if constraint["sense"] == "<=" else simulated - limit})
        try:
            front = int(float(row.get("nsga2_front", 1)))
        except (TypeError, ValueError) as error:
            raise PestppError(f"{member} states no Pareto front.", error_code="PESTPP_MOU_ARCHIVE") from error
        candidates.append({"id": member, "decisions": decided, "objectives": values,
                           "feasible": _feasible(row), "front": front, "rank": 1, "constraints": slacks})
    feasible = [candidate for candidate in candidates if candidate["feasible"]]
    for candidate in feasible:
        dominating = [other for other in feasible
                      if other["id"] != candidate["id"] and _dominates(other, candidate, objectives)]
        if dominating:
            raise PestppError(f"The archive holds {candidate['id']} after "
                              f"{dominating[0]['id']} dominates it in the stated directions.",
                              error_code="PESTPP_MOU_DOMINATED")
    return feasible, objectives


def read_result(directory: Path, *, run_id: str, manifest_uri: str, bound: dict[str, Any],
                uri: str = "") -> TradeoffResult:
    """Read the last generation of the archive: its feasible non-dominated candidates, decisions and objectives."""
    candidates, objectives = _candidates(directory, bound)
    return TradeoffResult(run_id=run_id, uri=uri, manifest_uri=manifest_uri,
                          outcome="feasible" if candidates else "empty", candidates=candidates,
                          objectives=objectives, constraints=[dict(row) for row in bound["constraints"]],
                          bindings=bound)


async def _publish_charts(result: TradeoffResult) -> None:
    from trid3nt.render.charts import build_chart_payload
    from trid3nt.render.formats import Chart, Deliverable, publish

    items = []
    names = [str(row["name"]).casefold() for row in result.objectives]
    if len(names) >= 2 and result.candidates:
        first = result.objectives[0]
        second = result.objectives[1]
        points = [{"candidate": candidate["id"], "front": candidate["front"],
                   **candidate["objectives"]} for candidate in result.candidates]
        payload = build_chart_payload(
            title="Pareto trade-off", vega_lite_spec={
                "mark": {"type": "point", "filled": True, "size": 90},
                "data": {"values": points},
                "encoding": {"x": {"field": names[0], "type": "quantitative",
                                   "title": f"{first['name']} ({first.get('units') or 'no unit'})"},
                             "y": {"field": names[1], "type": "quantitative",
                                   "title": f"{second['name']} ({second.get('units') or 'no unit'})"},
                             "tooltip": [{"field": "candidate", "type": "nominal"},
                                         {"field": "front", "type": "quantitative"},
                                         *({"field": name, "type": "quantitative"} for name in names)]}})
        items.append(Deliverable(product=Chart(payload=payload), caption="Pareto trade-off"))
    if result.constraints and result.candidates:
        values = [{"candidate": candidate["id"], "constraint": row["name"], "slack": row["slack"]}
                  for candidate in result.candidates for row in candidate["constraints"]]
        payload = build_chart_payload(
            title="Constraint slack by candidate", vega_lite_spec={
                "data": {"values": values},
                "mark": {"type": "bar"},
                "encoding": {"x": {"field": "candidate", "type": "nominal"},
                             "y": {"field": "slack", "type": "quantitative"},
                             "xOffset": {"field": "constraint", "type": "nominal"},
                             "color": {"field": "constraint", "type": "nominal"},
                             "tooltip": [{"field": "candidate", "type": "nominal"},
                                         {"field": "constraint", "type": "nominal"},
                                         {"field": "slack", "type": "quantitative"}]}})
        items.append(Deliverable(product=Chart(payload=payload), caption="Constraint slack"))
    if items:
        await publish(run_id=result.run_id, engine="pestpp", name="Trade-off", items=items)


class TradeoffWorkflow(BareWorkflow):
    """Bind caller-stated objectives and decisions to a reviewed PESTPP-MOU study."""

    engine = "pestpp"

    async def prepare(self, state) -> Deck:
        from ..modflow.authoring.validate import validate_authored_simulation
        from . import staging
        from .authoring.tradeoff import author_tradeoff

        held = production(state).run
        with tempfile.TemporaryDirectory(prefix="pestpp-mou-") as work:
            directory = Path(work)
            await asyncio.to_thread(native_assets, directory, native_files(held["files"]))
            _, bound = await asyncio.to_thread(
                author_tradeoff, directory, model=held["model"], generations=held["generations"],
                population=held["population"], generator=held["generator"], seed=held["seed"],
                decisions=held["decisions"], objectives=held["objectives"],
                constraints=held.get("constraints") or ())
            with tempfile.TemporaryDirectory(prefix="pestpp-mou-validation-") as validation:
                await asyncio.to_thread(shutil.copytree, directory, validation, dirs_exist_ok=True)
                await asyncio.to_thread(validate_authored_simulation, Path(validation))
            staged = await staging.stage_study(directory, tool="mou", control=staging.CONTROL_FILENAME,
                                               name=self.name, model="modflow", required=_REQUIRED)
        deck = Deck(state, run_id=staged["run_id"], prefix=self.engine, staged=staged, held={"tradeoff": bound})
        try:
            rows = [
                ParamSheetRow(name=name, value=value if isinstance(value, (str, int)) else json.dumps(value),
                              origin="user", editable=False)
                for name, value in (("model", held["model"]), ("generations", held["generations"]),
                                    ("population", held["population"]), ("generator", held["generator"]),
                                    ("seed", held["seed"]), ("decisions", bound["decisions"]),
                                    ("objectives", bound["objectives"]), ("constraints", bound["constraints"]))
            ]
            review = ParamSheet(workflow=self.name, title="Review the trade-off study", rows=rows)
            answer = await gate_input_review(tool_name=self.name, entries=_entries(rows),
                                             params={}, param_sheet=review)
            if not answer.proceed:
                raise DeclarationError(answer.cancel_reason or "The trade-off study was declined.",
                                       error_code="NO_SESSION" if answer.cancel_code == "no_session"
                                       else "USER_INPUT_CANCELLED")
            return deck
        except BaseException:
            await asyncio.to_thread(discard_run, deck.run_id, self.engine, stem=self.engine)
            raise

    async def solve(self, deck: Deck) -> RunResult:
        from . import engine, outputs

        try:
            handle = await solve_staged(deck.staged, dispatch=engine.solve_case)
        except SolveStopped as stop:
            async def partial(_handle, _reason):
                # Only a finished archive certifies a feasible set.
                return None

            await self.stopped(stop, {}, partial)
        with tempfile.TemporaryDirectory(prefix="pestpp-mou-result-") as work:
            directory = await asyncio.to_thread(outputs.fetch_study, handle["run_id"], Path(work))
            result = await asyncio.to_thread(
                read_result, directory, run_id=handle["run_id"], uri=handle["uri"],
                manifest_uri=deck.staged["manifest_uri"], bound=deck.held["tradeoff"])
        await _publish_charts(result)
        return RunResult(result, {"solve": handle, "study": {"tool": "mou", **result.model_dump(mode="json")}})


class ResolvedCandidate(NativeResult):
    """An explicit MODFLOW solve of one candidate's decisions."""

    candidate: str
    tradeoff_run: str
    decisions: list[dict[str, Any]] = Field(default_factory=list)
    objectives: list[dict[str, Any]] = Field(default_factory=list)
    constraints: list[dict[str, Any]] = Field(default_factory=list)


def _selected(result: TradeoffResult, candidate: str) -> dict[str, Any]:
    """The one candidate the caller named; a set with none feasible, or an unknown id, refuses by name."""
    if result.outcome != "feasible" or not result.candidates:
        raise PestppError("Only a feasible trade-off result holds a candidate to apply.",
                          error_code="PESTPP_MOU_INFEASIBLE")
    for row in result.candidates:
        if str(row.get("id")).casefold() == str(candidate).casefold():
            return row
    raise PestppError(f"Candidate {candidate!r} is not in the trade-off result.",
                      error_code="PESTPP_MOU_CANDIDATE")


def resolve_candidate(case: Any, result: TradeoffResult, candidate: str) -> ResolvedCandidate:
    """Solve the pinned native deck with exactly one candidate's decisions, then read its OBS CSVs."""
    selected = _selected(result, candidate)
    with case.bound():
        return asyncio.run(_resolve_candidate(result, selected))


async def _resolve_candidate(result: TradeoffResult, selected: Mapping[str, Any]) -> ResolvedCandidate:
    from .resolve import solve_decisions

    decisions = []
    for row in result.bindings["decisions"]:
        name = str(row["name"]).casefold()
        if name not in selected["decisions"]:
            raise PestppError(f"The candidate {selected['id']} does not state decision {row['name']}.",
                              error_code="PESTPP_MOU_CANDIDATE")
        decisions.append({**row, "name": name, "value": selected["decisions"][name]})
    handle, samples = await solve_decisions(
        manifest_uri=result.manifest_uri, templates=result.bindings["templates"], decisions=decisions,
        readings=result.bindings["readings"],
        label=f"Trade-off candidate {selected['id']} of {result.run_id}")
    objectives = [{**row, "simulated": samples[str(row["name"]).casefold()]} for row in result.objectives]
    constraints = [_constraint(row, samples[str(row["name"]).casefold()]) for row in result.constraints]
    return ResolvedCandidate(run_id=handle["run_id"], uri=handle["uri"], candidate=str(selected["id"]),
                             tradeoff_run=result.run_id,
                             decisions=[{**row, "value": row["value"]} for row in decisions],
                             objectives=objectives, constraints=constraints)
