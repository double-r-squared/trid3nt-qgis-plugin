"""Callable stages of a PEST++ study over an existing model run."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from pydantic import BaseModel, Field
from trid3nt_contracts.common import SyntheticInput

from trid3nt.errors import accuracy_not_cited_warning, parameters_at_bounds_warning, unit_start_warning
from trid3nt.inputs.run_notes import journal_note
from trid3nt.inputs.slots import BED
from trid3nt.render.emission import begin_substeps, current_emitter, substep
from trid3nt.workflows.runtime import RunResult
from trid3nt.workflows.runtime.fill import Fill, call, production, read
from trid3nt.workflows.runtime.module import keyword_floor

from . import engine, outputs, staging, weights
from .authoring.control import Parameter, apply_parameter_data, at_bounds, complete_values, parameter_rows, write_control
from .errors import PestppError
from .modules import PESTPP
from .workflow import (
    LOCALIZED, METHODS, _ROWS, PestppWorkflow, _bounds, _calibration, _method, _priors, _sigmas, localized, piece,
    stage_order,
)


class StudyResult(BaseModel):
    """Successful screening or calibration, with the run id of every completed stage."""

    study_id: str
    stages: dict[str, str]
    ranking: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    synthetic_inputs: list[SyntheticInput] = Field(default_factory=list)

    @property
    def run_id(self) -> str:
        return self.study_id


@dataclass
class Study:
    workflow: PestppWorkflow
    run: dict[str, Any]
    work: Path
    finished: Any
    deck: Any
    targets: list
    observed: list
    chosen: list[tuple[str, float, float, bool]]
    sources: dict[str, str]
    fields: dict
    options: dict
    method: str
    frame: str
    tune: int | None
    balanced: bool
    offsets: list
    stated_method: bool
    rows: dict[str, dict[str, Any]]
    plan: tuple[str, ...]
    explicit_plan: bool
    stages: dict[str, str] = field(default_factory=dict, init=False)
    ranking: list[str] = field(default_factory=list, init=False)
    parameters: list[Parameter] = field(default_factory=list, init=False)
    templates: list[tuple[str, str]] = field(default_factory=list, init=False)
    read_off: dict = field(default_factory=dict, init=False)
    value: Any = field(default=None, init=False)
    authored_names: set[str] = field(default_factory=set, init=False)
    staged: dict[str, tuple] = field(default_factory=dict, init=False)
    reviewed: set[str] = field(default_factory=set, init=False)

    @property
    def piece(self):
        return piece(self.finished.engine)

    @classmethod
    async def prepare(cls, workflow: PestppWorkflow, state: Fill, work: Path) -> Study:
        env = production(state)
        run = env.run
        method = _method(await read(env, "method"))
        finished = await read(env, "run")
        study = piece(finished.engine)
        unread = sorted(name for name in _ROWS if name != study.ROW and env.supplied.get(name) is not None)
        if unread:
            raise PestppError(f"{', '.join(unread)} is stated, and a {finished.engine} run is held to {study.ROW}",
                              error_code="PESTPP_OBSERVATIONS_UNREAD")
        frame = str(((finished.record.get("inputs") or {}).get("vertical_frame") or {}).get("value") or "")
        if frame:
            # An elevation the study reads is read on the frame the finished run counted its bed from.
            env.run.setdefault(BED.states[0].name, frame)
        observations = await read(env, study.ROW)
        observations = observations if isinstance(observations, tuple) else (observations,)
        named = dict(await read(env, "parameters") or {})
        tune = await read(env, "tune")
        balanced = bool(await read(env, "balance"))
        if balanced and method != "ies":
            raise PestppError("balancing the groups' shares of phi is PEST++'s ies_phi_factor_file, which pestpp-ies "
                              "reads and pestpp-glm does not; balance an ies study, or weigh the groups by sigma",
                              error_code="PESTPP_BALANCE_UNSUPPORTED")
        options, _ = keyword_floor([PESTPP], dict(state.keywords))
        chosen, sources, spatial = [], {}, {}
        for identifier, stated in named.items():
            lower, upper, sources[identifier.lower()], absolute = _bounds(identifier, stated)
            chosen.append((identifier, lower, upper, absolute))
            if isinstance(stated, Mapping) and "pilot_points" in stated:
                spatial[identifier] = stated["pilot_points"] or {}
        available = env.run.keys() | env.data.keys()
        stages = await read(env, "stages") if "stages" in available else None
        plan = stage_order(stages, screen=tune is not None and int(tune) < len(chosen))
        rows = parameter_rows(await read(env, "parameter_data")) if "parameter_data" in available else {}
        begin_substeps(current_emitter(), 1 + len(plan))
        deck_dir = work / "deck"
        await asyncio.to_thread(staging.fetch_deck, finished.staged, deck_dir)
        the_deck = await asyncio.to_thread(study.deck, deck_dir, finished)
        fields = study.fields(spatial, the_deck)
        targets, left = study.targets(observations, the_deck=the_deck, finished=finished, frame=frame)
        for line in left:
            journal_note(f"a {study.SITE} is not held to the run: {line}")
        if not targets:
            raise PestppError(f"no {study.SITE}'s reading is a {study.OBSERVED} at a {study.PLACE} of the run, so "
                              "the study has nothing to be held to", error_code="PESTPP_OBSERVATIONS_NONE")
        targets = weights.restated(targets, await read(env, "sigma"))
        observed = [target for target, _where in targets]
        uncited = sum(target.group == weights.NOT_CITED for target in observed)
        if uncited:
            journal_note(accuracy_not_cited_warning(count=uncited))
        if started := weights.unit_started(observed):
            journal_note(unit_start_warning(count=started))
        offsets, said = study.supplied(the_deck)
        for line in said:
            journal_note(line)
        if held := sum(target.datum is not None for target in observed):
            journal_note(f"{held} {study.SITE} zeros cite their accuracy, so each is a datum parameter of its own "
                         "whose prior sd that accuracy is, and leaves the target's sigma")
        journal_note(f"each {study.SITE} weighs one over its sigma in phi: " + "; ".join(
            f"{group} {len(held)} at sigma {min(held):.3g} to {max(held):.3g} in the run's unit"
            for group, held in _sigmas(observed).items()))

        return cls(workflow, run, work, finished, the_deck, targets, observed, chosen,
                   sources, fields, options, method, frame, tune, balanced, offsets,
                   env.supplied.get("method") is not None, rows, plan, stages is not None)

    async def stage(self, label: str, fn: Any, /, **kwargs: Any) -> Any:
        async with substep(current_emitter(), label):
            self.run[label] = await call(fn, kwargs, label)
        return self.run[label]

    async def author(self, tool: str, chosen: Sequence[tuple[str, float, float, bool]], stated: Mapping[str, Any]
                     ) -> tuple[dict, list[Parameter], list[tuple[str, str]]]:
        where = self.work / tool
        selected = {name for name, *_bounds in chosen}
        parameters, templates, instructions = await asyncio.to_thread(
            self.piece.interface, self.work / "deck", where, chosen, the_deck=self.deck, targets=self.targets,
            spatial={k: v for k, v in self.fields.items() if k in selected})
        names = {p.name for p in parameters}
        unknown = self.rows.keys() - names - self.authored_names
        if unknown:
            raise PestppError(f"Unknown PARNME in parameter_data: {', '.join(sorted(unknown))}.",
                              error_code="PESTPP_PARAMETER_DATA_INVALID")
        self.authored_names.update(names)
        rows = {name: row for name, row in self.rows.items() if name in names}
        parameters = apply_parameter_data(where, parameters, templates, stated, rows)
        if rows and self.finished.engine == "telemac":
            await asyncio.to_thread(self.piece.outflow_levels, where, the_deck=self.deck, parameters=parameters)
        write_control(where, name=staging.CONTROL_FILENAME, parameters=parameters, observations=self.observed,
                      templates=templates, instructions=instructions, command=self.piece.command(self.deck),
                      stated={k: v for k, v in stated.items() if k not in {"PARTRANS", "PARCHGLIM"}},
                      own=self.piece.OWN, balanced=self.balanced and tool == "ies")
        staged = await staging.stage_study(where, tool=tool, control=staging.CONTROL_FILENAME,
                                           name=f"{self.workflow.name} of {self.finished.run_id}",
                                           model=self.finished.engine)
        return staged, parameters, templates

    async def studied(self, tool: str, chosen: Sequence[tuple[str, float, float, bool]], stated: Mapping[str, Any]
                      ) -> tuple[dict, Path, list[Parameter], list[tuple[str, str]]]:
        authored = self.staged.pop(tool, None)
        staged, parameters, templates = authored if authored is not None else await self.author(tool, chosen, stated)
        handle = await self.stage(METHODS.get(tool, "ranking"), engine.solve_case,
                                  run=staged, cores=int(staged["case"]["cores"]))
        files = await asyncio.to_thread(outputs.fetch_study, handle["run_id"], self.work / f"{tool}-out")
        return handle, files, parameters, templates

    def require_new(self, name: str) -> None:
        if name in self.stages:
            raise PestppError(f"{name} already completed on this study.", error_code="PESTPP_STAGE_REPEATED")

    def charts(self) -> dict:
        return {"phi": self.read_off["phi"], "residuals": self.read_off["residuals"],
                "ensemble": self.read_off["spread"], "parameters": self.parameters}

    async def result(self) -> RunResult:
        """Publish the stage result and chart artifacts through the workflow's usual result seam."""
        if not self.stages:
            raise PestppError("Complete a study stage first.", error_code="PESTPP_STUDY_EMPTY")
        if self.value is not None:
            return RunResult(value=self.value, results=dict(self.run))
        study_id = self.stages.get("calibrate") or self.stages["screen"]
        if self.read_off:
            await self.workflow.publish_charts(self.piece, study_id, self.finished, **self.charts())
        return RunResult(value=StudyResult(study_id=study_id, stages=dict(self.stages), ranking=self.ranking),
                         results=dict(self.run))


async def review_screen(study: Study) -> None:
    if "screen" not in study.reviewed and (study.explicit_plan or study.rows):
        options = {k: v for k, v in study.options.items() if k.startswith("GSA_") or k.startswith("MORRIS_")}
        await study.workflow._reviewed(study.chosen, study.sources, study.observed, options, "sen", stated=False,
                                       fields=study.fields, offsets=study.offsets,
                                       stages=study.plan if study.explicit_plan else (), parameter_data=study.rows)
        study.reviewed.add("screen")


async def review_fit(study: Study) -> tuple[dict[str, Any], bool]:
    chosen = study.chosen
    options, localizing = localized(study.options, study.method, {k: v for k, v in study.fields.items()
                                                                  if k in {row[0] for row in chosen}})
    if "calibrate" not in study.reviewed:
        await study.workflow._reviewed(chosen, study.sources, study.observed, options, study.method,
                                       stated=study.stated_method, fields=study.fields, localizing=localizing,
                                       offsets=study.offsets, stages=study.plan if study.explicit_plan else (),
                                       parameter_data=study.rows)
        study.reviewed.add("calibrate")
    return options, localizing


def screen_options(options: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in options.items() if not k.startswith("IES_") and k != "NOPTMAX"}


async def screen(study: Study) -> str:
    """Run a Morris pass. An explicit tune may limit the parameters of a subsequent calibration."""
    study.require_new("screen")
    if str(study.options.get("GSA_METHOD", "morris")).lower() != "morris":
        raise PestppError("The screen stage reads the PEST++ Morris summary. State gsa_method morris.",
                          error_code="PESTPP_SCREEN_METHOD")
    chosen = study.chosen
    filtering = study.tune is not None and study.tune < len(chosen) and "calibrate" not in study.stages
    tune = int(study.tune) if filtering else len(chosen)
    groups = {}
    await review_screen(study)

    async def studied(tool, candidates, stated):
        result = await study.studied(tool, candidates, stated)
        groups.update({p.name: p.spatial_group or p.original_name or p.name for p in result[2]})
        return result

    handle, order, kept = await study.workflow._ranked(
        studied, chosen, study.options, tune, stages=study.plan if study.explicit_plan else (),
        parameter_data=study.rows, filtering=filtering)
    if filtering:
        selected = {groups.get(name, name) for name in kept}
        study.chosen = [row for row in chosen if row[0].lower() in selected]
    study.ranking = order
    study.stages["screen"] = handle["run_id"]
    record = study.run.setdefault("study", {"run_id": handle["run_id"], "tool": "sen",
                                            "model_engine": study.finished.engine,
                                            "finished_run": study.finished.run_id})
    if study.explicit_plan or "calibrate" not in study.plan:
        record.update(stages=dict(study.stages), ranking=order)
    return handle["run_id"]


async def calibrate(study: Study) -> str:
    """Fit the authored parameters with PEST++ IES or GLM."""
    study.require_new("calibrate")
    if study.options.get("NOPTMAX") in (None, 0):
        raise PestppError("State noptmax to run the calibrate stage.", error_code="PESTPP_ITERATIONS_UNSTATED")
    chosen, method, observed = study.chosen, study.method, study.observed
    fields, finished, sources, balanced = study.fields, study.finished, study.sources, study.balanced
    options, localizing = await review_fit(study)
    handle, files, parameters, templates = await study.studied(method, chosen, options)
    read_off = await asyncio.to_thread(_calibration, method, files)
    read_off["spread"] = {name: complete_values(parameters, values) for name, values in read_off["spread"].items()}
    best = read_off["best"] = complete_values(parameters, read_off["best"])
    stuck = at_bounds(parameters, best)
    if stuck:
        journal_note(parameters_at_bounds_warning(groups=stuck))
    widening = weights.widened(observed, read_off["residuals"])
    journal_note(f"the widening after the fit (the discrepancy rule) scales each group's sigma so its phi is "
                 f"its count, the calibrated parameters unchanged: phi {widening['phi']:.4g} over "
                 f"{widening['observations']} observations before, {widening['widened_phi']:.4g} after; "
                 + "; ".join(f"{group} x{held['factor']:.3g}" for group, held in widening["groups"].items()))
    run = study.run
    run["study"] = {"run_id": handle["run_id"], "tool": method, "model_engine": finished.engine,
                    "finished_run": finished.run_id, "parameters": {
                        p.name: {"lower": p.lower, "upper": p.upper,
                                 "source": sources.get(p.group) or sources.get(p.name) or None,
                                 "calibrated": best[p.name], "written": best[p.name] * p.scale + p.offset,
                                 **({"site": list(p.site)} if p.site else {})}
                        for p in parameters},
                    "prior": _priors(parameters, options, fields, files, method),
                    "phi": read_off["phi"], **read_off["kept"], "balanced": balanced, "at_bounds": stuck,
                    **({"localization": {"ies_autoadaloc": options[LOCALIZED[0]],
                                         "source": LOCALIZED[2] if localizing else "stated by the user"}}
                       if LOCALIZED[0] in options else {}),
                    "widening": {k: v for k, v in widening.items() if k != "sigma"},
                    "observations": [{"name": o.name, "target": o.value, "group": o.group, "sigma": o.sigma,
                                      "weight": o.weight, "basis": o.basis,
                                      "parts": [{"member": m, "sigma": s, "cite": c} for m, s, c in o.parts],
                                      **({"datum": {"sd": o.datum[0], "cite": o.datum[1]}} if o.datum else {}),
                                      "widened_sigma": widening["sigma"].get(o.name)} for o in observed]}
    study.parameters, study.templates, study.read_off = parameters, templates, read_off
    study.stages["calibrate"] = handle["run_id"]
    if study.explicit_plan:
        run["study"]["stages"] = dict(study.stages)
        if study.ranking:
            run["study"]["ranking"] = study.ranking
    return handle["run_id"]


async def resolve(study: Study) -> str:
    """Solve and publish the original model with the fitted values."""
    study.require_new("resolve")
    if "calibrate" not in study.stages:
        raise PestppError("resolve requires a completed calibrate stage.", error_code="PESTPP_STAGES_ORDER")
    solved = await study.stage("solve", study.piece.calibrated, finished=study.finished,
                               study_dir=study.work / study.method, the_deck=study.deck, parameters=study.parameters,
                               templates=study.templates, best=study.read_off["best"], frame=study.frame)
    study.value = await study.workflow._publish_calibrated(study.piece, solved, finished=study.finished,
                                                            **study.charts())
    study.stages["resolve"] = solved["run_id"]
    if study.explicit_plan:
        study.run["study"]["stages"] = dict(study.stages)
    return solved["run_id"]
