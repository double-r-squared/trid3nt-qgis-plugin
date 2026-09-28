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
from typing import Any, Callable, Mapping, Sequence

from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.coverage import SourceChoice

from . import journal
from .accepts import Accepts
from .data import DataDecl, data_rows
from .domain import bind_domain, reset_domain
from .errors import (DeclarativeError, GateRefusedError, WorkflowParkedError,
                     said)
from .levers import with_levers
from .params import Param, ResolvedParams, doors, param_rows
from .resolution import SensitivityDecl, sensitivity_notes
from .resolver import merge_provenance, provenance_entries
from .fill import Fill, fill, production

__all__ = ["RunResult", "Workflow", "WireArgsError", "register_workflow"]

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
    params: ResolvedParams | None = None
    outputs: list[dict[str, Any]] = field(default_factory=list)
    choices: list[SourceChoice] = field(default_factory=list)
    keywords: dict[str, Any] = field(default_factory=dict)


class WireArgsError(DeclarativeError):
    """The wire arguments cannot be coerced into a sheet the workflow can run."""

    error_code = "WIRE_ARGS_INVALID"


class Workflow:
    """The universal skeleton. A template declares; this runs.
    A workflow declares two facts about its engine - the solver family and the name
    of its solve step; hooks have SILENT defaults and no subtype restates one."""

    #: The solver family a run of this workflow records.
    engine: str = ""

    #: What the SOLVE step is NAMED. The skeleton reads the run prefix off that
    #: step when the result carries none, and a workflow that renamed its solve
    #: would otherwise lose the run id to a literal guess. Declared, never assumed.
    solve_step: str = ""

    @classmethod
    def levers(cls) -> tuple[str, ...]:
        """WHICH runtime levers a declaration of this workflow takes without
        restating them: the stages that READ a lever are this class's, so a
        workflow that builds none seats none."""
        return ()

    def __init__(self, *, metadata: Any, params: Any,
                 template: Any, data: Any = (),
                 sensitivity: Sequence[tuple[str, str]] = (),
                 coerce: Sequence[Callable[[dict], Mapping[str, Any]]] = (),
                 accepts: Accepts | None = None,
                 levers: Sequence[str] = ()) -> None:
        self.metadata = metadata
        self.name = metadata.name
        #: The declared PARAMS rows, in class-body order - the template hands over
        #: the body itself and the row names are the attribute names on it - plus
        #: each RUNTIME LEVER this declaration takes rather than restating.
        self.params = with_levers(param_rows(params), levers)
        #: The declared DATA rows, the same way.
        self.data = data_rows(data)
        #: What this template accepts when something is SUPPLIED to it, role by
        #: role. Declared beside PARAMS because it is part of the same readable
        #: input contract, and read back off the registry by every supply door;
        #: absence is a refusal, per role and overall.
        self.accepts = accepts
        #: The template MODULE this workflow is declared by. Every stage is read
        #: off its own names - STEERING, OUTPUTS, CAPTIONS and the files
        #: beside them - so no object stands between the declaration and the run.
        self.template = template
        #: What this template CALLS each thing it names: every published variable,
        #: and every DATA row it reads a measurement into. One dict, because a
        #: caption is one kind of statement whatever it is about.
        self.captions = dict(getattr(template, "CAPTIONS", {}) or {})
        #: Which published reads sit in a resolution-sensitive class. The skeleton
        #: turns this into the run's honesty label; see ``resolution.py``.
        self.sensitivity = SensitivityDecl(sensitivity)
        self.coercions = tuple(coerce)
        self.error_prefix = str(getattr(metadata, "engine", "") or "workflow").upper()
        self.check()

    # -- hooks: silent defaults ------------------------------------------- #

    def check(self) -> None:
        """Refuse at import what this template's declarations cannot run; the
        skeleton runs nothing of its own, so it refuses nothing."""

    async def launch(self, state: Fill) -> RunResult:
        """Write, solve and read a READY fill -> what the run produced.

        The skeleton knows no engine, so it launches nothing."""
        raise WireArgsError(f"{self.name} declares no engine to launch.")

    def sheet_doc(self) -> str | None:
        """The ENGINE SURFACE line of this template's docstring, or nothing.

        Only the workflow knows which engine surface it fills, so a skeleton
        that fills none claims none."""
        return None

    def slot_units(self) -> Mapping[str, str]:
        """The UNIT each slot's value is converted to, by role.

        The unit is the one the KEYWORD that role fills is read in, which the
        engine's transform fixes - so a runtime that writes no keywords states
        none and a matched record is then read in the unit it was measured in."""
        return {}

    def published_units(self) -> Mapping[str, str]:
        """The UNIT each variable this run publishes is written in, by the name
        the result carries it under.

        What an observe row is read in: a measurement of a published variable
        and the variable itself are comparable only in one unit. A runtime that
        publishes nothing named states none, and a row that observes one then
        refuses rather than being read in whatever its source published."""
        return {}

    def run_window_s(self, keywords: Mapping[str, Any]) -> float | None:
        """How long this run's solve covers, in seconds - the window a matched
        SERIES source has to hold a record over.

        The engine's own deck states it, so a runtime that knows no engine
        states none and a series match then holds only the opening instant."""
        return None

    def checks(self, run: RunResult) -> tuple[str, ...]:
        """Validation checks over the finished run, as NOTES the caller narrates.
        A template that declares no sensitivity classes produces no note, and a
        check reports - it never retracts a solved run."""
        params = getattr(run, "params", None)
        sheet = params.rows() if params is not None else ()
        return sensitivity_notes(self.sensitivity, self.metadata,
                                 self._published(run), sheet,
                                 fill=self._fill(run),
                                 mesh_size_m=self._mesh_size_m(run))

    def _published(self, run: RunResult) -> set[str]:
        """Every QUANTITY this run put on the map or on a chart, by the name the
        publish stage wrote it under.

        One name for one quantity whichever product carries it, so a declaration
        stands on something a reader can open rather than on a field of its own."""
        return {str(row.get("quantity") or "") for row in run.outputs} - {""}

    # -- the spine --------------------------------------------------------- #

    async def run(self, wire: Mapping[str, Any]) -> Any:
        """The absorbed tool body: fill every input, then launch when READY."""
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
                # A retryable typed error is a GATE: the adapter harvests its
                # .suggestions off the RAISED exception so the model can retry with
                # corrected args. Flattening it into an envelope destroys that channel.
                raise
            logger.exception("%s unexpected failure", self.name)
            return self._error(f"{self.error_prefix}_INTERNAL_ERROR", exc)
        run.notes[:0] = state.notes
        return await self._publish(run, time.monotonic() - started,
                                   supplied=dict(state.env.supplied))

    async def _launched(self, state: Fill) -> RunResult:
        """The launch under the run's own domain and collectors: what a stage
        notes, publishes or matches rides out on the run, and a failed run's
        notes ride on the failure that ends it."""
        env = production(state)
        entries = provenance_entries(state.params, self.params)
        _refuse_invented_physics(entries, self.name, env.input_mode)
        token = bind_domain(state.domain)
        notes, outputs = journal.bind_notes(), journal.bind_outputs()
        choices, coverage = journal.bind_choices(), journal.bind_coverage()
        # A context token resets once; the drain lives only in the finally so
        # a second reset never replaces the run's own error.
        try:
            try:
                run = await self.launch(state)
            finally:
                reset_domain(token)
                said_notes = journal.drain_notes(notes)
                published = journal.drain_outputs(outputs)
                matched = journal.drain_choices(choices)
                journal.drain_coverage(coverage)
        except Exception as exc:
            for note in [*said_notes, *env.absences]:
                exc.add_note(f"also missing from this run: {note}")
            raise
        run.entries, run.params = entries, state.params
        run.keywords = dict(state.keywords)
        run.notes += [*said_notes, *env.absences]
        run.outputs += published
        run.choices += matched
        return run

    def accept_keyword(self, name: str, value: Any) -> tuple[str, Any, str]:
        """One engine keyword at fill -> ``(identifier, value, note)``.

        A skeleton that writes no engine input takes none."""
        raise WireArgsError(f"{self.name} writes no engine keywords; "
                            f"{name!r} is not an input of it.")

    def _error(self, code: str, exc: BaseException) -> dict[str, Any]:
        """The failure, plus whatever auxiliary products the run also lost on the way."""
        notes = getattr(exc, "__notes__", ()) or ()
        # The envelope's sentence is the only thing a card can print, so an
        # exception that stringifies to nothing reports its type instead.
        return {"status": "error", "error_code": code,
                "error_message": " ".join([said(exc), *notes])}

    # -- post + publish ---------------------------------------------------- #

    async def _publish(self, run: RunResult, wall_seconds: float = 0.0, *,
                       supplied: Mapping[str, Any] | None = None) -> Any:
        result = run.value
        notes = list(run.notes) + [n for n in self.checks(run) if n]
        # THE TIE VIEW: several sources ranked equal on every fact the sort
        # reads, so the rows travel on the result and the model or the user
        # picks one. A list with a clear winner carries no table - the sentence
        # already said which source filled the slot and why.
        notes += [_ranked_rows(choice) for choice in run.choices if choice.tie]
        update: dict[str, Any] = {
            "synthetic_inputs": merge_provenance(
                getattr(result, "synthetic_inputs", None) or [], run.entries),
        }
        if notes:
            existing = getattr(result, "fallback_note", None)
            parts = [existing] if existing else []
            parts += [f"NOTE: {n}" for n in notes]
            update["fallback_note"] = " ".join(parts)
        result = result.model_copy(update=update)

        run_id = self._run_id(result, run)
        # The journal takes the MERGED notes, not the launch's alone: a
        # resolution-sensitivity label that lived only on the layer would be gone
        # the moment the layer was, and the journal is the record that outlives
        # the artifacts.
        await asyncio.to_thread(self._journal, run_id, run, result,
                                wall_seconds, notes, dict(supplied or {}))
        logger.info("%s complete layer_id=%s stages=%s notes=%s",
                    self.name, getattr(result, "layer_id", None),
                    list(run.results), notes)
        return result

    def _run_id(self, result: Any, run: RunResult) -> str | None:
        """The solve's run prefix, from the layer or from the solve step itself.
        Read off the DECLARED ``solve_step``, never the literal ``"solve"``; a
        workflow that declares none has no prefix to find here."""
        direct = getattr(result, "run_id", None)
        if direct or not self.solve_step:
            return direct
        return (run.results.get(self.solve_step) or {}).get("run_id")

    def _mesh_size_m(self, run: RunResult) -> Any:
        """The EDGE the run was meshed at, off the solve step's own record.
        The mesh the run published is where this is a fact; a workflow that
        declares no solve step meshed nothing and states no spacing."""
        if not self.solve_step:
            return None
        return (run.results.get(self.solve_step) or {}).get("mesh_size_m")

    def _module(self, run: RunResult) -> str | None:
        """WHICH module of the engine ran, as the solve step itself states it.
        The engine is the workflow's; the module is the run's, so a family that
        shares one engine does not record every run under one sibling's name."""
        if not self.solve_step:
            return None
        return (run.results.get(self.solve_step) or {}).get("module")

    def _fill(self, run: RunResult) -> dict[str, dict[str, Any]]:
        """Every slot of the solved deck -> the value it was solved at, and where
        the fill took it from. Read off the solve step's own sheet, so a workflow
        that fills no sheet records no fill rather than an invented one."""
        if not self.solve_step:
            return {}
        sheet = (run.results.get(self.solve_step) or {}).get("sheet") or {}
        return {name: {"value": row.get("value"),
                       "from": str(row.get("provenance") or "")}
                for name, row in (sheet.get("filled") or {}).items()}

    def _correct_end(self, run: RunResult) -> bool | None:
        """Did the solver say it reached its own correct end? ``None`` where the
        solve step's metrics state nothing either way."""
        if not self.solve_step:
            return None
        metrics = (run.results.get(self.solve_step) or {}).get("metrics") or {}
        flag = metrics.get("correct_end")
        return None if flag is None else bool(flag)

    def _journal(self, run_id: str | None, run: RunResult, result: Any,
                 wall_seconds: float,
                 notes: Sequence[str] = (),
                 supplied: Mapping[str, Any] | None = None) -> None:
        """Append this run to the run journal - one seam, every engine.
        Called from publish, the one point where the sheet, the provenance rows
        and the wall time are all in hand at once."""
        from trid3nt_server.render.pipeline_emitter import current_emitter

        sheet = run.params.rows() if run.params is not None else ()
        journal.append_record(journal.build_record(
            run_id=run_id, engine=self.engine or None,
            module=self._module(run), fill=self._fill(run),
            correct_end=self._correct_end(run), sheet=sheet,
            provenance=getattr(result, "synthetic_inputs", None) or [],
            result=result, wall_seconds=round(wall_seconds, 3),
            origin=journal.run_origin(live_session=current_emitter() is not None),
            notes=list(notes), outputs=run.outputs, keywords=run.keywords,
            supplied=dict(supplied or {}), sources=run.choices,
        ))


#: The one code the invented-physics floor refuses under, so callers route on the
#: reason rather than on the shape of the run that hit it.
_PHYSICS_INPUT_REQUIRED = "PHYSICS_INPUT_REQUIRED"


def _refuse_invented_physics(entries: Sequence[SyntheticInput], tool_name: str,
                             input_mode: str | None) -> None:
    """A physics value nobody approved never reaches a solve: only a live
    user_gated session, whose review card puts the values in front of a
    person, opens the floor."""
    from trid3nt_server.gates.input_review import (physics_refusal_reason,
                                                   resolve_input_gate_mode)
    from trid3nt_server.render.pipeline_emitter import current_emitter

    headless = resolve_input_gate_mode(input_mode) != "auto"
    live = current_emitter() is not None
    if headless and live:
        return
    reason = physics_refusal_reason(tool_name, entries,
                                    no_session=headless and not live,
                                    no_review_surface=False)
    if reason:
        raise GateRefusedError(reason, error_code=_PHYSICS_INPUT_REQUIRED)


# -- the registration factory --------------------------------------------- #

#: Controls every workflow carries: whether the run PAUSES, whether a kept mesh
#: is rebuilt, the RAW KEYWORD floor a caller states the engine's own keywords
#: through, the source a caller NAMES for a matched slot, and the OPS a caller
#: states for one - the ordered moves that compose it past the one row the match
#: ranked first. None of the five is a physical value, so none is a Param.
_CONTROLS: tuple[tuple[str, Any, Any], ...] = (
    ("input_mode", str | None, None),
    ("restart_clean", bool, False),
    ("keywords", dict | None, None),
    ("picks", dict | None, None),
    ("ops", dict | None, None),
)


# A TEMPLATE IS THE SIMULATION SURFACE. One registered template answers one
# question on one engine, out of its own declarations. A question that spans
# several templates, or a template plus fetchers - an alert polygon routed into
# a flood run, a described spill turned into a plume, damage summed across
# hazards - is COMPOSED by the model from what is already registered, and does
# not become a tool of its own.
#
# A wrapper tool is an archetype somebody guessed. It fixes the chain, the AOI
# rule and the degrade path at authoring time, so the question that differs by
# one step has nothing to call, and the judgment it encoded is invisible to the
# model that needed it. That judgment belongs where the model reads it: the
# system prompt, or the docstring of the tool it routes to.
def register_workflow(
    facade: type[Workflow],
    metadata: Any,
    template: Any,
    *,
    parked: str | None = None,
    sensitivity: Sequence[tuple[str, str]] = (),
    coerce: Sequence[Callable[[dict], Mapping[str, Any]]] = (),
    levers: Sequence[str] | None = None,
    extra_args: Sequence[tuple[str, Any]] = (),
    **register_kwargs: Any,
) -> Callable[..., Any]:
    """Generate and register the tool for a declared workflow.
    The TEMPLATE MODULE is the declaration: PARAMS, DATA, ACCEPTS and DOC
    are read off its own names, and the workflow class reads the rest. The
    signature is synthesized from the declared params, so the model-facing schema
    comes from the same declaration the run resolves."""
    # ``parked="<reason>"`` builds and validates the declaration as always, then
    # leaves the MODEL SURFACE: the tool is never registered and the generated
    # function refuses typed, so membership never depends on import order.
    from trid3nt_server.tools import register_tool

    params = param_rows(getattr(template, "PARAMS"))
    data = getattr(template, "DATA", ())
    doc = getattr(template, "DOC", None)
    # WHICH runtime levers this declaration takes without restating them: the
    # stages that read a lever are the workflow class's, so the class answers,
    # and a template that seats none of them states so here.
    if levers is None:
        levers = facade.levers()
    workflow = facade(metadata=metadata, params=params, template=template,
                      data=data, sensitivity=sensitivity, coerce=coerce,
                      accepts=getattr(template, "ACCEPTS", None), levers=levers)
    params = workflow.params

    async def _run(**wire: Any) -> Any:
        if parked:
            raise WorkflowParkedError(
                f"{workflow.name} is parked and cannot be run: {parked}")
        return await workflow.run(wire)

    _run.__name__ = workflow.name
    _run.__qualname__ = workflow.name
    _run.__module__ = getattr(template, "__name__", __name__)
    #: The reason this template is off the model surface, or ``None``. Read by the
    #: roster checks, which ask the declaration rather than the import order.
    _run.parked = parked  # type: ignore[attr-defined]
    sig, annotations = _wire_signature(params, extra_args, workflow.data)
    _run.__signature__ = sig  # type: ignore[attr-defined]
    _run.__annotations__ = dict(annotations)
    _run.workflow = workflow  # type: ignore[attr-defined]
    if doc:
        from .docstring import render_docstring

        # The prose sheet describes THIS wire, so it is rendered from the params
        # the signature actually carries. A template declares `params=PARAMS` and
        # the factory narrows it; documenting a constant the schema does not offer
        # would be the docstring inviting a call the tool cannot take.
        # The SHEET line is the WORKFLOW's own: only it knows which engine
        # surface it fills, so a template never restates it and cannot drift
        # from what it actually declares.
        sheet = workflow.sheet_doc()
        doc = {**doc, "params": _wire_params(params),
               **({} if sheet is None else {"sheet": sheet}),
               **_context_doc(workflow.data,
                              doc.get("controls", ()))}
        _run.__doc__ = render_docstring(**doc)
        _run.routing_doc = render_docstring(**doc, view="routing")  # type: ignore[attr-defined]

    register_kwargs.setdefault("read_only_hint", False)
    register_kwargs.setdefault("open_world_hint", False)
    register_kwargs.setdefault("destructive_hint", False)
    if parked:
        return _run
    register_kwargs.setdefault("idempotent_hint", False)
    return register_tool(metadata, **register_kwargs)(_run)


def _context_doc(data: Sequence[DataDecl],
                 controls: Sequence[tuple[str, str]]) -> dict[str, Any]:
    """The docstring's ``context`` rows, and the ``controls`` with the slots taken out.
    A slot a CALLER can fill is documented ONCE: its shape from the declaration,
    the template's own prose appended, and its control row dropped."""
    slots = tuple(decl for decl in data if decl.fills_from_user)
    names = {decl.name for decl in slots}
    written = dict(controls)
    return {
        "controls": tuple((n, text) for n, text in controls if n not in names),
        "context": tuple(
            (decl.name, " ".join(p for p in (decl.doc_line, written.get(decl.name))
                                 if p))
            for decl in slots),
    }


def _wire_params(params: Sequence[Param]) -> tuple[Param, ...]:
    """The declared params the MODEL-FACING wire carries - the one definition of it.
    Read by both the synthesized signature and the generated docstring, so the
    schema and the prose cannot drift apart."""
    # Two exclusions: ``wire=False`` marks a value a COERCION resolves out of other
    # wire args, and a CONSTANT-door param is non-question physics the schema must
    # never invite the model to fill.
    return tuple(prm for prm in params
                 if prm.wire and prm.door != doors.CONSTANT)


def _wire_signature(params: Sequence[Param], extra: Sequence[tuple[str, Any]],
                    data: Sequence[DataDecl] = ()) -> tuple[inspect.Signature, dict]:
    """The generated tool's signature: declared params, context slots, aliases, controls.
    Every argument is keyword-with-default, and a ``**`` absorber keeps an unknown
    key from dead-ending a call the doors could still answer."""
    # CONSTANT-door params are absent here and from the docstring's param list: that
    # exclusion is the whole of the enforcement. The generated body still takes
    # ``**wire`` and filters the sheet by DECLARED name, so a value that arrives for
    # a constant anyway seats through the USER door with ``basis=user`` - which is
    # what keeps the row a user lever on the form card and the all-params invocation.
    entries: list[tuple[str, Any, Any]] = [
        (prm.name, prm.wire_type | None, None) for prm in _wire_params(params)
    ]
    # A producer-less Data slot IS on the wire: it has no source of its own, so
    # the only way it ever gets filled is a caller naming the layer. The slot's
    # declared SHAPE travels on the annotation, which is the schema's own record
    # of what the argument accepts.
    entries += [(decl.name, decl.wire_annotation, None) for decl in data
                if decl.fills_from_user]
    entries += [(name, ann, None) for name, ann in extra]
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
    """The ranked list as the TOOL RESULT carries it: the rows and their facts.

    One line per source so a model can answer with a row rather than a name."""
    rows = "; ".join(
        f"{index + 1}) {row.fetcher} - {row.resolution}, {row.recency}, "
        f"{row.datum}, {row.extent}"
        for index, row in enumerate(choice.rows))
    return (f"{choice.slot}: several sources rank equal for {choice.need}, and "
            f"{choice.picked} was taken. {rows}. Name another by its row to "
            "re-run on it.")
