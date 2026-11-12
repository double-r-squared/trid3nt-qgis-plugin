"""The PEST++ workflow: a study over a finished run, and the run its best parameter set makes.

``launch`` runs one order - the finished run's deck fetched, its engine's piece writing a template over each
named parameter and observing each target, the study reviewed, a sensitivity pass where it ranks more
parameters than are tuned, the ensemble study, then the deck solved once more with the base realization's
last parameters through the run's own engine and published as that engine publishes a run.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import shutil
import tempfile
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from trid3nt_contracts.common import Origin, SyntheticInput

from trid3nt_server.inputs.instant import event_time
from trid3nt_server.workflows.runtime import DeclarationError, RunResult, Workflow
from trid3nt_server.workflows.runtime.workflow import EVENT_TIME
from trid3nt_server.workflows.runtime.module import identify_on, keyword_floor
from trid3nt_server.workflows.runtime.sheet import Keyword

from .errors import PestppError
from .modules import PESTPP

if TYPE_CHECKING:
    from trid3nt_server.workflows.runtime.fill import Fill

logger = logging.getLogger("trid3nt_server.workflows.pestpp.workflow")

__all__ = ["ENGINES", "PestppWorkflow", "piece", "ranked"]

#: The engines a study calibrates, each by its piece ``authoring/<engine>.py``: the parameter writer, the
#: observation reader, the trial's command, and the calibrated run's solve and publish.
ENGINES = ("modflow",)


def piece(engine: str) -> ModuleType:
    """The study's piece for a run of ``engine``; an engine with none refuses naming the engines a study calibrates."""
    if engine not in ENGINES:
        raise PestppError(f"a study calibrates a run of {' or '.join(ENGINES)}, and this is a "
                          f"{engine or 'engineless'} run, for which no calibration is written",
                          error_code="PESTPP_ENGINE_UNCALIBRATED")
    return importlib.import_module(f"{__package__}.authoring.{engine}")


def ranked(sensitivity: Sequence[Mapping[str, Any]]) -> list[str]:
    """The parameters of a Morris summary, most sensitive first: by the mean absolute elementary effect."""
    return [row["parameter"] for row in sorted(sensitivity, key=lambda row: -float(row["sen_mean_abs"]))]


def _bounds(identifier: str, stated: Any) -> tuple[float, float, str, bool]:
    """A parameter's bounds, their source, and whether they are values in the run's own unit rather than
    multipliers of the run's own values, as stated; unstated or malformed refuses naming it."""
    if isinstance(stated, Mapping) and {"lower_value", "upper_value"} <= set(stated):
        lower, upper = float(stated["lower_value"]), float(stated["upper_value"])
        if not 0.0 < lower < upper:
            raise PestppError(f"{identifier} is bounded {lower:g} to {upper:g} in the run's own unit; a bound "
                              "below another, both above 0", error_code="PESTPP_BOUNDS_UNSTATED")
        return lower, upper, str(stated.get("source") or ""), True
    if isinstance(stated, Mapping) and {"lower", "upper"} <= set(stated):
        lower, upper, source = stated["lower"], stated["upper"], str(stated.get("source") or "")
    elif isinstance(stated, (list, tuple)) and len(stated) == 2:
        (lower, upper), source = stated, ""
    else:
        raise PestppError(
            f"{identifier} states no bounds: a multiplier on the run's own array is tuned between a lower and an "
            "upper factor, stated by the user, read from a citation, or bounded in the engine's dictionary, and "
            f"none states one for it. State {identifier}: {{\"lower\": L, \"upper\": U, \"source\": \"...\"}}, or "
            "the bounds in the run's own unit as lower_value and upper_value.",
            error_code="PESTPP_BOUNDS_UNSTATED")
    lower, upper = float(lower), float(upper)
    if not 0.0 < lower < 1.0 < upper:
        raise PestppError(
            f"{identifier} is bounded {lower:g} to {upper:g}; a multiplier starts at 1, the run's own array, so "
            "it is bounded below 1 and above it, and above 0", error_code="PESTPP_BOUNDS_UNSTATED")
    return lower, upper, source, False


class PestppWorkflow(Workflow):
    """PEST++ over a finished run of an engine with a piece: the template module, read by its own names.
    A template DECLARES INPUTS (the run, its parameters and its observations) and CAPTIONS; this builds every stage off them."""

    engine = "pestpp"
    solve_step = "solve"
    moment = EVENT_TIME.name

    def __init__(self, *, coerce: Sequence[Any] = (), **declared: Any) -> None:
        super().__init__(coerce=(*coerce, event_time()), **declared)

    def seated(self) -> tuple[Any, ...]:
        """The moment the wells' readings are asked at, then what the types of its rows state."""
        return (EVENT_TIME,) + super().seated()

    def input_schema(self) -> tuple[Any, str] | None:
        """The PEST++ dictionary: where this template's values read their schema off."""
        from .modules.module import module_input_dir

        return (module_input_dir(), PESTPP.MODULE)

    def input(self, name: str) -> Any:
        """The template's own inputs first, then the PEST++ option a name spells."""
        declared = next((row for row in self.inputs if row.name == name), None)
        if declared is not None:
            return declared
        body, identifier = identify_on([PESTPP], name)
        return Keyword(body, identifier, True)

    def check(self) -> None:
        """Refuse at import a template that names no run, no parameters or no wells, or no caption for its charts."""
        names = {row.name for row in self.inputs}
        missing = {"run", "parameters", "observe"} - names
        if missing:
            raise DeclarationError(f"{self.name} declares no {sorted(missing)}: a study reads a finished run, "
                                   "the parameters it tunes and the wells it is held to.")
        captions = dict(self._states("CAPTIONS", {}))
        unnamed = {"phi", "residuals", "parameters"} - set(captions)
        if unnamed:
            raise DeclarationError(f"{self.name} names no caption for {sorted(unnamed)}.")

    def judged(self, state: Fill) -> dict[str, Any]:
        """An unstated noptmax is PEST++'s 0, an ensemble evaluated and nothing calibrated, so it refuses."""
        from trid3nt_server.workflows.runtime.fill import REJECTED, Verdict

        refused = super().judged(state)
        own, _ = keyword_floor([PESTPP], dict(state.keywords))
        if own.get("NOPTMAX") in (None, 0):
            refused["noptmax"] = Verdict(
                REJECTED, own.get("NOPTMAX"), code="PESTPP_ITERATIONS_UNSTATED",
                reason="noptmax states how many iterations PEST++ runs, and unstated it is PEST++'s own 0: the "
                       "ensemble is evaluated and nothing is calibrated. State it, keywords={\"noptmax\": N}.")
        return refused

    def recorded_engine(self, run: RunResult) -> str | None:
        """The record is the calibrated run's: an ordinary run of the engine the study ran its model on."""
        return str((run.results.get("study") or {}).get("model_engine") or self.engine)

    async def launch(self, state: Fill) -> RunResult:
        """The run, in order: the deck, the study's files, its review, the ranking where one is asked for, the
        ensemble study, then the calibrated deck solved and published."""
        from trid3nt_server.inputs.run_notes import journal_note
        from trid3nt_server.render.pipeline_emitter import begin_substeps, current_emitter, substep
        from trid3nt_server.workflows.runtime.fill import call, production, read

        from . import engine, outputs, staging
        from .authoring.control import write_control

        env = production(state)
        run = env.run
        emitter = current_emitter()
        finished = await read(env, "run")
        study = piece(finished.engine)
        observations = await read(env, "observe")
        observations = observations if isinstance(observations, tuple) else (observations,)
        named = dict(await read(env, "parameters") or {})
        tune = await read(env, "tune")
        options, _ = keyword_floor([PESTPP], dict(state.keywords))
        frame = str(((finished.record.get("inputs") or {}).get("vertical_frame") or {}).get("value") or "")
        chosen, sources = [], {}
        for identifier, stated in named.items():
            lower, upper, sources[identifier.lower()], absolute = _bounds(identifier, stated)
            chosen.append((identifier, lower, upper, absolute))
        ranking = tune is not None and int(tune) < len(chosen)
        begin_substeps(emitter, 3 + ranking)
        work = Path(tempfile.mkdtemp(prefix="pestpp-study-"))

        async def stage(label: str, fn: Any, /, **kwargs: Any) -> Any:
            async with substep(emitter, label):
                run[label] = await call(fn, kwargs, label)
            return run[label]

        try:
            deck_dir = work / "deck"
            await asyncio.to_thread(staging.fetch_deck, finished.staged, deck_dir)
            the_deck = await asyncio.to_thread(study.deck, deck_dir)
            targets, left = study.targets(observations, the_deck=the_deck, finished=finished, frame=frame)
            for line in left:
                journal_note(f"a {study.SITE} is not held to the run: {line}")
            if not targets:
                raise PestppError(f"no {study.SITE}'s reading is a {study.OBSERVED} at a {study.PLACE} of the run, so "
                                  "the study has nothing to be held to", error_code="PESTPP_OBSERVATIONS_NONE")
            journal_note(f"every one of the {len(targets)} {study.SITE}s weighs 1 in phi, the sum of the squared "
                         "residuals")
            observed = [target for target, _where in targets]

            async def studied(tool: str, chosen: Sequence[tuple[str, float, float, bool]], stated: Mapping[str, Any]
                              ) -> tuple[dict[str, Any], Path, list, list]:
                where = work / tool
                parameters, templates, instructions = await asyncio.to_thread(
                    study.interface, deck_dir, where, chosen, the_deck=the_deck, targets=targets)
                write_control(where, name=staging.CONTROL_FILENAME, parameters=parameters, observations=observed,
                              templates=templates, instructions=instructions, command=study.COMMAND,
                              stated=stated, own=study.OWN)
                staged = await staging.stage_study(where, tool=tool, control=staging.CONTROL_FILENAME,
                                                   name=f"{self.name} of {finished.run_id}")
                handle = await stage("ranking" if tool == "sen" else "ensemble", engine.solve_case,
                                     run=staged, cores=int(staged["case"]["cores"]))
                files = await asyncio.to_thread(outputs.fetch_study, handle["run_id"], work / f"{tool}-out")
                return handle, files, parameters, templates

            if ranking:
                kept = await self._ranked(studied, chosen, options, int(tune))
                chosen = [row for row in chosen if row[0].lower() in kept]
            await self._reviewed(chosen, sources, observed, options)
            handle, files, parameters, templates = await studied("ies", chosen, options)
            phi = outputs.read_phi(files)
            ensemble = outputs.read_parameter_ensemble(files)
            residuals = outputs.read_residuals(files)
            best = ensemble.get("base")
            if best is None:
                raise PestppError("the study's last ensemble carries no base realization, whose parameters the "
                                  "calibrated run is solved with", error_code="PESTPP_OUTPUT_MISSING")
            solved = await stage("solve", study.calibrated, finished=finished, study_dir=work / "ies",
                                 the_deck=the_deck, parameters=parameters, templates=templates, best=best,
                                 frame=frame)
            run["study"] = {"run_id": handle["run_id"], "tool": "ies", "model_engine": finished.engine,
                            "finished_run": finished.run_id, "parameters": {
                                p.name: {"lower": p.lower, "upper": p.upper, "source": sources.get(p.name) or None,
                                         "calibrated": best[p.name]} for p in parameters},
                            "phi": phi, "ensemble": ensemble,
                            "observations": [{"name": o.name, "target": o.value} for o in observed]}
            value = await self._publish_calibrated(study, solved, phi=phi, residuals=residuals, ensemble=ensemble,
                                                   parameters=parameters, finished=finished)
            return RunResult(value=value, results=dict(run))
        finally:
            shutil.rmtree(work, ignore_errors=True)

    async def _ranked(self, studied: Any, chosen: Sequence[tuple[str, float, float, bool]],
                      options: Mapping[str, Any], tune: int) -> list[str]:
        """A Morris pass over every named parameter, ranked; the top ``tune`` confirmed on a card where a
        person reviews, taken as ranked otherwise and said."""
        from trid3nt_server.inputs.gate.input_review import gate_input_review
        from trid3nt_server.inputs.run_notes import journal_note

        from . import outputs

        sen_options = {k: v for k, v in options.items() if not k.startswith("IES_") and k != "NOPTMAX"}
        handle, files, _parameters, _templates = await studied("sen", chosen, sen_options)
        sensitivity = outputs.read_sensitivity(files)
        order = ranked(sensitivity)
        outcome = await gate_input_review(
            tool_name=self.name,
            entries=[SyntheticInput(param=row["parameter"], value=round(float(row["sen_mean_abs"]), 6),
                                    basis=Origin.DERIVED, real_source_if_any=f"pestpp-sen {handle['run_id']}",
                                    note=f"rank {order.index(row['parameter']) + 1} by mean absolute elementary effect")
                     for row in sensitivity],
            params={"tuned": order[:tune]})
        if not outcome.proceed:
            raise PestppError(f"the ranking was not confirmed: {outcome.cancel_reason or 'declined'}",
                              error_code="PESTPP_RANKING_DECLINED")
        kept = [str(name).lower() for name in outcome.params.get("tuned") or order[:tune]]
        journal_note(f"the sensitivity pass ranks {', '.join(order)}; the study tunes {', '.join(kept)}")
        return kept

    async def _reviewed(self, chosen: Sequence[tuple[str, float, float, bool]], sources: Mapping[str, str],
                        observed: list, options: Mapping[str, Any]) -> None:
        """The study before it runs, on the one review card where a person reviews; a decline ends the run."""
        from trid3nt_server.inputs.gate.input_review import gate_input_review

        outcome = await gate_input_review(
            tool_name=self.name,
            entries=[SyntheticInput(param=name, value=f"{lower:g} to {upper:g} " + (
                                        "in the run's own unit" if absolute else "times the run's own"),
                                    basis=Origin.USER, real_source_if_any=sources.get(name.lower()) or None)
                     for name, lower, upper, absolute in chosen] + [SyntheticInput(param="observations", value=len(observed),
                                                                         basis=Origin.PRODUCER)],
            params={PESTPP.MODULE_INPUT[k].keyword: v for k, v in options.items()})
        if not outcome.proceed:
            raise PestppError(f"the study was not run: {outcome.cancel_reason or 'declined'}",
                              error_code="PESTPP_STUDY_DECLINED")

    async def _publish_calibrated(self, study: ModuleType, solved: Mapping[str, Any], *, phi: list,
                                  residuals: list, ensemble: Mapping[str, Mapping[str, float]], parameters: list,
                                  finished: Any) -> Any:
        """The calibrated run published as its engine publishes a run, the study's four charts beside it."""
        from trid3nt_server.render.formats import Chart, Deliverable, publish

        from .charts import fit_chart, parameter_chart, phi_chart, residual_chart

        record = await study.published(solved)
        captions = dict(self._states("CAPTIONS", {}))
        captions = {**captions, **{key: f"{captions[key]} {study.OBSERVED}" for key in ("fit", "residuals")
                                   if key in captions}}
        items = [Deliverable(product=Chart(payload=phi_chart(phi, caption=captions["phi"])), caption=captions["phi"]),
                 Deliverable(product=Chart(payload=fit_chart(residuals, caption=captions["fit"])),
                             caption=captions["fit"]),
                 Deliverable(product=Chart(payload=residual_chart(residuals, caption=captions["residuals"])),
                             caption=captions["residuals"]),
                 Deliverable(product=Chart(payload=parameter_chart(ensemble, parameters,
                                                                   caption=captions["parameters"])),
                             caption=captions["parameters"])]
        await publish(run_id=str(solved["run_id"]), engine=self.engine, name=f"{finished.run_id} calibrated",
                      items=items)
        return record
