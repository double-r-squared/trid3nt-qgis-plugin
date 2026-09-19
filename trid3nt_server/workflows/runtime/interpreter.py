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
import math
import os
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Iterable, Mapping, Sequence

from trid3nt_contracts.common import SyntheticInput

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

from .data import (
    BED, DISCHARGE, DOMAIN, LEVEL, LINE, OBSERVATION, RUNS, CoversAOI, DataDecl,
    Producer)
from .match import (
    Need, dropped_from, instant, match, sources_with_coverage)
from .domain import Domain, bind_domain, current_domain, domain_from_result, reset_domain
from .errors import (
    PlanValidationError,
    SuppliedCoverageError,
    DeclarativeError,
    GateRefusedError,
    LeakScanTruncated,
    ParamRefLeakedError,
    StepFailedError,
)
from .journal import (bind_choices, bind_notes, bind_outputs, drain_choices,
                      drain_notes, drain_outputs, journal_note, slot_choice)
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
from .validate import validate_plan

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
    #: plan ends; a derivation of this run reads them from the snapshot the
    #: publish stage writes out of here. Replayed records carry forward unchanged,
    #: which is what lets a grandchild inherit work its parent never re-executed.
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
    data: Sequence[DataDecl] = (),
    *,
    input_mode: str | None = None,
    keywords: Mapping[str, Any] | None = None,
    picks: Mapping[str, str] | None = None,
    domain: Domain | None = None,
    resume: bool = True,
    supplied: Mapping[str, Any] | None = None,
    continued: str | None = None,
    window_s: float | None = None,
) -> RunResult:
    """Validate, then walk the plan. The only place a declared workflow executes."""
    validate_plan(plan, declared_params, data)

    entries = provenance_entries(params, declared_params)
    key = invocation_key(plan.name, params.values_dict(), input_mode=input_mode,
                         continued=continued)
    ledger = await StepLedger.load(key, plan.name)
    if not resume:
        await ledger.clear()

    nodes = expand_plan(plan)
    emitter = current_emitter()
    begin_substeps(emitter, len(nodes))

    env = _Env(params=params, data={d.name: d for d in data}, results={},
               input_mode=input_mode, keywords=dict(keywords or {}),
               picks={str(k): str(v) for k, v in dict(picks or {}).items()},
               ledger=ledger,
               resume=resume, supplied=dict(supplied or {}), workflow=plan.name,
               continued=continued, window_s=window_s)
    out = RunResult(value=None, entries=entries, params=params,
                    keywords=dict(env.keywords))
    token = bind_domain(domain)
    notes_token = bind_notes()
    outputs_token = bind_outputs()
    choices_token = bind_choices()
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
                _refuse_invented_physics(out.entries, plan.name, input_mode,
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
            cached = (ledger.replay_for(node.index, node.label, key)
                      if resume else None)
            if cached is not None and await _artifacts_live(cached):
                value = _rehydrate(cached)
                if value is not _UNREPLAYABLE:
                    _adopt(env, node, value, out, replayed=True, record=cached)
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
    except Exception as exc:
        # The failed attempt CARRIES what it got done, so the skeleton can record
        # the partial run (see ``Workflow.execute``) - and carries it no FURTHER.
        # A failed terminal state is never replayable: the records a dead attempt
        # left behind were produced by whatever the code was at the time, and
        # replaying them into a re-run reports a superseded artifact as the new
        # run's answer. Only a run that REACHED its end leaves work a later
        # invocation may inherit, through the snapshot a derived run seeds from.
        out.data_records = list(env.data_records)
        if isinstance(exc, DeclarativeError):
            exc.partial_run = out
        await env.ledger.complete()
        raise
    finally:
        reset_domain(token)
        # In the FINALLY so a run that failed still carries what it measured: the
        # note a step wrote on its way to the failure is often the reason for it.
        out.notes.extend(drain_notes(notes_token))
        out.outputs.extend(drain_outputs(outputs_token))
        out.choices.extend(drain_choices(choices_token))
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
    out.notes.append(f"the {kind} {node.label!r} could not be produced: {exc}")


def expand_plan(plan: Plan) -> tuple[PlanNode, ...]:
    """Number every declared node, in declaration order - a step, then its charts."""
    nodes: list[PlanNode] = []
    for node in plan.steps:
        nodes.append(PlanNode(len(nodes), node.label, node.runner, "step", node))
        for spec in node.charts:
            nodes.append(PlanNode(len(nodes), f"{node.label}.chart:{spec.name}",
                                  spec.builder_path, "chart", node, spec))
    return tuple(nodes)


@dataclass
class _Env:
    params: ResolvedParams
    data: dict[str, DataDecl]
    results: dict[str, Any]
    input_mode: str | None = None
    #: The workflow this walk belongs to - what a gate card names as the asker.
    workflow: str = ""
    #: The raw keyword floor this invocation carried, by the name the caller used.
    keywords: dict[str, Any] = field(default_factory=dict)
    #: The source the run NAMES for a slot, by slot name. It picks among the
    #: survivors of that slot's match and never past its filters.
    picks: dict[str, str] = field(default_factory=dict)
    #: The run this one CONTINUES, as the artifact its state is read out of.
    #: Empty for a run that starts from its own initial conditions.
    continued: str | None = None
    ledger: StepLedger | None = None
    resume: bool = True
    artifacts: dict[str, Any] = field(default_factory=dict)
    charts: dict[str, Any] = field(default_factory=dict)
    #: Artifacts SUPPLIED rather than produced - a layer handle, a file uri, a
    #: gate's answer. What satisfies a producer-less ``Data`` slot.
    supplied: dict[str, Any] = field(default_factory=dict)
    #: Absences worth narrating, each as the SENTENCE the run carries: an
    #: optional Data nothing satisfied, or a context row whose source was empty.
    absences: list[str] = field(default_factory=list)
    #: One record per produced Data, replayed ones included - the Data half of what
    #: a derivation of this run inherits.
    data_records: list[LedgerRecord] = field(default_factory=list)
    #: How long the solve runs, in seconds, off the deck this run writes. It is
    #: the WINDOW a matched series source has to cover, so a run longer than the
    #: record refuses rather than opening on a record that stops early.
    window_s: float | None = None


def _data_step_label(name: str) -> str:
    return f"data:{name}"


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
    for name, row in env.data.items():
        if row.is_context and name not in reads:
            env.artifacts[name] = await _produce(env, row)


async def _produce(env: _Env, decl: DataDecl) -> Any:
    """Satisfy one declared artifact, ON DEMAND - when a step that reads it runs.

    A declared artifact nothing reads costs no fetch; a CONTEXT row is asked
    all the same, because the sentence it states IS its product."""
    handed_in = env.supplied.get(decl.name)
    if handed_in is not None:
        # A MARKED producer states the check the value handed to its row is held
        # to: a table of weather has no extent, so it is not a coverage question,
        # and the row that carries the mark is what knows that.
        _validate_supplied(env, decl, handed_in,
                           decl.producer.supplied_validate if decl.is_supplied
                           else decl.supplied_validate)
        return await _ingested(env, decl, handed_in)
    producer = decl.producer
    if producer is None and decl.role == RUNS:
        # THE DOMAIN'S PRODUCER measured these where it cut the polygon between
        # two faces, and they ride on the domain it returned; a template that
        # declares the slot does not restate them. Nothing measured is not the
        # answer here - the canvas is asked next, and a closed body ends with
        # none.
        measured = await _domain_runs(env)
        if measured:
            return measured
    if producer is None and decl.role == LINE:
        # THE DOMAIN'S PRODUCER measured this beside the polygon it cut - a
        # reach's centerline rides on the artifact it returned. A body whose
        # producer measured none is asked on the canvas next, which is what a
        # profile across a lake is.
        measured = await _domain_companion(env, "centerline")
        if measured is not None:
            return measured
    if producer is None and decl.data_class:
        # A SLOT THAT STATES A NEED: the match reads every fetcher's coverage row
        # and the runtime declares the row it picked, so the pick earns a ledger
        # record and a journal line like any other producer.
        return await _matched(env, decl)
    if producer is None and decl.role:
        # A SLOT the caller did not fill and no producer answers is asked for on
        # the canvas, where the user has one. A declined drawing is not a value:
        # the slot's own refusal below is what a reader then sees.
        from trid3nt_server.inputs.slots import ask_on_canvas

        drawn = await ask_on_canvas(decl.role, tool=env.workflow,
                                    param=decl.name, input_mode=env.input_mode)
        if drawn is not None:
            return drawn
    if producer is None:
        # A producer-less slot: nothing was handed in, and naming a default
        # fetcher for it would be this library inventing the source.
        if decl.is_optional:
            env.absences.append(
                f"the optional {decl.name!r} context layer was not supplied, so "
                "the run modelled the domain without it")
            logger.info("data %s is an optional slot nothing satisfied; the run "
                        "proceeds without it", decl.name)
            return None
        raise StepFailedError(
            f"Data {decl.name!r} is a producer-less slot and nothing satisfied it: "
            "supply a layer, a file uri, or declare it .optional().",
            error_code="DATA_SLOT_UNSATISFIED", step=_data_step_label(decl.name),
        )
    if producer.supplied_uri:
        _validate_supplied(env, decl, producer.supplied_uri,
                           producer.supplied_validate)
        return await _ingested(env, decl, producer.supplied_uri)
    cached = env.ledger.replay_data(decl.name) if (env.ledger and env.resume) else None
    if cached is not None and await _artifacts_live(cached):
        value = _rehydrate(cached)
        if value is not _UNREPLAYABLE:
            env.data_records.append(cached)
            logger.info("data %s REPLAYED from ledger", decl.name)
            return await _ingested(env, decl, value, cached.runner)
    label = _data_step_label(decl.name)
    if decl.is_context:
        return await _context(env, decl, label)
    answered, value = await _walk_ladder(env, producer, label)
    record = _record_for(decl.name, answered.runner, value,
                         inputs_key=inputs_digest(answered.kwargs))
    env.data_records.append(dataclasses.replace(
        record, index=-1, node=_data_step_label(decl.name)))
    if env.ledger is not None:
        await env.ledger.record_data(decl.name, record)
    return await _ingested(env, decl, value, answered.runner)


async def _matched(env: _Env, decl: DataDecl) -> Any:
    """Fill a slot that states a NEED, through the match.

    The bed is the one slot whose physics rule the runtime owns: the measurement
    where it measured, the terrain everywhere else. Every other slot takes the
    one source its own class matched."""
    if decl.role == BED:
        return await _ingested(env, decl, await _matched_bed(env, decl))
    choice, value = await _probe(env, decl, decl.data_class, decl.name)
    if value is None:
        if decl.is_optional:
            # An OPTIONAL slot nothing measured is an absence the run states,
            # the same absence a slot nobody filled is: the refusal below is for
            # a slot the run cannot stand without.
            env.absences.append(choice.sentence)
            return None
        raise StepFailedError(choice.sentence, error_code="DATA_NEED_UNMATCHED",
                              step=_data_step_label(decl.name))
    return await _ingested(env, decl, value, choice.picked)


async def _matched_bed(env: _Env, decl: DataDecl) -> Any:
    """THE BED, stated once here: the measurement where it measured, the terrain
    under the rest.

    A measurement that arrives as soundings is gridded at the mesh's own cell
    before the merge reads it; with no measurement over this domain the terrain
    is the whole bed, which is what the sheet then says."""
    _choice, terrain = await _probe(env, decl, "terrain", f"{decl.name} terrain")
    choice, measured = await _probe(env, decl, "bathymetry",
                                    f"{decl.name} bathymetry")
    if measured is None:
        if terrain is None:
            raise StepFailedError(
                _choice.sentence, error_code="DATA_NEED_UNMATCHED",
                step=_data_step_label(decl.name))
        return terrain
    if _spec_of(choice.picked).output.layer_type == "vector":
        measured = await _produce(env, _runtime_row(
            env, f"{decl.name}_surveyed", "derive_survey_surface",
            {"points": measured, "value_field": _value_column(choice.picked,
                                                              "bathymetry"),
             "resolution_m": _mesh_m(env)}))
    if terrain is None:
        return measured
    return await _produce(env, _runtime_row(
        env, f"{decl.name}_merged", "derive_merge_rasters",
        {"primary": measured, "fallback": terrain}))


async def _probe(env: _Env, decl: DataDecl, data_class: str,
                 label: str) -> tuple[SourceChoice, Any]:
    """Match a class, then CALL the survivors in rank order -> the first answer.

    A source that held nothing over this domain is dropped and the next takes
    its turn, which is how the list a reader sees says what the world answered
    rather than what the sort preferred. ``None`` where none of them answered."""
    choice = match(await _need(env, decl, data_class, label),
                   sources_with_coverage())
    while choice.picked:
        row = _runtime_row(env, f"{label.replace(' ', '_')}_{choice.picked}",
                           choice.picked,
                           await _ask_for(env, choice.picked, decl))
        try:
            value = await _produce(env, row)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - an empty source drops a rung
            if getattr(exc, "retryable", False) or _malformed_ask(exc):
                raise
            logger.info("%s: %s held nothing (%s); the next survivor takes its "
                        "turn", label, choice.picked, exc)
            choice = dropped_from(choice, choice.picked,
                                  f"held nothing here ({exc})")
            continue
        slot_choice(choice)
        journal_note(choice.sentence)
        return choice, value
    slot_choice(choice)
    journal_note(choice.sentence)
    return choice, None


def _runtime_row(env: _Env, name: str, runner: str,
                 ask: Mapping[str, Any]) -> DataDecl:
    """One producer row the RUNTIME declares, registered so it is produced like
    a question's own."""
    row = DataDecl(name=name, producer=Producer(runner=runner, kwargs=dict(ask),
                                                row=name))
    env.data[name] = row
    return row


def _spec_of(fetcher: str) -> Any:
    from trid3nt_server.tools.fetchers._router.registration import get_spec

    return get_spec(fetcher)


def _coverage_row(fetcher: str, data_class: str) -> Any:
    """The row THIS source states about THIS class, or ``None``.

    A source serving two classes states one row each, and the row a slot reads
    is the row of the class it asked for."""
    return next((row for row in getattr(_spec_of(fetcher), "coverage", ())
                 if row.data_class == data_class), None)


def _value_column(fetcher: str, data_class: str) -> str:
    """The column a matched source publishes its measurement under.

    Off the coverage row, which names it; a slot that states a need never names
    a column of a source it did not choose."""
    row = _coverage_row(fetcher, data_class)
    return row.value_column if row is not None else ""


def _mesh_m(env: _Env) -> float | None:
    return env.params.value_of("mesh_resolution_m") if env.params else None


async def _need(env: _Env, decl: DataDecl, data_class: str,
                label: str) -> Need:
    """What this slot asks the world for, assembled off the RUN.

    The class is the slot's; the place is the domain's or the point the row was
    told to rank against, the window is the run's and the frame is the lever's."""
    from .levers import run_frame

    lon, lat = await _place(env, decl)
    opens = env.params.value_of("event_time") if env.params else None
    return Need(slot=label, data_class=data_class, lon=lon, lat=lat,
                opens=str(opens) if opens else None,
                until=_closes(opens, env.window_s), frame=run_frame(env.params),
                mesh_m=_mesh_m(env), pick=_pick(env, decl, data_class))


def _pick(env: _Env, decl: DataDecl, data_class: str) -> str:
    """The source this RUN names for this slot, "" where it names none.

    A slot that reads two classes - the bed reads a measurement and a terrain -
    takes the name on the class the named source actually serves, so one
    statement never answers for the other."""
    named = str(env.picks.get(decl.name) or "")
    if not named:
        return ""
    if not any(getattr(_spec_of(named), "coverage", ())):
        raise PlanValidationError(
            f"the run picks {named!r} for the {decl.name!r} slot and it states "
            "no coverage at all: a source the match never weighs cannot be "
            "picked out of its list.")
    return named if _coverage_row(named, data_class) is not None else ""


def _closes(opens: Any, window_s: float | None) -> str | None:
    """When the run's window closes, off the deck's own length."""
    if not opens or window_s is None:
        return None
    started = instant(opens)
    if started is None:
        return None
    return (started + timedelta(seconds=float(window_s))).isoformat()


async def _place(env: _Env, decl: DataDecl) -> tuple[float | None, float | None]:
    """The point a slot's coverage is tested at: what the row ranks against, else
    the domain's own centre."""
    near = await _bind_value(decl.coercion.get("near"), env)
    if near is not None:
        lon, lat = ((getattr(near, "lon", None), getattr(near, "lat", None))
                    if hasattr(near, "lon") else (near[0], near[1]))
        if lon is not None and lat is not None:
            return (float(lon), float(lat))
    dom = current_domain()
    if dom is None or not dom.bbox:
        return (None, None)
    west, south, east, north = dom.bbox
    return ((west + east) / 2.0, (south + north) / 2.0)


async def _ask_for(env: _Env, fetcher: str, decl: DataDecl) -> dict[str, Any]:
    """What a matched source is CALLED with, read off its own declared params.

    Every source states where it wants the place - a box or a seed - and a
    series source states the window as two dates; nothing else is passed, so a
    source's own defaults stand."""
    spec = _spec_of(fetcher)
    ask: dict[str, Any] = {"purpose": decl.name.replace("_", " ")}
    dom = current_domain()
    if "bbox" in spec.params and dom is not None and dom.bbox:
        ask["bbox"] = _around(dom.bbox, _mesh_m(env))
    if "seed_point" in spec.params:
        lon, lat = await _place(env, decl)
        if lon is not None:
            ask["seed_point"] = [lon, lat]
    opens = env.params.value_of("event_time") if env.params else None
    if opens and "start_date" in spec.params and "end_date" in spec.params:
        ask["start_date"] = str(opens)[:10]
        ask["end_date"] = (_closes(opens, env.window_s) or str(opens))[:10]
    elif opens and "valid_time" in spec.params:
        ask["valid_time"] = str(opens)
    return ask


def _around(bbox: Sequence[float], mesh_m: float | None) -> list[float]:
    """The domain's box with a MARGIN, which is what a slot asks a source for.

    A surface that stops exactly at the domain's edge leaves the nodes on that
    edge standing on nothing, so the ask reaches a few cells past it."""
    west, south, east, north = (float(v) for v in bbox)
    pad = max(3.0 * float(mesh_m or 0.0), 100.0) / 111_320.0
    lon_pad = pad / max(math.cos(math.radians((south + north) / 2.0)), 0.1)
    return [west - lon_pad, south - pad, east + lon_pad, north + pad]


async def _domain_runs(env: _Env) -> tuple[Any, ...]:
    """The boundary runs the DOMAIN row carries, or ``()`` where it carries none.

    The domain is produced on demand like any other read, so asking it for its
    runs is what fills the runs slot on a question whose producer measured the
    edge it cut the polygon between."""
    row = next((r for r in env.data.values() if r.role == DOMAIN), None)
    if row is None:
        return ()
    bound = await _deref(Ref(row.name), env)
    return tuple(getattr(bound, "runs", None) or ())


async def _unstated_ask(env: _Env, decl: DataDecl) -> str:
    """The PARAM this row's producer reads that the caller left unset, or "".

    A row asked over a window nobody stated has no question to put: the param
    that decides it says so on its own declaration, which is where the run's
    honesty about it belongs."""
    for name, value in decl.producer_kwargs.items():
        if isinstance(value, ParamRef) and await _bind_value(value, env) is None:
            return f"{value.name} (the {name} this row reads)"
    return ""


async def _domain_companion(env: _Env, named: str) -> Any:
    """One geometry the DOMAIN's producer measured beside its polygon, or ``None``.

    Read off the domain on demand like any other slot, so a question that needs
    the companion pays for the domain and a question that does not never asks."""
    row = next((r for r in env.data.values() if r.role == DOMAIN), None)
    if row is None:
        return None
    bound = await _deref(Ref(row.name), env)
    return dict(getattr(bound, "companions", None) or {}).get(named)


async def _ingested(env: _Env, decl: DataDecl, value: Any,
                    runner: str = "") -> Any:
    """A SLOT's value through the one ingestion its role reads; a plain row's
    value as it came.

    The whole point of a slot is that what fills it reads the same afterwards,
    so the ingestion runs wherever the value entered. Off the loop: reading a
    layer's geometry is object-store IO, and the plan is walked on it."""
    if not decl.role:
        return value
    from trid3nt_server.inputs.slots import ingest_slot

    coercion = await _bind_value(dict(decl.coercion), env)
    coercion.update(await _on_the_run_s_frame(env, decl, value))
    coercion.update(_what_the_record_reports(env, decl, runner))
    ingested = await asyncio.to_thread(ingest_slot, decl.role, value,
                                       label=decl.name, **coercion)
    if decl.role == DOMAIN and ingested is not None:
        # THE DOMAIN a run solves over IS the run's domain from the moment its
        # slot is filled: what a supplied artifact is checked against, and what
        # a later fetch is bounded by. Nothing else has to acquire an AOI first.
        bind_domain(Domain(bbox=tuple(ingested.bbox),
                           geometry=dict(ingested.geometry),
                           label=ingested.name))
    return ingested


def _what_the_record_reports(env: _Env, decl: DataDecl,
                             runner: str) -> dict[str, Any]:
    """What an OBSERVATION slot is told about the record it was handed.

    The unit of each value column is the SOURCE's own statement, read off the
    coverage row of whichever fetcher answered - a record that carries no unit
    column is still read in the unit it was measured in, never in the unit the
    slot wanted. The moment the run opens at and how long it covers are the
    run's, so the series is placed on the run's clock and a record that stops
    early refuses."""
    if decl.role not in (OBSERVATION, LEVEL, DISCHARGE):
        return {}
    told: dict[str, Any] = {"window_s": env.window_s}
    rows = list(getattr(_spec_of(runner), "coverage", ())) if runner else []
    told["column_units"] = {column: unit for row in rows
                            for column, unit in row.units.items()}
    row = next((r for r in rows if r.data_class == decl.data_class), None) \
        if decl.data_class else None
    if row is not None:
        # A SLOT THAT STATED A NEED names no column: which column carries the
        # reading, the window and the zero is the source's own statement.
        told.update(field=row.value_column, series_field=row.series_column,
                    above_field=row.above_column)
    if decl.coercion.get("at") is None and env.params is not None:
        told["at"] = env.params.value_of("event_time")
    return told


async def _on_the_run_s_frame(env: _Env, decl: DataDecl,
                              value: Any) -> dict[str, Any]:
    """What an ELEVATION slot is told about the run's own vertical frame.

    One frame per run, stated once as a runtime lever: a bed is read on it and a
    level is read on it, so neither is a row a question writes. Nothing else a
    run ingests is an elevation, and a slot that is handed a frame it does not
    need would demand a datum of a temperature. A source counting from ANOTHER
    frame is bridged by the offset row below, which the slot then reads."""
    from .levers import run_frame

    if decl.role not in (BED, LEVEL):
        return {}
    frame = run_frame(env.params)
    told = {"frame": frame} if decl.role == BED else {"to_datum": frame}
    measured = await _offset_row(env, decl, value, frame)
    return told if measured is None else {**told, "offset": measured}


async def _datum_offset_ask(value: Any, frame: str) -> Mapping[str, Any] | None:
    """What the offset row asks for this source, off the loop: a spec read."""
    from trid3nt_server.inputs.vertical_datum import offset_ask

    return await asyncio.to_thread(offset_ask, value, frame)


async def _offset_row(env: _Env, decl: DataDecl, value: Any,
                      frame: str) -> Any:
    """The measured shift onto the run's frame, as a DATA row the RUNTIME declares.

    The frame is the runtime's, so the question a differing source raises is the
    runtime's too - and it is asked the way every other fact about the world is,
    as a producer row with a ledger record and a line on the journal naming the
    service that answered. ``None`` where the pair owes no row: the source stands
    on the frame already, publishes its own shift, or names a datum no service
    transforms - and that last one leaves the slot to refuse naming both."""
    from trid3nt_server.inputs.vertical_datum import OFFSET_FETCH

    ask = await _datum_offset_ask(value, frame)
    if ask is None:
        return None
    name = f"{decl.name}_datum_offset"
    row = DataDecl(name=name, producer=Producer(runner=OFFSET_FETCH,
                                                kwargs=ask, row=name))
    env.data[name] = row
    return await _produce(env, row)


async def _context(env: _Env, decl: DataDecl, label: str) -> Any:
    """A CONTEXT row: produced where the source has something, absent where it
    does not, and the run continues either way under its own stated sentence.

    Only an empty SOURCE is an absence - a cancelled run is not, and a retryable
    gate error is a channel the caller still has to see. A window the caller left
    UNSTATED is not asked at all: the row's producer reads a param that is not
    there, so there is no question to put to the source."""
    unasked = await _unstated_ask(env, decl)
    if unasked:
        env.absences.append(f"{decl.context_sentence} ({unasked} was not stated)")
        logger.info("data %s is CONTEXT and %s was not stated, so no source was "
                    "asked; the run continues", decl.name, unasked)
        return None
    try:
        answered, value = await _walk_ladder(env, decl.producer, label)
        # The ingestion is INSIDE the absence: a source that answered with rows
        # its slot finds nothing usable in - sites that report another
        # characteristic, a survey with no soundings - held nothing for this run
        # either, and a context row says so rather than refusing.
        ingested = await _ingested(env, decl, value, answered.runner)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - any empty source is the absence
        if getattr(exc, "retryable", False):
            raise
        if _malformed_ask(exc):
            raise
        env.absences.append(f"{decl.context_sentence} ({exc})")
        logger.info("data %s is CONTEXT and its source held nothing (%s); the run "
                    "continues", decl.name, exc)
        return None
    record = _record_for(decl.name, answered.runner, value,
                         inputs_key=inputs_digest(answered.kwargs))
    env.data_records.append(dataclasses.replace(
        record, index=-1, node=label))
    if env.ledger is not None:
        await env.ledger.record_data(decl.name, record)
    return ingested


def _malformed_ask(exc: BaseException) -> bool:
    """Is this the ASK being wrong rather than the source holding nothing?

    A window, a bbox or a unit the caller stated wrong is a refusal the caller
    has to see; only an empty source is the absence a context row continues on.
    A producer's refusal reaches here inside the step that called it, so the
    whole chain is read and not just the wrapper."""
    from trid3nt_server.inputs.user_input import UserInputError
    from trid3nt_server.tools.fetchers._router.errors import RouterInputError

    seen: set[int] = set()
    found: BaseException | None = exc
    while found is not None and id(found) not in seen:
        if isinstance(found, (UserInputError, RouterInputError)):
            return True
        seen.add(id(found))
        found = getattr(found, "cause", None) or found.__cause__
    return False


async def _walk_ladder(env: _Env, producer: Producer,
                       label: str) -> tuple[Producer, Any]:
    """Call the producer, then its declared rungs in order -> the one that ANSWERED.
    Primary first, each declared rung after it; the last rung's failure is what the
    run reports, and the answering rung is what the ledger record's ``runner`` names."""
    rungs = (producer, *producer.ladder_rungs)
    failures: list[str] = []
    for index, rung in enumerate(rungs):
        kwargs = await _bind(dict(rung.kwargs), env, label)
        if rung.temporal is None and producer.temporal is not None:
            # The declared transform is the ARTIFACT's, not the rung's: whichever
            # rung answers delivers the cadence and units the consumer was
            # promised, or refuses.
            kwargs.setdefault("temporal", producer.temporal)
        elif rung.temporal is not None:
            kwargs.setdefault("temporal", rung.temporal)
        try:
            async with substep(current_emitter(), rung.runner.rsplit(".", 1)[-1]):
                value = await _call_runner(rung.runner, kwargs, label)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the next rung is the response
            if index == len(rungs) - 1:
                raise
            failures.append(f"{rung.runner} refused "
                            f"{getattr(exc, 'error_code', None) or type(exc).__name__}")
            logger.warning("%s rung %s failed (%s); falling to %s",
                           label, rung.runner, exc, rungs[index + 1].runner)
            continue
        if index:
            # This is the one place that knows a rung fired, and a substitution
            # between DATASETS is a fact about the answer: it goes on the run's
            # own journal, where the packet carries it, rather than into a log
            # line that dies with the process.
            journal_note(f"a DIFFERENT dataset answered {label}: "
                         + "; ".join(failures) + f"; {rung.runner} answered.")
        return rung, value
    raise StepFailedError(  # unreachable: the last rung re-raises above
        f"{label}: no rung answered.", error_code="DATA_LADDER_EXHAUSTED",
        step=label)


def _validate_supplied(env: _Env, decl: DataDecl, supplied: Any,
                      validate: Any) -> None:
    """Two checks and no third before a supplied artifact is adopted: the slot's
    declared SHAPE against the artifact's class, and - under ``CoversAOI`` - that a
    domain with an extent is bound. The artifact's own extent is never read."""
    decl.refuse_wrong_shape(supplied)
    if isinstance(supplied, (int, float)) and not isinstance(supplied, bool):
        # A NUMBER is not an artifact: a stated depth or a stated reading has no
        # extent for a coverage check to be about, and it covers the domain by
        # construction - which is why a slot takes one at all.
        return
    if validate is not CoversAOI:
        return
    dom = current_domain()
    if dom is not None and dom.bbox is not None:
        return
    if any(row.role == DOMAIN for row in env.data.values()):
        # A workflow that DECLARES a domain slot carries its own: the slot binds
        # it the moment it is filled, and a row produced before that one - a
        # structure the mesh subtracts, the box the water is cut out of - is
        # adopted for the shape it declares. What must agree with the domain is
        # checked where the two are used together.
        return
    raise SuppliedCoverageError(
        f"the artifact supplied for {decl.name!r} cannot be checked against the "
        "modelled domain: no domain is bound. Resolve the AOI before supplying one."
    )


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
    )


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
