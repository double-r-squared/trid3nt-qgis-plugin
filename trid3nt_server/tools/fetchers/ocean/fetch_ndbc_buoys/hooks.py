"""ndbc_buoys hooks: one stdmet table, read out of the file the WINDOW names.

NDBC publishes a buoy's standard meteorological record twice - the last 45 days
in realtime2, each COMPLETED year in the historical archive - as one whitespace
table whose own header names its columns, so the window alone picks the file and
one parse reads both. The columns are keyed by that header because its shape
changed with the network: the oldest years state a two-digit year, an hour and
no minute, and carry no units line under the names.
"""

from __future__ import annotations

import datetime as dt
import gzip
import logging
import xml.etree.ElementTree as ET
from typing import Any

from trid3nt_contracts.coverage import CoveragePoint
from trid3nt_contracts.source_spec import SourceSpec

from ..._router.errors import (
    RouterError, router_empty_error, router_input_error, router_upstream_error)
from ..._router.hooks import RequestPlan, register_hook
from ..._router.transport import get_bytes, get_client

logger = logging.getLogger(__name__)

__all__ = ["stations", "resolve_build", "resolve_parse", "build_request",
           "parse_response", "classify_status"]

#: The listing every station fact is read off, and what a station has to be in
#: it to answer for a sea state: a MOORED BUOY carrying a met payload. A fixed
#: platform states the same payload and measures no waves, so neither the type
#: nor the payload alone lists the stations this fetch can answer for.
_LISTING = "https://www.ndbc.noaa.gov/activestations.xml"
_BUOY = "buoy"
_HAS_MET = "y"

#: How far back the realtime file reaches, in days, and the window rule it makes:
#: a window opening inside it is answered by it, a window ending in a completed
#: year by the archive, and the months between the two are in neither file.
_REALTIME_DAYS = 45

#: WHAT THIS FETCH PUBLISHES, by NDBC's own column name: the height, the period
#: and the direction of the sea state. The other stdmet columns (the wind, the
#: pressure, the air and water temperature) are a meteorological record this
#: source states no coverage row for and does not carry.
_PUBLISHED = (
    ("WVHT", "wave_height_m", "wave_height_series_csv"),
    ("DPD", "peak_period_s", "peak_period_series_csv"),
    ("MWD", "wave_direction_deg_true", "wave_direction_series_csv"),
)

#: The values NDBC writes where the instrument reported nothing. A 99, a 999 or
#: a 9999 in any of its spellings is a gap and never a reading - no buoy reports
#: a 99 m sea or a 999 deg heading - and "MM" is the gap in the realtime file.
_MISSING = (99.0, 999.0, 9999.0)
_MISSING_WORD = "MM"


def _listed(body: bytes) -> dict[str, dict[str, str]]:
    """Every wave-reporting buoy in the listing, by the id NDBC calls it.

    A row is read only where it states both a place and the payload; the id
    keeps NDBC's own spelling, which is the one its file names are written in."""
    found: dict[str, dict[str, str]] = {}
    for station in ET.fromstring(body).findall("station"):
        attrs = station.attrib
        if attrs.get("type") != _BUOY or attrs.get("met") != _HAS_MET:
            continue
        try:
            float(attrs["lon"]), float(attrs["lat"])
        except (KeyError, TypeError, ValueError):
            continue
        found[str(attrs["id"])] = dict(attrs)
    return found


@register_hook("ndbc_buoys.stations")
def stations() -> list[CoveragePoint]:
    """Every moored buoy the listing reports a met payload for, where it floats.

    A buoy states no vertical datum: a wave height is a height OF the water and
    not an elevation counted from a zero."""
    body, _ct, _url = get_bytes(get_client(), _LISTING)
    return [CoveragePoint(id=station_id, lon=float(attrs["lon"]),
                          lat=float(attrs["lat"]))
            for station_id, attrs in sorted(_listed(body).items())]


@register_hook("ndbc_buoys.resolve_build")
def resolve_build(spec: SourceSpec, params: dict[str, Any]) -> list[RequestPlan]:
    """GET the listing the asked buoy's place and name are read out of."""
    if not str(params.get("station") or "").strip():
        raise router_input_error(spec.error_code_prefix,
                                 "station is required and empty",
                                 spec.input_error_suffix)
    return [RequestPlan(url=_LISTING, headers={"User-Agent": spec.auth.user_agent})]


@register_hook("ndbc_buoys.resolve_parse")
def resolve_parse(spec: SourceSpec, params: dict[str, Any],
                  bodies: list[bytes]) -> dict[str, Any]:
    """Merge the buoy's own id spelling, name, owner and position into params."""
    sc = spec.error_code_prefix
    asked = str(params.get("station") or "").strip()
    try:
        listed = _listed(bodies[0])
    except ET.ParseError as exc:
        raise router_upstream_error(sc, f"NDBC station listing is not XML: {exc}")
    match = {key.upper(): key for key in listed}.get(asked.upper())
    if match is None:
        raise router_input_error(
            sc,
            f"station={asked!r} is not a wave-reporting NDBC buoy: the listing "
            f"names {len(listed)} moored buoys with a met payload, and a fixed "
            "platform or a buoy reporting no met record measures no sea state",
            spec.input_error_suffix,
        )
    attrs = listed[match]
    return {"_station_id": match,
            "_station_name": str(attrs.get("name") or match),
            "_owner": str(attrs.get("owner") or ""),
            "_lon": float(attrs["lon"]), "_lat": float(attrs["lat"])}


def _day(spec: SourceSpec, stamp: Any) -> dt.date:
    try:
        return dt.date.fromisoformat(str(stamp))
    except ValueError:
        raise router_input_error(spec.error_code_prefix,
                                 f"{stamp!r} is not ISO YYYY-MM-DD",
                                 spec.input_error_suffix)


@register_hook("ndbc_buoys.build_request")
def build_request(spec: SourceSpec, params: dict[str, Any]) -> list[RequestPlan]:
    """The file the window names: realtime2 where the window opens inside the
    last 45 days, one yearly archive per completed year it spans otherwise."""
    sc = spec.error_code_prefix
    station = str(params["_station_id"])
    start, end = _day(spec, params["start_date"]), _day(spec, params["end_date"])
    today = dt.date.today()
    headers = {"User-Agent": spec.auth.user_agent}
    if start > today - dt.timedelta(days=_REALTIME_DAYS):
        return [RequestPlan(
            url=str(spec.endpoints["realtime"].url_template).format(station=station),
            headers=headers)]
    if end.year < today.year:
        return [RequestPlan(
            url=str(spec.endpoints["archive"].url_template).format(
                station=station, year=year),
            headers=headers)
            for year in range(start.year, end.year + 1)]
    raise router_input_error(
        sc,
        f"NDBC holds no stdmet file over {start} to {end}: the realtime file "
        f"carries the last {_REALTIME_DAYS} days and the archive the completed "
        "years, so a window earlier this year than the realtime file reaches is "
        "published in neither; ask inside the last 45 days or inside a "
        "completed year",
        spec.input_error_suffix,
    )


def _table(spec: SourceSpec, body: bytes) -> tuple[list[str], list[list[str]]]:
    """One stdmet file as its own column names and its data rows.

    The archive is gzipped and the realtime file is not, and the header is the
    first line naming the year however the file's era spells it."""
    try:
        raw = gzip.decompress(body) if body[:2] == b"\x1f\x8b" else body
        text = raw.decode("utf-8", errors="replace")
    except (OSError, EOFError) as exc:
        raise router_upstream_error(spec.error_code_prefix,
                                    f"NDBC stdmet file did not decompress: {exc}")
    header: list[str] = []
    rows: list[list[str]] = []
    for line in text.splitlines():
        fields = line.split()
        if not fields:
            continue
        if not header:
            if fields[0].lstrip("#") in ("YY", "YYYY"):
                header = [name.lstrip("#") for name in fields]
            continue
        if not line.startswith("#"):
            rows.append(fields)
    return header, rows


def _stamp(header: list[str], fields: list[str]) -> str | None:
    """The instant one row reports at, UTC, or ``None`` where it spells none."""
    try:
        year, month, day, hour = (int(fields[i]) for i in range(4))
        minute = int(fields[4]) if header[4] == "mm" else 0
    except (IndexError, ValueError):
        return None
    if year < 100:
        year += 1900
    return f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}Z"


def _reading(token: str) -> float | None:
    """One number, or ``None`` where NDBC wrote a gap in its place."""
    if token == _MISSING_WORD:
        return None
    try:
        value = float(token)
    except ValueError:
        return None
    return None if value in _MISSING else value


@register_hook("ndbc_buoys.parse_response")
def parse_response(spec: SourceSpec, params: dict[str, Any],
                   bodies: list[bytes]) -> list[dict[str, Any]]:
    """One Point feature at the buoy, the window's sea state inline per column."""
    sc = spec.error_code_prefix
    opens = _day(spec, params["start_date"]).isoformat()
    closes = _day(spec, params["end_date"]).isoformat()
    series: dict[str, list[tuple[str, float]]] = {
        csv: [] for _column, _value, csv in _PUBLISHED}
    for body in bodies:
        header, rows = _table(spec, body)
        at = {name: i for i, name in enumerate(header)}
        for fields in rows:
            stamp = _stamp(header, fields)
            if stamp is None or not opens <= stamp[:10] <= closes:
                continue
            for column, _value, csv in _PUBLISHED:
                index = at.get(column)
                reading = (_reading(fields[index])
                           if index is not None and index < len(fields) else None)
                if reading is not None:
                    series[csv].append((stamp, reading))
    for rows_of in series.values():
        rows_of.sort(key=lambda row: row[0])
    height = series[_PUBLISHED[0][2]]
    if not height:
        raise router_empty_error(
            sc,
            f"buoy {params['_station_id']} reports no wave height between "
            f"{opens} and {closes}: its stdmet wave columns over this window "
            "carry NDBC's own missing value, so nothing here measured a sea "
            "state",
            spec.empty_error_suffix,
        )
    properties: dict[str, Any] = {
        "station_id": params["_station_id"],
        "station_name": params.get("_station_name", ""),
        "owner": params.get("_owner", ""),
        "n_timesteps": len(height),
        "time_start": height[0][0],
        "time_end": height[-1][0],
    }
    for column, value, csv in _PUBLISHED:
        rows_of = series[csv]
        properties[value] = round(rows_of[-1][1], 3) if rows_of else None
        properties[csv] = "\n".join(
            [f"iso,{value}"] + [f"{stamp},{number}" for stamp, number in rows_of])
    return [{"type": "Feature",
             "geometry": {"type": "Point",
                          "coordinates": [params["_lon"], params["_lat"]]},
             "properties": properties}]


@register_hook("ndbc_buoys.classify_status")
def classify_status(spec: SourceSpec, status: int | None,
                    body: str | None) -> RouterError | None:
    """A 404 is NDBC holding no file at that address - a buoy that reported
    nothing that year, or one whose realtime file has gone - which is an empty
    record and not an upstream failure. Every other status keeps the default."""
    if status != 404:
        return None
    return router_empty_error(
        spec.error_code_prefix,
        "NDBC publishes no stdmet file for this buoy over this window: the "
        "realtime file stands only while a buoy is reporting and the archive "
        "only for a year it reported in",
        spec.empty_error_suffix,
    )
