"""``fetch_climate_normals`` atomic tool — NOAA NCEI 1991-2020 U.S. Climate Normals.

Fetches the 1991-2020 U.S. Climate Normals (annual/seasonal product) from the
NOAA National Centers for Environmental Information (NCEI) for every Normals
station whose coordinates fall inside the requested bbox. The Climate Normals
are the official 30-year baseline of U.S. climate: long-term average annual
temperature (deg F) and total annual precipitation (inches), computed from
GHCN-Daily station records. They are the canonical "what is normal here?"
reference layer — the baseline against which an observed event (heat wave,
drought, anomalous rainfall) is judged.

API surface (NCEI Climate Normals, free, keyless HTTP access):

    inventory: https://www.ncei.noaa.gov/data/normals-annualseasonal/1991-2020/
               doc/inventory_30yr.txt
    per-station access file:
               https://www.ncei.noaa.gov/data/normals-annualseasonal/1991-2020/
               access/{STATION_ID}.csv

The station inventory is a fixed-width GHCN-Daily-style file (one row per
Normals station: id, lat, lon, elevation, state, name). All stations inside the
bbox are selected from the inventory (one cached fetch), then each matched
station's annual access CSV is downloaded and the annual-temperature and
annual-precipitation normals are extracted.

Output: a FlatGeobuf point layer (one point per station, at the station
coordinates) carrying ``station_id``, ``name``, ``normal_temp_f`` (annual
average temperature, deg F), ``normal_tmin_f``, ``normal_tmax_f``,
``normal_precip_in`` (annual total precipitation, inches), and
``elevation_m``. EPSG:4326. Cache: ``static-30d`` (the 1991-2020 normals are a
fixed published product that does not change).

FR-TA-2 / FR-AS-3 docstring discipline applies.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import math
import os
import tempfile
import urllib.error
import urllib.request
from typing import Any

from grace2_contracts.execution import LayerURI
from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import read_through

__all__ = [
    "fetch_climate_normals",
    "ClimateNormalsError",
    "ClimateNormalsInputError",
    "ClimateNormalsUpstreamError",
    "ClimateNormalsEmptyError",
    "estimate_payload_mb",
    "_discover_stations_in_bbox",
    "_fetch_station_normals",
    "_records_to_fgb",
]

logger = logging.getLogger("grace2_agent.tools.fetch_climate_normals")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class ClimateNormalsError(RuntimeError):
    """Base class for fetch_climate_normals failures.

    ``error_code`` maps to the WebSocket A.6 error frame emitted by the agent
    surface. ``retryable`` guides FR-AS-11 retry/clarify/fallback logic.
    """

    error_code: str = "CLIMATE_NORMALS_ERROR"
    retryable: bool = True


class ClimateNormalsInputError(ClimateNormalsError):
    """Invalid inputs — bad bbox geometry / out-of-range coordinates.

    Not retryable: the caller must fix the argument.
    """

    error_code = "CLIMATE_NORMALS_INPUT_ERROR"
    retryable = False


class ClimateNormalsUpstreamError(ClimateNormalsError):
    """NCEI request failed (network error, HTTP 5xx, malformed inventory/CSV).

    Retryable — transient NCEI outages recover on retry.
    """

    error_code = "CLIMATE_NORMALS_UPSTREAM_ERROR"
    retryable = True


class ClimateNormalsEmptyError(ClimateNormalsError):
    """No Normals stations in the bbox, or none carry usable normals.

    Not retryable — the bbox contains no 1991-2020 Normals stations with
    annual temperature or precipitation normals. Widen the bbox or move it
    over land within the U.S. + territories footprint.
    """

    error_code = "CLIMATE_NORMALS_EMPTY"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

_NCEI_BASE = "https://www.ncei.noaa.gov/data/normals-annualseasonal/1991-2020"
_INVENTORY_URL = f"{_NCEI_BASE}/doc/inventory_30yr.txt"
_ACCESS_URL = _NCEI_BASE + "/access/{sid}.csv"

# Descriptive User-Agent (NCEI best practice; helps their rate-limit logic).
_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)

# HTTP timeouts (seconds).
_INVENTORY_TIMEOUT = 60.0
_STATION_TIMEOUT = 30.0

# Maximum stations downloaded per call. NCEI access files are one HTTP GET
# each; the cap bounds latency for very large bboxes. The inventory is filtered
# spatially first so a small bbox stays well under the cap.
_MAX_STATIONS = 120

# NCEI sentinel for a missing normal in the access CSVs.
_MISSING_SENTINEL = -9999.0

# Annual normal column names in the annual/seasonal access CSV.
_COL_TAVG = "ANN-TAVG-NORMAL"   # annual average temperature (deg F)
_COL_TMIN = "ANN-TMIN-NORMAL"   # annual average daily minimum temperature
_COL_TMAX = "ANN-TMAX-NORMAL"   # annual average daily maximum temperature
_COL_PRCP = "ANN-PRCP-NORMAL"   # annual total precipitation (inches)


# ---------------------------------------------------------------------------
# AtomicToolMetadata — registered once at import time.
# ---------------------------------------------------------------------------


def _build_metadata() -> AtomicToolMetadata:
    """Build AtomicToolMetadata defensively to handle schema flag variants."""
    common: dict[str, Any] = dict(
        name="fetch_climate_normals",
        ttl_class="static-30d",
        source_class="climate_normals",
        cacheable=True,
    )
    try:
        return AtomicToolMetadata(**common, supports_global_query=False)  # type: ignore[call-arg]
    except Exception:
        logger.debug(
            "AtomicToolMetadata does not yet support supports_global_query; "
            "registering fetch_climate_normals without it"
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
    """Estimate output FlatGeobuf size in MB.

    Each Normals station is one small point feature (~150 bytes in FlatGeobuf
    with the handful of properties this tool exposes). The estimate uses bbox
    area and a rough CONUS Normals-station density (~6 stations per 1 deg
    square — CoCoRaHS + COOP + first-order), capped at ``_MAX_STATIONS``.
    """
    n_stations = 10  # default guess when bbox unavailable
    if bbox is not None:
        try:
            west, south, east, north = bbox
            sq_deg = max(0.0, east - west) * max(0.0, north - south)
            n_stations = max(1, min(_MAX_STATIONS, int(sq_deg * 6.0)))
        except (TypeError, ValueError):
            pass
    return max(0.001, n_stations * 150 / 1_000_000)


# ---------------------------------------------------------------------------
# HTTP helper.
# ---------------------------------------------------------------------------


def _http_get(url: str, timeout: float) -> bytes:
    """Plain HTTP GET. Raises ``ClimateNormalsUpstreamError`` on failure."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise ClimateNormalsUpstreamError(
            f"NCEI returned HTTP {exc.code} for {url}: {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise ClimateNormalsUpstreamError(
            f"Network error fetching {url}: {exc.reason}"
        ) from exc
    except TimeoutError as exc:
        raise ClimateNormalsUpstreamError(
            f"Timed out after {timeout}s fetching {url}"
        ) from exc


# ---------------------------------------------------------------------------
# Numeric coercion.
# ---------------------------------------------------------------------------


def _num(value: Any) -> float | None:
    """Coerce a Normals field to float, mapping NCEI sentinels to ``None``."""
    try:
        x = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(x) or x <= _MISSING_SENTINEL:
        return None
    return x


# ---------------------------------------------------------------------------
# Station discovery — find all Normals stations within a bbox.
# ---------------------------------------------------------------------------


def _parse_inventory(inv_bytes: bytes) -> list[dict[str, Any]]:
    """Parse the fixed-width ``inventory_30yr.txt`` into station dicts.

    GHCN-Daily-style fixed columns (0-indexed slices):
        id    [0:11]   station id
        lat   [12:20]  latitude
        lon   [21:30]  longitude
        elev  [30:37]  elevation (m)
        state [38:40]  US state / territory code
        name  [41:71]  station name
    """
    text = inv_bytes.decode("utf-8", errors="replace")
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        if len(line) < 40:
            continue
        sid = line[0:11].strip()
        if not sid:
            continue
        try:
            lat = float(line[12:20])
            lon = float(line[21:30])
        except ValueError:
            continue
        if not (math.isfinite(lat) and math.isfinite(lon)):
            continue
        out.append({
            "sid": sid,
            "lat": lat,
            "lon": lon,
            "elev": _num(line[30:37]),
            "state": line[38:40].strip(),
            "name": line[41:71].strip(),
        })
    return out


def _discover_stations_in_bbox(
    bbox: tuple[float, float, float, float],
    inv_bytes: bytes | None = None,
) -> list[dict[str, Any]]:
    """Find all 1991-2020 Normals stations whose coordinates fall in ``bbox``.

    Fetches (or reuses) the NCEI station inventory and filters spatially.
    Returns a list of station dicts: ``sid``, ``lat``, ``lon``, ``elev``,
    ``state``, ``name``. Capped at ``_MAX_STATIONS``.

    Raises:
        ``ClimateNormalsUpstreamError`` — inventory fetch / parse failure.
    """
    if inv_bytes is None:
        inv_bytes = _http_get(_INVENTORY_URL, timeout=_INVENTORY_TIMEOUT)
    stations = _parse_inventory(inv_bytes)
    if not stations:
        raise ClimateNormalsUpstreamError(
            "NCEI Normals inventory parsed to zero stations — file format "
            "may have changed or the download was truncated"
        )

    west, south, east, north = bbox
    matched: list[dict[str, Any]] = []
    for st in stations:
        if west <= st["lon"] <= east and south <= st["lat"] <= north:
            matched.append(st)
            if len(matched) >= _MAX_STATIONS:
                logger.info(
                    "fetch_climate_normals: station cap (%d) reached; truncating",
                    _MAX_STATIONS,
                )
                break
    return matched


# ---------------------------------------------------------------------------
# Per-station normals fetch.
# ---------------------------------------------------------------------------


def _parse_station_csv(raw: bytes) -> dict[str, Any] | None:
    """Parse a single station's annual access CSV into a normals dict.

    Returns ``None`` when the file is empty / unparseable. The access CSV has
    one data row for the annual product.
    """
    import csv

    text = raw.decode("utf-8", errors="replace")
    if not text.strip():
        return None
    reader = csv.DictReader(io.StringIO(text))
    try:
        row = next(reader)
    except StopIteration:
        return None
    return {
        "tavg": _num(row.get(_COL_TAVG)),
        "tmin": _num(row.get(_COL_TMIN)),
        "tmax": _num(row.get(_COL_TMAX)),
        "prcp": _num(row.get(_COL_PRCP)),
        "name": (row.get("NAME") or "").strip(),
        "lat": _num(row.get("LATITUDE")),
        "lon": _num(row.get("LONGITUDE")),
        "elev": _num(row.get("ELEVATION")),
    }


def _fetch_station_normals(
    stations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Download each station's annual normals and assemble records.

    Stations whose access file is missing (HTTP 404) or carries neither an
    annual temperature nor an annual precipitation normal are skipped — many
    inventory stations are precip-only (CoCoRaHS) and a few have no published
    annual access file. A station kept with precip-only normals reports
    ``normal_temp_f=None``.

    Returns a list of record dicts ready for FlatGeobuf serialization.
    """
    records: list[dict[str, Any]] = []
    for st in stations:
        url = _ACCESS_URL.format(sid=st["sid"])
        try:
            raw = _http_get(url, timeout=_STATION_TIMEOUT)
        except ClimateNormalsUpstreamError as exc:
            # 404 = this station has no annual access file; skip quietly.
            logger.debug(
                "fetch_climate_normals: skipping %s (%s)", st["sid"], exc
            )
            continue

        norm = _parse_station_csv(raw)
        if norm is None:
            continue
        if norm["tavg"] is None and norm["prcp"] is None:
            # No usable annual normal at this station.
            continue

        lat = norm["lat"] if norm["lat"] is not None else st["lat"]
        lon = norm["lon"] if norm["lon"] is not None else st["lon"]
        if lat is None or lon is None:
            continue
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            continue

        records.append({
            "station_id": st["sid"],
            "name": norm["name"] or st["name"],
            "elevation_m": (
                norm["elev"] if norm["elev"] is not None else st["elev"]
            ),
            "normal_temp_f": norm["tavg"],
            "normal_tmin_f": norm["tmin"],
            "normal_tmax_f": norm["tmax"],
            "normal_precip_in": norm["prcp"],
            "_lon": lon,
            "_lat": lat,
        })
    return records


# ---------------------------------------------------------------------------
# Records -> FlatGeobuf.
# ---------------------------------------------------------------------------


def _records_to_fgb(records: list[dict[str, Any]]) -> bytes:
    """Serialize normals records to FlatGeobuf bytes (point geometry).

    Raises:
        ``ClimateNormalsUpstreamError`` — geopandas/shapely/pyogrio missing
          or the FlatGeobuf write fails.
        ``ClimateNormalsEmptyError`` — zero records.
    """
    if not records:
        raise ClimateNormalsEmptyError(
            "No 1991-2020 Climate Normals stations with annual normals were "
            "found in the requested bbox"
        )

    try:
        import geopandas as gpd  # type: ignore[import-not-found]
        from shapely.geometry import Point  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ClimateNormalsUpstreamError(
            f"geopandas / shapely not available: {exc}"
        ) from exc

    geom = [Point(r["_lon"], r["_lat"]) for r in records]
    props = [
        {k: v for k, v in r.items() if k not in ("_lon", "_lat")}
        for r in records
    ]
    gdf = gpd.GeoDataFrame(props, geometry=geom, crs="EPSG:4326")

    logger.info(
        "fetch_climate_normals: %d station(s); %d with temp normals",
        len(gdf),
        sum(1 for r in records if r["normal_temp_f"] is not None),
    )

    tmp_fgb: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="grace2_normals_"
        ) as f:
            tmp_fgb = f.name
        try:
            gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        except Exception as exc:
            raise ClimateNormalsUpstreamError(
                f"FlatGeobuf write failed for {len(gdf)} stations: {exc}"
            ) from exc
        with open(tmp_fgb, "rb") as f:
            return f.read()
    finally:
        if tmp_fgb is not None:
            try:
                os.unlink(tmp_fgb)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Input validation.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    if len(bbox) != 4:
        raise ClimateNormalsInputError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    w, s, e, n = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise ClimateNormalsInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= w <= 180.0 and -180.0 <= e <= 180.0):
        raise ClimateNormalsInputError(
            f"bbox lon values out of [-180, 180]: {bbox!r}"
        )
    if not (-90.0 <= s <= 90.0 and -90.0 <= n <= 90.0):
        raise ClimateNormalsInputError(
            f"bbox lat values out of [-90, 90]: {bbox!r}"
        )
    if w >= e or s >= n:
        raise ClimateNormalsInputError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )


# ---------------------------------------------------------------------------
# Top-level fetch bytes (passed to read_through).
# ---------------------------------------------------------------------------


def _fetch_climate_normals_bytes(
    bbox: tuple[float, float, float, float],
) -> bytes:
    """End-to-end: discover stations -> fetch normals -> FlatGeobuf bytes."""
    stations = _discover_stations_in_bbox(bbox)
    if not stations:
        raise ClimateNormalsEmptyError(
            f"No 1991-2020 Climate Normals stations found inside bbox={bbox}; "
            "the NCEI Normals footprint is the U.S. + territories — widen the "
            "bbox or move it over U.S. land"
        )
    logger.info(
        "fetch_climate_normals: %d candidate station(s) in bbox", len(stations)
    )
    records = _fetch_station_normals(stations)
    return _records_to_fgb(records)


# ---------------------------------------------------------------------------
# Registered atomic tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    supports_global_query=False,
    payload_mb_estimator_name="estimate_payload_mb",
    # Annotations: readOnlyHint=True (read-only; no state mutation),
    # openWorldHint=True (calls external public NCEI endpoint),
    # destructiveHint=False, idempotentHint=True (cache shim deduplicates).
    open_world_hint=True,
)
def fetch_climate_normals(
    bbox: tuple[float, float, float, float],
    # Absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch NOAA NCEI 1991-2020 U.S. Climate Normals as a point FlatGeobuf.

    Retrieves the official 30-year (1991-2020) Climate Normals from the NOAA
    National Centers for Environmental Information (NCEI) for every Normals
    station inside the requested bbox. The Climate Normals are the canonical
    U.S. climate baseline: long-term average annual temperature (deg F) and
    annual total precipitation (inches), computed from GHCN-Daily station
    records. They answer "what is climatologically normal here?" and are the
    reference against which an observed event is judged anomalous.

    When to use:
      - User asks for the climate baseline / normal / average conditions of a
        place ("what's the average annual temperature in Tampa?", "normal
        yearly rainfall around Phoenix", "climate normals for this county").
      - Anomaly framing: establishing the long-term baseline so an observed
        heat wave, cold snap, drought, or wet year can be compared against
        "normal" (pair with fetch_asos_metar / fetch_era5_reanalysis for the
        observed side).
      - Multi-station spatial overlay of average temperature or precipitation
        across a region.

    When NOT to use:
      - Current or historical *observed* weather — use fetch_asos_metar (ASOS
        station observations) or fetch_era5_reanalysis (gridded reanalysis).
      - Forecasts — use fetch_nws_alerts_conus / fetch_hrrr_forecast.
      - Gridded precipitation climatology over large or non-US areas — use
        fetch_chirps_precipitation (global CHIRPS).
      - Non-US regions — the NCEI Normals footprint is the U.S. + territories
        only (supports_global_query=False).

    Parameters:
        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326. Required.
            Every NCEI Normals station whose coordinates fall inside this bbox
            is selected (capped at 120 stations). Example for the Tampa Bay
            area: ``(-82.7, 27.7, -82.2, 28.2)``.

    Returns:
        ``LayerURI`` pointing at a FlatGeobuf in the cache bucket:
        ``s3://<cache-bucket>/cache/static-30d/climate_normals/<key>.fgb``
        - ``layer_type="vector"``, ``role="context"``, ``units="mixed"``
          (temperature in deg F, precipitation in inches).
        - Geometry: Point at each station's coordinates, EPSG:4326.
        - Properties per station: ``station_id`` (GHCN id), ``name``,
          ``elevation_m``, ``normal_temp_f`` (annual average temperature,
          deg F), ``normal_tmin_f`` (annual avg daily minimum, deg F),
          ``normal_tmax_f`` (annual avg daily maximum, deg F),
          ``normal_precip_in`` (annual total precipitation, inches). A station
          that is precipitation-only (e.g. CoCoRaHS) reports
          ``normal_temp_f=None``; missing values are ``null``.

    Cache: ``static-30d`` — the 1991-2020 Normals are a fixed published
    product; identical ``bbox`` (rounded to 4 dp) reuses the cached FlatGeobuf.

    Cross-tool dependencies:
        - Composes WITH: ``publish_layer`` (map overlay), ``geocode_location``
          (bbox derivation from a place name),
          ``aggregate_claims_across_sources`` (baseline-vs-observed framing).
        - Complements: ``fetch_asos_metar`` (observed surface weather),
          ``fetch_era5_reanalysis`` (gridded reanalysis),
          ``fetch_chirps_precipitation`` (global precipitation climatology).
        - Upstream: NCEI Climate Normals annual/seasonal access files +
          inventory (www.ncei.noaa.gov/data/normals-annualseasonal/1991-2020).

    Errors (FR-AS-11 typed-error surface):
        - ``ClimateNormalsInputError``: invalid bbox (retryable=False).
        - ``ClimateNormalsUpstreamError``: NCEI network failure or malformed
          inventory/CSV (retryable=True).
        - ``ClimateNormalsEmptyError``: no Normals stations in bbox / none with
          usable annual normals (retryable=False).

    Source-tier: FR-HEP-2 Tier 1 (NOAA NCEI official 30-year Climate Normals).
    Claims should be marked ``source_authority_tier=1`` in ``ClaimSet``.

    supports_global_query=False — NCEI Normals cover the U.S. + territories.
    """
    # 1. Validate and normalize inputs.
    if not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
        raise ClimateNormalsInputError(
            f"bbox must be a 4-element tuple (min_lon, min_lat, max_lon, "
            f"max_lat); got {bbox!r}"
        )
    bbox = tuple(float(v) for v in bbox)  # type: ignore[assignment]
    _validate_bbox(bbox)  # type: ignore[arg-type]

    # Round bbox to 4 dp for stable cache keying.
    bbox_r = tuple(round(v, 4) for v in bbox)  # type: ignore[assignment]

    # 2. Build cache params.
    params: dict[str, Any] = {"bbox": list(bbox_r)}

    # 3. Read-through cache.
    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=lambda: _fetch_climate_normals_bytes(bbox_r),  # type: ignore[arg-type]
    )
    assert result.uri is not None, (
        "fetch_climate_normals is cacheable; uri must be set by read_through"
    )

    # 4. Build descriptive layer name + stable id.
    bbox_tag = (
        f"{bbox_r[0]:.2f},{bbox_r[1]:.2f}->{bbox_r[2]:.2f},{bbox_r[3]:.2f}"
    )
    seed = hashlib.sha256(
        json.dumps(params, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:8]

    return LayerURI(
        layer_id=f"climate-normals-{seed}",
        name=f"Climate Normals 1991-2020 — {bbox_tag}",
        layer_type="vector",
        uri=result.uri,
        style_preset="climate_normals",
        role="context",
        units="mixed",
        bbox=bbox_r,
    )
