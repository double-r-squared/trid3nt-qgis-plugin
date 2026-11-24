"""Open a configured calibration model for individual PEST++ stages."""

from __future__ import annotations

import asyncio
import fcntl
import json
import re
import shutil
import tempfile
import time
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, ClassVar, Mapping

from trid3nt.errors import DeclarationError
from trid3nt.store.objects import sha256_of
from trid3nt.inputs.bound_domain import bind_domain, reset_domain
from trid3nt.inputs.coverage import strictness
from trid3nt.inputs.review import unattended
from trid3nt.render.emission import bind_turn_case, current_turn_case
from trid3nt.settings.reader import bind_run, reset_run
from trid3nt.workflows.pestpp.errors import PestppError
from trid3nt.workflows.pestpp.study import Study, StudyResult, calibrate, resolve, screen
from trid3nt.workflows.pestpp.workflow import PestppWorkflow

from .lock import Lock, _check, canonical
from .model import Model, _deck
from trid3nt.workflows.runtime.workflow import Deck, RunResult, Said

__all__ = ["Study", "StudyResult", "calibrate", "open_study", "resolve", "screen"]


@asynccontextmanager
async def open_study(model: Model) -> AsyncIterator[Study]:
    """Fill a calibrate model in its case and hold the study files until the caller leaves."""
    workflow = model._workflow
    if not isinstance(workflow, PestppWorkflow):
        raise PestppError("open_study requires a calibrate model.", error_code="PESTPP_MODEL_REQUIRED")
    previous_case = current_turn_case()
    bind_turn_case(model._case.name)
    try:
        with model._case.bound(), unattended(), strictness(model.strict):
            state, _deck, refusal = await model._filling(model._template_warning())
            if refusal is not None or not state.ready:
                code, sentence = refusal or state.refusal()
                raise PestppError(sentence, error_code=code)
            domain, settings = bind_domain(state.domain), bind_run(state.settings)
            try:
                async with workflow.study(state) as study:
                    yield study
            finally:
                reset_run(settings)
                reset_domain(domain)
    finally:
        bind_turn_case(previous_case)


@dataclass(frozen=True)
class StudyStale:
    """The version mismatch recorded at the apply line."""

    name: str
    base_version: str
    current_version: str
    code: ClassVar[str] = "STUDY_STALE"

    def __str__(self) -> str:
        return f"{self.code} {self.name} {self.base_version} {self.current_version}"


@dataclass(frozen=True)
class AppliedStudy:
    case: Path
    name: str
    template: str
    base_version: str
    values: Mapping[str, Any]
    resolved: Mapping[str, Any] | None = None


def _study_name(root: Path, name: str, *, new: bool) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
        raise DeclarationError("Use a single study name.", error_code="STUDY_NAME_INVALID")
    target = root / "studies" / name
    if new and target.exists():
        match = re.fullmatch(r"(.+)-(\d+)", name)
        stem, index = (match[1], int(match[2]) + 1) if match else (name, 2)
        while (target.parent / f"{stem}-{index}").exists():
            index += 1
        raise DeclarationError(f"Study {name} already exists. The next free name is {stem}-{index}.",
                               error_code="STUDY_EXISTS")
    return target


class NamedStudy:
    """Configure and run named stages, or read the pinned result of a finished study."""

    def __init__(self, case: Any, base: str | Model, *, name: str | None, choices: dict[str, Any]):
        self.case = case
        self._study = None
        self._work = None
        self._state = None
        self._model = None
        self._started = time.monotonic()
        self._said = Said()
        self.choices = dict(choices)
        if isinstance(base, str):
            if name is not None or choices:
                raise DeclarationError("A named study lookup takes only its name.", error_code="STUDY_NAME_INVALID")
            self.name, self.base = base, None
            self.path = _study_name(case.root, base, new=False)
            lock = Lock(case.root)
            files = lock.data.get("studies", {}).get(base)
            if not files or f"studies/{base}/study.json" not in files:
                raise DeclarationError("The case has no pinned study of that name.", error_code="STUDY_MISSING")
            for path, digest in files.items():
                if not lock.local(path).is_file() and lock.data.get("served"):
                    lock._served(path, digest)
                _check(lock.local(path), digest)
            self.document = json.loads((self.path / "study.json").read_text(encoding="utf-8"))
            return
        if base._case.root != case.root or not isinstance(name, str):
            raise DeclarationError("Name a study over a model of this case.", error_code="STUDY_MODEL_MISMATCH")
        self.name, self.base = name, base
        self.base_version = base.version
        self.path = _study_name(case.root, name, new=True)
        self.document: dict[str, Any] | None = None

    @property
    def applied(self) -> AppliedStudy:
        if self.document is None:
            raise DeclarationError("Complete the study before applying it.", error_code="STUDY_MISSING")
        return AppliedStudy(self.case.root, self.name, self.document["template"], self.document["base_version"],
                            self.document["values"], self.document.get("resolved"))

    def screen(self) -> str:
        return asyncio.run(self._stage("screen"))

    def calibrate(self) -> str:
        return asyncio.run(self._stage("calibrate"))

    def resolve(self) -> str:
        return asyncio.run(self._stage("resolve"))

    async def _stage(self, name: str) -> str:
        from .script_run import current, interruptible
        from trid3nt.workflows.solver.stop import SolveStopped, Stopped

        if self.base is None or self.document is not None:
            root = self.case.root.parents[1] if current() else self.case.root
            _study_name(root, self.name, new=True)
            raise DeclarationError("The study already finished.", error_code="STUDY_EXISTS")
        child = current()
        previous = current_turn_case()
        bind_turn_case(child.handoff["case"] if child else self.case.name)
        try:
            binding = nullcontext() if child else self.case.bound()
            with binding, unattended(), strictness(self.base.strict), (interruptible() if child else nullcontext()):
                if self._study is None:
                    self._study = await self._prepare(child)
                remaining = self._study.plan[len(self._study.stages):]
                if not remaining or remaining[0] != name:
                    raise PestppError("Call the study stages in the stated order.", error_code="PESTPP_STAGES_ORDER")
                token = bind_run(child.handoff.get("settings", {}) if child else {})
                try:
                    operation = {"screen": screen, "calibrate": calibrate, "resolve": resolve}[name]
                    stopped = None

                    async def stage():
                        nonlocal stopped
                        try:
                            return await operation(self._study)
                        except SolveStopped as stop:
                            async def outputs(handle, reason):
                                if name == "resolve":
                                    return await self._study.piece.published(handle, partial=reason)
                                return None

                            try:
                                await self._study.workflow.stopped(stop, self._study.run, outputs)
                            except Stopped as ended:
                                stopped = ended
                                return None

                    run_id, self._said = await self._study.workflow._within(
                        self._state, self._said, stage)
                    if stopped is not None:
                        self._collect(stopped.run)
                        await asyncio.to_thread(self._study.workflow._journal_stop, stopped,
                                                time.monotonic() - self._started,
                                                self._state.env.supplied, self._model._named())
                        raise stopped
                    if tuple(self._study.stages) == self._study.plan:
                        await self._finish()
                    return run_id
                finally:
                    reset_run(token)
        except BaseException:
            if self._work is not None:
                self._work.cleanup()
                self._work, self._study, self._said = None, None, Said()
            elif child and self._study is not None:
                shutil.rmtree(self._study.work, ignore_errors=True)
            raise
        finally:
            bind_turn_case(previous)

    async def _prepare(self, child):
        from .script_run import load_deck
        from .version import finished

        choices = dict(self.choices)
        if "observations" in choices:
            row = "gauges" if self.base._workflow.engine == "telemac" else "observe"
            if row in choices:
                raise DeclarationError(f"State {row} once.", error_code="STUDY_OBSERVATIONS_REPEATED")
            choices[row] = choices.pop("observations")
        keywords = choices.pop("keywords", {}) or {}
        model = self.case.calibrate(**choices)
        self._model = model
        for key, value in keywords.items():
            model[key] = value
        if child:
            deck = load_deck(child.handoff["deck"], model._workflow)
            row = child.handoff["row"]
            local = Lock(self.case.root).data["models"].get("calibrate")
            if local != row or model.version != row["model"] or _deck(
                    self.case.root, deck.staged["inputs"], served=True) != row["deck"]:
                model._refuse_reviewed()
            child.verify()
            self._state, self._said = deck.state, deck.said
            self._state.settings = child.handoff.get("settings", self._state.settings)
            self._started -= deck.seconds
            return deck.held["study"]
        base = self.base._base_run
        if not base:
            raise DeclarationError("The base version has no finished deck.", error_code="STUDY_BASE_UNFINISHED")
        model._stated["run"] = finished(Lock(self.case.root), base)
        state = await model._workflow.filled(model._wire(), steps=model._steps)
        if not state.ready:
            code, sentence = state.refusal()
            raise PestppError(sentence, error_code=code)
        self._work = tempfile.TemporaryDirectory(prefix="pestpp-study-")
        self._state = state
        study, self._said = await model._workflow._within(
            state, self._said, lambda: Study.prepare(model._workflow, state, Path(self._work.name)))
        return study

    async def _finish(self) -> None:
        study = self._study
        from .script_run import current

        child = current()
        self._keep()
        run, self._said = await study.workflow._within(self._state, self._said, study.result)
        self._collect(run)
        await study.workflow._publish(run, time.monotonic() - self._started,
                                      supplied=self._state.env.supplied, model=self._model._named())
        if self._work is not None:
            self._work.cleanup()
        elif child:
            shutil.rmtree(study.work, ignore_errors=True)

    def _collect(self, run: RunResult) -> None:
        from trid3nt.workflows.runtime.provenance import provenance_entries

        run.outputs.extend(self._said.outputs)
        run.notes[:0] = [*self._state.notes, *self._state.moved, *self._said.notes]
        run.settings = {**self._state.settings_read, **self._said.settings, **run.settings}
        run.choices.extend(self._said.choices)
        run.inputs.update(self._state.env.inputs)
        run.entries = provenance_entries(run.inputs, self._study.workflow.inputs)

    def _record(self):
        study = self._study
        record = {**study.run["study"], "residuals": study.read_off.get("residuals", []),
                  "stages": dict(study.stages), "selected_parameters": [row[0] for row in study.chosen]}
        if study.method == "ies" and study.read_off:
            from trid3nt.workflows.pestpp.outputs import read_parameter_ensemble

            record["prior_ensemble"] = read_parameter_ensemble(study.work / "ies-out", 0)
        if study.read_off:
            record["posterior"] = study.read_off["kept"]
        solved = study.run.get("solve") or {}
        if "study_values" in solved:
            return record, solved["study_values"]
        if not study.read_off:
            return record, {}
        with tempfile.TemporaryDirectory(dir=study.work, prefix="fitted-") as directory:
            _, values = study.piece.realize(Path(directory), study.work / study.method, study.deck,
                                            study.parameters, study.templates, study.read_off["best"])
        return record, values

    def _keep(self) -> None:
        from .script_run import current

        root = self.case.root.parents[1] if current() else self.case.root
        target = _study_name(root, self.name, new=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        record, values = self._record()
        document = {"name": self.name, "template": self.base._template.key, "base_version": self.base_version,
                    "choices": self.choices, "result": record, "values": values}
        with tempfile.TemporaryDirectory(dir=target.parent, prefix=".study-") as scratch:
            staged = Path(scratch) / self.name
            staged.mkdir()
            if self._study is not None:
                for outputs in sorted(self._study.work.glob("*-out")):
                    shutil.copytree(outputs, staged / outputs.name)
                resolved = (self._study.run.get("solve") or {}).get("staged")
                if resolved:
                    from trid3nt.workflows.pestpp.staging import fetch_deck

                    fetch_deck(resolved["inputs"], staged / "resolve")
                    document["resolved"] = {"case": resolved["case"], "outputs": resolved["outputs"],
                                            "result_basename": resolved["result_basename"],
                                            "frame": self._study.frame, "finished": self._study.finished.run_id,
                                            "files": {row["dest"]: sha256_of(staged / "resolve" / row["dest"])
                                                      for row in resolved["inputs"]}}
            (staged / "study.json").write_bytes(canonical(document))
            files = {(target / p.relative_to(staged)).relative_to(root).as_posix(): sha256_of(p)
                     for p in sorted(staged.rglob("*")) if p.is_file()}
            with (target.parent / ".publish").open("a") as guard:
                fcntl.flock(guard, fcntl.LOCK_EX)
                _study_name(root, self.name, new=True)
                lock = Lock(root)
                staged.rename(target)
                try:
                    if root != self.case.root:
                        pin_archived_base(Lock(self.case.root), lock, self.base_version)
                    lock.data.setdefault("studies", {})[self.name] = files
                    lock.save()
                except BaseException:
                    target.rename(staged)
                    raise
        self.document = document

def pin_archived_base(archive: Lock, lock: Lock, version: str) -> None:
    """Carry the archived finished base into the editable chat case."""
    relative = f"versions/{version}.lock"
    versions = lock.data.setdefault("versions", {})
    if relative in versions:
        return
    source = archive.local(relative)
    digest = archive.data["versions"][relative]
    _check(source, digest)
    target = lock.local(relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    versions[relative] = digest
    lock.data.setdefault("remote_files", {}).update(archive.data.get("remote_files", {}))
    lock.data.setdefault("served", archive.data["served"])
    for field in ("called_templates", "copied_templates"):
        pinned_templates = lock.data.setdefault(field, {})
        for key, value in archive.data.get(field, {}).items():
            pinned_templates.setdefault(key, value)


async def stage_applied(model: Model, state: Any, applied: AppliedStudy) -> Deck:
    """Stage the verified resolve inputs for the exact applied model version."""
    from trid3nt_contracts import new_ulid
    from trid3nt.workflows.solver.staging import discard_run, new_rundir, stage_run
    from .version import finished

    engine = state.workflow.engine
    if engine == "modflow":
        from trid3nt.workflows.modflow.authoring.staged_check import check_staged_run
    else:
        from trid3nt.workflows.telemac.authoring.staged_check import check_staged_run

    held = model._case.version(applied.base_version).model._base_run
    base = finished(Lock(model._case.root), held)
    metadata = applied.resolved
    run_id = new_ulid()
    directory = new_rundir(run_id, stem=engine)
    source = model._case.root / "studies" / applied.name / "resolve"
    try:
        for name, digest in metadata["files"].items():
            path = (source / name).resolve()
            if not path.is_relative_to(source):
                raise DeclarationError("A study deck path escapes its folder.", error_code="STUDY_DECK_INVALID")
            _check(path, digest)
            target = directory / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
        case = metadata["case"]
        staged = await stage_run(directory, run_id, module=case["module"], steering=case["steering"],
                                 results=case["results"], outputs=metadata["outputs"], mesh_inputs=[], prefix=engine,
                                 sheet={}, server_facts=case.get("server_facts") or {},
                                 result_basename=metadata["result_basename"], check=check_staged_run,
                                 cores=int(case.get("cores", 1)), case=case)
        state.env.run["mesh"] = base.mesh
        return Deck(state, run_id=run_id, prefix=engine, staged=staged,
                    held={"applied_study": base, "frame": metadata.get("frame", "")})
    except BaseException:
        discard_run(run_id, engine, stem=engine)
        raise


async def solve_applied(deck: Deck) -> RunResult:
    """Solve the reproduced deck using the same native dispatch as study resolve."""
    from trid3nt.workflows.pestpp.workflow import piece

    base = deck.held["applied_study"]
    engine = piece(base.engine)
    solved = await engine.solve_resolved(deck.staged, base, frame=deck.held["frame"])
    return RunResult(value=await engine.published(solved), results={"solve": solved, "mesh": base.mesh})


async def restage_native(deck: Deck, values: Mapping[str, float]) -> Deck:
    """Apply scalar fields that live only inside an engine-native table."""
    from trid3nt.workflows.modflow.authoring.staged_check import check_staged_run
    from trid3nt.workflows.pestpp.authoring.modflow import patch_columns
    from trid3nt.workflows.solver.staging import stage_run

    staged = deck.staged
    directory = Path(staged["rundir"])
    await asyncio.to_thread(patch_columns, directory, values)
    case = staged["case"]
    deck.staged = await stage_run(directory, deck.run_id, module=case["module"], steering=case["steering"],
                                 results=case["results"], outputs=staged["outputs"], mesh_inputs=[],
                                 prefix=deck.prefix, sheet=staged["sheet"], server_facts=case.get("server_facts") or {},
                                 result_basename=staged["result_basename"], check=check_staged_run, case=case)
    return deck
