"""``fetch_usgs_volcano_alerts`` atomic tool — current US volcano alert levels.

Queries the USGS Volcano Hazards Program HANS public API (the same machine API
behind the USGS "current volcanic activity" / CAP-alert feeds) for the current
volcanic-alert status of every monitored US volcano, and returns one Point
feature per volcano at its summit carrying the four-stage Volcano Alert Level
(``Normal`` / ``Advisory`` / ``Watch`` / ``Warning``) and the Aviation Color
Code (``Green`` / ``Yellow`` / ``Orange`` / ``Red``). This is the canonical
OBSERVED volcanic-unrest status — the observatory's current call on each
volcano, NOT a probabilistic eruption-hazard model.

**Why two endpoints (the alert spine has no coordinates)**

The HANS ``getMonitoredVolcanoes`` route is the complete alert spine: it lists
every monitored US volcano with its current ``alert_level`` and ``color_code``,
INCLUDING the many that sit at ``NORMAL`` / ``GREEN`` (the
``getElevatedVolcanoes`` route, by contrast, lists only the handful currently
above-normal). But neither alert route carries latitude/longitude. The
``getUSVolcanoes`` route is the geographic spine: it lists every US volcano with
``latitude`` / ``longitude`` / ``elevation_meters`` / ``region`` keyed by
``vnum``. We fetch BOTH and inner-join on ``vnum`` so each alert gets a real
summit coordinate. Alert records that do not join to a coordinate (a couple of
aggregate "Alaskan Volcanoes" / "Cascade Range" placeholder rows with a null
``vnum``) are dropped — they are not point-locatable volcanoes.

**API surface** (USGS HANS public API, free, NO API key required):

    https://volcanoes.usgs.gov/hans-public/api/volcano/getMonitoredVolcanoes
        -> JSON list, each: {volcano_name, vnum, alert_level, color_code,
                             obs_abbr, sent_utc, notice_url, ...}
    https://volcanoes.usgs.gov/hans-public/api/volcano/getUSVolcanoes
        -> JSON list, each: {vnum, volcano_name, latitude, longitude,
                             elevation_meters, region, volcano_url,
                             nvews_threat, obs_abbr, ...}

Both return a plain JSON array (HTTP 200). The join key is ``vnum`` (the
Smithsonian Global Volcanism Program volcano number, as a string).

**bbox semantics**: the monitored-volcano list is tiny and bounded (~70
volcanoes), so the default is a GLOBAL (all-US) snapshot
(``supports_global_query=True``). When a ``bbox`` is supplied we filter the
joined points to that extent in-process (no server-side spatial query exists).
Hawaii, Alaska (Aleutians), the Cascades, and the western CONUS volcanoes each
fall in their own region, so a region bbox returns just that region's volcanoes.

**Honest-empty path** (data-source fallback norm — primary -> honest typed
error): a bbox that contains no monitored US volcano is a legitimate "no
volcanoes here" answer, not an error, so we raise a typed
``VolcanoAlertsNoVolcanoesError`` (retryable=False) carrying the scope — never
an empty success-shaped layer. The same applies if HANS ever returns an empty
monitored list.

**Output**: a vector ``LayerURI`` (``layer_type="vector"``) whose artifact is a
point FeatureCollection (one point per volcano), serialized as FlatGeobuf and
rendered via the inline vector path. ``style_preset="volcano_alerts"`` (the
client colors the marker by ``color_code`` / ``alert_level``);
``LayerURI.bbox`` is set to the volcanoes' extent so the camera auto-zooms.

Tier-1, no auth, ``supports_global_query=True`` (the monitored list is global to
the US volcano-observatory network and tiny/bounded).

FR-AS-11 typed-error surface; FR-TA-2 / FR-AS-3 docstring discipline applies.
"""

from __future__ import annotations

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

from grace2_contracts.execution import LayerURI
from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import read_through

__all__ = [
    "fetch_usgs_volcano_alerts",
    "estimate_payload_mb",
    "VolcanoAlertsError",
    "VolcanoAlertsInputError",
    "VolcanoAlertsUpstreamError",
    "VolcanoAlertsNoVolcanoesError",
    "_validate_bbox",
    "_round_bbox_to_6dp",
    "_parse_alert_list",
    "_parse_coord_list",
    "_join_alerts_to_coords",
    "_filter_to_bbox",
    "_volcanoes_bbox",
    "_build_flatgeobuf",
    "_fetch_usgs_volcano_alerts_bytes",
    "MONITORED_URL",
    "US_VOLCANOES_URL",
    "ALERT_LEVELS",
    "COLOR_CODES",
]

logger = logging.getLogger("grace2_agent.tools.fetch_usgs_volcano_alerts")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class VolcanoAlertsError(RuntimeError):
    """Base class for fetch_usgs_volcano_alerts failures.

    ``error_code`` maps to the WebSocket A.6 error frame emitted by the agent
    surface. ``retryable`` guides FR-AS-11 retry/clarify/fallback logic.
    """

    error_code: str = "USGS_VOLCANO_ALERTS_ERROR"
    retryable: bool = True


class VolcanoAlertsInputError(VolcanoAlertsError):
    """Invalid inputs — bad bbox shape or out-of-range coordinates.

    Not retryable: the caller must fix the argument.
    """

    error_code = "USGS_VOLCANO_ALERTS_INPUT_ERROR"
    retryable = False


class VolcanoAlertsUpstreamError(VolcanoAlertsError):
    """USGS HANS request failed (network error, HTTP 5xx, bad body).

    Retryable — transient USGS outages recover on retry.
    """

    error_code = "USGS_VOLCANO_ALERTS_UPSTREAM_ERROR"
    retryable = True


class VolcanoAlertsNoVolcanoesError(VolcanoAlertsError):
    """No monitored US volcano matched the bbox (or the monitored list is empty).

    Not retryable — there is simply no monitored volcano in that area. Either
    widen the bbox or run a global (bbox-less) snapshot. We never return an
    empty success-shaped layer.
    """

    error_code = "USGS_VOLCANO_ALERTS_NO_VOLCANOES"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: USGS HANS public API — complete monitored-volcano list WITH current alert
#: level + aviation color code (includes NORMAL/GREEN volcanoes). No coords.
MONITORED_URL = (
    "https://volcanoes.usgs.gov/hans-public/api/volcano/getMonitoredVolcanoes"
)

#: USGS HANS public API — every US volcano WITH latitude/longitude/elevation/
#: region keyed by vnum. The geographic spine we join the alerts onto.
US_VOLCANOES_URL = (
    "https://volcanoes.usgs.gov/hans-public/api/volcano/getUSVolcanoes"
)

#: The four-stage USGS Volcano Alert Level ladder (low -> high), used to order /
#: validate alert strings and to compute a numeric severity rank for styling.
ALERT_LEVELS = ("NORMAL", "ADVISORY", "WATCH", "WARNING")

#: The four-stage ICAO Aviation Color Code ladder (low -> high).
COLOR_CODES = ("GREEN", "YELLOW", "ORANGE", "RED")

#: User-Agent per USGS usage guidance (a descriptive UA is recommended).
_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)

#: HTTP timeout (seconds). HANS is small + fast.
_HTTP_TIMEOUT = 60.0


# ---------------------------------------------------------------------------
# AtomicToolMetadata — registered once at import time.
# ---------------------------------------------------------------------------


def _build_metadata() -> AtomicToolMetadata:
    """Construct AtomicToolMetadata defensively against schema flag variants."""
    common: dict[str, Any] = dict(
        name="fetch_usgs_volcano_alerts",
        ttl_class="dynamic-1h",
        source_class="usgs_volcano_alerts",
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
            "registering fetch_usgs_volcano_alerts without them"
        )
        return AtomicToolMetadata(**common)


_METADATA = _build_metadata()


# ---------------------------------------------------------------------------
# Payload-MB estimator (Wave 1.5 chat-warning system).
# ---------------------------------------------------------------------------


def estimate_payload_mb(
    bbox: tuple[float, float, float, float] | None = None,
    **_kw: Any,
) -> float:
    """Estimate the output FlatGeobuf size in MB.

    The monitored US volcano list is tiny and bounded (~70 volcanoes globally).
    Each volcano is one Point feature with a handful of small scalar properties
    (~250 bytes serialized). A bbox only ever shrinks the count. The whole
    layer is well under a megabyte regardless of scope.
    """
    # ~70 monitored US volcanoes globally; a bbox can only reduce that.
    n = 70.0
    if bbox is not None:
        # Crudest possible area heuristic: keep the global estimate (the layer
        # is so small the warning system never needs precision here).
        n = 70.0
    return max(0.001, n * 250 / 1_000_000.0)


# ---------------------------------------------------------------------------
# Input validation.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Raise ``VolcanoAlertsInputError`` if the bbox is malformed/out of range."""
    if not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
        raise VolcanoAlertsInputError(
            f"bbox must be (west, south, east, north); got {bbox!r}"
        )
    west, south, east, north = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise VolcanoAlertsInputError(
            f"bbox contains non-finite values: {bbox!r}"
        )
    if not (-180.0 <= west <= 180.0 and -180.0 <= east <= 180.0):
        raise VolcanoAlertsInputError(
            f"bbox lon values out of [-180, 180]: {bbox!r}"
        )
    if not (-90.0 <= south <= 90.0 and -90.0 <= north <= 90.0):
        raise VolcanoAlertsInputError(
            f"bbox lat values out of [-90, 90]: {bbox!r}"
        )
    if west >= east or south >= north:
        raise VolcanoAlertsInputError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )


def _round_bbox_to_6dp(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    return tuple(round(v, 6) for v in bbox)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# HTTP helper.
# ---------------------------------------------------------------------------


def _http_get_json(url: str, timeout: float = _HTTP_TIMEOUT) -> Any:
    """Plain HTTP GET returning parsed JSON.

    Raises ``VolcanoAlertsUpstreamError`` on network failure, HTTP error, or a
    body that is not valid JSON.
    """
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise VolcanoAlertsUpstreamError(
            f"USGS HANS returned HTTP {exc.code} for {url}: {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise VolcanoAlertsUpstreamError(
            f"Network error fetching {url}: {exc.reason}"
        ) from exc
    except TimeoutError as exc:
        raise VolcanoAlertsUpstreamError(
            f"Timed out after {timeout}s fetching {url}"
        ) from exc

    try:
        return json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise VolcanoAlertsUpstreamError(
            f"USGS HANS response from {url} is not valid JSON: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Parsers.
# ---------------------------------------------------------------------------


def _alert_rank(alert_level: str | None) -> int:
    """Numeric severity rank for an alert level (0=NORMAL .. 3=WARNING).

    Unknown / null -> -1 (rendered as "unknown"). Case-insensitive.
    """
    if not alert_level:
        return -1
    try:
        return ALERT_LEVELS.index(str(alert_level).strip().upper())
    except ValueError:
        return -1


def _color_rank(color_code: str | None) -> int:
    """Numeric severity rank for an aviation color code (0=GREEN .. 3=RED)."""
    if not color_code:
        return -1
    try:
        return COLOR_CODES.index(str(color_code).strip().upper())
    except ValueError:
        return -1


def _parse_alert_list(obj: Any) -> dict[str, dict[str, Any]]:
    """Parse the ``getMonitoredVolcanoes`` JSON -> {vnum: alert-record}.

    Each input element carries the current ``alert_level`` + ``color_code`` for
    one monitored volcano. We key by ``vnum`` (string). Rows without a usable
    ``vnum`` (the aggregate "Alaskan Volcanoes" / "Cascade Range" placeholders
    that carry ``vnum=None``) are skipped — they are not point-locatable.

    Returns ``{}`` for an empty list (-> honest no-volcanoes downstream).
    Raises ``VolcanoAlertsUpstreamError`` on a non-list body.
    """
    if not isinstance(obj, list):
        raise VolcanoAlertsUpstreamError(
            f"USGS HANS monitored-volcano response is not a JSON list: "
            f"type={type(obj).__name__}"
        )
    out: dict[str, dict[str, Any]] = {}
    for el in obj:
        if not isinstance(el, dict):
            continue
        vnum_raw = el.get("vnum")
        if vnum_raw in (None, "", "None"):
            continue
        vnum = str(vnum_raw).strip()
        if not vnum or vnum.lower() == "none":
            continue
        alert = el.get("alert_level")
        color = el.get("color_code")
        out[vnum] = {
            "vnum": vnum,
            "volcano_name": str(el.get("volcano_name") or "").strip() or None,
            "alert_level": (str(alert).strip().upper() if alert else None),
            "color_code": (str(color).strip().upper() if color else None),
            "observatory": str(el.get("obs_abbr") or "").strip() or None,
            "sent_utc": str(el.get("sent_utc") or "").strip() or None,
            "notice_url": str(el.get("notice_url") or "").strip() or None,
        }
    return out


def _parse_coord_list(obj: Any) -> dict[str, dict[str, Any]]:
    """Parse the ``getUSVolcanoes`` JSON -> {vnum: coord-record}.

    Each input element carries ``latitude`` / ``longitude`` /
    ``elevation_meters`` / ``region`` for one US volcano keyed by ``vnum``.
    Rows with a null/non-finite lat or lon are skipped (not point-locatable).

    Raises ``VolcanoAlertsUpstreamError`` on a non-list body.
    """
    if not isinstance(obj, list):
        raise VolcanoAlertsUpstreamError(
            f"USGS HANS US-volcano response is not a JSON list: "
            f"type={type(obj).__name__}"
        )
    out: dict[str, dict[str, Any]] = {}
    for el in obj:
        if not isinstance(el, dict):
            continue
        vnum_raw = el.get("vnum")
        if vnum_raw in (None, "", "None"):
            continue
        vnum = str(vnum_raw).strip()
        if not vnum or vnum.lower() == "none":
            continue
        try:
            lat = float(el.get("latitude"))
            lon = float(el.get("longitude"))
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(lat) and math.isfinite(lon)):
            continue
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            continue
        elev: float | None = None
        try:
            ev = el.get("elevation_meters")
            if ev is not None:
                fev = float(ev)
                if math.isfinite(fev):
                    elev = fev
        except (TypeError, ValueError):
            elev = None
        out[vnum] = {
            "vnum": vnum,
            "volcano_name": str(el.get("volcano_name") or "").strip() or None,
            "lat": lat,
            "lon": lon,
            "elevation_m": elev,
            "region": str(el.get("region") or "").strip() or None,
            "volcano_url": str(el.get("volcano_url") or "").strip() or None,
            "nvews_threat": str(el.get("nvews_threat") or "").strip() or None,
        }
    return out


def _join_alerts_to_coords(
    alerts: dict[str, dict[str, Any]],
    coords: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Inner-join alert records to coordinate records on ``vnum``.

    Every alert that has a matching coordinate becomes one output record with a
    real summit point + the current alert level / color code + elevation /
    region. Alerts with no coordinate match are dropped (logged at debug).

    Returns a list of merged records (possibly empty). Each record:
        {vnum, volcano_name, alert_level, color_code, alert_rank, color_rank,
         elevation_m, region, observatory, sent_utc, notice_url, volcano_url,
         nvews_threat, lat, lon}
    """
    merged: list[dict[str, Any]] = []
    dropped = 0
    for vnum, a in alerts.items():
        c = coords.get(vnum)
        if c is None:
            dropped += 1
            continue
        merged.append(
            {
                "vnum": vnum,
                # Prefer the geographic spine's canonical name; fall back to the
                # alert record's name.
                "volcano_name": c.get("volcano_name")
                or a.get("volcano_name"),
                "alert_level": a.get("alert_level"),
                "color_code": a.get("color_code"),
                "alert_rank": _alert_rank(a.get("alert_level")),
                "color_rank": _color_rank(a.get("color_code")),
                "elevation_m": c.get("elevation_m"),
                "region": c.get("region"),
                "observatory": a.get("observatory"),
                "sent_utc": a.get("sent_utc"),
                "notice_url": a.get("notice_url"),
                "volcano_url": c.get("volcano_url"),
                "nvews_threat": c.get("nvews_threat"),
                "lat": c["lat"],
                "lon": c["lon"],
            }
        )
    if dropped:
        logger.debug(
            "fetch_usgs_volcano_alerts: dropped %d alert(s) with no coordinate "
            "match (aggregate placeholders)",
            dropped,
        )
    # Stable, severity-descending order so the most-elevated alerts draw on top
    # and a downstream summary reads worst-first.
    merged.sort(
        key=lambda r: (-(r["color_rank"]), -(r["alert_rank"]), r["volcano_name"] or "")
    )
    return merged


def _filter_to_bbox(
    records: list[dict[str, Any]],
    bbox: tuple[float, float, float, float] | None,
) -> list[dict[str, Any]]:
    """Keep only records whose point falls inside ``bbox`` (inclusive).

    ``bbox`` is ``(west, south, east, north)`` in EPSG:4326. ``None`` -> no
    filter (global snapshot).
    """
    if bbox is None:
        return list(records)
    west, south, east, north = bbox
    return [
        r
        for r in records
        if west <= r["lon"] <= east and south <= r["lat"] <= north
    ]


# ---------------------------------------------------------------------------
# Extent + FlatGeobuf builder.
# ---------------------------------------------------------------------------


def _volcanoes_bbox(
    records: list[dict[str, Any]],
) -> tuple[float, float, float, float] | None:
    """Compute the (west, south, east, north) extent of the volcano points.

    Pads a degenerate single-point extent by ~0.25 deg so the camera does not
    zoom to an infinite level. Returns ``None`` for an empty list.
    """
    if not records:
        return None
    lons = [r["lon"] for r in records]
    lats = [r["lat"] for r in records]
    west, east = min(lons), max(lons)
    south, north = min(lats), max(lats)
    if west == east:
        west -= 0.25
        east += 0.25
    if south == north:
        south -= 0.25
        north += 0.25
    return (west, south, east, north)


def _build_flatgeobuf(records: list[dict[str, Any]]) -> bytes:
    """Serialize volcano records -> FlatGeobuf bytes (Point geometry, EPSG:4326).

    One Point feature per volcano carrying ``vnum``, ``volcano_name``,
    ``alert_level``, ``color_code``, ``alert_rank``, ``color_rank``,
    ``elevation_m``, ``region``, ``observatory``, ``sent_utc``, ``notice_url``,
    ``volcano_url``, ``nvews_threat``.

    Raises ``VolcanoAlertsUpstreamError`` if geopandas/shapely are unavailable
    or the write fails. ``records`` must be non-empty (the caller enforces the
    no-volcanoes honest-error gate before calling this).
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
        from shapely.geometry import Point  # type: ignore[import-not-found]
    except ImportError as exc:
        raise VolcanoAlertsUpstreamError(
            f"geopandas / shapely not available: {exc}"
        ) from exc

    geoms = [Point(r["lon"], r["lat"]) for r in records]
    data = {
        "vnum": [str(r.get("vnum") or "") for r in records],
        "volcano_name": [r.get("volcano_name") for r in records],
        "alert_level": [r.get("alert_level") for r in records],
        "color_code": [r.get("color_code") for r in records],
        "alert_rank": [int(r.get("alert_rank", -1)) for r in records],
        "color_rank": [int(r.get("color_rank", -1)) for r in records],
        "elevation_m": [r.get("elevation_m") for r in records],
        "region": [r.get("region") for r in records],
        "observatory": [r.get("observatory") for r in records],
        "sent_utc": [r.get("sent_utc") for r in records],
        "notice_url": [r.get("notice_url") for r in records],
        "volcano_url": [r.get("volcano_url") for r in records],
        "nvews_threat": [r.get("nvews_threat") for r in records],
    }
    gdf = gpd.GeoDataFrame(data, geometry=geoms, crs="EPSG:4326")

    tmp_fgb: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="grace2_volc_"
        ) as f:
            tmp_fgb = f.name
        gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        with open(tmp_fgb, "rb") as f:
            return f.read()
    except Exception as exc:
        raise VolcanoAlertsUpstreamError(
            f"FlatGeobuf write failed for {len(records)} volcano alerts: {exc}"
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


def _fetch_usgs_volcano_alerts_bytes(
    *,
    bbox: tuple[float, float, float, float] | None,
) -> tuple[bytes, tuple[float, float, float, float]]:
    """End-to-end fetch: HANS alerts + coords -> joined records -> FGB bytes.

    Returns ``(fgb_bytes, extent_bbox)``. Raises:
      - ``VolcanoAlertsUpstreamError`` on a HANS network/parse failure.
      - ``VolcanoAlertsNoVolcanoesError`` when zero volcanoes match the scope.
    """
    scope = f"bbox={bbox!r}" if bbox is not None else "global (no bbox)"

    logger.info("fetch_usgs_volcano_alerts: GET %s", MONITORED_URL)
    alert_obj = _http_get_json(MONITORED_URL)
    alerts = _parse_alert_list(alert_obj)

    logger.info("fetch_usgs_volcano_alerts: GET %s", US_VOLCANOES_URL)
    coord_obj = _http_get_json(US_VOLCANOES_URL)
    coords = _parse_coord_list(coord_obj)

    if not alerts:
        raise VolcanoAlertsNoVolcanoesError(
            "USGS HANS returned no monitored volcanoes. This is unexpected for "
            "the US volcano-observatory network; retry shortly."
        )

    merged = _join_alerts_to_coords(alerts, coords)
    if not merged:
        raise VolcanoAlertsNoVolcanoesError(
            "No monitored US volcano could be matched to a coordinate (the HANS "
            "alert list and geographic list did not join on vnum). Retry shortly."
        )

    filtered = _filter_to_bbox(merged, bbox)
    if not filtered:
        raise VolcanoAlertsNoVolcanoesError(
            f"No monitored US volcano falls within {scope}. The US volcano-"
            f"observatory network monitors Alaska/Aleutians, Hawaii, the Cascade "
            f"Range, the western CONUS, and a few Pacific/Mariana islands — widen "
            f"the bbox or run a global (bbox-less) snapshot."
        )

    logger.info(
        "fetch_usgs_volcano_alerts: %d volcano(es) for %s (%d monitored, "
        "%d with coords)",
        len(filtered),
        scope,
        len(alerts),
        len(coords),
    )
    extent = _volcanoes_bbox(filtered)
    assert extent is not None  # filtered is non-empty here
    return _build_flatgeobuf(filtered), extent


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
def fetch_usgs_volcano_alerts(
    bbox: tuple[float, float, float, float] | None = None,
    # Absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch the CURRENT USGS volcano alert levels as a point FlatGeobuf.

    Use this (not fetch_usgs_earthquakes, and go past geocode_location) when you want CURRENT USGS VOLCANO alert levels.

    Retrieves the current volcanic-alert status of every monitored US volcano
    from the USGS Volcano Hazards Program HANS public API and returns one Point
    feature per volcano at its summit, carrying the four-stage Volcano Alert
    Level (``NORMAL`` / ``ADVISORY`` / ``WATCH`` / ``WARNING``) and the ICAO
    Aviation Color Code (``GREEN`` / ``YELLOW`` / ``ORANGE`` / ``RED``), plus
    summit elevation, region, and the responsible observatory. This is the
    canonical OBSERVED volcanic-unrest status — each observatory's current call
    on its volcanoes — NOT a probabilistic eruption-hazard model.

    When to use:
        - The user asks for the current volcano alert status / "which volcanoes
          are at watch/warning" / "is any volcano erupting" / "volcano alert
          levels" / "aviation color codes" (e.g. "show me volcanoes on alert",
          "what is Kilauea's alert level", "any Alaska volcanoes acting up",
          "map the Cascade volcanoes and their status").
        - You need the real current alert state — the observatory record — to
          map, count, or annotate.
        - Providing volcanic context for an ashfall / aviation / hazard
          discussion ("which volcanoes are currently elevated?").

    When NOT to use:
        - PROBABILISTIC eruption HAZARD or ashfall-dispersion modeling — this
          tool returns the current alert STATUS, not a modeled hazard surface.
        - Historical eruption catalogs / Holocene eruption records — this is the
          CURRENT alert snapshot only.
        - Earthquakes (use ``fetch_usgs_earthquakes``); a volcano's seismicity
          is not returned here, only its summary alert level.
        - Non-US volcanoes — the USGS monitored list covers the US volcano-
          observatory network (Alaska/Aleutians, Hawaii, Cascades, western
          CONUS, and a few Pacific/Mariana islands).

    Parameters:
        bbox: Optional ``(west, south, east, north)`` in EPSG:4326 to restrict
            to an area of interest (a region or state). When omitted the query
            is a GLOBAL all-US snapshot (``supports_global_query=True``) — the
            monitored list is tiny/bounded (~70 volcanoes). Filtering is done
            in-process (HANS has no server-side spatial query). Derive a bbox
            from a place name with ``geocode_location`` or
            ``fetch_administrative_boundaries`` first for area-scoped asks.
            Example: ``bbox=(-156.5, 18.8, -154.5, 20.5)`` for the Hawaiian
            volcanoes (Kilauea, Mauna Loa, Mauna Kea, Hualalai).

    Returns:
        ``LayerURI`` pointing at a FlatGeobuf in the cache bucket. Geometry:
        Point at each summit, EPSG:4326. ``layer_type="vector"``,
        ``role="primary"``, ``style_preset="volcano_alerts"`` (the client colors
        the marker by ``color_code`` / ``alert_level``), ``units="alert level"``.
        ``bbox`` is set to the volcanoes' extent so the camera auto-zooms.
        Properties per volcano:
            - ``vnum`` (Smithsonian GVP volcano number, e.g. ``"332010"``),
            - ``volcano_name`` (e.g. "Kilauea"),
            - ``alert_level`` ("NORMAL" | "ADVISORY" | "WATCH" | "WARNING";
              null if unassigned),
            - ``color_code`` ("GREEN" | "YELLOW" | "ORANGE" | "RED"; null if
              unassigned),
            - ``alert_rank`` (0=NORMAL .. 3=WARNING; -1 if unknown — for styling),
            - ``color_rank`` (0=GREEN .. 3=RED; -1 if unknown — for styling),
            - ``elevation_m`` (summit elevation, meters; null if absent),
            - ``region`` (e.g. "Alaska - Aleutians", "Hawaii", "Cascade Range"),
            - ``observatory`` (responsible observatory abbreviation:
              "avo" | "cvo" | "hvo" | "yvo" | "vdap"),
            - ``sent_utc`` (timestamp of the current notice, UTC),
            - ``notice_url`` (the HANS notice page),
            - ``volcano_url`` (the observatory volcano-info page),
            - ``nvews_threat`` (National Volcano Early Warning System threat
              class, e.g. "Very High Threat", "Moderate Threat").

    Honest-empty path (data-source fallback norm — primary -> honest typed
    error): a bbox that contains no monitored US volcano is a legitimate "no
    volcanoes here" answer, not a success — so ``VolcanoAlertsNoVolcanoesError``
    is raised (never an empty success-shaped layer).

    Cache: ``ttl_class="dynamic-1h"``, ``source_class="usgs_volcano_alerts"``.
    Cache key is SHA-256 of the bbox (rounded 6dp) or ``null`` for global, so
    identical-scope calls within the hour reuse the FGB.

    Cross-tool dependencies (FR-TA-3):
        - Composes WITH: ``publish_layer`` (map overlay), ``geocode_location``
          (derive a bbox from a place name BEFORE this call),
          ``fetch_administrative_boundaries`` (state/region framing),
          ``fetch_usgs_earthquakes`` (volcano-related seismicity nearby).
        - Upstream data source: USGS Volcano Hazards Program HANS public API
          (volcanoes.usgs.gov/hans-public/api/volcano/...).

    Errors (FR-AS-11 typed-error surface):
        - ``VolcanoAlertsInputError``: bad bbox shape / out-of-range coordinates
          (retryable=False).
        - ``VolcanoAlertsUpstreamError``: USGS HANS network failure / HTTP error
          / bad body (retryable=True).
        - ``VolcanoAlertsNoVolcanoesError``: no monitored volcano matched the
          scope (retryable=False).

    Source-tier: FR-HEP-2 Tier 1 (USGS federal volcano-observatory network).
    Claims from these records should be marked ``source_authority_tier=1``.

    Tier-1 free. No API key. ``supports_global_query=True``.
    """
    # 1. Resolve + validate the spatial selector (optional bbox).
    resolved_bbox: tuple[float, float, float, float] | None = None
    if bbox is not None:
        if not isinstance(bbox, (tuple, list)):
            raise VolcanoAlertsInputError(
                f"bbox must be a 4-tuple/list or omitted; got "
                f"{type(bbox).__name__}"
            )
        bbox_t: tuple[float, float, float, float] = tuple(
            float(v) for v in bbox
        )  # type: ignore[assignment]
        _validate_bbox(bbox_t)
        resolved_bbox = _round_bbox_to_6dp(bbox_t)

    # 2. Cache-key params.
    params: dict[str, Any] = {
        "bbox": list(resolved_bbox) if resolved_bbox is not None else None,
    }

    # The fetch_fn returns (bytes, extent); read_through caches the bytes. We
    # need the extent for LayerURI.bbox, so capture it via a closure side-channel.
    captured: dict[str, Any] = {}

    def _fetch_bytes() -> bytes:
        fgb, extent = _fetch_usgs_volcano_alerts_bytes(bbox=resolved_bbox)
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
        "fetch_usgs_volcano_alerts is cacheable; uri must be set by read_through"
    )

    # 4. Resolve the camera extent. On a cache HIT the fetch_fn never ran so
    # ``captured`` is empty — fall back to the requested bbox (a global query
    # has no requested bbox, so leave it None: the inline vector path fits the
    # map to the rendered features).
    extent_bbox: tuple[float, float, float, float] | None = captured.get("extent")
    if extent_bbox is None:
        extent_bbox = resolved_bbox

    # 5. Build a descriptive layer name + stable id.
    if resolved_bbox is not None:
        scope_tag = (
            f"{resolved_bbox[0]:.2f},{resolved_bbox[1]:.2f}->"
            f"{resolved_bbox[2]:.2f},{resolved_bbox[3]:.2f}"
        )
    else:
        scope_tag = "all US"
    seed = hashlib.sha256(
        json.dumps(params, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:8]

    name = f"USGS volcano alerts — {scope_tag}"
    layer_id = f"usgs-volcano-alerts-{seed}"

    return LayerURI(
        layer_id=layer_id,
        name=name,
        layer_type="vector",
        uri=result.uri,
        style_preset="volcano_alerts",
        role="primary",
        units="alert level",
        bbox=extent_bbox,
    )
