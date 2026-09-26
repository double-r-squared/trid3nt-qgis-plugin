"""THE FILL: what puts a value into an input, and the one home of it.

Each input answers on arrival - accepted, rejected with its remedies, missing,
or defaulted to the module's own value - and a sourced input is fetched and
ingested before it answers, so its verdict is real. Filling never launches.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import math
from dataclasses import dataclass, field
from datetime import timedelta
from functools import partial
from typing import Any, Mapping, Sequence

from trid3nt_contracts.coverage import CoverageExtent, SourceChoice

from trid3nt_server.render.pipeline_emitter import current_emitter, substep
from trid3nt_server.tools.search.match import (
    Need, ask_for, base_ask, dropped_from, instant, match, sources_with_coverage)

from .data import (
    BED, DISCHARGE, DOMAIN, EXTENT, LEVEL, LINE, OBSERVE, WAVE, CoversAOI,
    DataDecl, Producer)
from .domain import Domain, bind_domain, current_domain
from .errors import (ContinuationRefused, PlanValidationError, StepFailedError,
                     SuppliedCoverageError)
from .interpreter import (_UNREPLAYABLE, _artifacts_live, _bind, _bind_value,
                          _call_runner, _deref, _record_for, _rehydrate)
from .journal import journal_note, slot_choice
from .ledger import LedgerRecord, StepLedger, inputs_digest
from .params import ResolvedParams
from .plan import ParamRef, Ref
from .temporal import RATE, STATE

logger = logging.getLogger("trid3nt_server.workflows.runtime.fill")

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
    #: The op the run STATES for a slot, by slot name - how the slot is filled
    #: where nothing measured it. The twin of ``picks``: a control somebody
    #: states on the call, read by the slot's own ingestion.
    ops: dict[str, Any] = field(default_factory=dict)
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
    #: The UNIT each slot's value is converted to, by role - the unit of the
    #: keyword that role fills, which the transform fixes. No row states one.
    slot_units: Mapping[str, str] = field(default_factory=dict)
    #: What the template CALLS each thing it publishes or measures, by published
    #: variable and by DATA row name. A measured row's caption is the noun the
    #: run's refusals and its journal line are written about.
    captions: Mapping[str, str] = field(default_factory=dict)
    #: The unit each PUBLISHED variable is written in, by the name the result
    #: carries. A row that observes one is read in that unit, so the record and
    #: the thing it is a measurement of are the same measurement.
    published_units: Mapping[str, str] = field(default_factory=dict)


def _data_step_label(name: str) -> str:
    return f"data:{name}"


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
            return await _ingested(env, decl, value, _coverage_row(
                cached.runner, decl.data_class))
    label = _data_step_label(decl.name)
    if decl.is_context:
        return await _context(env, decl, label)
    kwargs, value = await _produced(env, producer, label)
    record = _record_for(decl.name, producer.runner, value,
                         inputs_key=inputs_digest(kwargs))
    env.data_records.append(dataclasses.replace(
        record, index=-1, node=_data_step_label(decl.name)))
    if env.ledger is not None:
        await env.ledger.record_data(decl.name, record)
    return await _ingested(env, decl, value,
                           _coverage_row(producer.runner, decl.data_class))


async def _matched(env: _Env, decl: DataDecl) -> Any:
    """Fill a slot that states a NEED, through the match.

    Every slot takes the ONE source its own class matched. The bed takes the
    same one and then whatever the run's own ops lay over it, because a bed is
    the slot a domain most often reaches past and what to do about that is the
    person's statement, never this walk's."""
    await _somewhere_to_ask(env)
    if decl.role == BED:
        return await _ingested(env, decl, await _bed_surface(env, decl))
    # The INGESTION rides INSIDE the probe: a source that answered with rows this
    # slot finds nothing usable in - a gauge reporting no streamflow over a
    # closed body - held nothing for this run either, and the next survivor
    # takes its turn rather than the run standing on the first answer.
    beside = await _beside(env, decl)
    choice, value = await _probe(
        env, decl, decl.data_class, decl.name,
        read=lambda answer, row: _ingested(env, decl, answer, row, beside))
    if value is None:
        if decl.is_optional or decl.is_context:
            # A slot nothing measured is an absence the run STATES - the run's
            # own sentence, and the row's beside it where the row wrote one. The
            # refusal below is for a slot the run cannot stand without.
            env.absences.append(choice.sentence if decl.is_optional else
                                f"{decl.context_sentence} ({choice.sentence})")
            return None
        raise StepFailedError(choice.sentence, error_code="DATA_NEED_UNMATCHED",
                              step=_data_step_label(decl.name))
    return value


async def _beside(env: _Env, decl: DataDecl) -> dict[str, Any]:
    """The rows this SLOT declared beside the one its own class matched.

    A slot may state that what fills it is not one artifact - a line and the
    water surface it runs between - and each of those is produced here, under
    this slot, through the match, exactly as the rows a merge op names are. The
    slot still fetches nothing: it states a need and the match fills it. Empty
    for every slot and every kind that declares none."""
    from trid3nt_server.inputs.slots import needs_of

    seed = await _bind_value(decl.coercion.get("near"), env)
    held: dict[str, Any] = {}
    for name, word, geometry in needs_of(decl.role, _asked_of(decl, seed)):
        aside = dataclasses.replace(decl, name=f"{decl.name}_{name}", kind="",
                                    observes=word, geometry=geometry)
        _choice, value = await _probe(env, aside, decl.data_class, aside.name)
        if value is None:
            raise StepFailedError(
                f"the {decl.name!r} slot needs the {word} at this place beside "
                f"the {_asked_of(decl, seed)} it stands on, and nothing "
                "measured one here.",
                error_code="DATA_NEED_UNMATCHED",
                step=_data_step_label(aside.name))
        held[name] = value
    return held


async def _bed_surface(env: _Env, decl: DataDecl) -> Any:
    """THE BED: the ONE row the match ranked first, with the ops the run states
    laid over it.

    Nothing else paints. A second row reaches this bed only because the call
    named it in a merge op, and the water no row measured is painted only
    because the call stated the fill; what the run was NOT given is the surface's
    own coverage feedback to say, in the layer it publishes and on the journal.
    The rows a merge names are produced here, where every other fact about the
    world is produced, and which side of the cut each paints is its own
    declaration: a row serving this slot's class measured the bed, and one
    serving terrain measures the water TOP and stays outside the cut."""
    from trid3nt_server.inputs.bed import MERGE_DERIVE, interpolates, merge_rows
    from .levers import run_frame

    choice, top = await _probe(env, decl, decl.data_class,
                               f"{decl.name} {decl.data_class}")
    if top is None:
        raise StepFailedError(choice.sentence, error_code="DATA_NEED_UNMATCHED",
                              step=_data_step_label(decl.name))
    stated = env.ops.get(decl.name)
    laid = [(choice.picked, top)]
    under: list[tuple[str, Any]] = []
    for picked in merge_rows(stated):
        measures = _coverage_row(picked, decl.data_class) is not None
        held = await _named_row(env, decl, picked,
                                decl.data_class if measures else "terrain")
        (laid if measures else under).append((picked, held))
    frame = run_frame(env.params)
    surfaces = [await _surfaced(env, decl, picked, held)
                for picked, held in laid + under]
    return await _produce(env, _runtime_row(
        env, f"{decl.name}_merged", MERGE_DERIVE,
        {"primary": surfaces[:len(laid)], "fallback": surfaces[len(laid):],
         "frame": frame, "ops": stated,
         # THE CUT IS WHERE THE WATER IS, and a terrain surface measures the
         # water top rather than the bed, so it paints outside it only and the
         # feedback states the water and the land separately.
         "water": _cut_polygon(),
         # WHAT ELSE MATCHED and nothing laid, PER GROUND: the rows a person
         # names in a merge op to cover what this bed does not, stated in the
         # feedback rather than laid on their behalf. A WET hole is covered by
         # a row of this slot's own class and a DRY one by terrain, so each
         # ground is offered the rows the match ranked for the class that
         # measures it - never one another's.
         "water_alternatives": _unlaid(choice, laid + under),
         "land_alternatives": _unlaid(
             await _ranked(env, decl, "terrain", f"{decl.name} terrain"),
             laid + under),
         # THE SHORELINE THE FILL SEEDS is the free surface the run opens on,
         # so the elevation the run opens at and the elevation the shore is
         # seeded at are one number, asked for only where the fill is stated.
         "free_surface_m": (await _free_surface(env) if interpolates(stated)
                            else None),
         "primary_offset": [await _offset_row(env, f"{decl.name}_{picked}",
                                              surface, frame)
                            for (picked, _held), surface
                            in zip(laid, surfaces[:len(laid)])],
         "fallback_offset": [await _offset_row(env, f"{decl.name}_{picked}",
                                               surface, frame)
                             for (picked, _held), surface
                             in zip(under, surfaces[len(laid):])]}))


async def _ranked(env: _Env, decl: DataDecl, data_class: str,
                  label: str) -> SourceChoice:
    """What the match RANKS for this slot over one class, nothing produced.

    Both readers ask the class its own way round: the feedback offers the rows
    that measure a ground, and a named row is matched for the class it serves so
    the ask it is called with is the one that class states."""
    return match(await _need(env, decl, data_class, label),
                 sources_with_coverage())


def _unlaid(choice: SourceChoice, laid: Sequence[tuple[str, Any]]) -> list[str]:
    """The rows a match ranked that nothing laid, by name."""
    return [row.fetcher for row in choice.rows
            if not row.excluded and row.fetcher not in dict(laid)]


async def _free_surface(env: _Env) -> float | None:
    """The elevation this run OPENS at, off the run's own level slot.

    ``None`` where the run states no level: a bed op that needs the free surface
    refuses on that rather than seeding a number nobody measured."""
    row = next((r for r in env.data.values() if r.role == LEVEL), None)
    if row is None:
        return None
    held = env.artifacts.get(row.name)
    if held is None:
        held = env.artifacts[row.name] = await _produce(env, row)
    return None if held is None else float(getattr(held, "value", held))


async def _named_row(env: _Env, decl: DataDecl, picked: str,
                     data_class: str) -> Any:
    """One row a merge op NAMED, produced under this slot.

    Matched for the class the row serves so the ask it is called with is the one
    that class states, then called by name: the run named it, so the rank the
    match would have put it at decides nothing here. The RESOLUTION a named row
    is asked at is the merge's own statement, because a row asked at its posting
    over the whole domain is asked for cells this run has no node for."""
    from trid3nt_server.inputs.bed import merge_ask

    choice = await _ranked(env, decl, data_class, f"{decl.name} {picked}")
    ask = await _ask_for(env, choice.model_copy(update={"picked": picked}), decl)
    return await _produce(env, _runtime_row(
        env, f"{decl.name}_{picked}", picked,
        {**ask, **merge_ask(picked, _mesh_m(env))}))


async def _surfaced(env: _Env, decl: DataDecl, picked: str, held: Any) -> Any:
    """One row as a SURFACE: soundings through the grid that makes one of them,
    a raster as it came. What a bed covers is a statement about cells, so every
    row laid on one is read at the mesh's own scale first."""
    from trid3nt_server.inputs.bed import SURVEY_DERIVE

    if _spec_of(picked).output.layer_type != "vector":
        return held
    return await _produce(env, _runtime_row(
        env, f"{decl.name}_surveyed_{picked}", SURVEY_DERIVE,
        {"points": held, "value_field": _value_column(picked, decl.data_class),
         "resolution_m": _mesh_m(env)}))


async def _probe(env: _Env, decl: DataDecl, data_class: str, label: str, *,
                 read: Any = None) -> tuple[SourceChoice, Any]:
    """Match a class, then CALL the survivors in rank order -> the first answer.

    A source that held nothing over this domain is dropped and the next takes
    its turn, which is how the list a reader sees says what the world answered
    rather than what the sort preferred. ``read`` is the slot's own reading of an
    answer, handed the row that was matched and run HERE so a record this slot
    cannot read drops out like an empty one. ``None`` where none answered."""
    choice = match(await _need(env, decl, data_class, label),
                   sources_with_coverage())
    while choice.picked:
        row = _runtime_row(env, f"{label.replace(' ', '_')}_{choice.picked}",
                           choice.picked,
                           await _ask_for(env, choice, decl))
        try:
            value = await _produce(env, row)
            if read is not None:
                value = await read(value, _matched_row(choice, choice.picked))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - an empty source drops out
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


def _coverage_row(fetcher: str, data_class: str, kind: str = "") -> Any:
    """The row THIS source states about THIS class, or ``None``.

    A source serving two classes states one row each, and the row a slot reads
    is the row of the class it asked for; where it serves one class two ways - a
    measured record beside a prediction - the ``kind`` the match ranked says
    which of the two, because each publishes its own columns and units. A row
    asking about no class at all - a derive the runtime declared - names none."""
    if not data_class:
        return None
    return next((row for row in getattr(_spec_of(fetcher), "coverage", ())
                 if row.data_class == data_class
                 and (not kind or row.kind == kind)), None)


def _matched_row(choice: SourceChoice, fetcher: str) -> Any:
    """THE COVERAGE ROW the match produced, or ``None`` where it picked nothing.

    The ranked row names it by both facts a source can serve twice under - the
    class the slot asked for and the kind the row was ranked on - so what a slot
    is told about the record is the statement of the row that answered it."""
    kind = next((row.kind for row in choice.rows
                 if row.fetcher == fetcher and not row.excluded), "")
    return _coverage_row(fetcher, choice.need, kind) if fetcher else None


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

    seed = await _bind_value(decl.coercion.get("near"), env)
    lon, lat = _place(seed)
    opens = env.params.value_of("event_time") if env.params else None
    return Need(slot=label, data_class=data_class, lon=lon, lat=lat,
                geometry=decl.geometry or "",
                opens=str(opens) if opens else None,
                until=_closes(opens, env.window_s), frame=run_frame(env.params),
                mesh_m=_mesh_m(env), pick=_pick(env, decl, data_class),
                of=_asked_of(decl, seed), water=_water())


def _asked_of(decl: DataDecl, seed: Any) -> str:
    """WHAT OF ITS CLASS this row asks the world for.

    THE DOMAIN asks for its own KIND: the one the template states where the
    seed cannot imply it, else the one the SEED stands on - which water a place
    is, is a fact of the place, so the run reads it off the seed rather than
    hearing it from the question. Every other row asks for the variable it
    observes."""
    from trid3nt_server.inputs.domain import place_kind

    if decl.role == DOMAIN:
        return decl.kind or place_kind(seed)
    return str(decl.observes or "")


async def _somewhere_to_ask(env: _Env) -> None:
    """Make sure the run has a PLACE before a source is asked for one.

    A question whose domain is CUT out of a box has no polygon until the cut
    runs, and the cut's own sources are asked over ground: the window the
    question was asked in is that ground, so its slot is filled first."""
    if current_domain() is not None:
        return
    row = next((r for r in env.data.values() if r.role == EXTENT), None)
    if row is not None:
        await _produce(env, row)


def _water() -> CoverageExtent | None:
    """The domain's own outline, which is the water a gauge has to stand on.

    The polygon the run drew where the domain carries one, else its box, and the
    note says WHICH - a box is a coarser statement of the same water, and a
    reader of the refusal has to know which one answered."""
    from trid3nt_server.inputs.domain import domain_ring

    dom = current_domain()
    if dom is None:
        return None
    if dom.geometry:
        ring = [(float(x), float(y)) for x, y in domain_ring(dom)]
        note = "the domain's own polygon"
    elif dom.bbox:
        west, south, east, north = (float(v) for v in dom.bbox)
        ring = [(west, south), (east, south), (east, north), (west, north)]
        note = "the domain's own box"
    else:
        return None
    if len(ring) < 3:
        return None
    return CoverageExtent(kind="surface", rings=[ring + [ring[0]]], note=note)


def _cut_polygon() -> dict[str, Any] | None:
    """The polygon the domain was CUT with, or ``None`` where it carries none.

    A box is not a cut: it states where the question was asked, not where the
    water is, and a bed restricted to one would leave dry ground unpainted."""
    dom = current_domain()
    return dict(dom.geometry) if dom is not None and dom.geometry else None


def _ops(ops: Mapping[str, Any] | None,
         data: Sequence[DataDecl]) -> dict[str, Any]:
    """The ops this invocation states, by slot name - refusing the ones no slot
    can read.

    An op NAME means something only to the ingestion that reads it, so that half
    is the slot's to refuse. What is refused here is the half only the workflow
    knows: an op stated for a row it does not declare, or for one whose ingestion
    takes no op at all, which would otherwise be dropped in silence."""
    from trid3nt_server.inputs.slots import takes_op

    stated = {str(name): op for name, op in dict(ops or {}).items() if op}
    rows = sorted(decl.name for decl in data)
    for name in stated:
        if name not in rows:
            raise PlanValidationError(
                f"the run states an op for {name!r}, which is not a row this "
                f"workflow declares: {', '.join(rows)}.")
        if not takes_op(name):
            raise PlanValidationError(
                f"the run states an op for the {name!r} row, whose ingestion "
                "reads none: an op is read by the slot it fills.")
    return stated


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


def _place(seed: Any) -> tuple[float | None, float | None]:
    """The point a slot's coverage is tested at: what the row ranks against, else
    the domain's own centre."""
    from trid3nt_server.inputs.point import lonlat_of

    # THE POINT a row is asked at, in whatever shape the question stated it: a
    # pick, a pair, a drawn feature. A value no place reads out of is no place
    # rather than a refusal - the domain's own centre answers next.
    near = lonlat_of(seed)
    if near is not None:
        return near
    dom = current_domain()
    if dom is None or not dom.bbox:
        return (None, None)
    west, south, east, north = dom.bbox
    return ((west + east) / 2.0, (south + north) / 2.0)


async def _ask_for(env: _Env, choice: SourceChoice,
                   decl: DataDecl) -> dict[str, Any]:
    """What a matched source is CALLED with, read off its own declared params.

    Every source states where it wants the place - a box or a seed - and a
    series source states the window as two dates; what the matched ROW adds to
    that is the match's to say, so the ask closes through it. The row's own
    generic attributes travel with it, for the matched row to map onto the
    params this source states them in."""
    dom = current_domain()
    seed = await _bind_value(decl.coercion.get("near"), env)
    lon, lat = _place(seed)
    opens = env.params.value_of("event_time") if env.params else None
    return ask_for(choice, base_ask(
        choice, decl.name.replace("_", " "),
        _around(dom.bbox, _mesh_m(env)) if dom is not None and dom.bbox else None,
        lon, lat, str(opens) if opens else None,
        _closes(opens, env.window_s)), lon, lat,
        {"span_km": decl.span_km, "of": _asked_of(decl, seed) or None,
         "seed_point": None if lon is None or lat is None else [lon, lat]})


def _around(bbox: Sequence[float], mesh_m: float | None) -> list[float]:
    """The domain's box with a MARGIN, which is what a slot asks a source for.

    A surface that stops exactly at the domain's edge leaves the nodes on that
    edge standing on nothing, so the ask reaches a few cells past it."""
    west, south, east, north = (float(v) for v in bbox)
    pad = max(3.0 * float(mesh_m or 0.0), 100.0) / 111_320.0
    lon_pad = pad / max(math.cos(math.radians((south + north) / 2.0)), 0.1)
    return [west - lon_pad, south - pad, east + lon_pad, north + pad]


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
                    row: Any = None, beside: Mapping[str, Any] | None = None
                    ) -> Any:
    """A SLOT's value through the one ingestion its role reads; a plain row's
    value as it came.

    The whole point of a slot is that what fills it reads the same afterwards,
    so the ingestion runs wherever the value entered. Off the loop: reading a
    layer's geometry is object-store IO, and the plan is walked on it."""
    if not decl.role:
        return value
    from trid3nt_server.inputs.slots import ingest_slot

    coercion = await _bind_value(dict(decl.coercion), env)
    stated = str(coercion.pop("measures", "") or "")
    coercion.pop("opens", None)
    coercion.update(_what_the_run_calls_it(
        env, decl, stated, _asked_of(decl, coercion.get("near"))))
    coercion.update(_the_window_it_is_cut_from(decl))
    coercion.update(await _on_the_run_s_frame(env, decl, value))
    coercion.update(_what_the_record_reports(env, decl, row))
    # WHAT THE SLOT DECLARED BESIDE the matched row, and how far the question
    # reaches: the ingestion that states a need is the ingestion that reads it.
    coercion.update(dict(beside or {}))
    if beside:
        coercion["span_km"] = decl.span_km
    coercion["op"] = env.ops.get(decl.name)
    ingested = await asyncio.to_thread(ingest_slot, decl.role, value,
                                       label=decl.name, **coercion)
    if decl.role == DOMAIN and ingested is not None:
        # THE DOMAIN a run solves over IS the run's domain from the moment its
        # slot is filled: what a supplied artifact is checked against, and what
        # a later fetch is bounded by. Nothing else has to acquire an AOI first.
        bind_domain(Domain(bbox=tuple(ingested.bbox),
                           geometry=dict(ingested.geometry),
                           label=ingested.name))
    elif decl.role == EXTENT and ingested is not None and current_domain() is None:
        # A QUESTION WHOSE DOMAIN IS CUT OUT OF A BOX has no polygon until the
        # cut runs, and the cut's own sources have to be asked somewhere: the
        # window the question was asked in is that place until the domain slot
        # supersedes it.
        bind_domain(Domain(bbox=tuple(ingested.bbox), geometry={},
                           label=ingested.name))
    return ingested


#: The slots filled from a RECORD somebody measured - the four whose value is a
#: reading rather than a geometry or a surface. What a row of one OBSERVES is a
#: published variable and is read in that variable's unit; on any other slot
#: ``of`` names the FEATURE the source publishes and no unit is owed.
_READS_A_RECORD = (OBSERVE, LEVEL, DISCHARGE, WAVE)


def _the_window_it_is_cut_from(decl: DataDecl) -> dict[str, Any]:
    """The BOX a domain slot cuts a land-water edge against, where the run has one.

    The window a question is asked in is the RUN's - its own slot bound it
    before any source was asked - so a deck states it once and the ingestion
    that cuts with it reads it here. Empty once a polygon is bound: a domain
    that arrived closed is cut against nothing."""
    if decl.role != DOMAIN:
        return {}
    dom = current_domain()
    if dom is None or dom.geometry or not dom.bbox:
        return {}
    return {"extent": tuple(float(v) for v in dom.bbox)}


def _what_the_run_calls_it(env: _Env, decl: DataDecl, stated: str,
                           observed: str) -> dict[str, Any]:
    """The UNIT this slot converts to and the NOUN the run says it in.

    Neither is a row's to state: the unit is the one the keyword this role fills
    is read in, fixed by the transform that writes it, and the noun is the
    template's caption for the row - the same word the sheet and the published
    variable are captioned with. A row that OBSERVES a published variable is
    read in that variable's own unit instead, because a measurement and the
    thing it is a measurement of are comparable in one unit and no other; a role
    the workflow states no unit for is read in the unit the record was measured
    in."""
    told: dict[str, Any] = {}
    unit = (_observed_unit(env, decl, observed)
            if observed and decl.role in _READS_A_RECORD
            else env.slot_units.get(decl.role))
    if unit:
        told["to_units"] = unit
    caption = env.captions.get(decl.name) or stated
    if caption:
        told["caption"] = str(caption)
    return told


def _observed_unit(env: _Env, decl: DataDecl, observed: str) -> str:
    """The unit the variable this row OBSERVES is published in.

    A name this run publishes nothing under, or publishes under no stated unit,
    refuses: reading the record in whatever its source published would pair a
    measurement against a variable in another unit and call the difference the
    model's error."""
    unit = str(env.published_units.get(observed) or "")
    if unit:
        return unit
    raise PlanValidationError(
        f"Data {decl.name!r} observes {observed!r}, and this run publishes "
        f"no unit under that name (it publishes "
        f"{', '.join(sorted(n for n, u in env.published_units.items() if u)) or 'nothing named'}). "
        "A record read in its own unit against a variable written in another is "
        "a difference nobody measured: name the variable the run publishes, or "
        "state the unit where that variable is declared.")


def _what_the_record_reports(env: _Env, decl: DataDecl,
                             row: Any) -> dict[str, Any]:
    """What an OBSERVATION slot is told about the record it was handed.

    Every word of it is THE MATCHED ROW's own statement, never a reading across
    the other rows the source serves: one service publishing a level in metres
    and a temperature in degrees under the same column name states a row each,
    and the units of the row nobody matched are another measurement's. A record
    that carries no unit column is still read in the unit it was measured in,
    never in the unit the slot wanted. The moment the run opens at and how long
    it covers are the run's, so the series is placed on the run's clock and a
    record that stops early refuses."""
    if decl.role not in _READS_A_RECORD:
        return {}
    told: dict[str, Any] = {"window_s": env.window_s}
    if row is not None:
        # A SLOT THAT STATED A NEED names no column: which column carries the
        # reading, the window and the zero is the source's own statement.
        told.update(column_units=dict(row.units), field=row.value_column,
                    series_field=row.series_column, above_field=row.above_column)
    # HOW THE RECORD MOVES IN TIME is the source's own class where the row that
    # answered states one, else the slot's role: a flow is a per-time total and a
    # level is read at an instant. No author states it.
    from trid3nt_server.inputs.series import quantity_class

    told["quantity"] = (quantity_class(row.data_class) if row is not None
                        else RATE if decl.role == DISCHARGE else STATE)
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
    measured = await _offset_row(env, decl.name, value, frame)
    return told if measured is None else {**told, "offset": measured}


async def _datum_offset_ask(value: Any, frame: str,
                            seed: Any) -> Mapping[str, Any] | None:
    """What the offset row asks for this source, off the loop: a footprint read."""
    from trid3nt_server.inputs.vertical_datum import offset_ask

    return await asyncio.to_thread(partial(offset_ask, value, frame, at=seed))


async def _question_seed(env: _Env) -> Any:
    """The point the QUESTION named, read off the row that stands the run on a
    place, or ``None`` where it named none.

    A question states its point once, on the row that puts the run on the ground
    - a reach is cut from it, a basin is traced up from it. Another row's own
    point ranks a reporting site and is not the question's place."""
    from trid3nt_server.inputs.point import lonlat_of

    row = next((r for r in env.data.values() if r.role in (DOMAIN, EXTENT)
                and r.coercion.get("near") is not None), None)
    if row is None:
        return None
    return lonlat_of(await _bind_value(row.coercion.get("near"), env))


async def _offset_row(env: _Env, owner: str, value: Any, frame: str) -> Any:
    """The measured shift onto the run's frame, as a DATA row the RUNTIME declares.

    The frame is the runtime's, so the question a differing source raises is the
    runtime's too - and it is asked the way every other fact about the world is,
    as a producer row with a ledger record and a line on the journal naming the
    service that answered, at the point of the source's own footprint nearest
    the question's seed. ``None`` where the pair owes no row: the source stands
    on the frame already, publishes its own shift, or names a datum no service
    transforms - and that last one leaves the alignment to refuse naming both.

    ``owner`` is what the row is named after - a slot, or one of the two surfaces
    the bed merge reads onto the frame before it overlays them."""
    from trid3nt_server.inputs.vertical_datum import OFFSET_FETCH

    ask = await _datum_offset_ask(value, frame, await _question_seed(env))
    if ask is None:
        return None
    name = f"{owner}_datum_offset"
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
        kwargs, value = await _produced(env, decl.producer, label)
        # The ingestion is INSIDE the absence: a source that answered with rows
        # its slot finds nothing usable in - sites that report another
        # characteristic, a survey with no soundings - held nothing for this run
        # either, and a context row says so rather than refusing.
        ingested = await _ingested(env, decl, value, _coverage_row(
            decl.producer.runner, decl.data_class))
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
    record = _record_for(decl.name, decl.producer.runner, value,
                         inputs_key=inputs_digest(kwargs))
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


async def _produced(env: _Env, producer: Producer,
                    label: str) -> tuple[dict[str, Any], Any]:
    """Call one producer -> the reads it was called with, and what it answered.

    The reads come back because they are what the ledger record is keyed on, and
    binding them twice would ask the plan for the same values twice."""
    kwargs = await _bind(dict(producer.kwargs), env, label)
    async with substep(current_emitter(), producer.runner.rsplit(".", 1)[-1]):
        return kwargs, await _call_runner(producer.runner, kwargs, label)


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



ACCEPTED, REJECTED, MISSING, DEFAULTED = (
    "accepted", "rejected", "missing", "defaulted")

#: What rides a fill beside its inputs and is carried as stated: how the run is
#: reviewed, whether it resumes, and the ops a row's own ingestion reads.
_CARRIED = ("input_mode", "restart_clean", "ops")


@dataclass(frozen=True)
class Verdict:
    """One input's answer on arrival: its state, the value and where it came
    from, or the reason it was refused and what would be taken instead."""

    state: str
    value: Any = None
    origin: str = ""
    reason: str = ""
    remedies: tuple[str, ...] = ()
    code: str = ""


@dataclass
class Fill:
    """The fill's state: every input's verdict, and what a launch reads."""

    workflow: Any
    inputs: dict[str, Verdict] = field(default_factory=dict)
    stated: dict[str, Any] = field(default_factory=dict)
    carried: dict[str, Any] = field(default_factory=dict)
    keywords: dict[str, Any] = field(default_factory=dict)
    params: ResolvedParams | None = None
    continue_from: str | None = None
    continued: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    env: _Env | None = None
    domain: Domain | None = None

    @property
    def ready(self) -> bool:
        return not any(v.state in (REJECTED, MISSING)
                       for v in self.inputs.values())

    def refusal(self) -> tuple[str, str]:
        """``(code, sentence)`` naming every input that keeps this fill from ready."""
        held = [(n, v) for n, v in self.inputs.items()
                if v.state in (REJECTED, MISSING)]
        code = next((v.code for _n, v in held if v.code), "FILL_NOT_READY")
        return code, " ".join(
            v.reason + (f" Remedies: {', '.join(v.remedies)}." if v.remedies
                        else "") for _n, v in held)


async def fill(state: Fill, values: Mapping[str, Any]) -> Fill:
    """Put ``values`` into their inputs -> the fill's state, each input answered.

    A value is a literal, ``{"layer": <case layer id>}`` or ``{"source": <name>}``;
    a sourced or layer input is fetched and ingested before it answers. Inputs
    not named keep their verdicts; the rest default to the module's own value."""
    wf = state.workflow
    values = {k: v for k, v in dict(values).items() if v is not None}
    for name in _CARRIED:
        if name in values:
            state.carried[name] = values.pop(name)
    for name, source in dict(values.pop("picks", None) or {}).items():
        values[str(name)] = {"source": str(source)}
    keywords = values.pop("keywords", None) or {}
    if not isinstance(keywords, Mapping):
        state.inputs["keywords"] = Verdict(
            REJECTED, keywords, code="KEYWORD_REFUSED",
            reason="keywords takes a mapping of the engine's own keyword names "
            f"to values; got {type(keywords).__name__}.")
        keywords = {}
    for name, value in keywords.items():
        _keyword(state, str(name), value)
    rows = {decl.name: decl for decl in wf.data}
    sourced = {n: values.pop(n) for n in list(values) if n in rows}
    continue_from = values.pop("continue_from", None)
    state.stated.update(values)
    await _seat(state)
    if continue_from is not None:
        await _continuation(state, str(continue_from))
    if "ops" in state.carried:
        _carried_ops(state, rows)
    for name, value in sourced.items():
        await _row(state, rows[name], value)
    return state


def _keyword(state: Fill, name: str, value: Any) -> None:
    """One engine keyword through the module's own accept rule."""
    try:
        identifier, taken, note = state.workflow.accept_keyword(name, value)
    except Exception as exc:  # noqa: BLE001 - the refusal is the verdict
        if getattr(exc, "retryable", False):
            raise
        state.inputs[name] = Verdict(REJECTED, value, reason=str(exc),
                                     code="KEYWORD_REFUSED")
        return
    if note and note not in state.notes:
        state.notes.append(note)
    state.keywords[name] = value
    state.inputs[name] = Verdict(ACCEPTED, taken, origin="user")


async def _seat(state: Fill) -> None:
    """Every declared param through its coercion and its own accept rule."""
    from .resolver import seat_param

    wf = state.workflow
    args = {**state.stated, "input_mode": state.carried.get("input_mode")}
    refused: dict[str, BaseException] = {}
    for coercion in wf.coercions:
        try:
            coerced = coercion(args)
            if asyncio.iscoroutine(coerced) or hasattr(coerced, "__await__"):
                coerced = await coerced
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the refusal is the verdict
            if getattr(exc, "retryable", False) \
                    or getattr(exc, "error_code", None) is None:
                raise
            refused[str(getattr(coercion, "__name__", "")).split(":")[-1]] = exc
            continue
        args.update(coerced or {})
    declared = {prm.name for prm in wf.params}
    for name, exc in refused.items():
        if name not in declared:
            state.inputs[name] = Verdict(REJECTED, state.stated.get(name),
                                         reason=str(exc), code=exc.error_code)
    rows = {}
    for prm in wf.params:
        if prm.name in refused:
            exc = refused[prm.name]
            state.inputs[prm.name] = Verdict(
                REJECTED, state.stated.get(prm.name), reason=str(exc),
                code=getattr(exc, "error_code", None) or "INPUT_REFUSED")
            continue
        value = args.get(prm.name)
        try:
            rows[prm.name] = row = seat_param(prm, value)
        except Exception as exc:  # noqa: BLE001 - the refusal is the verdict
            state.inputs[prm.name] = Verdict(
                REJECTED, value, reason=str(exc),
                code=_refusal_code(exc, prm.name),
                remedies=(f"a value between {prm.bounds[0]} and "
                          f"{prm.bounds[1]}",) if prm.bounds else ())
            continue
        if row.required_missing:
            state.inputs[prm.name] = Verdict(
                MISSING, reason=f"{prm.name} was not supplied and has no "
                "default: supply it explicitly - it is never invented.",
                code="GATE_REFUSED")
        elif row.value is not None:
            state.inputs[prm.name] = Verdict(
                DEFAULTED if value is None else ACCEPTED, row.value,
                origin="default" if value is None else "user")
    state.params = ResolvedParams(rows) if len(rows) == len(wf.params) else None


async def _continuation(state: Fill, run_id: str) -> None:
    """``continue_from``: only a journaled solved run of THIS module is taken;
    its mesh is held to this run's once the mesh is built."""
    from .journal import read_records

    records = await asyncio.to_thread(read_records)
    line = next((r for r in reversed(records)
                 if str(r.get("run_id") or "") == run_id.strip()), None)
    module = getattr(state.workflow, "module_name", None)
    reason = ""
    if line is None or not line.get("solved"):
        reason = (f"continue_from={run_id!r} names no solved result: only a run "
                  "whose solve completed and was journaled leaves a state to "
                  "carry on.")
    elif module and line.get("module") != module:
        reason = (f"continue_from={run_id!r} solved {line.get('module')!r} and "
                  f"this fill fills {module!r}: a state carries on only in the "
                  "module that wrote it.")
    if reason:
        state.inputs["continue_from"] = Verdict(
            REJECTED, run_id, reason=reason, code=ContinuationRefused.error_code,
            remedies=("a run of this module on this mesh",))
        return
    state.continue_from = run_id
    state.continued = dict(line)
    state.inputs["continue_from"] = Verdict(ACCEPTED, run_id, origin="user")


def refuse_other_mesh(continued: Mapping[str, Any], mesh_key: str) -> None:
    """A continuation opens only on the mesh built by the same content as the
    run it continues; any other mesh is refused by name."""
    if not continued:
        return
    theirs = str((continued.get("mesh") or {}).get("key") or "")
    if theirs != mesh_key:
        raise ContinuationRefused(
            f"continue_from={continued.get('run_id')!r} was solved on another "
            f"mesh ({theirs or 'none recorded'}) than this run builds "
            f"({mesh_key}): a state carries on only on the mesh built by the "
            "same content.")


def _carried_ops(state: Fill, rows: Mapping[str, DataDecl]) -> None:
    """``ops`` is carried unchanged; a row that reads none refuses it by name."""
    ops = state.carried["ops"]
    try:
        _ops(ops, tuple(rows.values()))
    except PlanValidationError as exc:
        state.inputs["ops"] = Verdict(REJECTED, ops, reason=str(exc),
                                      code=exc.error_code)
        return
    state.inputs["ops"] = Verdict(ACCEPTED, ops, origin="user")


def production(state: Fill) -> _Env:
    """The state a sourced input is produced under, built once per fill."""
    wf = state.workflow
    if state.env is None:
        state.env = _Env(
            params=state.params, data={d.name: d for d in wf.data}, results={},
            input_mode=state.carried.get("input_mode"),
            keywords=dict(state.keywords), ops=_ops(
                state.carried.get("ops"), wf.data)
            if state.inputs.get("ops", Verdict(ACCEPTED)).state == ACCEPTED
            else {}, workflow=wf.name,
            window_s=wf.run_window_s(dict(state.keywords)),
            slot_units=wf.slot_units(), captions=wf.captions,
            published_units=wf.published_units())
    return state.env


async def _row(state: Fill, decl: DataDecl, value: Any) -> None:
    """A sourced input: fetched or read, and ingested, BEFORE it answers."""
    if state.params is None:
        state.inputs[decl.name] = Verdict(
            REJECTED, value, code="FILL_NOT_READY",
            reason=f"{decl.name} stands on the run's params, and one of them "
            "was refused.")
        return
    env = production(state)
    origin = "user"
    if isinstance(value, Mapping) and "source" in value:
        env.picks[decl.name] = origin = str(value["source"])
        origin = f"source:{origin}"
    else:
        env.supplied[decl.name] = (value.get("layer")
                                   if isinstance(value, Mapping) else value)
    try:
        if decl.role not in (DOMAIN, EXTENT):
            await _place_first(env)
        held = env.artifacts[decl.name] = await _produce(env, decl)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - the refusal is the verdict
        if getattr(exc, "retryable", False):
            raise
        picked = env.picks.pop(decl.name, None)
        env.supplied.pop(decl.name, None)
        state.inputs[decl.name] = Verdict(
            REJECTED, value, reason=str(exc), code=_refusal_code(exc, decl.name),
            remedies=await _instead(env, decl, picked) if picked else ())
        return
    state.domain = current_domain()
    state.inputs[decl.name] = Verdict(ACCEPTED, held, origin=origin)


def _refusal_code(exc: BaseException, name: str) -> str:
    """The code a refused input carries. An error that states no code is a
    fault, not a refusal of the value: the caller still gets the refusal, and
    the log gets the trace."""
    code = getattr(exc, "error_code", None)
    if code is None:
        logger.warning("input %s refused on an untyped error", name,
                       exc_info=exc)
    return code or "INPUT_REFUSED"


async def _place_first(env: _Env) -> None:
    """A sourced input stands on the run's place, so the place is filled first.

    Producing a row may register the runtime's own rows, so the walk is over
    the rows as they stood before it."""
    for row in list(env.data.values()):
        if row.role == DOMAIN and row.name not in env.artifacts:
            env.artifacts[row.name] = await _produce(env, row)


async def _instead(env: _Env, decl: DataDecl, picked: str) -> tuple[str, ...]:
    """The sources the match ranks for this input that a refused pick is not."""
    try:
        choice = await _ranked(env, decl, decl.data_class, decl.name)
    except Exception:  # noqa: BLE001 - no ranking is no remedy to name
        return ()
    return tuple(row.fetcher for row in choice.rows
                 if not row.excluded and row.fetcher != picked)
