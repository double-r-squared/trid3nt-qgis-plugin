"""The interpreter: it walks the plan. Plans never run themselves.

One execution NODE per step body, per declared render and per declared chart, so
the ledger can replay an expensive solve while a cheap chart re-executes.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from trid3nt_contracts.common import SyntheticInput

from trid3nt_server.emission.pipeline_emitter import (
    begin_substeps,
    current_emitter,
    emit_chart_payloads,
    substep,
)
from trid3nt_server.gates.input_review import (
    gate_input_review,
    physics_refusal_reason,
    resolve_input_gate_mode,
)

from .data import AuthoredProducer, CoversAOI, DataDecl
from .domain import Domain, bind_domain, current_domain, domain_from_result, reset_domain
from .errors import (
    ByoCoverageError,
    DeclarativeError,
    GateNotSupportedError,
    GateRefusedError,
    RenderSourceMissingError,
    StepFailedError,
)
from .ledger import LedgerRecord, StepLedger, invocation_key
from .params import Param, ResolvedParams
from .plan import ChartSpec, Gate, ParamRef, Plan, Ref, RenderSpec, RunMode, Step
from .resolver import provenance_entries, reseat_revised
from .validate import validate_plan

__all__ = ["RunResult", "interpret"]

logger = logging.getLogger("trid3nt_server.declarative.interpret")


@dataclass
class RunResult:
    """What a plan produced: the terminal result plus the run's provenance rows.

    ``notes`` carries what the run could NOT produce - an auxiliary chart or
    render that failed while the primary result stood. The caller narrates them.
    """

    value: Any
    results: dict[str, Any] = field(default_factory=dict)
    entries: list[SyntheticInput] = field(default_factory=list)
    replayed: list[str] = field(default_factory=list)
    executed: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    domain: Domain | None = None


@dataclass(frozen=True, slots=True)
class _Node:
    """One ledger-tracked execution unit."""

    index: int
    label: str
    runner: str
    kind: str
    step: Step
    spec: Any = None


async def interpret(
    plan: Plan,
    params: ResolvedParams,
    declared_params: Sequence[Param],
    data: Sequence[DataDecl] = (),
    *,
    input_mode: str | None = None,
    domain: Domain | None = None,
    resume: bool = True,
) -> RunResult:
    """Validate, then walk the plan. The only place a declared workflow executes."""
    validate_plan(plan, declared_params, data)

    entries = provenance_entries(params, declared_params)
    key = invocation_key(plan.name, params.values_dict(), input_mode=input_mode)
    ledger = await StepLedger.load(key, plan.name)
    if not resume:
        await ledger.clear()

    nodes = _expand(plan)
    emitter = current_emitter()
    begin_substeps(emitter, len(nodes))

    env = _Env(params=params, data={d.name: d for d in data}, results={},
               input_mode=input_mode, ledger=ledger, resume=resume)
    out = RunResult(value=None, entries=entries)
    token = bind_domain(domain)
    produce_at = _eager_data_index(nodes)
    final_index = _final_recordable_index(nodes)
    reviewed = any(isinstance(n.step, Gate) and n.step.kind == "form" for n in nodes)
    try:
        for node in nodes:
            if node.index == produce_at:
                await _produce_independent_data(env)
            if isinstance(node.step, Gate):
                revision = await _run_gate(node.step, env.params, declared_params,
                                           out.entries, input_mode=input_mode,
                                           tool_name=plan.name)
                if revision is not None:
                    env.params, out.entries = revision
                    # The approved sheet is a DIFFERENT invocation: re-key so a
                    # replay can only ever come from an attempt at these values.
                    ledger = env.ledger = await StepLedger.load(
                        invocation_key(plan.name, env.params.values_dict(),
                                       input_mode=input_mode),
                        plan.name)
                continue
            if node.step.consequential:
                _refuse_missing_required(env.params, plan.name)
                if not reviewed:
                    _refuse_invented_physics(out.entries, plan.name, input_mode)
            cached = ledger.replay_for(node.index, node.label) if resume else None
            if cached is not None and await _artifacts_live(cached):
                value = _rehydrate(cached)
                if value is not _UNREPLAYABLE:
                    _adopt(env, node, value, out, replayed=True, record=cached)
                    logger.info("plan %s node %d %s REPLAYED from ledger",
                                plan.name, node.index, node.label)
                    continue
            try:
                value = await _run_node(node, env, emitter)
            except Exception as exc:  # noqa: BLE001 - re-raised for the primary result
                if node.kind == "step" or isinstance(exc, RenderSourceMissingError):
                    _carry_notes(exc, out.notes)
                    raise
                _note_aux_failure(out, plan.name, node, exc)
                continue
            # Adopt BEFORE recording: a domain-rebinding step must record the
            # domain it LEAVES, not the one it started under.
            _adopt(env, node, value, out, replayed=False)
            await ledger.record(_record(node, value), final=node.index == final_index)
        out.domain = current_domain()
        await ledger.complete()
    finally:
        reset_domain(token)
    return out


def _final_recordable_index(nodes: Sequence[_Node]) -> int | None:
    """The LAST node whose completion is ledgered - gates leave no record."""
    return max((n.index for n in nodes if not isinstance(n.step, Gate)), default=None)


def _carry_notes(exc: BaseException, notes: Sequence[str]) -> None:
    """Attach what the run could not produce to the failure that ends it.

    Auxiliary misses are collected on the ``RunResult``, which a raising step
    never returns - so without this the narration would report the failure and
    silently drop the products the user was also promised.
    """
    for note in notes:
        exc.add_note(f"also missing from this run: {note}")


def _eager_data_index(nodes: Sequence[_Node]) -> int | None:
    """Where the independent-Data batch fires: the first node AFTER the last gate.

    A producer that ran before a gate would have fetched against params the gate
    exists to change. Anything a pre-gate step needs is still produced lazily on
    its first ``Ref``.
    """
    last_gate = max((n.index for n in nodes if isinstance(n.step, Gate)), default=-1)
    return next((n.index for n in nodes if n.index > last_gate), None)


def _note_aux_failure(out: RunResult, plan_name: str, node: _Node,
                      exc: BaseException) -> None:
    """An AUXILIARY node (chart/render) never kills the run - it says what is missing.

    The primary result already exists; retracting a 27-minute solve because a
    chart builder threw would be the failure-retracts-something anti-pattern.
    """
    kind = "chart" if node.kind == "chart" else "render"
    logger.warning("plan %s: %s node %r FAILED (%s); the run's primary result stands",
                   plan_name, kind, node.label, exc, exc_info=True)
    out.notes.append(f"the {kind} {node.label!r} could not be produced: {exc}")


def _expand(plan: Plan) -> tuple[_Node, ...]:
    nodes: list[_Node] = []
    for step in plan.flat():
        i = len(nodes)
        if isinstance(step, Gate):
            nodes.append(_Node(i, step.label, step.runner, "gate", step))
            continue
        nodes.append(_Node(i, step.label, step.runner, "step", step))
        for spec in step.renders:
            nodes.append(_Node(len(nodes), f"{step.label}.render:{spec.preset}",
                               step.runner, "render", step, spec))
        for spec in step.charts:
            nodes.append(_Node(len(nodes), f"{step.label}.chart:{spec.name}",
                               spec.builder, "chart", step, spec))
    return tuple(nodes)


@dataclass
class _Env:
    params: ResolvedParams
    data: dict[str, DataDecl]
    results: dict[str, Any]
    input_mode: str | None = None
    ledger: StepLedger | None = None
    resume: bool = True
    artifacts: dict[str, Any] = field(default_factory=dict)


async def _produce_independent_data(env: _Env) -> None:
    """The independent Data set - producers that Ref no other Data, run together.

    Everything else is produced lazily on first ``Ref``. Skipped until a domain is
    bound, because a spatial producer with no AOI would fetch the wrong world.
    """
    if current_domain() is None:
        return
    ready = [d for d in env.data.values()
             if d.name not in env.artifacts
             and not any(r.root in env.data for r in _refs(dict(d.producer.kwargs)))]
    if not ready:
        return
    produced = await asyncio.gather(*(_produce(env, decl) for decl in ready))
    for decl, value in zip(ready, produced):
        env.artifacts[decl.name] = value


def _data_step_label(name: str) -> str:
    return f"data:{name}"


async def _produce(env: _Env, decl: DataDecl) -> Any:
    producer = decl.producer
    if isinstance(producer, AuthoredProducer) and producer.byo_uri:
        _validate_byo(decl, producer.byo_uri, producer.byo_validate)
        return producer.byo_uri
    cached = env.ledger.replay_data(decl.name) if (env.ledger and env.resume) else None
    if cached is not None and await _artifacts_live(cached):
        value = _rehydrate(cached)
        if value is not _UNREPLAYABLE:
            logger.info("data %s REPLAYED from ledger", decl.name)
            return value
    kwargs = await _bind(dict(producer.kwargs), env)
    if producer.ladder_rungs:
        kwargs.setdefault("fallback", tuple(producer.ladder_rungs))
    label = _data_step_label(decl.name)
    async with substep(current_emitter(), producer.runner.rsplit(".", 1)[-1]):
        # The eager batch runs outside any node's body, so a producer that raises
        # would otherwise escape the typed family entirely.
        value = await _call_runner(producer.runner, kwargs, label)
    if env.ledger is not None:
        await env.ledger.record_data(
            decl.name, _record_for(decl.name, producer.runner, value))
    return value


def _validate_byo(decl: DataDecl, uri: str, validate: Any) -> None:
    if validate is not CoversAOI:
        return
    dom = current_domain()
    if dom is None or dom.bbox is None:
        raise ByoCoverageError(
            f"BYO artifact for {decl.name!r} cannot be coverage-validated: no domain "
            "is bound. Resolve the AOI before supplying a byo artifact."
        )


async def _run_node(node: _Node, env: _Env, emitter: Any) -> Any:
    async with substep(emitter, node.label):
        if node.kind == "step":
            kwargs = await _bind(dict(node.step.kwargs), env)
            return await _call_runner(node.runner, kwargs, node.label)
        if node.kind == "chart":
            return await _run_chart(node, env)
        return await _run_render(node, env)


async def _call_runner(runner: str, kwargs: dict[str, Any], label: str) -> Any:
    """Call a declared runner, converting whatever it raises into the typed family."""
    try:
        return await _call(_load(runner), kwargs)
    except asyncio.CancelledError:
        raise
    except DeclarativeError:
        raise
    except Exception as exc:  # noqa: BLE001 - re-raised typed, cause preserved
        if getattr(exc, "retryable", False):
            # A RETRYABLE typed error is a GATE, not a failure: the adapter
            # harvests its .suggestions off the raised exception so the model can
            # retry with corrected args. Flattening it into an envelope destroys
            # that channel.
            raise
        raise StepFailedError(
            f"step {label!r} failed: {exc}",
            error_code=getattr(exc, "error_code", None) or "STEP_FAILED",
            step=label, cause=exc,
        ) from exc


async def _run_chart(node: _Node, env: _Env) -> Any:
    spec: ChartSpec = node.spec
    source = env.results.get(node.step.name or node.step.label)
    payload = await _call_runner(
        spec.builder, {"result": source, "params": env.params.values_view()},
        node.label)
    if not payload:
        raise StepFailedError(
            f"chart {spec.name!r}: the builder produced no spec from the result.",
            error_code="CHART_NOT_BUILT", step=node.label,
        )
    await emit_chart_payloads(payload)
    return {"chart": spec.name, "emitted": True}


async def _run_render(node: _Node, env: _Env) -> Any:
    """Style a step's raster. NO raster is the step's defect; bad styling is a note."""
    spec: RenderSpec = node.spec
    source = env.results.get(node.step.name or node.step.label)
    uri = getattr(source, "uri", None)
    layer_id = getattr(source, "layer_id", None)
    if not uri or not layer_id or not str(uri).startswith(("s3://", "gs://")):
        raise RenderSourceMissingError(
            f"step {node.step.label!r} declares a {spec.preset!r} render but produced "
            f"no object-store raster to style (uri={uri!r}, layer_id={layer_id!r}); "
            "there is no map layer behind this result."
        )
    from trid3nt_server.data.publish_layer import publish_layer

    try:
        published = await asyncio.to_thread(
            publish_layer, layer_uri=uri, layer_id=layer_id, style_preset=spec.preset
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - a styling miss is auxiliary, not the result
        raise StepFailedError(
            f"render {spec.preset!r}: styling {uri} failed: {exc}",
            error_code=getattr(exc, "error_code", None) or "RENDER_STYLE_FAILED",
            step=node.label, cause=exc,
        ) from exc
    return {"render": spec.preset, "published": True, "uri": published}


async def _run_gate(gate: Gate, params: ResolvedParams, declared: Sequence[Param],
                    entries: list[SyntheticInput], *, input_mode: str | None,
                    tool_name: str
                    ) -> tuple[ResolvedParams, list[SyntheticInput]] | None:
    """Run one declared gate. Returns the REVISED sheet when the user edited it.

    What was approved is what runs: the form gate's outcome carries the user's
    edits, and they are re-seated through the resolver (declared bounds still
    apply) so the steps after the gate read the approved values, not the ones the
    sheet held when the plan value was built.
    """
    mode = resolve_input_gate_mode(input_mode)
    if gate.kind == "draw":
        row = params.row(gate.param or "")
        if row is not None and row.value is not None:
            return None
        target = next((p for p in declared if p.name == gate.param), None)
        if target is not None and target.optional:
            return None
        if mode == "user_gated":
            raise GateNotSupportedError(
                f"{tool_name}: the draw gate for {gate.param!r} ({gate.prompt or gate.geometry}) "
                "needs the plugin draw card, which lands in wave 2 of the declarative "
                "campaign. Pass the value explicitly, or re-run in auto mode."
            )
        raise GateRefusedError(
            f"{tool_name} needs {gate.param!r}: {gate.prompt or 'draw it on the canvas'}. "
            "In auto mode it must be passed explicitly - it is never invented."
        )
    outcome = await gate_input_review(
        tool_name=tool_name, mode=input_mode, entries=entries,
        params=params.values_dict(),
    )
    if outcome.cancelled or not outcome.proceed:
        raise GateRefusedError(
            f"{tool_name} {outcome.cancel_reason or 'input review not approved'}; "
            "the plan did not run.",
            error_code="INPUT_REVIEW_CANCELLED",
        )
    revised, changed = reseat_revised(declared, params, outcome.params or {})
    undeclared = sorted(set(outcome.params or {}) - {p.name for p in declared})
    if undeclared:
        logger.warning("%s: the input review revised %s, which this workflow "
                       "declares no param for; those edits cannot be seated",
                       tool_name, undeclared)
    if not changed:
        return None
    logger.info("%s: input review revised %s; re-seated through the GATE door",
                tool_name, changed)
    return revised, provenance_entries(revised, declared)


def _refuse_missing_required(params: ResolvedParams, tool_name: str) -> None:
    """Door 6, at the last honest moment: no gate filled these, so refuse typed."""
    missing = [r for r in params.rows() if r.required_missing]
    if not missing:
        return
    raise GateRefusedError(
        f"{tool_name} cannot run: " + "; ".join(
            f"{r.name} was not supplied and has no door to come through" for r in missing
        ) + ". Supply the values explicitly - they are never invented."
    )


def _refuse_invented_physics(entries: Sequence[SyntheticInput], tool_name: str,
                             input_mode: str | None) -> None:
    """Law 9, for a plan whose declared rows no form card will present.

    A plan that declares a ``FormGate`` refuses through the gate; one that does not
    (because its step reviews its own inputs) still may not run a physics value
    that fell back to an invented default with nobody to approve it.
    """
    if resolve_input_gate_mode(input_mode) != "auto":
        return
    reason = physics_refusal_reason(tool_name, entries)
    if reason:
        raise GateRefusedError(reason, error_code="PHYSICS_INPUT_REQUIRED")


def _adopt(env: _Env, node: _Node, value: Any, out: RunResult, *, replayed: bool,
           record: LedgerRecord | None = None) -> None:
    name = node.step.name or node.step.label
    if node.kind == "step":
        env.results[name] = value
        out.results[name] = value
        out.value = value
        if node.step.rebinds_domain:
            # On replay the RECORDED domain wins: it is what the step actually left
            # behind, rather than what re-reading its result happens to reproduce.
            refined = Domain.from_doc(record.domain) if record else None
            if refined is None:
                refined = domain_from_result(value)
            if refined is not None:
                bind_domain(refined)
    (out.replayed if replayed else out.executed).append(node.label)


async def _bind(kwargs: dict[str, Any], env: _Env) -> dict[str, Any]:
    return {k: await _bind_value(v, env) for k, v in kwargs.items()}


async def _bind_value(value: Any, env: _Env) -> Any:
    if value is RunMode:
        return env.input_mode
    if isinstance(value, ParamRef):
        # LATE binding: the sheet a gate may have revised, not the one the plan
        # value was built from.
        return env.params.get(value.name)
    if isinstance(value, Ref):
        return await _deref(value, env)
    if isinstance(value, dict):
        return {k: await _bind_value(v, env) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)([await _bind_value(v, env) for v in value])
    return value


async def _deref(ref: Ref, env: _Env) -> Any:
    if ref.root in env.results:
        base = env.results[ref.root]
    elif ref.root in env.artifacts:
        base = env.artifacts[ref.root]
    elif ref.root in env.data:
        base = env.artifacts[ref.root] = await _produce(env, env.data[ref.root])
    elif ref.root in env.params:
        base = env.params.get(ref.root)
    else:
        raise StepFailedError(f"Ref({ref.path!r}) resolves to nothing at run time.",
                              error_code="REF_UNRESOLVED")
    for part in ref.tail:
        base = base.get(part) if isinstance(base, Mapping) else getattr(base, part, None)
    return base


def _refs(value: Any) -> Iterable[Ref]:
    if isinstance(value, Ref):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _refs(v)
    elif isinstance(value, (list, tuple, set)):
        for v in value:
            yield from _refs(v)


def _load(dotted: str) -> Any:
    module_path, _, attr = dotted.rpartition(".")
    if not module_path:
        raise StepFailedError(f"runner {dotted!r} is not a dotted import path.",
                              error_code="RUNNER_UNRESOLVED")
    return getattr(importlib.import_module(module_path), attr)


async def _call(fn: Any, kwargs: dict[str, Any]) -> Any:
    out = fn(**kwargs)
    if inspect.isawaitable(out):
        out = await out
    return out


class _Unreplayable:
    def __repr__(self) -> str:
        return "UNREPLAYABLE"


_UNREPLAYABLE = _Unreplayable()


def _record(node: _Node, value: Any) -> LedgerRecord:
    return _record_for(node.label, node.runner, value, index=node.index)


def _record_for(label: str, runner: str, value: Any, *, index: int = 0) -> LedgerRecord:
    kind, payload, type_path = _serialize(value)
    dom = current_domain()
    return LedgerRecord(
        index=index, node=label, runner=runner,
        completed_at=datetime.now(timezone.utc).isoformat(),
        result_kind=kind, result=payload, result_type=type_path,
        artifact_uris=_artifact_uris(value),
        domain=dom.as_doc() if dom else None,
    )


#: Answers ``_artifact_state`` can give. Both non-live answers re-execute the
#: node; they differ in what they MEAN, which is what the log has to say.
_LIVE, _ABSENT, _UNREACHABLE = "live", "absent", "unreachable"


async def _artifacts_live(rec: LedgerRecord) -> bool:
    """Probe every artifact the cached record points at.

    A replay that hands back a URI whose object is gone is a dead handle wearing a
    success envelope; the node re-executes instead.
    """
    for uri in rec.artifact_uris:
        state = await asyncio.to_thread(_artifact_state, uri)
        if state == _LIVE:
            continue
        if state == _UNREACHABLE:
            logger.warning(
                "ledger record %s: the object store is UNREACHABLE for %s, so a "
                "replayable step is being re-executed because of an outage rather "
                "than because its artifact is gone", rec.node, uri)
        else:
            logger.info("ledger record %s points at an artifact that no longer "
                        "exists (%s); re-executing", rec.node, uri)
        return False
    return True


def _artifact_state(uri: str) -> str:
    """Is the cached artifact there, gone, or merely unreachable right now?

    ``s3://`` objects are probed; a local path is stat'd; anything else is taken as
    live. Never raises: a probe that cannot answer must not become a typed error
    about the RUN - it only means the node re-executes.
    """
    if uri.startswith("s3://"):
        bucket, _, key = uri[len("s3://"):].partition("/")
        if not bucket or not key:
            return _ABSENT
        try:
            from trid3nt_server.data.simulation.solver.solver import _get_s3_client

            _get_s3_client().head_object(Bucket=bucket, Key=key)
            return _LIVE
        except Exception as exc:  # noqa: BLE001 - answered, never propagated
            return _ABSENT if _is_not_found(exc) else _UNREACHABLE
    if "://" not in uri:
        return _LIVE if os.path.exists(uri) else _ABSENT
    return _LIVE


def _is_not_found(exc: BaseException) -> bool:
    """A botocore 404/NoSuchKey means GONE; every other fault means UNREACHABLE."""
    response = getattr(exc, "response", None)
    code = ""
    if isinstance(response, dict):
        code = str((response.get("Error") or {}).get("Code") or "")
        status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        if status == 404:
            return True
    return code in ("404", "NoSuchKey", "NotFound") or \
        type(exc).__name__ in ("NoSuchKey", "NotFound")


def _serialize(value: Any) -> tuple[str, Any, str | None]:
    if value is None:
        return "none", None, None
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return ("pydantic", dump(mode="json"),
                    f"{type(value).__module__}.{type(value).__name__}")
        except Exception:  # noqa: BLE001 - an undumpable model just re-executes next run
            return "opaque", None, None
    if isinstance(value, (str, int, float, bool, list, dict)):
        return "json", value, None
    return "opaque", None, None


def _rehydrate(rec: LedgerRecord) -> Any:
    if rec.result_kind == "none":
        return None
    if rec.result_kind == "json":
        return rec.result
    if rec.result_kind == "pydantic" and rec.result_type:
        try:
            return _load(rec.result_type).model_validate(rec.result)
        except Exception as exc:  # noqa: BLE001 - a stale shape re-executes, never crashes
            logger.warning("ledger record %s not rehydratable (%s); re-executing",
                           rec.node, exc)
            return _UNREPLAYABLE
    return _UNREPLAYABLE


def _artifact_uris(value: Any) -> tuple[str, ...]:
    uri = getattr(value, "uri", None)
    if isinstance(uri, str):
        return (uri,)
    if isinstance(value, dict) and isinstance(value.get("uri"), str):
        return (value["uri"],)
    return ()
