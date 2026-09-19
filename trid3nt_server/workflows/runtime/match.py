"""THE MATCH: a slot asks, a fetcher answers, nobody writes the ladder.

A slot states a NEED - a data class, the place off the domain, the window off
the run, the frame off the lever - and every fetcher states its COVERAGE. The
filter is mechanical and hard on class and place; the window is hard for a
series and a sort key for a surface; a differing datum is FLAGGED here and
refused at the offset row. What comes back is one ranked list: the card renders
it, the tool result carries it on a tie, the sheet stores the pick.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Sequence

from trid3nt_contracts.coverage import Coverage, SourceChoice, SourceOption

from .errors import PlanValidationError

__all__ = ["LOOSEN_DATUM", "LOOSEN_WINDOW", "Need", "RANKED_ROWS",
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
        reason = _excluded(need, coverage)
        if reason:
            dropped.append(_option(name, coverage, need, excluded=reason))
            continue
        kept.append((_rank(need, coverage), name, coverage))
    kept.sort(key=lambda row: (row[0], row[1]))
    survivors = [_option(name, coverage, need) for _key, name, coverage in kept]
    tie = len(kept) > 1 and kept[0][0] == kept[1][0]
    rows = (survivors + dropped)[:RANKED_ROWS]
    picked = survivors[0].fetcher if survivors else ""
    return SourceChoice(
        slot=need.slot, need=need.data_class, rows=rows, picked=picked,
        sentence=_sentence(need, survivors, dropped), tie=tie,
        loosened=need.loosen)


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


def _excluded(need: Need, coverage: Coverage) -> str:
    """Why a filter drops this source, or "" where it survives.

    Class is already answered. Place is hard; the window is hard for a SERIES
    and never for a surface, which a run outside simply ranks lower. The datum
    is not a filter - it is flagged on the row and refused at the offset row."""
    if need.lon is not None and need.lat is not None \
            and not coverage.extent.covers(need.lon, need.lat):
        return f"does not reach this place: {coverage.extent.note or 'stated extent'}"
    if coverage.window.series and need.loosen != LOOSEN_WINDOW:
        outside = _outside_window(need, coverage)
        if outside:
            return outside
    return ""


def _outside_window(need: Need, coverage: Coverage) -> str:
    """Whether the run's window falls outside what a series source holds."""
    opens, until = instant(need.opens), instant(need.until or need.opens)
    if opens is None:
        return ""
    earliest, latest = (instant(coverage.window.earliest),
                        instant(coverage.window.latest))
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
    """The sort key, on FACTS: the cell against the mesh, recency, native datum.

    A source coarser than the mesh cannot answer what the mesh resolves, so it
    ranks below every source that can, and the least coarse of those comes
    first. Among the sources that resolve the mesh the FINER one measured the
    ground more closely, so it ranks first. Lower sorts earlier throughout."""
    return (_cell_rank(need, coverage), -_recency(coverage),
            0.0 if _on_frame(need, coverage) else 1.0)


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
        fetcher=name, resolution=_cell(coverage), recency=_currency(coverage),
        datum=coverage.datum or "no datum stated",
        extent=coverage.extent.note or coverage.extent.kind,
        excluded=excluded)


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
    facts = f"{top.resolution}, {top.recency}, {top.datum}"
    over = ", ".join(f"{row.fetcher} {row.resolution}" for row in survivors[1:4])
    tail = f", over {over}" if over else ""
    loosened = (f" The run states {need.loosen} loosened, which is the user's "
                "choice." if need.loosen else "")
    return f"{need.slot}: {top.fetcher} ({facts}){tail}.{loosened}"


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
