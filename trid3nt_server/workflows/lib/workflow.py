"""The workflow SKELETON: the template method every declared workflow runs on.

A template file declares a workflow; :class:`Workflow` IS one. The skeleton owns
everything that never varies between questions - the stage sequence, the gate
mechanics, the chart scaffolding, the emission seam, solve supervision, ledger +
resume, provenance, the leak guard and the registration factory - and the engine
facade (:class:`EngineOps`, realized as ``TelemacWorkflow`` and friends) realizes
exactly five operations.

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
from typing import Any, Callable, Mapping, Sequence

from .data import DataDecl
from .errors import DeclarativeError
from .params import Param
from .plan import Plan, Ref, Step
from .resolver import merge_provenance, resolve_params
from .interpreter import RunResult, interpret

__all__ = ["DataRefs", "EngineOps", "Workflow", "WireArgsError", "register_workflow"]

logger = logging.getLogger("trid3nt_server.workflows.lib.workflow")


class WireArgsError(DeclarativeError):
    """The wire arguments cannot be coerced into a sheet the workflow can run."""

    error_code = "WIRE_ARGS_INVALID"


class DataRefs:
    """``d`` inside ``plan(p, d, ops)``: ``d.rivers`` IS ``Ref("rivers")``.

    Naming a Data that was never declared is refused HERE, while the plan value is
    being built, rather than surfacing as a run-time ``REF_UNRESOLVED`` after the
    geocode has already run.
    """

    __slots__ = ("_names",)

    def __init__(self, data: Sequence[DataDecl]) -> None:
        object.__setattr__(self, "_names", tuple(d.name for d in data))

    def __getattr__(self, name: str) -> Ref:
        names = object.__getattribute__(self, "_names")
        if name not in names:
            raise WireArgsError(
                f"the plan reads Data {name!r}, which this workflow does not declare "
                f"(declared: {sorted(names)})."
            )
        return Ref(name)


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

    def acquire_domain(self, **slots: Any) -> tuple[Step, ...]:
        """The steps that establish the modeled world and its resolved state."""
        raise NotImplementedError(f"{type(self).__name__} realizes no acquire_domain.")

    def build_mesh(self, domain: Any, policy: Any) -> Any:
        """The mesh, from an acquired domain and an engine-neutral :class:`MeshPolicy`.

        FROZEN interface: BYO-authored meshes, the shared generation front and the
        mesh gate all arrive behind it without the declaration changing.
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


class Workflow(EngineOps):
    """The universal skeleton. A template declares; this runs.

    Stage sequence (``plan.STAGES``): acquire -> prep -> mesh -> gates -> author ->
    solve -> post -> publish. The declared plan expresses the first six; the
    skeleton's own body is post + publish, plus the normalize/resolve/interpret
    spine in front of them.

    HOOKS have SILENT defaults: an unfilled hook does nothing, and no engine
    subtype ever restates one. ABSTRACT SLOTS must be filled: the physics the
    template declares and the :class:`EngineOps` five.
    """

    def __init__(self, *, metadata: Any, params: Sequence[Param],
                 plan: Callable[..., Any], data: Sequence[DataDecl] = (),
                 answer: Sequence[str] = (), provenance: Sequence[str] = (),
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
        self.answer_provenance = tuple(
            (row, f"{row}_note") if isinstance(row, str) else tuple(row)
            for row in provenance)
        self.coercions = tuple(coerce)
        self.error_prefix = str(getattr(metadata, "engine", "") or "workflow").upper()

    # -- hooks: silent defaults ------------------------------------------- #

    def checks(self, result: Any, run: RunResult) -> tuple[str, ...]:
        """Validation checks over the finished result, as NOTES the caller narrates.

        Silent default: no checks. A check reports; it never retracts a solved run.
        """
        return ()

    def context_layers(self, result: Any, run: RunResult) -> tuple[Any, ...]:
        """Extra sensor/context layers to surface beside the primary product.

        Silent default: none. The steps that fetch inputs already emit their own.
        """
        return ()

    # -- the spine --------------------------------------------------------- #

    def build_plan(self, p: Any) -> Plan:
        """The declared plan value, named and engined by the WORKFLOW, not restated."""
        nodes = self.plan_decl(p, DataRefs(self.data), self)
        if isinstance(nodes, Plan):
            return nodes
        return Plan(name=self.name, engine=self.engine or None,
                    steps=tuple(nodes) if isinstance(nodes, (list, tuple)) else (nodes,))

    async def run(self, wire: Mapping[str, Any]) -> Any:
        """The absorbed tool body: normalize, resolve, interpret, post, publish."""
        supplied, err = self._normalize(dict(wire))
        if err is not None:
            return err
        input_mode = wire.get("input_mode")
        try:
            p = await resolve_params(self.params, supplied)
            run = await interpret(
                self.build_plan(p), p, self.params, self.data,
                input_mode=input_mode, resume=not bool(wire.get("restart_clean")),
            )
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
        return await self._publish(run)

    # -- normalize --------------------------------------------------------- #

    def _normalize(self, args: dict[str, Any]) -> tuple[dict[str, Any], dict | None]:
        """Coerce the wire args into the door-1 sheet through the declared coercions."""
        try:
            for coercion in self.coercions:
                args.update(coercion(args) or {})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - every coercion refuses typed
            code = getattr(exc, "error_code", None) or f"{self.error_prefix}_PARAMS_INVALID"
            return {}, self._error(code, exc)
        declared = {prm.name for prm in self.params}
        return {k: v for k, v in args.items()
                if k in declared and v is not None}, None

    def _error(self, code: str, exc: BaseException) -> dict[str, Any]:
        """The failure, plus whatever auxiliary products the run also lost on the way."""
        notes = getattr(exc, "__notes__", ()) or ()
        return {"status": "error", "error_code": code,
                "error_message": " ".join([str(exc), *notes])}

    # -- post + publish ---------------------------------------------------- #

    async def _publish(self, run: RunResult) -> Any:
        result = run.value
        notes = list(run.notes) + [n for n in self.checks(result, run) if n]
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

        await self._emit_context_layers(result, run)
        metrics = self.answer(result)
        await self._persist(self._run_id(result, run), run.charts, metrics)
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

    @staticmethod
    def _run_id(result: Any, run: RunResult) -> str | None:
        """The solve's run prefix, from the layer or from the solve step itself."""
        return (getattr(result, "run_id", None)
                or (run.results.get("solve") or {}).get("run_id"))

    @staticmethod
    async def _persist(run_id: str | None, charts: Mapping[str, Any],
                       metrics: Mapping[str, Any]) -> None:
        from trid3nt_server.workflows.shared.run_products import persist_run_products

        await persist_run_products(run_id, charts=charts, metrics=metrics)

    async def _emit_context_layers(self, result: Any, run: RunResult) -> None:
        layers = tuple(self.context_layers(result, run))
        if not layers:
            return
        from trid3nt_server.emission.layer_uri_emit import publish_input_layer
        from trid3nt_server.emission.pipeline_emitter import current_emitter

        emitter = current_emitter()
        if emitter is None:
            return
        for layer in layers:
            try:
                await publish_input_layer(emitter, layer)
            except Exception as exc:  # noqa: BLE001 - a context layer never kills a run
                logger.warning("%s: context layer not surfaced (%s)", self.name, exc)


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
    provenance: Sequence[str] = (),
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
    """
    from trid3nt_contracts.tool_registry import AtomicToolMetadata  # noqa: F401
    from trid3nt_server.tools import register_tool

    workflow = facade(metadata=metadata, params=params, plan=plan, data=data,
                      answer=answer, provenance=provenance, coerce=coerce)

    async def _run(**wire: Any) -> Any:
        return await workflow.run(wire)

    _run.__name__ = workflow.name
    _run.__qualname__ = workflow.name
    _run.__module__ = getattr(plan, "__module__", __name__)
    sig, annotations = _wire_signature(params, extra_args)
    _run.__signature__ = sig  # type: ignore[attr-defined]
    _run.__annotations__ = dict(annotations)
    _run.workflow = workflow  # type: ignore[attr-defined]
    if doc:
        from .docstring import render_docstring

        _run.__doc__ = render_docstring(**doc)
        _run.routing_doc = render_docstring(**doc, view="routing")  # type: ignore[attr-defined]

    register_kwargs.setdefault("read_only_hint", False)
    register_kwargs.setdefault("open_world_hint", False)
    register_kwargs.setdefault("destructive_hint", False)
    register_kwargs.setdefault("idempotent_hint", False)
    return register_tool(metadata, **register_kwargs)(_run)


def _wire_signature(params: Sequence[Param],
                    extra: Sequence[tuple[str, Any]]) -> tuple[inspect.Signature, dict]:
    """The generated tool's signature: declared params, wire aliases, controls.

    Every argument is keyword-with-default: the doors supply what the caller omits,
    so a workflow argument is never positionally required. A ``**`` absorber keeps
    an unknown key from dead-ending a call the doors could still answer.
    """
    entries: list[tuple[str, Any, Any]] = [
        (prm.name, prm.wire_type | None, None) for prm in params if prm.wire
    ]
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
