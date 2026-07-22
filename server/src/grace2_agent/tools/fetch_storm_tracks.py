"""``fetch_storm_tracks`` atomic tool - hurricane / tropical-cyclone tracks.

Two data modes behind one tool:

**HISTORICAL (default, ``active_only=False``)** - IBTrACS v04r01 (NOAA NCEI
International Best Track Archive for Climate Stewardship), the authoritative
merged best-track record of every tropical cyclone since 1842. We download the
points CSV, subset by bbox + season (year) range + optional storm name, and
emit either one LineString per storm (``geometry="lines"``, default) or one
Point per 3/6-hourly best-track fix (``geometry="points"``), carrying wind
speed (kt), central pressure (mb), and the Saffir-Simpson category.

    CSV base (free, NO API key):
        https://www.ncei.noaa.gov/data/international-best-track-archive-for-climate-stewardship-ibtracs/v04r01/access/csv/

    File selection (bounded-download discipline; verified sizes 2026-07-07):
        - ``ibtracs.last3years.list.v04r01.csv`` (~9 MB) when the requested
          year range is within the most recent 3 seasons (the default).
        - per-basin ``ibtracs.<BASIN>.list.v04r01.csv`` (~5-60 MB) otherwise,
          basins derived from the bbox (NA/EP/WP/NI/SI/SP/SA). A bbox spanning
          more than 2 basins raises a typed input error - we never pull the
          330 MB ``ALL`` file.

    CSV shape: row 0 = column names, row 1 = units, then one row per fix.
    Key columns: SID, SEASON, BASIN, NAME, ISO_TIME, NATURE, LAT, LON,
    WMO_WIND/WMO_PRES, TRACK_TYPE (skip ``spur`` duplicates), USA_STATUS,
    USA_WIND/USA_PRES (preferred wind/pressure), USA_SSHS (Saffir-Simpson
    category, -5..5). Missing values are blank/whitespace strings.

    Track selection is storm-wise: a storm whose track touches the bbox is
    returned with its FULL track (not clipped), so landfalling storms keep
    their open-ocean history for context.

**ACTIVE (``active_only=True``)** - NHC ``CurrentStorms.json`` (the machine
feed behind the NHC "Active Cyclones" page) for storms being advised on RIGHT
NOW, enriched best-effort with each storm's official 5-day forecast-track GIS
points (the ``forecastTrack`` zipped shapefile from https://www.nhc.noaa.gov/gis/).
Active mode always emits POINTS: the current position (``tau=0``) plus the
forecast positions (``tau`` = forecast hour) with ``max_wind_kt`` per point.
If the forecast-track zip cannot be fetched/parsed the tool degrades to
current-position points only (logged, never fabricated).

    https://www.nhc.noaa.gov/CurrentStorms.json
        -> {"activeStorms": [{"id", "name", "classification", "intensity"(kt),
            "pressure"(mb), "latitudeNumeric", "longitudeNumeric",
            "movementDir", "movementSpeed", "lastUpdate",
            "forecastTrack": {"zipFile": ...}, ...}]}

**Honest-empty paths** (data-source fallback norm - primary -> honest typed
error, never an empty success-shaped layer):
    - historical: no storm track touches the bbox/window/name filter ->
      ``StormTracksNoStormsError``.
    - active: NHC is advising on zero storms (the common out-of-season state;
      live-verified 2026-07-07) or none match the bbox/name filter ->
      ``StormTracksNoActiveStormsError``.

Output: vector ``LayerURI`` (FlatGeobuf, EPSG:4326),
``style_preset="storm_tracks"``, ``role="primary"``. ``LayerURI.bbox`` is the
extent of the returned tracks so the camera frames the storms.

Tier-1, no auth. ``supports_global_query=False`` for historical (bbox is
REQUIRED to bound the subset); ``active_only=True`` accepts a missing bbox
(the active set is globally small).

FR-AS-11 typed-error surface; FR-TA-2 / FR-AS-3 docstring discipline applies.
"""

from __future__ import annotations

import csv
import datetime as _dt
import hashlib
import io
import json
import logging
import math
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from typing import Any

from grace2_contracts.execution import LayerURI
from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import read_through

__all__ = [
    "fetch_storm_tracks",
    "estimate_payload_mb",
    "StormTracksError",
    "StormTracksInputError",
    "StormTracksUpstreamError",
    "StormTracksNoStormsError",
    "StormTracksNoActiveStormsError",
    "IBTRACS_CSV_BASE",
    "NHC_CURRENT_STORMS_URL",
    "_validate_bbox",
    "_resolve_years",
    "_select_ibtracs_files",
    "_parse_ibtracs_csv",
    "_select_storms_in_bbox",
    "_saffir_label",
    "_parse_current_storms",
    "_records_bbox",
    "_build_line_flatgeobuf",
    "_build_point_flatgeobuf",
]

logger = logging.getLogger("grace2_agent.tools.fetch_storm_tracks")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class StormTracksError(RuntimeError):
    """Base class for fetch_storm_tracks failures.

    ``error_code`` maps to the WebSocket A.6 error frame emitted by the agent
    surface. ``retryable`` guides FR-AS-11 retry/clarify/fallback logic.
    """

    error_code: str = "STORM_TRACKS_ERROR"
    retryable: bool = True


class StormTracksInputError(StormTracksError):
    """Invalid inputs - bad bbox, bad year range, bad geometry mode.

    Not retryable: the caller must fix the argument.
    """

    error_code = "STORM_TRACKS_INPUT_ERROR"
    retryable = False


class StormTracksUpstreamError(StormTracksError):
    """NCEI / NHC request failed (network error, HTTP 5xx, bad body).

    Retryable - transient upstream outages recover on retry.
    """

    error_code = "STORM_TRACKS_UPSTREAM_ERROR"
    retryable = True


class StormTracksNoStormsError(StormTracksError):
    """No historical storm track touched the bbox / year range / name filter.

    Not retryable - the archive genuinely has no matching track. Widen the
    bbox, extend the year range, or drop the name filter.
    """

    error_code = "STORM_TRACKS_NO_STORMS"
    retryable = False


class StormTracksNoActiveStormsError(StormTracksError):
    """NHC is advising on zero active storms (or none match the filter).

    Not retryable - a quiet basin is the common steady state outside peak
    season. Use the historical mode (``active_only=False``) for past storms.
    """

    error_code = "STORM_TRACKS_NO_ACTIVE_STORMS"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: IBTrACS v04r01 points-CSV base URL (NOAA NCEI).
IBTRACS_CSV_BASE = (
    "https://www.ncei.noaa.gov/data/"
    "international-best-track-archive-for-climate-stewardship-ibtracs/"
    "v04r01/access/csv/"
)

#: NHC active-storms machine feed.
NHC_CURRENT_STORMS_URL = "https://www.nhc.noaa.gov/CurrentStorms.json"

#: IBTrACS starts in 1842.
_IBTRACS_FIRST_SEASON = 1842

#: The ``last3years`` file carries the most recent 3 complete seasons plus the
#: current one; ``start_year >= current_year - 2`` is the safe-coverage gate.
_LAST3YEARS_FILE = "ibtracs.last3years.list.v04r01.csv"

#: Approximate basin envelopes (west, south, east, north) used ONLY to pick
#: which per-basin CSV file(s) to download - generous on purpose (extratropical
#: transitions reach high latitudes). SP crosses the antimeridian so it has two
#: envelopes.
_BASIN_ENVELOPES: dict[str, list[tuple[float, float, float, float]]] = {
    "NA": [(-103.0, 0.0, 10.0, 70.0)],
    # EP stops at the Central-American divide: open Pacific west of -92, plus
    # the lower-latitude Pacific coast strip down to Panama (never the Gulf of
    # Mexico / Caribbean, which are NA).
    "EP": [(-180.0, 0.0, -92.0, 60.0), (-92.0, 0.0, -77.0, 15.0)],
    "WP": [(95.0, 0.0, 180.0, 65.0)],
    "NI": [(30.0, 0.0, 100.0, 35.0)],
    "SI": [(10.0, -55.0, 135.0, 0.0)],
    "SP": [(135.0, -55.0, 180.0, 0.0), (-180.0, -55.0, -60.0, 0.0)],
    "SA": [(-70.0, -55.0, 20.0, 0.0)],
}

#: Never download more than this many per-basin CSVs in one call (each is
#: ~5-60 MB); the 330 MB ALL file is never used.
_MAX_BASIN_FILES = 2

#: User-Agent per NOAA usage guidance (a descriptive UA is recommended).
_USER_AGENT = (
    "grace-2/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/GRACE-2; agent@grace-2.dev)"
)

#: HTTP timeout (seconds). Per-basin IBTrACS CSVs are up to ~60 MB.
_HTTP_TIMEOUT = 300.0

#: Cap on emitted point features (points mode) so a dense multi-decade basin
#: query stays a bounded payload.
_MAX_POINT_FEATURES = 50000


# ---------------------------------------------------------------------------
# AtomicToolMetadata - registered once at import time.
# ---------------------------------------------------------------------------


def _build_metadata() -> AtomicToolMetadata:
    """Construct AtomicToolMetadata defensively against schema flag variants."""
    common: dict[str, Any] = dict(
        name="fetch_storm_tracks",
        ttl_class="dynamic-1h",
        source_class="storm_tracks",
        cacheable=True,
    )
    try:
        return AtomicToolMetadata(  # type: ignore[call-arg]
            **common,
            supports_global_query=False,
            payload_mb_estimator_name="estimate_payload_mb",
        )
    except Exception:
        logger.debug(
            "AtomicToolMetadata does not support all Wave-1.5 flags; "
            "registering fetch_storm_tracks without them"
        )
        return AtomicToolMetadata(**common)


_METADATA = _build_metadata()


# ---------------------------------------------------------------------------
# Payload-MB estimator (Wave 1.5 chat-warning system).
# ---------------------------------------------------------------------------


def estimate_payload_mb(
    bbox: tuple[float, float, float, float] | None = None,
    start_year: int | None = None,
    end_year: int | None = None,
    active_only: bool = False,
    geometry: str = "lines",
    **_kw: Any,
) -> float:
    """Estimate the output FlatGeobuf size in MB.

    Active mode is tiny (a handful of storms x ~20 forecast points). Historical
    output scales with bbox area and year-range length; a per-storm line is
    ~2 KB, a per-fix point ~300 bytes. Estimates are conservative - track
    layers are small next to rasters.
    """
    if active_only:
        return 0.01
    try:
        y0, y1 = _resolve_years(start_year, end_year)
        n_years = max(1, y1 - y0 + 1)
    except Exception:
        n_years = 3
    area_sq_deg = 400.0
    if bbox is not None:
        try:
            west, south, east, north = (float(v) for v in bbox)
            area_sq_deg = max(1.0, (east - west) * (north - south))
        except (TypeError, ValueError):
            pass
    # ~0.005 bbox-touching storms per sq-deg per season in an active basin.
    n_storms = max(1.0, area_sq_deg * n_years * 0.005)
    if geometry == "points":
        return max(0.001, n_storms * 80 * 300 / 1_000_000.0)
    return max(0.001, n_storms * 2000 / 1_000_000.0)


# ---------------------------------------------------------------------------
# Input validation.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Raise ``StormTracksInputError`` if the bbox is malformed / out of range."""
    if not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
        raise StormTracksInputError(
            f"bbox must be (west, south, east, north); got {bbox!r}"
        )
    west, south, east, north = bbox
    if not all(math.isfinite(float(v)) for v in bbox):
        raise StormTracksInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= west <= 180.0 and -180.0 <= east <= 180.0):
        raise StormTracksInputError(f"bbox lon values out of [-180, 180]: {bbox!r}")
    if not (-90.0 <= south <= 90.0 and -90.0 <= north <= 90.0):
        raise StormTracksInputError(f"bbox lat values out of [-90, 90]: {bbox!r}")
    if west >= east or south >= north:
        raise StormTracksInputError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )


def _round_bbox_to_6dp(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    return tuple(round(float(v), 6) for v in bbox)  # type: ignore[return-value]


def _resolve_years(
    start_year: int | None,
    end_year: int | None,
) -> tuple[int, int]:
    """Resolve the (start, end) season range. Default = the last 3 seasons.

    Raises ``StormTracksInputError`` on a non-integer year, a year before the
    IBTrACS record starts (1842), a future year, or a reversed range.
    """
    current = _dt.datetime.now(_dt.timezone.utc).year

    def _coerce(v: Any, label: str) -> int:
        try:
            i = int(v)
        except (TypeError, ValueError) as exc:
            raise StormTracksInputError(
                f"{label} must be an integer year; got {v!r}"
            ) from exc
        if i < _IBTRACS_FIRST_SEASON:
            raise StormTracksInputError(
                f"{label}={i} predates the IBTrACS record "
                f"(starts {_IBTRACS_FIRST_SEASON})"
            )
        if i > current:
            raise StormTracksInputError(
                f"{label}={i} is in the future (current season is {current})"
            )
        return i

    if start_year is None and end_year is None:
        return (current - 2, current)
    if start_year is not None and end_year is None:
        y0 = _coerce(start_year, "start_year")
        return (y0, current)
    if start_year is None and end_year is not None:
        y1 = _coerce(end_year, "end_year")
        return (max(_IBTRACS_FIRST_SEASON, y1 - 2), y1)
    y0 = _coerce(start_year, "start_year")
    y1 = _coerce(end_year, "end_year")
    if y0 > y1:
        raise StormTracksInputError(
            f"start_year must be <= end_year; got {y0}..{y1}"
        )
    return (y0, y1)


# ---------------------------------------------------------------------------
# IBTrACS file selection.
# ---------------------------------------------------------------------------


def _envelopes_intersect(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def _select_ibtracs_files(
    bbox: tuple[float, float, float, float],
    y0: int,
    y1: int,
) -> list[str]:
    """Pick the smallest adequate IBTrACS CSV file set for bbox + year range.

    - Recent-only ranges (start within the last 3 seasons) -> the single
      ~9 MB ``last3years`` file.
    - Older ranges -> the per-basin file(s) whose envelope intersects the
      bbox. More than ``_MAX_BASIN_FILES`` intersecting basins raises a typed
      input error (we never fall back to the 330 MB ALL file).
    - A bbox intersecting NO basin envelope (e.g. polar) raises the honest
      no-storms error immediately - no download needed.
    """
    current = _dt.datetime.now(_dt.timezone.utc).year
    if y0 >= current - 2:
        return [_LAST3YEARS_FILE]

    basins: list[str] = []
    for basin, envs in _BASIN_ENVELOPES.items():
        if any(_envelopes_intersect(bbox, env) for env in envs):
            basins.append(basin)
    if not basins:
        raise StormTracksNoStormsError(
            f"bbox={bbox!r} lies outside every tropical-cyclone basin "
            f"envelope - the IBTrACS archive has no storm tracks there."
        )
    if len(basins) > _MAX_BASIN_FILES:
        raise StormTracksInputError(
            f"bbox={bbox!r} spans {len(basins)} tropical-cyclone basins "
            f"({', '.join(sorted(basins))}); a historical query is limited to "
            f"{_MAX_BASIN_FILES} basins per call. Narrow the bbox or issue "
            f"one call per basin region."
        )
    return [f"ibtracs.{b}.list.v04r01.csv" for b in sorted(basins)]


# ---------------------------------------------------------------------------
# HTTP helper.
# ---------------------------------------------------------------------------


def _http_get(url: str, timeout: float = _HTTP_TIMEOUT) -> bytes:
    """Plain HTTP GET. Raises ``StormTracksUpstreamError`` on failure."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise StormTracksUpstreamError(
            f"Upstream returned HTTP {exc.code} for {url}: {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise StormTracksUpstreamError(
            f"Network error fetching {url}: {exc.reason}"
        ) from exc
    except TimeoutError as exc:
        raise StormTracksUpstreamError(
            f"Timed out after {timeout}s fetching {url}"
        ) from exc


# ---------------------------------------------------------------------------
# IBTrACS CSV parsing.
# ---------------------------------------------------------------------------


def _blank_to_none_float(v: Any) -> float | None:
    """IBTrACS encodes missing numerics as blank/whitespace strings."""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    if not math.isfinite(f):
        return None
    return f


def _blank_to_none_int(v: Any) -> int | None:
    f = _blank_to_none_float(v)
    if f is None:
        return None
    return int(f)


#: USA_SSHS -> human label. -5 = unknown, -4 = post-tropical, -3 = misc
#: disturbance, -2 = subtropical, -1 = tropical depression, 0 = tropical
#: storm, 1..5 = Saffir-Simpson hurricane category.
_SSHS_LABELS = {
    -5: "unknown",
    -4: "post-tropical",
    -3: "disturbance",
    -2: "subtropical",
    -1: "tropical depression",
    0: "tropical storm",
    1: "category 1",
    2: "category 2",
    3: "category 3",
    4: "category 4",
    5: "category 5",
}


def _saffir_label(cat: int | None) -> str:
    """Human label for a USA_SSHS integer category (-5..5)."""
    if cat is None:
        return "unknown"
    return _SSHS_LABELS.get(int(cat), "unknown")


def _parse_ibtracs_csv(
    raw: bytes,
    *,
    y0: int,
    y1: int,
    storm_name: str | None,
) -> dict[str, list[dict[str, Any]]]:
    """Parse an IBTrACS points CSV -> {sid: [fix, ...]} filtered by season + name.

    Row 0 is the column-name header, row 1 is a units row (skipped). ``spur``
    TRACK_TYPE rows (alternate-agency duplicates) are dropped; ``main`` and
    ``PROVISIONAL*`` (current-season) rows are kept. Fixes with an unparseable
    lat/lon are dropped. Wind prefers USA_WIND, falling back to WMO_WIND;
    pressure prefers USA_PRES falling back to WMO_PRES.
    """
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception as exc:  # pragma: no cover - decode(errors=replace) is total
        raise StormTracksUpstreamError(f"IBTrACS CSV decode failed: {exc}") from exc

    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise StormTracksUpstreamError("IBTrACS CSV body is empty") from None
    idx = {name.strip(): i for i, name in enumerate(header)}
    required = ("SID", "SEASON", "BASIN", "NAME", "ISO_TIME", "LAT", "LON")
    missing = [c for c in required if c not in idx]
    if missing:
        raise StormTracksUpstreamError(
            f"IBTrACS CSV is missing expected columns {missing}; "
            f"got header {header[:12]}..."
        )

    def _col(row: list[str], name: str) -> str:
        i = idx.get(name)
        if i is None or i >= len(row):
            return ""
        return row[i]

    name_filter = storm_name.strip().upper() if storm_name else None

    storms: dict[str, list[dict[str, Any]]] = {}
    first_data = True
    for row in reader:
        if not row:
            continue
        # Row 1 is the units row ("' ', 'Year', ...'"); skip it once.
        if first_data:
            first_data = False
            if _col(row, "SEASON").strip().lower() == "year":
                continue
        track_type = _col(row, "TRACK_TYPE").strip().lower()
        if track_type.startswith("spur"):
            continue
        season = _blank_to_none_int(_col(row, "SEASON"))
        if season is None or not (y0 <= season <= y1):
            continue
        name = _col(row, "NAME").strip().upper()
        if name_filter and name != name_filter:
            continue
        lat = _blank_to_none_float(_col(row, "LAT"))
        lon = _blank_to_none_float(_col(row, "LON"))
        if lat is None or lon is None:
            continue
        sid = _col(row, "SID").strip()
        if not sid:
            continue
        wind = _blank_to_none_float(_col(row, "USA_WIND"))
        if wind is None:
            wind = _blank_to_none_float(_col(row, "WMO_WIND"))
        pres = _blank_to_none_float(_col(row, "USA_PRES"))
        if pres is None:
            pres = _blank_to_none_float(_col(row, "WMO_PRES"))
        cat = _blank_to_none_int(_col(row, "USA_SSHS"))
        # SPIDERWEB (2026-07-19): carry the ATCF wind-structure columns per fix so
        # the Holland parametric (workflows/sfincs_spiderweb) can size the RMW +
        # outer pressure + wind-radii. All blank-tolerant (frequently empty for
        # older/weaker fixes -> the spiderweb builder falls back to Knaff-Zehr /
        # standard atmosphere and SURFACES the fallback, never fabricates radii).
        # USA_RMW / USA_ROCI / USA_R34_* are nautical miles; USA_POCI is mb.
        rmw_nmi = _blank_to_none_float(_col(row, "USA_RMW"))
        poci_mb = _blank_to_none_float(_col(row, "USA_POCI"))
        roci_nmi = _blank_to_none_float(_col(row, "USA_ROCI"))
        r34_ne = _blank_to_none_float(_col(row, "USA_R34_NE"))
        r34_se = _blank_to_none_float(_col(row, "USA_R34_SE"))
        r34_sw = _blank_to_none_float(_col(row, "USA_R34_SW"))
        r34_nw = _blank_to_none_float(_col(row, "USA_R34_NW"))
        storms.setdefault(sid, []).append(
            {
                "sid": sid,
                "season": season,
                "basin": _col(row, "BASIN").strip() or None,
                "name": name or None,
                "iso_time": _col(row, "ISO_TIME").strip() or None,
                "nature": _col(row, "NATURE").strip() or None,
                "lat": lat,
                "lon": lon,
                "wind_kt": wind,
                "pres_mb": pres,
                "category": cat,
                "status": _col(row, "USA_STATUS").strip() or None,
                # Wind-structure (spiderweb) columns — blank-tolerant.
                "rmw_nmi": rmw_nmi,
                "poci_mb": poci_mb,
                "roci_nmi": roci_nmi,
                "r34_ne_nmi": r34_ne,
                "r34_se_nmi": r34_se,
                "r34_sw_nmi": r34_sw,
                "r34_nw_nmi": r34_nw,
            }
        )
    return storms


def _select_storms_in_bbox(
    storms: dict[str, list[dict[str, Any]]],
    bbox: tuple[float, float, float, float],
) -> dict[str, list[dict[str, Any]]]:
    """Keep storms whose track has at least one fix inside the bbox.

    Selection is storm-wise: a selected storm keeps its FULL track (not
    clipped to the bbox) so landfall context is preserved.
    """
    west, south, east, north = bbox
    out: dict[str, list[dict[str, Any]]] = {}
    for sid, fixes in storms.items():
        if any(
            west <= f["lon"] <= east and south <= f["lat"] <= north
            for f in fixes
        ):
            out[sid] = sorted(fixes, key=lambda f: f["iso_time"] or "")
    return out


# ---------------------------------------------------------------------------
# NHC active-storms parsing.
# ---------------------------------------------------------------------------


def _parse_signed_coord(v: Any) -> float | None:
    """Parse ``14.8N`` / ``52.9W`` hemisphere-suffixed coordinate strings."""
    if v is None:
        return None
    s = str(v).strip().upper()
    if not s:
        return None
    sign = 1.0
    if s[-1] in ("N", "S", "E", "W"):
        if s[-1] in ("S", "W"):
            sign = -1.0
        s = s[:-1]
    try:
        f = float(s)
    except ValueError:
        return None
    return sign * f if math.isfinite(f) else None


def _parse_current_storms(raw: bytes) -> list[dict[str, Any]]:
    """Parse NHC CurrentStorms.json -> one record per active storm.

    Prefers the ``latitudeNumeric`` / ``longitudeNumeric`` fields, falling back
    to parsing the hemisphere-suffixed ``latitude`` / ``longitude`` strings.
    Storms with no parseable position are dropped (logged).
    """
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise StormTracksUpstreamError(
            f"NHC CurrentStorms.json is not valid JSON: {exc}"
        ) from exc
    if not isinstance(obj, dict):
        raise StormTracksUpstreamError(
            f"NHC CurrentStorms.json is not a JSON object: "
            f"type={type(obj).__name__}"
        )
    storms_raw = obj.get("activeStorms")
    if storms_raw is None:
        raise StormTracksUpstreamError(
            "NHC CurrentStorms.json has no 'activeStorms' key - "
            "the feed schema may have changed"
        )

    records: list[dict[str, Any]] = []
    for s in storms_raw or []:
        if not isinstance(s, dict):
            continue
        lat = s.get("latitudeNumeric")
        lon = s.get("longitudeNumeric")
        lat = float(lat) if isinstance(lat, (int, float)) else _parse_signed_coord(
            s.get("latitude")
        )
        lon = float(lon) if isinstance(lon, (int, float)) else _parse_signed_coord(
            s.get("longitude")
        )
        if lat is None or lon is None:
            logger.warning(
                "fetch_storm_tracks: active storm %r has no parseable position; "
                "dropped",
                s.get("id") or s.get("name"),
            )
            continue
        fc_track = s.get("forecastTrack") or {}
        records.append(
            {
                "id": str(s.get("id") or "").strip() or None,
                "name": str(s.get("name") or "").strip() or None,
                "classification": str(s.get("classification") or "").strip()
                or None,
                "intensity_kt": _blank_to_none_float(s.get("intensity")),
                "pressure_mb": _blank_to_none_float(s.get("pressure")),
                "lat": lat,
                "lon": lon,
                "movement_dir_deg": _blank_to_none_float(s.get("movementDir")),
                "movement_speed_kt": _blank_to_none_float(s.get("movementSpeed")),
                "last_update": str(s.get("lastUpdate") or "").strip() or None,
                "forecast_track_zip": (
                    str(fc_track.get("zipFile") or "").strip() or None
                    if isinstance(fc_track, dict)
                    else None
                ),
            }
        )
    return records


def _fetch_forecast_track_points(
    zip_url: str,
    storm: dict[str, Any],
) -> list[dict[str, Any]]:
    """Best-effort: NHC 5-day forecast-track zipped shapefile -> point records.

    Downloads the ``forecastTrack`` zip, extracts the ``*_pts.shp`` layer, and
    emits one record per forecast position carrying ``tau`` (forecast hour)
    and ``max_wind_kt`` where the shapefile provides them (field names vary
    across NHC product generations, so lookups are case-insensitive and each
    field is optional). Any failure returns ``[]`` - the caller degrades to
    current-position-only and logs; we never fabricate a forecast.
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
    except ImportError:
        logger.warning(
            "fetch_storm_tracks: geopandas unavailable; skipping forecast track"
        )
        return []

    tmpdir: tempfile.TemporaryDirectory[str] | None = None
    try:
        raw = _http_get(zip_url)
        tmpdir = tempfile.TemporaryDirectory(prefix="grace2_nhc_")
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            zf.extractall(tmpdir.name)
        pts_shp = None
        for root, _dirs, files in os.walk(tmpdir.name):
            for fn in files:
                if fn.lower().endswith("_pts.shp"):
                    pts_shp = os.path.join(root, fn)
                    break
        if pts_shp is None:
            logger.warning(
                "fetch_storm_tracks: no *_pts.shp in forecast-track zip %s",
                zip_url,
            )
            return []
        gdf = gpd.read_file(pts_shp)
        if gdf.crs is not None:
            gdf = gdf.to_crs("EPSG:4326")
        cols = {c.lower(): c for c in gdf.columns}

        def _field(row: Any, *names: str) -> Any:
            for n in names:
                c = cols.get(n)
                if c is not None:
                    return row[c]
            return None

        out: list[dict[str, Any]] = []
        for _, row in gdf.iterrows():
            geom = row.geometry
            if geom is None or geom.is_empty or geom.geom_type != "Point":
                continue
            tau = _blank_to_none_float(_field(row, "tau", "fhour"))
            out.append(
                {
                    "id": storm.get("id"),
                    "name": storm.get("name"),
                    "classification": (
                        str(_field(row, "tcdvlp", "stormtype", "dvlbl") or "").strip()
                        or None
                    ),
                    "intensity_kt": _blank_to_none_float(
                        _field(row, "maxwind", "vmax")
                    ),
                    "pressure_mb": _blank_to_none_float(_field(row, "mslp")),
                    "lat": float(geom.y),
                    "lon": float(geom.x),
                    "movement_dir_deg": None,
                    "movement_speed_kt": None,
                    "last_update": (
                        str(_field(row, "fldatelbl", "validtime", "datelbl") or "")
                        .strip()
                        or None
                    ),
                    "tau_h": tau,
                }
            )
        return out
    except StormTracksUpstreamError as exc:
        logger.warning(
            "fetch_storm_tracks: forecast-track fetch failed (%s); degrading to "
            "current position only",
            exc,
        )
        return []
    except Exception as exc:  # noqa: BLE001 - best-effort enrichment boundary
        logger.warning(
            "fetch_storm_tracks: forecast-track parse failed for %s (%s); "
            "degrading to current position only",
            zip_url,
            exc,
        )
        return []
    finally:
        if tmpdir is not None:
            tmpdir.cleanup()


# ---------------------------------------------------------------------------
# Extent + FlatGeobuf builders.
# ---------------------------------------------------------------------------


def _records_bbox(
    records: list[dict[str, Any]],
) -> tuple[float, float, float, float] | None:
    """(west, south, east, north) extent of point records; pads degenerate."""
    if not records:
        return None
    lons = [r["lon"] for r in records]
    lats = [r["lat"] for r in records]
    west, east = min(lons), max(lons)
    south, north = min(lats), max(lats)
    if west == east:
        west -= 0.5
        east += 0.5
    if south == north:
        south -= 0.5
        north += 0.5
    return (west, south, east, north)


def _import_gpd() -> Any:
    try:
        import geopandas as gpd  # type: ignore[import-not-found]

        return gpd
    except ImportError as exc:
        raise StormTracksUpstreamError(
            f"geopandas not available for FlatGeobuf conversion: {exc}"
        ) from exc


def _write_fgb(gdf: Any, n_label: str) -> bytes:
    tmp_fgb: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="grace2_storm_"
        ) as f:
            tmp_fgb = f.name
        gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        with open(tmp_fgb, "rb") as f:
            return f.read()
    except Exception as exc:
        raise StormTracksUpstreamError(
            f"FlatGeobuf write failed for {n_label}: {exc}"
        ) from exc
    finally:
        if tmp_fgb is not None:
            try:
                os.unlink(tmp_fgb)
            except OSError:
                pass


def _build_line_flatgeobuf(
    storms: dict[str, list[dict[str, Any]]],
) -> bytes:
    """One LineString per storm (fixes in time order) -> FlatGeobuf bytes.

    Single-fix storms cannot form a line and are dropped (logged); the caller
    raises the honest no-storms error if nothing remains. Per-line props:
    sid, name, season, basin, max_wind_kt, min_pres_mb, max_category,
    max_category_label, start_time, end_time, n_fixes.
    """
    gpd = _import_gpd()
    from shapely.geometry import LineString  # type: ignore[import-not-found]

    geoms = []
    rows: list[dict[str, Any]] = []
    n_dropped = 0
    for sid, fixes in sorted(storms.items()):
        if len(fixes) < 2:
            n_dropped += 1
            continue
        winds = [f["wind_kt"] for f in fixes if f["wind_kt"] is not None]
        press = [f["pres_mb"] for f in fixes if f["pres_mb"] is not None]
        cats = [f["category"] for f in fixes if f["category"] is not None]
        max_cat = max(cats) if cats else None
        geoms.append(LineString([(f["lon"], f["lat"]) for f in fixes]))
        rows.append(
            {
                "sid": sid,
                "name": fixes[0]["name"],
                "season": fixes[0]["season"],
                "basin": fixes[0]["basin"],
                "max_wind_kt": max(winds) if winds else None,
                "min_pres_mb": min(press) if press else None,
                "max_category": max_cat,
                "max_category_label": _saffir_label(max_cat),
                "start_time": fixes[0]["iso_time"],
                "end_time": fixes[-1]["iso_time"],
                "n_fixes": len(fixes),
            }
        )
    if n_dropped:
        logger.info(
            "fetch_storm_tracks: dropped %d single-fix storm(s) in lines mode",
            n_dropped,
        )
    if not rows:
        raise StormTracksNoStormsError(
            "Every matching storm has a single best-track fix - too short to "
            "draw as a line. Re-issue with geometry='points'."
        )
    gdf = gpd.GeoDataFrame(
        {k: [r[k] for r in rows] for k in rows[0]},
        geometry=geoms,
        crs="EPSG:4326",
    )
    return _write_fgb(gdf, f"{len(rows)} storm track line(s)")


def _build_point_flatgeobuf(records: list[dict[str, Any]]) -> bytes:
    """One Point per record -> FlatGeobuf bytes (EPSG:4326).

    Used for historical per-fix points AND active-storm current/forecast
    positions; the property set is the union of both record shapes (absent
    keys become None columns).
    """
    gpd = _import_gpd()
    from shapely.geometry import Point  # type: ignore[import-not-found]

    keys: list[str] = []
    for r in records:
        for k in r:
            if k not in ("lat", "lon", "forecast_track_zip") and k not in keys:
                keys.append(k)
    geoms = [Point(r["lon"], r["lat"]) for r in records]
    gdf = gpd.GeoDataFrame(
        {k: [r.get(k) for r in records] for k in keys},
        geometry=geoms,
        crs="EPSG:4326",
    )
    return _write_fgb(gdf, f"{len(records)} storm point(s)")


# ---------------------------------------------------------------------------
# Top-level fetchers (passed to read_through via closures).
# ---------------------------------------------------------------------------


def _fetch_historical_bytes(
    *,
    bbox: tuple[float, float, float, float],
    y0: int,
    y1: int,
    storm_name: str | None,
    geometry: str,
) -> tuple[bytes, tuple[float, float, float, float], int]:
    """Historical IBTrACS path -> (fgb_bytes, extent, n_storms)."""
    files = _select_ibtracs_files(bbox, y0, y1)
    storms: dict[str, list[dict[str, Any]]] = {}
    for fn in files:
        url = IBTRACS_CSV_BASE + fn
        logger.info("fetch_storm_tracks: GET %s", url)
        raw = _http_get(url)
        parsed = _parse_ibtracs_csv(raw, y0=y0, y1=y1, storm_name=storm_name)
        # Later files never overwrite earlier SIDs (SIDs are globally unique
        # across basin files anyway).
        for sid, fixes in parsed.items():
            storms.setdefault(sid, []).extend(fixes)

    selected = _select_storms_in_bbox(storms, bbox)
    scope = (
        f"bbox={bbox!r}, seasons {y0}..{y1}"
        + (f", name={storm_name!r}" if storm_name else "")
    )
    if not selected:
        raise StormTracksNoStormsError(
            f"No IBTrACS storm track touches {scope}. Widen the bbox, extend "
            f"the year range, or drop the name filter."
        )
    logger.info(
        "fetch_storm_tracks: %d storm(s) matched %s", len(selected), scope
    )

    all_fixes = [f for fixes in selected.values() for f in fixes]
    extent = _records_bbox(all_fixes)
    assert extent is not None  # selected is non-empty here

    if geometry == "points":
        if len(all_fixes) > _MAX_POINT_FEATURES:
            raise StormTracksInputError(
                f"{len(all_fixes)} best-track fixes exceed the "
                f"{_MAX_POINT_FEATURES}-point cap for {scope}. Narrow the "
                f"bbox / year range, or use geometry='lines'."
            )
        rows = [
            dict(f, category_label=_saffir_label(f["category"]))
            for f in all_fixes
        ]
        return _build_point_flatgeobuf(rows), extent, len(selected)
    return _build_line_flatgeobuf(selected), extent, len(selected)


def _fetch_active_bytes(
    *,
    bbox: tuple[float, float, float, float] | None,
    storm_name: str | None,
) -> tuple[bytes, tuple[float, float, float, float], int]:
    """Active NHC path -> (fgb_bytes, extent, n_storms). Always points."""
    logger.info("fetch_storm_tracks: GET %s", NHC_CURRENT_STORMS_URL)
    raw = _http_get(NHC_CURRENT_STORMS_URL)
    storms = _parse_current_storms(raw)

    if storm_name:
        want = storm_name.strip().upper()
        storms = [s for s in storms if (s.get("name") or "").upper() == want]
    if bbox is not None:
        west, south, east, north = bbox
        storms = [
            s
            for s in storms
            if west <= s["lon"] <= east and south <= s["lat"] <= north
        ]
    if not storms:
        raise StormTracksNoActiveStormsError(
            "NHC is currently advising on zero active tropical cyclones"
            + (f" named {storm_name!r}" if storm_name else "")
            + (f" inside bbox={bbox!r}" if bbox is not None else "")
            + ". A quiet basin is normal outside peak season; use "
            "active_only=False for historical tracks."
        )

    records: list[dict[str, Any]] = []
    for s in storms:
        cur = {k: v for k, v in s.items() if k != "forecast_track_zip"}
        cur["tau_h"] = 0.0
        cur["is_forecast"] = 0
        records.append(cur)
        zip_url = s.get("forecast_track_zip")
        if zip_url:
            for p in _fetch_forecast_track_points(zip_url, s):
                p["is_forecast"] = 1
                records.append(p)

    extent = _records_bbox(records)
    assert extent is not None  # storms is non-empty here
    return _build_point_flatgeobuf(records), extent, len(storms)


# ---------------------------------------------------------------------------
# Registered atomic tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    supports_global_query=False,
    payload_mb_estimator_name="estimate_payload_mb",
    # Annotations: readOnlyHint=True (read-only; no state mutation),
    # openWorldHint=True (calls external public API endpoints),
    # destructiveHint=False, idempotentHint=True (cache shim deduplicates).
    open_world_hint=True,
)
def fetch_storm_tracks(
    bbox: tuple[float, float, float, float] | None = None,
    start_year: int | None = None,
    end_year: int | None = None,
    storm_name: str | None = None,
    active_only: bool = False,
    geometry: str = "lines",
    # Absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch REAL hurricane / tropical-cyclone tracks as a vector FlatGeobuf.

    HISTORICAL mode (default): subsets the IBTrACS v04r01 best-track archive
    (NOAA NCEI - every tropical cyclone since 1842, all basins) by bbox +
    season (year) range + optional storm name. Selection is storm-wise: any
    storm whose track touches the bbox is returned with its FULL track, so a
    landfalling hurricane keeps its open-ocean history. Emits one LineString
    per storm (default) or one Point per 3/6-hourly fix, with wind speed (kt),
    central pressure (mb), and Saffir-Simpson category attributes.

    ACTIVE mode (``active_only=True``): the NHC CurrentStorms.json feed - the
    storms under advisory RIGHT NOW - each enriched best-effort with its
    official 5-day forecast-track points (``tau`` = forecast hour,
    ``intensity_kt`` per position). Always emits Points. If the forecast-track
    GIS product cannot be fetched, the layer degrades to current-position
    points only (logged, never fabricated).

    When to use:
        - "show hurricane tracks near Florida since 2004", "storms that hit
          Puerto Rico", "Hurricane Michael's track", "typhoon tracks in the
          West Pacific 2015-2020".
        - "is there a hurricane right now / where is it heading" ->
          ``active_only=True``.
        - Providing storm-track context for a coastal-flood / surge / wind
          discussion (composes with SFINCS coastal scenarios).

    When NOT to use:
        - Severe LOCAL storm reports (tornado / hail / wind damage points) ->
          ``fetch_storm_events_db``.
        - Active WATCHES / WARNINGS polygons -> ``fetch_nws_event`` /
          ``fetch_nws_alerts_conus``.
        - Modeled surge / inundation -> the flood-scenario tools; this is the
          track record, not a hazard footprint.

    Parameters:
        bbox: ``(west, south, east, north)`` EPSG:4326. REQUIRED for
            historical mode (it bounds the archive subset). Optional in active
            mode (filters storms by CURRENT position).
        start_year / end_year: Season (calendar-year) range, inclusive.
            Default = the most recent 3 seasons. Southern-hemisphere seasons
            follow the IBTrACS SEASON convention. Ranges reaching further back
            than the last 3 seasons trigger a per-basin archive download
            (~5-60 MB, slower); a bbox spanning more than 2 basins is
            rejected with guidance rather than pulling the 330 MB global file.
        storm_name: Optional exact name filter, case-insensitive (e.g.
            ``"MICHAEL"``, ``"KATRINA"``). Note: names are reused across
            years - combine with a year range for a specific storm. Unnamed
            systems carry the IBTrACS name ``NOT_NAMED``.
        active_only: When True, use the live NHC feed (see ACTIVE mode above).
            ``start_year`` / ``end_year`` are ignored.
        geometry: ``"lines"`` (default; one LineString per storm with
            per-storm peak intensity attrs) or ``"points"`` (one Point per
            best-track fix with per-fix wind/pressure/category). Active mode
            is always points.

    Returns:
        ``LayerURI`` pointing at a FlatGeobuf in the cache bucket.
        ``layer_type="vector"``, ``role="primary"``,
        ``style_preset="storm_tracks"``, ``units="kt / mb"``. ``bbox`` is the
        extent of the returned tracks (which can exceed the query bbox because
        full tracks are kept).
        Line props: ``sid``, ``name``, ``season``, ``basin``,
        ``max_wind_kt``, ``min_pres_mb``, ``max_category`` (-5..5 USA_SSHS),
        ``max_category_label``, ``start_time``, ``end_time``, ``n_fixes``.
        Historical point props: ``sid``, ``name``, ``season``, ``basin``,
        ``iso_time``, ``nature``, ``wind_kt``, ``pres_mb``, ``category``,
        ``category_label``, ``status``, plus the wind-structure columns
        (blank-tolerant): ``rmw_nmi`` (USA_RMW), ``poci_mb`` (USA_POCI),
        ``roci_nmi`` (USA_ROCI), ``r34_ne_nmi`` / ``r34_se_nmi`` /
        ``r34_sw_nmi`` / ``r34_nw_nmi`` (USA_R34_*) — consumed by the
        hurricane-spiderweb parametric (``model_flood_scenario`` storm branch).
        Active point props: ``id``, ``name``, ``classification``,
        ``intensity_kt``, ``pressure_mb``, ``movement_dir_deg``,
        ``movement_speed_kt``, ``last_update``, ``tau_h`` (0 = current
        position), ``is_forecast`` (0/1).

    Honest-empty paths (data-source fallback norm):
        - ``StormTracksNoStormsError``: no archived track matches the scope.
        - ``StormTracksNoActiveStormsError``: NHC is advising on zero storms
          (the common out-of-season state) or none match the filter.
        Never an empty success-shaped layer.

    Errors (FR-AS-11 typed-error surface):
        - ``StormTracksInputError``: bad bbox / years / geometry, bbox spans
          too many basins, or a points-mode result over the 50000-fix cap
          (retryable=False).
        - ``StormTracksUpstreamError``: NCEI / NHC network failure, HTTP 5xx,
          or malformed body (retryable=True).

    Cache: ``ttl_class="dynamic-1h"``, ``source_class="storm_tracks"``. The
    hourly bucket keeps active storms fresh; identical historical queries
    within the hour reuse the FGB.

    Cross-tool dependencies (FR-TA-3):
        - Composes WITH: ``publish_layer`` (map overlay), ``geocode_location``
          (derive a bbox from a place name first),
          ``fetch_nws_event`` (live warnings for an approaching storm),
          ``fetch_noaa_coops_tides`` (observed surge at landfall).
        - Upstream sources: NOAA NCEI IBTrACS v04r01 (historical), NOAA NHC
          CurrentStorms.json + forecast-track GIS (active).

    Source-tier: FR-HEP-2 Tier 1 (NOAA NCEI / NHC). Tier-1 free, no API key.
    """
    if geometry not in ("lines", "points"):
        raise StormTracksInputError(
            f"geometry must be 'lines' or 'points'; got {geometry!r}"
        )
    if storm_name is not None and not isinstance(storm_name, str):
        raise StormTracksInputError(
            f"storm_name must be a string; got {type(storm_name).__name__}"
        )

    resolved_bbox: tuple[float, float, float, float] | None = None
    if bbox is not None:
        if not isinstance(bbox, (tuple, list)):
            raise StormTracksInputError(
                f"bbox must be a 4-tuple/list; got {type(bbox).__name__}"
            )
        bbox_t: tuple[float, float, float, float] = tuple(
            float(v) for v in bbox
        )  # type: ignore[assignment]
        _validate_bbox(bbox_t)
        resolved_bbox = _round_bbox_to_6dp(bbox_t)

    name_canon = storm_name.strip().upper() if storm_name else None

    captured: dict[str, Any] = {}

    if bool(active_only):
        params: dict[str, Any] = {
            "mode": "active",
            "bbox": list(resolved_bbox) if resolved_bbox is not None else None,
            "storm_name": name_canon,
        }

        def _fetch_bytes() -> bytes:
            fgb, extent, n = _fetch_active_bytes(
                bbox=resolved_bbox, storm_name=name_canon
            )
            captured["extent"] = extent
            captured["n"] = n
            return fgb

        scope_tag = "active (NHC)"
    else:
        if resolved_bbox is None:
            raise StormTracksInputError(
                "fetch_storm_tracks historical mode requires "
                "bbox=(west, south, east, north) in EPSG:4326 - it bounds the "
                "IBTrACS archive subset. (Only active_only=True may omit it.)"
            )
        y0, y1 = _resolve_years(start_year, end_year)
        params = {
            "mode": "historical",
            "bbox": list(resolved_bbox),
            "start_year": y0,
            "end_year": y1,
            "storm_name": name_canon,
            "geometry": geometry,
        }
        hist_bbox = resolved_bbox

        def _fetch_bytes() -> bytes:
            fgb, extent, n = _fetch_historical_bytes(
                bbox=hist_bbox,
                y0=y0,
                y1=y1,
                storm_name=name_canon,
                geometry=geometry,
            )
            captured["extent"] = extent
            captured["n"] = n
            return fgb

        scope_tag = f"{y0}..{y1}"

    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=_fetch_bytes,
    )
    assert result.uri is not None, (
        "fetch_storm_tracks is cacheable; uri must be set by read_through"
    )

    # On a cache HIT the fetch_fn never ran - fall back to the requested bbox
    # (active mode with no bbox leaves it None; the inline vector path fits
    # the map to the rendered features).
    extent_bbox: tuple[float, float, float, float] | None = captured.get("extent")
    if extent_bbox is None:
        extent_bbox = resolved_bbox

    seed = hashlib.sha256(
        json.dumps(params, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:8]

    if name_canon:
        scope_tag = f"{name_canon} {scope_tag}"
    mode_tag = "NHC active storms" if active_only else "IBTrACS tracks"
    name = f"Storm tracks - {mode_tag} ({scope_tag})"

    return LayerURI(
        layer_id=f"storm-tracks-{seed}",
        name=name,
        layer_type="vector",
        uri=result.uri,
        style_preset="storm_tracks",
        role="primary",
        units="kt / mb",
        bbox=extent_bbox,
    )
