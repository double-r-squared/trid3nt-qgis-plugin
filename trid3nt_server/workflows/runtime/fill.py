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

from trid3nt_contracts.common import Origin
from trid3nt_contracts.coverage import CoverageExtent, SourceChoice

from trid3nt_server.inputs.geometry import METRES_PER_DEGREE
from trid3nt_server.inputs.instant import NO_MOMENT_WORD
from trid3nt_server.render.pipeline_emitter import current_emitter, substep
from trid3nt_server.render.uri_registry import lookup_uri_for_handle
from trid3nt_server.tools.search.match import (
    AROUND_PLACE_DEG, HELD_NOTHING, Need, dropped_from, filled, instant, match, sources_with_coverage)

from .data import Producer, refuse_wrong_shape
from .sheet import Keyword

from trid3nt_server.inputs.slots import (
    BED, DISCHARGE, DOMAIN, EXTENT, LEVEL, LINE, MESH, RUN, Slot)
from trid3nt_server.inputs.input import Input
from trid3nt_server.inputs.bound_domain import Domain, bind_domain, current_domain
from .errors import (DeclarativeError, StepFailedError,
                     SuppliedCoverageError, said)
from . import journal
from .journal import slot_choice
from trid3nt_server.inputs.run_notes import (
    bind_coverage, bind_notes, cut_coverage, drain_coverage, drain_notes, journal_note)
from trid3nt_server.inputs.user_input import nearest
from trid3nt_server.settings import bind_run, reset_run, split_keywords
from trid3nt_server.inputs.temporal import RATE, STATE

logger = logging.getLogger("trid3nt_server.workflows.runtime.fill")

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
    return await _taken(env, env.data[name])


async def given(env: "_Env", value: Any) -> Any:
    """A string names an input of the run and is read; any other value passes through."""
    return await read(env, value) if isinstance(value, str) else value


async def anchors_of(env: "_Env", outputs: Sequence[Any]) -> list[dict[str, Any]]:
    """Each output's at, along and within read off the run, in output order, to rejoin the primitive at publish."""
    return [{"at": await given(env, p.at), "along": await given(env, p.along),
             "within": await given(env, p.within)} for p in outputs]


async def _taken(env: "_Env", row: Input) -> Any:
    """Produce one row into the run's mapping and stamp where it came from:
    the caller's own where one was supplied, else the source or producer that answered."""
    held = env.run[row.name] = await _produce(env, row)
    env.inputs[row.name] = Verdict(
        ACCEPTED, held,
        origin=(None if held is None else Origin.USER if row.name in env.supplied
                else Origin.PRODUCER),
        source=env.sources.get(row.name) or getattr(row.producer, "runner", ""))
    return held


async def _seed(env: "_Env", decl: Input) -> Any:
    """The point a row is asked at, read off the input its ``at`` names."""
    near = decl.coercion.get("near")
    return await read(env, near) if isinstance(near, str) else near


@dataclass
class _Env:
    data: dict[str, Input]
    #: Every input's verdict, by name - the fill's own mapping, stamped here as well by a row first read during the launch.
    inputs: dict[str, "Verdict"] = field(default_factory=dict)
    #: The workflow this fill belongs to - what a gate card names as the asker.
    workflow: str = ""
    #: The source the run NAMES for a slot, by slot name. It picks among the
    #: survivors of that slot's match and never past its filters.
    picks: dict[str, str] = field(default_factory=dict)
    #: The source the match took for a row, by row name.
    sources: dict[str, str] = field(default_factory=dict)
    #: THE RUN'S ONE MAPPING: stated values, fetched rows, what each stage produced
    #: and the keywords so far, by plain name.
    run: dict[str, Any] = field(default_factory=dict)
    #: Artifacts SUPPLIED rather than produced - a layer handle, a file uri, a
    #: gate's answer. What satisfies a producer-less row.
    supplied: dict[str, Any] = field(default_factory=dict)
    #: How long the solve runs, in seconds, off the deck this run writes: the WINDOW a
    #: matched series source must cover, or the run refuses.
    window_s: float | None = None
    #: The name of the input holding the moment the run opens at; "" where the workflow names none.
    moment: str = ""
    #: The UNIT each slot's value is converted to, by type - the unit of the
    #: keyword that type fills, which the transform fixes. No row states one.
    slot_units: Mapping[Slot, str] = field(default_factory=dict)
    #: What the template CALLS each thing it publishes or measures, by published
    #: variable and by DATA row name; a measured row's caption is the noun its refusals
    #: and journal line are written about.
    captions: Mapping[str, str] = field(default_factory=dict)
    #: The unit each PUBLISHED variable is written in, by the name the result carries;
    #: a row that observes one is read in that unit.
    published_units: Mapping[str, str] = field(default_factory=dict)
    #: The engine lays its own ladder of bed surfaces, so the bed slot neither merges nor fills.
    lays_own_bed: bool = False


def _data_step_label(name: str) -> str:
    return f"data:{name}"


async def _produce(env: _Env, decl: Input) -> Any:
    """Satisfy one declared artifact, ON DEMAND - when a step that reads it runs.

    An artifact nothing reads costs no fetch; a stated-only row nothing hands in is
    the run's own machinery's answer, and carries no verdict here."""
    handed_in = env.supplied.get(decl.name)
    if handed_in is not None and decl.slot is BED and isinstance(handed_in, list):
        return await _ingested(env, decl, await _bed_list(env, decl, handed_in))
    if handed_in is not None:
        _validate_supplied(env, decl, handed_in)
        return await _ingested(env, decl, handed_in)
    if decl.stated_only:
        # The user MAY state one; unstated, the run's own machinery answers - the
        # supplied-mesh builder, the mesher's own edge. No verdict, no sentence.
        return None
    if decl.instead_of and (env.supplied.get(decl.instead_of) is not None
                            or decl.instead_of in env.picks):
        journal_note(f"{decl.name} is not read: the run states {decl.instead_of}, which it stands in for.")
        return None
    for standing in [row for row in env.data.values() if row.instead_of == decl.name]:
        if await read(env, standing.name) is not None:
            journal_note(f"{decl.name} is not asked for: {standing.name} fills in its place.")
            return None
    producer = decl.producer
    if producer is None and decl.slot is LINE:
        # THE DOMAIN'S PRODUCER measured this beside the polygon it cut - a reach's
        # centerline rides on the artifact it returned. A body whose producer measured
        # none is asked on the canvas next.
        measured = await _domain_companion(env, "centerline")
        if measured is not None:
            return measured
    if producer is None and decl.slot is RUN:
        # A FINISHED RUN NOBODY NAMED is this case's own, of an engine the row takes; several are offered on
        # the review card, none refuses.
        from trid3nt_server.inputs.run import case_run

        named = await case_run(tuple(decl.coercion.get("engines") or ()), label=decl.name, tool=env.workflow)
        return await _ingested(env, decl, named)
    if producer is None and decl.data_class:
        # A SLOT THAT STATES A NEED: the match reads every fetcher's row
        # and the runtime declares the row it picked, so the pick earns a
        # journal line like any other producer.
        return await _matched(env, decl)
    if producer is None and (decl.slot is not None or decl.geometry):
        # A ROW the caller did not fill and no producer answers is asked for on the
        # canvas - the shape its slot states or the shape its own need names. A
        # declined drawing is not a value: the refusal below is what a reader sees.
        from trid3nt_server.inputs.slots import ask_on_canvas

        drawn = await ask_on_canvas(decl.slot, geometry=decl.geometry or "",
                                    tool=env.workflow, param=decl.name)
        if drawn is not None:
            return drawn
    if producer is None:
        # A producer-less slot: nothing was handed in, and naming a default
        # fetcher for it would be this library inventing the source.
        raise StepFailedError(
            f"Input {decl.name!r} is a producer-less slot and nothing satisfied it: "
            "supply a layer or a file uri.",
            error_code="DATA_SLOT_UNSATISFIED", step=_data_step_label(decl.name),
        )
    label = _data_step_label(decl.name)
    value = await _produced(env, producer, label)
    return await _ingested(env, decl, value,
                           _fetcher_row(producer.runner, decl.data_class))


async def _matched(env: _Env, decl: Input) -> Any:
    """Fill a slot that states a NEED, through the match.

    Every slot takes the ONE source its class matched, the bed included; what covers
    the rest is a layer the person composes and hands the slot, never this fill's."""
    near = decl.coercion.get("near")
    if decl.slot is DOMAIN and isinstance(near, str) and await _seed(env, decl) is None:
        # The water a domain stands on is found from a point; with none stated it is never guessed.
        raise StepFailedError(
            f"the {decl.name!r} is found from a point on the water, {near!r}, and "
            f"none was stated: state {near} on the water this run is about, or "
            f"draw or supply {decl.name} itself.",
            error_code="DATA_NEED_UNPLACED", step=_data_step_label(decl.name))
    await _somewhere_to_ask(env)
    if decl.slot is BED:
        return await _bed_surface(env, decl, lambda laid: _ingested(env, decl, laid))
    if decl.slot is DOMAIN and not _asked_of(decl, await _seed(env, decl)):
        decl = await _kind_of_the_water_under(env, decl)
    # The INGESTION rides INSIDE the probe: a source whose rows the slot finds nothing
    # usable in held nothing for this run, so the next survivor takes its turn.
    beside = await _beside(env, decl)
    choice, value = await _probe(
        env, decl, decl.data_class, decl.name,
        read=lambda answer, row: _ingested(env, decl, answer, row, beside))
    if value is None and not decl.optional:
        raise StepFailedError(choice.sentence, error_code="DATA_NEED_UNMATCHED",
                              step=_data_step_label(decl.name))
    return value


async def _beside(env: _Env, decl: Input) -> dict[str, Any]:
    """The rows this SLOT declared beside the one its own class matched.

    Each is produced here, under this slot, through the match; the slot fetches
    nothing itself. Empty for every slot and kind that declares none."""
    from trid3nt_server.inputs.slots import needs_of

    seed = await _seed(env, decl)
    held: dict[str, Any] = {}
    for name, word, geometry in needs_of(decl.slot, _asked_of(decl, seed)):
        aside = dataclasses.replace(decl, name=f"{decl.name}_{name}", kind="",
                                    slot=None, observes=word, geometry=geometry)
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


async def _kind_of_the_water_under(env: _Env, decl: Input) -> Input:
    """A domain whose seed arrived with no kind takes the kind of the mapped water it stands IN.

    The water surface is asked through the match as the banks are; no water there, or water of
    another kind, leaves the domain matched as it came."""
    from trid3nt_server.inputs.domain import kind_under
    from trid3nt_server.inputs.point import lonlat_of

    seed = await _seed(env, decl)
    if lonlat_of(seed) is None:
        return decl
    aside = dataclasses.replace(decl, name=f"{decl.name}_water", kind="", slot=None,
                                observes="water surface", geometry="polygon")
    _choice, water = await _probe(env, aside, decl.data_class, aside.name)
    kind = "" if water is None else await asyncio.to_thread(kind_under, water, seed)
    if not kind:
        return decl
    journal_note(f"{decl.name}: the point it is found from stands in a river's own mapped water (NHD "
                 "StreamRiver), so the domain is the reach walked downstream from it.")
    return dataclasses.replace(decl, kind=kind)


async def _bed_list(env: _Env, decl: Input, listed: list[Any]) -> list[Any]:
    """An ORDERED LIST handed the bed: each surface as it came, a ``{"source": name}`` among them fetched
    through the match under its own row, in the order handed - which is the order they win."""
    laid = []
    for rank, item in enumerate(listed, start=1):
        if not (isinstance(item, Mapping) and "source" in item):
            _validate_supplied(env, decl, item)
            laid.append(item)
            continue
        laid.append(await _named_surface(env, decl, rank, str(item["source"])))
    return laid


async def _named_surface(env: _Env, decl: Input, rank: int, named: str) -> Any:
    """The surface the NAMED source holds for this bed, matched on the bed class it serves; one serving none
    of them, or holding nothing here, refuses rather than another source taking its place in the order."""
    served = next((c for c in (decl.data_class, *sorted(BED.classes))
                   if c and _fetcher_row(named, c) is not None), "")
    if not served:
        raise StepFailedError(
            f"surface {rank} of the {decl.name!r} names {named!r}, which serves none of the bed's classes "
            f"({', '.join(sorted(BED.classes))}).", error_code="DATA_NEED_UNMATCHED",
            step=_data_step_label(decl.name))
    row = dataclasses.replace(decl, name=f"{decl.name}_{rank}", data_class=served)
    env.picks[row.name] = named
    choice, top = await _probe(env, row, served, f"{row.name} {served}")
    if top is None or choice.picked != named:
        raise StepFailedError(
            f"surface {rank} of the {decl.name!r} names {named!r}, and it holds nothing here: "
            f"{choice.sentence}", error_code="DATA_NEED_UNMATCHED", step=_data_step_label(decl.name))
    return top


async def _bed_surface(env: _Env, decl: Input, ingest: Any = None) -> Any:
    """THE BED: the row the match ranked first, then each next ranked surface while the water is left a wet hole.

    ``ingest`` reads what is laid; only BED_WET_HOLE continues the walk, and an engine laying its own bed
    takes the one row. Soundings are laid and filled at the slot, not here."""
    choice, top = await _probe(env, decl, decl.data_class,
                               f"{decl.name} {decl.data_class}")
    if top is None:
        raise StepFailedError(choice.sentence, error_code="DATA_NEED_UNMATCHED",
                              step=_data_step_label(decl.name))
    if ingest is None or env.lays_own_bed:
        return await ingest(top) if ingest is not None else top
    from trid3nt_server.inputs.user_input import UserInputError

    laid, won = [top], [choice.picked]
    while True:
        try:
            ingested = await ingest(laid[0] if len(laid) == 1 else list(laid))
        except Exception as hole:  # noqa: BLE001 - only a wet hole walks on
            if getattr(hole, "error_code", None) != "BED_WET_HOLE":
                raise
            nxt, more = await _probe(env, decl, decl.data_class,
                                     f"{decl.name} {decl.data_class}", skip=tuple(won))
            if more is None:
                raise UserInputError(f"{hole} Surfaces tried, in rank order: {', '.join(won)}.",
                                     code="BED_WET_HOLE") from hole
            laid.append(more)
            won.append(nxt.picked)
            continue
        if len(won) > 1:
            journal_note(f"{decl.name}: the surfaces won in this order, each measuring where the one before did "
                         f"not: {', '.join(won)}.")
        return ingested


async def _ranked(env: _Env, decl: Input, data_class: str,
                  label: str) -> SourceChoice:
    """What the match RANKS for this slot over one class, nothing produced.

    A named row is matched for the class it serves, so its ask is the one that class states."""
    return match(await _need(env, decl, data_class, label),
                 sources_with_coverage())


async def _probe(env: _Env, decl: Input, data_class: str, label: str, *,
                 read: Any = None, skip: Sequence[str] = ()) -> tuple[SourceChoice, Any]:
    """Match a class, then CALL the survivors in rank order -> the first answer.

    A source holding nothing over this domain is dropped and the next takes its turn;
    ``read`` is the slot's own reading, so an unreadable record drops out like an empty one;
    ``skip`` names the sources already laid, which are not asked again."""
    choice = match(await _need(env, decl, data_class, label),
                   sources_with_coverage())
    for laid in skip:
        choice = dropped_from(choice, laid, "already laid")
    if skip and not choice.picked:
        return choice, None
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
                                  f"{HELD_NOTHING} ({exc})")
            continue
        slot_choice(choice)
        journal_note(choice.sentence)
        env.sources[decl.name] = choice.picked
        return choice, value
    slot_choice(choice)
    journal_note(choice.sentence)
    return choice, None


def _runtime_row(env: _Env, name: str, runner: str,
                 ask: Mapping[str, Any]) -> Input:
    """One producer row the RUNTIME declares, registered so it is produced like a question's own."""
    row = Input(Producer(runner=runner, kwargs=dict(ask), row=name), name=name)
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
    from trid3nt_server.tools.mesh.recipe import RESOLUTION

    return env.run.get(RESOLUTION.name)


def _span_km(env: _Env, decl: Input) -> float | None:
    """The reach a span-stating row covers: the run's reach_length_km where it states one, else the need's own."""
    from trid3nt_server.inputs.user_input import UserInputError
    from .workflow import REACH_LENGTH

    stated = env.run.get(REACH_LENGTH.name)
    if stated is None or decl.span_km is None:
        return decl.span_km
    if not float(stated) > 0.0:
        raise UserInputError(
            f"{REACH_LENGTH.name}={stated} km is not a reach: it is the length of the line the domain is cut to, "
            f"so it is greater than 0 and a value past the line's own end takes the whole line. "
            f"Omit it for this template's {decl.span_km:g} km.", code="REACH_LENGTH_INVALID")
    return float(stated)


def _frame(env: _Env) -> str:
    """The vertical frame this run counts elevations from: the one the bed states, for every reader of it."""
    frame, = BED.states
    return str(env.run.get(frame.name) or frame.default)


async def _need(env: _Env, decl: Input, data_class: str,
                label: str) -> Need:
    """What this slot asks the world for, assembled off the RUN.

    The class is the slot's; the place is the domain's or the row's own point; the
    window is the run's and the frame is the bed's."""
    seed = await _seed(env, decl)
    lon, lat = _place(seed)
    opens = _opens(env, decl)
    return Need(slot=label, data_class=data_class, lon=lon, lat=lat,
                geometry=decl.geometry or getattr(decl.slot, "reads", ""),
                opens=str(opens) if opens else None,
                until=_closes(opens, _window(env, decl)), frame=_frame(env),
                mesh_m=_mesh_m(env), pick=_pick(env, decl, data_class),
                of=_asked_of(decl, seed), measured=_measured(env, decl),
                water=_water())


def _asked_of(decl: Input, seed: Any) -> str:
    """WHAT OF ITS CLASS this row asks the world for.

    The domain asks for its own KIND: the template's, else the one the SEED stands on,
    read off the seed because which water a place is is a fact of the place."""
    from trid3nt_server.inputs.domain import place_kind

    if decl.slot is DOMAIN:
        return decl.kind or place_kind(seed)
    return str(decl.observes or "")


async def _somewhere_to_ask(env: _Env) -> None:
    """Make sure the run has a PLACE before a source is asked for one.

    A domain CUT out of a box has no polygon until the cut runs, so the extent slot fills first."""
    if current_domain() is not None:
        return
    row = next((r for r in env.data.values() if r.slot is EXTENT), None)
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


def _pick(env: _Env, decl: Input, data_class: str) -> str:
    """The source this RUN names for this slot, "" where it names none.

    A slot reading two classes takes the name on the class the named source serves."""
    named = str(env.picks.get(decl.name) or "")
    if not named:
        return ""
    if not any(getattr(_spec_of(named), "coverage", ())):
        raise StepFailedError(
            f"the run picks {named!r} for the {decl.name!r} slot and it states "
            "no coverage at all: a source the match never weighs cannot be "
            "picked out of its list.", error_code="DATA_NEED_UNMATCHED")
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
                   decl: Input) -> dict[str, Any]:
    """What a matched source is CALLED with, read off its own declared params.

    A box or a seed for the place, two dates for a window; the matched ROW closes the
    ask and maps its generic attributes onto the params this source states them in."""
    dom = current_domain()
    seed = await _seed(env, decl)
    lon, lat = _place(seed)
    opens = _opens(env, decl)
    return filled(
        choice, decl.name.replace("_", " "),
        _around(dom.bbox, _mesh_m(env)) if dom is not None and dom.bbox
        else None if lon is None or lat is None else _about(lon, lat, _span_km(env, decl)),
        Need(slot=decl.name, data_class=choice.need, lon=lon, lat=lat,
             opens=str(opens) if opens else None,
             until=_closes(opens, _window(env, decl))),
        {"span_km": _span_km(env, decl), "of": _asked_of(decl, seed) or None,
         "measured": _measured(env, decl) or None,
         "seed_point": None if lon is None or lat is None else [lon, lat]})


async def _searched_in(env: _Env, decl: Input) -> list[float] | None:
    """The box a need is asked in: the bound domain's box with its margin, else the stated point reached the row's span."""
    dom = current_domain()
    if dom is not None and dom.bbox:
        return _around(dom.bbox, _mesh_m(env))
    lon, lat = _place(await _seed(env, decl))
    return None if lon is None or lat is None else _about(lon, lat, _span_km(env, decl))


def _around(bbox: Sequence[float], mesh_m: float | None) -> list[float]:
    """The domain's box with a MARGIN, which is what a slot asks a source for.

    A surface stopping exactly at the edge leaves the edge nodes standing on nothing."""
    west, south, east, north = (float(v) for v in bbox)
    pad = max(3.0 * float(mesh_m or 0.0), 100.0) / METRES_PER_DEGREE
    lon_pad = pad / max(math.cos(math.radians((south + north) / 2.0)), 0.1)
    return [west - lon_pad, south - pad, east + lon_pad, north + pad]


def _about(lon: float, lat: float, span_km: float | None) -> list[float]:
    """The box a search is placed in while the run has no domain: the stated point, reached the row's own span.

    A row stating no span asks the ground the point stands on, the margin a point is read with everywhere."""
    pad = AROUND_PLACE_DEG if span_km is None else float(span_km) * 1000.0 / METRES_PER_DEGREE
    lon_pad = pad / max(math.cos(math.radians(lat)), 0.1)
    return [lon - lon_pad, lat - pad, lon + lon_pad, lat + pad]


async def _domain_companion(env: _Env, named: str) -> Any:
    """One geometry the DOMAIN's producer measured beside its polygon, or ``None``.

    Read on demand, so a question that never needs the companion never asks for the domain."""
    row = next((r for r in env.data.values() if r.slot is DOMAIN), None)
    if row is None:
        return None
    bound = await read(env, row.name)
    return dict(getattr(bound, "companions", None) or {}).get(named)


async def _ingested(env: _Env, decl: Input, value: Any,
                    row: Any = None, beside: Mapping[str, Any] | None = None
                    ) -> Any:
    """A SLOT's value through the one ingestion its type reads; a plain row's value as it came.

    Runs off the loop: reading a layer's geometry is object-store IO."""
    if decl.slot is None:
        return value
    from trid3nt_server.inputs.slots import ingest_slot

    coercion = dict(decl.coercion)
    if "near" in coercion:
        coercion["near"] = await _seed(env, decl)
        if coercion["near"] is None and decl.slot.ingestion == "observation":
            # A record's sites rank against the question's own point where the row names none:
            # ranked against nothing, the source's row order picks, a tributary's gauge as soon as the reach's own.
            coercion["near"] = await _question_seed(env)
    if decl.slot.ingestion == "observation" and row is not None:
        off = await _off_the_reach(env, value)
        if off:
            coercion["off"] = off
    coercion.update(_what_the_run_calls_it(
        env, decl, _asked_of(decl, coercion.get("near"))))
    coercion.update(_the_window_it_is_cut_from(decl))
    coercion.update(_what_the_record_reports(env, decl, row))
    coercion.update(await _on_the_run_s_frame(env, decl, value, coercion))
    # WHAT THE SLOT DECLARED BESIDE the matched row, and how far the question
    # reaches: the ingestion that states a need is the ingestion that reads it.
    coercion.update(dict(beside or {}))
    if beside:
        coercion["span_km"] = _span_km(env, decl)
    if decl.slot is BED:
        # THE CUT IS WHERE THE WATER IS: the bed states its coverage over it, fills
        # inside it and refuses a wet hole, and an empty one says the run was cut with none.
        coercion["water"] = _cut_polygon() or {}
        coercion["merge"] = not env.lays_own_bed
    if decl.slot is DOMAIN and row is not None:
        # A domain the run's own SEARCH found is cut to the box it was searched in; one handed in is never cut.
        searched = await _searched_in(env, decl)
        if searched is not None:
            coercion["window"] = searched
    if decl.slot is MESH:
        # The domain is filled before any other row, so a supplied mesh is held to the ground this run solves over.
        coercion["over"] = getattr(current_domain(), "bbox", None)
    ingested = await asyncio.to_thread(ingest_slot, decl.slot, value,
                                       label=decl.name, **coercion)
    if decl.slot is DOMAIN and ingested is not None:
        # THE DOMAIN a run solves over IS the run's domain from the moment its
        # slot is filled: what a supplied artifact is checked against, and what
        # a later fetch is bounded by. Nothing else has to acquire an AOI first.
        bind_domain(Domain(bbox=tuple(ingested.bbox),
                           geometry=dict(ingested.geometry),
                           label=ingested.name))
    elif decl.slot is RUN and ingested is not None and current_domain() is None and ingested.bbox:
        # A RUN OVER A FINISHED RUN stands where that run was solved: its mesh's box is the place
        # every later row is asked for.
        bind_domain(Domain(bbox=tuple(ingested.bbox), geometry={}, label=f"run {ingested.run_id}"))
    elif decl.slot is EXTENT and ingested is not None and current_domain() is None:
        # A QUESTION WHOSE DOMAIN IS CUT OUT OF A BOX has no polygon until the cut runs,
        # and the cut's own sources have to be asked somewhere: the window the question
        # was asked in is that place until the domain slot supersedes it.
        bind_domain(Domain(bbox=tuple(ingested.bbox), geometry={},
                           label=ingested.name))
    return ingested


async def _off_the_reach(env: _Env, value: Any) -> dict[str | None, str]:
    """The sites of a matched record that are NOT this river run's water, each with why; empty off a river run.

    A site is the reach's when it stands inside the domain AND NLDI's NWIS index lists it on a
    flowline of the main stem walked UM and DM from the release, each the run's reach length."""
    from shapely.geometry import Point, shape

    from trid3nt_server.inputs.observation import sites

    row = next((r for r in env.data.values() if r.slot is DOMAIN), None)
    dom = current_domain()
    seed = await _question_seed(env)
    if row is None or dom is None or not dom.geometry or seed is None \
            or await _domain_companion(env, "centerline") is None:
        return {}
    water = shape(dom.geometry)
    span = _span_km(env, row)
    walk = {} if span is None else {"distance_km": span}
    reach = f"the main stem walked {span:g} km up and down from the release" if span is not None \
        else "the main stem walked up and down from the release"
    off: dict[str | None, str] = {}
    stem: frozenset[int] | None = None
    for site, _name, lon, lat in await asyncio.to_thread(sites, value):
        if lon is None or lat is None or not water.covers(Point(lon, lat)):
            off[site] = "stands outside the run's domain, the water the reach is cut to"
            continue
        if not site:
            off[site] = "states no site id, so NLDI's NWIS index cannot list it on a reach"
            continue
        if stem is None:
            stem = frozenset().union(*[
                await _walked(env, f"reach_stem_{way.lower()}",
                              {"seed_point": list(seed), "direction": way, **walk})
                for way in ("UM", "DM")])
        try:
            own = await _walked(env, f"gauge_{site}_reach",
                                {"nwis_site": str(site), "direction": "DM", "distance_km": 0.0})
        except StepFailedError as exc:
            # A retryable failure is raised as it came and never lands here.
            off[site] = f"is placed on no NHDPlus reach ({exc.cause or exc})"
            continue
        if not own & stem:
            off[site] = (f"stands on NHDPlus reach {', '.join(map(str, sorted(own)))}, which is not on {reach}")
    return off


async def _walked(env: _Env, name: str, ask: Mapping[str, Any]) -> frozenset[int]:
    """The COMIDs one walk of the network answered, as a DATA row the RUNTIME declares."""
    from trid3nt_server.inputs.domain import REACH_FETCH, flowline_ids

    if name not in env.data:
        _runtime_row(env, name, REACH_FETCH, ask)
    return await asyncio.to_thread(flowline_ids, await read(env, name))


def _the_window_it_is_cut_from(decl: Input) -> dict[str, Any]:
    """The BOX a domain slot cuts a land-water edge against, where the run has one.

    Empty once a polygon is bound: a domain that arrived closed is cut against nothing."""
    if decl.slot is not DOMAIN:
        return {}
    dom = current_domain()
    if dom is None or dom.geometry or not dom.bbox:
        return {}
    return {"extent": tuple(float(v) for v in dom.bbox)}


def _what_the_run_calls_it(env: _Env, decl: Input,
                           observed: str) -> dict[str, Any]:
    """The UNIT this slot converts to and the NOUN the run says it in.

    The unit is the keyword's, fixed by the transform; a row OBSERVING a published
    variable is read in that variable's unit, else in the unit the record was measured in."""
    told: dict[str, Any] = {}
    unit = (_observed_unit(env, decl, observed)
            if observed and decl.slot.record
            else env.slot_units.get(decl.slot))
    if unit:
        told["to_units"] = unit
    caption = env.captions.get(decl.name)
    if caption:
        told["caption"] = str(caption)
    return told


def _observed_unit(env: _Env, decl: Input, observed: str) -> str:
    """The unit the variable this row OBSERVES is published in.

    A name published under no stated unit refuses: pairing a measurement with a
    variable in another unit would call the difference the model's error."""
    unit = str(env.published_units.get(observed) or "")
    if unit:
        return unit
    raise StepFailedError(
        f"Input {decl.name!r} observes {observed!r}, and this run publishes "
        f"no unit under that name (it publishes "
        f"{', '.join(sorted(n for n, u in env.published_units.items() if u)) or 'nothing named'}). "
        "A record read in its own unit against a variable written in another is "
        "a difference nobody measured: name the variable the run publishes, or "
        "state the unit where that variable is declared.",
        error_code="DATA_NEED_UNMATCHED")


def _what_the_record_reports(env: _Env, decl: Input,
                             row: Any) -> dict[str, Any]:
    """What an OBSERVATION slot is told about the record it was handed.

    Every word is THE MATCHED ROW's own statement, never read across the source's other
    rows; a record with no unit column is read in its measured unit. A record stopping early refuses."""
    if not decl.slot.record:
        return {}
    told: dict[str, Any] = {"window_s": _window(env, decl)}
    if row is not None:
        # A SLOT THAT STATED A NEED names no column: which column carries the
        # reading, the window and the zero is the source's own statement.
        told.update(column_units=dict(row.units), field=row.value_column,
                    series_field=row.series_column, above_field=row.above_column,
                    stamp_field=row.stamp_column)
    # HOW THE RECORD MOVES IN TIME is the source's own class where the row that
    # answered states one, else the slot's role: a flow is a per-time total and a
    # level is read at an instant. No author states it.
    from trid3nt_server.inputs.series import quantity_class

    told["quantity"] = (quantity_class(row.data_class) if row is not None
                        else RATE if decl.slot is DISCHARGE else STATE)
    told["at"] = _opens(env, decl)
    return told


def _opens(env: _Env, decl: Input) -> Any:
    """The instant a row is read at: the one its ``when`` names, where the run states it, else the run's moment.
    A stated instant that does not parse refuses by its input's name rather than reading the latest."""
    from trid3nt_server.inputs.instant import instant as stated_instant

    named = decl.coercion.get("when")
    if not named:
        return env.run.get(env.moment)
    if named not in env.run:
        raise StepFailedError(
            f"Input {decl.name!r} is read when {named!r} says, and no input of "
            f"this run is called that: its inputs are {sorted(env.run)}.",
            error_code="INPUT_UNNAMED")
    held = stated_instant(env.run[named], label=named,
                          code="OBSERVATION_INSTANT_INVALID")
    return held if held is not None else env.run.get(env.moment)


def _measured(env: _Env, decl: Input) -> str:
    """What a row's record measures, as the run input its ``measured`` names states it; "" where it names none."""
    named = decl.coercion.get("measured")
    if not named:
        return ""
    if named not in env.run:
        raise StepFailedError(
            f"Input {decl.name!r} measures what {named!r} says, and no input of "
            f"this run is called that: its inputs are {sorted(env.run)}.",
            error_code="INPUT_UNNAMED")
    return str(env.run[named] or "")


def _window(env: _Env, decl: Input) -> float | None:
    """How long a record must span from the instant it is read at: the run's window, or none at a stated instant, which is read and not forced over."""
    named = decl.coercion.get("when")
    return None if named and env.run.get(named) else env.window_s


async def _on_the_run_s_frame(env: _Env, decl: Input, value: Any,
                              told_so_far: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """What an ELEVATION slot is told about the run's own vertical frame.

    Only the bed and the level are elevations; another slot handed a frame would demand a
    datum of a temperature. A source on ANOTHER frame is bridged by the offset row; a level
    record is bridged for the gauge the reading picks, which states its own datum."""
    if decl.slot not in (BED, LEVEL):
        return {}
    frame = _frame(env)
    told = {"frame": frame} if decl.slot is BED else {"to_datum": frame}
    if decl.slot is BED and isinstance(value, list):
        # ONE OFFSET PER SURFACE, each asked where that surface has data.
        return {**told, "offset": [await _offset_row(env, f"{decl.name}_{rank}", item, frame)
                                   for rank, item in enumerate(value, start=1)]}
    chosen = await _picked_gauge(decl, value, told_so_far or {}) if decl.slot is LEVEL else None
    source, at = chosen if chosen else (value, None)
    measured = await _offset_row(env, decl.name, source, frame, at=at)
    return told if measured is None else {**told, "offset": measured}


async def _picked_gauge(decl: Input, value: Any, told: Mapping[str, Any]) -> Any:
    """The level reading's own datum and its site's point, to ask the offset row of, or ``None``.

    A record's gauges each state a zero, so the one the reading picks is known only by
    reading; a record whose picked reading states none is left to the record's own datum."""
    from trid3nt_server.inputs.observation import ObservationError
    from trid3nt_server.inputs.slots import ingest_slot

    kept = {k: v for k, v in told.items() if k not in ("to_datum", "caption", "label")}
    try:
        found = await asyncio.to_thread(ingest_slot, decl.slot, value,
                                        label=decl.name, **kept)
    except ObservationError:
        return None
    if found is None or not getattr(found, "datum", None) or found.lon is None:
        return None
    return {"vertical_datum": found.datum}, (found.lon, found.lat)


async def _datum_offset_ask(value: Any, frame: str,
                            seed: Any) -> Mapping[str, Any] | None:
    """What the offset row asks for this source, off the loop: a footprint read.

    A case layer handed by its handle or uri is asked as the record its producer stated, which carries its zero."""
    from trid3nt_server.inputs.vertical_datum import offset_ask
    from trid3nt_server.render.uri_registry import lookup_layer_for_handle

    held = lookup_layer_for_handle(value) if isinstance(value, str) else None
    return await asyncio.to_thread(partial(offset_ask, held or value, frame, at=seed))


async def _question_seed(env: _Env) -> Any:
    """The point the QUESTION named, off the row that stands the run on a place, or ``None``.

    Another row's own point ranks a reporting site and is not the question's place."""
    from trid3nt_server.inputs.point import lonlat_of

    row = next((r for r in env.data.values() if r.slot in (DOMAIN, EXTENT)
                and r.coercion.get("near") is not None), None)
    if row is None:
        return None
    return lonlat_of(await _seed(env, row))


async def _offset_row(env: _Env, owner: str, value: Any, frame: str,
                      at: Any = None) -> Any:
    """The measured shift onto the run's frame, as a DATA row the RUNTIME declares.

    Asked like any fact about the world, with a journal line naming the service. ``None``
    where no row is owed, which leaves the alignment to refuse. ``owner`` names the row."""
    from trid3nt_server.inputs.vertical_datum import OFFSET_FETCH

    ask = await _datum_offset_ask(value, frame, at or await _question_seed(env))
    if ask is None:
        return None
    name = f"{owner}_datum_offset"
    row = Input(Producer(runner=OFFSET_FETCH, kwargs=ask, row=name), name=name)
    env.data[name] = row
    return await _produce(env, row)


def _malformed_ask(exc: BaseException) -> bool:
    """Is this the ASK being wrong rather than the source holding nothing?

    A producer's refusal arrives inside the step that called it, so the whole chain is read."""
    from trid3nt_server.inputs.user_input import EmptyResultError, UserInputError
    from trid3nt_server.tools.fetchers._router.errors import RouterInputError

    seen: set[int] = set()
    found: BaseException | None = exc
    while found is not None and id(found) not in seen:
        if isinstance(found, EmptyResultError):
            return False
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


def _validate_supplied(env: _Env, decl: Input, supplied: Any) -> None:
    """Two checks and no third before a supplied artifact is adopted: the slot's declared
    SHAPE against its class, and that a domain is bound or declared; the artifact's own extent is never read."""
    refuse_wrong_shape(decl, supplied)
    if isinstance(supplied, (int, float)) and not isinstance(supplied, bool):
        # A NUMBER is not an artifact: a stated depth or a stated reading has no
        # extent for a coverage check to be about, and it covers the domain by
        # construction - which is why a slot takes one at all.
        return
    dom = current_domain()
    if dom is not None and dom.bbox is not None:
        return
    if any(row.slot in (DOMAIN, RUN) for row in env.data.values()):
        # A workflow that DECLARES a domain or a finished run carries its own place: the slot binds
        # it the moment it is filled, so a row produced before it is adopted for its declared shape.
        return
    raise SuppliedCoverageError(
        f"the artifact supplied for {decl.name!r} cannot be checked against the "
        "modelled domain: no domain is bound. Resolve the AOI before supplying one."
    )



ACCEPTED, REJECTED, MISSING, DEFAULTED = (
    "accepted", "rejected", "missing", "defaulted")

#: What rides a fill beside its inputs and is carried as stated: whether a kept mesh is rebuilt, and how many engine steps a check run takes.
_CARRIED = ("restart_clean", "check_steps")

#: The code of a row that was not produced because a param it stands on was refused.
_WAITS = "FILL_NOT_READY"


@dataclass(frozen=True)
class Verdict:
    """One input's answer on arrival: its state, the value taken and where it
    came from, or the reason it was refused and what would be taken instead."""

    state: str
    value: Any = None
    reason: str = ""
    remedies: tuple[str, ...] = ()
    code: str = ""
    #: Who stated the value, and the source or producer that names it; none on an input that holds no value.
    origin: Origin | None = None
    source: str = ""
    #: What the fill did with the name on its way in: a name stated on the wrong side of ``keywords`` is moved, and says so.
    note: str = ""


@dataclass
class Fill:
    """The fill's state: every input's verdict, and what a launch reads."""

    workflow: Any
    inputs: dict[str, Verdict] = field(default_factory=dict)
    stated: dict[str, Any] = field(default_factory=dict)
    carried: dict[str, Any] = field(default_factory=dict)
    #: What an accept rule has to say of a run that launches; a refused or failed one never carries it.
    notes: list[str] = field(default_factory=list)
    env: _Env | None = None
    domain: Domain | None = None
    #: What producing an input said, chose and covered, in that order: the fill
    #: runs before the run's own channels open, so the launch restates them.
    said: tuple[list[str], list[Any], list[str]] = field(
        default_factory=lambda: ([], [], []))
    #: The settings the run states in its keywords, by identifier, and the ones the fill has read: the launch binds the first and carries the second onto the journal.
    settings: dict[str, Any] = field(default_factory=dict)
    settings_read: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return not any(v.state in (REJECTED, MISSING)
                       for v in self.inputs.values())

    @property
    def keywords(self) -> dict[str, Any]:
        """The engine's own keywords this fill accepted, by the name the caller
        used: every accepted input the workflow's body does not declare."""
        declared = {row.name for row in self.workflow.inputs}
        return {name: verdict.value for name, verdict in self.inputs.items()
                if verdict.state == ACCEPTED and name not in declared}

    def refusal(self) -> tuple[str, str]:
        """``(code, sentence)`` naming every input that keeps this fill from ready, each reason
        said once, the rows waiting on a refused param in one sentence, and every name moved."""
        held = [(n, v) for n, v in self.inputs.items()
                if v.state in (REJECTED, MISSING)]
        code = next((v.code for _n, v in held if v.code and v.code != _WAITS),
                    _WAITS)
        waiting = [n for n, v in held if v.code == _WAITS]
        said = dict.fromkeys(
            v.reason + (f" Remedies: {', '.join(v.remedies)}." if v.remedies
                        else "") for _n, v in held if v.code != _WAITS)
        if waiting:
            said[f"{', '.join(waiting)} {'are' if len(waiting) > 1 else 'is'} "
                 "produced once the refused inputs above are stated again."] = None
        return code, " ".join([*said, *self.moved])

    @property
    def moved(self) -> list[str]:
        """What the fill said of each name it moved across ``keywords``."""
        return [v.note for v in self.inputs.values() if v.note]


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
    stated_settings, unsaid, keywords = split_keywords(keywords)
    state.settings.update(stated_settings)
    for name, reason in unsaid.items():
        state.inputs[name] = Verdict(REJECTED, None, reason=reason, code="KEYWORD_REFUSED")
    values.update({str(name): value for name, value in keywords.items()})
    tokens = (bind_notes(), journal.bind_choices(), bind_coverage())
    reading, run_token = journal.bind_settings(), bind_run(state.settings)
    try:
        moves: dict[str, str] = {}
        found = _looked_up(state, values, set(keywords), moves)
        declared = {row.name: row for row in wf.inputs}
        state.stated.update({name: values[name] for name in found
                             if name in declared and not declared[name].is_row})
        args, refused = await _coerced(state)
        # A declared input refused inside keywords keeps that verdict.
        refused |= set(keywords) - set(found)
        # Keywords answer first and every value after them, stated or not; a row
        # stands on both, so the stated rows answer next and the named ones last.
        for name, held in found.items():
            if name not in declared:
                await _answer(state, name, held, values[name])
        for row in wf.inputs:
            if not row.is_row and row.name not in refused:
                await _answer(state, row.name, row, args.get(row.name))
        for name in wf.unnamed():
            state.inputs[name] = Verdict(
                REJECTED, name, code="INPUT_UNNAMED",
                reason=f"the template names {name!r} as an input, and no param, "
                "row or output of this run is called that.")
        # What the workflow refuses of the whole run answers before any unstated row is fetched.
        state.inputs.update(wf.judged(state))
        sourced = {name: values[name] for name, held in found.items()
                   if getattr(held, "is_row", False)}
        # Every stated row is registered before any is produced: producing one
        # forces the domain, which must already see the caller's own.
        for name, value in sourced.items():
            _stated(production(state), declared[name], value)
        # EVERY ROW A STAGE READS WITHOUT THE MESH is produced here: a consumer only reads the mapping, so a row that cannot be
        # produced is refused now, by name, and a READY fill is followed by no fetch but a placed mark's.
        # The place answers first: every other row stands on it.
        for name, value in sorted(sourced.items(), key=lambda item: (
                declared[item[0]].slot not in (DOMAIN, EXTENT, RUN))):
            await _answer(state, name, declared[name], value)
        for name in wf.named_rows(production(state).run) if state.ready else ():
            if name not in production(state).run:
                await _answer(state, name, declared[name], None)
        for name, note in moves.items():
            if name in state.inputs:
                state.inputs[name] = dataclasses.replace(state.inputs[name], note=note)
    finally:
        for held, drain, token in zip(state.said, (
                drain_notes, journal.drain_choices,
                drain_coverage), tokens):
            held += drain(token)
        state.settings_read.update(journal.drain_settings(reading))
        reset_run(run_token)
    return state


def _engine_keyword(wf: Any, name: str) -> Any:
    """The engine keyword ``name`` spells exactly on this run, or ``None``."""
    try:
        return wf.input(name)
    except Exception:  # noqa: BLE001 - a name no body spells is no keyword
        return None


def _run_length_said(wf: Any) -> str:
    """The sentence naming the keyword the template's module spells its run length in, with the value the template states; empty where the module has none."""
    steering = getattr(wf, "steering", None)
    slot = steering.run_length() if steering is not None else None
    if slot is None:
        return ""
    unit = f" ({slot.unit})" if slot.unit else ""
    stated = steering.ASSERTED.get(slot.identifier)
    return (f" The run length is the engine keyword {slot.keyword}{unit}"
            + (f", stated by this template as {stated:g}" if isinstance(stated, (int, float)) else "")
            + f": keywords={{\"{slot.keyword}\": ...}}.")


def _looked_up(state: Fill, values: Mapping[str, Any], keywords: set[str],
               moves: dict[str, str]) -> dict[str, Any]:
    """Every stated name through the workflow's ONE lookup -> the input each names. A declared
    input stated inside ``keywords``, or an engine keyword spelled exactly beside it, is taken on
    its own side and the move is said; any other name is refused here."""
    wf = state.workflow
    known = sorted(row.name for row in wf.inputs)
    found: dict[str, Any] = {}
    unknown: list[str] = []
    for name, value in values.items():
        if name not in keywords:
            if name in known:
                found[name] = wf.input(name)
            elif isinstance(keyword := _engine_keyword(wf, name), Keyword):
                found[name] = keyword
                moves[name] = (
                    f"{name!r} is an engine keyword and was moved inside "
                    "keywords; state it there.")
            else:
                unknown.append(name)
            continue
        if name in known:
            found[name] = wf.input(name)
            moves[name] = (
                f"{name!r} is an input of {wf.name} and was moved out of "
                "keywords; state it by its own name beside keywords.")
            continue
        try:
            found[name] = wf.input(name)
        except Exception as exc:  # noqa: BLE001 - the refusal is the verdict
            if getattr(exc, "retryable", False):
                raise
            state.inputs[name] = Verdict(REJECTED, value, reason=str(exc),
                                         code="KEYWORD_REFUSED")
    for name in unknown:
        # An invented field resembles no input, so only a near spelling is offered; the keywords sentence is said once, on the last.
        state.inputs[name] = Verdict(
            REJECTED, values[name], code="INPUT_UNNAMED",
            reason=f"{name!r} is not an input of {wf.name} "
            f"({nearest(name, known, cutoff=0.6)})."
            + (_run_length_said(wf) if name == unknown[-1] else "")
            + (" An engine keyword is stated by its own name inside keywords."
               if name == unknown[-1] else ""))
    return found


def restate(state: Fill) -> None:
    """Say on the run in progress what producing its inputs said at the fill."""
    notes, choices, covered = state.said
    for text in notes:
        (cut_coverage if text in covered else journal_note)(text)
    for choice in choices:
        slot_choice(choice)


async def _coerced(state: Fill) -> tuple[dict[str, Any], set[str]]:
    """Every stated value through the workflow's coercions -> what each value input answers
    on, and the names a coercion refused: its typed refusal is that input's verdict."""
    args = dict(state.stated)
    refused: set[str] = set()
    for coercion in state.workflow.coercions:
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
            name = str(getattr(coercion, "__name__", "")).split(":")[-1]
            refused.add(name)
            state.inputs[name] = Verdict(REJECTED, state.stated.get(name),
                                         reason=str(exc), code=exc.error_code)
            continue
        args.update(coerced or {})
    return args, refused


async def _answer(state: Fill, name: str, held: Any, value: Any) -> None:
    """ONE input answers: its own accept rule takes the stated value or its default, a row is
    fetched or read and ingested first, and the verdict carries where the value came from."""
    row = getattr(held, "is_row", False)
    stated = held.default if value is None else value
    if name == getattr(state.workflow, "moment", "") and isinstance(stated, str) and stated.strip().lower() == NO_MOMENT_WORD:
        state.inputs[name] = Verdict(ACCEPTED, None, origin=Origin.USER)
        return
    try:
        if row:
            await _row(state, held, value)
            return
        taken = None if stated is None else held.accept(stated)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - the refusal is the verdict
        if getattr(exc, "retryable", False):
            raise
        picked = None
        if row and state.env is not None:
            picked = state.env.picks.pop(name, None)
            state.env.supplied.pop(name, None)
        code = _refusal_code(exc, name)
        state.inputs[name] = Verdict(
            REJECTED, value, code=code,
            reason=str(exc) if value is not None or not row
            else f"{name!r} could not be produced: {exc}",
            remedies=await _instead(state.env, held, picked) if picked
            else (f"a value between {held.bounds[0]} and {held.bounds[1]}",)
            if getattr(held, "bounds", None)
            else await _otherwise(held, value) if row and code != _WAITS else ())
        return
    note = getattr(held, "note", "")
    if note and note not in state.notes:
        state.notes.append(note)
    if taken is None and held.required:
        state.inputs[name] = Verdict(
            MISSING, reason=f"{name} was not supplied and has no "
            "default: supply it explicitly - it is never invented.",
            code="GATE_REFUSED")
    elif taken is None:
        state.inputs.pop(name, None)
    else:
        state.inputs[name] = Verdict(
            DEFAULTED if value is None else ACCEPTED, taken,
            origin=Origin.TEMPLATE if value is None else Origin.USER)


def production(state: Fill) -> _Env:
    """The state a sourced input is produced under, built once per fill; it reads every value the fill has seated."""
    wf = state.workflow
    if state.env is None:
        state.env = _Env(
            data={row.name: row for row in wf.inputs if row.is_row},
            inputs=state.inputs,
            workflow=wf.name,
            window_s=wf.run_window_s(state.keywords), moment=wf.moment,
            slot_units=wf.slot_units(), captions=wf.captions,
            lays_own_bed=wf.lays_own_bed)
    state.env.run.update({row.name: getattr(state.inputs.get(row.name), "value", None)
                          for row in wf.inputs if not row.is_row})
    # A unit the run states is published under it, so the table is read off the values just seated.
    state.env.published_units = wf.published_units(state.env.run)
    return state.env


def _stated(env: _Env, decl: Input, value: Any) -> None:
    if decl.slot is BED and isinstance(value, (list, tuple)):
        # AN ORDERED LIST OF SURFACES: each read as it would be alone, a source kept to be fetched.
        env.supplied[decl.name] = [item if isinstance(item, Mapping) and "source" in item
                                   else _handed(item) for item in value]
    elif isinstance(value, Mapping) and "source" in value:
        env.picks[decl.name] = str(value["source"])
    elif value is not None:
        env.supplied[decl.name] = _handed(value)


def _handed(value: Any) -> Any:
    """One supplied value as the slot reads it."""
    # A mapping that names no layer IS the value: a drawn geometry arrives as one.
    held = (value["layer"] if isinstance(value, Mapping)
            and "layer" in value else value)
    # A case layer named by its handle is read at its uri; any other string is the value as it came.
    return lookup_uri_for_handle(held) or held if isinstance(held, str) else held


async def _row(state: Fill, decl: Input, value: Any) -> None:
    """A sourced input: fetched or read, and ingested, BEFORE it answers."""
    if any(state.inputs[row.name].state == REJECTED for row in state.workflow.inputs
           if not row.is_row and row.name in state.inputs):
        raise StepFailedError(
            f"{decl.name} stands on the run's params, and one of them was "
            "refused.", error_code=_WAITS)
    env = production(state)
    _stated(env, decl, value)
    if decl.slot not in (DOMAIN, EXTENT):
        await _place_first(state, env, decl)
    await _taken(env, decl)
    state.domain = current_domain()


def _refusal_code(exc: BaseException, name: str) -> str:
    """The code a refused input carries; an error stating none is a fault, logged with its trace."""
    code = getattr(exc, "error_code", None)
    if code is None:
        logger.warning("input %s refused on an untyped error", name,
                       exc_info=exc)
    return code or "INPUT_REFUSED"


async def _place_first(state: Fill, env: _Env, decl: Input) -> None:
    """A sourced input stands on the run's place, so the place is filled first. A place that
    refuses answers as its own input, once, and the row waits on it.

    Producing a row may register runtime rows, so the loop is over the rows as they stood."""
    for row in list(env.data.values()):
        if row.slot is not DOMAIN or row.name in env.run:
            continue
        if getattr(state.inputs.get(row.name), "state", None) != REJECTED:
            try:
                await _taken(env, row)
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the refusal is the place's verdict
                if getattr(exc, "retryable", False):
                    raise
                state.inputs[row.name] = Verdict(
                    REJECTED, None, code=_refusal_code(exc, row.name),
                    reason=f"{row.name!r} could not be produced: {exc}",
                    remedies=await _otherwise(row, None))
        raise StepFailedError(
            f"{decl.name} stands on the run's {row.name}, which was refused.",
            error_code=_WAITS)


async def _otherwise(decl: Input, value: Any) -> tuple[str, ...]:
    """What a refused row takes instead: each Case layer its slot's ingestion takes, and, for a
    row the match can fill, leaving it unstated."""
    from trid3nt_server.inputs.accept import layers_taking
    from trid3nt_server.render.uri_registry import case_layers

    held = case_layers()
    taken = await asyncio.to_thread(layers_taking, decl.slot, decl.name, held) \
        if held and decl.slot is not None and decl.slot.ingest is not None else []
    return (*(f"the Case layer {layer_id} ({label})" if label != layer_id
              else f"the Case layer {layer_id}" for layer_id, label in taken),
            *((f"leave {decl.name} unstated and the run matches a source of "
               f"{decl.data_class}",) if decl.data_class and value is not None else ()))


async def _instead(env: _Env, decl: Input, picked: str) -> tuple[str, ...]:
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
