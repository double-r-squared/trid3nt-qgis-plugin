"""``fetch_usgs_nwis_gauges`` atomic tool — real USGS NWIS / Water Services stream gauges.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import math
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.cache import read_through

__all__ = [
    "fetch_usgs_nwis_gauges",
    "estimate_payload_mb",
    "NwisGaugesError",
    "NwisInputError",
    "NwisBboxTooLargeError",
    "NwisUpstreamError",
    "NwisNoStationsError",
    "_validate_bbox",
    "_validate_state_code",
    "_round_bbox_to_6dp",
    "_resolve_window",
    "_build_iv_url",
    "_build_site_url",
    "_parse_iv_json",
    "_parse_iv_json_window",
    "_parse_site_rdb",
    "_build_flatgeobuf",
    "_build_window_flatgeobuf",
    "_fetch_usgs_nwis_gauges_bytes",
    "_fetch_usgs_nwis_hydrograph_bytes",
    "IV_URL",
    "SITE_URL",
]

logger = logging.getLogger("trid3nt_server.tools.fetchers.hydrology.fetch_usgs_nwis_gauges")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class NwisGaugesError(RuntimeError):
    """Base class for fetch_usgs_nwis_gauges failures.

    ``error_code`` maps to the WebSocket A.6 error frame emitted by the agent
    surface. ``retryable`` guides FR-AS-11 retry/clarify/fallback logic.
    """

    error_code: str = "NWIS_GAUGES_ERROR"
    retryable: bool = True


class NwisInputError(NwisGaugesError):
    """Invalid inputs — bad bbox shape, bad state code, no spatial selector.

    Not retryable: the caller must fix the argument.
    """

    error_code = "NWIS_GAUGES_INPUT_ERROR"
    retryable = False


class NwisBboxTooLargeError(NwisInputError):
    """The requested bbox exceeds the USGS bBox area limit (~25 deg^2).

    Not retryable as-is. The caller should re-issue with ``state_code`` set
    (no area limit) for a state-level ask, or pass a smaller bbox.
    """

    error_code = "NWIS_GAUGES_BBOX_TOO_LARGE"
    retryable = False


class NwisUpstreamError(NwisGaugesError):
    """USGS Water Services request failed (network error, HTTP 5xx, bad body).

    Retryable — transient USGS outages recover on retry.
    """

    error_code = "NWIS_GAUGES_UPSTREAM_ERROR"
    retryable = True


class NwisNoStationsError(NwisGaugesError):
    """No USGS gauge stations found — BOTH the IV service and the Site-service
    fallback returned zero stations in scope.

    Not retryable — the area genuinely has no active NWIS gauges reporting
    discharge/stage. Either widen the scope or pick an area with known gauges.
    """

    error_code = "NWIS_GAUGES_NO_STATIONS"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: USGS Water Services Instantaneous Values (real-time observed) endpoint.
IV_URL = "https://waterservices.usgs.gov/nwis/iv/"

#: USGS Water Services Site service (station locations only) endpoint.
SITE_URL = "https://waterservices.usgs.gov/nwis/site/"

#: NWIS parameter codes: 00060 = discharge (ft^3/s); 00065 = gage height (ft).
_PARAM_DISCHARGE = "00060"
_PARAM_GAGE_HEIGHT = "00065"
_PARAMETER_CD = f"{_PARAM_DISCHARGE},{_PARAM_GAGE_HEIGHT}"

#: User-Agent per USGS usage guidance (a descriptive UA is recommended).
_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)

#: HTTP timeout (seconds).
_HTTP_TIMEOUT = 60.0

#: USGS bBox area limit. The product of the lon-range and lat-range must be
#: <= ~25 deg^2 or the service 400s. We clamp the gate just under that.
_MAX_BBOX_SQ_DEG = 24.5

#: Maximum hydrograph window (days). The IV service serves up to ~120 days of
#: instantaneous values; we cap conservatively so a window request stays a
#: bounded payload. A wider span should be chunked by the caller.
_MAX_WINDOW_DAYS = 120

#: 2-letter USPS state / territory codes accepted by NWIS ``stateCd``.
_VALID_STATE_CODES: frozenset[str] = frozenset(
    {
        "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
        "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
        "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
        "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
        "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
        "DC", "PR", "VI", "GU", "AS", "MP",
    }
)


# ---------------------------------------------------------------------------
# AtomicToolMetadata — registered once at import time.
# ---------------------------------------------------------------------------


def _build_metadata() -> AtomicToolMetadata:
    """Construct AtomicToolMetadata defensively against schema flag variants."""
    common: dict[str, Any] = dict(
        name="fetch_usgs_nwis_gauges",
        ttl_class="dynamic-1h",
        source_class="usgs_nwis_gauges",
        cacheable=True,
    )
    try:
        return AtomicToolMetadata(**common, supports_global_query=False)  # type: ignore[call-arg]
    except Exception:
        logger.debug(
            "AtomicToolMetadata does not support supports_global_query; "
            "registering fetch_usgs_nwis_gauges without it"
        )
        return AtomicToolMetadata(**common)


_METADATA = _build_metadata()


# ---------------------------------------------------------------------------
# Payload-MB estimator (Wave 1.5 chat-warning system).
# ---------------------------------------------------------------------------


def estimate_payload_mb(
    bbox: tuple[float, float, float, float] | None = None,
    state_code: str | None = None,
    **_kw: Any,
) -> float:
    """Estimate output FlatGeobuf size in MB.

    Each gauge station is one Point feature with a handful of small scalar
    properties (~150 bytes serialized). Station density:

    - 1° × 1° bbox (~a metro / small basin) → ~5-30 gauges → ~5 KB
    - whole-state ``state_code`` query → up to ~hundreds of gauges → ~50 KB

    The estimate is conservative; gauge layers are always tiny.
    """
    n_stations = 50  # default guess (state-level)
    if state_code is None and bbox is not None:
        try:
            west, south, east, north = bbox
            sq_deg = max(0.0, east - west) * max(0.0, north - south)
            # ~10 gauges per 1° square in the populated CONUS.
            n_stations = max(1, int(sq_deg * 10))
        except (TypeError, ValueError):
            pass
    return max(0.001, n_stations * 150 / 1_000_000.0)


# ---------------------------------------------------------------------------
# Input validation helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Raise ``NwisInputError`` if the bbox is malformed or out of range.

    Does NOT check the area limit — that is the caller's responsibility via the
    ``state_code``-vs-bbox decision in ``fetch_usgs_nwis_gauges`` so the typed
    ``NwisBboxTooLargeError`` carries the right remediation hint.
    """
    if not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
        raise NwisInputError(
            f"bbox must be (west, south, east, north); got {bbox!r}"
        )
    west, south, east, north = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise NwisInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= west <= 180.0 and -180.0 <= east <= 180.0):
        raise NwisInputError(f"bbox lon values out of [-180, 180]: {bbox!r}")
    if not (-90.0 <= south <= 90.0 and -90.0 <= north <= 90.0):
        raise NwisInputError(f"bbox lat values out of [-90, 90]: {bbox!r}")
    if west >= east or south >= north:
        raise NwisInputError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )


def _validate_state_code(state_code: str) -> str:
    """Normalize + validate a 2-letter USPS state/territory code."""
    if not isinstance(state_code, str):
        raise NwisInputError(
            f"state_code must be a 2-letter string; got {type(state_code).__name__}"
        )
    sc = state_code.strip().upper()
    if sc not in _VALID_STATE_CODES:
        raise NwisInputError(
            f"state_code={state_code!r} is not a recognized 2-letter USPS code; "
            f"expected one of e.g. 'WA', 'FL', 'CA' (USGS NWIS stateCd)"
        )
    return sc


def _bbox_area_sq_deg(bbox: tuple[float, float, float, float]) -> float:
    west, south, east, north = bbox
    return max(0.0, east - west) * max(0.0, north - south)


def _round_bbox_to_6dp(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    return tuple(round(v, 6) for v in bbox)  # type: ignore[return-value]


def _resolve_window(
    start_date: str | None,
    end_date: str | None,
    period: str | None,
) -> tuple[str, str] | str | None:
    """Resolve the hydrograph time-window selector for the IV service.

    Returns one of:
      - ``None`` — no window requested (the default latest-instantaneous mode).
      - ``str`` — a validated ISO-8601 ``period`` (e.g. ``"P7D"``); passed
        verbatim to the IV ``period`` parameter.
      - ``(start, end)`` — validated ISO ``YYYY-MM-DD`` dates for ``startDT`` /
        ``endDT``.

    ``period`` (when given) WINS over explicit dates — it is the simpler,
    relative form the LLM is most likely to emit. Raises ``NwisInputError`` on
    a malformed selector (bad date, reversed range, over the day cap).
    """
    if period is not None and str(period).strip() != "":
        p = str(period).strip().upper()
        # USGS accepts ISO-8601 durations like "P7D", "P1M", "PT6H".
        if not re.fullmatch(r"P(?:\d+[YMWD])*(?:T(?:\d+[HMS])+)?", p) or p == "P":
            raise NwisInputError(
                f"period={period!r} is not a valid ISO-8601 duration (e.g. "
                f"'P7D' = last 7 days, 'P1M' = last month, 'PT6H' = last 6 hours)"
            )
        return p

    if start_date is None and end_date is None:
        return None

    if start_date is None or end_date is None:
        raise NwisInputError(
            "a hydrograph window requires BOTH start_date and end_date "
            "(ISO YYYY-MM-DD), or a single relative period (e.g. period='P7D'); "
            f"got start_date={start_date!r}, end_date={end_date!r}"
        )

    try:
        d0 = _dt.date.fromisoformat(str(start_date))
    except ValueError as exc:
        raise NwisInputError(
            f"start_date={start_date!r} is not a valid ISO date (YYYY-MM-DD): {exc}"
        ) from exc
    try:
        d1 = _dt.date.fromisoformat(str(end_date))
    except ValueError as exc:
        raise NwisInputError(
            f"end_date={end_date!r} is not a valid ISO date (YYYY-MM-DD): {exc}"
        ) from exc
    if d0 > d1:
        raise NwisInputError(
            f"start_date must be <= end_date; got start={d0}, end={d1}"
        )
    n_days = (d1 - d0).days + 1
    if n_days > _MAX_WINDOW_DAYS:
        raise NwisInputError(
            f"hydrograph window {n_days} days exceeds the {_MAX_WINDOW_DAYS}-day "
            f"cap; request a shorter window or call in chunks"
        )
    return (d0.isoformat(), d1.isoformat())


# ---------------------------------------------------------------------------
# HTTP helper.
# ---------------------------------------------------------------------------


def _http_get(url: str, timeout: float = _HTTP_TIMEOUT) -> bytes:
    """Plain HTTP GET. Raises ``NwisUpstreamError`` on failure.

    Note: a USGS 404 on the IV/Site services means "no sites matched the
    query" — it is NOT a hard upstream failure. We surface it as an empty
    body so the caller's fallback chain engages instead of aborting.
    """
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            # USGS uses 404 for "no sites found" on these services.
            logger.info("fetch_usgs_nwis_gauges: USGS returned 404 (no sites) for %s", url)
            return b""
        raise NwisUpstreamError(
            f"USGS Water Services returned HTTP {exc.code} for {url}: {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise NwisUpstreamError(
            f"Network error fetching {url}: {exc.reason}"
        ) from exc
    except TimeoutError as exc:
        raise NwisUpstreamError(
            f"Timed out after {timeout}s fetching {url}"
        ) from exc


# ---------------------------------------------------------------------------
# URL builders.
# ---------------------------------------------------------------------------


def _build_iv_url(
    *,
    state_code: str | None,
    bbox: tuple[float, float, float, float] | None,
    window: tuple[str, str] | str | None = None,
) -> str:
    """Build the Instantaneous Values URL for a state or bbox selector.

    ``window`` controls the temporal selector:
      - ``None`` (default) — no temporal parameter; the IV service returns the
        latest instantaneous value (the original behaviour).
      - ``str`` — an ISO-8601 ``period`` (e.g. ``"P7D"``) → ``&period=P7D``.
      - ``(start, end)`` — ISO dates → ``&startDT=...&endDT=...``.

    With a window the IV body carries the FULL per-site time series (every
    sample in the window) instead of just the latest reading.
    """
    params: list[tuple[str, str]] = [
        ("format", "json"),
        ("siteStatus", "active"),
        ("parameterCd", _PARAMETER_CD),
    ]
    if state_code is not None:
        params.append(("stateCd", state_code))
    elif bbox is not None:
        west, south, east, north = bbox
        params.append(("bBox", f"{west},{south},{east},{north}"))
    if isinstance(window, str):
        params.append(("period", window))
    elif isinstance(window, tuple):
        start, end = window
        params.append(("startDT", start))
        params.append(("endDT", end))
    return IV_URL + "?" + urllib.parse.urlencode(params)


def _build_site_url(
    *,
    state_code: str | None,
    bbox: tuple[float, float, float, float] | None,
) -> str:
    """Build the Site-service URL (station locations only) — RDB format."""
    params: list[tuple[str, str]] = [
        ("format", "rdb"),
        ("siteStatus", "active"),
        ("hasDataTypeCd", "iv"),
        ("parameterCd", _PARAMETER_CD),
    ]
    if state_code is not None:
        params.append(("stateCd", state_code))
    elif bbox is not None:
        west, south, east, north = bbox
        params.append(("bBox", f"{west},{south},{east},{north}"))
    return SITE_URL + "?" + urllib.parse.urlencode(params)


# ---------------------------------------------------------------------------
# Parsers — IV WaterML-JSON and Site-service RDB.
# ---------------------------------------------------------------------------


def _parse_iv_json(raw: bytes) -> list[dict[str, Any]]:
    """Parse the IV WaterML-JSON body → one station record per ``site_no``.

    Each ``value.timeSeries[]`` is one (site × parameter) series. We group by
    site_no, merging the discharge (00060) and gage-height (00065) series for
    the same station into a single record:

        {site_no, site_name, lon, lat, discharge_cfs?, gage_height_ft?, reading_dt?}

    Series with no latest value are skipped for that parameter. Records with no
    parseable coordinate are dropped entirely. Returns ``[]`` when the body is
    empty or carries zero series.
    """
    if not raw:
        return []
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise NwisUpstreamError(f"USGS IV response is not valid JSON: {exc}") from exc

    series = (obj.get("value") or {}).get("timeSeries") or []
    by_site: dict[str, dict[str, Any]] = {}

    for ts in series:
        source = ts.get("sourceInfo") or {}
        site_codes = source.get("siteCode") or []
        if not site_codes:
            continue
        site_no = str(site_codes[0].get("value") or "").strip()
        if not site_no:
            continue
        site_name = str(source.get("siteName") or "").strip()

        geo = (source.get("geoLocation") or {}).get("geogLocation") or {}
        try:
            lat = float(geo.get("latitude"))
            lon = float(geo.get("longitude"))
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(lat) and math.isfinite(lon)):
            continue

        # Variable code (00060 / 00065).
        var = ts.get("variable") or {}
        var_codes = var.get("variableCode") or []
        param = str(var_codes[0].get("value") or "").strip() if var_codes else ""

        # Latest value: values[0].value[-1] (most recent sample).
        values_blocks = ts.get("values") or []
        latest_val: float | None = None
        latest_dt: str | None = None
        if values_blocks:
            samples = values_blocks[0].get("value") or []
            if samples:
                last = samples[-1]
                raw_v = last.get("value")
                try:
                    fv = float(raw_v)
                    # USGS encodes no-data as -999999.
                    if fv > -999990.0:
                        latest_val = fv
                        latest_dt = str(last.get("dateTime") or "") or None
                except (TypeError, ValueError):
                    latest_val = None

        rec = by_site.setdefault(
            site_no,
            {
                "site_no": site_no,
                "site_name": site_name,
                "lon": lon,
                "lat": lat,
                "discharge_cfs": None,
                "gage_height_ft": None,
                "reading_dt": None,
            },
        )
        # Keep a non-empty name if a later series carries one.
        if site_name and not rec.get("site_name"):
            rec["site_name"] = site_name

        if param == _PARAM_DISCHARGE and latest_val is not None:
            rec["discharge_cfs"] = latest_val
            if latest_dt and not rec["reading_dt"]:
                rec["reading_dt"] = latest_dt
        elif param == _PARAM_GAGE_HEIGHT and latest_val is not None:
            rec["gage_height_ft"] = latest_val
            if latest_dt and not rec["reading_dt"]:
                rec["reading_dt"] = latest_dt

    return list(by_site.values())


def _parse_iv_json_window(raw: bytes) -> list[dict[str, Any]]:
    """Parse a WINDOWED IV WaterML-JSON body → one record per station with a
    full discharge HYDROGRAPH (``time_series_csv``).

    Mirrors ``_parse_iv_json`` (grouping by ``site_no``) but reads the WHOLE
    ``values[0].value[]`` array for the DISCHARGE parameter (00060) and emits an
    inline ``time_series_csv`` attribute (``"iso,value"`` rows, ft^3/s) — the
    EXACT shape ``fetch_noaa_coops_tides`` emits for water level — so the SFINCS
    forcing adapter can build a real multi-point river hydrograph.

    Per station record:

        {site_no, site_name, lon, lat,
         discharge_cfs (latest sample, for the static overlay),
         gage_height_ft (latest sample),
         reading_dt (latest discharge sample timestamp),
         time_series_csv (the full 00060 hydrograph; "" if 00060 absent),
         time_start, time_end, n_timesteps,
         discharge_min_cfs, discharge_max_cfs, discharge_mean_cfs}

    Stations with no parseable coordinate are dropped. Records that carry NO
    discharge series (only gage height in the window) still survive with an
    empty ``time_series_csv`` — the FGB builder keeps them so the overlay shows
    the station, and the forcing adapter skips empty-series rows. Returns ``[]``
    for an empty body.
    """
    if not raw:
        return []
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise NwisUpstreamError(
            f"USGS IV (window) response is not valid JSON: {exc}"
        ) from exc

    series = (obj.get("value") or {}).get("timeSeries") or []
    by_site: dict[str, dict[str, Any]] = {}

    for ts in series:
        source = ts.get("sourceInfo") or {}
        site_codes = source.get("siteCode") or []
        if not site_codes:
            continue
        site_no = str(site_codes[0].get("value") or "").strip()
        if not site_no:
            continue
        site_name = str(source.get("siteName") or "").strip()

        geo = (source.get("geoLocation") or {}).get("geogLocation") or {}
        try:
            lat = float(geo.get("latitude"))
            lon = float(geo.get("longitude"))
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(lat) and math.isfinite(lon)):
            continue

        var = ts.get("variable") or {}
        var_codes = var.get("variableCode") or []
        param = str(var_codes[0].get("value") or "").strip() if var_codes else ""

        # Parse the FULL sample array (the hydrograph), filtering no-data.
        samples: list[tuple[str, float]] = []
        values_blocks = ts.get("values") or []
        if values_blocks:
            for s in values_blocks[0].get("value") or []:
                raw_v = s.get("value")
                try:
                    fv = float(raw_v)
                except (TypeError, ValueError):
                    continue
                if fv <= -999990.0:  # USGS no-data sentinel
                    continue
                dt_s = str(s.get("dateTime") or "").strip()
                if not dt_s:
                    continue
                samples.append((dt_s, fv))

        rec = by_site.setdefault(
            site_no,
            {
                "site_no": site_no,
                "site_name": site_name,
                "lon": lon,
                "lat": lat,
                "discharge_cfs": None,
                "gage_height_ft": None,
                "reading_dt": None,
                "time_series_csv": "",
                "time_start": None,
                "time_end": None,
                "n_timesteps": 0,
                "discharge_min_cfs": None,
                "discharge_max_cfs": None,
                "discharge_mean_cfs": None,
            },
        )
        if site_name and not rec.get("site_name"):
            rec["site_name"] = site_name

        if not samples:
            continue

        if param == _PARAM_DISCHARGE:
            # Build the inline time_series_csv (the hydrograph driver).
            lines = [f"{dt_s},{v:.6f}" for dt_s, v in samples]
            rec["time_series_csv"] = "\n".join(lines) + "\n"
            vals = [v for _dt_s, v in samples]
            rec["n_timesteps"] = len(vals)
            rec["time_start"] = samples[0][0]
            rec["time_end"] = samples[-1][0]
            rec["discharge_min_cfs"] = min(vals)
            rec["discharge_max_cfs"] = max(vals)
            rec["discharge_mean_cfs"] = sum(vals) / len(vals)
            # Latest sample → the static-overlay scalar (same as instant mode).
            rec["discharge_cfs"] = samples[-1][1]
            rec["reading_dt"] = samples[-1][0]
        elif param == _PARAM_GAGE_HEIGHT:
            rec["gage_height_ft"] = samples[-1][1]
            if rec["reading_dt"] is None:
                rec["reading_dt"] = samples[-1][0]

    return list(by_site.values())


def _parse_site_rdb(raw: bytes) -> list[dict[str, Any]]:
    """Parse the Site-service RDB (tab-delimited) body → station-location records.

    RDB layout: comment lines start with ``#``; the first non-comment line is
    the column header, the second is a type/width line we skip, then data rows.
    We extract ``site_no``, ``station_nm``, ``dec_lat_va``, ``dec_long_va``.
    Records carry NO readings (discharge/gage_height stay ``None``). Returns
    ``[]`` when the body is empty or carries zero data rows.
    """
    if not raw:
        return []
    text = raw.decode("utf-8", errors="replace")
    data_lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    if len(data_lines) < 3:
        # Need header + type line + at least one data row.
        return []

    header = data_lines[0].split("\t")
    try:
        i_site = header.index("site_no")
        i_lat = header.index("dec_lat_va")
        i_lon = header.index("dec_long_va")
    except ValueError:
        raise NwisUpstreamError(
            f"USGS Site RDB missing required columns; got header {header[:12]}"
        )
    i_name = header.index("station_nm") if "station_nm" in header else None

    records: list[dict[str, Any]] = []
    # Skip the header (0) and the type/width line (1).
    for row in data_lines[2:]:
        cols = row.split("\t")
        if len(cols) <= max(i_site, i_lat, i_lon):
            continue
        site_no = cols[i_site].strip()
        if not site_no:
            continue
        try:
            lat = float(cols[i_lat])
            lon = float(cols[i_lon])
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(lat) and math.isfinite(lon)):
            continue
        site_name = cols[i_name].strip() if (i_name is not None and len(cols) > i_name) else ""
        records.append(
            {
                "site_no": site_no,
                "site_name": site_name,
                "lon": lon,
                "lat": lat,
                "discharge_cfs": None,
                "gage_height_ft": None,
                "reading_dt": None,
            }
        )
    return records


# ---------------------------------------------------------------------------
# FlatGeobuf builder.
# ---------------------------------------------------------------------------


def _build_flatgeobuf(records: list[dict[str, Any]]) -> bytes:
    """Serialize station records → FlatGeobuf bytes (Point geometry, EPSG:4326).

    One Point feature per station carrying ``site_no``, ``site_name``,
    ``discharge_cfs``, ``gage_height_ft``, ``reading_dt``.

    Raises ``NwisUpstreamError`` if geopandas/shapely are unavailable or the
    write fails. ``records`` must be non-empty (the caller enforces the
    no-stations honest-error gate before calling this).
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
        from shapely.geometry import Point  # type: ignore[import-not-found]
    except ImportError as exc:
        raise NwisUpstreamError(
            f"geopandas / shapely not available: {exc}"
        ) from exc

    geoms = [Point(r["lon"], r["lat"]) for r in records]
    data = {
        "site_no": [str(r["site_no"]) for r in records],
        "site_name": [str(r.get("site_name") or "") for r in records],
        "discharge_cfs": [r.get("discharge_cfs") for r in records],
        "gage_height_ft": [r.get("gage_height_ft") for r in records],
        "reading_dt": [r.get("reading_dt") for r in records],
    }
    gdf = gpd.GeoDataFrame(data, geometry=geoms, crs="EPSG:4326")

    tmp_fgb: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="trid3nt_nwis_"
        ) as f:
            tmp_fgb = f.name
        gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        with open(tmp_fgb, "rb") as f:
            return f.read()
    except Exception as exc:
        raise NwisUpstreamError(
            f"FlatGeobuf write failed for {len(records)} gauge stations: {exc}"
        ) from exc
    finally:
        if tmp_fgb is not None:
            try:
                os.unlink(tmp_fgb)
            except OSError:
                pass


def _build_window_flatgeobuf(records: list[dict[str, Any]]) -> bytes:
    """Serialize WINDOW (hydrograph) station records → FlatGeobuf bytes.

    One Point feature per station carrying the inline ``time_series_csv``
    hydrograph attribute (discharge, ft^3/s) plus the latest-sample scalars and
    the per-station summary fields — the SAME column shape
    ``fetch_noaa_coops_tides`` emits, so the SFINCS forcing adapter consumes it
    via the existing ``time_series_csv`` path.

    Raises ``NwisUpstreamError`` if geopandas/shapely are unavailable or the
    write fails. ``records`` must be non-empty.
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
        from shapely.geometry import Point  # type: ignore[import-not-found]
    except ImportError as exc:
        raise NwisUpstreamError(
            f"geopandas / shapely not available: {exc}"
        ) from exc

    geoms = [Point(r["lon"], r["lat"]) for r in records]
    data = {
        "site_no": [str(r["site_no"]) for r in records],
        "site_name": [str(r.get("site_name") or "") for r in records],
        "discharge_cfs": [r.get("discharge_cfs") for r in records],
        "gage_height_ft": [r.get("gage_height_ft") for r in records],
        "reading_dt": [r.get("reading_dt") for r in records],
        "time_series_csv": [str(r.get("time_series_csv") or "") for r in records],
        "time_start": [r.get("time_start") for r in records],
        "time_end": [r.get("time_end") for r in records],
        "n_timesteps": [int(r.get("n_timesteps") or 0) for r in records],
        "discharge_min_cfs": [r.get("discharge_min_cfs") for r in records],
        "discharge_max_cfs": [r.get("discharge_max_cfs") for r in records],
        "discharge_mean_cfs": [r.get("discharge_mean_cfs") for r in records],
    }
    gdf = gpd.GeoDataFrame(data, geometry=geoms, crs="EPSG:4326")

    tmp_fgb: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="trid3nt_nwis_hyd_"
        ) as f:
            tmp_fgb = f.name
        gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        with open(tmp_fgb, "rb") as f:
            return f.read()
    except Exception as exc:
        raise NwisUpstreamError(
            f"FlatGeobuf write failed for {len(records)} gauge hydrographs: {exc}"
        ) from exc
    finally:
        if tmp_fgb is not None:
            try:
                os.unlink(tmp_fgb)
            except OSError:
                pass


def _records_bbox(
    records: list[dict[str, Any]],
) -> tuple[float, float, float, float] | None:
    """Compute the (west, south, east, north) extent of the station points.

    Pads a degenerate single-point extent by ~0.05° so the camera does not
    zoom to an infinite level. Returns ``None`` for an empty list.
    """
    if not records:
        return None
    lons = [r["lon"] for r in records]
    lats = [r["lat"] for r in records]
    west, east = min(lons), max(lons)
    south, north = min(lats), max(lats)
    if west == east:
        west -= 0.05
        east += 0.05
    if south == north:
        south -= 0.05
        north += 0.05
    return (west, south, east, north)


# ---------------------------------------------------------------------------
# Top-level fetch (passed to read_through). Returns (fgb_bytes, extent_bbox).
# ---------------------------------------------------------------------------


def _fetch_usgs_nwis_gauges_bytes(
    *,
    state_code: str | None,
    bbox: tuple[float, float, float, float] | None,
) -> tuple[bytes, tuple[float, float, float, float]]:
    """End-to-end fetch: IV (observed) → Site (locations) fallback → FGB bytes.

    Returns ``(fgb_bytes, extent_bbox)``. Raises ``NwisNoStationsError`` when
    BOTH the IV service and the Site-service fallback return zero stations.
    """
    # 1. PRIMARY: Instantaneous Values (real-time observed discharge/stage).
    iv_url = _build_iv_url(state_code=state_code, bbox=bbox)
    logger.info("fetch_usgs_nwis_gauges: IV GET %s", iv_url)
    iv_raw = _http_get(iv_url)
    records = _parse_iv_json(iv_raw)

    if records:
        logger.info(
            "fetch_usgs_nwis_gauges: IV returned %d gauge station(s) with readings",
            len(records),
        )
    else:
        # 2. FALLBACK: Site service — at least the station LOCATIONS.
        logger.info(
            "fetch_usgs_nwis_gauges: IV returned 0 active sites; falling back "
            "to the Site service for station locations"
        )
        site_url = _build_site_url(state_code=state_code, bbox=bbox)
        logger.info("fetch_usgs_nwis_gauges: SITE GET %s", site_url)
        site_raw = _http_get(site_url)
        records = _parse_site_rdb(site_raw)
        if records:
            logger.info(
                "fetch_usgs_nwis_gauges: Site-service fallback returned %d "
                "station location(s) (no current reading)",
                len(records),
            )

    # 3. Honest typed error if BOTH miss — never an empty success layer.
    if not records:
        scope = (
            f"state_code={state_code!r}"
            if state_code is not None
            else f"bbox={bbox!r}"
        )
        raise NwisNoStationsError(
            f"No active USGS NWIS gauge stations reporting discharge (00060) or "
            f"gage height (00065) found for {scope}. The IV real-time service "
            f"and the Site-service location fallback both returned zero stations. "
            f"Either the area has no instrumented gauges or none are currently "
            f"active; try a different area or a state-level query."
        )

    extent = _records_bbox(records)
    assert extent is not None  # records is non-empty here
    return _build_flatgeobuf(records), extent


def _fetch_usgs_nwis_hydrograph_bytes(
    *,
    state_code: str | None,
    bbox: tuple[float, float, float, float] | None,
    window: tuple[str, str] | str,
) -> tuple[bytes, tuple[float, float, float, float]]:
    """End-to-end WINDOW fetch: IV-window (full hydrographs) → FGB bytes.

    Unlike the latest-instantaneous path, this requests the IV service with a
    time window so the body carries the FULL per-site discharge series, then
    emits one Point feature per station carrying an inline ``time_series_csv``.
    There is NO Site-service fallback here (the Site service has no readings, so
    a hydrograph request that misses is an honest no-stations error).

    Returns ``(fgb_bytes, extent_bbox)``. Raises ``NwisNoStationsError`` when
    the IV-window request returns zero stations.
    """
    iv_url = _build_iv_url(state_code=state_code, bbox=bbox, window=window)
    logger.info("fetch_usgs_nwis_gauges: IV-window GET %s", iv_url)
    iv_raw = _http_get(iv_url)
    records = _parse_iv_json_window(iv_raw)

    if records:
        n_with_series = sum(1 for r in records if r.get("time_series_csv"))
        logger.info(
            "fetch_usgs_nwis_gauges: IV-window returned %d station(s); "
            "%d carry a discharge hydrograph",
            len(records),
            n_with_series,
        )
    else:
        scope = (
            f"state_code={state_code!r}"
            if state_code is not None
            else f"bbox={bbox!r}"
        )
        raise NwisNoStationsError(
            f"No active USGS NWIS gauge stations reporting discharge (00060) or "
            f"gage height (00065) found for {scope} over window {window!r}. The IV "
            f"real-time service returned zero stations for that time range. Try a "
            f"different area, a wider window, or a state-level query."
        )

    extent = _records_bbox(records)
    assert extent is not None
    return _build_window_flatgeobuf(records), extent


# ---------------------------------------------------------------------------
# Registered atomic tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    supports_global_query=False,
    payload_mb_estimator_name="estimate_payload_mb",
    # Annotations: readOnlyHint=True (read-only; no state mutation),
    # openWorldHint=True (calls external public API endpoint),
    # destructiveHint=False, idempotentHint=True (cache shim deduplicates).
    open_world_hint=True,
)
def fetch_usgs_nwis_gauges(
    state_code: str | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    period: str | None = None,
    # Absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch REAL, OBSERVED USGS stream-gauge stations as a point FlatGeobuf.

    Retrieves active USGS NWIS / Water Services stream gauges and their latest
    instantaneous readings — discharge (00060, ft^3/s) and/or gage height
    (00065, ft) — from the machine API behind ``waterdata.usgs.gov``. Returns
    one Point feature per gauge station at the station's coordinates, carrying
    the latest reading. This is the canonical **observed** (instrument-record)
    gauge source.

    TWO MODES (the temporal selector decides):
        - DEFAULT (no start/end/period): one Point per station carrying the
          LATEST instantaneous discharge / gage height (the map-overlay mode).
        - HYDROGRAPH WINDOW (an explicit ``start_date`` + ``end_date``, or a
          relative ``period`` like ``"P7D"``): one Point per station carrying
          the FULL discharge time series as an inline ``time_series_csv``
          attribute (``"iso,value"`` rows, ft^3/s) — the same shape
          ``fetch_noaa_coops_tides`` emits. This is the REAL river DISCHARGE
          HYDROGRAPH the compound-flood SFINCS deck needs as its fluvial driver
          (instead of a flat constant synthesised from a single value).

    When to use:
        - The user asks for USGS water/stream gauges, "gauge stations", "stream
          gages", observed streamflow/stage, or "real gauge readings"
          (e.g. "show me the USGS stream gauges in Washington", "where are the
          river gauges near Boise", "current discharge at the gages upstream").
        - You need actual measured discharge or gage height at instrumented
          sites — the real instrument record, NOT a model estimate.
        - Cross-checking / ground-truthing a modeled streamflow layer against
          the observed gauge network.

    When NOT to use:
        - MODELED reach flow on the full NHDPlus channel network — that is
          ``fetch_noaa_nwm_streamflow`` (the National Water Model, ~2.7M modeled
          reaches). This tool (``fetch_usgs_nwis_gauges``) is the OBSERVED gauge
          network; NWM is the MODELED companion. When the user says "USGS
          gauges" / "real readings" → THIS tool. When they say "modeled flow" /
          "NWM" / "the whole river network" → ``fetch_noaa_nwm_streamflow``.
        - River-reach POLYLINES without gauges — use ``fetch_river_geometry``
          (NHDPlus HR) or ``fetch_nhdplus_nldi_navigate``.
        - Global / non-US discharge — use ``fetch_cama_flood_discharge`` (this
          tool is US + territories only; supports_global_query=False).
        - Precipitation / design-storm forcing — use ``fetch_mrms_qpe`` or
          ``lookup_precip_return_period``.
        - Coastal tide-station water levels — use ``fetch_noaa_coops_tides``.

    Spatial selector (pass EXACTLY ONE):
        state_code: Optional 2-letter USPS state/territory code (e.g. ``"WA"``,
            ``"FL"``, ``"CA"``). PREFER THIS for state-level asks ("Washington
            state", "gauges in Florida"). ``stateCd`` has NO area limit, so it
            is the correct call when the area of interest is a whole state —
            a whole-state bbox would exceed the USGS bBox area cap and 400.
        bbox: Optional ``(west, south, east, north)`` in EPSG:4326 for an
            area-of-interest query (a metro, watershed, or sub-state region).
            USGS limits the bbox so ``(east-west) × (north-south)`` must be
            <= ~25 deg^2. If the bbox exceeds ~24.5 deg^2 AND ``state_code`` is
            not given, ``NwisBboxTooLargeError`` is raised telling you to pass
            ``state_code`` (or a smaller bbox) — we never silently 400.
        When both are given, ``state_code`` wins. When neither is given,
        ``NwisInputError`` is raised.

    Temporal selector (controls instantaneous vs hydrograph mode):
        start_date / end_date: Optional ISO ``YYYY-MM-DD`` window bounds. Pass
            BOTH to request the full per-station DISCHARGE HYDROGRAPH over the
            window (one Point per station carrying a ``time_series_csv``). The
            window is capped at 120 days. Example: ``start_date="2018-10-08"``,
            ``end_date="2018-10-14"`` for the Hurricane Michael week.
        period: Optional relative ISO-8601 duration (e.g. ``"P7D"`` = last 7
            days, ``"P1M"`` = last month, ``"PT6H"`` = last 6 hours). A simpler
            alternative to start/end; ``period`` WINS if both are supplied. Use
            this for "the last week's flow at the gauges".
        When NONE of start_date / end_date / period is given, the tool returns
        the LATEST instantaneous reading per station (the default overlay mode).

    Returns:
        ``LayerURI`` pointing at a FlatGeobuf in the cache bucket:
        ``s3://trid3nt-cache/cache/dynamic-1h/usgs_nwis_gauges/<key>.fgb``
        - ``layer_type="vector"``, ``role="primary"``,
          ``style_preset="usgs_gauges"``, ``units="mixed (cfs / ft)"``.
        - Geometry: Point at each gauge station's coordinates, EPSG:4326.
        - ``bbox`` is set to the stations' extent so the client camera
          auto-zooms (the layer renders via the inline-GeoJSON vector path).
        - Properties per station: ``site_no`` (NWIS site number), ``site_name``,
          ``discharge_cfs`` (latest 00060 reading, ft^3/s; null if not
          reported), ``gage_height_ft`` (latest 00065 reading, ft; null if not
          reported), ``reading_dt`` (ISO-8601 timestamp of the latest reading;
          null when only locations are available via the Site-service fallback).

    Fallback behaviour (data-source fallback norm — primary → fallback → honest
    typed error): the Instantaneous Values service is the primary (observed
    readings). If it returns zero active sites in scope, the tool falls back to
    the Site service to at least return station LOCATIONS (with null readings).
    If BOTH return zero stations, ``NwisNoStationsError`` is raised — never an
    empty success-shaped layer.

    Cache: ``ttl_class="dynamic-1h"``, ``source_class="usgs_nwis_gauges"``.
    Cache key is SHA-256 of the resolved selector (``state_code`` or
    bbox-rounded-6dp), so identical-scope calls within the hour reuse the FGB.

    Cross-tool dependencies:
        - Composes WITH: ``publish_layer`` (map overlay), ``geocode_location``
          (derive a bbox or surface a state from a place name BEFORE this call),
          ``fetch_administrative_boundaries`` (state/county framing).
        - Cross-checks: ``fetch_noaa_nwm_streamflow`` (modeled reach flow —
          observed-vs-modeled comparison), ``fetch_river_geometry`` (the reach
          polylines the gauges sit on).
        - Upstream data source: USGS Water Services
          (waterservices.usgs.gov/nwis/iv + /nwis/site).

    Errors:
        - ``NwisInputError``: no selector / bad bbox / bad state code
          (retryable=False).
        - ``NwisBboxTooLargeError``: bbox exceeds the USGS ~25 deg^2 area limit
          and no state_code given — re-issue with state_code (retryable=False).
        - ``NwisUpstreamError``: USGS network failure / HTTP 5xx / bad body
          (retryable=True).
        - ``NwisNoStationsError``: no active gauges in scope from EITHER service
          (retryable=False).


    Tier-1 free. No API key. ``supports_global_query=False`` (US + territories).
    """
    # 1. Resolve the spatial selector. state_code wins when both are given.
    resolved_state: str | None = None
    resolved_bbox: tuple[float, float, float, float] | None = None

    if state_code is not None and str(state_code).strip() != "":
        resolved_state = _validate_state_code(state_code)
    elif bbox is not None:
        if not isinstance(bbox, (tuple, list)):
            raise NwisInputError(
                f"bbox must be a 4-tuple/list or omitted; got {type(bbox).__name__}"
            )
        bbox_t: tuple[float, float, float, float] = tuple(
            float(v) for v in bbox
        )  # type: ignore[assignment]
        _validate_bbox(bbox_t)
        area = _bbox_area_sq_deg(bbox_t)
        if area > _MAX_BBOX_SQ_DEG:
            raise NwisBboxTooLargeError(
                f"bbox area {area:.1f} deg^2 exceeds the USGS NWIS bBox limit "
                f"(~25 deg^2); a whole-state bbox (e.g. Washington ~28 deg^2) "
                f"will 400. For a state-level query pass state_code (e.g. "
                f"state_code='WA') instead — stateCd has no area limit — or "
                f"re-issue with a smaller bbox (<= ~{_MAX_BBOX_SQ_DEG:.0f} deg^2)."
            )
        resolved_bbox = _round_bbox_to_6dp(bbox_t)
    else:
        raise NwisInputError(
            "fetch_usgs_nwis_gauges requires a spatial selector: pass "
            "state_code (2-letter USPS, e.g. 'WA') for a state-level query, or "
            "bbox=(west, south, east, north) for an area query."
        )

    # 1b. Resolve the TEMPORAL selector → instantaneous (default) or hydrograph.
    window = _resolve_window(start_date, end_date, period)
    is_window = window is not None

    # 2. Cache-key params (resolved selector + temporal window).
    params: dict[str, Any] = {
        "state_code": resolved_state,
        "bbox": list(resolved_bbox) if resolved_bbox is not None else None,
        "window": (
            list(window) if isinstance(window, tuple)
            else (window if isinstance(window, str) else None)
        ),
    }

    # The fetch_fn returns (bytes, extent); read_through caches the bytes. We
    # need the extent for LayerURI.bbox, so capture it via a closure side-channel.
    captured: dict[str, Any] = {}

    def _fetch_bytes() -> bytes:
        if is_window:
            fgb, extent = _fetch_usgs_nwis_hydrograph_bytes(
                state_code=resolved_state, bbox=resolved_bbox, window=window
            )
        else:
            fgb, extent = _fetch_usgs_nwis_gauges_bytes(
                state_code=resolved_state, bbox=resolved_bbox
            )
        captured["extent"] = extent
        return fgb

    # 3. Read-through cache.
    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=_fetch_bytes,
    )
    assert result.uri is not None, (
        "fetch_usgs_nwis_gauges is cacheable; uri must be set by read_through"
    )

    # 4. Resolve the camera extent. On a cache HIT the fetch_fn never ran so
    # ``captured`` is empty — fall back to the requested bbox (state-level
    # queries have no requested bbox, so leave it None: the inline-GeoJSON
    # vector path still fits the map to the rendered features).
    extent_bbox: tuple[float, float, float, float] | None = captured.get("extent")
    if extent_bbox is None:
        extent_bbox = resolved_bbox

    # 5. Build a descriptive layer name + stable id.
    scope_tag = resolved_state if resolved_state is not None else (
        f"{resolved_bbox[0]:.2f},{resolved_bbox[1]:.2f}→"
        f"{resolved_bbox[2]:.2f},{resolved_bbox[3]:.2f}"
        if resolved_bbox is not None
        else "?"
    )
    seed = hashlib.sha256(
        json.dumps(params, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:8]

    if is_window:
        if isinstance(window, str):
            window_tag = window
        else:
            window_tag = f"{window[0]}..{window[1]}"  # type: ignore[index]
        name = f"USGS discharge hydrographs — {scope_tag} ({window_tag})"
        layer_id = f"usgs-hydrograph-{seed}"
        style_preset = "usgs_gauges_hydrograph"
        units = "ft^3/s (discharge hydrograph)"
    else:
        name = f"USGS stream gauges — {scope_tag}"
        layer_id = f"usgs-gauges-{seed}"
        style_preset = "usgs_gauges"
        units = "mixed (cfs / ft)"

    return LayerURI(
        layer_id=layer_id,
        name=name,
        layer_type="vector",
        uri=result.uri,
        style_preset=style_preset,
        role="primary",
        units=units,
        bbox=extent_bbox,
    )
