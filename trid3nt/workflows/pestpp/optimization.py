"""A reviewed deterministic MODFLOW management study."""

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

_DISPATCH = "trid3nt.workflows.pestpp.engine.solve_case"
_REQUIRED = ("pestpp.par", "pestpp.slp.iobj.csv", "pestpp.res")


class OptimizeResult(NativeResult):
    """OPT outcome and decisions, with observations evaluated by the native model."""

    outcome: Literal["feasible", "infeasible"]
    manifest_uri: str
    decisions: list[dict[str, Any]] = Field(default_factory=list)
    objective: dict[str, Any]
    constraints: list[dict[str, Any]] = Field(default_factory=list)
    objective_trace: list[dict[str, int | float]] = Field(default_factory=list)
    bindings: dict[str, Any]


def _number(value: float, name: str) -> float:
    if not math.isfinite(value) or value == 3.0e30:
        raise PestppError(f"OPT output {name} has no simulated value.", error_code="PESTPP_OPT_OUTPUT")
    return value


def _constraint(row: Mapping[str, Any], simulated: float) -> dict[str, Any]:
    limit = row["limit"]
    return {**row, "simulated": simulated,
            "slack": limit - simulated if row["sense"] == "<=" else simulated - limit}


def read_result(directory: Path, *, run_id: str, manifest_uri: str, bound: dict[str, Any],
                outcome: str, uri: str = "") -> OptimizeResult:
    """Read the incumbent, objective iterations and final true-model constraint values."""
    from . import outputs

    try:
        with (directory / _REQUIRED[1]).open(newline="") as handle:
            trace = [{"iteration": int(row["iteration"]),
                      "objective_function": _number(float(row["objective_function"]), "objective trace")}
                     for row in csv.DictReader(handle)]
        decisions, constraints = [], []
        objective = dict(bound["objective"])
        if outcome != "infeasible":
            best = {name.casefold(): value for name, value in outputs.read_parameters(directory).items()}
            values = {row["name"].casefold(): _number(row["modelled"], row["name"])
                      for row in outputs.read_residual_table(directory / _REQUIRED[2])}
            for row in bound["decisions"]:
                name = row["name"].casefold()
                value = _number(best[name], name)
                if not row["lower"] <= value <= row["upper"]:
                    raise PestppError(f"The OPT decision {name} is outside its authored bounds.",
                                      error_code="PESTPP_OPT_OUTPUT")
                decisions.append({**row, "value": value})
            objective["simulated"] = values[objective["name"].casefold()]
            constraints = [_constraint(row, values[row["name"].casefold()]) for row in bound["constraints"]]
            outcome = "feasible" if all(row["slack"] >= 0 for row in constraints) else "infeasible"
    except (OSError, KeyError, ValueError) as error:
        raise PestppError(f"Cannot read the OPT result: {error}.", error_code="PESTPP_OPT_OUTPUT") from error
    return OptimizeResult(run_id=run_id, uri=uri, outcome=outcome, manifest_uri=manifest_uri,
                          decisions=decisions, objective=objective, constraints=constraints,
                          objective_trace=trace, bindings=bound)


async def _publish_charts(result: OptimizeResult) -> None:
    from trid3nt.render.charts import build_chart_payload
    from trid3nt.render.formats import Chart, Deliverable, publish

    items = []
    if result.objective_trace:
        caption = "Objective by iteration"
        payload = build_chart_payload(
            title=caption, vega_lite_spec={
                "mark": {"type": "line", "point": True}, "data": {"values": result.objective_trace},
                "encoding": {"x": {"field": "iteration", "type": "quantitative"},
                             "y": {"field": "objective_function", "type": "quantitative",
                                   "title": result.objective.get("units")}}})
        items.append(Deliverable(product=Chart(payload=payload), caption="Objective"))
    if result.constraints:
        payload = build_chart_payload(
            title="Signed constraint slack", vega_lite_spec={
                "data": {"values": result.constraints}, "facet": {"field": "units", "type": "nominal"},
                "spec": {"mark": "bar", "encoding": {
                    "y": {"field": "name", "type": "nominal", "title": "Constraint"},
                    "x": {"field": "slack", "type": "quantitative"},
                    "tooltip": [{"field": key, "type": "quantitative"} for key in ("simulated", "limit", "slack")]}},
                "resolve": {"scale": {"x": "independent"}}})
        items.append(Deliverable(product=Chart(payload=payload), caption="Constraint slack"))
    await publish(run_id=result.run_id, engine="pestpp", name="Optimization", items=items)


class OptimizeWorkflow(BareWorkflow):
    """Bind caller-stated decisions and native observations to a reviewed study."""

    engine = "pestpp"

    async def prepare(self, state) -> Deck:
        from ..modflow.authoring.validate import validate_authored_simulation
        from . import staging
        from .authoring.optimize import author_optimize

        held = production(state).run
        with tempfile.TemporaryDirectory(prefix="pestpp-opt-") as work:
            directory = Path(work)
            await asyncio.to_thread(native_assets, directory, native_files(held["files"]))
            _, bound = await asyncio.to_thread(
                author_optimize, directory, model=held["model"], iterations=held["iterations"],
                decisions=held["decisions"], objective=held["objective"], constraints=held["constraints"])
            with tempfile.TemporaryDirectory(prefix="pestpp-opt-validation-") as validation:
                await asyncio.to_thread(shutil.copytree, directory, validation, dirs_exist_ok=True)
                await asyncio.to_thread(validate_authored_simulation, Path(validation))
            staged = await staging.stage_study(directory, tool="opt", control=staging.CONTROL_FILENAME,
                                               name=self.name, model="modflow", required=_REQUIRED)
        deck = Deck(state, run_id=staged["run_id"], prefix=self.engine, staged=staged, held={"optimize": bound})
        try:
            rows = [
                ParamSheetRow(name=name, value=value if isinstance(value, (str, int)) else json.dumps(value),
                              origin="user", editable=False)
                for name, value in (("model", held["model"]), ("iterations", held["iterations"]),
                                    ("decisions", bound["decisions"]), ("objective", bound["objective"]),
                                    ("constraints", bound["constraints"]))
            ]
            review = ParamSheet(workflow=self.name, title="Review the optimization study", rows=rows)
            answer = await gate_input_review(tool_name=self.name, entries=_entries(rows),
                                             params={}, param_sheet=review)
            if not answer.proceed:
                raise DeclarationError(answer.cancel_reason or "The optimization study was declined.",
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
                # Only a finished OPT result certifies the last incumbent and its margins.
                return None

            await self.stopped(stop, {}, partial)
        with tempfile.TemporaryDirectory(prefix="pestpp-opt-result-") as work:
            directory = await asyncio.to_thread(outputs.fetch_study, handle["run_id"], Path(work))
            result = await asyncio.to_thread(
                read_result, directory, run_id=handle["run_id"], uri=handle["uri"],
                manifest_uri=deck.staged["manifest_uri"], bound=deck.held["optimize"],
                outcome=handle["metrics"].get("opt_status", "feasible"))
        await _publish_charts(result)
        return RunResult(result, {"solve": handle, "study": {"tool": "opt", **result.model_dump(mode="json")}})


class ResolvedOptimization(NativeResult):
    """An explicit MODFLOW solve with the chosen decisions applied."""

    optimization_run: str
    objective: dict[str, Any]
    constraints: list[dict[str, Any]]


def resolve_optimum(case: Any, result: OptimizeResult) -> ResolvedOptimization:
    """Solve the pinned native deck with the feasible OPT rates, then read its OBS CSVs."""
    if result.outcome != "feasible":
        raise PestppError("Only a feasible OPT result can be applied.", error_code="PESTPP_OPT_INFEASIBLE")
    with case.bound():
        return asyncio.run(_resolve_optimum(result))


async def _resolve_optimum(result: OptimizeResult) -> ResolvedOptimization:
    from trid3nt_contracts import new_ulid

    from trid3nt.store import objects
    from trid3nt.tools.cache import read_object_bytes_s3
    from trid3nt.workflows.modflow import engine as modflow
    from trid3nt.workflows.modflow.authoring.validate import validate_authored_simulation

    from ..solver.staging import new_rundir, stage_run
    from . import staging
    from .authoring.control import Parameter, substitute

    manifest = json.loads(await asyncio.to_thread(read_object_bytes_s3, result.manifest_uri))
    run_id = new_ulid()
    directory = new_rundir(run_id, stem="modflow")
    try:
        await asyncio.to_thread(staging.fetch_deck, manifest["inputs"], directory)
        templates = result.bindings["templates"]
        values = {row["name"].casefold(): row["value"] for row in result.decisions}
        parameters = [Parameter(row["name"].casefold(), row["start"], row["lower"], row["upper"],
                                "well_rates", transform="none") for row in result.decisions]
        await asyncio.to_thread(substitute, directory, templates, parameters, values)
        readings = result.bindings["readings"]
        observed_csvs = sorted({reading[0] for reading in readings.values()})

        def check(work: Path, **_: Any) -> None:
            with tempfile.TemporaryDirectory(prefix="pestpp-opt-validation-") as validation:
                shutil.copytree(work, validation, dirs_exist_ok=True)
                validate_authored_simulation(Path(validation))

        staged = await stage_run(
            directory, run_id, module="gwf", steering="mfsim.nam", results=["mfsim.lst"],
            outputs=[*observed_csvs, "modflow_metrics.json"], mesh_inputs=[],
            prefix="modflow", sheet={}, server_facts={"name": f"OPT decision {result.run_id}"},
            result_basename="mfsim.lst", check=check)
        handle = await solve_staged(staged, dispatch=modflow.solve_case)
        samples = {}
        for file in observed_csvs:
            uri = f"s3://{objects.runs_bucket()}/{run_id}/{file}"
            text = (await asyncio.to_thread(read_object_bytes_s3, uri)).decode("utf-8")
            rows = [{name.casefold(): value for name, value in row.items()}
                    for row in csv.DictReader(text.splitlines())]
            for name, (filename, ordinal) in readings.items():
                if filename != file:
                    continue
                try:
                    samples[name] = _number(float(rows[ordinal - 1][name]), name)
                except (IndexError, KeyError, ValueError) as error:
                    raise PestppError(f"The native solve did not write {name} at output row {ordinal}.",
                                      error_code="PESTPP_OPT_OUTPUT") from error
    except BaseException:
        await asyncio.to_thread(discard_run, run_id, "modflow", stem="modflow")
        raise
    objective = {**result.objective, "simulated": samples[result.objective["name"].casefold()]}
    constraints = [_constraint(row, samples[row["name"].casefold()]) for row in result.constraints]
    return ResolvedOptimization(run_id=run_id, uri=handle["uri"], optimization_run=result.run_id,
                                objective=objective, constraints=constraints)
