"""The interpreter: it walks the plan. Plans never run themselves.

One execution NODE per step body, per declared render and per declared chart, so
the ledger can replay an expensive solve while a cheap chart re-executes.
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib
import inspect
import logging
import os
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Iterable, Mapping, Sequence

from trid3nt_contracts.common import SyntheticInput

from trid3nt_server.emission.pipeline_emitter import (
    begin_substeps,
    current_emitter,
    emit_chart_payloads,
    substep,
)
from trid3nt_server.gates.draw_input import gate_draw_input
from trid3nt_server.gates.input_review import (
    gate_input_review,
    physics_refusal_reason,
    resolve_input_gate_mode,
)

from .data import AuthoredProducer, CoversAOI, DataDecl
from .domain import Domain, bind_domain, current_domain, domain_from_result, reset_domain
from .errors import (
    SuppliedCoverageError,
    DeclarativeError,
    GateRefusedError,
    LeakScanTruncated,
    ParamRefLeakedError,
    RenderSourceMissingError,
    StepFailedError,
)
from .form import build_param_sheet
from .ledger import LedgerRecord, StepLedger, invocation_key
from .params import Param, ResolvedParams
from .plan import (
    ChartSpec,
    Gate,
    ParamRef,
    Plan,
    Ref,
    RunMode,
    Step,
    StyleSpec,
    When,
    declared_reads,
)
from .resolver import provenance_entries, rederive_revised, reseat_revised
from .validate import validate_plan

__all__ = ["PlanNode", "RunResult", "expand_plan", "interpret"]

logger = logging.getLogger("trid3nt_server.workflows.lib.interpreter")


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
    #: The sheet the run ACTUALLY RAN ON - the caller's own sheet once a form gate
    #: has revised it. A caller that narrates from the sheet it passed in would
    #: report the values the user replaced, while the solver used the approved
    #: ones: the same what-was-approved-is-what-ran promise, on the way out.
    params: ResolvedParams | None = None
    #: The chart SPECS this run built, by declared chart name. The spec IS the
    #: product, so the caller can persist the run's own chart rather than leaving
    #: a verifier to rebuild one from the scalars and hope it matches.
    charts: dict[str, Any] = field(default_factory=dict)
    #: One record per node this run completed, REPLAYED ones included. The ledger
    #: tombstones itself at completion, so these are gone from it the moment the
    #: plan ends; a derivation of this run reads them from the snapshot the
    #: publish stage writes out of here. Replayed records carry forward unchanged,
    #: which is what lets a grandchild inherit work its parent never re-executed.
    records: list[LedgerRecord] = field(default_factory=list)
    data_records: list[LedgerRecord] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class PlanNode:
    """One ledger-tracked execution unit.

    ``guards`` are the indices of the ``When`` nodes whose bodies enclose it. Every
    declared node is numbered, guarded or not, so an index means the same thing
    whichever way the branches fall - which is what lets the ledger replay a run
    that took a different branch than the attempt before it.
    """

    index: int
    label: str
    runner: str
    kind: str
    step: Step
    spec: Any = None
    guards: tuple[int, ...] = ()


async def interpret(
    plan: Plan,
    params: ResolvedParams,
    declared_params: Sequence[Param],
    data: Sequence[DataDecl] = (),
    *,
    input_mode: str | None = None,
    domain: Domain | None = None,
    resume: bool = True,
    supplied: Mapping[str, Any] | None = None,
) -> RunResult:
    """Validate, then walk the plan. The only place a declared workflow executes."""
    validate_plan(plan, declared_params, data)

    entries = provenance_entries(params, declared_params)
    key = invocation_key(plan.name, params.values_dict(), input_mode=input_mode)
    ledger = await StepLedger.load(key, plan.name)
    if not resume:
        await ledger.clear()

    nodes = expand_plan(plan)
    emitter = current_emitter()
    begin_substeps(emitter, len(nodes))

    env = _Env(params=params, data={d.name: d for d in data}, results={},
               input_mode=input_mode, ledger=ledger, resume=resume, supplied=dict(supplied or {}))
    out = RunResult(value=None, entries=entries, params=params)
    token = bind_domain(domain)
    final_index = _final_recordable_index(nodes)
    first_step = next((n.index for n in nodes if n.kind == "step"), None)
    reviewed = any(n.kind == "gate" and n.step.kind == "form" for n in nodes)
    self_reviewed = any(n.kind != "gate" and n.step.self_gating for n in nodes)
    #: Which guarded branches fired, by the ``When`` node's index. A node whose
    #: guard is absent or False is SKIPPED - and so is everything it would have
    #: pulled, which is what makes an unfired branch cost no fetch.
    taken: dict[int, bool] = {}
    try:
        for node in nodes:
            if any(not taken.get(g, False) for g in node.guards):
                continue
            if node.kind == "when":
                taken[node.index] = bool(await _bind_value(node.spec, env))
                logger.info("plan %s branch %s -> %s", plan.name, node.label,
                            taken[node.index])
                continue
            if node.kind == "gate":
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
                _refuse_invented_physics(out.entries, plan.name, input_mode,
                                         self_reviewed=self_reviewed)
            if node.step.consequential:
                _refuse_missing_required(env.params, plan.name)
            cached = ledger.replay_for(node.index, node.label) if resume else None
            if cached is not None and await _artifacts_live(cached):
                value = _rehydrate(cached)
                if value is not _UNREPLAYABLE:
                    _adopt(env, node, value, out, replayed=True, record=cached)
                    out.records.append(cached)
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
            record = _record(node, value)
            out.records.append(record)
            await ledger.record(record, final=node.index == final_index)
        out.domain = current_domain()
        out.charts = dict(env.charts)
        out.data_records = list(env.data_records)
        # An unfilled context slot is LABELLED, never silent: the run answered a
        # slightly different question than one that had the layer, and the reader
        # is the only one who can decide whether that matters.
        for name in env.absences:
            out.notes.append(
                f"the optional {name!r} context layer was not supplied, so the run "
                "modelled the domain without it")
        await env.ledger.complete()
    finally:
        reset_domain(token)
    # The terminal leak guard: a ParamRef in what the caller receives is a
    # declaration that escaped binding, never data. Three surfaces, three budgets,
    # one shared cycle guard - so the value that is also a step result is walked
    # once, and a large value cannot leave the entries unscanned.
    _refuse_leaked_param_refs(
        {"value": out.value, "results": out.results, "entries": out.entries},
        f"the result of plan {plan.name!r}")
    return out


def _final_recordable_index(nodes: Sequence[PlanNode]) -> int | None:
    """The LAST node whose completion is ledgered - gates and branches leave none."""
    return max((n.index for n in nodes if n.kind not in ("gate", "when")),
               default=None)


def _carry_notes(exc: BaseException, notes: Sequence[str]) -> None:
    """Attach what the run could not produce to the failure that ends it.

    Auxiliary misses are collected on the ``RunResult``, which a raising step
    never returns - so without this the narration would report the failure and
    silently drop the products the user was also promised.
    """
    for note in notes:
        exc.add_note(f"also missing from this run: {note}")


def _note_aux_failure(out: RunResult, plan_name: str, node: PlanNode,
                      exc: BaseException) -> None:
    """An AUXILIARY node (chart/render) never kills the run - it says what is missing.

    The primary result already exists; retracting a 27-minute solve because a
    chart builder threw would be the failure-retracts-something anti-pattern.
    """
    kind = "chart" if node.kind == "chart" else "style"
    logger.warning("plan %s: %s node %r FAILED (%s); the run's primary result stands",
                   plan_name, kind, node.label, exc, exc_info=True)
    out.notes.append(f"the {kind} {node.label!r} could not be produced: {exc}")


def expand_plan(plan: Plan) -> tuple[PlanNode, ...]:
    """Number EVERY declared node, guarded ones included, in declaration order."""
    nodes: list[PlanNode] = []
    _expand_into(nodes, plan.steps, ())
    return tuple(nodes)


def _expand_into(nodes: list[PlanNode], declared: tuple[Any, ...],
                 guards: tuple[int, ...]) -> None:
    for node in declared:
        i = len(nodes)
        if isinstance(node, When):
            nodes.append(PlanNode(i, node.label, "declarative.when", "when",
                               _WHEN_STEP, node.condition, guards))
            _expand_into(nodes, node.body, guards + (i,))
            continue
        if isinstance(node, Gate):
            nodes.append(PlanNode(i, node.label, node.runner, "gate", node,
                               guards=guards))
            continue
        nodes.append(PlanNode(i, node.label, node.runner, "step", node, guards=guards))
        for spec in node.styles:
            nodes.append(PlanNode(len(nodes), f"{node.label}.style", node.runner,
                               "style", node, spec, guards))
        for spec in node.charts:
            nodes.append(PlanNode(len(nodes), f"{node.label}.chart:{spec.name}",
                               spec.builder_path, "chart", node, spec, guards))


#: A ``When`` node carries no work of its own; ``PlanNode.step`` is typed as a Step
#: and this stands in so the branch marker fits the same list.
_WHEN_STEP = Step(runner="declarative.when")


@dataclass
class _Env:
    params: ResolvedParams
    data: dict[str, DataDecl]
    results: dict[str, Any]
    input_mode: str | None = None
    ledger: StepLedger | None = None
    resume: bool = True
    artifacts: dict[str, Any] = field(default_factory=dict)
    charts: dict[str, Any] = field(default_factory=dict)
    #: Artifacts SUPPLIED rather than produced - a layer handle, a file uri, a
    #: gate's answer. What satisfies a producer-less ``Data`` slot.
    supplied: dict[str, Any] = field(default_factory=dict)
    #: Absences worth narrating: an optional Data nothing satisfied.
    absences: list[str] = field(default_factory=list)
    #: One record per produced Data, replayed ones included - the Data half of what
    #: a derivation of this run inherits.
    data_records: list[LedgerRecord] = field(default_factory=list)


async def _reseat_after_gate(env: _Env, revision: "_Revision", plan: Plan,
                             input_mode: str | None, out: RunResult) -> None:
    """Adopt an approved revision: new sheet, stale data evicted, ledger re-keyed."""
    env.params, out.entries, out.params = (revision.params, revision.entries,
                                           revision.params)
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
            kwargs = dict(decl.producer_kwargs)
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
    """Satisfy one declared artifact, ON DEMAND - when a step that reads it runs.

    Demand-pulled rather than fetched up front, which is what makes a branch that
    does not fire cost nothing: the producer behind a ``When``-guarded consumer is
    never reached.
    """
    handed_in = env.supplied.get(decl.name)
    if handed_in is not None:
        _validate_supplied(decl, handed_in, decl.supplied_validate)
        return handed_in
    producer = decl.producer
    if producer is None:
        # A producer-less slot: nothing was handed in, and naming a default
        # fetcher for it would be this library inventing the source.
        if decl.is_optional:
            env.absences.append(decl.name)
            logger.info("data %s is an optional slot nothing satisfied; the run "
                        "proceeds without it", decl.name)
            return None
        raise StepFailedError(
            f"Data {decl.name!r} is a producer-less slot and nothing satisfied it: "
            "supply a layer, a file uri, or declare it .optional().",
            error_code="DATA_SLOT_UNSATISFIED", step=_data_step_label(decl.name),
        )
    if isinstance(producer, AuthoredProducer) and producer.supplied_uri:
        _validate_supplied(decl, producer.supplied_uri, producer.supplied_validate)
        return producer.supplied_uri
    cached = env.ledger.replay_data(decl.name) if (env.ledger and env.resume) else None
    if cached is not None and await _artifacts_live(cached):
        value = _rehydrate(cached)
        if value is not _UNREPLAYABLE:
            env.data_records.append(cached)
            logger.info("data %s REPLAYED from ledger", decl.name)
            return value
    label = _data_step_label(decl.name)
    kwargs = await _bind(dict(producer.kwargs), env, label)
    if producer.ladder_rungs:
        kwargs.setdefault("fallback", tuple(producer.ladder_rungs))
    if producer.temporal is not None:
        # The declared transform travels TO the producer, which is the only party
        # that knows the payload's quantity class and native cadence. The library
        # owns the mechanism; the interpreter never reshapes a payload it cannot
        # read.
        kwargs.setdefault("temporal", producer.temporal)
    async with substep(current_emitter(), producer.runner.rsplit(".", 1)[-1]):
        value = await _call_runner(producer.runner, kwargs, label)
    record = _record_for(decl.name, producer.runner, value)
    env.data_records.append(dataclasses.replace(
        record, index=-1, node=_data_step_label(decl.name)))
    if env.ledger is not None:
        await env.ledger.record_data(decl.name, record)
    return value


def _validate_supplied(decl: DataDecl, supplied: Any, validate: Any) -> None:
    """What a supplied artifact is checked for before the run adopts it.

    Two checks and no third: the slot's declared SHAPE against the artifact's
    class, and - under ``CoversAOI`` - that a domain with an extent is bound at
    all, so an artifact is never adopted against no modelled world. Whether the
    artifact's own extent COVERS that domain is not checked; see
    :class:`~trid3nt_server.workflows.lib.data._CoversAOI` for what that costs.
    """
    decl.refuse_wrong_shape(supplied)
    if validate is not CoversAOI:
        return
    dom = current_domain()
    if dom is None or dom.bbox is None:
        raise SuppliedCoverageError(
            f"the artifact supplied for {decl.name!r} cannot be checked against the "
            "modelled domain: no domain is bound. Resolve the AOI before supplying one."
        )


async def _run_node(node: PlanNode, env: _Env, emitter: Any) -> Any:
    async with substep(emitter, node.label):
        if node.kind == "step":
            kwargs = await _bind(dict(node.step.kwargs), env, node.label)
            return await _call_runner(node.runner, kwargs, node.label)
        if node.kind == "chart":
            return await _run_chart(node, env)
        return await _run_style(node, env)


async def _call_runner(runner: str, kwargs: dict[str, Any], label: str) -> Any:
    """Call a declared runner, converting whatever it raises into the typed family."""
    return await _call_fn(_load(runner), kwargs, label)


async def _call_fn(fn: Any, kwargs: dict[str, Any], label: str) -> Any:
    """Call a resolved callable inside the typed error family."""
    try:
        return await _call(fn, kwargs)
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


async def _run_chart(node: PlanNode, env: _Env) -> Any:
    spec: ChartSpec = node.spec
    source = env.results.get(node.step.name or node.step.label)
    payload = await _call_fn(
        spec.builder, {"result": source, "params": env.params.values_view()},
        node.label)
    if not payload:
        raise StepFailedError(
            f"chart {spec.name!r}: the builder produced no spec from the result.",
            error_code="CHART_NOT_BUILT", step=node.label,
        )
    # The PAYLOAD is what goes over the wire, not the small dict this node returns,
    # so it is the surface a ref in a chart title would leak through.
    _refuse_leaked_param_refs({"payload": payload},
                              f"the chart payload for {spec.name!r}")
    env.charts[spec.name] = payload
    await emit_chart_payloads(payload)
    return {"chart": spec.name, "emitted": True}


async def _run_style(node: PlanNode, env: _Env) -> Any:
    """Re-emit a step's layer under its declared style OVERRIDE.

    Emission already happened - the step's own publisher put the layer on the map
    through the one seam - so this touches the DISPLAY FACE only. NO layer behind
    the result is the step's defect and says so: a step that declared a style and
    produced nothing to paint did not do what it claimed.
    """
    spec: StyleSpec = node.spec
    source = env.results.get(node.step.name or node.step.label)
    uri = getattr(source, "uri", None)
    layer_id = getattr(source, "layer_id", None)
    if not uri or not layer_id or not str(uri).startswith(("s3://", "gs://")):
        raise RenderSourceMissingError(
            f"step {node.step.label!r} declares a style override but produced no "
            f"object-store layer to paint (uri={uri!r}, layer_id={layer_id!r}); "
            "there is no map layer behind this result."
        )
    from trid3nt_server.emission.restyle import apply_style

    try:
        applied = await asyncio.to_thread(
            apply_style, layer_uri=uri, layer_id=layer_id, preset=spec.preset,
            colormap=spec.colormap, policy=spec.policy, value_range=spec.range,
            transform=spec.transform, clip=spec.clip,
            fallback_preset=getattr(source, "style_preset", None))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - a styling miss is auxiliary, not the result
        raise StepFailedError(
            f"style override on {uri} failed: {exc}",
            error_code=getattr(exc, "error_code", None) or "RENDER_STYLE_FAILED",
            step=node.label, cause=exc,
        ) from exc
    return {"style": applied.preset, "legend": applied.legend_note()}


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
    """Run one declared gate. Returns the REVISED sheet when the user answered it.

    What was approved is what runs: the form gate's outcome carries the user's
    edits, and they are re-seated through the resolver (declared bounds still
    apply) so the steps after the gate read the approved values, not the ones the
    sheet held when the plan value was built. Derivations then re-run over the
    approved sheet, so a derived row never contradicts the value it derives from.
    A drawn value takes exactly the same path - the card differs, the seating does
    not.
    """
    if gate.kind == "draw":
        return await _run_draw_gate(gate, params, declared, input_mode=input_mode,
                                    tool_name=tool_name)
    outcome = await gate_input_review(
        tool_name=tool_name, mode=input_mode, entries=entries,
        params=params.values_dict(),
        param_sheet=build_param_sheet(tool_name, gate.prompt, declared, params),
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
    undeclared = sorted(set(outcome.params or {}) - {p.name for p in declared})
    if undeclared:
        logger.warning("%s: the input review revised %s, which this workflow "
                       "declares no param for; those edits cannot be seated",
                       tool_name, undeclared)
    return await _seat(declared, params, outcome.params or {},
                       note="revised at input review", tool_name=tool_name,
                       what="input review")


async def _run_draw_gate(gate: Gate, params: ResolvedParams,
                         declared: Sequence[Param], *, input_mode: str | None,
                         tool_name: str) -> _Revision | None:
    """Ask for ONE param on the canvas; ``None`` when nothing needed asking.

    A value already on the sheet answers the gate - the user passed it, so there
    is nothing to draw.

    Otherwise the two modes differ on what an OPTIONAL param means. ``auto``
    never asks: an optional param's ``derived_when_absent`` describes its own
    absence, and a required one refuses typed rather than being invented.
    ``user_gated`` ASKS in both cases, because declaring the gate is the request
    to ask and the whole point of the mode is that the user gets to answer. What
    differs is the DECLINE: an optional param falls back to its declared absence,
    a required one refuses.
    """
    row = params.row(gate.param or "")
    if row is not None and row.value is not None:
        return None
    target = next((p for p in declared if p.name == gate.param), None)
    optional = target is not None and target.optional
    if resolve_input_gate_mode(input_mode) != "user_gated":
        if optional:
            return None
        raise GateRefusedError(
            f"{tool_name} needs {gate.param!r}: {gate.prompt or 'draw it on the canvas'}. "
            "In auto mode it must be passed explicitly - it is never invented."
        )
    outcome = await gate_draw_input(
        tool_name=tool_name, param=gate.param or "", geometry=gate.geometry or "point",
        prompt=gate.prompt,
    )
    if not outcome.drawn:
        if optional:
            logger.info("%s: the draw gate for %r was not answered (%s); the "
                        "declared absence stands", tool_name, gate.param,
                        outcome.reason)
            return None
        raise GateRefusedError(
            f"{tool_name} needs {gate.param!r} drawn on the canvas "
            f"({gate.prompt or gate.geometry}), and {outcome.reason}. It is not "
            "invented - supply the value explicitly or draw it and re-run."
        )
    return await _seat(declared, params, {gate.param: outcome.value},
                       note="drawn on the canvas", tool_name=tool_name,
                       what="draw gate")


async def _seat(declared: Sequence[Param], params: ResolvedParams,
                answered: Mapping[str, Any], *, note: str, tool_name: str,
                what: str) -> _Revision | None:
    """Seat a gate's answer through the GATE door and re-derive what reads it."""
    revised, changed = reseat_revised(declared, params, answered, note=note)
    if not changed:
        return None
    revised, rederived, conflicts = await rederive_revised(declared, revised, changed)
    for conflict in conflicts:
        logger.info("%s: %s", tool_name, conflict)
    logger.info("%s: the %s set %s; re-seated through the GATE door%s",
                tool_name, what, changed,
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
                             input_mode: str | None, *,
                             self_reviewed: bool) -> None:
    """Law 9, for a plan whose declared rows no form card will present.

    A plan that declares a ``FormGate`` refuses through the gate; one that does not
    (because its step reviews its own inputs) still may not run a physics value
    that fell back to an invented default with nobody to approve it.

    The exemption keys on a REVIEW SURFACE, not on a session. Approval needs a card
    to happen on, and only two things put one in front of the user: the plan's own
    ``FormGate`` (whose caller skips this floor entirely) or a ``self_gating`` step
    that runs its own input review. A live session with neither is a session that
    will never be asked, so it refuses like a headless one - an emitter is where a
    card COULD be shown, never evidence that one was.

    So: refuse in auto mode; refuse in user_gated mode with no emitter (nobody to
    approve); refuse in user_gated mode with an emitter but no review surface
    (nothing to approve on). Step aside only for a live user_gated session whose
    plan actually reviews these values.
    """
    headless = resolve_input_gate_mode(input_mode) != "auto"
    live = current_emitter() is not None
    if headless and live and self_reviewed:
        return
    reason = physics_refusal_reason(
        tool_name, entries,
        no_session=headless and not live,
        no_review_surface=headless and live,
    )
    if reason:
        raise GateRefusedError(reason, error_code=_PHYSICS_INPUT_REQUIRED)


def _adopt(env: _Env, node: PlanNode, value: Any, out: RunResult, *, replayed: bool,
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
    # Per-kwarg surfaces: one huge argument must not spend the budget the others
    # need.
    _refuse_leaked_param_refs(bound, f"the arguments of {label!r}")
    return bound


async def _bind_value(value: Any, env: _Env) -> Any:
    if value is RunMode:
        return env.input_mode
    if isinstance(value, ParamRef):
        # LATE binding: the sheet a gate may have revised, not the one the plan
        # value was built from.
        return env.params.value_of(value.name)
    if isinstance(value, Ref):
        return await _deref(value, env)
    # Any Mapping, not dicts alone: a deep-frozen binding block is a
    # MappingProxyType, which the VALIDATOR walks, so a ref inside one is a
    # declared read the binder has to honor or the two disagree about the plan. A
    # bound mapping comes back as a plain dict because a read-only proxy has no
    # constructor to rebuild it with, and bound kwargs are consumed as ``**kwargs``.
    if isinstance(value, Mapping):
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
        base = env.params.value_of(ref.root)
    else:
        raise StepFailedError(f"Ref({ref.path!r}) resolves to nothing at run time.",
                              error_code="REF_UNRESOLVED")
    for part in ref.tail:
        base = base.get(part) if isinstance(base, Mapping) else getattr(base, part, None)
    return base


def _refs(value: Any) -> Iterable[Ref]:
    yield from declared_reads(value, Ref)


def _param_refs(value: Any) -> Iterable[ParamRef]:
    yield from declared_reads(value, ParamRef)


#: How many nodes the leak scan walks PER SURFACE before it stops looking. A leaked
#: ref is a DECLARATION that escaped binding, so it sits in the argument/result
#: shape an author wrote - never buried under a million-element numeric array. The
#: bound is what keeps the guard off the critical path of a large payload; running
#: out of it is reported, never read as "clean".
_LEAK_SCAN_BUDGET = 50_000


@dataclass
class _Scan:
    """One leak sweep: the cycle guard, the remaining budget, whether it ran out.

    ``seen`` is shared across the surfaces of one sweep so a value that is both the
    plan's result and a step result is walked once; ``budget`` is NOT, so a large
    surface cannot starve the ones scanned after it.
    """

    seen: set[int]
    budget: int
    truncated: bool = False


def _refuse_leaked_param_refs(surfaces: Mapping[str, Any], where: str) -> None:
    """Refuse an unsubstituted ``ParamRef`` before it becomes data.

    The interpreter is the ONLY thing that substitutes a ref, so one that reaches a
    runner's arguments, a persisted ledger record or the returned result means a
    declaration escaped binding. That is always a bug - a ref is a description of a
    read, and a description written to disk or handed to a solver is a lie about a
    number. Loud and typed beats ``ParamRef('reach_km')`` in a layer title.

    Each named surface gets its OWN budget, and exhausting one is WARNED rather
    than passed: a scan that stopped looking has not found the surface clean.
    """
    seen: set[int] = set()
    for name, surface in surfaces.items():
        scan = _Scan(seen=seen, budget=_LEAK_SCAN_BUDGET)
        hit = _find_param_ref(surface, scan, f"[{name!r}]")
        # Warn BEFORE the refusal below: a truncated surface is a fact about this
        # sweep, and a leak found on a later surface must not swallow it.
        if scan.truncated:
            _warn_scan_truncated(name, where)
        if hit is not None:
            path, ref = hit
            raise ParamRefLeakedError(
                f"ParamRef({ref.name!r}) reached {where} at {path} without being "
                "bound. A plan value describes a read; only the interpreter turns it "
                "into a number. Pass the ref through a step kwarg (which the binder "
                "walks) rather than storing it on an object or building it into a "
                "value by hand."
            )


def _warn_scan_truncated(unscanned: str, where: str) -> None:
    message = (
        f"the ParamRef leak scan of {where} ran out of its {_LEAK_SCAN_BUDGET}-node "
        f"budget on {unscanned!r}: that surface is only PARTLY checked, so an unbound "
        "ref could still be sitting in it. A scan that stopped looking is not a clean "
        "scan. Shrink what the plan carries through this surface, or raise the budget."
    )
    logger.warning(message)
    warnings.warn(message, LeakScanTruncated, stacklevel=3)


def _find_param_ref(value: Any, scan: _Scan,
                    path: str) -> tuple[str, ParamRef] | None:
    """Depth-first hunt for an unbound ref; returns where it sits, or ``None``."""
    if scan.budget <= 0:
        scan.truncated = True
        return None
    scan.budget -= 1
    if isinstance(value, ParamRef):
        return path or "<root>", value
    if value is None or isinstance(value, (str, bytes, bool, int, float)):
        return None
    if id(value) in scan.seen:
        return None
    scan.seen.add(id(value))
    if isinstance(value, Mapping):
        items: Iterable[tuple[str, Any]] = ((f"[{k!r}]", v) for k, v in value.items())
    elif isinstance(value, (list, tuple, set, frozenset)):
        items = ((f"[{i}]", v) for i, v in enumerate(value))
    else:
        attrs = _object_attrs(value)
        if attrs is None:
            return None
        items = ((f".{k}", v) for k, v in attrs)
    for suffix, item in items:
        hit = _find_param_ref(item, scan, f"{path}{suffix}")
        if hit is not None:
            return hit
    return None


def _object_attrs(obj: Any) -> list[tuple[str, Any]] | None:
    """The attributes of a plain object: ``__dict__``, ``__slots__`` and fields.

    A frozen+slots dataclass has NO ``__dict__``, and it is the house idiom for a
    value type - so a guard that read only ``__dict__`` could not see a ref held on
    one, and the ref reached the wire as ``str()`` text through a serializer's
    ``default=``.
    """
    pairs: list[tuple[str, Any]] = []
    attrs = getattr(obj, "__dict__", None)
    if isinstance(attrs, dict):
        pairs.extend(attrs.items())
    taken = {name for name, _ in pairs}
    for name in _declared_attribute_names(type(obj)):
        if name in taken:
            continue
        try:
            pairs.append((name, getattr(obj, name)))
        except AttributeError:      # an unset slot holds nothing to leak
            continue
        taken.add(name)
    return pairs or None


@lru_cache(maxsize=1024)
def _declared_attribute_names(cls: type) -> tuple[str, ...]:
    """Every ``__slots__`` name up the MRO, plus a dataclass's own field names."""
    names: list[str] = []
    for klass in getattr(cls, "__mro__", ()):
        declared = klass.__dict__.get("__slots__")
        if isinstance(declared, str):
            declared = (declared,)
        for name in declared or ():
            if name not in ("__dict__", "__weakref__") and name not in names:
                names.append(name)
    if dataclasses.is_dataclass(cls):
        names.extend(f.name for f in dataclasses.fields(cls) if f.name not in names)
    return tuple(names)


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


def _record(node: PlanNode, value: Any) -> LedgerRecord:
    return _record_for(node.label, node.runner, value, index=node.index)


def _record_for(label: str, runner: str, value: Any, *, index: int = 0) -> LedgerRecord:
    _refuse_leaked_param_refs({"result": value},
                              f"the ledger record for {label!r}")
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
            from trid3nt_server.workflows.solver.solver import _get_s3_client

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
