"""``fetch_storm_events_db`` atomic tool — NOAA Storm Events DB Tier-1 fetcher (job-0091).

Downloads the annual NOAA Storm Events Database details CSV (gzip) from
``https://www.ncei.noaa.gov/pub/data/swdi/stormevents/csvfiles/``, filters by
state, event-type, an optional spatial ``bbox`` (W,S,E,N), and an optional
``begin_date``/``end_date`` temporal window, converts to FlatGeobuf with point
geometry from ``BEGIN_LAT``/``BEGIN_LON``, and returns a ``LayerURI`` pointing
at the cached artifact.

The NOAA Storm Events Database is the authoritative US storm-event catalog
maintained by NCEI. Files follow the pattern::

    StormEvents_details-ftp_v1.0_d{year}_c{processed_date}.csv.gz

``processed_date`` is volatile (re-stamped on every NCEI reprocessing), so the
implementation scrapes the HTTP directory index to find the current file for
``year`` rather than hard-coding the processed date.

A ``begin_date``/``end_date`` window may span more than one calendar year; the
fetcher then downloads every annual CSV the window touches (``year`` is used as
the anchor when no window is given) and concatenates the rows before filtering.

FR-TA-2: atomic tool returning ``LayerURI``.
FR-CE-8 / FR-DC-3/4: routed through ``read_through`` so identical
``(year, state, event_types, bbox, begin_date, end_date)`` calls reuse the
cached FlatGeobuf (static-30d).
"""

from __future__ import annotations

import csv
import datetime as _dt
import gzip
import hashlib
import io
import json
import logging
import math
import re
import tempfile
from typing import Any

import httpx

from grace2_contracts.execution import LayerURI
from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import read_through

__all__ = [
    "fetch_storm_events_db",
    "StormEventsUpstreamError",
    "estimate_payload_mb",
]

logger = logging.getLogger("grace2_agent.tools.fetch_storm_events_db")

# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

_INDEX_URL = "https://www.ncei.noaa.gov/pub/data/swdi/stormevents/csvfiles/"
_FILE_RE = re.compile(
    r"StormEvents_details-ftp_v1\.0_d(\d{4})_c(\d{8})\.csv\.gz"
)

_USER_AGENT = (
    "grace-2/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/GRACE-2; agent@grace-2.dev)"
)

# Properties retained on each output point. Matches the audit.md spec; the
# MAGNITUDE + DEATHS_DIRECT/DEATHS_INDIRECT columns were added in the bbox /
# date-window upgrade (props were requested for severity context).
_RETAINED_COLUMNS = (
    "EVENT_ID",
    "EVENT_TYPE",
    "STATE",
    "BEGIN_DATE_TIME",
    "END_DATE_TIME",
    "INJURIES_DIRECT",
    "DEATHS_DIRECT",
    "DEATHS_INDIRECT",
    "DAMAGE_PROPERTY",
    "MAGNITUDE",
    "EPISODE_NARRATIVE",
)


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class StormEventsError(RuntimeError):
    """Base class for fetch_storm_events_db failures."""

    error_code: str = "STORM_EVENTS_ERROR"
    retryable: bool = True


class StormEventsUpstreamError(StormEventsError):
    """NOAA Storm Events Database download or parsing failed."""

    error_code = "STORM_EVENTS_UPSTREAM_ERROR"
    retryable = True


class StormEventsEmptyError(StormEventsError):
    """No events remain after filtering. Not retryable — filter is the cause."""

    error_code = "STORM_EVENTS_EMPTY"
    retryable = False


class StormEventsArgError(StormEventsError):
    """Invalid argument (e.g. year out of range, non-string state)."""

    error_code = "STORM_EVENTS_ARG_INVALID"
    retryable = False


# ---------------------------------------------------------------------------
# AtomicToolMetadata — registered once at import time.
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="fetch_storm_events_db",
    ttl_class="static-30d",
    source_class="storm_events",
    cacheable=True,
    # The NOAA Storm Events DB is national: a bbox-less / state-less call is a
    # legitimate CONUS-wide query, so this tool supports the global form.
    supports_global_query=True,
    payload_mb_estimator_name="estimate_payload_mb",
)


# ---------------------------------------------------------------------------
# Payload estimator (Wave 2 chat-warning system).
# ---------------------------------------------------------------------------


def estimate_payload_mb(
    year: int = 0,
    state: str | None = None,
    event_types: list[str] | None = None,
    bbox: tuple[float, float, float, float] | list[float] | None = None,
    begin_date: str | None = None,
    end_date: str | None = None,
    **_kw: Any,
) -> float:
    """Estimate the output FlatGeobuf size in MB for the requested filter.

    Each retained event is one Point feature with a handful of small scalar
    properties plus the (long) narrative, so ~600 bytes serialized on average.
    A full national year of Storm Events is ~70k events (~40 MB). State,
    event-type, bbox and window filters each shrink that. The estimate is
    deliberately conservative - it bounds the chat-warning gate, not the fetch.
    """
    # Base: number of annual files the window touches (each ~70k events).
    n_years = len(_window_years(year if isinstance(year, int) else 0,
                                begin_date, end_date))
    events = 70000.0 * max(1, n_years)

    # A single state is ~1/30 of the national count on average.
    if state is not None:
        events *= 1.0 / 30.0

    # Event-type filter: each named type is a slice of the 40+ categories.
    if event_types:
        events *= min(1.0, 0.10 * len(event_types))

    # bbox: scale by box area vs CONUS (~ -125,24 .. -66,50 ~= 59*26 sq deg).
    if bbox is not None:
        try:
            west, south, east, north = (float(v) for v in bbox)
            area = max(0.0, east - west) * max(0.0, north - south)
            events *= min(1.0, area / (59.0 * 26.0))
        except (TypeError, ValueError):
            pass

    # Window narrower than a full year scales linearly by day fraction.
    if begin_date is not None and end_date is not None:
        try:
            b = _parse_window_arg(begin_date, "begin_date")
            e = _parse_window_arg(end_date, "end_date")
            days = max(1.0, (e - b).total_seconds() / 86400.0)
            events *= min(1.0, days / (365.0 * max(1, n_years)))
        except StormEventsArgError:
            pass

    return max(0.001, events * 600.0 / 1_000_000.0)


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _validate_inputs(
    year: int,
    state: str | None,
    event_types: list[str] | None,
    bbox: tuple[float, float, float, float] | list[float] | None = None,
    begin_date: str | None = None,
    end_date: str | None = None,
) -> None:
    """Validate year/state/event_types/bbox/window or raise ``StormEventsArgError``.

    ``state`` accepts EITHER an ISO 2-letter code (``"OK"``) OR a full US state
    name (``"Oklahoma"``), case-insensitive — the LLM routinely passes the
    word the user typed. Both forms are recognized against the NOAA state
    catalog; only a genuinely-unrecognized state is rejected. The query path
    (``_parse_filter_and_serialize``) is already tolerant of both, so we
    validate-only here and let it normalize.

    ``bbox`` (W, S, E, N) is an optional spatial filter applied to each event's
    ``BEGIN_LON``/``BEGIN_LAT``; it must be a 4-tuple of finite floats inside
    the WGS84 range with W<E and S<N.

    ``begin_date``/``end_date`` form an optional inclusive temporal window in
    ISO ``YYYY-MM-DD`` (or full ``YYYY-MM-DDTHH:MM:SS``) form; the start must
    not be after the end. The window may span multiple calendar years.
    """
    if not isinstance(year, int):
        raise StormEventsArgError(f"year must be int, got {type(year).__name__}")
    # NOAA Storm Events DB coverage begins 1950.
    if year < 1950 or year > 2100:
        raise StormEventsArgError(
            f"year={year} out of NOAA Storm Events DB range [1950, 2100]"
        )
    if state is not None:
        if not isinstance(state, str) or not state.strip():
            raise StormEventsArgError(
                f"state must be a non-empty US state name or ISO 2-letter "
                f"code (e.g. 'Oklahoma' or 'OK'), got {state!r}"
            )
        token = state.strip().upper()
        # Accept an ISO 2-letter code (key) or a full state name (value).
        if token not in _ISO_TO_STATE_NAME and token not in _STATE_NAME_TO_ISO:
            raise StormEventsArgError(
                f"unrecognized US state {state!r}; expected a state name "
                f"(e.g. 'Oklahoma') or ISO 2-letter code (e.g. 'OK')"
            )
    if event_types is not None:
        if not isinstance(event_types, list) or not all(
            isinstance(e, str) and e for e in event_types
        ):
            raise StormEventsArgError(
                f"event_types must be list[str] with non-empty strings, got {event_types!r}"
            )
    if bbox is not None:
        _validate_bbox(bbox)
    # Window: parse both ends (if present) and enforce ordering.
    b_dt = _parse_window_arg(begin_date, "begin_date") if begin_date is not None else None
    e_dt = _parse_window_arg(end_date, "end_date") if end_date is not None else None
    if b_dt is not None and e_dt is not None and b_dt > e_dt:
        raise StormEventsArgError(
            f"begin_date {begin_date!r} is after end_date {end_date!r}"
        )


def _validate_bbox(
    bbox: tuple[float, float, float, float] | list[float],
) -> None:
    """Raise ``StormEventsArgError`` if ``bbox`` (W, S, E, N) is malformed.

    Mirrors the ``fetch_usgs_earthquakes`` bbox contract: a 4-tuple of finite
    floats inside the WGS84 range with strictly W<E and S<N.
    """
    if not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
        raise StormEventsArgError(
            f"bbox must be (west, south, east, north); got {bbox!r}"
        )
    try:
        west, south, east, north = (float(v) for v in bbox)
    except (TypeError, ValueError) as exc:
        raise StormEventsArgError(
            f"bbox must contain four numbers; got {bbox!r}"
        ) from exc
    if not all(math.isfinite(v) for v in (west, south, east, north)):
        raise StormEventsArgError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= west <= 180.0 and -180.0 <= east <= 180.0):
        raise StormEventsArgError(f"bbox lon values out of [-180, 180]: {bbox!r}")
    if not (-90.0 <= south <= 90.0 and -90.0 <= north <= 90.0):
        raise StormEventsArgError(f"bbox lat values out of [-90, 90]: {bbox!r}")
    if west >= east or south >= north:
        raise StormEventsArgError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )


def _parse_window_arg(value: str, label: str) -> _dt.datetime:
    """Parse one window endpoint (``begin_date`` / ``end_date``) to a datetime.

    Accepts ISO ``YYYY-MM-DD`` (interpreted as midnight) or a full
    ``YYYY-MM-DDTHH:MM:SS`` form. Raises ``StormEventsArgError`` on a malformed
    string. The returned value is naive (no tz) for direct comparison against
    NOAA's local event times, which carry no zone offset.
    """
    if not isinstance(value, str) or not value.strip():
        raise StormEventsArgError(
            f"{label} must be a non-empty ISO date string "
            f"(YYYY-MM-DD), got {value!r}"
        )
    raw = value.strip().replace("Z", "")
    try:
        dt = _dt.datetime.fromisoformat(raw)
    except ValueError as exc:
        raise StormEventsArgError(
            f"{label}={value!r} is not ISO YYYY-MM-DD or "
            f"YYYY-MM-DDTHH:MM:SS: {exc}"
        ) from exc
    # Drop any tz to compare against NOAA's zone-naive local timestamps.
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt


def _window_years(
    year: int,
    begin_date: str | None,
    end_date: str | None,
) -> list[int]:
    """Resolve which annual NOAA CSVs the request must download.

    When no window is given, only ``year`` is fetched (backward compatible).
    When a window is given, every calendar year the inclusive window touches is
    fetched so events near a year boundary are not lost. The ``year`` anchor is
    still included (it bounds the layer name / cache seed and is harmless when
    the window already covers it).
    """
    years = {year}
    b = _parse_window_arg(begin_date, "begin_date") if begin_date is not None else None
    e = _parse_window_arg(end_date, "end_date") if end_date is not None else None
    if b is not None or e is not None:
        lo = (b or e).year  # type: ignore[union-attr]
        hi = (e or b).year  # type: ignore[union-attr]
        if lo > hi:
            lo, hi = hi, lo
        years.update(range(lo, hi + 1))
    return sorted(years)


def _resolve_csv_url(year: int, *, client: httpx.Client | None = None) -> str:
    """Scrape the directory index to find the current CSV URL for ``year``.

    NCEI re-stamps the ``c{YYYYMMDD}`` suffix on every reprocessing, so we cannot
    hard-code it. We fetch the directory listing once and pick the newest
    processed-date suffix for the requested year.

    Raises:
        ``StormEventsUpstreamError`` if the index cannot be loaded or no entry
        exists for ``year``.
    """
    own_client = client is None
    if client is None:
        client = httpx.Client(
            timeout=60.0,
            headers={"User-Agent": _USER_AGENT},
            follow_redirects=True,
        )
    try:
        try:
            resp = client.get(_INDEX_URL)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise StormEventsUpstreamError(
                f"failed to fetch NOAA Storm Events index {_INDEX_URL}: {exc}"
            ) from exc

        candidates: list[tuple[str, str]] = []  # (processed_date, filename)
        for match in _FILE_RE.finditer(resp.text):
            file_year, processed_date = match.group(1), match.group(2)
            if int(file_year) == year:
                candidates.append((processed_date, match.group(0)))

        if not candidates:
            raise StormEventsUpstreamError(
                f"no NOAA Storm Events CSV found for year={year} in {_INDEX_URL}"
            )
        # Highest processed date = most recently reprocessed = canonical.
        candidates.sort(reverse=True)
        return _INDEX_URL + candidates[0][1]
    finally:
        if own_client:
            client.close()


def _download_csv_gz(url: str, *, client: httpx.Client | None = None) -> bytes:
    """Download a gzipped CSV from ``url`` and return raw gzip bytes.

    Raises ``StormEventsUpstreamError`` on transport errors.
    """
    own_client = client is None
    if client is None:
        # CSV gzip can be 50MB+ for active years; allow 120s.
        client = httpx.Client(
            timeout=120.0,
            headers={"User-Agent": _USER_AGENT},
            follow_redirects=True,
        )
    try:
        try:
            resp = client.get(url)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise StormEventsUpstreamError(
                f"failed to download {url}: {exc}"
            ) from exc
        return resp.content
    finally:
        if own_client:
            client.close()


def _parse_filter_and_serialize(
    gz_bytes: bytes | list[bytes],
    state: str | None,
    event_types: list[str] | None,
    bbox: tuple[float, float, float, float] | list[float] | None = None,
    begin_date: str | None = None,
    end_date: str | None = None,
) -> bytes:
    """Decompress + filter + emit FlatGeobuf bytes.

    ``gz_bytes`` may be a single gzip blob (one annual CSV) OR a list of blobs
    (the window spanned multiple years); the decompressed rows are concatenated
    before filtering.

    Filters:
        - ``state`` is matched case-insensitively against the ``STATE`` column
          using the ISO 2-letter code's state name (NOAA uses the full name in
          the CSV, e.g. ``FLORIDA``).
        - ``event_types`` is matched case-insensitively against ``EVENT_TYPE``.
        - ``bbox`` (W, S, E, N) keeps only events whose ``BEGIN_LON``/
          ``BEGIN_LAT`` fall inside the box (inclusive), applied AFTER numeric
          coercion.
        - ``begin_date``/``end_date`` keep only events whose begin instant
          falls in the inclusive window. The begin instant is taken from the
          structured ``BEGIN_YEARMONTH``+``BEGIN_DAY``(+``BEGIN_TIME``) columns
          when present (unambiguous), else parsed from ``BEGIN_DATE_TIME``.
        - Rows with non-finite ``BEGIN_LAT``/``BEGIN_LON`` are silently dropped.

    Returns FlatGeobuf bytes of a point layer in EPSG:4326.

    Raises:
        ``StormEventsUpstreamError`` if the gzip is corrupt or the CSV is
          missing required columns.
        ``StormEventsEmptyError`` if all rows are filtered out.
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
        import pandas as pd  # type: ignore[import-not-found]
        from shapely.geometry import Point  # type: ignore[import-not-found]
    except ImportError as exc:
        raise StormEventsUpstreamError(
            f"geopandas / pandas / shapely not available: {exc}"
        ) from exc

    blobs = [gz_bytes] if isinstance(gz_bytes, (bytes, bytearray)) else list(gz_bytes)

    frames = []
    for blob in blobs:
        # Decompress.
        try:
            csv_text = gzip.decompress(blob).decode("utf-8", errors="replace")
        except (OSError, EOFError) as exc:
            raise StormEventsUpstreamError(
                f"NOAA Storm Events gzip is corrupt: {exc}"
            ) from exc

        # Parse CSV with pandas — handles quoting + embedded newlines correctly.
        try:
            # low_memory=False so dtype inference is single-pass and stable on
            # the EPISODE_NARRATIVE long-text column.
            frame = pd.read_csv(
                io.StringIO(csv_text),
                dtype=str,
                low_memory=False,
                keep_default_na=False,
                na_values=[""],
            )
        except pd.errors.ParserError as exc:
            raise StormEventsUpstreamError(
                f"NOAA Storm Events CSV parse failed: {exc}"
            ) from exc
        frames.append(frame)

    df = pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0]

    required = {"BEGIN_LAT", "BEGIN_LON", "STATE", "EVENT_TYPE"}
    missing = required - set(df.columns)
    if missing:
        raise StormEventsUpstreamError(
            f"NOAA Storm Events CSV missing required columns: {sorted(missing)}; "
            f"got {sorted(df.columns)[:10]}..."
        )

    # State filter — NOAA writes the full state name; we accept either an ISO
    # 2-letter code ("OK") or a full name ("Oklahoma"), normalized here.
    if state is not None:
        state_name = _normalize_state(state)
        df = df[df["STATE"].str.upper() == state_name].copy()

    # Event-type filter (case-insensitive).
    if event_types is not None and len(event_types) > 0:
        wanted = {e.upper() for e in event_types}
        df = df[df["EVENT_TYPE"].str.upper().isin(wanted)].copy()

    # Temporal window filter on each event's begin instant.
    if begin_date is not None or end_date is not None:
        begin_dt = _derive_begin_datetime(df, pd)
        keep = pd.Series(True, index=df.index)
        if begin_date is not None:
            lo = _parse_window_arg(begin_date, "begin_date")
            keep &= begin_dt >= pd.Timestamp(lo)
        if end_date is not None:
            hi = _parse_window_arg(end_date, "end_date")
            # A bare date end means the whole day is included.
            if hi.hour == 0 and hi.minute == 0 and hi.second == 0 \
                    and len(str(end_date).strip()) <= 10:
                hi = hi + _dt.timedelta(days=1) - _dt.timedelta(seconds=1)
            keep &= begin_dt <= pd.Timestamp(hi)
        # Rows whose begin instant could not be parsed (NaT) drop out of any
        # window filter rather than silently passing.
        keep &= begin_dt.notna()
        df = df[keep].copy()

    # Coerce coordinates; drop rows with non-finite or missing values.
    df["BEGIN_LAT"] = pd.to_numeric(df["BEGIN_LAT"], errors="coerce")
    df["BEGIN_LON"] = pd.to_numeric(df["BEGIN_LON"], errors="coerce")
    df = df.dropna(subset=["BEGIN_LAT", "BEGIN_LON"]).copy()
    # Sanity-clip to WGS84 valid range; drop anything else as bad data.
    df = df[
        (df["BEGIN_LAT"].between(-90.0, 90.0))
        & (df["BEGIN_LON"].between(-180.0, 180.0))
    ].copy()

    # Spatial bbox filter (W, S, E, N), inclusive, after coercion.
    if bbox is not None:
        west, south, east, north = (float(v) for v in bbox)
        df = df[
            df["BEGIN_LON"].between(west, east)
            & df["BEGIN_LAT"].between(south, north)
        ].copy()

    if df.empty:
        raise StormEventsEmptyError(
            f"no NOAA Storm Events match state={state!r} "
            f"event_types={event_types!r} bbox={bbox!r} "
            f"begin_date={begin_date!r} end_date={end_date!r} "
            "after filtering"
        )

    # Restrict to retained columns (plus the lat/lon we use for geometry).
    keep_cols = [c for c in _RETAINED_COLUMNS if c in df.columns]
    df_out = df[keep_cols].copy()

    # Build GeoDataFrame with point geometry from BEGIN_LON/BEGIN_LAT.
    geom = [Point(lon, lat) for lon, lat in zip(df["BEGIN_LON"], df["BEGIN_LAT"])]
    gdf = gpd.GeoDataFrame(df_out, geometry=geom, crs="EPSG:4326")

    logger.info(
        "fetch_storm_events_db: %d feature(s) after filter state=%s event_types=%s",
        len(gdf),
        state,
        event_types,
    )

    # Serialize to FlatGeobuf.
    tmp_fgb: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="grace2_storm_"
        ) as fgb_f:
            tmp_fgb = fgb_f.name
        gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        with open(tmp_fgb, "rb") as f:
            return f.read()
    except Exception as exc:  # noqa: BLE001 — surface as upstream error
        raise StormEventsUpstreamError(
            f"FlatGeobuf serialization failed: {exc}"
        ) from exc
    finally:
        if tmp_fgb is not None:
            import os as _os
            try:
                _os.unlink(tmp_fgb)
            except OSError:
                pass


def _derive_begin_datetime(df: Any, pd: Any) -> Any:
    """Return a ``pandas`` datetime Series for each row's begin instant.

    Preference order (most-robust first):

    1. ``BEGIN_YEARMONTH`` (``YYYYMM``) + ``BEGIN_DAY`` (+ ``BEGIN_TIME``,
       ``HHMM``) - the structured columns NOAA ships. These carry an explicit
       4-digit year, so there is no 2-digit-year century ambiguity.
    2. ``BEGIN_DATE_TIME`` parsed as ``%d-%b-%y %H:%M:%S`` (the human form,
       e.g. ``28-SEP-22 14:00:00``). pandas resolves the 2-digit year via its
       1969-2068 pivot, which is correct for the modern Storm Events record.

    Unparseable rows become ``NaT`` and are excluded by the caller's window
    filter. Returns an empty datetime Series if neither source is present.
    """
    if "BEGIN_YEARMONTH" in df.columns and "BEGIN_DAY" in df.columns:
        ym = pd.to_numeric(df["BEGIN_YEARMONTH"], errors="coerce")
        day = pd.to_numeric(df["BEGIN_DAY"], errors="coerce")
        year = (ym // 100)
        month = (ym % 100)
        parts = pd.DataFrame({"year": year, "month": month, "day": day})
        base = pd.to_datetime(parts, errors="coerce")
        if "BEGIN_TIME" in df.columns:
            # BEGIN_TIME is HHMM (e.g. "1418"); add it as minutes-of-day.
            t = pd.to_numeric(df["BEGIN_TIME"], errors="coerce").fillna(0)
            minutes = (t // 100) * 60 + (t % 100)
            base = base + pd.to_timedelta(minutes, unit="m")
        return base
    if "BEGIN_DATE_TIME" in df.columns:
        return pd.to_datetime(
            df["BEGIN_DATE_TIME"],
            format="%d-%b-%y %H:%M:%S",
            errors="coerce",
        )
    return pd.Series(pd.NaT, index=df.index)


# ---------------------------------------------------------------------------
# ISO 2-letter → state name (NOAA convention).
#
# NOAA stores the full state name (uppercase) in the STATE column. We accept
# ISO 2-letter from callers (more ergonomic) and map to NOAA's spelling for
# the filter. Coverage: 50 states + DC + territories tracked by Storm Events.
# ---------------------------------------------------------------------------

_ISO_TO_STATE_NAME: dict[str, str] = {
    "AL": "ALABAMA", "AK": "ALASKA", "AZ": "ARIZONA", "AR": "ARKANSAS",
    "CA": "CALIFORNIA", "CO": "COLORADO", "CT": "CONNECTICUT",
    "DE": "DELAWARE", "DC": "DISTRICT OF COLUMBIA", "FL": "FLORIDA",
    "GA": "GEORGIA", "HI": "HAWAII", "ID": "IDAHO", "IL": "ILLINOIS",
    "IN": "INDIANA", "IA": "IOWA", "KS": "KANSAS", "KY": "KENTUCKY",
    "LA": "LOUISIANA", "ME": "MAINE", "MD": "MARYLAND",
    "MA": "MASSACHUSETTS", "MI": "MICHIGAN", "MN": "MINNESOTA",
    "MS": "MISSISSIPPI", "MO": "MISSOURI", "MT": "MONTANA",
    "NE": "NEBRASKA", "NV": "NEVADA", "NH": "NEW HAMPSHIRE",
    "NJ": "NEW JERSEY", "NM": "NEW MEXICO", "NY": "NEW YORK",
    "NC": "NORTH CAROLINA", "ND": "NORTH DAKOTA", "OH": "OHIO",
    "OK": "OKLAHOMA", "OR": "OREGON", "PA": "PENNSYLVANIA",
    "RI": "RHODE ISLAND", "SC": "SOUTH CAROLINA", "SD": "SOUTH DAKOTA",
    "TN": "TENNESSEE", "TX": "TEXAS", "UT": "UTAH", "VT": "VERMONT",
    "VA": "VIRGINIA", "WA": "WASHINGTON", "WV": "WEST VIRGINIA",
    "WI": "WISCONSIN", "WY": "WYOMING", "PR": "PUERTO RICO",
    "VI": "VIRGIN ISLANDS", "GU": "GUAM", "AS": "AMERICAN SAMOA",
    "MP": "NORTHERN MARIANA ISLANDS",
}

# Reverse map: full state name (uppercase) → ISO 2-letter code. Lets callers
# pass the spoken-language name ("Oklahoma") which the LLM almost always does;
# we normalize to the canonical form for the filter + cache key.
_STATE_NAME_TO_ISO: dict[str, str] = {
    name: iso for iso, name in _ISO_TO_STATE_NAME.items()
}


def _normalize_state(state: str | None) -> str | None:
    """Normalize a state arg to its NOAA full-name spelling (uppercase).

    Accepts an ISO 2-letter code (``"OK"``) or a full state name
    (``"Oklahoma"``), case-insensitive. Returns the NOAA STATE-column spelling
    (e.g. ``"OKLAHOMA"``). ``None`` passes through. An unrecognized token is
    returned upper-cased unchanged (the query filter then matches the raw
    STATE column directly) — ``_validate_inputs`` is the gate that rejects
    genuinely-bad states before we get here.
    """
    if state is None:
        return None
    token = state.strip().upper()
    if token in _ISO_TO_STATE_NAME:
        return _ISO_TO_STATE_NAME[token]
    if token in _STATE_NAME_TO_ISO:
        # Already a full name; uppercase canonical NOAA spelling.
        return token
    return token


# ---------------------------------------------------------------------------
# Fetch function — builds the bytes callable for read_through.
# ---------------------------------------------------------------------------


def _fetch_storm_events_bytes(
    year: int,
    state: str | None,
    event_types: list[str] | None,
    bbox: tuple[float, float, float, float] | list[float] | None = None,
    begin_date: str | None = None,
    end_date: str | None = None,
) -> bytes:
    """Resolve the NCEI CSV URL(s), download, filter, serialize to FlatGeobuf.

    When a ``begin_date``/``end_date`` window spans more than one calendar year
    every annual CSV the window touches is downloaded and the rows concatenated
    before filtering (NOAA ships one file per year).
    """
    years = _window_years(year, begin_date, end_date)
    # Share one httpx.Client across the index lookup + every annual download so
    # the directory index is fetched once.
    client = httpx.Client(
        timeout=120.0,
        headers={"User-Agent": _USER_AGENT},
        follow_redirects=True,
    )
    blobs: list[bytes] = []
    try:
        for y in years:
            url = _resolve_csv_url(y, client=client)
            logger.info("fetch_storm_events_db: resolved URL=%s", url)
            gz_bytes = _download_csv_gz(url, client=client)
            logger.info(
                "fetch_storm_events_db: downloaded %d gzip bytes for year=%d",
                len(gz_bytes),
                y,
            )
            blobs.append(gz_bytes)
    finally:
        client.close()
    return _parse_filter_and_serialize(
        blobs if len(blobs) > 1 else blobs[0],
        state,
        event_types,
        bbox=bbox,
        begin_date=begin_date,
        end_date=end_date,
    )


# ---------------------------------------------------------------------------
# Registered atomic tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    # Annotations: readOnlyHint=True (read-only; no state mutation),
    # openWorldHint=True (calls external public API endpoint),
    # destructiveHint=False, idempotentHint=True (cache shim deduplicates).
    open_world_hint=True,
)
def fetch_storm_events_db(
    year: int,
    state: str | None = None,
    event_types: list[str] | None = None,
    bbox: tuple[float, float, float, float] | list[float] | None = None,
    begin_date: str | None = None,
    end_date: str | None = None,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch historical NOAA Storm Events Database records as a FlatGeobuf point layer.

    **What it does:** Downloads the annual NOAA Storm Events DB gzip CSV for a
    given year from NCEI (``ncei.noaa.gov``), filters by state, event type, an
    optional spatial ``bbox``, and an optional ``begin_date``/``end_date``
    window, geocodes each row at ``BEGIN_LAT``/``BEGIN_LON``, and writes a
    FlatGeobuf Point layer. The DB is the authoritative US storm-event catalog
    covering 40+ categories (tornado, hurricane, hail, flood, winter storm, …)
    from 1950 to present. Tier-1 free, no API key. Cached ``static-30d``.

    **When to use:**
    - Agent needs historical storm-event locations for spatial context — e.g.
      "what flood events affected Lee County FL in 2022?"
    - User asks for storm events inside a specific map view / AOI (pass ``bbox``)
      or within a date range (pass ``begin_date``/``end_date``).
    - Workflow requires comparing past event locations against a modeled hazard
      footprint or a current NWS alert.
    - User asks for storm frequency, damage summaries, or narrative context for
      a specific year and region.
    - Providing historical baseline to accompany a real-time ``fetch_nws_event``
      or ``fetch_nws_alerts_conus`` result.

    **When NOT to use:**
    - Real-time or current storm tracking (use ``fetch_nws_event`` for active
      NWS alerts; NHC ATCF tracks are not in scope for v0.1).
    - Parcel-level damage loss data (the DB carries summary strings only; use
      Pelicun post-processor for modeled loss).
    - Non-US meteorology (Storm Events is US + territories only).
    - Sub-annual temporal resolution (the DB records are per-event, not
      gridded time series; use MRMS or NWP output for gridded precipitation).

    **Parameters:**
    - ``year`` (int): calendar year in range [1950, 2100]. Coverage is sparse
      before ~1996 and comprehensive from that year onward. Acts as the anchor
      when no window is given. Example: ``2022``.
    - ``state`` (str or None): US state — either a full name (``"Oklahoma"``,
      ``"Florida"``) OR an ISO 2-letter code (``"OK"``, ``"FL"``).
      Case-insensitive; ``None`` returns all states/territories.
    - ``event_types`` (list[str] or None): list of NOAA event-type name strings,
      case-insensitive (e.g. ``["Hurricane", "Flash Flood"]``, ``["Tornado"]``).
      ``None`` returns all categories.
    - ``bbox`` (tuple or None): optional spatial filter ``(west, south, east,
      north)`` in WGS84 degrees; keeps only events whose begin point lies inside
      the box. A bbox spans state lines (it is spatial, not administrative).
    - ``begin_date`` / ``end_date`` (str or None): optional inclusive temporal
      window in ISO ``YYYY-MM-DD`` (or ``YYYY-MM-DDTHH:MM:SS``) form. The window
      may cross a year boundary - every annual CSV it touches is fetched.

    **Returns:**
    ``LayerURI(layer_type="vector", role="context", units=None)`` pointing at a
    FlatGeobuf with fields: ``EVENT_ID``, ``EVENT_TYPE``, ``STATE``,
    ``BEGIN_DATE_TIME``, ``END_DATE_TIME``, ``INJURIES_DIRECT``,
    ``DEATHS_DIRECT``, ``DEATHS_INDIRECT``, ``DAMAGE_PROPERTY``, ``MAGNITUDE``,
    ``EPISODE_NARRATIVE``. One point per event at ``BEGIN_LAT``/``BEGIN_LON``,
    EPSG:4326.

    **Cross-tool dependencies:**
    - Pairs with: ``fetch_nws_event`` / ``fetch_nws_alerts_conus`` (historical
      baseline alongside current active alerts).
    - Upstream of: ``compute_zonal_statistics`` (count events inside a polygon),
      narrative hazard-impact summaries.
    - Complements: ``fetch_dem``, ``fetch_river_geometry`` for flood context.
    """
    _validate_inputs(year, state, event_types, bbox, begin_date, end_date)

    # Normalize for cache-key stability: collapse "OK"/"Oklahoma"/"oklahoma"
    # to one canonical NOAA full-name spelling so they share a cache entry;
    # event_types sorted-upper; bbox rounded to 6 decimals (~0.1 m) as a tuple.
    state_norm = _normalize_state(state)
    event_types_norm = (
        sorted({e.upper() for e in event_types}) if event_types else None
    )
    bbox_norm = (
        tuple(round(float(v), 6) for v in bbox) if bbox is not None else None
    )

    params: dict[str, Any] = {
        "year": year,
        "state": state_norm,
        "event_types": event_types_norm,
        "bbox": list(bbox_norm) if bbox_norm is not None else None,
        "begin_date": begin_date,
        "end_date": end_date,
    }

    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=lambda: _fetch_storm_events_bytes(
            year, state, event_types,
            bbox=bbox, begin_date=begin_date, end_date=end_date,
        ),
    )
    assert result.uri is not None, (
        "fetch_storm_events_db is cacheable; uri must be set by read_through"
    )

    # Build a human-friendly layer name reflecting the filter.
    filter_bits: list[str] = []
    if state_norm:
        filter_bits.append(state_norm)
    if event_types_norm:
        filter_bits.append(", ".join(event_types_norm[:3]))
    if bbox_norm is not None:
        filter_bits.append("bbox")
    if begin_date is not None or end_date is not None:
        filter_bits.append(f"{begin_date or '..'}..{end_date or '..'}")
    filter_str = f" — {' / '.join(filter_bits)}" if filter_bits else ""

    # layer_id seed: short content hash of the full filter set - stable.
    seed_payload = json.dumps(
        {
            "y": year,
            "s": state_norm,
            "e": event_types_norm,
            "b": list(bbox_norm) if bbox_norm is not None else None,
            "bd": begin_date,
            "ed": end_date,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    layer_seed = hashlib.sha256(seed_payload.encode("utf-8")).hexdigest()[:8]

    return LayerURI(
        layer_id=f"storm-events-{year}-{layer_seed}",
        name=f"NOAA Storm Events {year}{filter_str}",
        layer_type="vector",
        uri=result.uri,
        style_preset="storm_events",
        role="context",
        units=None,
    )
