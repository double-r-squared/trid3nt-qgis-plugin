"""The PEST++ calibration template, its review cards and result publication."""

from __future__ import annotations

import importlib
import logging
import math
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from trid3nt_contracts.common import Origin, SyntheticInput

from trid3nt.inputs.instant import event_time
from trid3nt.workflows.runtime import DeclarationError, RunResult, Workflow
from trid3nt.workflows.runtime.module import identify_on, keyword_floor
from trid3nt.workflows.runtime.sheet import Keyword
from trid3nt.workflows.runtime.workflow import EVENT_TIME, Deck

from .errors import PestppError
from .modules import PESTPP

if TYPE_CHECKING:
    from trid3nt.workflows.runtime.fill import Fill

logger = logging.getLogger("trid3nt.workflows.pestpp.workflow")

__all__ = ["ENGINES", "LOCALIZED", "METHODS", "PestppWorkflow", "localized", "piece", "ranked"]

#: The engines a study calibrates, each by its piece ``authoring/<engine>.py``: the parameter writer, the
#: observation reader, the trial's command, and the calibrated run's solve and publish.
ENGINES = ("modflow", "telemac")

#: The PEST++ tools a calibration runs, the study's ``method``, each by the stage it is shown as: an ensemble
#: smoother, or Gauss-Levenberg-Marquardt regression of one parameter set over a finite-difference Jacobian.
METHODS = {"ies": "ensemble", "glm": "regression"}

#: The study's observation rows; each piece reads one, and a stated row its piece does not read refuses.
_ROWS = ("observe", "gauges")

#: An ensemble study over pilot points localizes, unstated, by PEST++'s own automatic adaptive localization: many
#: correlated points against few observations is the case its spurious correlations are filtered for.
LOCALIZED = ("IES_AUTOADALOC", True,
             "PEST++ 5.2.27 users manual 9.1.11 Localization: automatic adaptive localization (Luo et al. 2018), "
             "activated by ies_autoadaloc")


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


def localized(options: Mapping[str, Any], method: str, fields: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    """The options with an ies study over pilot points localized by PEST++'s automatic adaptive localization where the
    run does not state ies_autoadaloc -> the options and whether the study turned it on."""
    identifier, value, _source = LOCALIZED
    if method != "ies" or not fields or identifier in options:
        return dict(options), False
    return {**options, identifier: value}, True


def _method(stated: Any) -> str:
    """The study's method as stated, ies unstated; one PEST++ calibrates by no other way refuses naming them."""
    method = "ies" if stated in (None, "") else str(stated).strip().lower()
    if method not in METHODS:
        raise PestppError(f"a study calibrates by {' or '.join(METHODS)}, and {stated!r} is neither",
                          error_code="PESTPP_METHOD_UNKNOWN")
    return method


def _calibration(method: str, files: Path) -> dict[str, Any]:
    """What a finished calibration is read for: phi per iteration, the residuals and the calibrated parameters
    (the base realization's last, an ensemble's; the best set, glm's), and the values each parameter took - the
    last ensemble, or glm's set at each iteration."""
    from . import outputs

    phi = outputs.read_phi(files, tool=method)
    residuals = outputs.read_residuals(files, tool=method)
    if method == "glm":
        best, iterations = outputs.read_parameters(files), outputs.read_parameter_iterations(files)
        return {"phi": phi, "residuals": residuals, "best": best,
                "spread": {**{f"iteration {i}": values for i, values in iterations.items()}, "base": best},
                "kept": {"iterations": iterations}}
    ensemble = outputs.read_parameter_ensemble(files)
    best = ensemble.get("base")
    if best is None:
        raise PestppError("the study's last ensemble carries no base realization, whose parameters the "
                          "calibrated run is solved with", error_code="PESTPP_OUTPUT_MISSING")
    return {"phi": phi, "residuals": residuals, "best": best, "spread": ensemble, "kept": {"ensemble": ensemble}}


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


def _priors(parameters: Sequence[Any], options: Mapping[str, Any], fields: Mapping[str, Mapping[str, Any]],
            files: Path, method: str) -> dict[str, dict[str, Any]]:
    """Each parameter group's prior as stated to PEST++, its pilot points' field where it has one, and for an ensemble
    study the mean and spread of log10 across the prior ensemble PEST++ drew and the last one."""
    import statistics

    from . import outputs
    from .authoring.control import at_bounds, complete_values, prior

    held = prior(parameters, {k: v for k, v in options.items() if k != "PARTRANS"})
    for identifier, field in fields.items():
        if identifier.lower() in held:
            held[identifier.lower()]["pilot_points"] = {"count": held[identifier.lower()]["parameters"], **field}
    if method != "ies":
        return held
    for label, iteration in (("prior_ensemble", 0), ("posterior_ensemble", None)):
        ensemble = {name: complete_values(parameters, values) for name, values in
                    outputs.read_parameter_ensemble(files, iteration).items()}
        for group, stated in held.items():
            members = [p for p in parameters if p.group == group]
            rows = [[math.log10(real[p.name]) if p.transform == "log" else real[p.name] + p.offset / p.scale
                     for real in ensemble.values()] for p in members]
            stuck = sum(c["lower"] + c["upper"] for real in ensemble.values()
                        for c in at_bounds(members, real).values())
            stated[label] = {"mean": statistics.fmean(v for row in rows for v in row),
                             "sd": statistics.fmean(statistics.pstdev(row) for row in rows), "of": stated["sd_of"],
                             "at_bounds": stuck, "values": len(members) * len(ensemble)}
    return held


def _sigmas(observed: Sequence[Any]) -> dict[str, list[float]]:
    """Each observation group's sigmas, in the order the groups first appear."""
    held: dict[str, list[float]] = {}
    for target in observed:
        held.setdefault(target.group, []).append(target.sigma)
    return held


_STAGES = ("screen", "calibrate", "resolve")


def stage_order(stated: Any, *, screen: bool) -> tuple[str, ...]:
    """A stated sequence is exact. An unstated sequence applies the legacy tune condition."""
    if stated is None:
        return _STAGES if screen else _STAGES[1:]
    if (not isinstance(stated, (list, tuple)) or not stated
            or any(not isinstance(name, str) or name not in _STAGES for name in stated)
            or len(set(stated)) != len(stated)):
        raise PestppError("stages takes a nonempty sequence of distinct screen, calibrate and resolve names.",
                          error_code="PESTPP_STAGES_INVALID")
    plan = tuple(stated)
    if "resolve" in plan and ("calibrate" not in plan or plan.index("resolve") < plan.index("calibrate")):
        raise PestppError("resolve requires calibrate earlier in stages.", error_code="PESTPP_STAGES_ORDER")
    return plan


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
        from trid3nt.workflows.runtime.fill import REJECTED, Verdict

        refused = super().judged(state)
        stated = getattr(state, "inputs", {}).get("method")
        try:
            method = _method(getattr(stated, "value", None))
        except PestppError as unknown:
            refused["method"] = Verdict(REJECTED, stated.value, code=unknown.error_code, reason=str(unknown))
            method = "ies"
        declared = getattr(state, "inputs", {}).get("stages")
        try:
            stages = stage_order(getattr(declared, "value", None), screen=False)
        except PestppError as invalid:
            refused["stages"] = Verdict(REJECTED, declared.value, code=invalid.error_code, reason=str(invalid))
            return refused
        own, _ = keyword_floor([PESTPP], dict(state.keywords))
        if "calibrate" in stages and own.get("NOPTMAX") in (None, 0):
            evaluated = "ensemble is" if method == "ies" else "starting parameters are"
            refused["noptmax"] = Verdict(
                REJECTED, own.get("NOPTMAX"), code="PESTPP_ITERATIONS_UNSTATED",
                reason=f"noptmax states how many iterations PEST++ runs, and unstated it is PEST++'s own 0: the "
                       f"{evaluated} evaluated and nothing is calibrated. State it, keywords={{\"noptmax\": N}}.")
        return refused

    def recorded_engine(self, run: RunResult) -> str | None:
        """The record is the calibrated run's: an ordinary run of the engine the study ran its model on."""
        from .study import StudyResult

        if isinstance(run.value, StudyResult):
            return self.engine
        return str((run.results.get("study") or {}).get("model_engine") or self.engine)

    @asynccontextmanager
    async def study(self, state: Fill):
        """Hold the reviewed study's working files for the selected stages."""
        from .study import Study

        with tempfile.TemporaryDirectory(prefix="pestpp-study-") as directory:
            yield await Study.prepare(self, state, Path(directory))

    async def prepare(self, state: Fill) -> Deck:
        """Stage the first selected PEST++ tool and collect the reviews possible before solving."""
        import shutil

        from .study import Study, review_fit, review_screen, screen_options

        work = Path(tempfile.mkdtemp(prefix="pestpp-study-"))
        try:
            study = await Study.prepare(self, state, work)
            if study.plan[0] == "screen":
                await review_screen(study)
                tool, options = "sen", screen_options(study.options)
            else:
                options, _ = await review_fit(study)
                tool = study.method
            authored = await study.author(tool, study.chosen, options)
            study.staged[tool] = authored
            staged, _, _ = authored
            return Deck(state, run_id=staged["run_id"], prefix=self.engine, staged=staged, held={"study": study})
        except BaseException:
            shutil.rmtree(work, ignore_errors=True)
            raise

    async def solve(self, deck: Deck) -> RunResult:
        """Execute the selected stages from the prepared study."""
        import shutil

        from .study import calibrate, resolve, screen

        study = deck.held["study"]
        operation = {"screen": screen, "calibrate": calibrate, "resolve": resolve}
        try:
            for name in study.plan:
                await operation[name](study)
            return await study.result()
        finally:
            shutil.rmtree(study.work, ignore_errors=True)

    async def _ranked(self, studied: Any, chosen: Sequence[tuple[str, float, float, bool]],
                      options: Mapping[str, Any], tune: int, *, stages: Sequence[str] = (),
                      parameter_data: Mapping[str, Mapping[str, Any]] = {}, filtering: bool = True
                      ) -> tuple[dict[str, Any], list[str], list[str]]:
        """A Morris pass over every named parameter, ranked; the top ``tune`` confirmed on a card where a
        person reviews, taken as ranked otherwise and said."""
        from trid3nt.sdk.script_run import study_review
        from trid3nt.inputs.run_notes import journal_note

        from . import outputs
        from .study import screen_options

        sen_options = screen_options(options)
        handle, files, _parameters, _templates = await studied("sen", chosen, sen_options)
        sensitivity = outputs.read_sensitivity(files)
        order = ranked(sensitivity)
        entries = [SyntheticInput(param=row["parameter"], value=round(float(row["sen_mean_abs"]), 6),
                                  basis=Origin.DERIVED, real_source_if_any=f"pestpp-sen {handle['run_id']}",
                                  note=f"rank {order.index(row['parameter']) + 1} by mean absolute elementary effect")
                   for row in sensitivity]
        if stages:
            entries.append(SyntheticInput(param="stages", value=", ".join(stages), basis=Origin.USER))
            entries.extend(SyntheticInput(param=f"{name} {column.upper()}", value=value, basis=Origin.USER)
                           for name, row in parameter_data.items() for column, value in row.items())
        outcome = await study_review(tool_name=self.name, entries=entries,
                                          params={"tuned": order[:tune]} if filtering else {})
        if not outcome.proceed:
            raise PestppError(f"the ranking was not confirmed: {outcome.cancel_reason or 'declined'}",
                              error_code="PESTPP_RANKING_DECLINED")
        kept = [str(name).lower() for name in outcome.params.get("tuned") or order[:tune]]
        selection = f"; the study tunes {', '.join(kept)}" if filtering else ""
        journal_note(f"the sensitivity pass ranks {', '.join(order)}{selection}")
        return handle, order, kept

    async def _reviewed(self, chosen: Sequence[tuple[str, float, float, bool]], sources: Mapping[str, str],
                        observed: list, options: Mapping[str, Any], method: str, *, stated: bool,
                        fields: Mapping[str, Mapping[str, Any]] = {}, localizing: bool = False,
                        offsets: Sequence[tuple[str, float, str]] = (), stages: Sequence[str] = (),
                        parameter_data: Mapping[str, Mapping[str, Any]] = {}) -> None:
        """The study before it runs, on the one review card where a person reviews; a decline ends the run."""
        from trid3nt.sdk.script_run import study_review

        from .authoring.control import sigma_range
        from .weights import USER

        bounds = [SyntheticInput(param=name, value=f"{lower:g} to {upper:g} " + (
                                     "in the run's own unit" if absolute else "times the run's own"),
                                 basis=Origin.USER, real_source_if_any=sources.get(name.lower()) or None)
                  for name, lower, upper, absolute in chosen]
        span = sigma_range(options)
        bounds += [SyntheticInput(param=f"{name} prior", value=(
                       f"mean 1 times the run's own, sd {(math.log10(upper) - math.log10(lower)) / span:.3g} in log10"
                       if not absolute else f"sd {(upper - lower) / span:.3g} in the run's own unit")
                       + f": the bounds as {span:g} standard deviations (PEST++ par_sigma_range)",
                       basis=Origin.USER if "PAR_SIGMA_RANGE" in options else Origin.PRODUCER)
                   for name, lower, upper, absolute in chosen]
        bounds += [SyntheticInput(param=f"{name} pilot points", value=(
                       f"every {field['spacing']:g} m; {field['variogram']} variogram, range {field['a']:g} m, "
                       f"contribution {field['contribution']:g}"
                       + "".join(f", {key} {field[key]:g}" for key in field["structure_stated"] if key != "contribution")
                       + (f"; maxpts_interp {field['maxpts_interp']}, remove_negative_factors {field['remove_negative_factors']}"
                          if field.get("kriging_stated") else "")),
                       basis=Origin.USER if field["stated"] else Origin.TEMPLATE,
                       real_source_if_any=f"spacing: {field['spacing_source']}; range: {field['a_source']}")
                   for name, field in fields.items() if name in {row[0] for row in chosen}]
        bounds += [SyntheticInput(param=f"datum {o.name}", value=f"sd {o.datum[0]:.3g} in the run's unit about the "
                                  "zero the gauge states: a parameter, outside sigma", basis=Origin.PRODUCER,
                                  real_source_if_any=o.datum[1]) for o in observed if o.datum is not None]
        bounds += [SyntheticInput(param=name, value=f"offset, mean 0 and sd {sd:.3g} in the run's unit: a parameter, "
                                  "bounded 2 sd either side", basis=Origin.PRODUCER, real_source_if_any=cite)
                   for name, sd, cite in offsets]
        if localizing:
            bounds.append(SyntheticInput(param="localization", value="automatic adaptive (ies_autoadaloc true): the "
                                         "study's own over pilot points; state ies_autoadaloc false to turn it off",
                                         basis=Origin.TEMPLATE, real_source_if_any=LOCALIZED[2]))
        sigmas = [SyntheticInput(param=f"sigma {group}", value=f"{min(held):.3g} to {max(held):.3g} in the run's unit",
                                 basis=Origin.USER if any(o.basis == USER for o in observed if o.group == group)
                                 else Origin.PRODUCER) for group, held in _sigmas(observed).items()]
        bounds += [SyntheticInput(param="stages", value=", ".join(stages), basis=Origin.USER)] if stages else []
        bounds += [SyntheticInput(param=f"{name} {column.upper()}", value=value, basis=Origin.USER)
                   for name, row in parameter_data.items() for column, value in row.items()]
        outcome = await study_review(
            tool_name=self.name,
            entries=[*bounds, SyntheticInput(param="observations", value=len(observed), basis=Origin.PRODUCER),
                     *sigmas, SyntheticInput(param="method", value=f"pestpp-{method}",
                                             basis=Origin.USER if stated else Origin.TEMPLATE)],
            params={PESTPP.MODULE_INPUT[k].keyword: v for k, v in options.items()})
        if not outcome.proceed:
            raise PestppError(f"the study was not run: {outcome.cancel_reason or 'declined'}",
                              error_code="PESTPP_STUDY_DECLINED")

    async def _publish_calibrated(self, study: ModuleType, solved: Mapping[str, Any], *, phi: list,
                                  residuals: list, ensemble: Mapping[str, Mapping[str, float]], parameters: list,
                                  finished: Any) -> Any:
        """The calibrated run published as its engine publishes a run, the study's four charts beside it."""
        record = await study.published(solved)
        await self.publish_charts(study, str(solved["run_id"]), finished, phi=phi, residuals=residuals,
                                  ensemble=ensemble, parameters=parameters)
        return record

    async def publish_charts(self, study: ModuleType, run_id: str, finished: Any, *, phi: list,
                             residuals: list, ensemble: Mapping[str, Mapping[str, float]], parameters: list) -> None:
        from trid3nt.render.formats import Chart, Deliverable, publish

        from .charts import fit_chart, parameter_chart, phi_chart, residual_chart

        captions = dict(self._states("CAPTIONS", {}))
        captions = {**captions, **{key: f"{captions[key]} {study.OBSERVED}" for key in ("fit", "residuals")
                                   if key in captions}}
        items = [Deliverable(product=Chart(payload=phi_chart(phi, caption=captions["phi"])), caption=captions["phi"]),
                 Deliverable(product=Chart(payload=fit_chart(residuals, caption=captions["fit"])),
                             caption=captions["fit"]),
                 Deliverable(product=Chart(payload=residual_chart(residuals, caption=captions["residuals"])),
                             caption=captions["residuals"]),
                 Deliverable(product=Chart(payload=parameter_chart(ensemble, [p for p in parameters
                                                                              if p.transform == "log"],
                                                                   caption=captions["parameters"])),
                             caption=captions["parameters"])]
        await publish(run_id=run_id, engine=self.engine, name=f"{finished.run_id} calibrated", items=items)
