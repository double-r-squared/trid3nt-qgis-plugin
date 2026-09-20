"""THE MATCH: a slot asks, a fetcher answers, nobody writes the ladder.

A slot states a NEED - a data class, the place off the domain, the window off
the run, the frame off the lever - and every fetcher states its COVERAGE. The
filter is mechanical and hard on class and place; the window is hard for a
series and a sort key for a surface; a differing datum is FLAGGED here and
refused at the offset row. The sort reads facts in one order: how the numbers
were come by, how far the place is from them, the cell against the mesh, how
current the record is, the zero it counts from. What comes back is one ranked
list: the card renders it, the tool result carries it on a tie, the sheet
stores the pick.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Sequence

from typing import Any, Mapping

from trid3nt_contracts.coverage import (
    PROVENANCE_KINDS, Coverage, CoverageExtent, CoveragePoint, SourceChoice,
    SourceOption)

from trid3nt_server.workflows.runtime.errors import PlanValidationError

logger = logging.getLogger(__name__)

__all__ = ["LOOSEN_DATUM", "LOOSEN_WINDOW", "Need", "RANKED_ROWS", "ask_for",
           "dropped_from", "instant", "match", "sources_with_coverage"]

#: How many rows the ranked list carries. Five: enough for the facts to be
#: comparable in one read, few enough that a model answering with a row number
#: is choosing rather than scanning.
RANKED_ROWS = 5


@dataclass(frozen=True, slots=True)
class Need:
    """What one slot asks the world for.

    Every field but the class is read off the run rather than the question: the
    place is the domain's, the window is the run's and the frame is the lever's,
    which is why a template states none of them."""

    slot: str
    data_class: str
    lon: float | None = None
    lat: float | None = None
    #: The instants the run opens and closes at, ISO. A series source that does
    #: not span them has no record for this run.
    opens: str | None = None
    until: str | None = None
    #: The vertical frame the run is solved on, off the lever.
    frame: str | None = None
    #: The cell the mesh resolves, which is what a source's own cell is ranked
    #: against - finer than the mesh is detail the mesh cannot carry.
    mesh_m: float | None = None
    #: The run-level statement that loosens ONE filter. It shows as the user's
    #: choice and never as the match's own judgement.
    loosen: str = ""
    #: The source the run NAMES for this slot, "" where it names none. A named
    #: source the filters excluded is a refusal, not a pick.
    pick: str = ""
    #: THE DOMAIN'S OWN WATER: the outline a gauge has to stand on for its flow
    #: to be this run's flow, with the note saying whether the run drew the
    #: polygon or only its box. ``None`` where the run states no domain.
    water: CoverageExtent | None = None

    def __post_init__(self) -> None:
        if not self.data_class:
            raise PlanValidationError(
                f"the {self.slot!r} slot declares a need with no data class: a "
                "match filters on the class first, and a need without one asks "
                "the whole catalogue for anything.")


#: The loosening a run states, by the filter it lifts. A window stand-in takes
#: the nearest record the source holds; an unconverted datum takes a source on
#: another zero as it is.
LOOSEN_WINDOW = "window"
LOOSEN_DATUM = "datum"


def sources_with_coverage() -> list[tuple[str, Coverage]]:
    """Every coverage row every registered fetcher states, by source name.

    ONE PAIR PER ROW: a source serving two classes is two candidates, and each
    is filtered on the class it actually serves. A source with no row is never
    matched - it stays model-callable and nothing here can say whether it
    reaches this place."""
    from trid3nt_server.tools.fetchers._router.registration import _SPEC_REGISTRY

    return sorted(((name, row) for name, spec in _SPEC_REGISTRY.items()
                   for row in spec.coverage),
                  key=lambda pair: (pair[0], pair[1].data_class))


def match(need: Need,
          candidates: Sequence[tuple[str, Coverage]]) -> SourceChoice:
    """The ranked list for one need, PURE over the declarations it is handed.

    Survivors first in rank order, then what each filter excluded and why. The
    caller probes the top survivor and drops it on empty."""
    kept: list[tuple[tuple[float, ...], str, Coverage]] = []
    dropped: list[SourceOption] = []
    for name, coverage in candidates:
        if coverage.data_class != need.data_class:
            continue
        reason = _unaskable(name, coverage) or _excluded(need, coverage)
        if reason:
            dropped.append(_option(name, coverage, need, excluded=reason))
            continue
        kept.append((_rank(need, coverage), name, coverage))
    kept.sort(key=lambda row: (row[0], row[1]))
    survivors = [_option(name, coverage, need) for _key, name, coverage in kept]
    tie = len(kept) > 1 and kept[0][0] == kept[1][0]
    if need.pick:
        survivors = _named_first(need, survivors, dropped)
        tie = False
    rows = (survivors + dropped)[:RANKED_ROWS]
    picked = survivors[0].fetcher if survivors else ""
    return SourceChoice(
        slot=need.slot, need=need.data_class, rows=rows, picked=picked,
        sentence=_sentence(need, survivors, dropped), tie=tie,
        loosened=need.loosen, picked_by_user=bool(need.pick))


def _named_first(need: Need, survivors: Sequence[SourceOption],
                 dropped: Sequence[SourceOption]) -> list[SourceOption]:
    """The list with the source the RUN named at its head.

    The ranked list stays whole - what the sort would have taken is still on it
    - and only the pick moves, because a user's choice is a choice among the
    survivors and not a way past the filters."""
    named = next((row for row in survivors if row.fetcher == need.pick), None)
    if named is None:
        excluded = next((row.excluded for row in dropped
                         if row.fetcher == need.pick), "")
        raise PlanValidationError(
            f"the run picks {need.pick!r} for the {need.slot!r} slot and it is "
            "not a survivor of the match: "
            + (excluded or f"no coverage row on it serves {need.data_class}")
            + ". Pick a source the list carries, or state the value.")
    return [named] + [row for row in survivors if row.fetcher != need.pick]


def dropped_from(choice: SourceChoice, fetcher: str, why: str) -> SourceChoice:
    """The same list with ``fetcher`` moved out of the pick: what the PROBE found.

    A source that answered nothing over this domain is excluded by the world
    rather than by a filter, and the next survivor fills the slot."""
    rows = [row.model_copy(update={"excluded": why}) if row.fetcher == fetcher
            else row for row in choice.rows]
    nxt = next((row.fetcher for row in rows
                if row.fetcher != fetcher and not row.excluded), "")
    return choice.model_copy(update={
        "rows": rows, "picked": nxt, "tie": False,
        "sentence": _probe_sentence(choice, rows, fetcher, nxt)})


#: What the probe can supply a source off the run itself: the domain's box, its
#: seed, and the window. Anything else a source requires is askable only where
#: its own coverage row says what to pass - the row's ask block, or, for the
#: station a source is called by name, the stations the row lists.
_ASKABLE = frozenset({"bbox", "seed_point", "start_date", "end_date",
                      "valid_time"})

#: THE CLASS WHOSE PLACE FILTER IS THE DOMAIN ITSELF. A level propagates up a
#: reach, so a gauge within reach of this water speaks for it; a discharge is the
#: water passing ONE section, so a gauge that does not stand on this domain's
#: water measures another river's flow however near it is.
_ON_THE_WATER = "discharge series"

#: The param a station-addressed source is called by. A row whose extent lists
#: its stations answers it with the nearest one; a discovered network is called
#: by box and never states one.
_STATION = "station"


def _required(name: str) -> list[str]:
    """The params this source refuses to be called without."""
    from trid3nt_server.tools.fetchers._router.registration import _SPEC_REGISTRY

    spec = _SPEC_REGISTRY.get(name)
    if spec is None:
        return []
    return [param for param, decl in spec.params.items()
            if bool(getattr(decl, "required", None)
                    or (isinstance(decl, dict) and decl.get("required")))]


def _unaskable(name: str, coverage: Coverage) -> str:
    """Why the probe could not call this source at all, or "" where it can."""
    asked = set(_ASKABLE) | set(coverage.ask)
    if coverage.extent.points or coverage.extent.read_from:
        asked.add(_STATION)
    needs = [param for param in _required(name) if param not in asked]
    if not needs:
        return ""
    return (f"asks to be called by {', '.join(needs)} and its coverage row "
            "says nothing to pass for it")


#: Stations read in from a listing, by the hook that read them. A listing is a
#: bounded static fact about a network, so it is read once per process.
_LISTED: dict[str, list[CoveragePoint]] = {}


def _station_set(coverage: Coverage) -> CoverageExtent:
    """This row's extent with its listed stations read in.

    A row that names a listing carries the stations themselves once the listing
    has answered; a listing that cannot be read leaves the extent as declared,
    so the region's rings still say where the source is and the probe answers
    for the rest."""
    hook = coverage.extent.read_from
    if not hook or coverage.extent.points:
        return coverage.extent
    if hook not in _LISTED:
        from trid3nt_server.tools.fetchers._router.hooks import resolve_hook

        try:
            _LISTED[hook] = list(resolve_hook(hook)())
        except Exception as exc:  # noqa: BLE001 - an unread listing is not a match failure
            logger.warning("the %s station listing did not answer (%s); the "
                           "region's own outline stands for it", hook, exc)
            _LISTED[hook] = []
    listed = _LISTED[hook]
    return coverage.extent.model_copy(update={"points": listed}) if listed \
        else coverage.extent


def ask_for(choice: SourceChoice, base: Mapping[str, Any], lon: float | None,
            lat: float | None) -> dict[str, Any]:
    """What the PICKED source is called with: the run's own facts, plus what the
    matched ROW says it takes.

    The row, not the spec's default, is what the match weighed, so the values
    that make the source answer with that row travel with it; a source called by
    a station name is given the nearest station the row lists."""
    ask = dict(base)
    row = _picked_row(choice)
    if row is None:
        return ask
    ask.update(row.ask)
    if lon is None or lat is None:
        return ask
    station = _station_set(row).nearest(lon, lat)
    if station is None:
        return ask
    if _STATION in _required(choice.picked):
        ask[_STATION] = station.id
    elif "bbox" in ask:
        ask["bbox"] = _reaching(ask["bbox"], station)
    return ask


def _reaching(bbox: Any, station: CoveragePoint) -> list[float]:
    """The run's own box, widened to REACH the station the row was ranked on.

    A source called by a box rather than by a name is still ranked on its
    nearest listed station, and the reach the row states is what put it on the
    list; a box that stops at the domain's edge asks it a different question."""
    west, south, east, north = (float(v) for v in bbox)
    return [min(west, station.lon - _AROUND_STATION),
            min(south, station.lat - _AROUND_STATION),
            max(east, station.lon + _AROUND_STATION),
            max(north, station.lat + _AROUND_STATION)]


#: How far past a station the widened box reaches, in degrees - about a hundred
#: metres, so a station ON the edge is inside the box rather than on its line.
_AROUND_STATION = 0.001


def _picked_row(choice: SourceChoice) -> Coverage | None:
    """The row the pick was ranked on: this source's row of the class asked for
    AND of the kind the ranked list shows, since a source serving a measured and
    a predicted series of one class is two candidates and only one was picked."""
    from trid3nt_server.tools.fetchers._router.registration import _SPEC_REGISTRY

    spec = _SPEC_REGISTRY.get(choice.picked)
    if spec is None:
        return None
    kind = next((row.kind for row in choice.rows
                 if row.fetcher == choice.picked and not row.excluded), "")
    return next((row for row in spec.coverage
                 if row.data_class == choice.need
                 and (not kind or row.kind == kind)), None)


def _excluded(need: Need, coverage: Coverage) -> str:
    """Why a filter drops this source, or "" where it survives.

    Class is already answered. Place is hard; the window is hard for a SERIES
    and never for a surface, which a run outside simply ranks lower. The datum
    is not a filter - it is flagged on the row and refused at the offset row."""
    if need.lon is not None and need.lat is not None:
        extent = _station_set(coverage)
        if need.data_class == _ON_THE_WATER and need.water is not None:
            off = _off_the_water(need, extent)
            if off:
                return off
        else:
            away = extent.distance_km(need.lon, need.lat)
            reach = coverage.reach_km or 0.0
            if away > reach:
                where = coverage.extent.note or "stated extent"
                if coverage.extent.kind == "stations":
                    return (f"its nearest station is about {away:.0f} km away "
                            f"and one serves {reach:g} km: {where}")
                return f"does not reach this place: {where}"
    if coverage.window.series and need.loosen != LOOSEN_WINDOW:
        outside = _outside_window(need, coverage)
        if outside:
            return outside
    return ""


def _off_the_water(need: Need, extent: CoverageExtent) -> str:
    """Why this source's stations do not stand on the domain's own water, or "".

    A LISTED set is answered station by station: one station inside the outline,
    or within the cell the mesh resolves of it, is this domain's flow. A network
    with no listing states no station here, so the probe searches the domain's
    own box and the world answers for it."""
    water = need.water
    if water is None:
        return ""
    stations = extent.listed()
    if not stations:
        return ""
    cell_km = float(need.mesh_m or 0.0) / 1000.0
    nearest = min(stations,
                  key=lambda p: water.distance_km(p.lon, p.lat))
    away = water.distance_km(nearest.lon, nearest.lat)
    if away <= cell_km:
        return ""
    return (f"its nearest station {nearest.id} stands about {away:.0f} km off "
            f"{water.note}: a discharge is the water passing one section, so a "
            "gauge off this domain's water reports another flow")


def _outside_window(need: Need, coverage: Coverage) -> str:
    """Whether the run's window falls outside what a series source holds."""
    opens, until = instant(need.opens), instant(need.until or need.opens)
    if opens is None:
        return ""
    earliest, latest = (instant(coverage.window.earliest),
                        instant(coverage.window.latest))
    if latest is None and coverage.kind == "measured":
        # An instrument has not recorded tomorrow: a measured series that states
        # no close reports TO NOW, and a run opening past now has no record from
        # it however far forward the source keeps answering.
        latest = dt.datetime.now(dt.timezone.utc)
        if until is not None and until > latest:
            return (f"reports to now and this run closes at "
                    f"{need.until or need.opens}")
    if earliest is not None and opens < earliest:
        return (f"reports from {coverage.window.earliest} and this run opens at "
                f"{need.opens}")
    if latest is not None and until is not None and until > latest:
        return (f"reports to {coverage.window.latest} and this run closes at "
                f"{need.until or need.opens}")
    return ""


def instant(value: str | None) -> dt.datetime | None:
    """One ISO stamp as a UTC instant, or ``None`` where it is unreadable."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        read = dt.datetime.fromisoformat(
            text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    return read if read.tzinfo is not None else read.replace(tzinfo=dt.timezone.utc)


def _rank(need: Need, coverage: Coverage) -> tuple[float, ...]:
    """The sort key, on FACTS, in the order they decide: how the numbers were
    come by, how far away they are, the cell against the mesh, recency, datum.

    An instrument record at the place answers what a prediction estimates and a
    model grid computes, so the kind leads; among records of one kind the
    nearest speaks for the place. A source coarser than the mesh cannot answer
    what the mesh resolves, so it ranks below every source that can, and the
    least coarse of those comes first. Among the sources that resolve the mesh
    the FINER one measured the ground more closely, so it ranks first. Lower
    sorts earlier throughout."""
    return (float(PROVENANCE_KINDS.index(coverage.kind)),
            _distance_km(need, coverage), _cell_rank(need, coverage),
            -_recency(coverage), 0.0 if _on_frame(need, coverage) else 1.0)


def _distance_km(need: Need, coverage: Coverage) -> float:
    """How far the place is from what this source holds, zero where it is inside."""
    if need.lon is None or need.lat is None:
        return 0.0
    return _station_set(coverage).distance_km(need.lon, need.lat)


def _cell_rank(need: Need, coverage: Coverage) -> float:
    """Where this source's cell stands against the mesh the run resolves."""
    cell = coverage.resolution_m
    if cell is None or need.mesh_m is None:
        return 0.0
    if cell <= float(need.mesh_m):
        return -1.0 / cell
    return cell


def _recency(coverage: Coverage) -> float:
    """How current this source's records are, as a sortable instant.

    A source that reports TO NOW states no ``latest`` and is the most current
    thing there is."""
    latest = instant(coverage.window.latest)
    return latest.timestamp() if latest is not None else float("inf")


def _on_frame(need: Need, coverage: Coverage) -> bool:
    """Is this source already counted from the run's own frame?

    A differing or unstated datum is not excluded here - it is a fact on the row
    and a shift the offset row measures."""
    from trid3nt_server.inputs.vertical_datum import names_frame

    if not need.frame or not coverage.datum:
        return not need.frame
    return names_frame(coverage.datum, need.frame)


def _option(name: str, coverage: Coverage, need: Need, *,
            excluded: str = "") -> SourceOption:
    """One ranked row: the source and the four facts it is picked on."""
    return SourceOption(
        fetcher=name, kind=coverage.kind, distance=_range(need, coverage),
        resolution=_cell(coverage), recency=_currency(coverage),
        datum=coverage.datum or "no datum stated",
        extent=coverage.extent.note or coverage.extent.kind,
        excluded=excluded)


def _range(need: Need, coverage: Coverage) -> str:
    """How far the place is from this source, in the words the card shows."""
    if need.lon is None or need.lat is None:
        return ""
    away = _distance_km(need, coverage)
    if away <= 0.0:
        return "over this place"
    return f"about {away:.0f} km away"


def _cell(coverage: Coverage) -> str:
    if coverage.resolution_m is not None:
        return f"{coverage.resolution_m:g} m"
    return "stations" if coverage.extent.kind == "stations" else "no cell stated"


def _currency(coverage: Coverage) -> str:
    if coverage.window.series:
        return coverage.window.cadence or "a reported series"
    return f"measured to {coverage.window.latest}" if coverage.window.latest \
        else "static"


def _sentence(need: Need, survivors: Sequence[SourceOption],
              dropped: Sequence[SourceOption]) -> str:
    """ONE sentence: what the model is told and what the card shows.

    Authored here so the two cannot drift - a second rendering of the same facts
    is a second chance to say something the run did not do."""
    if not survivors:
        named = "; ".join(f"{row.fetcher} {row.excluded}" for row in dropped)
        return (f"{need.slot}: nothing measures {need.data_class} here - "
                + (named or "no source states coverage of this class")
                + ". State the value on the call, or ask about a place or a "
                  "moment a source reaches.")
    top = survivors[0]
    facts = ", ".join(fact for fact in (top.kind, top.distance, top.resolution,
                                        top.recency, top.datum) if fact)
    over = ", ".join(f"{row.fetcher} {row.resolution}" for row in survivors[1:4])
    tail = f", over {over}" if over else ""
    loosened = (f" The run states {need.loosen} loosened, which is the user's "
                "choice." if need.loosen else "")
    named = " The run picks it, which is the user's choice." if need.pick else ""
    return f"{need.slot}: {top.fetcher} ({facts}){tail}.{named}{loosened}"


def _probe_sentence(choice: SourceChoice, rows: Sequence[SourceOption],
                    fetcher: str, nxt: str) -> str:
    """What the run says once the world has answered.

    With a survivor left the line names the drop and who took its turn; with
    none left it names EVERY source and what each one had to say, which is the
    refusal a reader can act on."""
    if nxt:
        return f"{choice.slot}: {fetcher} held nothing, so {nxt} fills the slot."
    named = "; ".join(f"{row.fetcher} {row.excluded}" for row in rows
                      if row.excluded)
    return (f"{choice.slot}: nothing measures {choice.need} here - {named}. "
            "State the value on the call, or ask about a place or a moment a "
            "source reaches.")
