"""The storm file a sheet's Storm composite names, written by Clawpack's own ``Storm`` class.

``storm_track`` turns the fixes ``fetch_storm_tracks`` returns for ONE storm into the SI
rows the engine reads, counted in seconds from the run's reference instant; ``write_stormfiles``
writes them in GeoClaw's storm format through Clawpack's writer, which omits a fix with no radius of
maximum wind and gives a missing outer radius its 500 km default. A track with too few fixes left to span the run refuses.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..errors import GeoclawError

__all__ = ["STORM", "landfall_instant", "storm_track", "write_stormfiles"]

#: The key a composite's file entry carries its track under.
STORM = "storm"

#: A fix's published fields -> the Clawpack ``Storm`` attribute, the unit it is published in and the unit the engine reads.
_FIELDS = {
    "wind_kt": ("max_wind_speed", "knots", "m/s"),
    "rmw_nmi": ("max_wind_radius", "nmi", "m"),
    "pres_mb": ("central_pressure", "mbar", "Pa"),
    "roci_nmi": ("storm_radius", "nmi", "m"),
}


def _refuse(code: str, message: str) -> None:
    raise GeoclawError(message, error_code=f"GEOCLAW_{code}")


def _utc(text: str) -> dt.datetime:
    """An ISO-8601 instant -> a naive UTC datetime (the storm format carries no zone)."""
    moment = dt.datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    return moment.astimezone(dt.timezone.utc).replace(tzinfo=None) if moment.tzinfo else moment


def _fixes(doc: Any) -> list[dict[str, Any]]:
    """The one storm's fixes of a points document, in time order; none or several storms refuse."""
    features = doc.get("features") if isinstance(doc, Mapping) else None
    rows = [{**f["properties"], "longitude": f["geometry"]["coordinates"][0], "latitude": f["geometry"]["coordinates"][1]}
            for f in features or () if isinstance(f.get("geometry"), Mapping) and f["geometry"].get("type") == "Point"
            and (f.get("properties") or {}).get("iso_time")]
    if not rows:
        _refuse("STORM_UNSTATED", "the storm record holds no fix: Storm reads the points fetch_storm_tracks returns "
                "(geometry 'points'), one per best-track fix.")
    storms = sorted({(row.get("sid"), row.get("name")) for row in rows}, key=str)
    if len(storms) != 1:
        _refuse("STORM_SEVERAL", f"the storm record holds {len(storms)} storms ({', '.join(f'{n} {s}' for s, n in storms)}): "
                "a run has one storm, so state the name and season of one.")
    ordered = sorted(rows, key=lambda row: _utc(row["iso_time"]))
    for before, after in zip(ordered, ordered[1:]):
        if _utc(before["iso_time"]) == _utc(after["iso_time"]):
            _refuse("STORM_FIX_DUPLICATE", f"the storm record holds two fixes at {after['iso_time']}: the engine "
                    "divides by the time between fixes.")
    return ordered


def landfall_instant(doc: Any) -> str:
    """The instant of the storm's last landfall fix (USA_RECORD 'L') as a UTC ISO-8601 timestamp; a track with none refuses."""
    landings = [row for row in _fixes(doc) if row.get("record") == "L"]
    if not landings:
        _refuse("STORM_NO_LANDFALL", "the storm record holds no landfall fix (USA_RECORD 'L'), so no instant of the "
                "storm's own to count the run from: state event_time.")
    return landings[-1]["iso_time"].strip().replace(" ", "T") + "+00:00"


def storm_track(doc: Any, *, offset: str | None, t0: float, tfinal: float) -> dict[str, Any]:
    """A points document of one storm -> ``{time_offset, fixes}`` in SI units, seconds from the reference ``offset``.

    ``offset`` is the run's reference instant (the stated event_time; unstated, the storm's last landfall fix).
    The fixes kept run from the last at or before ``t0`` to the first at or after ``tfinal`` (the engine stops when
    the track begins after the run and would carry the last fix on unchanged past the track's end); the track
    also names the kept fixes the writer omits (no radius of maximum wind) and those it gives the default outer radius."""
    import clawpack.geoclaw.units as units

    rows = _fixes(doc)
    reference = _utc(offset if offset else landfall_instant(doc))
    seconds = [(_utc(row["iso_time"]) - reference).total_seconds() for row in rows]
    # A fix with no wind or pressure is the interpolated track between reports, not an observation of the storm's strength.
    strength = [i for i, row in enumerate(rows) if row.get("wind_kt") is not None and row.get("pres_mb") is not None]
    # Clawpack's writer omits a fix with no radius of maximum wind, so only a fix with one can span the run.
    usable = [i for i in strength if rows[i].get("rmw_nmi") is not None]
    if not usable or seconds[usable[0]] > t0:
        began = f"at {rows[usable[0]]['iso_time']}" if usable else "never"
        _refuse("STORM_STARTS_LATE", f"the storm's strength and radius of maximum wind are first reported {began}, after the run opens at "
                f"{t0:g} s from {reference.isoformat()}: the engine stops when the track begins after the run.")
    if seconds[usable[-1]] < tfinal:
        _refuse("STORM_ENDS_EARLY", f"the storm's strength and radius of maximum wind are last reported at {rows[usable[-1]]['iso_time']}, "
                f"before the run ends at {tfinal:g} s from {reference.isoformat()}: the engine would carry the last "
                "fix on unchanged, so shorten CLAW_TFINAL.")
    first = max(i for i in usable if seconds[i] <= t0)
    last = min(i for i in usable if seconds[i] >= tfinal)
    kept = [i for i in strength if first <= i <= last]
    if sum(rows[i].get("rmw_nmi") is not None for i in kept) < 2:
        _refuse("STORM_TOO_FEW_FIXES", f"only {sum(rows[i].get('rmw_nmi') is not None for i in kept)} fix spans the run once the fixes with no "
                "radius of maximum wind (IBTrACS USA_RMW) are omitted: the engine takes the storm's velocity from two.")
    # A value the fix lacks stays NaN: the writer omits a fix with no radius of maximum wind and fills a missing outer radius with 500 km.
    nan = float("nan")
    track = [{"t": seconds[i], "longitude": float(rows[i]["longitude"]), "latitude": float(rows[i]["latitude"]),
              **{attribute: float(units.convert(float(rows[i][name]), have, want)) if rows[i].get(name) is not None else nan
                 for name, (attribute, have, want) in _FIELDS.items()}} for i in kept]
    return {"time_offset": reference.isoformat(timespec="seconds"), "fixes": track,
            "omitted": [rows[i]["iso_time"] for i in kept if rows[i].get("rmw_nmi") is None],
            "default_radius": [rows[i]["iso_time"] for i in kept if rows[i].get("rmw_nmi") is not None and rows[i].get("roci_nmi") is None]}


def write_stormfiles(sheet: Any, work: Path) -> list[str]:
    """Write every storm file the sheet's composite named into ``work`` -> their names."""
    written: list[str] = []
    for name, content in sheet.files.items():
        if isinstance(content, Mapping) and STORM in content:
            _written(content[STORM], work / name)
            written.append(name)
    return written


def _written(spec: Mapping[str, Any], path: Path) -> None:
    import numpy as np
    from clawpack.geoclaw.surge.storm import Storm

    fixes: Sequence[Mapping[str, Any]] = spec["fixes"]
    storm = Storm()
    # The engine counts the file's seconds from the run's t = 0 whatever the header says, and Clawpack's reader cannot
    # read a dated header back, so the times are seconds from a numeric offset of 0.
    storm.time_offset = 0.0
    storm.t = np.array([fix["t"] for fix in fixes])
    storm.eye_location = np.array([[fix["longitude"], fix["latitude"]] for fix in fixes])
    for attribute in (a for a, _, _ in _FIELDS.values()):
        setattr(storm, attribute, np.array([fix[attribute] for fix in fixes]))
    storm.write(path, file_format="geoclaw")
    written = int(path.read_text().split()[0])
    expected = sum(not np.isnan(fix["max_wind_radius"]) for fix in fixes)
    if written != expected:
        raise GeoclawError(f"the storm file {path.name} holds {written} fixes where {expected} carry a radius of maximum wind.",
                           error_code="GEOCLAW_STORM_WRITE_MISMATCH")
