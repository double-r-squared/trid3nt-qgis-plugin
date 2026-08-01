"""storm_events_db hooks (chained-resolution mode, ADR 0064): NOAA Storm Events DB
bulk-gzip-CSV behind an HTML directory index.

The bulk-file-behind-an-index shape retired here reuses the EXISTING resolve phase
(ADR 0063) -- no new machinery. The index-scrape is the PHASE-R resolve: the router
GETs the NCEI directory listing (``resolve_build``), and ``resolve_parse`` regex-scrapes
it for the window's year(s), picks the newest processed-date file per year, and merges
the resolved bulk-CSV URL(s) into ``params`` (pure regex over a router-fetched body,
exactly like a JSON resolve_parse). The main fetch then GETs each gzip CSV
(``build_request``), and ``parse_response`` decompresses, parses, filters, and
synthesizes points. All I/O (the index GET, the bulk downloads, retry, cache, FGB
serialize) stays router-owned; these hooks only compute.
"""

from __future__ import annotations

import datetime as _dt
import gzip
import io
import math
import re
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from .. import hooks as _hooks
from ..errors import router_empty_error, router_input_error, router_upstream_error

__all__ = ["resolve_build", "resolve_parse", "build_request", "parse_response"]

_INDEX_URL = "https://www.ncei.noaa.gov/pub/data/swdi/stormevents/csvfiles/"
_FILE_RE = re.compile(r"StormEvents_details-ftp_v1\.0_d(\d{4})_c(\d{8})\.csv\.gz")

_RETAINED_COLUMNS = (
    "EVENT_ID", "EVENT_TYPE", "STATE", "BEGIN_DATE_TIME", "END_DATE_TIME",
    "INJURIES_DIRECT", "DEATHS_DIRECT", "DEATHS_INDIRECT", "DAMAGE_PROPERTY",
    "MAGNITUDE", "EPISODE_NARRATIVE",
)

#: ISO 2-letter -> NOAA full state name (the STATE column spelling, uppercase).
_ISO_TO_STATE_NAME: dict[str, str] = {
    "AL": "ALABAMA", "AK": "ALASKA", "AZ": "ARIZONA", "AR": "ARKANSAS",
    "CA": "CALIFORNIA", "CO": "COLORADO", "CT": "CONNECTICUT", "DE": "DELAWARE",
    "DC": "DISTRICT OF COLUMBIA", "FL": "FLORIDA", "GA": "GEORGIA", "HI": "HAWAII",
    "ID": "IDAHO", "IL": "ILLINOIS", "IN": "INDIANA", "IA": "IOWA", "KS": "KANSAS",
    "KY": "KENTUCKY", "LA": "LOUISIANA", "ME": "MAINE", "MD": "MARYLAND",
    "MA": "MASSACHUSETTS", "MI": "MICHIGAN", "MN": "MINNESOTA", "MS": "MISSISSIPPI",
    "MO": "MISSOURI", "MT": "MONTANA", "NE": "NEBRASKA", "NV": "NEVADA",
    "NH": "NEW HAMPSHIRE", "NJ": "NEW JERSEY", "NM": "NEW MEXICO", "NY": "NEW YORK",
    "NC": "NORTH CAROLINA", "ND": "NORTH DAKOTA", "OH": "OHIO", "OK": "OKLAHOMA",
    "OR": "OREGON", "PA": "PENNSYLVANIA", "RI": "RHODE ISLAND", "SC": "SOUTH CAROLINA",
    "SD": "SOUTH DAKOTA", "TN": "TENNESSEE", "TX": "TEXAS", "UT": "UTAH",
    "VT": "VERMONT", "VA": "VIRGINIA", "WA": "WASHINGTON", "WV": "WEST VIRGINIA",
    "WI": "WISCONSIN", "WY": "WYOMING", "PR": "PUERTO RICO", "VI": "VIRGIN ISLANDS",
    "GU": "GUAM", "AS": "AMERICAN SAMOA", "MP": "NORTHERN MARIANA ISLANDS",
}
_STATE_NAME_TO_ISO: dict[str, str] = {name: iso for iso, name in _ISO_TO_STATE_NAME.items()}


def _headers(spec: SourceSpec) -> dict[str, str]:
    return {"User-Agent": spec.auth.user_agent}


def _normalize_state(state: str | None) -> str | None:
    if state is None:
        return None
    token = state.strip().upper()
    if token in _ISO_TO_STATE_NAME:
        return _ISO_TO_STATE_NAME[token]
    return token


def _parse_window_arg(sc: str, value: str, label: str) -> _dt.datetime:
    if not isinstance(value, str) or not value.strip():
        raise router_input_error(sc, f"{label} must be a non-empty ISO date string (YYYY-MM-DD), got {value!r}", "ARG_INVALID")
    raw = value.strip().replace("Z", "")
    try:
        dt = _dt.datetime.fromisoformat(raw)
    except ValueError as exc:
        raise router_input_error(sc, f"{label}={value!r} is not ISO YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS: {exc}", "ARG_INVALID")
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt


def _window_years(sc: str, year: int, begin_date: str | None, end_date: str | None) -> list[int]:
    years = {year}
    b = _parse_window_arg(sc, begin_date, "begin_date") if begin_date is not None else None
    e = _parse_window_arg(sc, end_date, "end_date") if end_date is not None else None
    if b is not None or e is not None:
        lo = (b or e).year
        hi = (e or b).year
        if lo > hi:
            lo, hi = hi, lo
        years.update(range(lo, hi + 1))
    return sorted(years)


def _validate(spec: SourceSpec, params: dict[str, Any]) -> None:
    """Bespoke pre-fetch validation the declarative param surface cannot express."""
    sc = spec.error_code_prefix
    state = params.get("state")
    if state is not None:
        if not isinstance(state, str) or not state.strip():
            raise router_input_error(sc, f"state must be a non-empty US state name or ISO 2-letter code (e.g. 'Oklahoma' or 'OK'), got {state!r}", "ARG_INVALID")
        token = state.strip().upper()
        if token not in _ISO_TO_STATE_NAME and token not in _STATE_NAME_TO_ISO:
            raise router_input_error(sc, f"unrecognized US state {state!r}; expected a state name (e.g. 'Oklahoma') or ISO 2-letter code (e.g. 'OK')", "ARG_INVALID")
    begin_date = params.get("begin_date")
    end_date = params.get("end_date")
    b = _parse_window_arg(sc, begin_date, "begin_date") if begin_date is not None else None
    e = _parse_window_arg(sc, end_date, "end_date") if end_date is not None else None
    if b is not None and e is not None and b > e:
        raise router_input_error(sc, f"begin_date {begin_date!r} is after end_date {end_date!r}", "ARG_INVALID")


# --------------------------------------------------------------------------- #
# PHASE R -- directory-index resolve (index -> newest bulk-CSV URL per year).
# --------------------------------------------------------------------------- #


@_hooks.register_hook("storm_events_db.resolve_build")
def resolve_build(spec: SourceSpec, params: dict[str, Any]) -> list["_hooks.RequestPlan"]:
    """Validate inputs, then GET the NCEI directory index once (year-independent)."""
    _validate(spec, params)
    return [_hooks.RequestPlan(url=_INDEX_URL, headers=_headers(spec))]


@_hooks.register_hook("storm_events_db.resolve_parse")
def resolve_parse(spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]) -> dict[str, Any]:
    """Regex-scrape the index for the window's year(s), pick the newest file per year."""
    sc = spec.error_code_prefix
    try:
        text = bodies[0].decode("utf-8", errors="replace")
    except (IndexError, AttributeError) as exc:
        raise router_upstream_error(sc, f"NOAA Storm Events index unreadable: {exc}")
    years = _window_years(sc, int(params["year"]), params.get("begin_date"), params.get("end_date"))
    by_year: dict[int, list[tuple[str, str]]] = {}
    for m in _FILE_RE.finditer(text):
        fy, cdate = int(m.group(1)), m.group(2)
        by_year.setdefault(fy, []).append((cdate, m.group(0)))
    urls: list[str] = []
    for y in years:
        cands = by_year.get(y)
        if not cands:
            raise router_upstream_error(sc, f"no NOAA Storm Events CSV found for year={y} in {_INDEX_URL}")
        cands.sort(reverse=True)  # newest processed-date = canonical
        urls.append(_INDEX_URL + cands[0][1])
    return {"_csv_urls": urls}


# --------------------------------------------------------------------------- #
# MAIN FETCH -- GET each resolved bulk gzip CSV.
# --------------------------------------------------------------------------- #


@_hooks.register_hook("storm_events_db.build_request")
def build_request(spec: SourceSpec, params: dict[str, Any]) -> list["_hooks.RequestPlan"]:
    """GET each resolved bulk-CSV URL (one per annual file the window touches)."""
    urls = params.get("_csv_urls") or []
    return [_hooks.RequestPlan(url=u, headers=_headers(spec)) for u in urls]


# --------------------------------------------------------------------------- #
# PARSE -- decompress + filter + synthesize points.
# --------------------------------------------------------------------------- #


def _derive_begin_datetime(df: Any, pd: Any) -> Any:
    if "BEGIN_YEARMONTH" in df.columns and "BEGIN_DAY" in df.columns:
        ym = pd.to_numeric(df["BEGIN_YEARMONTH"], errors="coerce")
        day = pd.to_numeric(df["BEGIN_DAY"], errors="coerce")
        parts = pd.DataFrame({"year": ym // 100, "month": ym % 100, "day": day})
        base = pd.to_datetime(parts, errors="coerce")
        if "BEGIN_TIME" in df.columns:
            t = pd.to_numeric(df["BEGIN_TIME"], errors="coerce").fillna(0)
            base = base + pd.to_timedelta((t // 100) * 60 + (t % 100), unit="m")
        return base
    if "BEGIN_DATE_TIME" in df.columns:
        return pd.to_datetime(df["BEGIN_DATE_TIME"], format="%d-%b-%y %H:%M:%S", errors="coerce")
    return pd.Series(pd.NaT, index=df.index)


@_hooks.register_hook("storm_events_db.parse_response")
def parse_response(spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]) -> list[dict[str, Any]]:
    """Decompress each gzip CSV, concat, filter (state/event_types/bbox/window), synthesize points."""
    sc = spec.error_code_prefix
    import pandas as pd  # type: ignore[import-not-found]

    state = params.get("state")
    event_types = params.get("event_types")
    bbox = params.get("bbox")
    begin_date = params.get("begin_date")
    end_date = params.get("end_date")

    frames = []
    for blob in bodies:
        try:
            csv_text = gzip.decompress(blob).decode("utf-8", errors="replace")
        except (OSError, EOFError) as exc:
            raise router_upstream_error(sc, f"NOAA Storm Events gzip is corrupt: {exc}")
        try:
            frame = pd.read_csv(io.StringIO(csv_text), dtype=str, low_memory=False, keep_default_na=False, na_values=[""])
        except pd.errors.ParserError as exc:
            raise router_upstream_error(sc, f"NOAA Storm Events CSV parse failed: {exc}")
        frames.append(frame)

    df = pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0]
    required = {"BEGIN_LAT", "BEGIN_LON", "STATE", "EVENT_TYPE"}
    missing = required - set(df.columns)
    if missing:
        raise router_upstream_error(sc, f"NOAA Storm Events CSV missing required columns: {sorted(missing)}; got {sorted(df.columns)[:10]}...")

    if state is not None:
        df = df[df["STATE"].str.upper() == _normalize_state(state)].copy()
    if event_types:
        wanted = {e.upper() for e in event_types}
        df = df[df["EVENT_TYPE"].str.upper().isin(wanted)].copy()
    if begin_date is not None or end_date is not None:
        begin_dt = _derive_begin_datetime(df, pd)
        keep = pd.Series(True, index=df.index)
        if begin_date is not None:
            keep &= begin_dt >= pd.Timestamp(_parse_window_arg(sc, begin_date, "begin_date"))
        if end_date is not None:
            hi = _parse_window_arg(sc, end_date, "end_date")
            if hi.hour == 0 and hi.minute == 0 and hi.second == 0 and len(str(end_date).strip()) <= 10:
                hi = hi + _dt.timedelta(days=1) - _dt.timedelta(seconds=1)
            keep &= begin_dt <= pd.Timestamp(hi)
        keep &= begin_dt.notna()
        df = df[keep].copy()

    df["BEGIN_LAT"] = pd.to_numeric(df["BEGIN_LAT"], errors="coerce")
    df["BEGIN_LON"] = pd.to_numeric(df["BEGIN_LON"], errors="coerce")
    df = df.dropna(subset=["BEGIN_LAT", "BEGIN_LON"]).copy()
    df = df[(df["BEGIN_LAT"].between(-90.0, 90.0)) & (df["BEGIN_LON"].between(-180.0, 180.0))].copy()
    if bbox is not None:
        west, south, east, north = (float(v) for v in bbox)
        df = df[df["BEGIN_LON"].between(west, east) & df["BEGIN_LAT"].between(south, north)].copy()

    if df.empty:
        raise router_empty_error(
            sc,
            f"no NOAA Storm Events match state={state!r} event_types={event_types!r} "
            f"bbox={bbox!r} begin_date={begin_date!r} end_date={end_date!r} after filtering",
        )

    keep_cols = [c for c in _RETAINED_COLUMNS if c in df.columns]
    lons = df["BEGIN_LON"].tolist()
    lats = df["BEGIN_LAT"].tolist()
    col_vals = {c: df[c].tolist() for c in keep_cols}
    out: list[dict[str, Any]] = []
    for i in range(len(df)):
        lon, lat = float(lons[i]), float(lats[i])
        if not (math.isfinite(lon) and math.isfinite(lat)):
            continue
        out.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {c: col_vals[c][i] for c in keep_cols},
        })
    return out
