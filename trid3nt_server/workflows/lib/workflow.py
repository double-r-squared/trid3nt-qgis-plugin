"""The workflow SKELETON: the template method every declared workflow runs on.

A template file declares a workflow; :class:`Workflow` IS one. The skeleton owns
everything that never varies between questions - the normalize -> resolve ->
interpret spine, the post + publish stages, the typed error envelope, the chart
HOOK and its persistence, the answer artifact, and the registration factory - and
the engine facade (:class:`EngineOps`, realized as ``TelemacWorkflow`` and
friends) realizes exactly five operations. The mechanics behind the invariants -
gate cards, chart building and emission, solve supervision, ledger + resume, the
leak guard - are the interpreter's, and stay there.

The skeleton COMPOSES the library; it does not reimplement it. Gates, ledger,
binding and the leak guard all live in ``interpreter.py`` and stay there - the
no-double-middleware law applies to our own library as much as to the fetcher
router.

See ``docs/design/declarative-workflows.md``, "The Workflow Skeleton".
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from typing import Any, Callable, Mapping, Sequence

from trid3nt_contracts import new_ulid

from . import journal, snapshot
from .data import DataDecl
from .errors import DeclarativeError, PlanValidationError
from .params import Param, ResolvedParams, doors
from .plan import Plan, Ref, Step
from .resolution import SensitivityDecl, sensitivity_notes
from .resolver import merge_provenance, resolve_params
from .snapshot import Derivation
from .validate import validate_plan
from .validity import Validity, check_validity, refuse_undeclared_reads
from .interpreter import RunResult, interpret

__all__ = ["EngineOps", "FacadeIncompleteError",
           "Workflow", "WireArgsError", "register_workflow"]

logger = logging.getLogger("trid3nt_server.workflows.lib.workflow")


class WireArgsError(DeclarativeError):
    """The wire arguments cannot be coerced into a sheet the workflow can run."""

    error_code = "WIRE_ARGS_INVALID"


class FacadeIncompleteError(DeclarativeError):
    """A registered workflow's facade leaves one of the EngineOps five unrealized.

    An AUTHORING error, refused at registration (import) time: a facade with a hole
    in it would otherwise run until the plan reached the missing operation and then
    surface the raw ``NotImplementedError`` to the model as an
    ``<ENGINE>_INTERNAL_ERROR`` - a declaration defect wearing a runtime failure's
    clothes.
    """

    error_code = "FACADE_INCOMPLETE"


class EngineOps:
    """The engine facade: five operations, and nothing else.

    The facade's value is STABILITY - the interface never changes while the
    mechanisms behind it (meshers, deck writers, result readers) evolve freely.
    Facades are named by engine only; a domain qualifier in the name would weld a
    domain assumption into the engine, and domain shape arrives through
    ``acquire_domain``'s slots instead.
    """

    #: The solver family a plan built by this facade records.
    engine: str = ""

    #: What ``solver_spec`` NAMES its step. The skeleton reads the run prefix off
    #: that step when the result carries none, and a facade that renamed its solve
    #: would otherwise lose the run id to a literal guess. Declared, never assumed.
    solve_step: str = ""

    #: The operations a facade must realize to be registrable.
    MUST_FILL: tuple[str, ...] = ("acquire_domain", "build_mesh", "author",
                                  "solver_spec", "read_results")

    def acquire_domain(self, **slots: Any) -> tuple[Step, ...]:
        """The steps that establish the modeled world and its resolved state."""
        raise NotImplementedError(f"{type(self).__name__} realizes no acquire_domain.")

    def build_mesh(self, domain: Any, policy: Any, **slots: Any) -> Any:
        """The mesh, from an acquired domain and an engine-neutral :class:`MeshPolicy`.

        FROZEN interface: user-supplied meshes, the shared generation front and the
        mesh gate all arrive behind it without the declaration changing. Domain
        SHAPE that is not universal (a corridor's extent and width, a basin's
        outlet) arrives as an engine slot, never as a field on the neutral policy.
        """
        raise NotImplementedError(f"{type(self).__name__} realizes no build_mesh.")

    def author(self, *, mesh: Any, physics: Any, forcing: Any) -> Step:
        """Serialize the mesh + physics + forcing slots into the engine's own deck."""
        raise NotImplementedError(f"{type(self).__name__} realizes no author.")

    def solver_spec(self, **slots: Any) -> Step:
        """The declared solve: which image, which limits, which sizing class."""
        raise NotImplementedError(f"{type(self).__name__} realizes no solver_spec.")

    def read_results(self, run: Any, **slots: Any) -> Step:
        """Read the solve's raw output into the question's published deliverable."""
        raise NotImplementedError(f"{type(self).__name__} realizes no read_results.")


def _provenance_row(row: str | tuple[str, str]) -> tuple[str, str]:
    """One declared ``provenance=`` entry, as ``(param, note_key)``.

    A bare name takes the conventional ``<param>_note`` key. A PAIR names the note
    key where the answer artifact has always called it something else - and it must
    be exactly a pair: a three-element row would silently drop its tail and a
    one-element row would raise deep inside :meth:`Workflow.answer`, long after the
    declaration that was wrong.
    """
    if isinstance(row, str):
        return (row, f"{row}_note")
    pair = tuple(row)
    if len(pair) != 2 or not all(isinstance(part, str) for part in pair):
        raise PlanValidationError(
            f"provenance row {row!r} is not (param, note_key): a provenance entry is "
            "either a param NAME or a two-string pair naming the note's key.")
    return (pair[0], pair[1])


class Workflow(EngineOps):
    """The universal skeleton. A template declares; this runs.

    Stage sequence (``plan.STAGES``): acquire -> prep -> mesh -> gates -> author ->
    solve -> post -> publish. The declared plan expresses the first six; the
    skeleton's own body is post + publish, plus the normalize/resolve/interpret
    spine in front of them.

    HOOKS have SILENT defaults: an unfilled hook does nothing, and no engine
    subtype ever restates one. ABSTRACT SLOTS must be filled: the physics the
    template declares and the :class:`EngineOps` five.

    The contract also names a sensor/context-layer hook. It is deliberately NOT
    here: the steps that fetch inputs already emit their own through the one
    emission seam, so a skeleton-level second emitter would be exactly the
    double-emission the input-surfacing guard exists to catch. It arrives with
    the emission-unification wave, where the seam is the single home.
    """

    def __init__(self, *, metadata: Any, params: Sequence[Param],
                 plan: Callable[..., Any], data: Sequence[DataDecl] = (),
                 answer: Sequence[str] = (),
                 provenance: Sequence[str | tuple[str, str]] = (),
                 sensitivity: Sequence[tuple[str, str]] = (),
                 validity: Sequence[Validity] = (),
                 coerce: Sequence[Callable[[dict], Mapping[str, Any]]] = ()) -> None:
        self.metadata = metadata
        self.name = metadata.name
        self.params = tuple(params)
        self.data = tuple(data)
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
        #: validated ONCE, here, at import. A P/D typo, an unreachable Ref, a
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

        The one universal check is the RESOLUTION-SENSITIVITY label: an answer in a
        class the mesh decides says so, and says which way a coarse mesh reads it.
        Every engine gets it free the moment its template declares the classes; a
        template that declares none produces no note. A check reports; it never
        retracts a solved run.
        """
        params = getattr(run, "params", None)
        sheet = params.rows() if params is not None else ()
        return sensitivity_notes(self.sensitivity, self.metadata, result, sheet)

    # -- the spine --------------------------------------------------------- #

    async def run(self, wire: Mapping[str, Any]) -> Any:
        """The absorbed tool body: normalize, resolve, then the shared spine."""
        supplied, err = self._normalize(dict(wire))
        if err is not None:
            return err
        return await self.execute(
            self._resolve(supplied), input_mode=wire.get("input_mode"),
            resume=not bool(wire.get("restart_clean")),
            supplied=self._supplied_artifacts(wire))

    async def execute(self, resolving: Any, *, input_mode: str | None = None,
                      resume: bool = True,
                      supplied: Mapping[str, Any] | None = None,
                      derived_from: Derivation | None = None) -> Any:
        """Run the plan on a resolved sheet: interpret, post, publish.

        The spine BOTH lanes share. A fresh invocation resolves its sheet from the
        wire; a rerun-with-overrides resolves it from its parent's. From here on
        the two runs are the same run, which is what keeps a derived run's
        gates, ledger, journal line and answer artifact identical to an original's
        instead of a second implementation of them.

        ``resolving`` is the sheet or an awaitable of it, so the resolve itself
        lands inside this method's error envelope - a bounds refusal is a refusal
        about the run, and reporting it any other way would give one class of
        typed error two shapes.
        """
        supplied_artifacts = dict(supplied or {})
        started = time.monotonic()
        try:
            p = await resolving if inspect.isawaitable(resolving) else resolving
            check_validity(self.validity, p, workflow=self.name)
            run = await interpret(
                self.plan, p, self.params, self.data,
                input_mode=input_mode, resume=resume,
                supplied=supplied_artifacts,
            )
        except asyncio.CancelledError:
            raise
        except DeclarativeError as exc:
            logger.warning("%s %s: %s", self.name, exc.error_code, exc)
            return await self._record_failure(exc, input_mode, supplied_artifacts)
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "retryable", False):
                # A retryable typed error is a GATE: the adapter harvests its
                # .suggestions off the RAISED exception so the model can retry with
                # corrected args. Flattening it into an envelope destroys that channel.
                raise
            logger.exception("%s unexpected failure", self.name)
            return self._error(f"{self.error_prefix}_INTERNAL_ERROR", exc)
        return await self._publish(run, time.monotonic() - started,
                                   input_mode=input_mode,
                                   supplied=supplied_artifacts,
                                   derived_from=derived_from)

    async def _resolve(self, supplied: Mapping[str, Any]) -> ResolvedParams:
        return await resolve_params(self.params, supplied)

    async def _record_failure(self, exc: DeclarativeError, input_mode: str | None,
                              supplied: Mapping[str, Any]) -> dict[str, Any]:
        """The failure envelope, plus a handle on the work the attempt DID finish.

        A run that dies at the deck has already geocoded, fetched and meshed. The
        step ledger keeps that for a retry of the SAME invocation - but the retry
        a failure actually wants is the same question with the bad value
        CORRECTED, which is a different invocation and would replay nothing. So
        the attempt is recorded like a completed run, under an id the envelope
        names, and the caller re-runs it through the one primitive instead of
        paying for the good work twice.
        """
        envelope = self._error(exc.error_code, exc)
        run = getattr(exc, "partial_run", None)
        records = list(getattr(run, "records", ()) or ())
        if run is None or run.params is None or not records:
            return envelope
        attempt = new_ulid()
        await snapshot.write_snapshot(
            run_id=attempt, workflow=self.name, input_mode=input_mode,
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

    def _normalize(self, args: dict[str, Any]) -> tuple[dict[str, Any], dict | None]:
        """Coerce the wire args into the door-1 sheet through the declared coercions.

        The same three-way discrimination :meth:`run` makes, because a coercion can
        raise all three things: a RETRYABLE typed error is a gate and must PROPAGATE
        (flattening it into an envelope destroys the ``.suggestions`` channel the
        adapter harvests off the raised exception); a typed refusal reports under its
        own code; and anything else is a BUG in our own coercion, which reports as an
        internal error rather than blaming the caller's params.
        """
        try:
            for coercion in self.coercions:
                args.update(coercion(args) or {})
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

        A context slot has no producer BY DESIGN - the template will not name a
        default source for a breakwater or a clip zone - so the only way one gets
        filled is somebody handing it over. The wire argument carries the slot's
        own name, which is what makes "which layer is this" answerable from the
        declaration alone.
        """
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

        Persisted beside the chart spec so verification cites the run's own figures
        rather than recomputing them from the raster. A declared provenance name
        rides its resolved value AND its note, so a fetched cycle (never a bare
        "latest") is pinned here too.
        """
        out: dict[str, Any] = {f: getattr(result, f, None) for f in self.answer_fields}
        out["layer_uri"] = getattr(result, "uri", None)
        rows = getattr(result, "synthetic_inputs", None) or []
        for name, note_key in self.answer_provenance:
            row = next((r for r in rows if getattr(r, "param", None) == name), None)
            out[name] = getattr(row, "value", None) if row else None
            out[note_key] = getattr(row, "note", None) if row else None
        return out

    def _run_id(self, result: Any, run: RunResult) -> str | None:
        """The solve's run prefix, from the layer or from the solve step itself.

        The step is the one the FACADE declares (``solve_step``), never the literal
        ``"solve"``: a facade that names its solve step something else would
        silently lose the prefix, and the run's chart spec and metrics would be
        persisted nowhere. An analysis-only workflow declares no solve step and
        simply has no prefix to find here.
        """
        direct = getattr(result, "run_id", None)
        if direct or not self.solve_step:
            return direct
        return (run.results.get(self.solve_step) or {}).get("run_id")

    def _journal(self, run_id: str | None, run: RunResult, result: Any,
                 metrics: Mapping[str, Any], wall_seconds: float,
                 notes: Sequence[str] = (),
                 derived_from: Derivation | None = None) -> None:
        """Append this run to the run journal - one seam, every engine.

        The publish stage is where a run has everything the record needs at once:
        the sheet it actually ran on, the answer it published, the provenance rows
        and the wall time. Anywhere else would be reassembling it from artifacts
        that are allowed to disappear.
        """
        from trid3nt_server.emission.pipeline_emitter import current_emitter

        sheet = run.params.rows() if run.params is not None else ()
        journal.append_record(journal.build_record(
            run_id=run_id, template=self.name, engine=self.engine or None,
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
        from trid3nt_server.workflows.shared.run_products import persist_run_products

        await persist_run_products(run_id, charts=charts, metrics=metrics)


# -- the registration factory --------------------------------------------- #

#: Controls every workflow carries. They govern whether the run PAUSES and whether
#: it resumes; neither is a physical value, so neither is a Param.
_CONTROLS: tuple[tuple[str, Any, Any], ...] = (
    ("input_mode", str | None, None),
    ("restart_clean", bool, False),
)


def register_workflow(
    facade: type[Workflow],
    metadata: Any,
    params: Sequence[Param],
    plan: Callable[..., Any],
    *,
    data: Sequence[DataDecl] = (),
    answer: Sequence[str] = (),
    provenance: Sequence[str | tuple[str, str]] = (),
    sensitivity: Sequence[tuple[str, str]] = (),
    validity: Sequence[Validity] = (),
    coerce: Sequence[Callable[[dict], Mapping[str, Any]]] = (),
    doc: Mapping[str, Any] | None = None,
    extra_args: Sequence[tuple[str, Any]] = (),
    **register_kwargs: Any,
) -> Callable[..., Any]:
    """Generate and register the tool for a declared workflow.

    The generated body IS the skeleton: the ~70 lines of ``_normalize`` /
    ``_with_notes`` / ``_physical_answer`` plus the try/except tail every template
    used to repeat live in :class:`Workflow` once. The tool's SIGNATURE is
    synthesized from the declared params (plus any wire aliases the template names
    in ``extra_args``), so the model-facing schema is generated from the same
    declaration the run resolves.

    The facade is checked for HOLES first: the EngineOps five are must-fill slots,
    and registration is the last moment an unfilled one is still an authoring
    error rather than a mid-run failure.

    THE CONSTANT DOOR AND THE WIRE. A CONSTANT-door param is absent from the
    synthesized signature and from the model-facing docstring's param list, so the
    model is never offered it and cannot fill it. That is the whole of the
    enforcement, and stating its EDGE is part of stating it: the generated body
    takes ``**wire`` and the sheet is filtered by DECLARED name, not by the
    signature, so a value that arrives for a constant anyway is seated through the
    USER door with ``basis=user``. That is deliberate and is what keeps the row a
    user LEVER on the three surfaces it lives on - the form card's advanced fold,
    the ``!run`` / Tier-A all-params invocation, and the harness that drives the
    resolved sheet. The exclusion is about who the SCHEMA invites, and the schema
    invites the user, never the model.
    """
    from trid3nt_server.tools import register_tool

    _refuse_incomplete_facade(facade)
    workflow = facade(metadata=metadata, params=params, plan=plan, data=data,
                      answer=answer, provenance=provenance,
                      sensitivity=sensitivity, validity=validity, coerce=coerce)

    async def _run(**wire: Any) -> Any:
        return await workflow.run(wire)

    _run.__name__ = workflow.name
    _run.__qualname__ = workflow.name
    _run.__module__ = getattr(plan, "__module__", __name__)
    sig, annotations = _wire_signature(params, extra_args, data)
    _run.__signature__ = sig  # type: ignore[attr-defined]
    _run.__annotations__ = dict(annotations)
    _run.workflow = workflow  # type: ignore[attr-defined]
    if doc:
        from .docstring import render_docstring

        # The prose sheet describes THIS wire, so it is rendered from the params
        # the signature actually carries. A template declares `params=PARAMS` and
        # the factory narrows it; documenting a constant the schema does not offer
        # would be the docstring inviting a call the tool cannot take.
        doc = {**doc, "params": _wire_params(params),
               **_context_doc(data, doc.get("controls", ()))}
        _run.__doc__ = render_docstring(**doc)
        _run.routing_doc = render_docstring(**doc, view="routing")  # type: ignore[attr-defined]

    register_kwargs.setdefault("read_only_hint", False)
    register_kwargs.setdefault("open_world_hint", False)
    register_kwargs.setdefault("destructive_hint", False)
    register_kwargs.setdefault("idempotent_hint", False)
    return register_tool(metadata, **register_kwargs)(_run)


def _refuse_incomplete_facade(facade: type[Workflow]) -> None:
    """A facade with an unrealized operation never reaches a caller as a run failure.

    The design contract calls the EngineOps five must-fill slots and promises the
    library refuses to register a template that leaves one empty; this is that
    refusal. Without it the hole surfaces mid-run - after the geocode, the fetches
    and possibly the solve - as a bare ``NotImplementedError`` flattened into an
    ``<ENGINE>_INTERNAL_ERROR``, which tells the reader a runtime broke when in
    fact the declaration was never complete.
    """
    if not (isinstance(facade, type) and issubclass(facade, EngineOps)):
        raise FacadeIncompleteError(
            f"{facade!r} is not an EngineOps facade; register_workflow takes the "
            "facade CLASS (e.g. TelemacWorkflow).")
    unfilled = [op for op in EngineOps.MUST_FILL
                if getattr(facade, op, None) is getattr(EngineOps, op)]
    if unfilled:
        raise FacadeIncompleteError(
            f"{facade.__name__} realizes no {unfilled} - the EngineOps five are "
            "must-fill. Implement them on the facade, or register against one that "
            "does.")


def _context_doc(data: Sequence[DataDecl],
                 controls: Sequence[tuple[str, str]]) -> dict[str, Any]:
    """The docstring's ``context`` rows, and the ``controls`` with the slots taken out.

    A producer-less slot is documented in ONE place. Its SHAPE comes from the
    declaration, which is the only party that knows it; whatever prose the template
    wrote about the same wire argument is appended and its control row drops, so
    the model reads one description of one argument instead of two that can drift.
    """
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

    Two exclusions, for two different reasons. ``wire=False`` marks a value a
    COERCION resolves out of other wire args, so sending it would be sending the
    same thing twice. A CONSTANT-door param is excluded because the door is a
    BINDING AUTHORITY contract: a constant is non-question physics, nobody asks
    for it, and a schema that offers one invites exactly the invented physics the
    doors exist to prevent. Both the synthesized signature and the generated
    docstring read this function, so the schema and the prose cannot drift apart.
    """
    return tuple(prm for prm in params
                 if prm.wire and prm.door != doors.CONSTANT)


def _wire_signature(params: Sequence[Param], extra: Sequence[tuple[str, Any]],
                    data: Sequence[DataDecl] = ()) -> tuple[inspect.Signature, dict]:
    """The generated tool's signature: declared params, context slots, aliases, controls.

    Every argument is keyword-with-default: the doors supply what the caller omits,
    so a workflow argument is never positionally required. A ``**`` absorber keeps
    an unknown key from dead-ending a call the doors could still answer.

    CONSTANT-door params are NOT here. The door is a BINDING AUTHORITY contract,
    not documentation: a constant is non-question physics, so the model has no
    business supplying it, and a schema that offers it invites exactly the invented
    physics the doors exist to prevent. The row keeps its full life on the
    ``ParamSheet`` - it is form-editable and shows under the card's advanced fold -
    and a template that decides a particular constant DOES deserve model access
    re-doors it in one line. ``wire=False`` is the other, orthogonal exclusion: a
    value a coercion resolves out of other wire args.
    """
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
