"""``fetch_tsunami_events`` atomic tool - real historical tsunamis as points.

Queries the NOAA NCEI / World Data Service (WDS) **Global Historical Tsunami
Database** (the authoritative catalog of tsunamis from 2100 BC to the present,
the same record behind the NCEI Natural Hazards viewer) for tsunami SOURCE
EVENTS or coastal RUNUP observations inside a bbox and year window. Returns one
Point feature per record at the source epicenter (events) or the observation
shore location (runups), carrying ``year``, ``cause`` (earthquake / volcano /
landslide / meteorological / ...), ``max_water_height`` (m), ``deaths``,
``eq_magnitude`` and the ``source`` attribution. This is the canonical OBSERVED
historical-tsunami record - what actually happened - NOT a probabilistic
tsunami-inundation hazard model.

**API surface** (NCEI Hazel "hazard-service", free, NO API key required):

    https://www.ngdc.noaa.gov/hazel/hazard-service/api/v1/tsunamis/events
        ?minYear=...&maxYear=...
        &minLatitude=...&maxLatitude=...&minLongitude=...&maxLongitude=...
        &page=1&itemsPerPage=200

    https://www.ngdc.noaa.gov/hazel/hazard-service/api/v1/tsunamis/runups
        ?...same selectors...

The response is a paginated JSON object::

    {"items": [ {...event...}, ... ],
     "page": 1, "totalPages": 3, "itemsPerPage": 200, "totalItems": 481}

Each ``events[]`` item carries ``id``, ``year``/``month``/``day``,
``causeCode`` (integer tsunami cause), ``locationName``, ``country``,
``latitude``/``longitude`` (the SOURCE location), ``eqMagnitude``,
``maxWaterHeight`` (max observed water height, m), ``numRunups`` and
``deathsTotal``/``deaths``. Each ``runups[]`` item carries the shore-side
``latitude``/``longitude``, ``runupHt`` (the observed runup/water height at that
shore, m), ``distFromSource`` (km), ``sourceCauseCode`` and
``sourceEqMagnitude``.

**Two observation modes** (``observation_type``):

    - ``"events"`` (default): one Point per tsunami at its SOURCE. Sparse and
      bounded - the whole global catalog is only a few thousand events. The
      requested ``year`` / ``cause`` / ``max_water_height`` / ``deaths`` /
      ``source`` props live here. This is the right default for "tsunamis near
      X" / "historical tsunamis".
    - ``"runups"``: one Point per coastal RUNUP observation (the dense
      shore-by-shore measured water heights). A single active region/decade can
      hold tens of thousands of runups, so this mode is strictly paginated and
      capped; use it only for a tight bbox + year window.

**Window semantics**: the database is YEAR-granular. ``min_year`` / ``max_year``
default to the full historical range (``DEFAULT_MIN_YEAR`` .. current year). A
``None`` bbox is a GLOBAL query (``supports_global_query=True``).

**Result-cap handling**: pagination is followed up to ``MAX_PAGES`` pages
(``MAX_PAGES * 200`` records). If the scope exceeds that we raise a typed
``TsunamiResultTooLargeError`` telling the caller to narrow the bbox, shorten the
year window, or switch to ``observation_type="events"`` - we never silently
truncate.

**Honest-empty path** (data-source fallback norm - primary -> honest typed
error): the service returns HTTP 200 with ``totalItems = 0`` for a bbox/window
with no recorded tsunamis. That is a legitimate "no tsunamis" answer, not an
error, so we raise a typed ``TsunamiNoEventsError`` (retryable=False) - never an
empty success-shaped layer.

**Output**: a vector ``LayerURI`` (``layer_type="vector"``) whose artifact is a
Point FeatureCollection serialized as FlatGeobuf and rendered via the inline
vector path. ``style_preset="tsunami_events"``; ``LayerURI.bbox`` is set to the
records' extent so the camera auto-zooms.

Tier-1, no auth, ``supports_global_query=True`` (the NCEI catalog is global; a
bbox-less call covers the whole planet, bounded by the year window + the page
cap).

FR-AS-11 typed-error surface; FR-TA-2 / FR-AS-3 docstring discipline applies.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import math
import os
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
    "fetch_tsunami_events",
    "estimate_payload_mb",
    "TsunamiError",
    "TsunamiInputError",
    "TsunamiResultTooLargeError",
    "TsunamiUpstreamError",
    "TsunamiNoEventsError",
    "_validate_bbox",
    "_resolve_year_window",
    "_round_bbox_to_6dp",
    "_build_query_url",
    "_parse_items",
    "_records_bbox",
    "_build_flatgeobuf",
    "_fetch_tsunami_bytes",
    "_cause_label",
    "NCEI_TSUNAMI_BASE",
    "CAUSE_CODES",
    "DEFAULT_MIN_YEAR",
    "ITEMS_PER_PAGE",
    "MAX_PAGES",
]

logger = logging.getLogger("trid3nt_server.tools.fetchers.hazard.fetch_tsunami_events")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class TsunamiError(RuntimeError):
    """Base class for fetch_tsunami_events failures.

    ``error_code`` maps to the WebSocket A.6 error frame emitted by the agent
    surface. ``retryable`` guides FR-AS-11 retry/clarify/fallback logic.
    """

    error_code: str = "TSUNAMI_EVENTS_ERROR"
    retryable: bool = True


class TsunamiInputError(TsunamiError):
    """Invalid inputs - bad bbox shape, bad year, reversed window, bad mode.

    Not retryable: the caller must fix the argument.
    """

    error_code = "TSUNAMI_EVENTS_INPUT_ERROR"
    retryable = False


class TsunamiResultTooLargeError(TsunamiError):
    """The query would return more records than the page cap allows.

    Not retryable as-is. The caller should narrow the bbox, shorten the year
    window, or switch to ``observation_type="events"`` (far sparser than
    runups).
    """

    error_code = "TSUNAMI_EVENTS_RESULT_TOO_LARGE"
    retryable = False


class TsunamiUpstreamError(TsunamiError):
    """NCEI hazard-service request failed (network error, HTTP 5xx, bad body).

    Retryable - transient NCEI outages recover on retry.
    """

    error_code = "TSUNAMI_EVENTS_UPSTREAM_ERROR"
    retryable = True


class TsunamiNoEventsError(TsunamiError):
    """No tsunamis matched the bbox / year window.

    Not retryable - no tsunami is recorded for that area/window in the NCEI
    catalog. Either widen the window, widen the bbox, or pick a more
    tsunami-prone coastline (Pacific Rim, Indonesia, Japan, Chile, Alaska,
    the Mediterranean). We never return an empty success-shaped layer.
    """

    error_code = "TSUNAMI_EVENTS_NO_EVENTS"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: NCEI Hazel hazard-service tsunami API base.
NCEI_TSUNAMI_BASE = (
    "https://www.ngdc.noaa.gov/hazel/hazard-service/api/v1/tsunamis"
)

#: NCEI tsunami ``causeCode`` integer -> human label. This is the published
#: NCEI / WDS Global Historical Tsunami Database cause classification. The raw
#: integer ``cause_code`` is ALWAYS preserved on the feature, so an unmapped /
#: future code still carries its source value even if the label falls back.
CAUSE_CODES: dict[int, str] = {
    0: "Unknown",
    1: "Earthquake",
    2: "Questionable Earthquake",
    3: "Earthquake and Landslide",
    4: "Volcano and Earthquake",
    5: "Volcano, Earthquake, and Landslide",
    6: "Volcano",
    7: "Volcano and Landslide",
    8: "Landslide",
    9: "Meteorological",
    10: "Explosion",
    11: "Astronomical Tide",
}

#: Earliest sensible default ``min_year`` - the NCEI catalog reaches back to
#: 2100 BC, but the modern, spatially reliable record is overwhelmingly
#: post-1900. We default the floor here; a caller can pass an older ``min_year``
#: explicitly to reach the ancient record.
DEFAULT_MIN_YEAR = 1900

#: NCEI default page size (the service caps a page at 200 items).
ITEMS_PER_PAGE = 200

#: Maximum pages we will follow before declaring the scope too large. Events
#: are sparse (the whole global catalog is a few thousand), so this only ever
#: bites the dense ``runups`` mode on a too-wide bbox/window.
MAX_PAGES = 25

#: User-Agent per NOAA usage guidance (a descriptive UA is recommended).
_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)

#: HTTP timeout (seconds) per page request.
_HTTP_TIMEOUT = 60.0

#: Valid observation modes.
_VALID_MODES = ("events", "runups")


# ---------------------------------------------------------------------------
# AtomicToolMetadata - registered once at import time.
# ---------------------------------------------------------------------------


def _build_metadata() -> AtomicToolMetadata:
    """Construct AtomicToolMetadata defensively against schema flag variants."""
    common: dict[str, Any] = dict(
        name="fetch_tsunami_events",
        # Historical catalog updated rarely (new events appended after an
        # event); a weekly bucket is plenty and keeps repeat asks cheap.
        ttl_class="semi-static-7d",
        source_class="ncei_tsunami",
        cacheable=True,
    )
    try:
        return AtomicToolMetadata(  # type: ignore[call-arg]
            **common,
            supports_global_query=True,
            payload_mb_estimator_name="estimate_payload_mb",
        )
    except Exception:
        logger.debug(
            "AtomicToolMetadata does not support all Wave-1.5 flags; "
            "registering fetch_tsunami_events without them"
        )
        return AtomicToolMetadata(**common)


_METADATA = _build_metadata()


# ---------------------------------------------------------------------------
# Payload-MB estimator (Wave 1.5 chat-warning system).
# ---------------------------------------------------------------------------


def estimate_payload_mb(
    bbox: tuple[float, float, float, float] | None = None,
    min_year: int | None = None,
    max_year: int | None = None,
    observation_type: str = "events",
    **_kw: Any,
) -> float:
    """Estimate the output FlatGeobuf size in MB.

    Each record is one Point feature with ~12 small scalar properties
    (~250 bytes serialized). Event density is sparse (the whole global event
    catalog is a few thousand points, ~1 MB); runups are ~10-30x denser. The
    estimate is intentionally conservative and capped at the page-limit ceiling
    (``MAX_PAGES * ITEMS_PER_PAGE`` records).
    """
    mode = str(observation_type or "events").lower()

    # Year span (years).
    try:
        lo, hi = _resolve_year_window(min_year, max_year)
        n_years = max(1.0, float(hi - lo + 1))
    except Exception:
        n_years = float(_dt.datetime.now(_dt.timezone.utc).year - DEFAULT_MIN_YEAR + 1)

    # Area (sq deg). A bbox-less (global) query covers the whole sphere.
    if bbox is None:
        area_sq_deg = 64800.0  # 360 * 180
    else:
        try:
            west, south, east, north = (float(v) for v in bbox)
            area_sq_deg = max(0.0, east - west) * max(0.0, north - south)
        except (TypeError, ValueError):
            area_sq_deg = 100.0

    # Global density anchors (records per sq-deg per year), tuned to the
    # observed catalog: events are very sparse, runups much denser.
    per_sqdeg_year = 6.0e-5 if mode == "events" else 1.2e-3
    n_records = area_sq_deg * n_years * per_sqdeg_year
    n_records = min(float(MAX_PAGES * ITEMS_PER_PAGE), max(0.0, n_records))
    return max(0.001, n_records * 250 / 1_000_000.0)


# ---------------------------------------------------------------------------
# Input validation + window resolution.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Raise ``TsunamiInputError`` if the bbox is malformed or out of range."""
    if not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
        raise TsunamiInputError(
            f"bbox must be (west, south, east, north); got {bbox!r}"
        )
    west, south, east, north = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise TsunamiInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= west <= 180.0 and -180.0 <= east <= 180.0):
        raise TsunamiInputError(f"bbox lon values out of [-180, 180]: {bbox!r}")
    if not (-90.0 <= south <= 90.0 and -90.0 <= north <= 90.0):
        raise TsunamiInputError(f"bbox lat values out of [-90, 90]: {bbox!r}")
    if west >= east or south >= north:
        raise TsunamiInputError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )


def _resolve_year_window(
    min_year: int | None,
    max_year: int | None,
) -> tuple[int, int]:
    """Resolve the (min_year, max_year) window as integers.

    Both omitted -> ``DEFAULT_MIN_YEAR`` .. current UTC year. Accepts ints or
    int-like strings/floats. Raises ``TsunamiInputError`` on a non-integer year
    or a reversed range.
    """
    cur = _dt.datetime.now(_dt.timezone.utc).year

    def _coerce(v: Any, label: str) -> int:
        try:
            iv = int(v)
        except (TypeError, ValueError) as exc:
            raise TsunamiInputError(
                f"{label} must be an integer year; got {v!r}"
            ) from exc
        # The NCEI catalog spans 2100 BC (-2100) to the present. Guard against
        # nonsense like a year far in the future.
        if not (-2100 <= iv <= cur + 1):
            raise TsunamiInputError(
                f"{label}={iv} is outside the catalog range [-2100, {cur + 1}]"
            )
        return iv

    lo = DEFAULT_MIN_YEAR if min_year is None else _coerce(min_year, "min_year")
    hi = cur if max_year is None else _coerce(max_year, "max_year")
    if lo > hi:
        raise TsunamiInputError(
            f"min_year must be <= max_year; got min_year={lo}, max_year={hi}"
        )
    return lo, hi


def _validate_mode(observation_type: str | None) -> str:
    """Validate + normalize the observation mode. ``None`` -> ``"events"``."""
    if observation_type is None:
        return "events"
    mode = str(observation_type).strip().lower()
    # Tolerate singular/plural and a couple of natural synonyms.
    if mode in ("event", "events", "source", "sources"):
        return "events"
    if mode in ("runup", "runups", "observation", "observations"):
        return "runups"
    raise TsunamiInputError(
        f"observation_type must be one of {_VALID_MODES!r}; got "
        f"{observation_type!r}"
    )


def _round_bbox_to_6dp(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    return tuple(round(v, 6) for v in bbox)  # type: ignore[return-value]


def _cause_label(cause_code: Any) -> str:
    """Map an NCEI ``causeCode`` integer to a human label (fallback Unknown)."""
    try:
        ci = int(cause_code)
    except (TypeError, ValueError):
        return "Unknown"
    return CAUSE_CODES.get(ci, "Unknown")


# ---------------------------------------------------------------------------
# HTTP helper.
# ---------------------------------------------------------------------------


def _http_get_json(url: str, timeout: float = _HTTP_TIMEOUT) -> dict[str, Any]:
    """HTTP GET returning a parsed JSON object.

    Raises ``TsunamiUpstreamError`` on network failure, HTTP error, or a body
    that is not a JSON object.
    """
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise TsunamiUpstreamError(
            f"NCEI tsunami service returned HTTP {exc.code} for {url}: "
            f"{exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise TsunamiUpstreamError(
            f"Network error fetching {url}: {exc.reason}"
        ) from exc
    except TimeoutError as exc:
        raise TsunamiUpstreamError(
            f"Timed out after {timeout}s fetching {url}"
        ) from exc

    try:
        obj = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise TsunamiUpstreamError(
            f"NCEI tsunami response is not valid JSON: {exc}"
        ) from exc
    if not isinstance(obj, dict):
        raise TsunamiUpstreamError(
            f"NCEI tsunami response is not a JSON object: "
            f"type={type(obj).__name__}"
        )
    return obj


# ---------------------------------------------------------------------------
# URL builder.
# ---------------------------------------------------------------------------


def _build_query_url(
    *,
    mode: str,
    bbox: tuple[float, float, float, float] | None,
    min_year: int,
    max_year: int,
    page: int,
) -> str:
    """Build the NCEI tsunami query URL for one page."""
    params: list[tuple[str, str]] = [
        ("minYear", str(min_year)),
        ("maxYear", str(max_year)),
        ("page", str(page)),
        ("itemsPerPage", str(ITEMS_PER_PAGE)),
    ]
    if bbox is not None:
        west, south, east, north = bbox
        params.append(("minLongitude", repr(west)))
        params.append(("minLatitude", repr(south)))
        params.append(("maxLongitude", repr(east)))
        params.append(("maxLatitude", repr(north)))
    return f"{NCEI_TSUNAMI_BASE}/{mode}?" + urllib.parse.urlencode(params)


# ---------------------------------------------------------------------------
# JSON parser.
# ---------------------------------------------------------------------------


def _f(value: Any) -> float | None:
    """Coerce to a finite float, else None."""
    if value is None:
        return None
    try:
        fv = float(value)
    except (TypeError, ValueError):
        return None
    return fv if math.isfinite(fv) else None


def _i(value: Any) -> int | None:
    """Coerce to an int, else None."""
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _s(value: Any) -> str | None:
    """Coerce to a stripped non-empty string, else None."""
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _parse_items(
    items: list[Any],
    mode: str,
) -> list[dict[str, Any]]:
    """Parse NCEI ``items[]`` -> point records for the given mode.

    Records with no parseable lat/lon are dropped (some catalog rows have a
    known year/cause but no geocoded location). Both modes emit a uniform
    record shape so the FlatGeobuf schema is stable:

        {id, lon, lat, year, cause_code, cause, location_name, country,
         eq_magnitude, max_water_height, deaths, num_runups,
         dist_from_source_km, observation_type, source}

    For ``events`` the location is the SOURCE; ``max_water_height`` is the
    event ``maxWaterHeight`` and ``num_runups`` the runup count. For ``runups``
    the location is the SHORE observation; ``max_water_height`` is that shore's
    ``runupHt`` and ``dist_from_source_km`` is the great-circle distance from
    the source.
    """
    records: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        lat = _f(it.get("latitude"))
        lon = _f(it.get("longitude"))
        if lat is None or lon is None:
            continue
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            continue

        if mode == "events":
            cause_code = _i(it.get("causeCode"))
            # NCEI exposes both a per-source ``deaths`` and a ``deathsTotal``
            # (source + secondary effects). Prefer the total when present.
            deaths = _i(it.get("deathsTotal"))
            if deaths is None:
                deaths = _i(it.get("deaths"))
            rec = {
                "id": _i(it.get("id")),
                "lon": lon,
                "lat": lat,
                "year": _i(it.get("year")),
                "cause_code": cause_code,
                "cause": _cause_label(cause_code),
                "location_name": _s(it.get("locationName")),
                "country": _s(it.get("country")),
                "eq_magnitude": _f(it.get("eqMagnitude")),
                "max_water_height": _f(it.get("maxWaterHeight")),
                "deaths": deaths,
                "num_runups": _i(it.get("numRunups")),
                "dist_from_source_km": None,
                "observation_type": "event",
                "source": "NCEI/WDS Global Historical Tsunami Database",
            }
        else:  # runups
            cause_code = _i(it.get("sourceCauseCode"))
            rec = {
                "id": _i(it.get("id")),
                "lon": lon,
                "lat": lat,
                "year": _i(it.get("year")),
                "cause_code": cause_code,
                "cause": _cause_label(cause_code),
                "location_name": _s(it.get("locationName")),
                "country": _s(it.get("country")),
                "eq_magnitude": _f(it.get("sourceEqMagnitude")),
                "max_water_height": _f(it.get("runupHt")),
                "deaths": _i(it.get("deaths")),
                "num_runups": None,
                "dist_from_source_km": _f(it.get("distFromSource")),
                "observation_type": "runup",
                "source": "NCEI/WDS Global Historical Tsunami Database",
            }
        records.append(rec)
    return records


# ---------------------------------------------------------------------------
# Extent + FlatGeobuf builder.
# ---------------------------------------------------------------------------


def _records_bbox(
    records: list[dict[str, Any]],
) -> tuple[float, float, float, float] | None:
    """Compute the (west, south, east, north) extent of the record points.

    Pads a degenerate single-point extent by ~0.1 deg so the camera does not
    zoom to an infinite level. Returns ``None`` for an empty list.
    """
    if not records:
        return None
    lons = [r["lon"] for r in records]
    lats = [r["lat"] for r in records]
    west, east = min(lons), max(lons)
    south, north = min(lats), max(lats)
    if west == east:
        west -= 0.1
        east += 0.1
    if south == north:
        south -= 0.1
        north += 0.1
    return (west, south, east, north)


def _build_flatgeobuf(records: list[dict[str, Any]]) -> bytes:
    """Serialize records -> FlatGeobuf bytes (Point geometry, EPSG:4326).

    Raises ``TsunamiUpstreamError`` if geopandas/shapely are unavailable or the
    write fails. ``records`` must be non-empty (the caller enforces the
    no-events honest-error gate before calling this).
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
        from shapely.geometry import Point  # type: ignore[import-not-found]
    except ImportError as exc:
        raise TsunamiUpstreamError(
            f"geopandas / shapely not available: {exc}"
        ) from exc

    geoms = [Point(r["lon"], r["lat"]) for r in records]
    data = {
        "id": [r.get("id") for r in records],
        "year": [r.get("year") for r in records],
        "cause_code": [r.get("cause_code") for r in records],
        "cause": [r.get("cause") for r in records],
        "location_name": [r.get("location_name") for r in records],
        "country": [r.get("country") for r in records],
        "eq_magnitude": [r.get("eq_magnitude") for r in records],
        "max_water_height": [r.get("max_water_height") for r in records],
        "deaths": [r.get("deaths") for r in records],
        "num_runups": [r.get("num_runups") for r in records],
        "dist_from_source_km": [r.get("dist_from_source_km") for r in records],
        "observation_type": [r.get("observation_type") for r in records],
        "source": [r.get("source") for r in records],
    }
    gdf = gpd.GeoDataFrame(data, geometry=geoms, crs="EPSG:4326")

    tmp_fgb: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="trid3nt_tsunami_"
        ) as f:
            tmp_fgb = f.name
        gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        with open(tmp_fgb, "rb") as f:
            return f.read()
    except Exception as exc:
        raise TsunamiUpstreamError(
            f"FlatGeobuf write failed for {len(records)} tsunami records: {exc}"
        ) from exc
    finally:
        if tmp_fgb is not None:
            try:
                os.unlink(tmp_fgb)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Top-level fetch (passed to read_through). Returns (fgb_bytes, extent_bbox).
# ---------------------------------------------------------------------------


def _fetch_tsunami_bytes(
    *,
    mode: str,
    bbox: tuple[float, float, float, float] | None,
    min_year: int,
    max_year: int,
) -> tuple[bytes, tuple[float, float, float, float]]:
    """End-to-end fetch: paginated NCEI JSON -> records -> FGB bytes.

    Returns ``(fgb_bytes, extent_bbox)``. Raises:
      - ``TsunamiResultTooLargeError`` when the scope exceeds the page cap.
      - ``TsunamiNoEventsError`` when zero records match.
    """
    scope = f"bbox={bbox!r}" if bbox is not None else "global (no bbox)"
    window_str = f"{min_year}..{max_year}"

    all_items: list[Any] = []
    total_items: int | None = None
    total_pages: int = 1
    page = 1
    while page <= total_pages:
        if page > MAX_PAGES:
            raise TsunamiResultTooLargeError(
                f"NCEI tsunami {mode} query ({scope}, years {window_str}) spans "
                f"{total_pages} pages (>{MAX_PAGES}-page cap, "
                f"~{total_items if total_items is not None else 'many'} records). "
                f"Narrow the bbox, shorten the year window, or use "
                f"observation_type='events' (far sparser than runups)."
            )
        url = _build_query_url(
            mode=mode,
            bbox=bbox,
            min_year=min_year,
            max_year=max_year,
            page=page,
        )
        logger.info("fetch_tsunami_events: GET %s", url)
        body = _http_get_json(url)

        if total_items is None:
            total_items = _i(body.get("totalItems"))
            tp = _i(body.get("totalPages"))
            total_pages = tp if (tp is not None and tp >= 1) else 1
            # Early too-large guard: if the service reports more pages than the
            # cap, fail before walking them.
            if total_pages > MAX_PAGES:
                raise TsunamiResultTooLargeError(
                    f"NCEI tsunami {mode} query ({scope}, years {window_str}) "
                    f"reports {total_items} records over {total_pages} pages "
                    f"(>{MAX_PAGES}-page cap). Narrow the bbox, shorten the year "
                    f"window, or use observation_type='events'."
                )

        items = body.get("items") or []
        if isinstance(items, list):
            all_items.extend(items)
        page += 1

    records = _parse_items(all_items, mode)

    if not records:
        raise TsunamiNoEventsError(
            f"No tsunami {mode} matched {scope} over years {window_str}. The "
            f"NCEI Global Historical Tsunami Database has no record for that "
            f"scope. Widen the window, widen the bbox, or pick a more "
            f"tsunami-prone coastline (Pacific Rim, Japan, Indonesia, Chile, "
            f"Alaska, the Mediterranean)."
        )

    logger.info(
        "fetch_tsunami_events: %d %s record(s) for %s over years %s",
        len(records),
        mode,
        scope,
        window_str,
    )
    extent = _records_bbox(records)
    assert extent is not None  # records is non-empty here
    return _build_flatgeobuf(records), extent


# ---------------------------------------------------------------------------
# Registered atomic tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    supports_global_query=True,
    payload_mb_estimator_name="estimate_payload_mb",
    # Annotations: readOnlyHint=True (read-only; no state mutation),
    # openWorldHint=True (calls external public API endpoint),
    # destructiveHint=False, idempotentHint=True (cache shim deduplicates).
    open_world_hint=True,
)
def fetch_tsunami_events(
    bbox: tuple[float, float, float, float] | None = None,
    min_year: int | None = None,
    max_year: int | None = None,
    observation_type: str = "events",
    # Absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch REAL historical tsunamis as a point FlatGeobuf.

    Retrieves recorded tsunami SOURCE EVENTS (or coastal RUNUP observations)
    from the NOAA NCEI / World Data Service **Global Historical Tsunami
    Database** (the authoritative catalog from 2100 BC to the present, the same
    record behind the NCEI Natural Hazards viewer) inside a bbox and year
    window. Returns one Point feature per record carrying the year, the cause
    (earthquake / volcano / landslide / meteorological / ...), the maximum
    observed water height (m), the death toll, the source earthquake magnitude,
    and the NCEI source attribution. This is the canonical OBSERVED
    historical-tsunami record - what actually happened - NOT a probabilistic
    tsunami-inundation hazard model.

    When to use:
        - The user asks for historical / past tsunamis, "tsunami events",
          "tidal waves", "where have tsunamis happened", a tsunami history, or
          tsunami deaths / run-up heights for an area (e.g. "show tsunamis near
          Japan", "historical tsunamis in the Pacific", "what tsunamis hit
          Indonesia", "biggest tsunamis in Chile", "the 2011 Tohoku tsunami").
        - You need the actual recorded source epicenters, water heights, causes,
          and death tolls - the real event catalog - to map, count, or annotate
          a coastal-hazard / SLR / surge discussion.

    When NOT to use:
        - PROBABILISTIC tsunami INUNDATION / run-up HAZARD with a return period,
          or a modeled inundation extent for a hypothetical event - this tool
          returns the OBSERVED historical record, not a modeled hazard surface.
        - Live tsunami WARNINGS / advisories (use a warning feed; this is a
          historical catalog).
        - Earthquakes themselves (use ``fetch_usgs_earthquakes`` - this tool
          returns the tsunamis those quakes generated, with ``eq_magnitude`` as
          a property).
        - Coastal sea-level-rise scenarios (use the NOAA SLR tools).

    Parameters:
        bbox: Optional ``(west, south, east, north)`` in EPSG:4326 to restrict
            to a coastline / region / ocean basin. When omitted the query is
            GLOBAL (``supports_global_query=True``) - bounded by the year window
            and the page cap. Derive a bbox from a place name with
            ``geocode_location`` or ``fetch_administrative_boundaries`` first for
            area-scoped asks.
        min_year / max_year: Optional integer year bounds (inclusive). When both
            omitted the window is ``1900`` .. the current year. Pass an older
            ``min_year`` (the catalog reaches 2100 BC) for the ancient record.
            Example: ``min_year=2011, max_year=2011`` for the Tohoku year.
        observation_type: ``"events"`` (default) for one Point per tsunami at
            its SOURCE (sparse; carries cause / max_water_height / deaths /
            num_runups) - the right default for "tsunamis near X". ``"runups"``
            for the dense per-shore RUNUP observations (the measured water
            height at each coastline); use only with a TIGHT bbox + short year
            window, as a wide runups scope hits the result cap.

    Returns:
        ``LayerURI`` pointing at a FlatGeobuf in the cache bucket. Geometry:
        Point per record, EPSG:4326. ``layer_type="vector"``, ``role="primary"``,
        ``style_preset="tsunami_events"`` (the client sizes the marker by
        ``max_water_height`` and colors by ``cause``),
        ``units="m water height"``. ``bbox`` is set to the records' extent so
        the camera auto-zooms. Properties per record:
            - ``id`` (NCEI record id),
            - ``year`` (event year; negative = BC),
            - ``cause_code`` (NCEI integer cause) + ``cause`` (human label:
              "Earthquake", "Volcano", "Landslide", "Meteorological", ...),
            - ``location_name`` / ``country`` (source or shore location),
            - ``eq_magnitude`` (source earthquake magnitude; null if non-seismic),
            - ``max_water_height`` (max observed water/run-up height, m),
            - ``deaths`` (death toll; total incl. secondary effects when known),
            - ``num_runups`` (count of run-up observations; events mode only),
            - ``dist_from_source_km`` (great-circle km from source; runups only),
            - ``observation_type`` ("event" | "runup"),
            - ``source`` (NCEI/WDS attribution string).

    Honest-empty path (data-source fallback norm - primary -> honest typed
    error): the NCEI service returns HTTP 200 with ``totalItems = 0`` for a
    bbox/window with no recorded tsunamis. That is a legitimate "no tsunamis"
    answer, not a success - so ``TsunamiNoEventsError`` is raised (never an
    empty success-shaped layer).

    Cache: ``ttl_class="semi-static-7d"``, ``source_class="ncei_tsunami"``.
    Cache key is SHA-256 of ``(mode, bbox-rounded-6dp, min_year, max_year)``, so
    identical-scope calls within the week reuse the FGB.

    Cross-tool dependencies (FR-TA-3):
        - Composes WITH: ``publish_layer`` (map overlay), ``geocode_location``
          (derive a bbox from a place name BEFORE this call),
          ``fetch_administrative_boundaries`` (country/region framing),
          ``fetch_usgs_earthquakes`` (the quakes that generated these tsunamis),
          ``fetch_noaa_slr_scenarios`` (coastal-hazard context),
          ``compute_zonal_statistics`` (count events inside a polygon).
        - Upstream data source: NOAA NCEI Hazel hazard-service
          (ngdc.noaa.gov/hazel/hazard-service/api/v1/tsunamis).

    Errors (FR-AS-11 typed-error surface):
        - ``TsunamiInputError``: bad bbox / bad year / reversed window / bad
          observation_type (retryable=False).
        - ``TsunamiResultTooLargeError``: the scope exceeds the page cap -
          narrow bbox / shorten window / use observation_type='events'
          (retryable=False).
        - ``TsunamiUpstreamError``: NCEI network failure / HTTP error / bad body
          (retryable=True).
        - ``TsunamiNoEventsError``: no tsunamis matched the scope
          (retryable=False).

    Source-tier: FR-HEP-2 Tier 1 (NOAA NCEI federal hazard catalog). Claims
    from these records should be marked ``source_authority_tier=1``.

    Tier-1 free. No API key. ``supports_global_query=True``.
    """
    # 1. Resolve + validate the spatial selector (optional bbox).
    resolved_bbox: tuple[float, float, float, float] | None = None
    if bbox is not None:
        if not isinstance(bbox, (tuple, list)):
            raise TsunamiInputError(
                f"bbox must be a 4-tuple/list or omitted; got "
                f"{type(bbox).__name__}"
            )
        bbox_t: tuple[float, float, float, float] = tuple(
            float(v) for v in bbox
        )  # type: ignore[assignment]
        _validate_bbox(bbox_t)
        resolved_bbox = _round_bbox_to_6dp(bbox_t)

    # 2. Resolve the year window + observation mode.
    lo_year, hi_year = _resolve_year_window(min_year, max_year)
    mode = _validate_mode(observation_type)

    # 3. Cache-key params.
    params: dict[str, Any] = {
        "mode": mode,
        "bbox": list(resolved_bbox) if resolved_bbox is not None else None,
        "min_year": lo_year,
        "max_year": hi_year,
    }

    # The fetch_fn returns (bytes, extent); read_through caches the bytes. We
    # need the extent for LayerURI.bbox, so capture it via a closure side-channel.
    captured: dict[str, Any] = {}

    def _fetch_bytes() -> bytes:
        fgb, extent = _fetch_tsunami_bytes(
            mode=mode,
            bbox=resolved_bbox,
            min_year=lo_year,
            max_year=hi_year,
        )
        captured["extent"] = extent
        return fgb

    # 4. Read-through cache.
    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=_fetch_bytes,
    )
    assert result.uri is not None, (
        "fetch_tsunami_events is cacheable; uri must be set by read_through"
    )

    # 5. Resolve the camera extent. On a cache HIT the fetch_fn never ran so
    # ``captured`` is empty - fall back to the requested bbox (a global query
    # has no requested bbox, so leave it None: the inline vector path fits the
    # map to the rendered features).
    extent_bbox: tuple[float, float, float, float] | None = captured.get("extent")
    if extent_bbox is None:
        extent_bbox = resolved_bbox

    # 6. Build a descriptive layer name + stable id.
    if resolved_bbox is not None:
        scope_tag = (
            f"{resolved_bbox[0]:.2f},{resolved_bbox[1]:.2f}->"
            f"{resolved_bbox[2]:.2f},{resolved_bbox[3]:.2f}"
        )
    else:
        scope_tag = "global"
    mode_tag = "runups" if mode == "runups" else "events"
    seed = hashlib.sha256(
        json.dumps(params, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:8]

    name = (
        f"NCEI tsunamis ({mode_tag}) - {scope_tag} "
        f"({lo_year}..{hi_year})"
    )
    layer_id = f"tsunami-events-{seed}"

    return LayerURI(
        layer_id=layer_id,
        name=name,
        layer_type="vector",
        uri=result.uri,
        style_preset="tsunami_events",
        role="primary",
        units="m water height",
        bbox=extent_bbox,
    )
