"""``fetch_noaa_nwm_streamflow`` atomic tool — NOAA National Water Model streamflow (job A3).
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import logging
import math
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Literal

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.cache import read_through

__all__ = ["fetch_noaa_nwm_streamflow"]

logger = logging.getLogger("trid3nt_server.tools.fetchers.hydrology.fetch_noaa_nwm_streamflow")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class NWMStreamflowError(RuntimeError):
    """Base class for fetch_noaa_nwm_streamflow failures.

    ``error_code`` maps to the WebSocket A.6 error frame emitted by the agent
    surface. ``retryable`` guides FR-AS-11 retry logic.
    """

    error_code: str = "NWM_STREAMFLOW_ERROR"
    retryable: bool = True


class NWMStreamflowInputError(NWMStreamflowError):
    """Bad inputs (malformed bbox, unknown product, bad forecast_hour, bad date)."""

    error_code = "NWM_STREAMFLOW_INPUT_ERROR"
    retryable = False


class NWMStreamflowUpstreamError(NWMStreamflowError):
    """NOAA NWM S3 download or netCDF parse failed."""

    error_code = "NWM_STREAMFLOW_UPSTREAM_ERROR"
    retryable = True


class NWMStreamflowNotAvailableError(NWMStreamflowError):
    """Requested cycle has no published file (gap, future date, retention window)."""

    error_code = "NWM_STREAMFLOW_NOT_AVAILABLE"
    retryable = False


class NWMStreamflowEmptyError(NWMStreamflowError):
    """No NHDPlus reaches discovered inside the requested bbox.

    Either the bbox falls in an area with no NHDPlus coverage (offshore,
    ungauged headwater) or NLDI returned no snapped COMIDs for any sample
    point in the 5×5 grid.
    """

    error_code = "NWM_STREAMFLOW_EMPTY"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: NOAA NWM public S3 bucket (open access, no auth).
_S3_BASE = "https://noaa-nwm-pds.s3.amazonaws.com"

#: USGS NLDI base (used to discover COMIDs + geometries for the bbox subset).
_NLDI_BASE = "https://api.water.usgs.gov/nldi"

#: Supported NWM products. v0.1 surfaces analysis_assim + short_range; the
#: medium/long-range ensembles are surfaced as ``OQ-A3-NWM-ENSEMBLES`` for a
#: future expansion since they require ensemble-member resolution.
_VALID_PRODUCTS: frozenset[str] = frozenset(
    {"analysis_assim", "short_range"}
)

#: CONUS bounding box (EPSG:4326) — the NHDPlus v2.1 CONUS domain.
_CONUS_BBOX: tuple[float, float, float, float] = (-130.0, 20.0, -60.0, 55.0)

#: User-Agent per AWS Open Data + NOAA usage guidelines.
_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)

#: HTTP timeouts (seconds).
_LIST_TIMEOUT = 30.0
_DOWNLOAD_TIMEOUT = 300.0  # ~14 MB netCDF, generous pad for slow links
_NLDI_TIMEOUT = 20.0

#: NLDI point-sample grid density (5×5 = 25 probes per bbox call).
#: Each probe returns at most one snapped COMID. We dedupe + expand via
#: navigation up to ``_NLDI_NAV_DEPTH`` reaches.
_NLDI_SAMPLE_GRID = 5
_NLDI_NAV_DEPTH = 25  # max upstream reaches per seed COMID

#: Cap on total reaches we'll attempt to materialize (bounds API spend).
_MAX_REACHES = 500


# ---------------------------------------------------------------------------
# AtomicToolMetadata — registered once at import time.
# ---------------------------------------------------------------------------


def _build_metadata() -> AtomicToolMetadata:
    """Construct AtomicToolMetadata defensively against schema flag variants."""
    common = dict(
        name="fetch_noaa_nwm_streamflow",
        ttl_class="dynamic-1h",
        source_class="nwm_streamflow",
        cacheable=True,
    )
    try:
        return AtomicToolMetadata(**common, supports_global_query=False)  # type: ignore[call-arg]
    except Exception:  # pydantic ValidationError when field absent
        logger.debug(
            "AtomicToolMetadata does not support supports_global_query; "
            "registering fetch_noaa_nwm_streamflow without it"
        )
        return AtomicToolMetadata(**common)


_METADATA = _build_metadata()


# ---------------------------------------------------------------------------
# Payload-MB estimator (Wave 1.5 chat-warning system).
# ---------------------------------------------------------------------------


def estimate_payload_mb(
    bbox: tuple[float, float, float, float] | None = None,
    product: str = "analysis_assim",
    valid_time: str | None = None,
    forecast_hour: int = 0,
    **_kw: Any,
) -> float:
    """Estimate output FlatGeobuf size in MB.

    NWM channel_rt netCDF download is ~14 MB but the emitted FlatGeobuf only
    carries the subset of reaches in the bbox. Typical bbox-subset sizes:

    - 1° × 1° bbox (~Fort Myers / a metro area) → ~50-200 reaches → ~10 KB
    - 5° × 5° bbox (~Florida) → ~5,000 reaches → ~500 KB
    - Full CONUS subset cap (_MAX_REACHES = 500) → ~50 KB

    We deliberately ignore the 14 MB upstream download from the user-facing
    estimate because the cache hit path returns only the small FlatGeobuf
    payload. The estimate is therefore the **output** size only.
    """
    if bbox is None:
        return 1.0  # CONUS not supported; bbox required
    try:
        west, south, east, north = bbox
        sq_deg = max(0.0, (east - west)) * max(0.0, (north - south))
    except (TypeError, ValueError):
        return 1.0
    # ~100 reaches per 1° square, ~100 bytes per reach in FlatGeobuf →
    # 10 KB per 1° square. Convert to MB.
    reaches = min(_MAX_REACHES, int(sq_deg * 100.0))
    return max(0.01, reaches * 100 / 1_000_000.0)


# ---------------------------------------------------------------------------
# bbox + date helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Raise ``NWMStreamflowInputError`` if the bbox is degenerate or out of range."""
    if len(bbox) != 4:
        raise NWMStreamflowInputError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise NWMStreamflowInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise NWMStreamflowInputError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise NWMStreamflowInputError(f"bbox lat out of [-90,90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise NWMStreamflowInputError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )
    # Sanity: must intersect CONUS.
    if max_lon < _CONUS_BBOX[0] or min_lon > _CONUS_BBOX[2]:
        raise NWMStreamflowInputError(
            f"bbox {bbox} does not intersect NWM CONUS domain {_CONUS_BBOX}"
        )
    if max_lat < _CONUS_BBOX[1] or min_lat > _CONUS_BBOX[3]:
        raise NWMStreamflowInputError(
            f"bbox {bbox} does not intersect NWM CONUS domain {_CONUS_BBOX}"
        )


def _round_bbox_to_6dp(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    return tuple(round(v, 6) for v in bbox)  # type: ignore[return-value]


def _parse_valid_time(valid_time: str | None) -> _dt.datetime | None:
    """Parse the ``valid_time`` ISO-8601 UTC string. None means "latest available"."""
    if valid_time is None:
        return None
    if not isinstance(valid_time, str):
        raise NWMStreamflowInputError(
            f"valid_time must be a string; got {type(valid_time).__name__}"
        )
    s = valid_time.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = _dt.datetime.fromisoformat(s)
    except ValueError as exc:
        raise NWMStreamflowInputError(
            f"valid_time={valid_time!r} is not a parseable ISO-8601 string"
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt.astimezone(_dt.timezone.utc)


# ---------------------------------------------------------------------------
# HTTP helpers.
# ---------------------------------------------------------------------------


def _http_get(url: str, timeout: float) -> bytes:
    """Plain HTTP GET. Raises ``NWMStreamflowUpstreamError`` on failure."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise NWMStreamflowUpstreamError(
            f"upstream HTTP {exc.code} for {url}"
        ) from exc
    except urllib.error.URLError as exc:
        raise NWMStreamflowUpstreamError(
            f"network error for {url}: {exc.reason}"
        ) from exc
    except TimeoutError as exc:
        raise NWMStreamflowUpstreamError(
            f"timed out after {timeout}s for {url}"
        ) from exc


def _list_s3_prefixes(prefix: str, max_keys: int = 1000) -> list[str]:
    """List CommonPrefixes under ``prefix`` (single bucket-list call)."""
    url = (
        f"{_S3_BASE}/?list-type=2"
        f"&prefix={urllib.parse.quote(prefix)}"
        f"&delimiter=/&max-keys={max_keys}"
    )
    body = _http_get(url, timeout=_LIST_TIMEOUT).decode("utf-8", errors="replace")
    return re.findall(r"<Prefix>([^<]+)</Prefix>", body)


def _list_s3_keys(prefix: str, max_keys: int = 1000) -> list[str]:
    url = (
        f"{_S3_BASE}/?list-type=2"
        f"&prefix={urllib.parse.quote(prefix)}"
        f"&max-keys={max_keys}"
    )
    body = _http_get(url, timeout=_LIST_TIMEOUT).decode("utf-8", errors="replace")
    return re.findall(r"<Key>([^<]+)</Key>", body)


# ---------------------------------------------------------------------------
# NWM file resolution.
# ---------------------------------------------------------------------------


def _latest_nwm_date() -> str:
    """Find the most-recent ``nwm.YYYYMMDD/`` prefix in the bucket.

    Bucket retention is ~30 days. We page once and pick the lexicographically
    largest prefix matching ``nwm.YYYYMMDD/``.
    """
    # Probe a date range starting from "today" and scan backwards. The bucket
    # holds ~30 days of data; we walk back at most 35 days.
    today = _dt.datetime.now(_dt.timezone.utc).date()
    for days_back in range(0, 35):
        candidate = today - _dt.timedelta(days=days_back)
        prefix = f"nwm.{candidate.strftime('%Y%m%d')}/"
        # Cheap presence probe: list with this exact prefix and delimiter.
        keys = _list_s3_keys(prefix, max_keys=1)
        if keys:
            return candidate.strftime("%Y%m%d")
    raise NWMStreamflowNotAvailableError(
        "no NWM cycles found in the last 35 days; bucket may be down "
        "or retention window changed"
    )


def _resolve_nwm_key(
    product: str,
    valid_time: _dt.datetime | None,
    forecast_hour: int,
) -> tuple[str, _dt.datetime]:
    """Resolve the S3 key for the requested ``(product, valid_time, fhour)``.

    For ``analysis_assim``: filename is
        ``nwm.tHHz.analysis_assim.channel_rt.tm00.conus.nc``
    For ``short_range``: filename is
        ``nwm.tHHz.short_range.channel_rt.fNNN.conus.nc``
    """
    if valid_time is None:
        date_str = _latest_nwm_date()
        # For latest, pick the most recent hour with data.
        prefix = f"nwm.{date_str}/{product}/"
        keys = _list_s3_keys(prefix, max_keys=200)
        # Filter to channel_rt files matching our forecast_hour.
        if product == "analysis_assim":
            matcher = re.compile(r"\.t(\d{2})z\.analysis_assim\.channel_rt\.tm00\.conus\.nc$")
        else:  # short_range
            matcher = re.compile(
                rf"\.t(\d{{2}})z\.short_range\.channel_rt\.f{forecast_hour:03d}\.conus\.nc$"
            )
        candidates: list[tuple[str, str]] = []
        for k in keys:
            m = matcher.search(k)
            if m:
                candidates.append((m.group(1), k))
        if not candidates:
            raise NWMStreamflowNotAvailableError(
                f"no {product} channel_rt files found for date={date_str} "
                f"forecast_hour={forecast_hour}"
            )
        # Pick the latest cycle.
        candidates.sort()
        cycle_hh, latest_key = candidates[-1]
        resolved_dt = _dt.datetime(
            int(date_str[0:4]),
            int(date_str[4:6]),
            int(date_str[6:8]),
            int(cycle_hh),
            tzinfo=_dt.timezone.utc,
        )
        # For short_range, valid_time = cycle + forecast_hour
        if product == "short_range":
            resolved_dt = resolved_dt + _dt.timedelta(hours=forecast_hour)
        return latest_key, resolved_dt

    # Targeted valid_time.
    cycle_dt = valid_time.replace(minute=0, second=0, microsecond=0)
    if product == "short_range":
        cycle_dt = cycle_dt - _dt.timedelta(hours=forecast_hour)

    date_str = cycle_dt.strftime("%Y%m%d")
    cycle_hh = cycle_dt.strftime("%H")
    if product == "analysis_assim":
        key = (
            f"nwm.{date_str}/analysis_assim/"
            f"nwm.t{cycle_hh}z.analysis_assim.channel_rt.tm00.conus.nc"
        )
    else:
        key = (
            f"nwm.{date_str}/short_range/"
            f"nwm.t{cycle_hh}z.short_range.channel_rt.f{forecast_hour:03d}.conus.nc"
        )
    # Probe presence.
    probe = _list_s3_keys(key, max_keys=1)
    if not probe or key not in probe:
        raise NWMStreamflowNotAvailableError(
            f"NWM file not found: {key}; may be outside the bucket "
            f"retention window (~30 days) or not yet published"
        )
    return key, valid_time


# ---------------------------------------------------------------------------
# NLDI bbox-sampling → list of (feature_id, point_geometry).
# ---------------------------------------------------------------------------


def _nldi_snap_point(lon: float, lat: float) -> int | None:
    """Snap (lon, lat) to nearest NHDPlus reach via NLDI; return COMID or None.

    Errors are swallowed silently and return None so a failed sample point
    doesn't abort the whole bbox discovery.
    """
    url = (
        f"{_NLDI_BASE}/linked-data/comid/position"
        f"?coords=POINT({lon}%20{lat})"
    )
    try:
        body = _http_get(url, timeout=_NLDI_TIMEOUT).decode("utf-8")
    except NWMStreamflowUpstreamError:
        return None
    try:
        import json as _json
        obj = _json.loads(body)
        feats = obj.get("features", [])
        if not feats:
            return None
        comid = feats[0].get("properties", {}).get("comid")
        if comid is None:
            return None
        return int(comid)
    except (ValueError, KeyError, TypeError):
        return None


def _nldi_get_reach_geometry(comid: int) -> list[tuple[float, float]] | None:
    """Fetch the LineString geometry for ``comid`` from NLDI. Returns coords or None."""
    url = f"{_NLDI_BASE}/linked-data/comid/{comid}"
    try:
        body = _http_get(url, timeout=_NLDI_TIMEOUT).decode("utf-8")
    except NWMStreamflowUpstreamError:
        return None
    try:
        import json as _json
        obj = _json.loads(body)
        feats = obj.get("features", [])
        if not feats:
            return None
        geom = feats[0].get("geometry", {})
        if geom.get("type") != "LineString":
            return None
        coords = geom.get("coordinates", [])
        if not coords:
            return None
        return [(float(c[0]), float(c[1])) for c in coords]
    except (ValueError, KeyError, TypeError, IndexError):
        return None


def _discover_comids_in_bbox(
    bbox: tuple[float, float, float, float],
) -> list[int]:
    """Sample a 5×5 grid inside bbox, snap each point to NHDPlus, dedupe.

    Returns a list of COMIDs (NHDPlus v2.1 identifiers). Capped at
    ``_MAX_REACHES`` so even a dense urban bbox cannot exhaust NLDI.
    """
    west, south, east, north = bbox
    found: set[int] = set()
    for i in range(_NLDI_SAMPLE_GRID):
        for j in range(_NLDI_SAMPLE_GRID):
            # Offset interior so corners don't fall on the bbox edge
            u = (i + 0.5) / _NLDI_SAMPLE_GRID
            v = (j + 0.5) / _NLDI_SAMPLE_GRID
            lon = west + (east - west) * u
            lat = south + (north - south) * v
            comid = _nldi_snap_point(lon, lat)
            if comid is not None:
                found.add(comid)
                if len(found) >= _MAX_REACHES:
                    return list(found)
    return list(found)


# ---------------------------------------------------------------------------
# netCDF → streamflow lookup.
# ---------------------------------------------------------------------------


def _load_streamflow_by_feature(nc_path: str) -> tuple[dict[int, float], _dt.datetime]:
    """Open the NWM channel_rt netCDF; return {feature_id → streamflow_cms} + valid_time."""
    try:
        import xarray as xr
        import numpy as np
    except ImportError as exc:
        raise NWMStreamflowUpstreamError(
            f"xarray / numpy not available: {exc}"
        ) from exc

    try:
        ds = xr.open_dataset(nc_path, engine="netcdf4")
    except Exception as exc:
        try:
            ds = xr.open_dataset(nc_path)
        except Exception as exc2:
            raise NWMStreamflowUpstreamError(
                f"could not open NWM netCDF {nc_path}: {exc2}"
            ) from exc2

    try:
        if "streamflow" not in ds.variables:
            raise NWMStreamflowUpstreamError(
                f"NWM netCDF missing 'streamflow' variable; got {list(ds.variables)}"
            )
        if "feature_id" not in ds.variables and "feature_id" not in ds.coords:
            raise NWMStreamflowUpstreamError(
                f"NWM netCDF missing 'feature_id'; got {list(ds.variables)}"
            )
        # Squeeze time dim if present.
        flow = ds["streamflow"]
        if "time" in flow.dims:
            flow = flow.isel(time=0)
        feature_ids = np.asarray(ds["feature_id"].values, dtype=np.int64)
        flows = np.asarray(flow.values, dtype=np.float64)
        if feature_ids.shape != flows.shape:
            raise NWMStreamflowUpstreamError(
                f"streamflow shape {flows.shape} != feature_id shape "
                f"{feature_ids.shape}"
            )
        # Extract valid_time.
        valid_time = _dt.datetime.now(_dt.timezone.utc)  # fallback
        if "time" in ds.coords:
            try:
                t = ds["time"].values
                t0 = t.item(0) if hasattr(t, "item") else t[0]
                # numpy datetime64 → python datetime
                if hasattr(t0, "astype"):
                    valid_time = (
                        _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)
                        + _dt.timedelta(seconds=int(t0.astype("int64") / 1_000_000_000))
                    )
            except Exception:
                pass

        return dict(zip(feature_ids.tolist(), flows.tolist())), valid_time
    finally:
        try:
            ds.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Build the FlatGeobuf output.
# ---------------------------------------------------------------------------


def _build_fgb(
    rows: list[dict[str, Any]],
    valid_time: _dt.datetime,
    product: str,
) -> bytes:
    """Build a FlatGeobuf with point geometry + streamflow attrs."""
    try:
        import geopandas as gpd
        from shapely.geometry import Point
    except ImportError as exc:
        raise NWMStreamflowUpstreamError(
            f"geopandas / shapely not available: {exc}"
        ) from exc

    geoms = [Point(r["_lon"], r["_lat"]) for r in rows]
    data = {
        "feature_id": [r["feature_id"] for r in rows],
        "streamflow_cms": [r["streamflow_cms"] for r in rows],
        "valid_time": [valid_time.isoformat() for _ in rows],
        "product": [product for _ in rows],
    }
    gdf = gpd.GeoDataFrame(data, geometry=geoms, crs="EPSG:4326")

    tmp_fgb: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="trid3nt_nwm_"
        ) as f:
            tmp_fgb = f.name
        gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        with open(tmp_fgb, "rb") as f:
            return f.read()
    except Exception as exc:
        raise NWMStreamflowUpstreamError(
            f"FlatGeobuf serialization failed: {exc}"
        ) from exc
    finally:
        if tmp_fgb is not None:
            try:
                os.unlink(tmp_fgb)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Top-level fetch (passed to read_through).
# ---------------------------------------------------------------------------


def _fetch_nwm_streamflow_bytes(
    bbox: tuple[float, float, float, float],
    product: str,
    valid_time_dt: _dt.datetime | None,
    forecast_hour: int,
) -> bytes:
    """End-to-end: download NWM channel_rt + discover bbox COMIDs → FGB bytes."""
    # 1. Resolve + download the NWM netCDF.
    key, resolved_valid_time = _resolve_nwm_key(product, valid_time_dt, forecast_hour)
    url = f"{_S3_BASE}/{key}"
    logger.info(
        "fetch_noaa_nwm_streamflow: downloading %s (resolved valid_time=%s)",
        url,
        resolved_valid_time.isoformat(),
    )
    nc_bytes = _http_get(url, timeout=_DOWNLOAD_TIMEOUT)
    if not nc_bytes:
        raise NWMStreamflowUpstreamError(f"empty response from {url}")

    # 2. Write to tempfile + parse streamflow lookup.
    tmp_nc: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".nc", delete=False, prefix="trid3nt_nwm_"
        ) as f:
            f.write(nc_bytes)
            tmp_nc = f.name
        flow_by_id, derived_valid_time = _load_streamflow_by_feature(tmp_nc)
    finally:
        if tmp_nc is not None:
            try:
                os.unlink(tmp_nc)
            except OSError:
                pass

    logger.info(
        "fetch_noaa_nwm_streamflow: loaded %d feature streamflow values",
        len(flow_by_id),
    )

    # 3. Discover bbox COMIDs via NLDI.
    bbox_comids = _discover_comids_in_bbox(bbox)
    logger.info(
        "fetch_noaa_nwm_streamflow: NLDI discovered %d COMIDs in bbox=%s",
        len(bbox_comids),
        bbox,
    )
    if not bbox_comids:
        raise NWMStreamflowEmptyError(
            f"NLDI returned no NHDPlus COMIDs for bbox={bbox}; the bbox may "
            f"fall outside NHDPlus coverage or have no rivers in the sampled "
            f"5x5 grid (try a larger bbox or one containing a known river)"
        )

    # 4. Join streamflow + geometry; build row records.
    rows: list[dict[str, Any]] = []
    for comid in bbox_comids:
        flow_val = flow_by_id.get(comid)
        if flow_val is None:
            continue
        coords = _nldi_get_reach_geometry(comid)
        if coords is None or not coords:
            continue
        # Midpoint of the LineString.
        mid = coords[len(coords) // 2]
        rows.append({
            "feature_id": comid,
            "streamflow_cms": float(flow_val),
            "_lon": float(mid[0]),
            "_lat": float(mid[1]),
        })

    if not rows:
        raise NWMStreamflowEmptyError(
            f"no matched (NHDPlus COMID, streamflow, geometry) tuples for bbox={bbox}; "
            f"discovered {len(bbox_comids)} COMIDs but none had both streamflow "
            f"data and resolvable geometry"
        )

    logger.info(
        "fetch_noaa_nwm_streamflow: built %d feature rows; min=%.4f, max=%.4f, "
        "mean=%.4f m^3/s",
        len(rows),
        min(r["streamflow_cms"] for r in rows),
        max(r["streamflow_cms"] for r in rows),
        sum(r["streamflow_cms"] for r in rows) / len(rows),
    )

    return _build_fgb(rows, derived_valid_time, product)


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
def fetch_noaa_nwm_streamflow(
    bbox: tuple[float, float, float, float],
    product: Literal["analysis_assim", "short_range"] = "analysis_assim",
    valid_time: str | None = None,
    forecast_hour: int = 0,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch NOAA National Water Model streamflow as a point FlatGeobuf.

    Use this when: the agent needs gridded river discharge as fluvial-boundary
    forcing for a compound-flood model run (composes with
    ``model_flood_scenario`` and similar engines that ask for upstream
    hydrographs), as a real-time discharge overlay for a hazard-event narrative
    ("how high is the Caloosahatchee flowing right now?"), or as a contextual
    river-network display for a CONUS hydrology query. NWM is the NOAA
    operational hydrologic forecast and the canonical CONUS streamflow source
    (~2.7M NHDPlus reaches at ~hourly cadence).

    Do NOT use this for: gauge-based observed point streamflow at a USGS
    station (use ``fetch_streamflow`` — NWIS, the actual instrument record);
    global / non-CONUS river discharge (use ``fetch_cama_flood_discharge`` for
    the CaMa-Flood global product); river-reach polylines without flow values
    (use ``fetch_river_geometry`` — NHDPlus HR); precipitation forcing (use
    ``fetch_mrms_qpe`` for radar/gauge QPE or
    ``lookup_precip_return_period`` for Atlas 14 design storms); flood-extent
    rasters (NWM does not publish inundation directly — call
    ``model_flood_scenario`` with NWM forcing).

    Params:
        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326. Required
            — NWM is CONUS-only, supports_global_query=False. The bbox MUST
            intersect ``(-130, 20, -60, 55)`` or
            ``NWMStreamflowInputError`` is raised. Typical scales:
            - Watershed (~1° × 1°): ~50-200 reaches returned
            - Regional (~5° × 5°): up to _MAX_REACHES=500 (hard cap)
            Example for Fort Myers / Caloosahatchee:
            ``(-82.0, 26.4, -81.7, 26.7)``.
        product: One of:
            - ``"analysis_assim"`` (default): real-time analysis cycle,
              streamflow at the cycle hour (no forecast). Best for "what is
              the river doing right now" queries and historical reconstructions.
            - ``"short_range"``: 18-hour deterministic forecast initialized
              from the named cycle; pair with ``forecast_hour`` 1-18 to pick
              the forecast lead time.
        valid_time: Optional ISO-8601 UTC timestamp
            (e.g. ``"2025-01-01T12:00:00Z"``). When None, fetches the latest
            available cycle in the bucket (~30-day retention). For
            ``short_range``, valid_time = cycle + forecast_hour, so the cycle
            file is computed as ``valid_time - forecast_hour``.
        forecast_hour: Forecast lead in hours, 1-18 for ``short_range``;
            ignored for ``analysis_assim`` (always 0). Default 0.

    Returns:
        ``LayerURI`` pointing at a FlatGeobuf in the cache bucket:
        ``s3://trid3nt-cache/cache/dynamic-1h/nwm_streamflow/<key>.fgb``
        - ``layer_type="vector"``, ``role="primary"``,
          ``style_preset="nwm_streamflow"``, ``units="m^3/s"``.
        - Geometry: Point at each NHDPlus reach centroid (LineString midpoint
          from NLDI), EPSG:4326.
        - Properties per feature: ``feature_id`` (int, NHDPlus v2.1 COMID),
          ``streamflow_cms`` (float, m^3/s), ``valid_time`` (ISO-8601 UTC),
          ``product``. The ``feature_id`` is the join key downstream composers
          use to map to NHDPlus reach polylines (via
          ``fetch_river_geometry``) — this tool emits point centroids so the
          layer renders standalone as a discharge map.

    Cache: ``ttl_class="dynamic-1h"``, ``source_class="nwm_streamflow"``.
    Cache key is SHA-256 of
    ``(bbox-rounded-6dp, product, valid_time-iso-or-LATEST, forecast_hour)``
    so identical-bbox/hour calls hit the same FlatGeobuf.

    Cross-tool dependencies:
        - Composes WITH: ``model_flood_scenario`` (fluvial forcing),
          ``fetch_river_geometry`` (downstream NHDPlus polyline join via
          ``feature_id``), ``publish_layer`` (display on the map).
        - Composes ALONGSIDE: ``fetch_mrms_qpe`` (precip forcing),
          ``fetch_streamflow`` (NWIS point gauge cross-check),
          ``fetch_administrative_boundaries`` (watershed framing).
        - Sibling for non-CONUS coverage: ``fetch_cama_flood_discharge``
          (global, Tier-2, reanalysis-only).
        - Upstream data sources: NOAA NWM (s3://noaa-nwm-pds) + USGS NLDI
          (api.water.usgs.gov/nldi) for the geometry join.

    Errors:
        - ``NWMStreamflowInputError``: bad bbox / product / valid_time /
          forecast_hour (retryable=False).
        - ``NWMStreamflowUpstreamError``: S3 / NLDI network failure or
          netCDF parse error (retryable=True).
        - ``NWMStreamflowNotAvailableError``: requested cycle not published
          (gap, future, outside ~30-day retention) (retryable=False).
        - ``NWMStreamflowEmptyError``: NLDI returned no COMIDs in the bbox
          (offshore or no rivers in the 5x5 grid) (retryable=False).

    Tier-1 free. No API key. ``supports_global_query=False``.
    OQ-A3-NWM-GEOMETRY-JOIN: v0.1 uses an NLDI 5×5-grid bbox sample for
    geometry discovery; a future iteration could ship the NWM RouteLink table
    as a static-30d-cached lookup to avoid the per-call NLDI round-trips.
    """
    # 1. Validate inputs.
    if product not in _VALID_PRODUCTS:
        raise NWMStreamflowInputError(
            f"unknown product={product!r}; allowed: {sorted(_VALID_PRODUCTS)}"
        )

    if not isinstance(forecast_hour, int) or not (0 <= forecast_hour <= 18):
        raise NWMStreamflowInputError(
            f"forecast_hour must be int in [0, 18]; got {forecast_hour!r}"
        )
    if product == "short_range" and forecast_hour == 0:
        raise NWMStreamflowInputError(
            "short_range requires forecast_hour >= 1 (f000 is not published "
            "for the channel_rt streamflow stream)"
        )

    # Coerce bbox tuple-like to tuple.
    if not isinstance(bbox, tuple):
        try:
            bbox = tuple(bbox)  # type: ignore[arg-type]
        except TypeError as exc:
            raise NWMStreamflowInputError(
                f"bbox must be a 4-tuple or list; got {type(bbox).__name__}"
            ) from exc
    _validate_bbox(bbox)  # type: ignore[arg-type]
    q_bbox = _round_bbox_to_6dp(bbox)  # type: ignore[arg-type]

    valid_time_dt = _parse_valid_time(valid_time)

    # 2. Cache-key params.
    params: dict[str, Any] = {
        "bbox": list(q_bbox),
        "product": product,
        "valid_time": valid_time if valid_time is not None else "LATEST",
        "forecast_hour": forecast_hour,
    }

    # 3. Read-through cache.
    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=lambda: _fetch_nwm_streamflow_bytes(
            q_bbox, product, valid_time_dt, forecast_hour
        ),
    )
    assert result.uri is not None, (
        "fetch_noaa_nwm_streamflow is cacheable; uri must be set by read_through"
    )

    # 4. Build LayerURI.
    bbox_tag = (
        f"{q_bbox[0]:.4f}-{q_bbox[1]:.4f}-{q_bbox[2]:.4f}-{q_bbox[3]:.4f}"
    )
    vt_tag = valid_time if valid_time is not None else "latest"
    seed = hashlib.sha256(
        f"{bbox_tag}-{product}-{vt_tag}-{forecast_hour}".encode("utf-8")
    ).hexdigest()[:8]

    return LayerURI(
        layer_id=f"nwm-streamflow-{product}-{seed}",
        name=(
            f"NWM streamflow — {product} "
            f"({'latest' if valid_time is None else valid_time}"
            f"{f' +f{forecast_hour:03d}' if product == 'short_range' else ''})"
        ),
        layer_type="vector",
        uri=result.uri,
        style_preset="nwm_streamflow",
        role="primary",
        units="m^3/s",
        bbox=q_bbox,
    )
