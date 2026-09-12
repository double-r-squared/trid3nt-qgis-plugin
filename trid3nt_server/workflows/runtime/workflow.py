"""The workflow SKELETON: the template method every declared workflow runs on.

A template file declares a workflow; :class:`Workflow` IS one, and owns the
normalize/resolve/interpret spine, post and publish, and the registration factory.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from typing import Any, Callable, Mapping, Sequence

from trid3nt_contracts import new_ulid

from . import journal, snapshot
from .accepts import Accepts
from .data import DataDecl, data_rows
from .errors import DeclarativeError, PlanValidationError, WorkflowParkedError
from .params import Param, ResolvedParams, doors, param_rows
from .plan import Plan, Ref, Step
from .resolution import SensitivityDecl, answered, sensitivity_notes
from .resolver import merge_provenance, resolve_params
from .snapshot import Derivation
from .validate import validate_plan
from .validity import Validity, check_validity, refuse_undeclared_reads
from .interpreter import RunResult, interpret

__all__ = ["Workflow", "WireArgsError", "register_workflow"]

logger = logging.getLogger("trid3nt_server.workflows.runtime.workflow")


class WireArgsError(DeclarativeError):
    """The wire arguments cannot be coerced into a sheet the workflow can run."""

    error_code = "WIRE_ARGS_INVALID"


def _provenance_row(row: str | tuple[str, str]) -> tuple[str, str]:
    """One declared ``provenance=`` entry, as ``(param, note_key)``.
    A bare name takes the ``<param>_note`` key; a row that is not exactly a pair is
    refused here rather than dropping its tail or raising at answer time."""
    if isinstance(row, str):
        return (row, f"{row}_note")
    pair = tuple(row)
    if len(pair) != 2 or not all(isinstance(part, str) for part in pair):
        raise PlanValidationError(
            f"provenance row {row!r} is not (param, note_key): a provenance entry is "
            "either a param NAME or a two-string pair naming the note's key.")
    return (pair[0], pair[1])


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

    def __init__(self, *, metadata: Any, params: Any,
                 plan: Callable[..., Any], data: Any = (),
                 answer: Sequence[str] = (),
                 provenance: Sequence[str | tuple[str, str]] = (),
                 sensitivity: Sequence[tuple[str, str]] = (),
                 validity: Sequence[Validity] = (),
                 coerce: Sequence[Callable[[dict], Mapping[str, Any]]] = (),
                 accepts: Accepts | None = None) -> None:
        self.metadata = metadata
        self.name = metadata.name
        #: The declared PARAMS rows, in class-body order - the template hands over
        #: the body itself and the row names are the attribute names on it.
        self.params = param_rows(params)
        #: The declared DATA rows, the same way.
        self.data = data_rows(data)
        #: What this template accepts when something is SUPPLIED to it, role by
        #: role. Declared beside PARAMS because it is part of the same readable
        #: input contract, and read back off the registry by every supply door;
        #: absence is a refusal, per role and overall.
        self.accepts = accepts
        self.plan_decl = plan
        self.answer_fields = tuple(answer)
        #: Each declared provenance name lifts its resolved VALUE and its NOTE onto
        #: the answer. A pair names the note's key where the value's name plus
        #: "_note" is not what the answer has always called it.
        self.answer_provenance = tuple(_provenance_row(row) for row in provenance)
        #: Which ANSWER fields sit in a resolution-sensitive class. The skeleton
        #: turns this into the run's honesty label; see ``resolution.py``.
        self.sensitivity = SensitivityDecl(sensitivity)
        #: Cross-param rules a single Param declaration cannot express - checked
        #: on every lane, because a sheet is a sheet whether it came from a fresh
        #: invocation or from a derivation of one. See ``validity.py``.
        self.validity = tuple(validity)
        refuse_undeclared_reads(self.validity, self.params)
        self.coercions = tuple(coerce)
        self.error_prefix = str(getattr(metadata, "engine", "") or "workflow").upper()
        #: The plan is STATIC - it reads no concrete value - so it is built and
        #: validated ONCE, here, at import. An unreachable Ref, a
        #: misplaced gate or a physics process the facade does not model is an
        #: AUTHORING error, and this is the last moment it can be reported as one.
        self.plan = self.build_plan()
        validate_plan(self.plan, self.params, self.data)

    def build_plan(self) -> Plan:
        """The declared plan value, named and engined by the WORKFLOW, not restated."""
        nodes = self.plan_decl(self)
        if isinstance(nodes, Plan):
            return nodes
        return Plan(name=self.name, engine=self.engine or None,
                    steps=tuple(nodes) if isinstance(nodes, (list, tuple)) else (nodes,))

    # -- hooks: silent defaults ------------------------------------------- #

    def checks(self, result: Any, run: RunResult) -> tuple[str, ...]:
        """Validation checks over the finished result, as NOTES the caller narrates.
        A template that declares no sensitivity classes produces no note, and a
        check reports - it never retracts a solved run."""
        params = getattr(run, "params", None)
        sheet = params.rows() if params is not None else ()
        return sensitivity_notes(self.sensitivity, self.metadata, result, sheet)

    # -- the spine --------------------------------------------------------- #

    async def run(self, wire: Mapping[str, Any]) -> Any:
        """The absorbed tool body: normalize, resolve, then the shared spine."""
        supplied, err = await self._normalize(dict(wire))
        if err is not None:
            return err
        return await self.execute(
            self._resolve(supplied), input_mode=wire.get("input_mode"),
            keywords=wire.get("keywords"),
            resume=not bool(wire.get("restart_clean")),
            supplied=self._supplied_artifacts(wire))

    async def execute(self, resolving: Any, *, input_mode: str | None = None,
                      keywords: Mapping[str, Any] | None = None,
                      resume: bool = True,
                      supplied: Mapping[str, Any] | None = None,
                      derived_from: Derivation | None = None) -> Any:
        """Run the plan on a resolved sheet: interpret, post, publish - the spine a
        fresh invocation and a rerun-with-overrides both take. ``resolving`` may be
        an awaitable, so a resolve refusal lands inside this method's envelope."""
        supplied_artifacts = dict(supplied or {})
        started = time.monotonic()
        try:
            p = await resolving if inspect.isawaitable(resolving) else resolving
            check_validity(self.validity, p, workflow=self.name)
            run = await interpret(
                self.plan, p, self.params, self.data,
                input_mode=input_mode, keywords=keywords, resume=resume,
                supplied=supplied_artifacts,
            )
        except asyncio.CancelledError:
            raise
        except DeclarativeError as exc:
            logger.warning("%s %s: %s", self.name, exc.error_code, exc)
            return await self._record_failure(exc, input_mode, keywords,
                                              supplied_artifacts)
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "retryable", False):
                # A retryable typed error is a GATE: the adapter harvests its
                # .suggestions off the RAISED exception so the model can retry with
                # corrected args. Flattening it into an envelope destroys that channel.
                raise
            logger.exception("%s unexpected failure", self.name)
            return self._error(f"{self.error_prefix}_INTERNAL_ERROR", exc)
        return await self._publish(run, time.monotonic() - started,
                                   input_mode=input_mode, keywords=keywords,
                                   supplied=supplied_artifacts,
                                   derived_from=derived_from)

    async def _resolve(self, supplied: Mapping[str, Any]) -> ResolvedParams:
        return await resolve_params(self.params, supplied)

    async def _record_failure(self, exc: DeclarativeError, input_mode: str | None,
                              keywords: Mapping[str, Any] | None,
                              supplied: Mapping[str, Any]) -> dict[str, Any]:
        """The failure envelope, plus a handle on the work the attempt DID finish.
        The attempt is recorded like a completed run under an id the envelope names,
        because the retry a failure wants is a different invocation."""
        envelope = self._error(exc.error_code, exc)
        run = getattr(exc, "partial_run", None)
        records = list(getattr(run, "records", ()) or ())
        if run is None or run.params is None or not records:
            return envelope
        attempt = new_ulid()
        await snapshot.write_snapshot(
            run_id=attempt, workflow=self.name, input_mode=input_mode,
            keywords=dict(keywords or {}),
            sheet=run.params.rows(), records=records,
            data_records=list(run.data_records), supplied=dict(supplied))
        envelope["run_id"] = attempt
        envelope["error_message"] += (
            f" This attempt is recorded as run {attempt}, with "
            f"{', '.join(run.executed)} already done: rerun_workflow(run_id="
            f"'{attempt}', overrides={{...}}) re-runs the question with the value "
            "corrected and inherits that work.")
        return envelope

    # -- normalize --------------------------------------------------------- #

    async def _normalize(self, args: dict[str, Any]
                         ) -> tuple[dict[str, Any], dict | None]:
        """Coerce the wire args into the door-1 sheet through the declared coercions.
        Three-way: a retryable typed error PROPAGATES, a typed refusal reports under
        its own code, and anything else reports as an internal error. A coercion
        that ingests from the world - a layer read, a geocode, a canvas pick - is
        awaited where it stands."""
        # A retryable error must not be flattened into an envelope: that destroys
        # the ``.suggestions`` channel the adapter harvests off the raised exception.
        try:
            for coercion in self.coercions:
                coerced = coercion(args)
                if inspect.isawaitable(coerced):
                    coerced = await coerced
                args.update(coerced or {})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "retryable", False):
                raise
            code = getattr(exc, "error_code", None)
            if code is None:
                logger.exception("%s coercion failed", self.name)
                code = f"{self.error_prefix}_INTERNAL_ERROR"
            return {}, self._error(code, exc)
        declared = {prm.name for prm in self.params}
        return {k: v for k, v in args.items()
                if k in declared and v is not None}, None

    def _supplied_artifacts(self, wire: Mapping[str, Any]) -> dict[str, Any]:
        """Artifacts handed in for producer-less ``Data`` slots, by slot name.
        The wire argument carries the slot's own name, so "which layer is this" is
        answerable from the declaration alone."""
        return {decl.name: wire[decl.name] for decl in self.data
                if decl.producer is None and wire.get(decl.name) is not None}

    def _error(self, code: str, exc: BaseException) -> dict[str, Any]:
        """The failure, plus whatever auxiliary products the run also lost on the way."""
        notes = getattr(exc, "__notes__", ()) or ()
        return {"status": "error", "error_code": code,
                "error_message": " ".join([str(exc), *notes])}

    # -- post + publish ---------------------------------------------------- #

    async def _publish(self, run: RunResult, wall_seconds: float = 0.0, *,
                       input_mode: str | None = None,
                       keywords: Mapping[str, Any] | None = None,
                       supplied: Mapping[str, Any] | None = None,
                       derived_from: Derivation | None = None) -> Any:
        result = run.value
        notes = list(run.notes) + [n for n in self.checks(result, run) if n]
        if derived_from is not None:
            notes.append(
                f"derived from run {derived_from.parent_run_id} by overriding "
                + ", ".join(derived_from.overrides))
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

        metrics = self.answer(result)
        run_id = self._run_id(result, run)
        await self._persist(run_id, run.charts, metrics)
        # The journal takes the MERGED notes, not the interpreter's alone: a
        # resolution-sensitivity label that lived only on the layer would be gone
        # the moment the layer was, and the journal is the record that outlives
        # the artifacts.
        await asyncio.to_thread(self._journal, run_id, run, result, metrics,
                                wall_seconds, notes, derived_from)
        # The snapshot rides the same moment for the same reason: this is where a
        # run holds its own past whole - the sheet it ran on, the records it left,
        # the artifacts it was handed - and any later point would be reassembling
        # it from products that are allowed to disappear.
        await snapshot.write_snapshot(
            run_id=run_id, workflow=self.name, input_mode=input_mode,
            keywords=dict(keywords or {}),
            sheet=run.params.rows() if run.params is not None else (),
            records=run.records, data_records=run.data_records,
            supplied=dict(supplied or {}), derived_from=derived_from)
        logger.info("%s complete layer_id=%s answer=%s executed=%s replayed=%s notes=%s",
                    self.name, getattr(result, "layer_id", None),
                    {k: v for k, v in metrics.items() if not isinstance(v, list)},
                    run.executed, run.replayed, notes)
        return result

    def answer(self, result: Any) -> dict[str, Any]:
        """The run's ANSWER: the numbers a reader has to be able to check.
        A declared provenance name rides its resolved value AND its note, so what a
        row was pinned to is on the artifact rather than recomputed."""
        out: dict[str, Any] = {f: answered(result, f) for f in self.answer_fields}
        out["layer_uri"] = getattr(result, "uri", None)
        rows = getattr(result, "synthetic_inputs", None) or []
        for name, note_key in self.answer_provenance:
            row = next((r for r in rows if getattr(r, "param", None) == name), None)
            out[name] = getattr(row, "value", None) if row else None
            out[note_key] = getattr(row, "note", None) if row else None
        return out

    def _run_id(self, result: Any, run: RunResult) -> str | None:
        """The solve's run prefix, from the layer or from the solve step itself.
        Read off the DECLARED ``solve_step``, never the literal ``"solve"``; a
        workflow that declares none has no prefix to find here."""
        direct = getattr(result, "run_id", None)
        if direct or not self.solve_step:
            return direct
        return (run.results.get(self.solve_step) or {}).get("run_id")

    def _module(self, run: RunResult) -> str | None:
        """WHICH module of the engine ran, as the solve step itself states it.
        The engine is the workflow's; the module is the run's, so a family that
        shares one engine does not record every run under one sibling's name."""
        if not self.solve_step:
            return None
        return (run.results.get(self.solve_step) or {}).get("module")

    def _fill(self, run: RunResult) -> dict[str, str]:
        """Every slot of the solved deck, and where the fill took it from.
        Read off the solve step's own sheet, so a workflow that fills no sheet
        records no fill rather than an invented one."""
        if not self.solve_step:
            return {}
        sheet = (run.results.get(self.solve_step) or {}).get("sheet") or {}
        return {name: str(row.get("provenance") or "")
                for name, row in (sheet.get("filled") or {}).items()}

    def _journal(self, run_id: str | None, run: RunResult, result: Any,
                 metrics: Mapping[str, Any], wall_seconds: float,
                 notes: Sequence[str] = (),
                 derived_from: Derivation | None = None) -> None:
        """Append this run to the run journal - one seam, every engine.
        Called from publish, the one point where the sheet, the answer, the
        provenance rows and the wall time are all in hand at once."""
        from trid3nt_server.render.pipeline_emitter import current_emitter

        sheet = run.params.rows() if run.params is not None else ()
        journal.append_record(journal.build_record(
            run_id=run_id, engine=self.engine or None,
            module=self._module(run), fill=self._fill(run),
            sheet=sheet, answer=metrics,
            provenance=getattr(result, "synthetic_inputs", None) or [],
            result=result, wall_seconds=round(wall_seconds, 3),
            origin=journal.run_origin(live_session=current_emitter() is not None),
            executed=run.executed, replayed=run.replayed, notes=list(notes),
            parent_run_id=derived_from.parent_run_id if derived_from else None,
            overrides=derived_from.overrides if derived_from else (),
        ))

    @staticmethod
    async def _persist(run_id: str | None, charts: Mapping[str, Any],
                       metrics: Mapping[str, Any]) -> None:
        from trid3nt_server.workflows.runtime.run_products import persist_run_products

        await persist_run_products(run_id, charts=charts, metrics=metrics)


# -- the registration factory --------------------------------------------- #

#: Controls every workflow carries: whether the run PAUSES, whether it resumes,
#: and the RAW KEYWORD floor a caller states the engine's own keywords through.
#: None of the three is a physical value, so none of them is a Param.
_CONTROLS: tuple[tuple[str, Any, Any], ...] = (
    ("input_mode", str | None, None),
    ("restart_clean", bool, False),
    ("keywords", dict | None, None),
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
    params: Any,
    plan: Callable[..., Any],
    *,
    data: Any = (),
    parked: str | None = None,
    answer: Sequence[str] = (),
    provenance: Sequence[str | tuple[str, str]] = (),
    sensitivity: Sequence[tuple[str, str]] = (),
    validity: Sequence[Validity] = (),
    coerce: Sequence[Callable[[dict], Mapping[str, Any]]] = (),
    accepts: Accepts | None = None,
    doc: Mapping[str, Any] | None = None,
    extra_args: Sequence[tuple[str, Any]] = (),
    **register_kwargs: Any,
) -> Callable[..., Any]:
    """Generate and register the tool for a declared workflow.
    The signature is synthesized from the declared params, so the model-facing
    schema comes from the same declaration the run resolves."""
    # ``parked="<reason>"`` builds and validates the declaration as always, then
    # leaves the MODEL SURFACE: the tool is never registered and the generated
    # function refuses typed, so membership never depends on import order.
    from trid3nt_server.tools import register_tool

    params = param_rows(params)
    workflow = facade(metadata=metadata, params=params, plan=plan, data=data,
                      answer=answer, provenance=provenance,
                      sensitivity=sensitivity, validity=validity, coerce=coerce,
                      accepts=accepts)

    async def _run(**wire: Any) -> Any:
        if parked:
            raise WorkflowParkedError(
                f"{workflow.name} is parked and cannot be run: {parked}")
        return await workflow.run(wire)

    _run.__name__ = workflow.name
    _run.__qualname__ = workflow.name
    _run.__module__ = getattr(plan, "__module__", __name__)
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
        # The SHEET line is the plan's own: only the plan knows which engine
        # surface it fills, so a template never restates it and cannot drift
        # from what it actually declares.
        sheet_doc = getattr(plan, "sheet_doc", None)
        doc = {**doc, "params": _wire_params(params),
               **({} if sheet_doc is None else {"sheet": sheet_doc()}),
               **_context_doc(workflow.data, doc.get("controls", ()))}
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
    A producer-less slot is documented ONCE: its shape from the declaration, the
    template's own prose appended, and its control row dropped."""
    slots = tuple(decl for decl in data if decl.producer is None)
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
                if decl.producer is None]
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
