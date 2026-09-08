"""The basin's free surface, read from the gauge that watches it.

A lake has a level, and the run has to open at it. Left at the dictionary's own
zero the free surface sits ON the chart datum, so every node the survey sounded
at datum carries no water at all and the rim of the domain is dry ground the
solver still has to integrate over.

The level is an OBSERVATION, not a knob: the nearest CO-OPS gauge inside the AOI,
at the day the run is about. Data is assumed true and nothing here thresholds it.

WHAT MAKES THE ARITHMETIC LEGAL is that both documents state their zero. The
gauge reading and the bed elevation are counted from the same lake datum, which
each source row says in its own words, so the offset between them is zero and the
reading IS the elevation. Two rows stating different zeros refuse by name rather
than adding numbers that are not on the same axis.

Only this question reads a lake level, so it lives beside the recipe that
declares it; a second question asking for one is what earns it a shared home.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(
    "trid3nt_server.workflows.telemac.templates.stratified_flow.lake_level")

__all__ = ["observed_lake_level", "reading_day"]


async def reading_day(*, event_time: str | None = None) -> dict[str, Any]:
    """The DAY the gauge is read over, as the window the fetch asks for.

    Unset is TODAY, resolved here rather than defaulted onto the param: a value
    seated on the invocation would report on the card as something the caller
    supplied. A value that is not a date REFUSES - silently reading a different
    day than the one asked for is the swallow class.
    """
    import datetime as dt

    from trid3nt_server.workflows.telemac.helpers.errors import OpenWaterError

    stated = str(event_time or "").strip()
    if not stated:
        return {"date": dt.datetime.now(dt.timezone.utc).date().isoformat(),
                "basis": "today"}
    try:
        day = dt.date.fromisoformat(stated[:10])
    except ValueError:
        raise OpenWaterError(
            f"event_time={event_time!r} is not an ISO date (e.g. '2026-09-05'), "
            "so the day the lake level is read at is unknown. Omit it to read "
            "today.",
            error_code="TELEMAC3D_EVENT_TIME_INVALID") from None
    return {"date": day.isoformat(), "basis": "stated"}


async def observed_lake_level(*, level: Any, gauge_source: str,
                              bed_source: str, aoi: dict[str, Any]
                              ) -> dict[str, Any]:
    """The observed level -> the elevation the run's free surface opens at.

    Returns the reading, the gauge it came from and the sentence the journal says
    the datum arithmetic in. An AOI no gauge watches REFUSES: the answer to "no
    instrument reports this water" is a wider domain or a stated level, never a
    number this step made up.
    """
    return await asyncio.to_thread(_observed, level, gauge_source, bed_source,
                                   aoi)


def _observed(level: Any, gauge_source: str, bed_source: str,
              aoi: dict[str, Any]) -> dict[str, Any]:
    import geopandas as gpd

    from trid3nt_server.workflows.runtime import journal_note
    from trid3nt_server.workflows.telemac.helpers.errors import OpenWaterError

    datum = _one_datum(gauge_source, bed_source)
    frame = _stations(level)
    if frame.empty:
        raise OpenWaterError(
            f"{gauge_source} reports no water-level gauge over this AOI, so the "
            "level the basin opens at is not observed anywhere. Widen the bbox "
            "to reach a gauge on this lake, or state the level on the call "
            "(keywords={'INITIAL ELEVATION': <metres on the bed's datum>}).",
            error_code="TELEMAC3D_LAKE_IS_UNGAUGED")

    centre = gpd.points_from_xy([float(aoi["lon"])], [float(aoi["lat"])],
                                crs=4326)[0]
    metric = frame.estimate_utm_crs()
    distances = frame.to_crs(metric).distance(
        gpd.GeoSeries([centre], crs=4326).to_crs(metric).iloc[0])
    row = frame.loc[distances.idxmin()]
    stamp, elevation = _last_reading(row, gauge_source)

    note = (
        f"lake level {elevation:.3f} m at {stamp} from {row['station_name']} "
        f"({row['station_id']}), {float(distances.min()) / 1000.0:.1f} km from "
        f"the AOI centre. The gauge reads on {datum} and {bed_source} states the "
        f"same datum, so the offset between them is 0.000 m and the free surface "
        f"opens at the reading.")
    journal_note(note)
    logger.info("stratified basin: %s", note)
    return {
        "elevation_m": round(float(elevation), 3),
        "station_id": str(row["station_id"]),
        "station_name": str(row["station_name"]),
        "reading_time": stamp,
        "distance_km": round(float(distances.min()) / 1000.0, 2),
        "datum": datum,
        "note": note,
    }


def _one_datum(gauge_source: str, bed_source: str) -> str:
    """The zero BOTH rows state, or the refusal that names the two they state.

    Neither document's numbers can be read against the other's until they are
    counted from the same place, and what they are counted from is stated on the
    source row rather than readable out of the values.
    """
    from trid3nt_server.tools.fetchers._router.registration import get_spec
    from trid3nt_server.workflows.telemac.helpers.errors import OpenWaterError

    stated = {name: (get_spec(name).vertical_datum or "").strip()
              for name in (gauge_source, bed_source)}
    missing = sorted(name for name, datum in stated.items() if not datum)
    if missing:
        raise OpenWaterError(
            f"{', '.join(missing)} states no vertical datum, so a water level "
            "and a bed elevation cannot be placed on one axis. State the datum "
            "on the source row from the dataset's own documentation.",
            error_code="TELEMAC3D_DATUM_UNSTATED")
    if stated[gauge_source] != stated[bed_source]:
        raise OpenWaterError(
            f"{gauge_source} reads on {stated[gauge_source]!r} and {bed_source} "
            f"is on {stated[bed_source]!r}, and no offset between the two is "
            "stated anywhere, so the free surface cannot be placed over this "
            "bed. Name a gauge and a bed on one datum, or state the offset on "
            "the rows.",
            error_code="TELEMAC3D_DATUMS_DIFFER")
    return stated[gauge_source]


def _stations(level: Any) -> Any:
    """The fetched gauge layer, as the station rows it carries."""
    import tempfile
    from pathlib import Path

    import geopandas as gpd

    from trid3nt_server.tools.cache import read_object_bytes_s3
    from trid3nt_server.workflows.shared.geometry import source_uri
    from trid3nt_server.workflows.telemac.helpers.errors import OpenWaterError

    uri = str(source_uri(level) or "").strip()
    if not uri:
        raise OpenWaterError(
            "the lake-level fetch returned no layer to read the gauge from.",
            error_code="TELEMAC3D_LAKE_LEVEL_UNREADABLE")
    if not uri.startswith("s3://"):
        return _in_4326(gpd.read_file(uri))
    local = Path(tempfile.mkdtemp(prefix="lake-level-")) / "gauges.fgb"
    try:
        local.write_bytes(read_object_bytes_s3(uri))
        return _in_4326(gpd.read_file(local))
    finally:
        local.unlink(missing_ok=True)


def _in_4326(frame: Any) -> Any:
    """The station rows in the lon/lat the AOI centre is spoken in."""
    return frame.to_crs(4326) if frame.crs is not None else frame.set_crs(4326)


def _last_reading(row: Any, gauge_source: str) -> tuple[str, float]:
    """The LAST sample the gauge published in the window -> ``(stamp, metres)``.

    The window is one day, so its last sample is the level as that day closed -
    and for a run about today, the most recent reading there is.
    """
    from trid3nt_server.workflows.telemac.helpers.errors import OpenWaterError

    for line in reversed(str(row.get("time_series_csv") or "").splitlines()):
        stamp, _sep, value = line.strip().partition(",")
        try:
            return stamp, float(value)
        except ValueError:
            continue
    raise OpenWaterError(
        f"{gauge_source} returned station {row.get('station_id')} with no "
        "readable water level in the window asked for. Ask for a day the gauge "
        "published, or state the level on the call.",
        error_code="TELEMAC3D_LAKE_LEVEL_EMPTY")
