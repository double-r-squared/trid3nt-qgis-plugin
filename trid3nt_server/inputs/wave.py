"""A WAVE: the sea state measured near an open edge, as the record reports it.

A height, a peak period and the bearing the waves come FROM, each in its own unit.
What an engine forces its boundary at - a frequency, a bearing run toward - is that
engine's reading of these three.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping

from trid3nt_contracts.coverage import Member

from .observation import Observation, ObservationError, observation

__all__ = ["Wave", "wave"]

logger = logging.getLogger("trid3nt_server.inputs.wave")

_CODE = "WAVE_INVALID"

#: The unit each of the three quantities a sea state is is read in, and what it measures; each is the
#: member of that name in the source's field map, naming the window the record reports it over.
_HEIGHT = ("m", "a wave height")
_PERIOD = ("s", "a wave period")
_BEARING = ("degT", "a wave direction")


@dataclass(frozen=True, slots=True)
class Wave:
    """One measured sea state: its height, its peak period, and where it comes from."""

    height_m: float
    peak_period_s: float
    #: The bearing the waves come FROM, in degrees true.
    from_direction_deg: float
    site_id: str | None = None
    site_name: str | None = None
    sampled: str | None = None
    distance_km: float | None = None


def wave(source: Any, *, near: Any = None, at: Any = None,
         fields: Mapping[str, Member] | None = None, producer: str = "",
         window_s: Any = None, caption: str = "",
         label: str = "wave", code: str = _CODE) -> Wave | None:
    """THE ingestion: a fetched sea-state record -> the one sea state measured.

    ``fields`` is the source's map, whose ``height``, ``period`` and ``from_direction`` name each
    quantity's window; ``near`` ranks the candidates and ``at``/``window_s`` pick the sea state measured
    when the run is about."""
    if source is None:
        return None
    read = {
        name: _measured(source, dict(fields or {}), name, unit, measures, near=near, at=at,
                        window_s=window_s, label=label, code=code, producer=producer)
        for name, (unit, measures) in (("height", _HEIGHT),
                                       ("period", _PERIOD),
                                       ("from_direction", _BEARING))}
    height, period, heading = read["height"], read["period"], read["from_direction"]
    if period.value <= 0.0:
        raise ObservationError(
            code,
            f"{label} reports a wave period of {period.value:g} s: a period of "
            "zero or less is not a sea state.")
    found = Wave(height_m=float(height.value),
                 peak_period_s=float(period.value),
                 from_direction_deg=float(heading.value),
                 site_id=height.site_id, site_name=height.site_name,
                 sampled=height.sampled, distance_km=height.distance_km)
    _journal(found, caption=caption or "the sea state")
    return found


def _measured(source: Any, fields: Mapping[str, Member], name: str, unit: str,
              measures: str, **asked: Any) -> Observation:
    """One quantity of the sea state, off the window its member names."""
    label, code = str(asked.pop("label")), str(asked.pop("code"))
    member = fields.get(name)
    if member is None:
        raise ObservationError(
            code,
            f"{label} has to report {measures}, and its source maps no {name!r} "
            f"(it maps {', '.join(sorted(fields)) or 'nothing'}). Name a source that measures it.")
    who = {k: v for k, v in fields.items() if k in ("site", "site_name", "time")}
    found = observation(source, fields={**who, "series": member}, to_units=unit,
                        label=label, code=code, **asked)
    if found is None:
        raise ObservationError(
            code, f"{label} was handed no record, so {measures} was not read.")
    return found


def _journal(found: Wave, *, caption: str) -> None:
    """Say on the run journal what sea state was read and who reported it; off a run, nothing."""
    from .run_notes import journal_note

    where = found.site_name or found.site_id or "an unnamed buoy"
    how_far = (f", {found.distance_km:.1f} km away" if found.distance_km is not None
               else "")
    when = f" on {found.sampled}" if found.sampled else " at an undated moment"
    journal_note(
        f"{caption} is {found.height_m:.2f} m at {found.peak_period_s:.1f} s "
        f"from {found.from_direction_deg:.0f} deg true, which {where}{how_far} "
        f"reported{when}.")
