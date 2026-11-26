"""A reviewed sequential DA study over a pinned MODFLOW 6 deck: forecast/analysis by cycle."""

from __future__ import annotations

import asyncio
import csv
import json
import math
import statistics
import tempfile
from pathlib import Path
from typing import Any, Mapping

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

_PHI = "pestpp.global.phi.actual.csv"
_DISPATCH = "trid3nt.workflows.pestpp.engine.solve_case"
#: The completion marker the worker reads out of the manager's record.
_RECORD = "pestpp.rec"


class AssimilateResult(NativeResult):
    """Sequential DA outcome: each cycle's forecast/analysis ensembles, phi trace and state transfer."""

    manifest_uri: str
    cycles: list[dict[str, Any]] = Field(default_factory=list)
    observations: list[dict[str, Any]] = Field(default_factory=list)
    parameters: list[dict[str, Any]] = Field(default_factory=list)
    state: list[dict[str, Any]] = Field(default_factory=list)
    phi_trace: list[dict[str, Any]] = Field(default_factory=list)
    bindings: dict[str, Any]


def _missing(name: str) -> PestppError:
    return PestppError(f"{name} is not among the study's files, so its sequential result cannot be read.",
                       error_code="PESTPP_DA_OUTPUT_MISSING")


def _number(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise PestppError(f"The DA output holds no numeric value for {name}.",
                          error_code="PESTPP_DA_NONFINITE") from error
    if not math.isfinite(number) or number == 3.0e30:
        raise PestppError(f"The DA output holds a dropped or non-finite value for {name}.",
                          error_code="PESTPP_DA_NONFINITE")
    return number


def _rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise _missing(path.name)
    with path.open(newline="", encoding="utf-8") as handle:
        return [{str(key).casefold(): value for key, value in row.items()} for row in csv.DictReader(handle)]


def _ensemble(path: Path) -> dict[str, dict[str, float]]:
    rows = _rows(path)
    if not rows:
        raise PestppError(f"{path.name} holds no members.", error_code="PESTPP_DA_MEMBER_DROPPED")
    first = next(iter(rows[0]))
    out: dict[str, dict[str, float]] = {}
    for row in rows:
        member = str(row.pop(first, "")).strip()
        if not member:
            raise PestppError(f"{path.name} holds a row without a member name.",
                              error_code="PESTPP_DA_MEMBER_DROPPED")
        if member in out:
            raise PestppError(f"{path.name} repeats member {member}.", error_code="PESTPP_DA_MEMBER_DROPPED")
        out[member] = {name: _number(value, name) for name, value in row.items() if value not in (None, "")}
    return out


def _summary(values: Mapping[str, float]) -> dict[str, Any]:
    held = list(values.values())
    if not held:
        return {"mean": 0.0, "spread": 0.0, "min": 0.0, "max": 0.0, "values": dict(values)}
    return {"mean": statistics.fmean(held), "spread": statistics.pstdev(held),
            "min": min(held), "max": max(held), "values": dict(values)}


def _at(values: Mapping[Any, Any], cycle: int, default: Any) -> Any:
    for key in (cycle, str(cycle)):
        if key in values:
            return values[key]
    return default


def _phi(directory: Path) -> dict[int, list[dict[str, Any]]]:
    found: dict[int, list[dict[str, Any]]] = {}
    for row in _rows(directory / _PHI):
        try:
            cycle, iteration = int(float(row["cycle"])), int(float(row["iteration"]))
        except (KeyError, TypeError, ValueError) as error:
            raise PestppError(f"{_PHI} does not state its cycles and iterations.",
                              error_code="PESTPP_DA_OUTPUT_MISSING") from error
        found.setdefault(cycle, []).append({
            "cycle": cycle, "iteration": iteration, "mean": _number(row.get("mean"), "phi mean"),
            "standard_deviation": _number(row.get("standard_deviation"), "phi spread"),
            "min": _number(row.get("min"), "phi min"), "max": _number(row.get("max"), "phi max")})
    for rows in found.values():
        rows.sort(key=lambda item: item["iteration"])
    return found


def _forecast_only(bound: Mapping[str, Any], cycle: int) -> bool:
    weights = []
    for row in bound.get("observations") or ():
        weights.append(float(_at(row.get("weights") or {}, cycle, row.get("weight", 0.0))))
    return not any(weight > 0 for weight in weights)


def _transfer(bound: Mapping[str, Any], cycles: list[int],
              analyses: Mapping[int, Mapping[str, Mapping[str, float]]],
              initial_parameters: Mapping[int, Mapping[str, Mapping[str, float]]]) -> list[dict[str, Any]]:
    out = []
    for pair in bound.get("state") or ():
        observation, parameter = str(pair["observation"]), str(pair["parameter"])
        for rank, cycle in enumerate(cycles):
            analysis = {name: values[observation.casefold()]
                        for name, values in analyses.get(cycle, {}).items()
                        if observation.casefold() in values}
            nxt = cycles[rank + 1] if rank + 1 < len(cycles) else None
            initial = {name: values[parameter.casefold()]
                       for name, values in initial_parameters.get(nxt, {}).items()
                       if parameter.casefold() in values} if nxt is not None else {}
            row = {"parameter": parameter, "observation": observation, "cycle": cycle,
                   "analysis": statistics.fmean(analysis.values()) if analysis else None,
                   "next_initial": statistics.fmean(initial.values()) if initial else None}
            out.append(row)
    return out


def read_result(directory: Path, *, run_id: str, manifest_uri: str, bound: dict[str, Any],
                uri: str = "") -> AssimilateResult:
    """Read each cycle's forecast (iteration 0) and analysis (last iteration) ensembles and the phi trace."""
    phi = _phi(directory)
    cycles = [int(cycle) for cycle in bound.get("cycles") or ()]
    if not cycles:
        cycles = sorted(phi)
    summaries, parameters, observations = [], [], []
    analyses: dict[int, dict[str, dict[str, float]]] = {}
    forecasts: dict[int, dict[str, dict[str, float]]] = {}
    initial_parameters: dict[int, dict[str, dict[str, float]]] = {}
    for cycle in cycles:
        trace = phi.get(cycle)
        if not trace:
            raise PestppError(f"{_PHI} holds no row for cycle {cycle}.", error_code="PESTPP_DA_OUTPUT_MISSING")
        forecast_iteration, analysis_iteration = trace[0]["iteration"], trace[-1]["iteration"]
        only = _forecast_only(bound, cycle)
        forecast_obs = _ensemble(directory / f"pestpp.{cycle}.0.obs.csv")
        forecast_par = _ensemble(directory / f"pestpp.{cycle}.0.par.csv")
        if only or analysis_iteration == 0:
            analysis_obs, analysis_par = forecast_obs, forecast_par
        else:
            analysis_obs = _ensemble(directory / f"pestpp.{cycle}.{analysis_iteration}.obs.csv")
            analysis_par = _ensemble(directory / f"pestpp.{cycle}.{analysis_iteration}.par.csv")
        missing = set(forecast_obs) - set(analysis_obs)
        if missing:
            member = sorted(missing)[0]
            raise PestppError(f"Cycle {cycle} drops member {member} between its forecast and analysis.",
                              error_code="PESTPP_DA_MEMBER_DROPPED")
        missing_par = set(forecast_par) - set(analysis_par)
        if missing_par:
            member = sorted(missing_par)[0]
            raise PestppError(f"Cycle {cycle} drops member {member} from its parameter ensemble.",
                              error_code="PESTPP_DA_MEMBER_DROPPED")
        forecasts[cycle] = forecast_obs
        initial_parameters[cycle] = forecast_par
        analyses[cycle] = analysis_obs
        summaries.append({"cycle": cycle, "forecast_iteration": forecast_iteration,
                          "analysis_iteration": analysis_iteration, "forecast_only": only,
                          "members": sorted(forecast_obs),
                          "forecast_phi": trace[0]["mean"], "analysis_phi": trace[-1]["mean"],
                          "forecast_spread": trace[0]["standard_deviation"],
                          "analysis_spread": trace[-1]["standard_deviation"]})
        if only:
            continue
        forecast_names = {name for values in forecast_obs.values() for name in values}
        analysis_names = {name for values in analysis_obs.values() for name in values}
        for row in bound.get("observations") or ():
            name = str(row["name"]).casefold()
            weight = float(_at(row.get("weights") or {}, cycle, row.get("weight", 0.0)))
            if name not in forecast_names and name not in analysis_names:
                if weight > 0:
                    raise PestppError(f"Observation {row['name']} is active in cycle {cycle} and the ensemble "
                                      "does not state it.", error_code="PESTPP_DA_OUTPUT_MISSING")
                continue
            forecast = {member: values[name] for member, values in forecast_obs.items() if name in values}
            analysis = {member: values[name] for member, values in analysis_obs.items() if name in values}
            if weight > 0 and (not forecast or not analysis):
                raise PestppError(f"Observation {row['name']} is active in cycle {cycle} and its simulated "
                                  "ensemble is incomplete.", error_code="PESTPP_DA_MEMBER_DROPPED")
            observations.append({"name": row["name"], "cycle": cycle, "group": row.get("group", ""),
                                 "observed": float(_at(row.get("values") or {}, cycle, row.get("value", 0.0))),
                                 "weight": weight, "forecast": _summary(forecast),
                                 "analysis": _summary(analysis)})
        for row in bound.get("parameters") or ():
            name = str(row["name"]).casefold()
            forecast = {member: values[name] for member, values in forecast_par.items() if name in values}
            analysis = {member: values[name] for member, values in analysis_par.items() if name in values}
            if forecast or analysis:
                parameters.append({"name": row["name"], "cycle": cycle, "transform": row.get("transform", ""),
                                   "forecast": _summary(forecast), "analysis": _summary(analysis)})
    trace = [row for cycle in cycles for row in phi.get(cycle, ())]
    return AssimilateResult(run_id=run_id, uri=uri, manifest_uri=manifest_uri, cycles=summaries,
                            observations=observations, parameters=parameters,
                            state=_transfer(bound, cycles, analyses, initial_parameters),
                            phi_trace=trace, bindings=bound)


async def _publish_charts(result: AssimilateResult) -> None:
    from trid3nt.render.charts import build_chart_payload
    from trid3nt.render.formats import Chart, Deliverable, publish

    items = []
    if result.phi_trace:
        points = []
        for row in result.cycles:
            points.append({"cycle": row["cycle"], "phase": "forecast", "phi": row["forecast_phi"]})
            if not row["forecast_only"]:
                points.append({"cycle": row["cycle"], "phase": "analysis", "phi": row["analysis_phi"]})
        payload = build_chart_payload(
            title="Cycle objective", vega_lite_spec={
                "mark": {"type": "line", "point": True}, "data": {"values": points},
                "encoding": {"x": {"field": "cycle", "type": "ordinal", "title": "Cycle"},
                             "y": {"field": "phi", "type": "quantitative", "title": "phi"},
                             "color": {"field": "phase", "type": "nominal", "title": "Phase"}}})
        items.append(Deliverable(product=Chart(payload=payload), caption="Cycle objective"))
    if result.observations:
        points = []
        for row in result.observations:
            if row["weight"] <= 0:
                continue
            points += [
                {"observation": row["name"], "cycle": row["cycle"], "trace": "observed", "value": row["observed"]},
                {"observation": row["name"], "cycle": row["cycle"], "trace": "forecast",
                 "value": row["forecast"]["mean"]},
                {"observation": row["name"], "cycle": row["cycle"], "trace": "analysis",
                 "value": row["analysis"]["mean"]},
            ]
        if points:
            payload = build_chart_payload(
                title="Observation innovation", vega_lite_spec={
                    "data": {"values": points}, "facet": {"field": "observation", "type": "nominal",
                                                          "columns": 1},
                    "spec": {"mark": {"type": "line", "point": True},
                             "encoding": {"x": {"field": "cycle", "type": "ordinal", "title": "Cycle"},
                                          "y": {"field": "value", "type": "quantitative", "title": "Value"},
                                          "color": {"field": "trace", "type": "nominal", "title": "Trace"}},
                             "resolve": {"scale": {"y": "independent"}}}})
            items.append(Deliverable(product=Chart(payload=payload), caption="Observation innovation"))
    if items:
        await publish(run_id=result.run_id, engine="pestpp", name="Assimilation", items=items)


class AssimilateWorkflow(BareWorkflow):
    """Bind the caller's cycle windows, parameters, observations and state to a reviewed DA study."""

    engine = "pestpp"

    async def prepare(self, state) -> Deck:
        from . import staging
        from .authoring.assimilate import author_assimilate

        held = production(state).run
        if not held.get("files"):
            raise PestppError("State the native simulation and its PEST++ template and instruction files.",
                              error_code="PESTPP_DA_FILES")
        with tempfile.TemporaryDirectory(prefix="pestpp-da-") as work:
            directory = Path(work)
            await asyncio.to_thread(native_assets, directory, native_files(held["files"]))
            bound = await asyncio.to_thread(
                author_assimilate, directory, model=held["model"], cycles=held["cycles"],
                parameters=held["parameters"], observations=held["observations"],
                options=held.get("options") or {}, command=held.get("command") or "mf6")
            staged = await staging.stage_study(
                directory, tool="da", control="pestpp.pst", name=self.name, model="modflow",
                required=(_PHI,))
        deck = Deck(state, run_id=staged["run_id"], prefix=self.engine, staged=staged,
                    held={"assimilate": bound})
        try:
            rows = [
                ParamSheetRow(name=name, value=value if isinstance(value, (str, int)) else json.dumps(value),
                              origin="user", editable=False)
                for name, value in (("model", held["model"]), ("cycles", bound["windows"]),
                                    ("parameters", bound["parameters"]),
                                    ("observations", bound["observations"]),
                                    ("state", bound["state"]))
            ]
            review = ParamSheet(workflow=self.name, title="Review the sequential DA study", rows=rows)
            answer = await gate_input_review(tool_name=self.name, entries=_entries(rows),
                                             params={}, param_sheet=review)
            if not answer.proceed:
                raise DeclarationError(answer.cancel_reason or "The sequential DA study was declined.",
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
                # Only a finished multi-cycle result certifies every cycle's analysis.
                return None

            await self.stopped(stop, {}, partial)
        with tempfile.TemporaryDirectory(prefix="pestpp-da-result-") as work:
            directory = await asyncio.to_thread(outputs.fetch_study, handle["run_id"], Path(work))
            result = await asyncio.to_thread(
                read_result, directory, run_id=handle["run_id"], uri=handle["uri"],
                manifest_uri=deck.staged["manifest_uri"], bound=deck.held["assimilate"])
        await _publish_charts(result)
        return RunResult(result, {"solve": handle, "study": {"tool": "da", **result.model_dump(mode="json")}})
