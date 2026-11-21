"""An OBSERVATION: one measured value a slot opens on, read off what was fetched.

The value arrives in the slot's unit and on its datum; a reading counted from another
zero moves onto it only through an offset row the template names. A record that
reported a WINDOW carries its series beside the reading. A sample is a moment, never
a climatology, so what is read is said on the run journal.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
from dataclasses import dataclass, replace
from typing import Any, Mapping, Sequence

from trid3nt.settings import setting

from .temporal import (
    Series,
    convert_units,
    spelling,
    TemporalGapError,
    TemporalShapeError,
    TemporalUnitsError,
)

from .series import align

from trid3nt_contracts.coverage import UnitField, Member

from .fields import FieldError, Record, produced_by

from .geometry import read_geometry_doc, source_uri
from .vertical_datum import DatumError, datum_of, onto_frame

__all__ = ["Accuracy", "Observation", "ObservationError", "convert", "note",
           "observation", "sited", "sites"]

logger = logging.getLogger("trid3nt.inputs.observation")

_CODE = "OBSERVATION_INVALID"

#: How far AHEAD of the asked moment a record may be read when it sampled nothing before
#: it: one of the record's OWN cadences, so a daily record's same-day reading is that
#: day's value and a six-minute gauge reaches six minutes on. A record with one
#: readable stamp states no spacing, so a day is the coarsest cadence.
_LONE_CADENCE = dt.timedelta(days=1)

#: How a record no source states a field map for is read: the layer a user hands over names its reading
#: ``value`` and its window ``time_series_csv``, each in the unit its ``unit`` field states, and what reported,
#: what the value is and what it is counted from under ``site_id``, ``site_name``, ``quantity`` and ``vertical_datum``.
_UNSOURCED: Mapping[str, Member] = {
    "value": Member(field="value", unit=UnitField(field="unit")),
    "series": Member(field="time_series_csv", unit=UnitField(field="unit")),
    "site": Member(field="site_id"),
    "site_name": Member(field="site_name"),
    "quantity": Member(field="quantity"),
    "datum": Member(field="vertical_datum"),
    "depth": Member(field="depth", unit=UnitField(field="depth_unit")),
}


@dataclass(frozen=True, slots=True)
class Accuracy:
    """A published accuracy on the reading's unit and where it is read from, or the code saying why none is;
    ``widest`` is the widest the source cites for it elsewhere (its table, or the set's other sites)."""

    value: float | None
    cite: str = ""
    unread: str = ""
    widest: float | None = None


@dataclass(frozen=True, slots=True)
class Observation:
    """One measured value with where, when and how far away it was measured."""

    value: float
    units: str | None = None
    site_id: str | None = None
    site_name: str | None = None
    sampled: str | None = None
    distance_km: float | None = None
    #: Where the site stands, in lon/lat; ``None`` for a stated number, which stands nowhere.
    lon: float | None = None
    lat: float | None = None
    #: What the value IS, as the reading states it in its own ``quantity`` column; ``None`` where it states none.
    quantity: str | None = None
    #: What the value is counted from - on the slot's datum where the slot reads one, else
    #: as the reading states it - and what the run says about the shift that got it there.
    datum: str | None = None
    datum_note: str = ""
    #: Every instant the record reported over its window, on the slot's own unit
    #: and datum - the same conversion and the same shift the value above rode.
    #: ``None`` where the source reported one moment and nothing more.
    series: Series | None = None
    #: The instant the reading was asked AT, as UTC ISO: the window it was admitted
    #: under closes there. ``None`` where nothing was asked, or the value was stated.
    asked: str | None = None
    #: How far below the land surface the measuring interval ends, as the source
    #: publishes it (a well's depth); the unit it is counted in beside it. ``None``
    #: where the source publishes no depth.
    depth: float | None = None
    depth_units: str | None = None
    #: The published accuracy of the reading, in ``units``; ``None`` for a stated number.
    accuracy: Accuracy | None = None
    #: The published accuracy of the gauge zero the value is lifted onto; ``None`` where no zero lifts it.
    zero_accuracy: Accuracy | None = None

    @property
    def forcing(self) -> Series | float:
        """What an engine is DRIVEN by here: the whole window where the record
        reported one, else the one number.

        The choice between a measured record and a lumped constant is the record's own."""
        return self.series if self.series is not None else self.value


class ObservationError(RuntimeError):
    """A typed refusal: nothing near this place measured what the slot opens on."""

    error_code: str
    retryable: bool = False

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def convert(value: float, units: Any, to_units: Any) -> float:
    """One reading moved onto the unit a slot reads, or a refusal naming both.

    ONE table serves a reading and its window: the runtime's, where "deg C", "degC" and "DEGC" are one row."""
    have, want = spelling(units), spelling(to_units)
    if not want or have == want:
        return float(value)
    if not have:
        raise ObservationError(
            "OBSERVATION_UNIT_UNSTATED",
            f"a reading in no stated unit cannot be read as {to_units!r}. The "
            "unit of a record's value is stated on its member in the source's "
            "fields; a number read in the unit the slot wanted is a different "
            "measurement.")
    try:
        return convert_units(float(value), str(units), str(to_units))
    except TemporalUnitsError as exc:
        raise ObservationError(
            "OBSERVATION_UNIT_UNCONVERTIBLE",
            f"a reading in {units!r} cannot be read as {to_units!r}: {exc}"
        ) from exc


def _features(source: Any) -> list[dict[str, Any]]:
    """The features behind a fetched point layer, properties kept."""
    doc = read_geometry_doc(source)
    if not isinstance(doc, Mapping):
        return []
    if str(doc.get("type") or "") == "FeatureCollection":
        return [f for f in (doc.get("features") or ()) if isinstance(f, dict)]
    return [doc] if doc.get("properties") is not None else []


def _samples(text: str, member: Member | None = None) -> list[tuple[str, float]]:
    """Every readable reading of a station's window, in file order: the source's own reading objects where its
    member names their keys, else ``stamp,value`` rows (a header and an empty value fail the float read and are skipped)."""
    rows: list[tuple[str, float]] = []
    if member is not None and member.time_key:
        try:
            readings = json.loads(str(text or "[]"))
        except ValueError:
            return rows
        for reading in readings if isinstance(readings, list) else ():
            try:
                rows.append((str(reading[member.time_key]), float(reading[member.value_key])))
            except (KeyError, TypeError, ValueError):
                continue
        return rows
    for line in str(text or "").splitlines():
        stamp, _sep, value = line.strip().partition(",")
        try:
            rows.append((stamp, float(value)))
        except ValueError:
            continue
    return rows


def _cadence(rows: list[tuple[str, float]]) -> dt.timedelta:
    """The spacing a record SPEAKS at: the median gap, so one hole does not coarsen it."""
    return _median_gap([taken for taken in (_moment(stamp) for stamp, _ in rows)
                        if taken is not None]) or _LONE_CADENCE


def _median_gap(instants: Sequence[Any]) -> Any:
    """The median spacing of instants in order, ``None`` where no two differ."""
    gaps = sorted(later - earlier for earlier, later in zip(instants, instants[1:])
                  if later > earlier)
    return gaps[len(gaps) // 2] if gaps else None


def _pick(rows: list[tuple[str, float]],
          at: Any) -> tuple[str | None, float] | None:
    """THE row of a record the run OPENS on - one picker, every slot.

    The latest sample at or before ``at``, else the earliest after it within one
    cadence; with no moment asked, the last row."""
    if not rows:
        return None
    asked = _moment(at)
    if asked is None:
        return (rows[-1][0] or None, rows[-1][1])
    before = [row for row in rows if (_moment(row[0]) or asked) <= asked]
    if before:
        return (before[-1][0] or None, before[-1][1])
    ahead = sorted((taken, index) for index, taken in
                   ((index, _moment(row[0])) for index, row in enumerate(rows))
                   if taken is not None and taken - asked <= _cadence(rows))
    if ahead:
        nearest = rows[ahead[0][1]]
        return (nearest[0] or None, nearest[1])
    return None


def _reading(rec: Record, at: Any = None) -> tuple[str | None, float, dt.timedelta] | None:
    """One record's value, the stamp it carries and the cadence it speaks at.

    A windowed record is read AT the moment asked; a single reading is read at the lone cadence,
    stamped by the source's ``time`` member, and undated where it maps none."""
    window = rec.stated("series")
    if window:
        rows = _samples(str(window), rec.fields["series"])
        found = _pick(rows, at)
        if found is not None:
            return (found[0], found[1], _cadence(rows))
    direct = rec.stated("value")
    if direct is not None:
        try:
            stamp = rec.stated("time")
            return (None if stamp is None else str(stamp), float(direct), _LONE_CADENCE)
        except (TypeError, ValueError):
            return None
    return None


def _moment(value: Any) -> dt.datetime | None:
    """One stamp as a UTC instant, or ``None`` where it is absent or unreadable."""
    text = str(value or "").strip()
    if not text:
        return None
    iso = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        read = dt.datetime.fromisoformat(iso)
    except ValueError:
        return None
    return read if read.tzinfo is not None else read.replace(tzinfo=dt.timezone.utc)


def _in_window(sampled: Any, at: Any,
               cadence: dt.timedelta = _LONE_CADENCE) -> bool:
    """Whether a sample speaks for a run asking at ``at``: no older than the window, or at
    most one cadence ahead. An undated sample is outside."""
    taken, asked = _moment(sampled), _moment(at)
    if asked is None:
        return True
    if taken is None:
        return False
    if taken <= asked:
        return asked - taken <= dt.timedelta(days=setting("OBSERVATION_WINDOW_DAYS"))
    return taken - asked <= cadence


def observation(source: Any, *, near: Any = None, fields: Mapping[str, Member] | None = None,
                producer: str = "", to_units: Any = None,
                at: Any = None, window_s: Any = None,
                to_datum: Any = None, offset: Any = None, caption: str = "",
                quantity: str = "state", label: str = "observation",
                code: str = _CODE, every: bool = False,
                off: Mapping[str | None, str] | None = None
                ) -> Observation | tuple[Observation, ...] | None:
    """THE ingestion: a fetched point layer, or a STATED value -> one reading, or with ``every`` each site's.

    A number stands over any record and is already on the slot's datum; a reading on
    another zero reaches ``to_datum`` only through ``offset``. ``near`` ranks several
    candidates, none outside the window closing at ``at`` and none ``off`` names (site id ->
    why it is not this run's water); ``caption`` names the quantity in every refusal and the journal note.
    A record reads by ``fields``, the map its ``producer`` states, its accuracy beside it where the map cites one."""
    if source is None:
        return None
    measures, opens = caption or "this value", _opening(caption)
    stated = _stated(source)
    if stated is not None:
        found = Observation(value=stated,
                            units=str(to_units) if to_units is not None else None,
                            datum=str(to_datum) if to_datum is not None else None)
        _journal(found, opens=opens, stated=True)
        return found
    fields = dict(fields) if fields else dict(_UNSOURCED)
    candidates: list[tuple[float, dict[str, Any], tuple[str | None, float]]] = []
    for feature in _features(source):
        props = feature.get("properties") or {}
        reading = _reading(_record(props, fields, producer, label), at)
        if reading is None:
            continue
        candidates.append((_distance_km(feature, near), feature, reading))
    within = [row for row in candidates if _in_window(row[2][0], at, row[2][2])]
    if candidates and not within:
        undated = ("" if "time" in fields or not any(row[2][0] is None for row in candidates) else
                   f" {label} maps no time member, the field carrying the moment a reading was "
                   "taken, so its readings are undated: state it in the source's fields.")
        raise ObservationError(
            code,
            f"{label} reports {measures} near this domain, but nothing sampled "
            f"within {setting("OBSERVATION_WINDOW_DAYS"):g} days of {at}: the nearest reading is from "
            "another moment, and a sample outside the window this run is about "
            "is not this run's water. State the value on the call, or ask about "
            "a moment a sample was taken near." + undated)
    candidates = _on_the_water(within, off or {}, label, measures, fields, producer)
    if not candidates:
        raise ObservationError(
            code,
            f"nothing in {label} reports {measures}, so the value this run "
            "opens on is not measured anywhere near it. State the value on the "
            "call, or name a source that reaches this place.")
    candidates.sort(key=lambda row: row[0])
    found = tuple(_found(source, feature, sampled, raw, distance_km,
                         _record(feature.get("properties") or {}, fields, producer, label),
                         to_units=to_units,
                         at=at, window_s=window_s, to_datum=to_datum,
                         offset=offset, quantity=quantity, label=label,
                         measures=measures)
                  for distance_km, feature, (sampled, raw, _spacing)
                  in (candidates if every else candidates[:1]))
    if every:
        from .run_notes import journal_note

        found = _widest_of_the_set(found, fields)
        journal_note(f"{len(found)} sites of {label} report {measures} within "
                     f"{setting("OBSERVATION_WINDOW_DAYS"):g} days of {at}, each at its own sample; a "
                     "site the mesh does not hold is left out and said.")
        return found
    _journal(found[0], opens=opens, stated=False)
    return found[0]


def _on_the_water(candidates: list[Any], off: Mapping[str | None, str], label: str,
                  measures: str, fields: Mapping[str, Member], producer: str) -> list[Any]:
    """The candidates standing on this run's water; every one off it refuses, naming each site and why."""
    if not off:
        return candidates
    recs = {id(row): _record(row[1].get("properties") or {}, fields, producer, label) for row in candidates}
    kept = [row for row in candidates if recs[id(row)].site not in off]
    left = [recs[id(row)] for row in candidates if row not in kept]
    if not left:
        return kept
    said = "; ".join(f"{_stated_as(rec, 'site_name') or 'an unnamed site'} "
                     f"({rec.site or 'no site id'}) {off[rec.site]}" for rec in left)
    if candidates and not kept:
        raise ObservationError(
            "OBSERVATION_OFF_THE_REACH",
            f"{label} reports {measures} only at sites that are not this reach's water: {said}. "
            "State the value on the call, or hand the record of a gauge on the reach.")
    from .run_notes import journal_note

    journal_note(f"{label}: left out, not this reach's water - {said}.")
    return kept


def sites(source: Any) -> list[tuple[str | None, str | None, float | None, float | None, str | None, int | None]]:
    """Each site a record reports at: its id, its name, where it stands in lon/lat, its USGS site number
    where the record is NWIS's own (``site_no``) and the NHDPlus COMID where the record's id IS one
    (a National Water Model reach, ``feature_id``); ``None`` for what a record does not state.

    A record reads its id and name by the map of the source that produced it."""
    producer, fields = produced_by(source) or ("", _UNSOURCED)
    rows = []
    for feature in _features(source):
        props = feature.get("properties") or {}
        rec = _record(props, fields, producer, "the record")
        usgs, reach = _text(props, "site_no"), _text(props, "feature_id")
        rows.append((rec.site, _stated_as(rec, "site_name"), *_site(feature),
                     usgs.strip() if usgs and usgs.strip().isdigit() else None,
                     int(reach) if reach and reach.strip().isdigit() else None))
    return rows


def _record(props: Mapping[str, Any], fields: Mapping[str, Member], producer: str,
            label: str) -> Record:
    rec = Record(props, fields, source=producer or label)
    rec.site = _stated_as(rec, "site")
    return rec


def _stated_as(rec: Record, member: str) -> str | None:
    """A member's text off the record, ``None`` where the source maps none or the record leaves it empty."""
    if not rec.maps(member):
        return None
    try:
        found = rec.read(member)
    except FieldError as exc:
        if exc.error_code == "FIELD_EMPTY":
            return None
        raise ObservationError(exc.error_code, str(exc)) from exc
    return str(found).strip() or None


def _depth(rec: Record) -> tuple[float | None, str | None]:
    """How far below the land surface the record's measuring interval ends, and the unit it is counted in."""
    stated, unit = rec.stated("depth"), rec.unit("depth")
    try:
        return (float(stated) if stated is not None else None), unit
    except (TypeError, ValueError):
        return None, unit


def _found(source: Any, feature: Mapping[str, Any], sampled: str | None, raw: float,
           distance_km: float, rec: Record, *, to_units: Any,
           at: Any, window_s: Any, to_datum: Any, offset: Any,
           quantity: str, label: str, measures: str) -> Observation:
    """One site's reading on the slot's unit and datum, with its window where it reported one."""
    props = feature.get("properties") or {}
    units = rec.unit("value") or rec.unit("series")
    zero = _gauge_zero(rec, units, label, measures)
    value = (convert(raw + zero, units, to_units) if to_units is not None
             else float(raw) + zero)
    datum, shift, datum_note = _onto_datum(source, rec, to_datum, offset, label)
    reported = props.get("distance_km")
    lon, lat = _site(feature)
    asked = _moment(at)
    depth, depth_units = _depth(rec)
    return Observation(
        value=float(value) + shift,
        lon=lon, lat=lat, quantity=_stated_as(rec, "quantity"),
        units=str(to_units) if to_units is not None else (
            str(units) if units is not None else None),
        site_id=rec.site,
        site_name=_stated_as(rec, "site_name"),
        sampled=sampled,
        distance_km=float(reported) if reported is not None else (
            distance_km if distance_km != float("inf") else None),
        datum=datum, datum_note=datum_note,
        asked=None if asked is None else asked.isoformat(),
        depth=depth, depth_units=depth_units,
        accuracy=_accuracy(rec, "accuracy", to_units or units),
        zero_accuracy=_accuracy(rec, "above_accuracy", to_units or units) if rec.maps("above") else None,
        series=_series(rec, rec.unit("series") or units,
                       to_units, shift + convert(zero, units, to_units)
                       if to_units is not None else shift + zero,
                       label, at, window_s, quantity))


def sited(at: Any) -> bool:
    """Whether a read is placed at a data input: one reading's site, or each site of a set."""
    return isinstance(at, Observation) or (
        isinstance(at, tuple) and bool(at)
        and all(isinstance(one, Observation) for one in at))


def _series(rec: Record, units: Any,
            to_units: Any, shift: float, label: str, at: Any = None,
            window_s: Any = None, quantity: str = "state") -> Series | None:
    """THE WINDOW this station reported, on the slot's unit and datum, opened at the run's moment.

    It rides the SAME shift as the value; ``None`` for a single reading. A record with
    no unit refuses: 2000 ft3/s read as 2000 m3/s is a different river."""
    rows = _samples(str(rec.stated("series") or ""), rec.fields.get("series"))
    if len(rows) < 2:
        return None
    if to_units is not None and not units:
        raise ObservationError(
            "OBSERVATION_UNIT_UNSTATED",
            f"{label} reports a window in no stated unit and this slot reads "
            f"{to_units!r}. The unit of a record's window is stated on its "
            "member in the source's fields.")
    _covers_the_run(rows, at, window_s, label)
    try:
        found = Series.from_samples(rows, units=str(units), at=at)
    except TemporalShapeError:
        # A record whose stamps repeat or run backwards states no window; the
        # reading above stands and the run is lumped, which the row says.
        return None
    want = str(to_units) if to_units is not None and found.units else None
    try:
        # The record is written at the cadence it was MEASURED at - the engine
        # reads between two rows - so nothing is moved here but the unit; the
        # hole bound is what this alignment is asked to judge.
        found = align(found, units=want, quantity=quantity).series
    except TemporalUnitsError as exc:
        raise ObservationError(
            "OBSERVATION_UNIT_UNCONVERTIBLE",
            f"{label} reports its window in {found.units!r} and this slot "
            f"reads {to_units!r}: {exc}") from exc
    except TemporalGapError as exc:
        raise ObservationError("OBSERVATION_WINDOW_UNCOVERED", str(exc)) from exc
    return found.shifted(shift)


def _gauge_zero(rec: Record, units: Any, label: str, measures: str) -> float:
    """The elevation of the zero this reading is counted from, in the reading's own unit.

    A gauge publishes a HEIGHT above its own datum; a record that states no zero refuses."""
    if not rec.maps("above"):
        return 0.0
    stated = rec.stated("above")
    if stated is None:
        raise ObservationError(
            "OBSERVATION_GAUGE_ZERO_UNSTATED",
            f"{label} reports {measures} as a height above its own zero and "
            f"publishes no {rec.field('above')!r}, so there is nothing to place it on "
            "the frame this run is solved on. State the elevation on the call, "
            "or ask about a site whose datum is published.")
    return convert(float(stated), rec.unit("above") or units, units)


def _accuracy(rec: Record, name: str, to_units: Any) -> Accuracy:
    """A member's published accuracy on the reading's unit; where none is cited, the refusal's code instead."""
    try:
        widest = max((convert(m.value, m.unit, to_units) for m in rec.cited(name)), default=None)
    except (FieldError, ObservationError):
        widest = None
    try:
        found = rec.read(name)
        return Accuracy(convert(found.value, found.unit, to_units), cite=rec.cite(name), widest=widest)
    except (FieldError, ObservationError) as exc:
        return Accuracy(None, cite=rec.cite(name) if rec.maps(name) else "", unread=exc.error_code, widest=widest)


def _widest_of_the_set(found: tuple[Observation, ...], fields: Mapping[str, Member]) -> tuple[Observation, ...]:
    """Each site's accuracy told the widest any site of the set cites, where the source states it per record."""
    for name in ("accuracy", "zero_accuracy"):
        member = fields.get("above_accuracy" if name == "zero_accuracy" else name)
        if member is None or not member.field or member.table is not None:
            continue
        stated = [(getattr(one, name).value, one.units) for one in found
                  if getattr(one, name) is not None and getattr(one, name).value is not None]
        if not stated:
            continue
        found = tuple(one if getattr(one, name) is None else replace(one, **{name: replace(
            getattr(one, name), widest=max(convert(value, units, one.units) for value, units in stated))})
            for one in found)
    return found


def _covers_the_run(rows: list[tuple[str, float]], at: Any,
                    window_s: Any, label: str) -> None:
    """Refuse a record that does not span the window this run solves over.

    A record stopping early would be read flat past its last sample, a forcing nobody measured."""
    opens = _moment(at)
    if opens is None or window_s is None:
        return
    first, last = _moment(rows[0][0]), _moment(rows[-1][0])
    if first is None or last is None:
        return
    closes = opens + dt.timedelta(seconds=float(window_s))
    if first <= opens and closes <= last:
        return
    nearest = first if abs((first - opens).total_seconds()) < \
        abs((last - opens).total_seconds()) else last
    raise ObservationError(
        "OBSERVATION_WINDOW_UNCOVERED",
        f"{label} reports from {rows[0][0]} to {rows[-1][0]}, and this run "
        f"opens at {opens.isoformat()} and closes at {closes.isoformat()}: "
        f"the nearest sample it holds is {nearest.isoformat()}. Ask about a "
        "moment the record spans, state the value on the call, or state the "
        "window loosened, which takes the record as a stand-in and says so.")


def _onto_datum(source: Any, rec: Record, to_datum: Any,
                offset: Any, label: str) -> tuple[str | None, float, str]:
    """This reading on the datum the slot reads, and what the shift cost.

    A slot naming no datum is not asking for an elevation: nothing is checked or shifted, and the reading's own zero is kept."""
    if to_datum is None:
        return (_stated_as(rec, "datum"), 0.0, "")
    on = {"vertical_datum": _stated_as(rec, "datum") or datum_of(source),
          "name": rec.site or label}
    try:
        aligned = onto_frame(on, to_datum, offset=offset,
                             code_prefix="OBSERVATION_")
    except DatumError as exc:
        raise ObservationError(exc.error_code, str(exc)) from exc
    return (aligned.datum, aligned.shift_m, aligned.note)


def _opening(caption: str) -> str:
    """What the run journal opens its sentence with, off the caption ("a streamflow" -> "the"); no caption, nothing said."""
    words = str(caption or "").split()
    if not words:
        return ""
    if words[0].lower() in ("a", "an", "the"):
        words = words[1:]
    return f"the {' '.join(words)} opens at"


def _stated(source: Any) -> float | None:
    """The value a caller STATED, or ``None`` when the source is a record; a bool is not a reading."""
    if isinstance(source, bool):
        return None
    if isinstance(source, (int, float)):
        return float(source)
    return None


def _journal(found: Observation, *, opens: str, stated: bool) -> None:
    """Say on the run journal what this run opened on; off a run there is no journal."""
    if not opens:
        return
    from .run_notes import journal_note

    units = f" {found.units}" if found.units else ""
    on = f" on {found.datum}" if found.datum else ""
    journal_note(f"{opens} {found.value:.3f}{units}{on}, the value stated on the "
                 "call, which stands over any record."
                 if stated else note(found, opens=opens))


def note(found: Observation, *, opens: str) -> str:
    """What the run SAYS about a reading it opened on: the site, how far off it
    is, when it was taken, and that a sample is a moment rather than a mean."""
    where = found.site_name or found.site_id or "an unnamed site"
    how_far = (f", {found.distance_km:.1f} km away" if found.distance_km is not None
               else "")
    when = f" on {found.sampled}" if found.sampled else " at an undated moment"
    units = f" {found.units}" if found.units else ""
    shifted = f" It was {found.datum_note}." if found.datum_note else ""
    return (f"{opens} {found.value:.3f}{units}, which {where}"
            f"{how_far} reported{when}.{shifted} That is a SAMPLE at a moment, "
            "not a mean: the run's own forcing is what moves it from there.")


def _text(props: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = props.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return None


def _site(feature: Mapping[str, Any]) -> tuple[float | None, float | None]:
    """Where a point feature stands, in lon/lat; ``(None, None)`` for any other geometry."""
    geometry = feature.get("geometry") or {}
    coords = geometry.get("coordinates")
    if geometry.get("type") != "Point" or not isinstance(coords, (list, tuple)) \
            or len(coords) < 2:
        return (None, None)
    return (float(coords[0]), float(coords[1]))


def _distance_km(feature: Mapping[str, Any], near: Any) -> float:
    """How far a feature is from the place asked about; ``inf`` orders nothing.

    Ranking only orders candidates over a local box, so flat-earth distance suffices."""
    from trid3nt.inputs.point import lonlat_of

    place = lonlat_of(near)
    if place is None:
        return float("inf")
    lon, lat = place
    coords = (feature.get("geometry") or {}).get("coordinates")
    while isinstance(coords, (list, tuple)) and coords and \
            isinstance(coords[0], (list, tuple)):
        coords = coords[0]
    if not (isinstance(coords, (list, tuple)) and len(coords) >= 2):
        return float("inf")
    east = (float(coords[0]) - lon) * 111.32 * math.cos(math.radians(lat))
    north = (float(coords[1]) - lat) * 110.57
    return math.hypot(east, north)
