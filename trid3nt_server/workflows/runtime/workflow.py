"""The workflow SKELETON: what every declared workflow runs on.

A template file declares a workflow; :class:`Workflow` IS one, and owns the
fill-then-launch spine, post and publish, and the registration factory.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Collection, Mapping, Sequence

from trid3nt_contracts.common import Origin, SyntheticInput
from trid3nt_contracts.coverage import SourceChoice

from trid3nt_server.inputs.bound_domain import bind_domain, reset_domain
from trid3nt_server.inputs.input import Input, declared
from trid3nt_server.inputs.run_notes import (
    bind_coverage, bind_notes, drain_coverage, drain_notes)
from trid3nt_server.inputs.slots import Slot
from trid3nt_server.inputs.user_input import WireArgsError

from . import journal
from .errors import DeclarativeError, said
from .resolution import SensitivityDecl, sensitivity_notes
from .provenance import merge_provenance, provenance_entries

if TYPE_CHECKING:
    from .fill import Fill

__all__ = ["RunResult", "Workflow", "register_workflow"]

logger = logging.getLogger("trid3nt_server.workflows.runtime.workflow")


@dataclass
class RunResult:
    """What a launch produced: the published result, what each stage returned by
    name, and what the run collected on the way - its provenance rows, the notes
    it could not produce, the layers it published and the sources it matched."""

    value: Any
    results: dict[str, Any] = field(default_factory=dict)
    entries: list[SyntheticInput] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: Every input's verdict by name, as the fill and the launch stamped them.
    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: list[dict[str, Any]] = field(default_factory=list)
    choices: list[SourceChoice] = field(default_factory=list)


class Workflow:
    """The universal skeleton. A template declares; this runs.
    A workflow declares its engine's facts - the solver family, its solve step's name
    and the input holding its moment; hooks have SILENT defaults and no subtype restates one."""

    engine: str = ""

    #: The SOLVE step's name; the skeleton reads the run prefix off it when the result carries none.
    solve_step: str = ""

    #: The name of the input holding the moment a run opens at; a runtime that dates nothing names none.
    moment: str = ""

    def seated(self) -> tuple[Input, ...]:
        """The inputs a declaration of this workflow carries without restating
        them: what the type of each of its rows states beside itself. An engine
        adds its own; the skeleton declares none."""
        return tuple(stated for slot in dict.fromkeys(
            row.slot for row in self.inputs if row.slot is not None)
            for stated in slot.states)

    def __init__(self, *, metadata: Any, inputs: Any, template: Any,
                 sensitivity: Sequence[tuple[str, str]] | SensitivityDecl = (),
                 coerce: Sequence[Callable[[dict], Mapping[str, Any]]] = ()) -> None:
        self.metadata = metadata
        self.name = metadata.name
        #: The template MODULE: STEERING, OUTPUTS, CAPTIONS and its files are read off its own names.
        self.template = template
        #: THE ONE BODY: the template's inputs, then each one a row's type or the engine declares; a template's own wins.
        #: Values lead the wire and rows follow, each in body order.
        self.inputs = declared(inputs)
        stated = {row.name for row in self.inputs}
        self.inputs = tuple(sorted(
            self.inputs + tuple(row for row in self.seated()
                                if row.name not in stated),
            key=lambda row: row.is_row))
        #: What the template calls every published variable and every row it reads into; one dict.
        self.captions = dict(getattr(template, "CAPTIONS", {}) or {})
        #: Published reads in a resolution-sensitive class; see ``resolution.py``.
        self.sensitivity = (sensitivity if isinstance(sensitivity, SensitivityDecl)
                            else SensitivityDecl(sensitivity))
        self.coercions = tuple(coerce)
        self.error_prefix = str(getattr(metadata, "engine", "") or "workflow").upper()
        self.check()

    def input(self, name: str) -> Any:
        """THE ONE LOOKUP: the input a name states - the template's own, then one the
        engine or a row's type declares; an engine answers for its keywords after
        them, and a name nothing declares refuses."""
        found = next((row for row in self.inputs if row.name == name), None)
        if found is None:
            raise WireArgsError(f"{name!r} is not an input of {self.name}.")
        return found

    def check(self) -> None:
        """Refuse at import what this template's declarations cannot run; the
        skeleton runs nothing of its own, so it refuses nothing."""

    async def launch(self, state: Fill) -> RunResult:
        """Write, solve and read a READY fill -> what the run produced.

        The skeleton knows no engine, so it launches nothing."""
        raise WireArgsError(f"{self.name} declares no engine to launch.")

    def judged(self, state: Fill) -> dict[str, Any]:
        """What refuses the whole run before any stage of it, as a verdict under the name it is said by.
        An engine's run waits on a person, so one with no session to wait on refuses here; an engine adds its own."""
        from trid3nt_server.inputs.gate.input_review import no_session
        from .fill import REJECTED, Verdict

        mode = state.carried.get("input_mode")
        sentence = no_session(self.name, mode) if self.engine else None
        return {} if sentence is None else {"input_mode": Verdict(REJECTED, mode, reason=sentence, code="NO_SESSION")}

    def unnamed(self) -> tuple[str, ...]:
        """Every input the template names as a plain string that nothing of
        this run is called; the fill refuses each by name."""
        return ()

    def named_rows(self, held: Collection[str] = ()) -> tuple[str, ...]:
        """Every declared row the fill produces, given the names the run already holds; the skeleton names none."""
        return ()

    def sheet_doc(self) -> str | None:
        """The ENGINE SURFACE line of this template's docstring, or nothing.

        Only the workflow knows which engine surface it fills, so a skeleton
        that fills none claims none."""
        return None

    def slot_units(self) -> Mapping[Slot, str]:
        """The UNIT each slot's value is converted to, by type.
        Fixed by the engine's transform for the keyword the type fills; a runtime writing no keywords states none."""
        return {}

    def published_units(self) -> Mapping[str, str]:
        """The UNIT each published variable is written in, by the name the result carries it under.
        A measurement and its variable compare in one unit only; a runtime publishing nothing named states none."""
        return {}

    def run_window_s(self, keywords: Mapping[str, Any]) -> float | None:
        """How long this run's solve covers, in seconds - the window a matched SERIES source must hold.
        The engine's deck states it; a runtime knowing no engine states none."""
        return None

    def checks(self, run: RunResult,
               inputs: Mapping[str, Mapping[str, Any]]) -> tuple[str, ...]:
        """Validation checks over the finished run, as NOTES the caller narrates.
        A template that declares no sensitivity classes produces no note, and a
        check reports - it never retracts a solved run."""
        return sensitivity_notes(self.sensitivity, self._published(run), inputs,
                                 mesh_size_m=self._mesh_size_m(run))

    def _published(self, run: RunResult) -> set[str]:
        """Every QUANTITY the run put on the map or a chart, by the name the publish stage wrote it under."""
        return {str(row.get("quantity") or "") for row in run.outputs} - {""}


    async def run(self, wire: Mapping[str, Any]) -> Any:
        """The absorbed tool body: fill every input, then launch when READY."""
        from .fill import Fill, fill

        try:
            state = await fill(Fill(workflow=self), wire)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "retryable", False):
                raise
            logger.exception("%s fill failed", self.name)
            return self._error(f"{self.error_prefix}_INTERNAL_ERROR", exc)
        if not state.ready:
            code, sentence = state.refusal()
            logger.warning("%s %s: %s", self.name, code, sentence)
            return {"status": "error", "error_code": code,
                    "error_message": sentence}
        return await self.execute(state)

    async def execute(self, state: Fill) -> Any:
        """Launch a READY fill, then post and publish what it produced."""
        started = time.monotonic()
        try:
            run = await self._launched(state)
        except asyncio.CancelledError:
            raise
        except DeclarativeError as exc:
            logger.warning("%s %s: %s", self.name, exc.error_code, exc)
            return self._error(exc.error_code, exc)
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "retryable", False):
                # A retryable typed error is a GATE: its .suggestions are harvested off the RAISED exception; an envelope destroys that channel.
                raise
            logger.exception("%s unexpected failure", self.name)
            return self._error(f"{self.error_prefix}_INTERNAL_ERROR", exc)
        run.notes[:0] = state.notes
        return await self._publish(run, time.monotonic() - started,
                                   supplied=dict(state.env.supplied))

    async def _launched(self, state: Fill) -> RunResult:
        """Launch under the run's own domain and collectors; notes ride out on the run or on the failure."""
        from .fill import production, restate

        env = production(state)
        token = bind_domain(state.domain)
        notes, outputs = bind_notes(), journal.bind_outputs()
        choices, coverage = journal.bind_choices(), bind_coverage()
        restate(state)
        # The drain lives only in the finally so a second reset never replaces the run's own error.
        try:
            try:
                run = await self.launch(state)
            finally:
                reset_domain(token)
                said_notes = drain_notes(notes)
                published = journal.drain_outputs(outputs)
                matched = journal.drain_choices(choices)
                drain_coverage(coverage)
        except Exception as exc:
            for note in [*said_notes, *env.absences]:
                exc.add_note(f"also missing from this run: {note}")
            raise
        # Read after the launch: a value a person edited on the review card and a row first read by a stage are stamped by then.
        run.entries = provenance_entries(env.inputs, self.inputs)
        run.inputs = dict(env.inputs)
        run.notes += [*said_notes, *env.absences]
        run.outputs += published
        run.choices += matched
        return run

    def _error(self, code: str, exc: BaseException) -> dict[str, Any]:
        """The failure, plus whatever auxiliary outputs the run also lost on the way."""
        notes = getattr(exc, "__notes__", ()) or ()
        # An exception that stringifies to nothing reports its type; the sentence is all a card prints.
        return {"status": "error", "error_code": code,
                "error_message": " ".join([said(exc), *notes])}


    async def _publish(self, run: RunResult, wall_seconds: float = 0.0, *,
                       supplied: Mapping[str, Any] | None = None) -> Any:
        result = run.value
        inputs = self._inputs(run, supplied or {})
        notes = list(run.notes) + [n for n in self.checks(run, inputs) if n]
        # The tie view: sources ranked equal on every fact travel as rows so the model or user picks; a clear winner carries no table.
        notes += [_ranked_rows(choice) for choice in run.choices if choice.tie]
        update: dict[str, Any] = {
            "synthetic_inputs": merge_provenance(
                getattr(result, "synthetic_inputs", None) or [], run.entries),
        }
        if notes:
            update["notes"] = [*(getattr(result, "notes", None) or []), *notes]
        result = result.model_copy(update=update)

        run_id = self._run_id(result, run)
        # The journal takes the MERGED notes: a label only on the layer would vanish with it.
        await asyncio.to_thread(self._journal, run_id, run, result,
                                wall_seconds, notes, inputs)
        logger.info("%s complete layer_id=%s stages=%s notes=%s",
                    self.name, getattr(result, "layer_id", None),
                    list(run.results), notes)
        return result

    def _run_id(self, result: Any, run: RunResult) -> str | None:
        """The solve's run prefix from the layer or the declared ``solve_step``; none without one."""
        direct = getattr(result, "run_id", None)
        if direct or not self.solve_step:
            return direct
        return (run.results.get(self.solve_step) or {}).get("run_id")

    def _mesh_size_m(self, run: RunResult) -> Any:
        """The edge the run was meshed at, off the solve step's record; none without a solve step."""
        if not self.solve_step:
            return None
        return (run.results.get(self.solve_step) or {}).get("mesh_size_m")

    def _module(self, run: RunResult) -> str | None:
        """Which module of the engine ran, as the solve step states it."""
        if not self.solve_step:
            return None
        return (run.results.get(self.solve_step) or {}).get("module")

    def _inputs(self, run: RunResult,
                supplied: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        """THE RUN'S INPUTS BY NAME - value, origin, source: every keyword of the solved deck off the
        solve step's sheet, then every input the fill answered that the deck does not already carry.
        A row's value is what the caller supplied for it; a matched one holds none and names its source."""
        sheet = (run.results.get(self.solve_step) or {}).get("sheet") or {} \
            if self.solve_step else {}
        inputs: dict[str, dict[str, Any]] = {}
        for name, row in (sheet.get("filled") or {}).items():
            origin, _, source = str(row.get("provenance") or "").partition(": ")
            inputs[name] = {"value": row.get("value"), "origin": origin,
                            "source": source or None}
        for name, verdict in run.inputs.items():
            if verdict.origin is None:
                continue
            held = self.input(name)
            # A keyword of the carrier's deck is that deck's own row; one stated on a coupled body keeps the name it was stated under.
            if getattr(held, "carried", False) and held.name in inputs:
                continue
            inputs[name] = {
                "value": (supplied.get(name) if getattr(held, "is_row", False)
                          else verdict.value),
                "origin": verdict.origin.value, "source": verdict.source or None}
        # An absent optional value holds none: its source is the sentence saying what the run derived in its place.
        for held in self.inputs:
            if held.absent_note and not held.is_row and held.name not in inputs:
                inputs[held.name] = {"value": None, "origin": Origin.DERIVED.value,
                                     "source": held.absent_note}
        return inputs

    def _correct_end(self, run: RunResult) -> bool | None:
        """Did the solver reach its own correct end? None where the metrics say nothing."""
        if not self.solve_step:
            return None
        metrics = (run.results.get(self.solve_step) or {}).get("metrics") or {}
        flag = metrics.get("correct_end")
        return None if flag is None else bool(flag)

    def _journal(self, run_id: str | None, run: RunResult, result: Any,
                 wall_seconds: float, notes: Sequence[str],
                 inputs: Mapping[str, Mapping[str, Any]]) -> None:
        """Append this run to the run journal, from publish where the inputs and wall time are both in hand."""
        from trid3nt_server.render.pipeline_emitter import current_emitter

        journal.append_record(journal.build_record(
            run_id=run_id, engine=self.engine or None,
            module=self._module(run), inputs=inputs,
            correct_end=self._correct_end(run),
            result=result, wall_seconds=round(wall_seconds, 3),
            origin=journal.run_origin(live_session=current_emitter() is not None),
            notes=list(notes), outputs=run.outputs,
            mesh=run.results.get("mesh"),
        ))


#: Controls every workflow carries: whether the run pauses, whether a kept mesh is rebuilt, the raw keyword floor,
#: and the source a caller names for a matched slot. None is a physical value, so none is an Input.
_CONTROLS: tuple[tuple[str, Any, Any], ...] = (
    ("input_mode", str | None, None),
    ("restart_clean", bool, False),
    ("keywords", dict | None, None),
    ("picks", dict | None, None),
)


# A template answers one question on one engine from its own declarations; a question spanning templates or fetchers
# is composed by the model, never a wrapper tool (a wrapper fixes the chain and hides its judgment from the model).
def register_workflow(
    facade: type[Workflow],
    metadata: Any,
    template: Any,
    *,
    sensitivity: Sequence[tuple[str, str]] | SensitivityDecl = (),
    coerce: Sequence[Callable[[dict], Mapping[str, Any]]] = (),
    **register_kwargs: Any,
) -> Callable[..., Any]:
    """Generate and register the tool for a declared workflow.
    The template module is the declaration (INPUTS, DOC); the signature is synthesized from the
    declared inputs, so the model-facing schema comes from the declaration the run resolves."""
    from trid3nt_server.tools import register_tool

    doc = getattr(template, "DOC", None)
    workflow = facade(metadata=metadata, inputs=getattr(template, "INPUTS"),
                      template=template, sensitivity=sensitivity, coerce=coerce)

    async def _run(**wire: Any) -> Any:
        return await workflow.run(wire)

    _run.__name__ = workflow.name
    _run.__qualname__ = workflow.name
    _run.__module__ = getattr(template, "__name__", __name__)
    sig, annotations = _wire_signature(workflow.inputs)
    _run.__signature__ = sig  # type: ignore[attr-defined]
    _run.__annotations__ = dict(annotations)
    _run.workflow = workflow  # type: ignore[attr-defined]
    if doc:
        from .docstring import render_docstring

        # The doc describes THIS wire, so it renders from the inputs the signature carries; the SHEET line is the
        # workflow's own, so a template cannot drift from it.
        sheet = workflow.sheet_doc()
        doc = {**doc, "inputs": workflow.inputs,
               **({} if sheet is None else {"sheet": sheet})}
        _run.__doc__ = render_docstring(**doc)
        _run.routing_doc = render_docstring(**doc, view="routing")  # type: ignore[attr-defined]

    register_kwargs.setdefault("read_only_hint", False)
    register_kwargs.setdefault("open_world_hint", False)
    register_kwargs.setdefault("destructive_hint", False)
    register_kwargs.setdefault("idempotent_hint", False)
    return register_tool(metadata, **register_kwargs)(_run)


def _wire_signature(inputs: Sequence[Input]) -> tuple[inspect.Signature, dict]:
    """The generated tool's signature: every input on the wire, then the controls.
    The ``**`` absorber carries an unknown key to the fill, which refuses it by name; without it the dispatch drops the key unsaid."""
    # A required input's annotation admits no None, which is what lists it in the tool schema's ``required``;
    # a producer-less row is on the wire because a caller naming the layer is the only way it is filled.
    entries: list[tuple[str, Any, Any]] = [
        (row.name, row.wire_type if row.required else row.wire_type | None, None)
        for row in inputs if row.fills_from_user]
    entries += list(_CONTROLS)
    seen: set[str] = set()
    sig_params: list[inspect.Parameter] = []
    annotations: dict[str, Any] = {}
    for name, ann, default in entries:
        if name in seen:
            raise WireArgsError(f"the wire declares {name!r} twice.")
        seen.add(name)
        annotations[name] = ann
        sig_params.append(inspect.Parameter(
            name, inspect.Parameter.POSITIONAL_OR_KEYWORD, default=default,
            annotation=ann))
    sig_params.append(inspect.Parameter("_extra_ignored",
                                        inspect.Parameter.VAR_KEYWORD, annotation=Any))
    annotations["_extra_ignored"] = Any
    annotations["return"] = Any
    return inspect.Signature(sig_params, return_annotation=Any), annotations


def _ranked_rows(choice: Any) -> str:
    """The ranked list as the tool result carries it: one line per source so a model can answer with a row."""
    rows = "; ".join(
        f"{index + 1}) {row.fetcher} - {row.resolution}, {row.recency}, "
        f"{row.datum}, {row.extent}"
        for index, row in enumerate(choice.rows))
    return (f"{choice.slot}: several sources rank equal for {choice.need}, and "
            f"{choice.picked} was taken. {rows}. Name another by its row to "
            "re-run on it.")
