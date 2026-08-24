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
    ParamRefLeakedError,
    RenderSourceMissingError,
    StepFailedError,
)
from .ledger import LedgerRecord, StepLedger, invocation_key
from .params import Param, ResolvedParams
from .plan import ChartSpec, Gate, ParamRef, Plan, Ref, RenderSpec, RunMode, Step
from .resolver import provenance_entries, rederive_revised, reseat_revised
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
    validate_plan(plan, declared_params, data, sheet=params)

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
    first_step = next((n.index for n in nodes if not isinstance(n.step, Gate)), None)
    reviewed = any(isinstance(n.step, Gate) and n.step.kind == "form" for n in nodes)
    try:
        for node in nodes:
            if isinstance(node.step, Gate):
                revision = await _run_gate(node.step, env.params, declared_params,
                                           out.entries, input_mode=input_mode,
                                           tool_name=plan.name)
                if revision is not None:
                    await _reseat_after_gate(env, revision, plan, input_mode, out)
                    ledger = env.ledger
                continue
            if node.index == first_step and not reviewed:
                # Law 9 fires before the FIRST step, not the first CONSEQUENTIAL
                # one: an invented physics value poisons the prep work as surely as
                # the solve, and a plan that tags nothing consequential would
                # otherwise skip the floor entirely.
                _refuse_invented_physics(out.entries, plan.name, input_mode)
            if node.index == produce_at:
                await _produce_independent_data(env)
            if node.step.consequential:
                _refuse_missing_required(env.params, plan.name)
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
        await env.ledger.complete()
    finally:
        reset_domain(token)
    # The terminal leak guard: a ParamRef in what the caller receives is a
    # declaration that escaped binding, never data. One scan over all three, so
    # the cycle guard dedupes the value that is also a step result.
    _refuse_leaked_param_refs(
        {"value": out.value, "results": out.results, "entries": out.entries},
        f"the result of plan {plan.name!r}")
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


async def _reseat_after_gate(env: _Env, revision: "_Revision", plan: Plan,
                             input_mode: str | None, out: RunResult) -> None:
    """Adopt an approved revision: new sheet, stale data evicted, ledger re-keyed."""
    env.params, out.entries = revision.params, revision.entries
    _evict_revised_data(env, revision.changed)
    # The attempt under the OLD key belongs to a run that continued somewhere
    # else. Leaving it behind orphans a document nobody can ever resume from, and
    # its records were computed from the very values the review replaced.
    if env.ledger is not None:
        await env.ledger.clear()
    # The approved sheet is a DIFFERENT invocation: re-key so a replay can only
    # ever come from an attempt at these values. Reaping the old key is what makes
    # that a MOVE rather than a fork - including its `data:` records.
    env.ledger = await StepLedger.load(
        invocation_key(plan.name, env.params.values_dict(), input_mode=input_mode),
        plan.name)


def _evict_revised_data(env: _Env, changed: Sequence[str]) -> None:
    """Drop artifacts produced from params the review changed - and their dependents.

    A producer's kwargs carry the ``ParamRef``/``Ref`` reads it makes, so "did this
    artifact consume a revised value" is a question the plan value can answer. One
    fetched before the gate against the pre-review sheet is stale by construction;
    keeping it would run the approved params over the un-approved world.
    """
    stale = _data_consuming(env.data, changed)
    evicted = sorted(n for n in stale if env.artifacts.pop(n, None) is not None)
    if evicted:
        logger.info("input review revised %s; evicting produced data %s so it is "
                    "re-produced against the approved sheet",
                    sorted(changed), evicted)


def _data_consuming(data: Mapping[str, DataDecl],
                    changed: Sequence[str]) -> set[str]:
    """Every declared Data that reads a changed param, transitively through Data."""
    revised = set(changed)
    stale: set[str] = set()
    for _ in range(len(data) + 1):
        grew = False
        for name, decl in data.items():
            if name in stale:
                continue
            kwargs = dict(decl.producer.kwargs)
            if any(r.name in revised for r in _param_refs(kwargs)) or \
                    any(r.root in revised or r.root in stale for r in _refs(kwargs)):
                stale.add(name)
                grew = True
        if not grew:
            break
    return stale


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
    label = _data_step_label(decl.name)
    kwargs = await _bind(dict(producer.kwargs), env, label)
    if producer.ladder_rungs:
        kwargs.setdefault("fallback", tuple(producer.ladder_rungs))
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
            kwargs = await _bind(dict(node.step.kwargs), env, node.label)
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


#: One code for law 9 whether the refusal came from a declared form gate or from
#: the gateless floor below, so callers route on the reason and not on the shape of
#: the plan that hit it.
_PHYSICS_INPUT_REQUIRED = "PHYSICS_INPUT_REQUIRED"


@dataclass(frozen=True, slots=True)
class _Revision:
    """What a form gate's approved edits changed: the sheet, its provenance, the names."""

    params: ResolvedParams
    entries: list[SyntheticInput]
    #: Every row the revision moved - the user's own edits AND the derived rows
    #: that re-derived because of them. This is what dependent data is evicted on.
    changed: tuple[str, ...]


async def _run_gate(gate: Gate, params: ResolvedParams, declared: Sequence[Param],
                    entries: list[SyntheticInput], *, input_mode: str | None,
                    tool_name: str) -> _Revision | None:
    """Run one declared gate. Returns the REVISED sheet when the user edited it.

    What was approved is what runs: the form gate's outcome carries the user's
    edits, and they are re-seated through the resolver (declared bounds still
    apply) so the steps after the gate read the approved values, not the ones the
    sheet held when the plan value was built. Derivations then re-run over the
    approved sheet, so a derived row never contradicts the value it derives from.
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
        # The gate refuses for TWO reasons, and callers route on them differently:
        # law 9 (a physics value nobody approved) shares its code with the gateless
        # floor, so a refusal reads the same whether a form card was declared.
        law_nine = str(outcome.cancel_reason or "").startswith(
            _PHYSICS_INPUT_REQUIRED)
        raise GateRefusedError(
            f"{tool_name} {outcome.cancel_reason or 'input review not approved'}; "
            "the plan did not run.",
            error_code=_PHYSICS_INPUT_REQUIRED if law_nine else "INPUT_REVIEW_CANCELLED",
        )
    revised, changed = reseat_revised(declared, params, outcome.params or {})
    undeclared = sorted(set(outcome.params or {}) - {p.name for p in declared})
    if undeclared:
        logger.warning("%s: the input review revised %s, which this workflow "
                       "declares no param for; those edits cannot be seated",
                       tool_name, undeclared)
    if not changed:
        return None
    revised, rederived, conflicts = await rederive_revised(declared, revised, changed)
    for note in conflicts:
        logger.info("%s: %s", tool_name, note)
    logger.info("%s: input review revised %s; re-seated through the GATE door%s",
                tool_name, changed,
                f"; re-derived {rederived}" if rederived else "")
    return _Revision(params=revised, entries=provenance_entries(revised, declared),
                     changed=tuple(changed) + tuple(rederived))


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

    The floor is NOT weaker in ``user_gated`` mode. It refuses in auto mode, and it
    refuses in user_gated mode when there is NO EMITTER - a headless direct call
    has no session to present the default on, so "someone will approve it" is
    false. Only a live user_gated session is exempt, because there the card the
    user is looking at is what owns the approval. This mirrors
    ``gate_input_review``'s own two arms exactly; a gateless plan must not be the
    softer path.
    """
    headless = resolve_input_gate_mode(input_mode) != "auto"
    if headless and current_emitter() is not None:
        return
    reason = physics_refusal_reason(tool_name, entries, no_session=headless)
    if reason:
        raise GateRefusedError(reason, error_code=_PHYSICS_INPUT_REQUIRED)


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


async def _bind(kwargs: dict[str, Any], env: _Env, label: str) -> dict[str, Any]:
    """Substitute every declared plan value, inside the typed error family.

    Binding is real work over author-supplied containers - a namedtuple kwarg, a
    set whose bound members are unhashable - so it fails like a step fails and must
    be reported like one. A raw ``TypeError`` escaping here would bypass the
    envelope every other plan fault arrives in.
    """
    try:
        bound = {k: await _bind_value(v, env) for k, v in kwargs.items()}
    except asyncio.CancelledError:
        raise
    except DeclarativeError:
        raise
    except Exception as exc:  # noqa: BLE001 - re-raised typed, cause preserved
        if getattr(exc, "retryable", False):
            raise
        raise StepFailedError(
            f"step {label!r}: its declared arguments could not be bound: {exc}",
            error_code=getattr(exc, "error_code", None) or "STEP_ARGS_UNBINDABLE",
            step=label, cause=exc,
        ) from exc
    _refuse_leaked_param_refs(bound, f"the arguments of {label!r}")
    return bound


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
    # sets and frozensets included: the VALIDATOR walks them, so a ref an author
    # put in one is a declared read the binder has to honor or the two disagree
    # about what the plan says.
    if isinstance(value, (list, tuple, set, frozenset)):
        return _rebuild(value, [await _bind_value(v, env) for v in value])
    return value


def _rebuild(original: Any, items: list[Any]) -> Any:
    """Put bound members back into the container the author declared.

    ``type(original)(items)`` is wrong for a namedtuple, whose fields are
    positional, so that one is rebuilt through ``_make``. A subclass whose
    constructor takes something else entirely still raises - and ``_bind`` turns
    that into a typed plan error rather than letting it escape raw.
    """
    if isinstance(original, tuple) and hasattr(original, "_make"):
        return original._make(items)          # a namedtuple keeps its field names
    return type(original)(items)


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
    yield from _declared_reads(value, Ref)


def _param_refs(value: Any) -> Iterable[ParamRef]:
    yield from _declared_reads(value, ParamRef)


def _declared_reads(value: Any, kind: type) -> Iterable[Any]:
    if isinstance(value, kind):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _declared_reads(v, kind)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for v in value:
            yield from _declared_reads(v, kind)


#: How many nodes the leak scan walks before it stops looking. A leaked ref is a
#: DECLARATION that escaped binding, so it sits in the argument/result shape an
#: author wrote - never buried under a million-element numeric array. The bound is
#: what keeps the guard off the critical path of a large payload.
_LEAK_SCAN_BUDGET = 50_000


def _refuse_leaked_param_refs(value: Any, where: str) -> None:
    """Refuse an unsubstituted ``ParamRef`` before it becomes data.

    The interpreter is the ONLY thing that substitutes a ref, so one that reaches a
    runner's arguments, a persisted ledger record or the returned result means a
    declaration escaped binding. That is always a bug - a ref is a description of a
    read, and a description written to disk or handed to a solver is a lie about a
    number. Loud and typed beats ``ParamRef('reach_km')`` in a layer title.
    """
    hit = _find_param_ref(value, set(), [_LEAK_SCAN_BUDGET], "")
    if hit is None:
        return
    path, ref = hit
    raise ParamRefLeakedError(
        f"ParamRef({ref.name!r}) reached {where} at {path} without being bound. A "
        "plan value describes a read; only the interpreter turns it into a number. "
        "Pass the ref through a step kwarg (which the binder walks) rather than "
        "storing it on an object or building it into a value by hand."
    )


def _find_param_ref(value: Any, seen: set[int], budget: list[int],
                    path: str) -> tuple[str, ParamRef] | None:
    """Depth-first hunt for an unbound ref; returns where it sits, or ``None``."""
    if budget[0] <= 0:
        return None
    budget[0] -= 1
    if isinstance(value, ParamRef):
        return path or "<root>", value
    if value is None or isinstance(value, (str, bytes, bool, int, float)):
        return None
    if id(value) in seen:
        return None
    seen.add(id(value))
    if isinstance(value, Mapping):
        items: Iterable[tuple[str, Any]] = ((f"[{k!r}]", v) for k, v in value.items())
    elif isinstance(value, (list, tuple, set, frozenset)):
        items = ((f"[{i}]", v) for i, v in enumerate(value))
    else:
        # Object attributes, where they are CHEAP to read: a __dict__ is a plain
        # dict. A __slots__ object is skipped rather than introspected - the guard
        # is a floor, not a deep-object crawler.
        attrs = getattr(value, "__dict__", None)
        if not isinstance(attrs, dict):
            return None
        items = ((f".{k}", v) for k, v in attrs.items())
    for suffix, item in items:
        hit = _find_param_ref(item, seen, budget, f"{path}{suffix}")
        if hit is not None:
            return hit
    return None


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
    _refuse_leaked_param_refs(value, f"the ledger record for {label!r}")
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
