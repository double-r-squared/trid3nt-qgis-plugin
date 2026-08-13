"""Hurricane / tropical-cyclone track delegate hooks (ADR 0111): the ``fetch_storm_tracks`` fold.

``fetch_storm_tracks`` folds onto the router as a ``library_delegate`` VECTOR source
carrying BOTH of its modes under one name. The decisive blocker ADR 0090 named was the
ACTIVE mode's second fetch round: a BINARY zip-shapefile (the NHC forecast-track GIS
product) that must be extracted + read via geopandas + reprojected -- I/O inside what
would be a PURE enrich hook, which no chained-resolution phase carries. The fold
expresses that binary-secondary-enrichment as the SANCTIONED delegate socket impurity
(the topobathy precedent): the delegate hook owns BOTH network rounds, so the second
round's zip read is just more delegate I/O, not a new executor phase. The whole tool's
bespoke body lives here as the delegate:

  * ``storm_tracks.validate`` (delegate_validate) -- the historical-mode bbox-required
    gate + geometry / storm_name shape checks the declarative param surface cannot
    express (bbox is required for historical, OPTIONAL for active), raised pre-cache /
    pre-network as a ``StormTracksInputError``.
  * ``storm_tracks.resolve`` (pre_resolve) -- canonicalize ``storm_name`` (upper) and
    resolve the historical season window (default = the last 3 seasons) BEFORE
    read_through so the resolved years enter the cache key (a default-year request is
    deterministic per day and refreshes yearly, the twin's contract).
  * ``storm_tracks.read`` (delegate) -- branch on ``active_only``: HISTORICAL subsets
    the IBTrACS v04r01 archive (basin CSV -> storm-wise full-track selection -> line /
    point features); ACTIVE resolves NHC CurrentStorms.json then, per storm, fetches
    the ``forecastTrack.zipFile`` binary, extracts ``*_pts.shp`` to a tempdir, reads it
    via geopandas, and reprojects to EPSG:4326. Returns GeoJSON features for the shared
    ``vector_fgb`` serializer, and RECORDS the fetch-time mode provenance (ADR 0110).
  * ``storm_tracks.envelope`` -- the twin's exact ``storm-tracks-{seed}`` layer_id +
    ``Storm tracks - <mode> (<scope>)`` name, plus the mode / storm-attribution
    provenance read back from the channel (declared defaults on a pre-channel cache
    object).

The ``StormTracks*Error`` classes live HERE (their stable importable home now that the
coded twin is deleted). Their base is ``FetchError`` so ``library_delegate.invoke``
passes them through unchanged (its ``except FetchError: raise`` passthrough, ADR 0097),
preserving the pinned ``error_code`` through the delegate wrapper.
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
import urllib.request
import zipfile
from typing import Any

from trid3nt_server.agent.tools.cache import record_provenance

from ..._fetch_common import FetchError
from . import register_hook

logger = logging.getLogger(
    "trid3nt_server.agent.tools.fetchers._router.hooks.storm_tracks"
)

__all__ = [
    "StormTracksError",
    "StormTracksInputError",
    "StormTracksUpstreamError",
    "StormTracksNoStormsError",
    "StormTracksNoActiveStormsError",
    "estimate_payload_mb",
    "validate_storm_tracks",
    "resolve_storm_tracks",
    "read_storm_tracks",
    "envelope_storm_tracks",
    "IBTRACS_CSV_BASE",
    "NHC_CURRENT_STORMS_URL",
]


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface). Base = FetchError so the pinned
# error_code survives library_delegate.invoke's passthrough (ADR 0097).
# ---------------------------------------------------------------------------


class StormTracksError(FetchError):
    """Base class for fetch_storm_tracks failures."""

    error_code: str = "STORM_TRACKS_ERROR"
    retryable: bool = True


class StormTracksInputError(StormTracksError):
    """Invalid inputs - bad bbox, bad year range, bad geometry mode."""

    error_code = "STORM_TRACKS_INPUT_ERROR"
    retryable = False


class StormTracksUpstreamError(StormTracksError):
    """NCEI / NHC request failed (network error, HTTP 5xx, bad body)."""

    error_code = "STORM_TRACKS_UPSTREAM_ERROR"
    retryable = True


class StormTracksNoStormsError(StormTracksError):
    """No historical storm track touched the bbox / year range / name filter."""

    error_code = "STORM_TRACKS_NO_STORMS"
    retryable = False


class StormTracksNoActiveStormsError(StormTracksError):
    """NHC is advising on zero active storms (or none match the filter)."""

    error_code = "STORM_TRACKS_NO_ACTIVE_STORMS"
    retryable = False


# ---------------------------------------------------------------------------
# Constants (twin-identical).
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
    "EP": [(-180.0, 0.0, -92.0, 60.0), (-92.0, 0.0, -77.0, 15.0)],
    "WP": [(95.0, 0.0, 180.0, 65.0)],
    "NI": [(30.0, 0.0, 100.0, 35.0)],
    "SI": [(10.0, -55.0, 135.0, 0.0)],
    "SP": [(135.0, -55.0, 180.0, 0.0), (-180.0, -55.0, -60.0, 0.0)],
    "SA": [(-70.0, -55.0, 20.0, 0.0)],
}

#: Never download more than this many per-basin CSVs in one call.
_MAX_BASIN_FILES = 2

#: User-Agent per NOAA usage guidance.
_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)

#: HTTP timeout (seconds). Per-basin IBTrACS CSVs are up to ~60 MB.
_HTTP_TIMEOUT = 300.0

#: Cap on emitted point features (points mode).
_MAX_POINT_FEATURES = 50000


# ---------------------------------------------------------------------------
# Payload estimator (kept importable for tests; the router synthesizes its own
# from source.yaml's payload_estimate block for the promoted tool).
# ---------------------------------------------------------------------------


def estimate_payload_mb(
    bbox: tuple[float, float, float, float] | None = None,
    start_year: int | None = None,
    end_year: int | None = None,
    active_only: bool = False,
    geometry: str = "lines",
    **_kw: Any,
) -> float:
    """Estimate the output FlatGeobuf size in MB."""
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
    n_storms = max(1.0, area_sq_deg * n_years * 0.005)
    if geometry == "points":
        return max(0.001, n_storms * 80 * 300 / 1_000_000.0)
    return max(0.001, n_storms * 2000 / 1_000_000.0)


# ---------------------------------------------------------------------------
# Input validation + year resolution.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
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


def _resolve_years(
    start_year: int | None,
    end_year: int | None,
) -> tuple[int, int]:
    """Resolve the (start, end) season range. Default = the last 3 seasons."""
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
    """Pick the smallest adequate IBTrACS CSV file set for bbox + year range."""
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
# HTTP helper (the delegate owns its own socket, the sanctioned impurity).
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
    """Parse an IBTrACS points CSV -> {sid: [fix, ...]} filtered by season + name."""
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception as exc:  # pragma: no cover
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
    """Keep storms whose track has at least one fix inside the bbox (FULL track kept)."""
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
    """Parse NHC CurrentStorms.json -> one record per active storm."""
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

    This is the BINARY-SECONDARY-ENRICHMENT round (ADR 0090/0111): the delegate
    fetches the ``forecastTrack`` zip, extracts ``*_pts.shp`` to a tempdir, and reads
    it via geopandas + reproject -- I/O the delegate socket sanctions. Any failure
    returns ``[]`` (the caller degrades to current-position-only, never fabricates).
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
        tmpdir = tempfile.TemporaryDirectory(prefix="trid3nt_nhc_")
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
# GeoJSON feature builders (delegate returns features; vector_fgb serializes).
# ---------------------------------------------------------------------------


def _line_features(
    storms: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """One LineString feature per storm (fixes in time order)."""
    feats: list[dict[str, Any]] = []
    n_dropped = 0
    for sid, fixes in sorted(storms.items()):
        if len(fixes) < 2:
            n_dropped += 1
            continue
        winds = [f["wind_kt"] for f in fixes if f["wind_kt"] is not None]
        press = [f["pres_mb"] for f in fixes if f["pres_mb"] is not None]
        cats = [f["category"] for f in fixes if f["category"] is not None]
        max_cat = max(cats) if cats else None
        feats.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[f["lon"], f["lat"]] for f in fixes],
                },
                "properties": {
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
                },
            }
        )
    if n_dropped:
        logger.info(
            "fetch_storm_tracks: dropped %d single-fix storm(s) in lines mode",
            n_dropped,
        )
    if not feats:
        raise StormTracksNoStormsError(
            "Every matching storm has a single best-track fix - too short to "
            "draw as a line. Re-issue with geometry='points'."
        )
    return feats


def _point_features(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One Point feature per record (EPSG:4326)."""
    feats: list[dict[str, Any]] = []
    for r in records:
        props = {
            k: v
            for k, v in r.items()
            if k not in ("lat", "lon", "forecast_track_zip")
        }
        feats.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
                "properties": props,
            }
        )
    return feats


# ---------------------------------------------------------------------------
# The two fetch modes (delegate body).
# ---------------------------------------------------------------------------


def _fetch_historical(
    *,
    bbox: tuple[float, float, float, float],
    y0: int,
    y1: int,
    storm_name: str | None,
    geometry: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Historical IBTrACS path -> (features, storm_names)."""
    files = _select_ibtracs_files(bbox, y0, y1)
    storms: dict[str, list[dict[str, Any]]] = {}
    for fn in files:
        url = IBTRACS_CSV_BASE + fn
        logger.info("fetch_storm_tracks: GET %s", url)
        raw = _http_get(url)
        parsed = _parse_ibtracs_csv(raw, y0=y0, y1=y1, storm_name=storm_name)
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

    names = sorted(
        {(fixes[0].get("name") or sid) for sid, fixes in selected.items()}
    )

    if geometry == "points":
        all_fixes = [f for fixes in selected.values() for f in fixes]
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
        return _point_features(rows), names
    return _line_features(selected), names


def _fetch_active(
    *,
    bbox: tuple[float, float, float, float] | None,
    storm_name: str | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Active NHC path -> (features, storm_names). Always points."""
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

    names = sorted({(s.get("name") or s.get("id") or "storm") for s in storms})
    return _point_features(records), names


# ---------------------------------------------------------------------------
# HOOK: delegate_validate -- historical bbox-required + shape gate (pre-cache).
# ---------------------------------------------------------------------------


@register_hook("storm_tracks.validate")
def validate_storm_tracks(spec: Any, params: dict[str, Any]) -> None:
    """Pre-cache input gate: historical requires bbox; geometry/storm_name shape."""
    geometry = params.get("geometry", "lines")
    if geometry not in ("lines", "points"):
        raise StormTracksInputError(
            f"geometry must be 'lines' or 'points'; got {geometry!r}"
        )
    storm_name = params.get("storm_name")
    if storm_name is not None and not isinstance(storm_name, str):
        raise StormTracksInputError(
            f"storm_name must be a string; got {type(storm_name).__name__}"
        )
    bbox = params.get("bbox")
    if bbox is not None:
        _validate_bbox(tuple(float(v) for v in bbox))
    if not bool(params.get("active_only", False)) and bbox is None:
        raise StormTracksInputError(
            "fetch_storm_tracks historical mode requires "
            "bbox=(west, south, east, north) in EPSG:4326 - it bounds the "
            "IBTrACS archive subset. (Only active_only=True may omit it.)"
        )


# ---------------------------------------------------------------------------
# HOOK: pre_resolve -- storm_name canon + historical year resolution (pre-key).
# ---------------------------------------------------------------------------


@register_hook("storm_tracks.resolve")
def resolve_storm_tracks(spec: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Merge the canonical storm_name + resolved season window before read_through."""
    out: dict[str, Any] = {}
    name = params.get("storm_name")
    if isinstance(name, str) and name.strip():
        out["storm_name"] = name.strip().upper()
    if not bool(params.get("active_only", False)):
        y0, y1 = _resolve_years(params.get("start_year"), params.get("end_year"))
        out["start_year"] = y0
        out["end_year"] = y1
    return out


# ---------------------------------------------------------------------------
# HOOK: delegate -- branch on active_only; fetch; record provenance; features.
# ---------------------------------------------------------------------------


@register_hook("storm_tracks.read")
def read_storm_tracks(
    spec: Any, params: dict[str, Any], *, timeout_s: float
) -> list[dict[str, Any]]:
    """Fetch storm tracks (both modes); RECORD mode provenance (ADR 0110); return features."""
    bbox = params.get("bbox")
    resolved_bbox = tuple(float(v) for v in bbox) if bbox is not None else None
    name_canon = params.get("storm_name")
    if isinstance(name_canon, str):
        name_canon = name_canon.strip().upper() or None
    else:
        name_canon = None

    if bool(params.get("active_only", False)):
        feats, names = _fetch_active(bbox=resolved_bbox, storm_name=name_canon)
        mode = "active"
    else:
        y0, y1 = _resolve_years(params.get("start_year"), params.get("end_year"))
        feats, names = _fetch_historical(
            bbox=resolved_bbox,  # type: ignore[arg-type]
            y0=y0,
            y1=y1,
            storm_name=name_canon,
            geometry=str(params.get("geometry", "lines")),
        )
        mode = "historical"

    record_provenance(
        {"mode": mode, "storm_count": len(names), "storm_names": names}
    )
    return feats


# ---------------------------------------------------------------------------
# HOOK: envelope -- twin layer_id/name + mode provenance (channel replay).
# ---------------------------------------------------------------------------


@register_hook("storm_tracks.envelope")
def envelope_storm_tracks(
    spec: Any,
    params: dict[str, Any],
    layer: Any,
    data: bytes | None,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the twin's exact layer_id / name + the mode-provenance fields (ADR 0110)."""
    active_only = bool(params.get("active_only", False))
    name_canon = params.get("storm_name")
    if isinstance(name_canon, str):
        name_canon = name_canon.strip().upper() or None
    else:
        name_canon = None

    # Reconstruct the twin's params-hash seed (deterministic from validated params).
    if active_only:
        seed_params: dict[str, Any] = {
            "mode": "active",
            "bbox": list(params["bbox"]) if params.get("bbox") is not None else None,
            "storm_name": name_canon,
        }
        scope_tag = "active (NHC)"
        mode_tag = "NHC active storms"
    else:
        y0, y1 = _resolve_years(params.get("start_year"), params.get("end_year"))
        seed_params = {
            "mode": "historical",
            "bbox": list(params["bbox"]),
            "start_year": y0,
            "end_year": y1,
            "storm_name": name_canon,
            "geometry": str(params.get("geometry", "lines")),
        }
        scope_tag = f"{y0}..{y1}"
        mode_tag = "IBTrACS tracks"

    seed = hashlib.sha256(
        json.dumps(seed_params, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:8]
    if name_canon:
        scope_tag = f"{name_canon} {scope_tag}"
    name = f"Storm tracks - {mode_tag} ({scope_tag})"

    prov = provenance or {}
    return {
        "layer_id": f"storm-tracks-{seed}",
        "name": name,
        "mode": str(prov.get("mode", "active" if active_only else "historical")),
        "storm_count": int(prov.get("storm_count", 0)),
        "storm_names": list(prov.get("storm_names", []) or []),
    }
