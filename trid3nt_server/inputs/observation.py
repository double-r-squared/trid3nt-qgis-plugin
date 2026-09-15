"""An OBSERVATION: one measured value a slot opens on, read off what was fetched.

A gauge layer, a sample-site layer and a user's own point layer all carry the
same thing - somebody measured something somewhere at some time - and a slot
that opens on it needs the number in ITS unit with the site, the distance and
the date travelling beside it. A sample is a moment, never a climatology, so
what is read is said on the run journal rather than folded into the answer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping

from .geometry import read_geometry_doc, source_uri

__all__ = ["Observation", "ObservationError", "convert", "note",
           "observation"]

logger = logging.getLogger("trid3nt_server.inputs.observation")

_CODE = "OBSERVATION_INVALID"

#: The unit names a reading arrives under, by the quantity they measure. A
#: portal federates state and federal programs, so the unit travels with the ROW
#: rather than being the portal's, and a Fahrenheit row is converted by name.
_FAHRENHEIT = ("degf", "f", "deg f", "fahrenheit")
_CELSIUS = ("degc", "c", "deg c", "celsius", "")


@dataclass(frozen=True, slots=True)
class Observation:
    """One measured value with where, when and how far away it was measured."""

    value: float
    units: str | None = None
    site_id: str | None = None
    site_name: str | None = None
    sampled: str | None = None
    distance_km: float | None = None


class ObservationError(RuntimeError):
    """A typed refusal: nothing near this place measured what the slot opens on."""

    error_code: str
    retryable: bool = False

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def _normal(unit: Any) -> str:
    return str(unit or "").strip().lower().replace("°", "").replace(".", "")


def convert(value: float, units: Any, to_units: Any) -> float:
    """One reading moved onto the unit a slot reads, or a refusal naming both.

    Only pairs something actually asks for are convertible; anything else
    refuses rather than passing a number through under the wrong name."""
    have, want = _normal(units), _normal(to_units)
    if not want or have == want:
        return float(value)
    if have in _FAHRENHEIT and want in _CELSIUS:
        return (float(value) - 32.0) / 1.8
    if have in _CELSIUS and want in _FAHRENHEIT:
        return float(value) * 1.8 + 32.0
    raise ObservationError(
        "OBSERVATION_UNIT_UNCONVERTIBLE",
        f"a reading in {units!r} cannot be read as {to_units!r}: no conversion "
        "between the two is stated anywhere. Ask the source for the unit the "
        "slot reads, or state the value on the call.")


def _features(source: Any) -> list[dict[str, Any]]:
    """The features behind a fetched point layer, properties kept."""
    doc = read_geometry_doc(source)
    if not isinstance(doc, Mapping):
        return []
    if str(doc.get("type") or "") == "FeatureCollection":
        return [f for f in (doc.get("features") or ()) if isinstance(f, dict)]
    return [doc] if doc.get("properties") is not None else []


def _last_sample(csv_text: str) -> tuple[str | None, float] | None:
    """The LAST readable ``stamp,value`` row of a station's series."""
    for line in reversed(str(csv_text or "").splitlines()):
        stamp, _sep, value = line.strip().partition(",")
        try:
            return (stamp or None, float(value))
        except ValueError:
            continue
    return None


def _reading(props: Mapping[str, Any], field: str,
             series_field: str) -> tuple[str | None, float] | None:
    """One feature's value and the stamp it carries, from a column or a series."""
    direct = props.get(field)
    if direct is not None:
        try:
            return (str(props.get("result_date") or "").strip() or None, float(direct))
        except (TypeError, ValueError):
            return None
    if series_field and props.get(series_field):
        return _last_sample(str(props[series_field]))
    return None


def observation(source: Any, *, near: Any = None, field: str = "value",
                units_field: str = "unit", to_units: Any = None,
                series_field: str = "time_series_csv",
                measures: str = "this value", label: str = "observation",
                code: str = _CODE) -> Observation:
    """THE ingestion: a fetched or supplied point layer -> ONE measured value.

    ``near`` ranks the candidates when the source carries several - a fetch that
    already asked for the nearest station returns one and nothing is ranked.
    ``to_units`` is the unit the slot reads; nothing that reports refuses typed."""
    candidates: list[tuple[float, dict[str, Any], tuple[str | None, float]]] = []
    for feature in _features(source):
        props = feature.get("properties") or {}
        reading = _reading(props, field, series_field)
        if reading is None:
            continue
        candidates.append((_distance_km(feature, near), feature, reading))
    if not candidates:
        raise ObservationError(
            code,
            f"nothing in {label} reports {measures}, so the value this run "
            "opens on is not measured anywhere near it. State the value on the "
            "call, or name a source that reaches this place.")
    distance_km, feature, (sampled, raw) = min(candidates, key=lambda row: row[0])
    props = feature.get("properties") or {}
    units = props.get(units_field)
    value = convert(raw, units, to_units) if to_units is not None else float(raw)
    stated = props.get("distance_km")
    return Observation(
        value=float(value),
        units=str(to_units) if to_units is not None else (
            str(units) if units is not None else None),
        site_id=_text(props, "site_id", "station_id"),
        site_name=_text(props, "site_name", "station_name"),
        sampled=sampled,
        distance_km=float(stated) if stated is not None else (
            distance_km if distance_km != float("inf") else None))


def note(found: Observation, *, opens: str) -> str:
    """What the run SAYS about a reading it opened on: the site, how far off it
    is, when it was taken, and that a sample is a moment rather than a mean."""
    where = found.site_name or found.site_id or "an unnamed site"
    how_far = (f", {found.distance_km:.1f} km away" if found.distance_km is not None
               else "")
    when = f" on {found.sampled}" if found.sampled else " at an undated moment"
    units = f" {found.units}" if found.units else ""
    return (f"{opens} {found.value:.3f}{units}, which {where}"
            f"{how_far} reported{when}. That is a SAMPLE at a moment, not a "
            "mean: the run's own forcing is what moves it from there.")


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
    lon, lat = float(near[0]), float(near[1])
    coords = (feature.get("geometry") or {}).get("coordinates")
    while isinstance(coords, (list, tuple)) and coords and \
            isinstance(coords[0], (list, tuple)):
        coords = coords[0]
    if not (isinstance(coords, (list, tuple)) and len(coords) >= 2):
        return float("inf")
    return km_between(lon, lat, float(coords[0]), float(coords[1]))
