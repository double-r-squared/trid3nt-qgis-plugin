"""THE FILL: what puts a value into an input, and the one home of it.

Each input answers on arrival - accepted, rejected with its remedies, missing,
or defaulted to the module's own value - and a sourced input is fetched and
ingested before it answers, so its verdict is real. Filling never launches.
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib
import inspect
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

from ..workflows.runtime.data import CoversAOI, DataDecl, Producer

from .slots import BED, DISCHARGE, DOMAIN, EXTENT, LEVEL, LINE
from ..workflows.runtime.domain import Domain, bind_domain, current_domain
from ..workflows.runtime.errors import (DeclarativeError, PlanValidationError, StepFailedError,
                     SuppliedCoverageError, said)
from ..workflows.runtime import journal
from ..workflows.runtime.journal import journal_note, slot_choice
from ..workflows.runtime.params import ResolvedParams
from ..workflows.runtime.temporal import RATE, STATE

logger = logging.getLogger("trid3nt_server.inputs.fill")

async def read(env: "_Env", name: str) -> Any:
    """One input of the run's mapping by its plain name: a declared row nothing
    has read yet is produced on its first read, and a name nothing of the run
    is called refuses by name."""
    if name in env.run:
        return env.run[name]
    if name not in env.data:
        raise StepFailedError(
            f"{name!r} names no input of this run: its inputs are "
            f"{sorted(set(env.run) | set(env.data))}.",
            error_code="INPUT_UNNAMED")
    held = await _produce(env, env.data[name])
    env.run[name] = held
    return held


async def _seed(env: "_Env", decl: DataDecl) -> Any:
    """The point a row is asked at, read off the input its ``at`` names."""
    near = decl.coercion.get("near")
    return await read(env, near) if isinstance(near, str) else near


@dataclass
class _Env:
    params: ResolvedParams
    data: dict[str, DataDecl]
    input_mode: str | None = None
    #: The workflow this fill belongs to - what a gate card names as the asker.
    workflow: str = ""
    #: The raw keyword floor this invocation carried, by the name the caller used.
    keywords: dict[str, Any] = field(default_factory=dict)
    #: The source the run NAMES for a slot, by slot name. It picks among the
    #: survivors of that slot's match and never past its filters.
    picks: dict[str, str] = field(default_factory=dict)
    #: THE RUN'S ONE MAPPING: params, fetched rows, what each stage produced
    #: and the keywords so far, by plain name.
    run: dict[str, Any] = field(default_factory=dict)
    #: Artifacts SUPPLIED rather than produced - a layer handle, a file uri, a
    #: gate's answer. What satisfies a producer-less ``Data`` slot.
    supplied: dict[str, Any] = field(default_factory=dict)
    #: Absences worth narrating, each as the SENTENCE the run carries: an
    #: optional Data nothing satisfied, or a context row whose source was empty.
    absences: list[str] = field(default_factory=list)
    #: How long the solve runs, in seconds, off the deck this run writes: the WINDOW a
    #: matched series source must cover, or the run refuses.
    window_s: float | None = None
    #: The UNIT each slot's value is converted to, by role - the unit of the
    #: keyword that role fills, which the transform fixes. No row states one.
    slot_units: Mapping[str, str] = field(default_factory=dict)
    #: What the template CALLS each thing it publishes or measures, by published
    #: variable and by DATA row name; a measured row's caption is the noun its refusals
    #: and journal line are written about.
    captions: Mapping[str, str] = field(default_factory=dict)
    #: The unit each PUBLISHED variable is written in, by the name the result carries;
    #: a row that observes one is read in that unit.
    published_units: Mapping[str, str] = field(default_factory=dict)


def _data_step_label(name: str) -> str:
    return f"data:{name}"


async def _produce(env: _Env, decl: DataDecl) -> Any:
    """Satisfy one declared artifact, ON DEMAND - when a step that reads it runs.

    An artifact nothing reads costs no fetch; a CONTEXT row is asked regardless."""
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
        # THE DOMAIN'S PRODUCER measured this beside the polygon it cut - a reach's
        # centerline rides on the artifact it returned. A body whose producer measured
        # none is asked on the canvas next.
        measured = await _domain_companion(env, "centerline")
        if measured is not None:
            return measured
    if producer is None and decl.data_class:
        # A SLOT THAT STATES A NEED: the match reads every fetcher's row
        # and the runtime declares the row it picked, so the pick earns a
        # journal line like any other producer.
        return await _matched(env, decl)
    if producer is None and decl.role:
        # A SLOT the caller did not fill and no producer answers is asked for on the
        # canvas, where the user has one. A declined drawing is not a value: the slot's
        # own refusal below is what a reader sees.
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
    label = _data_step_label(decl.name)
    if decl.is_context:
        return await _context(env, decl, label)
    value = await _produced(env, producer, label)
    return await _ingested(env, decl, value,
                           _fetcher_row(producer.runner, decl.data_class))


async def _matched(env: _Env, decl: DataDecl) -> Any:
    """Fill a slot that states a NEED, through the match.

    Every slot takes the ONE source its class matched, the bed included; what covers
    the rest is a layer the person composes and hands the slot, never this fill's."""
    await _somewhere_to_ask(env)
    if decl.role == BED:
        return await _ingested(env, decl, await _bed_surface(env, decl))
    # The INGESTION rides INSIDE the probe: a source whose rows the slot finds nothing
    # usable in held nothing for this run, so the next survivor takes its turn.
    beside = await _beside(env, decl)
    choice, value = await _probe(
        env, decl, decl.data_class, decl.name,
        read=lambda answer, row: _ingested(env, decl, answer, row, beside))
    if value is None:
        if decl.is_optional or decl.is_context:
            # A slot nothing measured is an absence the run STATES - the run's own
            # sentence, and the row's beside it where the row wrote one. The refusal
            # below is for a slot the run cannot stand without.
            env.absences.append(choice.sentence if decl.is_optional else
                                f"{decl.context_sentence} ({choice.sentence})")
            return None
        raise StepFailedError(choice.sentence, error_code="DATA_NEED_UNMATCHED",
                              step=_data_step_label(decl.name))
    return value


async def _beside(env: _Env, decl: DataDecl) -> dict[str, Any]:
    """The rows this SLOT declared beside the one its own class matched.

    Each is produced here, under this slot, through the match; the slot fetches
    nothing itself. Empty for every slot and kind that declares none."""
    from trid3nt_server.inputs.slots import needs_of

    seed = await _seed(env, decl)
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
    """THE BED: the ONE row the match ranked first, as it came.

    Nothing is gridded here: soundings refuse at the slot's ingestion, and water nothing
    measured refuses there naming the steps that compose a covering bed."""
    choice, top = await _probe(env, decl, decl.data_class,
                               f"{decl.name} {decl.data_class}")
    if top is None:
        raise StepFailedError(choice.sentence, error_code="DATA_NEED_UNMATCHED",
                              step=_data_step_label(decl.name))
    return top


async def _ranked(env: _Env, decl: DataDecl, data_class: str,
                  label: str) -> SourceChoice:
    """What the match RANKS for this slot over one class, nothing produced.

    A named row is matched for the class it serves, so its ask is the one that class states."""
    return match(await _need(env, decl, data_class, label),
                 sources_with_coverage())


async def _probe(env: _Env, decl: DataDecl, data_class: str, label: str, *,
                 read: Any = None) -> tuple[SourceChoice, Any]:
    """Match a class, then CALL the survivors in rank order -> the first answer.

    A source holding nothing over this domain is dropped and the next takes its turn;
    ``read`` is the slot's own reading, so an unreadable record drops out like an empty one."""
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
    """One producer row the RUNTIME declares, registered so it is produced like a question's own."""
    row = DataDecl(name=name, producer=Producer(runner=runner, kwargs=dict(ask),
                                                row=name))
    env.data[name] = row
    return row


def _spec_of(fetcher: str) -> Any:
    from trid3nt_server.tools.fetchers._router.registration import get_spec

    return get_spec(fetcher)


def _fetcher_row(fetcher: str, data_class: str, kind: str = "") -> Any:
    """The row THIS source states about THIS class, or ``None``.

    Where a source serves one class two ways (a record beside a prediction) the ranked
    ``kind`` picks the row, since each publishes its own columns and units."""
    if not data_class:
        return None
    return next((row for row in getattr(_spec_of(fetcher), "coverage", ())
                 if row.data_class == data_class
                 and (not kind or row.kind == kind)), None)


def _matched_row(choice: SourceChoice, fetcher: str) -> Any:
    """THE ROW the match produced, or ``None`` where it picked nothing.

    Named by the class asked for and the kind ranked, so a slot is told the statement of the row that answered."""
    kind = next((row.kind for row in choice.rows
                 if row.fetcher == fetcher and not row.excluded), "")
    return _fetcher_row(fetcher, choice.need, kind) if fetcher else None


def _mesh_m(env: _Env) -> float | None:
    return env.params.value_of("mesh_resolution_m") if env.params else None


async def _need(env: _Env, decl: DataDecl, data_class: str,
                label: str) -> Need:
    """What this slot asks the world for, assembled off the RUN.

    The class is the slot's; the place is the domain's or the row's own point; the
    window is the run's and the frame is the lever's."""
    from ..workflows.runtime.levers import run_frame

    seed = await _seed(env, decl)
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

    The domain asks for its own KIND: the template's, else the one the SEED stands on,
    read off the seed because which water a place is is a fact of the place."""
    from trid3nt_server.inputs.domain import place_kind

    if decl.role == DOMAIN:
        return decl.kind or place_kind(seed)
    return str(decl.observes or "")


async def _somewhere_to_ask(env: _Env) -> None:
    """Make sure the run has a PLACE before a source is asked for one.

    A domain CUT out of a box has no polygon until the cut runs, so the extent slot fills first."""
    if current_domain() is not None:
        return
    row = next((r for r in env.data.values() if r.role == EXTENT), None)
    if row is not None:
        await _produce(env, row)


def _water() -> CoverageExtent | None:
    """The domain's own outline, which is the water a gauge has to stand on.

    The polygon where the domain has one, else its box, and the note says WHICH."""
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

    A box is not a cut: a bed restricted to one would leave dry ground unpainted."""
    dom = current_domain()
    return dict(dom.geometry) if dom is not None and dom.geometry else None


def _pick(env: _Env, decl: DataDecl, data_class: str) -> str:
    """The source this RUN names for this slot, "" where it names none.

    A slot reading two classes takes the name on the class the named source serves."""
    named = str(env.picks.get(decl.name) or "")
    if not named:
        return ""
    if not any(getattr(_spec_of(named), "coverage", ())):
        raise PlanValidationError(
            f"the run picks {named!r} for the {decl.name!r} slot and it states "
            "no coverage at all: a source the match never weighs cannot be "
            "picked out of its list.")
    return named if _fetcher_row(named, data_class) is not None else ""


def _closes(opens: Any, window_s: float | None) -> str | None:
    """When the run's window closes, off the deck's own length."""
    if not opens or window_s is None:
        return None
    started = instant(opens)
    if started is None:
        return None
    return (started + timedelta(seconds=float(window_s))).isoformat()


def _place(seed: Any) -> tuple[float | None, float | None]:
    """The point a slot's coverage is tested at: what the row ranks against, else the domain's centre."""
    from trid3nt_server.inputs.point import lonlat_of

    # THE POINT a row is asked at, in whatever shape the question stated it: a pick, a
    # pair, a drawn feature. A value no place reads out of is no place, not a refusal.
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

    A box or a seed for the place, two dates for a window; the matched ROW closes the
    ask and maps its generic attributes onto the params this source states them in."""
    dom = current_domain()
    seed = await _seed(env, decl)
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

    A surface stopping exactly at the edge leaves the edge nodes standing on nothing."""
    west, south, east, north = (float(v) for v in bbox)
    pad = max(3.0 * float(mesh_m or 0.0), 100.0) / 111_320.0
    lon_pad = pad / max(math.cos(math.radians((south + north) / 2.0)), 0.1)
    return [west - lon_pad, south - pad, east + lon_pad, north + pad]


async def _domain_companion(env: _Env, named: str) -> Any:
    """One geometry the DOMAIN's producer measured beside its polygon, or ``None``.

    Read on demand, so a question that never needs the companion never asks for the domain."""
    row = next((r for r in env.data.values() if r.role == DOMAIN), None)
    if row is None:
        return None
    bound = await read(env, row.name)
    return dict(getattr(bound, "companions", None) or {}).get(named)


async def _ingested(env: _Env, decl: DataDecl, value: Any,
                    row: Any = None, beside: Mapping[str, Any] | None = None
                    ) -> Any:
    """A SLOT's value through the one ingestion its role reads; a plain row's value as it came.

    Runs off the loop: reading a layer's geometry is object-store IO."""
    if not decl.role:
        return value
    from trid3nt_server.inputs.slots import ingest_slot

    coercion = dict(decl.coercion)
    if "near" in coercion:
        coercion["near"] = await _seed(env, decl)
    coercion.update(_what_the_run_calls_it(
        env, decl, _asked_of(decl, coercion.get("near"))))
    coercion.update(_the_window_it_is_cut_from(decl))
    coercion.update(await _on_the_run_s_frame(env, decl, value))
    coercion.update(_what_the_record_reports(env, decl, row))
    # WHAT THE SLOT DECLARED BESIDE the matched row, and how far the question
    # reaches: the ingestion that states a need is the ingestion that reads it.
    coercion.update(dict(beside or {}))
    if beside:
        coercion["span_km"] = decl.span_km
    if decl.role == BED:
        # THE CUT IS WHERE THE WATER IS: the bed states its coverage over it and
        # refuses a wet hole, and an empty one says the run was cut with none.
        coercion["water"] = _cut_polygon() or {}
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
        # A QUESTION WHOSE DOMAIN IS CUT OUT OF A BOX has no polygon until the cut runs,
        # and the cut's own sources have to be asked somewhere: the window the question
        # was asked in is that place until the domain slot supersedes it.
        bind_domain(Domain(bbox=tuple(ingested.bbox), geometry={},
                           label=ingested.name))
    return ingested


def _the_window_it_is_cut_from(decl: DataDecl) -> dict[str, Any]:
    """The BOX a domain slot cuts a land-water edge against, where the run has one.

    Empty once a polygon is bound: a domain that arrived closed is cut against nothing."""
    if decl.role != DOMAIN:
        return {}
    dom = current_domain()
    if dom is None or dom.geometry or not dom.bbox:
        return {}
    return {"extent": tuple(float(v) for v in dom.bbox)}


def _what_the_run_calls_it(env: _Env, decl: DataDecl,
                           observed: str) -> dict[str, Any]:
    """The UNIT this slot converts to and the NOUN the run says it in.

    The unit is the keyword's, fixed by the transform; a row OBSERVING a published
    variable is read in that variable's unit, else in the unit the record was measured in."""
    told: dict[str, Any] = {}
    unit = (_observed_unit(env, decl, observed)
            if observed and decl.slot.record
            else env.slot_units.get(decl.role))
    if unit:
        told["to_units"] = unit
    caption = env.captions.get(decl.name)
    if caption:
        told["caption"] = str(caption)
    return told


def _observed_unit(env: _Env, decl: DataDecl, observed: str) -> str:
    """The unit the variable this row OBSERVES is published in.

    A name published under no stated unit refuses: pairing a measurement with a
    variable in another unit would call the difference the model's error."""
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

    Every word is THE MATCHED ROW's own statement, never read across the source's other
    rows; a record with no unit column is read in its measured unit. A record stopping early refuses."""
    if not decl.slot.record:
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
    if env.params is not None:
        told["at"] = env.params.value_of("event_time")
    return told


async def _on_the_run_s_frame(env: _Env, decl: DataDecl,
                              value: Any) -> dict[str, Any]:
    """What an ELEVATION slot is told about the run's own vertical frame.

    Only the bed and the level are elevations; another slot handed a frame would demand a
    datum of a temperature. A source on ANOTHER frame is bridged by the offset row."""
    from ..workflows.runtime.levers import run_frame

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
    """The point the QUESTION named, off the row that stands the run on a place, or ``None``.

    Another row's own point ranks a reporting site and is not the question's place."""
    from trid3nt_server.inputs.point import lonlat_of

    row = next((r for r in env.data.values() if r.role in (DOMAIN, EXTENT)
                and r.coercion.get("near") is not None), None)
    if row is None:
        return None
    return lonlat_of(await _seed(env, row))


async def _offset_row(env: _Env, owner: str, value: Any, frame: str) -> Any:
    """The measured shift onto the run's frame, as a DATA row the RUNTIME declares.

    Asked like any fact about the world, with a journal line naming the service. ``None``
    where no row is owed, which leaves the alignment to refuse. ``owner`` names the row."""
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
    """A CONTEXT row: produced where the source has something, absent where it has not.

    Only an empty SOURCE is an absence; a cancelled run or a retryable gate error is not."""
    try:
        value = await _produced(env, decl.producer, label)
        # The ingestion is INSIDE the absence: rows the slot finds nothing usable in
        # held nothing, and a context row says so rather than refusing.
        ingested = await _ingested(env, decl, value, _fetcher_row(
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
    return ingested


def _malformed_ask(exc: BaseException) -> bool:
    """Is this the ASK being wrong rather than the source holding nothing?

    A producer's refusal arrives inside the step that called it, so the whole chain is read."""
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


async def _produced(env: _Env, producer: Producer, label: str) -> Any:
    """Call one producer with its stated kwargs -> what it answered."""
    kwargs = dict(producer.kwargs)
    async with substep(current_emitter(), producer.runner.rsplit(".", 1)[-1]):
        return await _call_runner(producer.runner, kwargs, label)


def _validate_supplied(env: _Env, decl: DataDecl, supplied: Any,
                      validate: Any) -> None:
    """Two checks and no third before a supplied artifact is adopted: the slot's declared
    SHAPE against its class, and under ``CoversAOI`` that a domain with an extent is bound."""
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
        # A workflow that DECLARES a domain slot carries its own: the slot binds it the
        # moment it is filled, so a row produced before it is adopted for its declared shape.
        return
    raise SuppliedCoverageError(
        f"the artifact supplied for {decl.name!r} cannot be checked against the "
        "modelled domain: no domain is bound. Resolve the AOI before supplying one."
    )



ACCEPTED, REJECTED, MISSING, DEFAULTED = (
    "accepted", "rejected", "missing", "defaulted")

#: What rides a fill beside its inputs and is carried as stated: how the run is
#: reviewed, and whether a kept mesh is rebuilt.
_CARRIED = ("input_mode", "restart_clean")


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
    notes: list[str] = field(default_factory=list)
    env: _Env | None = None
    domain: Domain | None = None
    #: What producing an input said, chose and covered, in that order: the fill
    #: runs before the run's own channels open, so the launch restates them.
    said: tuple[list[str], list[Any], list[str]] = field(
        default_factory=lambda: ([], [], []))

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

    A value is a literal, ``{"layer": id}`` or ``{"source": name}``; a sourced or layer
    input is fetched and ingested first. Inputs not named keep their verdicts."""
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
    state.stated.update(values)
    await _seat(state)
    for name in state.workflow.unnamed():
        state.inputs[name] = Verdict(
            REJECTED, name, code="INPUT_UNNAMED",
            reason=f"the template names {name!r} as an input, and no param, "
            "row or product of this run is called that.")
    tokens = (journal.bind_notes(), journal.bind_choices(),
              journal.bind_coverage())
    try:
        for name, value in sourced.items():
            await _row(state, rows[name], value)
        # EVERY ROW THE TEMPLATE NAMES is produced here, before anything
        # expands: a consumer only reads the mapping, so a row that cannot be
        # produced is refused now, by name.
        for name in state.workflow.named_rows() if state.ready else ():
            if name not in production(state).run:
                await _row(state, rows[name], None)
    finally:
        for held, drain, token in zip(state.said, (
                journal.drain_notes, journal.drain_choices,
                journal.drain_coverage), tokens):
            held += drain(token)
    return state


def restate(state: Fill) -> None:
    """Say on the run in progress what producing its inputs said at the fill."""
    notes, choices, covered = state.said
    for text in notes:
        (journal.cut_coverage if text in covered else journal_note)(text)
    for choice in choices:
        slot_choice(choice)


def _keyword(state: Fill, name: str, value: Any) -> None:
    """One engine keyword through the module's own accept rule; a file keyword filled with a layer takes it as the file."""
    if isinstance(value, Mapping) and "layer" in value:
        value = value["layer"]
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
    from ..workflows.runtime.resolver import seat_param

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


def production(state: Fill) -> _Env:
    """The state a sourced input is produced under, built once per fill."""
    wf = state.workflow
    if state.env is None:
        state.env = _Env(
            params=state.params, data={d.name: d for d in wf.data},
            input_mode=state.carried.get("input_mode"),
            keywords=dict(state.keywords), workflow=wf.name,
            window_s=wf.run_window_s(dict(state.keywords)),
            slot_units=wf.slot_units(), captions=wf.captions,
            published_units=wf.published_units())
        state.env.run.update(state.params.values_dict() if state.params else {})
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
    origin = "user" if value is not None else "produced"
    if isinstance(value, Mapping) and "source" in value:
        env.picks[decl.name] = origin = str(value["source"])
        origin = f"source:{origin}"
    elif value is not None:
        env.supplied[decl.name] = (value.get("layer")
                                   if isinstance(value, Mapping) else value)
    try:
        if decl.role not in (DOMAIN, EXTENT):
            await _place_first(env)
        held = env.run[decl.name] = await _produce(env, decl)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - the refusal is the verdict
        if getattr(exc, "retryable", False):
            raise
        picked = env.picks.pop(decl.name, None)
        env.supplied.pop(decl.name, None)
        state.inputs[decl.name] = Verdict(
            REJECTED, value, code=_refusal_code(exc, decl.name),
            reason=str(exc) if value is not None
            else f"{decl.name!r} could not be produced: {exc}",
            remedies=await _instead(env, decl, picked) if picked else ())
        return
    state.domain = current_domain()
    state.inputs[decl.name] = Verdict(ACCEPTED, held, origin=origin)


def _refusal_code(exc: BaseException, name: str) -> str:
    """The code a refused input carries; an error stating none is a fault, logged with its trace."""
    code = getattr(exc, "error_code", None)
    if code is None:
        logger.warning("input %s refused on an untyped error", name,
                       exc_info=exc)
    return code or "INPUT_REFUSED"


async def _place_first(env: _Env) -> None:
    """A sourced input stands on the run's place, so the place is filled first.

    Producing a row may register runtime rows, so the loop is over the rows as they stood."""
    for row in list(env.data.values()):
        if row.role == DOMAIN and row.name not in env.run:
            env.run[row.name] = await _produce(env, row)


async def _instead(env: _Env, decl: DataDecl, picked: str) -> tuple[str, ...]:
    """The sources the match ranks for this input that a refused pick is not."""
    try:
        choice = await _ranked(env, decl, decl.data_class, decl.name)
    except Exception:  # noqa: BLE001 - no ranking is no remedy to name
        return ()
    return tuple(row.fetcher for row in choice.rows
                 if not row.excluded and row.fetcher != picked)


async def _call_runner(runner: str, kwargs: dict[str, Any], label: str) -> Any:
    """Call a named runner inside the typed error family."""
    return await call(_load(runner), kwargs, label)


async def call(fn: Any, kwargs: dict[str, Any], label: str) -> Any:
    """Call a function, sync or async; what it raises untyped arrives typed with
    its cause, and a retryable gate is raised as it came."""
    try:
        out = fn(**kwargs)
        return await out if inspect.isawaitable(out) else out
    except asyncio.CancelledError:
        raise
    except DeclarativeError:
        raise
    except Exception as exc:  # noqa: BLE001 - re-raised typed, cause preserved
        if getattr(exc, "retryable", False):
            raise
        raise StepFailedError(
            f"step {label!r} failed: {said(exc)}",
            error_code=getattr(exc, "error_code", None) or "STEP_FAILED",
            step=label, cause=exc,
        ) from exc


def _load(runner: str) -> Any:
    """The runner a row names: a REGISTERED TOOL first, then a dotted import path.

    A name that resolves as BOTH refuses rather than letting lookup order decide."""
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
