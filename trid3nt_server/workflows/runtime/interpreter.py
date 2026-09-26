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
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

from trid3nt_contracts.common import SyntheticInput

from trid3nt_server.render.layer_uri_emit import republish_input_row
from trid3nt_server.render.pipeline_emitter import (
    begin_substeps,
    current_emitter,
    emit_chart_payloads,
    substep,
)
from trid3nt_server.gates.input_review import (
    physics_refusal_reason,
    resolve_input_gate_mode,
)

from trid3nt_contracts.coverage import SourceChoice

from .domain import Domain, bind_domain, current_domain, domain_from_result, reset_domain
from .errors import (
    DeclarativeError,
    GateRefusedError,
    LeakScanTruncated,
    ParamRefLeakedError,
    StepFailedError,
    said,
)
from .journal import (bind_choices, bind_coverage, bind_notes, bind_outputs,
                      drain_choices, drain_coverage, drain_notes,
                      drain_outputs)
from .ledger import LedgerRecord, StepLedger, inputs_digest, invocation_key
from .params import Param, ResolvedParams
from .plan import (
    ChartSpec,
    Continued,
    ParamRef,
    Plan,
    Ref,
    RawKeywords,
    RunMode,
    Step,
    declared_reads,
)
from .resolver import provenance_entries

if TYPE_CHECKING:
    from .fill import _Env

__all__ = ["PlanNode", "RunResult", "expand_plan", "interpret"]

logger = logging.getLogger("trid3nt_server.workflows.runtime.interpreter")


@dataclass
class RunResult:
    """What a plan produced: the terminal result plus the run's provenance rows.

    ``notes`` carries what the run could NOT produce, for the caller to narrate."""

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
    #: Every layer the run published, as the publish stage emitted it. Carried
    #: onto the run's own record, which outlives the session that saw them.
    outputs: list[dict[str, Any]] = field(default_factory=list)
    #: One record per node this run completed, REPLAYED ones included. The ledger
    #: tombstones itself at completion, so these are gone from it the moment the
    #: plan ends.
    records: list[LedgerRecord] = field(default_factory=list)
    data_records: list[LedgerRecord] = field(default_factory=list)
    #: The RANKED LIST each matched slot was filled from, in the order the slots
    #: were produced. One object in three views: the card renders it, the tool
    #: result carries it on a tie, and the sheet stores the pick and its reason.
    choices: list[SourceChoice] = field(default_factory=list)
    #: The RAW KEYWORD floor this invocation carried. Not a Param, so it is on no
    #: param sheet - and a run that was pinned by one is not reproducible from
    #: its arguments alone unless the record carries it too.
    keywords: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PlanNode:
    """One ledger-tracked execution unit: a step, or a chart built from one."""

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
    *,
    env: "_Env",
    domain: Domain | None = None,
    resume: bool = True,
    continued: str | None = None,
    continued_mesh: Mapping[str, Any] | None = None,
    mesh_step: str = "",
) -> RunResult:
    """Walk the plan. The only place a declared workflow executes."""
    entries = provenance_entries(params, declared_params)
    key = invocation_key(plan.name, params.values_dict(),
                         input_mode=env.input_mode, continued=continued)
    ledger = await StepLedger.load(key, plan.name)
    if not resume:
        await ledger.clear()

    nodes = expand_plan(plan)
    emitter = current_emitter()
    begin_substeps(emitter, len(nodes))

    from .fill import refuse_other_mesh

    env.ledger, env.resume, env.continued = ledger, resume, continued
    out = RunResult(value=None, entries=entries, params=params,
                    keywords=dict(env.keywords))
    token = bind_domain(domain)
    notes_token = bind_notes()
    outputs_token = bind_outputs()
    choices_token = bind_choices()
    coverage_token = bind_coverage()
    final_index = _final_recordable_index(nodes)
    first_step = next((n.index for n in nodes if n.kind == "step"), None)
    self_reviewed = any(n.step.self_gating for n in nodes)
    try:
        await _ask_unread_context(env, nodes)
        for node in nodes:
            if node.index == first_step:
                # The invented-physics floor fires before the FIRST step, not the
                # first CONSEQUENTIAL one: an invented value poisons the prep work
                # as surely as the solve, and a plan that tags nothing consequential
                # would otherwise skip the floor entirely.
                _refuse_invented_physics(out.entries, plan.name, env.input_mode,
                                         self_reviewed=self_reviewed)
            if node.step.consequential:
                _refuse_missing_required(env.params, plan.name)
            # BIND FIRST: what a node would run on is what decides whether the
            # work it already did is still this run's work. The producers a
            # binding dereferences have all been produced by now, so this costs
            # a walk rather than a fetch.
            bound = (await _bind(dict(node.step.kwargs), env, node.label)
                     if node.kind == "step" else None)
            key = inputs_digest(bound)
            if mesh_step and node.step.name == mesh_step:
                refuse_other_mesh(continued_mesh or {}, key)
            cached = (ledger.replay_for(node.index, node.label, key)
                      if resume else None)
            if cached is not None and await _artifacts_live(cached):
                value = _rehydrate(cached)
                if value is not _UNREPLAYABLE:
                    _adopt(env, node, value, out, replayed=True, record=cached)
                    # The publish rides inside the node, so a replayed one
                    # never reaches it: the record puts its surface back.
                    await republish_input_row(cached.layer)
                    out.records.append(cached)
                    logger.info("plan %s node %d %s REPLAYED from ledger",
                                plan.name, node.index, node.label)
                    continue
            try:
                value = await _run_node(node, env, emitter, bound)
            except Exception as exc:  # noqa: BLE001 - re-raised for the primary result
                if node.kind == "step":
                    _carry_notes(exc, out.notes)
                    raise
                _note_aux_failure(out, plan.name, node, exc)
                continue
            # Adopt BEFORE recording: a domain-rebinding step must record the
            # domain it LEAVES, not the one it started under.
            _adopt(env, node, value, out, replayed=False)
            record = _record(node, value, key)
            out.records.append(record)
            await ledger.record(record, final=node.index == final_index)
        out.domain = current_domain()
        out.charts = dict(env.charts)
        out.data_records = list(env.data_records)
        # An unfilled context slot is LABELLED, never silent: the run answered a
        # slightly different question than one that had the layer, and the reader
        # is the only one who can decide whether that matters.
        out.notes.extend(env.absences)
        await env.ledger.complete()
    except Exception:
        # A failed terminal state is never replayable: the records a dead attempt
        # left behind were produced by whatever the code was at the time, and
        # replaying them into a re-run reports a superseded artifact as the new
        # run's answer.
        await env.ledger.complete()
        raise
    finally:
        reset_domain(token)
        # In the FINALLY so a run that failed still carries what it measured: the
        # note a step wrote on its way to the failure is often the reason for it.
        out.notes.extend(drain_notes(notes_token))
        out.outputs.extend(drain_outputs(outputs_token))
        out.choices.extend(drain_choices(choices_token))
        drain_coverage(coverage_token)
    # The terminal leak guard: a ParamRef in what the caller receives is a
    # declaration that escaped binding, never data. Three surfaces, three budgets,
    # one shared cycle guard - so the value that is also a step result is walked
    # once, and a large value cannot leave the entries unscanned.
    _refuse_leaked_param_refs(
        {"value": out.value, "results": out.results, "entries": out.entries},
        f"the result of plan {plan.name!r}")
    return out


def _final_recordable_index(nodes: Sequence[PlanNode]) -> int | None:
    """The LAST node whose completion is ledgered."""
    return max((n.index for n in nodes), default=None)


def _carry_notes(exc: BaseException, notes: Sequence[str]) -> None:
    """Attach what the run could not produce to the failure that ends it.
    A raising step never returns the ``RunResult`` the auxiliary misses collect on,
    so they travel on the exception instead."""
    for note in notes:
        exc.add_note(f"also missing from this run: {note}")


def _note_aux_failure(out: RunResult, plan_name: str, node: PlanNode,
                      exc: BaseException) -> None:
    """An AUXILIARY node (chart/render) never kills the run - it says what is missing.

    The primary result stands; a failure here never retracts it."""
    kind = "chart"
    logger.warning("plan %s: %s node %r FAILED (%s); the run's primary result stands",
                   plan_name, kind, node.label, exc, exc_info=True)
    out.notes.append(
        f"the {kind} {node.label!r} could not be produced: {said(exc)}")


def expand_plan(plan: Plan) -> tuple[PlanNode, ...]:
    """Number every declared node, in declaration order - a step, then its charts."""
    nodes: list[PlanNode] = []
    for node in plan.steps:
        nodes.append(PlanNode(len(nodes), node.label, node.runner, "step", node))
        for spec in node.charts:
            nodes.append(PlanNode(len(nodes), f"{node.label}.chart:{spec.name}",
                                  spec.builder_path, "chart", node, spec))
    return tuple(nodes)



async def _ask_unread_context(env: _Env, nodes: Sequence[PlanNode]) -> None:
    """Ask every CONTEXT row nothing in this plan reads, before the work starts.

    A context row's product is the SENTENCE - the reading the run opened beside,
    or the absence of one - so a row no step and no other row dereferences is
    still asked, and says what it found either way."""
    reads = {ref.root for node in nodes
             for value in (node.step.kwargs, node.spec)
             for ref in declared_reads(value, Ref)}
    for row in env.data.values():
        reads.update(ref.root
                     for ref in declared_reads(dict(row.producer_kwargs), Ref))
    from .fill import _produce

    for name, row in env.data.items():
        if row.is_context and name not in reads:
            env.artifacts[name] = await _produce(env, row)



async def _run_node(node: PlanNode, env: _Env, emitter: Any,
                    bound: dict[str, Any] | None) -> Any:
    async with substep(emitter, node.label):
        if node.kind == "step":
            return await _call_runner(node.runner, dict(bound or {}), node.label)
        return await _run_chart(node, env)


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
            f"step {label!r} failed: {said(exc)}",
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


#: The one code the invented-physics floor refuses under, so callers route on the
#: reason rather than on the shape of the plan that hit it.
_PHYSICS_INPUT_REQUIRED = "PHYSICS_INPUT_REQUIRED"


def _refuse_missing_required(params: ResolvedParams, tool_name: str) -> None:
    """The last honest moment: nothing filled these, so refuse typed."""
    missing = [r for r in params.rows() if r.required_missing]
    if not missing:
        return
    raise GateRefusedError(
        f"{tool_name} cannot run: " + "; ".join(
            f"{r.name} was not supplied and has no door to come through"
            for r in missing
        ) + ". Supply the values explicitly - they are never invented."
    )


def _refuse_invented_physics(entries: Sequence[SyntheticInput], tool_name: str,
                             input_mode: str | None, *,
                             self_reviewed: bool) -> None:
    """A physics value nobody approved never reaches a solve.
    The exemption keys on a REVIEW SURFACE, not on a session: only a ``self_gating``
    step puts a card in front of the user, and an emitter is not evidence of one."""
    headless = resolve_input_gate_mode(input_mode) != "auto"
    live = current_emitter() is not None
    # The only opening: a live user_gated session whose plan actually reviews these
    # values. Auto mode refuses; user_gated with no emitter has nobody to approve;
    # user_gated with an emitter but no review surface has nothing to approve on.
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
    Binding walks author-supplied containers, so it fails like a step fails: no raw
    ``TypeError`` escapes the envelope every other plan fault arrives in."""
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
            f"step {label!r}: its declared arguments could not be bound: "
            f"{said(exc)}",
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
    if value is RawKeywords:
        return dict(env.keywords)
    if value is Continued:
        return env.continued
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
    A namedtuple is rebuilt through ``_make``; a container whose constructor takes
    something else raises, and ``_bind`` types it."""
    if isinstance(original, tuple) and hasattr(original, "_make"):
        return original._make(items)          # a namedtuple keeps its field names
    return type(original)(items)


#: What a missing field reads as, distinct from a field that is present and None.
_NO_FIELD = object()


async def _deref(ref: Ref, env: _Env) -> Any:
    """Bind one declared read, REFUSING rather than yielding a missing field.
    An attribute tail naming a field the referenced thing does not define, or one
    that is there and empty, refuses here rather than binding to ``None``."""
    from .fill import _produce

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
    if base is None and ref.root in env.data:
        # A row that is WHOLLY ABSENT reads like a field that is present and
        # empty. A context row whose source held nothing states nothing, and
        # what reads it - a keyword, a composite - expands to nothing in turn.
        return None
    read = ref.root
    for part in ref.tail:
        found = (base.get(part, _NO_FIELD) if isinstance(base, Mapping)
                 else getattr(base, part, _NO_FIELD))
        if found is _NO_FIELD or found is None:
            missing = ("defines no field" if found is _NO_FIELD
                       else "carries no value for")
            raise StepFailedError(
                f"Ref({ref.path!r}) reads {part!r} off {read}, which {missing} "
                f"{part!r}. A ref to a field that is not there is refused at "
                "binding; nothing downstream receives it as an absence.",
                error_code="REF_FIELD_MISSING")
        base = found
        read = f"{read}.{part}"
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
    ``seen`` is shared across a sweep's surfaces; ``budget`` is per surface, so a
    large surface cannot starve the ones scanned after it."""

    seen: set[int]
    budget: int
    truncated: bool = False


def _refuse_leaked_param_refs(surfaces: Mapping[str, Any], where: str) -> None:
    """Refuse an unsubstituted ``ParamRef`` before it becomes data.
    Each named surface gets its own budget, and exhausting one WARNS rather than
    passing: a scan that stopped looking has not found the surface clean."""
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
    All three, because a frozen+slots dataclass has no ``__dict__`` and would
    otherwise hide a ref from the scan."""
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


def _load(runner: str) -> Any:
    """The runner a node named: a REGISTERED TOOL first, then a dotted import path.
    A name that resolves as BOTH refuses rather than letting lookup order decide
    which namespace answered."""
    from trid3nt_server.tools import TOOL_REGISTRY

    registered = TOOL_REGISTRY.get(runner)
    module_path, _, attr = runner.rpartition(".")
    if registered is not None and module_path:
        raise StepFailedError(
            f"runner {runner!r} is a registered tool AND reads as an import path; "
            "rename one of them.", error_code="RUNNER_AMBIGUOUS")
    if registered is not None:
        return registered.fn
    if not module_path:
        raise StepFailedError(
            f"runner {runner!r} is neither a registered tool nor a dotted import "
            "path.", error_code="RUNNER_UNRESOLVED")
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


def _record(node: PlanNode, value: Any, inputs_key: str = "") -> LedgerRecord:
    return _record_for(node.label, node.runner, value, index=node.index,
                       inputs_key=inputs_key)


def _record_for(label: str, runner: str, value: Any, *, index: int = 0,
                inputs_key: str = "") -> LedgerRecord:
    _refuse_leaked_param_refs({"result": value},
                              f"the ledger record for {label!r}")
    kind, payload, type_path = _serialize(value)
    dom = current_domain()
    return LedgerRecord(
        index=index, node=label, runner=runner, inputs_key=inputs_key,
        completed_at=datetime.now(timezone.utc).isoformat(),
        result_kind=kind, result=payload, result_type=type_path,
        artifact_uris=_artifact_uris(value),
        domain=dom.as_doc() if dom else None,
        layer=_input_row(value),
    )


def _input_row(value: Any) -> dict[str, Any] | None:
    """The input row a result publishes for itself, as the raster seam takes it."""
    row = getattr(value, "input_row", None)
    return dict(row()) if callable(row) else None


#: Answers ``_artifact_state`` can give. Both non-live answers re-execute the
#: node; they differ in what they MEAN, which is what the log has to say.
_LIVE, _ABSENT, _UNREACHABLE = "live", "absent", "unreachable"


async def _artifacts_live(rec: LedgerRecord) -> bool:
    """Probe every artifact the cached record points at.
    A record pointing at an object that is gone is not replayable; the node
    re-executes instead."""
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
    ``s3://`` is probed, a local path stat'd, anything else taken as live. Never
    raises: an unanswerable probe means the node re-executes, not that the run fails."""
    if uri.startswith("s3://"):
        bucket, _, key = uri[len("s3://"):].partition("/")
        if not bucket or not key:
            return _ABSENT
        try:
            from trid3nt_server import storage

            storage.client().head_object(Bucket=bucket, Key=key)
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


#: Marks a dataclass packed into an otherwise-JSON result, so the unpack knows
#: which type to hand back. A step that returns an artifact - alone or under a key
#: beside its own fields - must replay as that artifact: its consumers read it by
#: attribute, and a plain dict in its place either crashes them or degrades them
#: into reporting a fact the run never had.
_DATACLASS_TAG = "__dataclass__"


def _packable(value: Any) -> bool:
    """Whether this object states its own JSON both ways."""
    return callable(getattr(value, "to_json", None)) and \
        callable(getattr(type(value), "from_json", None))


def _pack(value: Any) -> Any:
    if _packable(value):
        return {_DATACLASS_TAG: f"{type(value).__module__}.{type(value).__name__}",
                "doc": _pack(value.to_json())}
    if isinstance(value, dict):
        return {k: _pack(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_pack(v) for v in value]
    return value


def _unpack(value: Any) -> Any:
    if isinstance(value, dict):
        tag = value.get(_DATACLASS_TAG)
        if isinstance(tag, str):
            return _load(tag).from_json(_unpack(value.get("doc")))
        return {k: _unpack(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_unpack(v) for v in value]
    return value


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
    if isinstance(value, (str, int, float, bool, list, dict)) or _packable(value):
        return "json", _pack(value), None
    return "opaque", None, None


def _rehydrate(rec: LedgerRecord) -> Any:
    if rec.result_kind == "none":
        return None
    if rec.result_kind == "json":
        try:
            return _unpack(rec.result)
        except Exception as exc:  # noqa: BLE001 - a stale shape re-executes, never crashes
            logger.warning("ledger record %s not rehydratable (%s); re-executing",
                           rec.node, exc)
            return _UNREPLAYABLE
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
