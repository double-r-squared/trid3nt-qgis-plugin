"""A WAVE: the sea state at an open boundary, in the words a wave deck states it.

A moored buoy reports a height, a period and a direction over one window, and a
spectral deck forces its open edge with the three as scalars - so this is where
a record's columns become the boundary keywords, and where the two conversions
between them live. A PEAK FREQUENCY is one over the period the buoy reported; a
deck's MAIN DIRECTION is the bearing the waves run toward, while a met record
publishes the one they come from. Both are said on the run journal rather than
folded silently into a number.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping

from trid3nt_server.workflows.runtime.temporal import spelling

from .observation import Observation, ObservationError, observation

__all__ = ["Wave", "wave"]

logger = logging.getLogger("trid3nt_server.inputs.wave")

_CODE = "WAVE_INVALID"

#: The unit each of the three quantities a sea state is is reported in, and what
#: the deck reads it as. A source states the unit of EVERY column it carries, so
#: the column a quantity is read from is the one reported in that quantity's own
#: unit - a record that carries none of them measures no sea state, and one that
#: carries two refuses rather than picking.
_HEIGHT = ("m", "a wave height")
_PERIOD = ("s", "a wave period")
_BEARING = ("degT", "a wave direction")

#: What every source in this tree writes into the name of the column carrying a
#: WINDOW rather than a single reading. A row names the pair its own reading is
#: taken from and nothing else, so the window behind another of its columns is
#: found by that word.
_WINDOW = "series"


@dataclass(frozen=True, slots=True)
class Wave:
    """One measured sea state, as the values a boundary spectrum is stated at."""

    #: BOUNDARY SIGNIFICANT WAVE HEIGHT, in metres.
    height_m: float
    #: BOUNDARY PEAK FREQUENCY, in hertz - one over the period reported.
    peak_frequency_hz: float
    #: BOUNDARY MAIN DIRECTION 1, as the bearing the waves run TOWARD.
    direction_deg: float
    #: What the record itself reported, before the two conversions above.
    peak_period_s: float
    from_direction_deg: float
    site_id: str | None = None
    site_name: str | None = None
    sampled: str | None = None
    distance_km: float | None = None


def wave(source: Any, *, near: Any = None, at: Any = None,
         column_units: Any = None, window_s: Any = None, caption: str = "",
         label: str = "wave", code: str = _CODE) -> Wave | None:
    """THE ingestion: a fetched sea-state record -> the boundary keywords' values.

    ``column_units`` is the source's own statement of what each of its columns
    is reported in, which is what says which column carries which quantity;
    ``near`` ranks the candidates, ``at`` is the moment the run opens at and
    ``window_s`` how long it covers, so the sea state read is the one measured
    when the run is about."""
    if source is None:
        return None
    columns = dict(column_units or {})
    read = {
        name: _measured(source, columns, unit, measures, near=near, at=at,
                        window_s=window_s, label=label, code=code)
        for name, (unit, measures) in (("height", _HEIGHT),
                                       ("period", _PERIOD),
                                       ("heading", _BEARING))}
    height, period, heading = read["height"], read["period"], read["heading"]
    if period.value <= 0.0:
        raise ObservationError(
            code,
            f"{label} reports a wave period of {period.value:g} s, and a peak "
            "FREQUENCY is one over it: a period of zero or less is not a sea "
            "state a boundary can be forced at.")
    found = Wave(height_m=float(height.value),
                 peak_frequency_hz=1.0 / float(period.value),
                 direction_deg=(float(heading.value) + 180.0) % 360.0,
                 peak_period_s=float(period.value),
                 from_direction_deg=float(heading.value),
                 site_id=height.site_id, site_name=height.site_name,
                 sampled=height.sampled, distance_km=height.distance_km)
    _journal(found, caption=caption or "the sea state")
    return found


def _measured(source: Any, columns: Mapping[str, str], unit: str,
              measures: str, **asked: Any) -> Observation:
    """One quantity of the sea state, off the column reported in its own unit."""
    label, code = str(asked.pop("label")), str(asked.pop("code"))
    field, window = _column(columns, unit, measures, label, code)
    found = observation(source, field=field, series_field=window,
                        column_units=dict(columns), to_units=unit,
                        label=label, code=code, **asked)
    if found is None:
        raise ObservationError(
            code, f"{label} was handed no record, so {measures} was not read.")
    return found


def _column(columns: Mapping[str, str], unit: str, measures: str, label: str,
            code: str) -> tuple[str, str]:
    """The column carrying one quantity, and the one carrying its window.

    A source states the unit of every column it publishes, so a quantity is
    read off the column reported in that quantity's unit; where the source also
    publishes the window behind it, the reading is taken at the moment the run
    opens at rather than at whatever the record last held."""
    named = [name for name, stated in columns.items()
             if spelling(stated) == spelling(unit)]
    windows = [name for name in named if _WINDOW in name]
    values = [name for name in named if _WINDOW not in name]
    if len(values) != 1:
        raise ObservationError(
            code,
            f"{label} has to report {measures}, in {unit}, and it states "
            + (f"{len(values)} columns in that unit ({', '.join(values)}): a "
               "sea state read off whichever of them came first is not a "
               "measurement anybody took."
               if values else
               f"none (it states {', '.join(columns) or 'no column at all'}). "
               "Name a source that measures it."))
    return (values[0], windows[0] if len(windows) == 1 else "")


def _journal(found: Wave, *, caption: str) -> None:
    """Say on the run journal what sea state the boundary was forced at, and
    under which conventions the two numbers beside it were turned.

    Off a run there is no journal and nothing is said."""
    from trid3nt_server.workflows.runtime import journal_note

    where = found.site_name or found.site_id or "an unnamed buoy"
    how_far = (f", {found.distance_km:.1f} km away" if found.distance_km is not None
               else "")
    when = f" on {found.sampled}" if found.sampled else " at an undated moment"
    journal_note(
        f"{caption} is {found.height_m:.2f} m at {found.peak_period_s:.1f} s "
        f"from {found.from_direction_deg:.0f} deg true, which {where}{how_far} "
        f"reported{when}. The deck is forced at a peak frequency of "
        f"{found.peak_frequency_hz:.4f} Hz - one over that period - running "
        f"TOWARD {found.direction_deg:.0f} deg, which is the bearing the "
        "record's own is turned through 180 to reach.")
