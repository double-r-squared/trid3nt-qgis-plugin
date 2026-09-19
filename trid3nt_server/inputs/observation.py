"""An OBSERVATION: one measured value a slot opens on, read off what was fetched.

A gauge layer, a sample-site layer and a user's own point layer all carry the
same thing - somebody measured something somewhere at some time - and a slot
that opens on it needs the number in ITS unit, on ITS datum, with the site, the
distance and the date travelling beside it. A record that reported a WINDOW
carries its whole series beside the reading, because a value the engine takes
as a file of instants is lumped only where nothing measured one. A reading counted from a zero the
slot does not read on moves onto it through an offset row the template names,
never silently. A sample is a moment, never a climatology, so what is read is
said on the run journal rather than folded into the answer.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any, Mapping

from trid3nt_server.workflows.runtime.temporal import (
    Series,
    convert_units,
    TemporalShapeError,
    TemporalUnitsError,
)

from .geometry import read_geometry_doc, source_uri
from .vertical_datum import DatumError, datum_of, onto_frame

__all__ = ["Observation", "ObservationError", "convert", "note",
           "observation"]

logger = logging.getLogger("trid3nt_server.inputs.observation")

_CODE = "OBSERVATION_INVALID"

#: The unit names a reading arrives under, by the quantity they measure. A
#: portal federates state and federal programs, so the unit travels with the ROW
#: rather than being the portal's: a Fahrenheit row is converted by name, and two
#: spellings of one unit - "deg C" beside "degC" - are one unit.
_FAHRENHEIT = ("degf", "f", "deg f", "fahrenheit")
_CELSIUS = ("degc", "c", "deg c", "celsius")

#: What a source calls the MOMENT it reported, and what it calls the thing that
#: reported. A sample portal names a site; a gridded analysis names the reach it
#: published for and the cycle it published at, and both are what a reader of
#: the journal note needs to find the number again.
_STAMP_FIELDS = ("result_date", "valid_time", "datetime", "date_time")
_SITE_FIELDS = ("site_id", "station_id", "feature_id")

#: How far back of the moment a run asks at a SAMPLE still speaks for the water
#: it was taken from. A month: what a body carries drifts with the season, so a
#: reading from the same month of the same winter is the water this run opens
#: on and one from another decade is a different river. A row that states no
#: moment is asking about none, and every sample it fetched is a candidate.
_WINDOW_DAYS = 30.0

#: What a ROW calls the zero its elevation is counted from. A portal that
#: federates programs publishes readings on several, so the datum rides on the
#: row and the layer's own is read only where the rows state none.
_DATUM_FIELD = "vertical_datum"


@dataclass(frozen=True, slots=True)
class Observation:
    """One measured value with where, when and how far away it was measured."""

    value: float
    units: str | None = None
    site_id: str | None = None
    site_name: str | None = None
    sampled: str | None = None
    distance_km: float | None = None
    #: What the value is counted from, once it is on the slot's datum, and what
    #: the run says about the shift that got it there. Both empty on a reading
    #: that is not an elevation.
    datum: str | None = None
    datum_note: str = ""
    #: Every instant the record reported over its window, on the slot's own unit
    #: and datum - the same conversion and the same shift the value above rode.
    #: ``None`` where the source reported one moment and nothing more.
    series: Series | None = None


class ObservationError(RuntimeError):
    """A typed refusal: nothing near this place measured what the slot opens on."""

    error_code: str
    retryable: bool = False

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def _normal(unit: Any) -> str:
    """One unit's spelling, as every source spells it: "deg C", "degC", "°C"."""
    return "".join(str(unit or "").lower().replace("°", "").replace(".", "").split())


def convert(value: float, units: Any, to_units: Any) -> float:
    """One reading moved onto the unit a slot reads, or a refusal naming both.

    ONE table for a reading and for the window it came out of: the runtime's,
    which is where a conversion is declared and a reader can check it. The
    spellings a portal federates - "deg C" beside "degC" - are one unit here
    before the table is asked."""
    have, want = _normal(units), _normal(to_units)
    if not want or have == want or any(have in family and want in family
                                       for family in (_CELSIUS, _FAHRENHEIT)):
        return float(value)
    if have in _FAHRENHEIT and want in _CELSIUS:
        return (float(value) - 32.0) / 1.8
    if have in _CELSIUS and want in _FAHRENHEIT:
        return float(value) * 1.8 + 32.0
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


def _samples(csv_text: str) -> list[tuple[str, float]]:
    """Every readable ``stamp,value`` row of a station's series, in file order.

    A header row and a gap the source writes as an empty value both fail the
    float read and are skipped: the row is not a reading."""
    rows: list[tuple[str, float]] = []
    for line in str(csv_text or "").splitlines():
        stamp, _sep, value = line.strip().partition(",")
        try:
            rows.append((stamp, float(value)))
        except ValueError:
            continue
    return rows


def _last_sample(csv_text: str) -> tuple[str | None, float] | None:
    """The LAST readable ``stamp,value`` row of a station's series."""
    rows = _samples(csv_text)
    return (rows[-1][0] or None, rows[-1][1]) if rows else None


def _reading(props: Mapping[str, Any], field: str,
             series_field: str) -> tuple[str | None, float] | None:
    """One feature's value and the stamp it carries, from a column or a series."""
    direct = props.get(field)
    if direct is not None:
        try:
            return (_text(props, *_STAMP_FIELDS), float(direct))
        except (TypeError, ValueError):
            return None
    if series_field and props.get(series_field):
        return _last_sample(str(props[series_field]))
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


def _in_window(sampled: Any, at: Any) -> bool:
    """Whether a sample speaks for a run asking at ``at``: taken on or before
    that moment and no older than the window.

    An undated sample is never shown to be inside it, so it is outside."""
    taken, asked = _moment(sampled), _moment(at)
    if asked is None:
        return True
    if taken is None:
        return False
    return dt.timedelta(0) <= asked - taken <= dt.timedelta(days=_WINDOW_DAYS)


def observation(source: Any, *, near: Any = None, field: str = "value",
                units_field: str = "unit", to_units: Any = None,
                series_field: str = "time_series_csv",
                record_units: Any = None,
                at: Any = None, to_datum: Any = None, offset: Any = None,
                measures: str = "this value", opens: str = "",
                label: str = "observation",
                code: str = _CODE) -> Observation | None:
    """THE ingestion: a fetched point layer, or a STATED value -> one reading.

    ``near`` ranks the candidates when the source carries several - a fetch that
    already asked for the nearest station returns one and nothing is ranked; a
    sample outside the window that closes at ``at`` is not a sample for this run
    and is never ranked at all; ``to_units`` is the unit the slot reads and
    ``to_datum`` the zero it counts from, which a reading on another zero
    reaches only through the ``offset`` row; ``record_units`` is the unit a
    source that names none per site reports in. A number is the value the caller
    stated, which stands over any record and is already on the slot's own datum;
    ``opens`` says on the run journal what this run opened on, because a sample
    is a moment and its age is the reader's business. Nothing that reports
    refuses typed."""
    if source is None:
        return None
    stated = _stated(source)
    if stated is not None:
        found = Observation(value=stated,
                            units=str(to_units) if to_units is not None else None,
                            datum=str(to_datum) if to_datum is not None else None)
        _journal(found, opens=opens, stated=True)
        return found
    candidates: list[tuple[float, dict[str, Any], tuple[str | None, float]]] = []
    for feature in _features(source):
        props = feature.get("properties") or {}
        reading = _reading(props, field, series_field)
        if reading is None:
            continue
        candidates.append((_distance_km(feature, near), feature, reading))
    within = [row for row in candidates if _in_window(row[2][0], at)]
    if candidates and not within:
        raise ObservationError(
            code,
            f"{label} reports {measures} near this domain, but nothing sampled "
            f"within {_WINDOW_DAYS:g} days of {at}: the nearest reading is from "
            "another moment, and a sample outside the window this run is about "
            "is not this run's water. State the value on the call, or ask about "
            "a moment a sample was taken near.")
    candidates = within
    if not candidates:
        raise ObservationError(
            code,
            f"nothing in {label} reports {measures}, so the value this run "
            "opens on is not measured anywhere near it. State the value on the "
            "call, or name a source that reaches this place.")
    distance_km, feature, (sampled, raw) = min(candidates, key=lambda row: row[0])
    props = feature.get("properties") or {}
    units = props.get(units_field) or record_units
    value = convert(raw, units, to_units) if to_units is not None else float(raw)
    datum, shift, datum_note = _onto_datum(source, props, to_datum, offset, label)
    reported = props.get("distance_km")
    found = Observation(
        value=float(value) + shift,
        units=str(to_units) if to_units is not None else (
            str(units) if units is not None else None),
        site_id=_text(props, *_SITE_FIELDS),
        site_name=_text(props, "site_name", "station_name"),
        sampled=sampled,
        distance_km=float(reported) if reported is not None else (
            distance_km if distance_km != float("inf") else None),
        datum=datum, datum_note=datum_note,
        series=_series(props, series_field, units, to_units,
                       shift, label))
    _journal(found, opens=opens, stated=False)
    return found


def _series(props: Mapping[str, Any], series_field: str, units: Any,
            to_units: Any, shift: float, label: str) -> Series | None:
    """THE WINDOW this station reported, on the slot's own unit and datum.

    The series rides the SAME shift the value did rather than a second one
    derived off the same row, and ``None`` where the record reported one moment
    - a single reading has no interval a reader could read between."""
    rows = _samples(str(props.get(series_field) or "")) if series_field else []
    if len(rows) < 2:
        return None
    try:
        found = Series.from_samples(rows, units=str(units or to_units or ""))
    except TemporalShapeError:
        # A record whose stamps repeat or run backwards states no window; the
        # reading above stands and the run is lumped, which the row says.
        return None
    if to_units is not None and found.units and str(to_units) != found.units:
        try:
            found = found.in_units(str(to_units))
        except TemporalUnitsError as exc:
            raise ObservationError(
                "OBSERVATION_UNIT_UNCONVERTIBLE",
                f"{label} reports its window in {found.units!r} and this slot "
                f"reads {to_units!r}: {exc}") from exc
    return found.shifted(shift)


def _onto_datum(source: Any, props: Mapping[str, Any], to_datum: Any,
                offset: Any, label: str) -> tuple[str | None, float, str]:
    """This reading on the datum the slot reads, and what the shift cost.

    A slot that names no datum is not asking for an elevation, so nothing is
    checked."""
    if to_datum is None:
        return (None, 0.0, "")
    on = {"vertical_datum": props.get(_DATUM_FIELD) or datum_of(source),
          "name": _text(props, *_SITE_FIELDS) or label}
    try:
        aligned = onto_frame(on, to_datum, offset=offset,
                             code_prefix="OBSERVATION_")
    except DatumError as exc:
        raise ObservationError(exc.error_code, str(exc)) from exc
    return (aligned.datum, aligned.shift_m, aligned.note)


def _stated(source: Any) -> float | None:
    """The value a caller STATED, or ``None`` when the source is a record.

    A bool is not a reading: ``True`` would enter the sheet as 1.0."""
    if isinstance(source, bool):
        return None
    if isinstance(source, (int, float)):
        return float(source)
    return None


def _journal(found: Observation, *, opens: str, stated: bool) -> None:
    """Say on the run journal what this run opened on, where it says what it is.

    Off a run there is no journal and nothing is said."""
    if not opens:
        return
    from trid3nt_server.workflows.runtime import journal_note

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


def _distance_km(feature: Mapping[str, Any], near: Any) -> float:
    """How far a feature is from the place asked about; ``inf`` orders nothing."""
    from trid3nt_server.tools.fetchers._router.transforms.nearest import (
        km_between,
    )

    if near is None:
        return float("inf")
    lon, lat = ((float(near.lon), float(near.lat))
                if hasattr(near, "lon") and hasattr(near, "lat")
                else (float(near[0]), float(near[1])))
    coords = (feature.get("geometry") or {}).get("coordinates")
    while isinstance(coords, (list, tuple)) and coords and \
            isinstance(coords[0], (list, tuple)):
        coords = coords[0]
    if not (isinstance(coords, (list, tuple)) and len(coords) >= 2):
        return float("inf")
    return km_between(lon, lat, float(coords[0]), float(coords[1]))
