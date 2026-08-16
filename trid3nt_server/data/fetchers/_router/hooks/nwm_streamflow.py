"""NOAA National Water Model streamflow delegate hooks: the ``fetch_noaa_nwm_streamflow`` fold.

THE FETCHER-FINALE ENDGAME (the LAST coded data-fetcher). ``fetch_noaa_nwm_streamflow``
is a MULTI-SOURCE COMPOSITE: resolve the NWM S3 channel_rt key ->
download the whole-object netCDF -> an xarray ``{feature_id: streamflow}`` LOOKUP DICT
+ an NLDI 5x5-grid spatial sample (25 point-snap requests) -> COMIDs + per-reach NLDI
geometry (up to 500 requests) + a ``feature_id`` JOIN -> a point FlatGeobuf.
STOP-RULED a fold because "no router MODE fetches-a-lookup-dict + spatially-samples-a-2nd
-API + joins". The finale resolves that: the composite is expressed as ORDINARY delegate
socket I/O (the topobathy / storm_tracks precedent) -- the delegate hook
OWNS the S3 read, the NLDI sampling rounds, AND the in-delegate join, exactly as the twin
did. No new executor machinery is added for one source (the bar); the join is
plain in-process computation, and each NLDI probe is an independent best-effort request
(no transport-layer coalescing/retry semantics the delegate cannot reach), so the delegate
shape hits NO wall. ``fetch_noaa_nwm_streamflow`` folds onto a ``library_delegate`` VECTOR
source (``shape: vector-fgb``, ``ingest.access: library_delegate``):

  * ``nwm_streamflow.validate`` (delegate_validate) -- the CONUS-intersect gate (the
    twin's own (-130, 20, -60, 55) NHDPlus domain, more generous than the generic
    conus_only gate), the ``short_range`` cross-param rule (forecast_hour >= 1), and the
    ``valid_time`` ISO parse -- shape checks the declarative param surface cannot express,
    raised pre-cache / pre-network as ``NWMStreamflowInputError``.
  * ``nwm_streamflow.read`` (delegate) -- OWN every round: resolve the NWM cycle key,
    download the channel_rt netCDF, parse the ``{feature_id: streamflow}`` lookup, sample
    the 5x5 NLDI grid for bbox COMIDs, fetch each reach's geometry, JOIN streamflow +
    geometry -> point GeoJSON features (props ``feature_id`` / ``streamflow_cms`` /
    ``valid_time`` / ``product``, the SFINCS-adapter + river_dye consumed shape) for the
    shared ``vector_fgb`` serializer, and RECORD the fetch-time provenance (the resolved
    NWM reference time + reach count + NLDI sample stats) via the channel.
  * ``nwm_streamflow.envelope`` -- the twin's exact ``nwm-streamflow-{product}-{seed}``
    layer_id + ``NWM streamflow -- <product> (<latest|valid_time>[ +fNNN])`` name (seed
    recomputed deterministically from the validated params) plus the reference-time /
    reach-count / NLDI-sample provenance replayed from the channel (a pre-channel cache
    object -> the declared defaults hold, byte-identical to the twin's cache-hit shape).

The ``NWMStreamflow*Error`` classes live HERE (their stable importable home now that the
coded twin is deleted). Their base is ``FetchError`` so ``library_delegate.invoke`` passes
them through unchanged (its ``except FetchError: raise`` passthrough), preserving
the pinned ``error_code`` (``NWM_STREAMFLOW_INPUT_ERROR`` / ``_UPSTREAM_ERROR`` /
``_NOT_AVAILABLE`` / ``_EMPTY``) through the delegate wrapper.
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

from trid3nt_server.data.cache import record_provenance

from ..._fetch_common import FetchError
from . import register_hook

logger = logging.getLogger(
    "trid3nt_server.data.fetchers._router.hooks.nwm_streamflow"
)

__all__ = [
    "NWMStreamflowError",
    "NWMStreamflowInputError",
    "NWMStreamflowUpstreamError",
    "NWMStreamflowNotAvailableError",
    "NWMStreamflowEmptyError",
    "estimate_payload_mb",
    "validate_nwm_streamflow",
    "read_nwm_streamflow",
    "envelope_nwm_streamflow",
]


# ---------------------------------------------------------------------------
# Error types (typed-error surface). Base = FetchError so the pinned
# error_code survives library_delegate.invoke's passthrough.
# ---------------------------------------------------------------------------


class NWMStreamflowError(FetchError):
    """Base class for fetch_noaa_nwm_streamflow failures."""

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
    point in the 5x5 grid.
    """

    error_code = "NWM_STREAMFLOW_EMPTY"
    retryable = False


# ---------------------------------------------------------------------------
# Constants (twin-identical).
# ---------------------------------------------------------------------------

#: NOAA NWM public S3 bucket (open access, no auth).
_S3_BASE = "https://noaa-nwm-pds.s3.amazonaws.com"

#: USGS NLDI base (used to discover COMIDs + geometries for the bbox subset).
_NLDI_BASE = "https://api.water.usgs.gov/nldi"

#: Supported NWM products.
_VALID_PRODUCTS: frozenset[str] = frozenset({"analysis_assim", "short_range"})

#: CONUS bounding box (EPSG:4326) -- the NHDPlus v2.1 CONUS domain.
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

#: NLDI point-sample grid density (5x5 = 25 probes per bbox call).
_NLDI_SAMPLE_GRID = 5

#: Cap on total reaches we'll attempt to materialize (bounds API spend).
_MAX_REACHES = 500


# ---------------------------------------------------------------------------
# Payload estimator (kept importable for tests; the router synthesizes its own
# from source.yaml's payload_estimate block for the promoted tool).
# ---------------------------------------------------------------------------


def estimate_payload_mb(
    bbox: tuple[float, float, float, float] | None = None,
    product: str = "analysis_assim",
    valid_time: str | None = None,
    forecast_hour: int = 0,
    **_kw: Any,
) -> float:
    """Estimate the output FlatGeobuf size in MB (the bbox-subset only)."""
    if bbox is None:
        return 1.0
    try:
        west, south, east, north = bbox
        sq_deg = max(0.0, (east - west)) * max(0.0, (north - south))
    except (TypeError, ValueError):
        return 1.0
    reaches = min(_MAX_REACHES, int(sq_deg * 100.0))
    return max(0.01, reaches * 100 / 1_000_000.0)


# ---------------------------------------------------------------------------
# bbox + date helpers (twin-identical; NWMStreamflowInputError-typed).
# ---------------------------------------------------------------------------


def _validate_conus_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Raise ``NWMStreamflowInputError`` unless the bbox intersects the NWM CONUS domain.

    The router's generic bbox validation already stamps ``NWM_STREAMFLOW_INPUT_ERROR``
    for shape / range / degenerate / non-finite bboxes (via ``error_prefix=NWM_STREAMFLOW``);
    this hook adds the CONUS-intersect gate over the twin's own more-generous domain
    envelope (distinct from the generic gridmet-bounds conus_only gate).
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    if max_lon < _CONUS_BBOX[0] or min_lon > _CONUS_BBOX[2]:
        raise NWMStreamflowInputError(
            f"bbox {bbox} does not intersect NWM CONUS domain {_CONUS_BBOX}"
        )
    if max_lat < _CONUS_BBOX[1] or min_lat > _CONUS_BBOX[3]:
        raise NWMStreamflowInputError(
            f"bbox {bbox} does not intersect NWM CONUS domain {_CONUS_BBOX}"
        )


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
# HTTP helpers (the delegate owns its own socket, the sanctioned impurity).
# ---------------------------------------------------------------------------


def _http_get(url: str, timeout: float) -> bytes:
    """Plain HTTP GET. Raises ``NWMStreamflowUpstreamError`` on failure."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise NWMStreamflowUpstreamError(f"upstream HTTP {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise NWMStreamflowUpstreamError(
            f"network error for {url}: {exc.reason}"
        ) from exc
    except TimeoutError as exc:
        raise NWMStreamflowUpstreamError(f"timed out after {timeout}s for {url}") from exc


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
    """Find the most-recent ``nwm.YYYYMMDD/`` prefix in the bucket (retention ~30d)."""
    today = _dt.datetime.now(_dt.timezone.utc).date()
    for days_back in range(0, 35):
        candidate = today - _dt.timedelta(days=days_back)
        prefix = f"nwm.{candidate.strftime('%Y%m%d')}/"
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
    """Resolve the S3 key for the requested ``(product, valid_time, fhour)``."""
    if valid_time is None:
        date_str = _latest_nwm_date()
        prefix = f"nwm.{date_str}/{product}/"
        keys = _list_s3_keys(prefix, max_keys=200)
        if product == "analysis_assim":
            matcher = re.compile(
                r"\.t(\d{2})z\.analysis_assim\.channel_rt\.tm00\.conus\.nc$"
            )
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
        candidates.sort()
        cycle_hh, latest_key = candidates[-1]
        resolved_dt = _dt.datetime(
            int(date_str[0:4]),
            int(date_str[4:6]),
            int(date_str[6:8]),
            int(cycle_hh),
            tzinfo=_dt.timezone.utc,
        )
        if product == "short_range":
            resolved_dt = resolved_dt + _dt.timedelta(hours=forecast_hour)
        return latest_key, resolved_dt

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
    probe = _list_s3_keys(key, max_keys=1)
    if not probe or key not in probe:
        raise NWMStreamflowNotAvailableError(
            f"NWM file not found: {key}; may be outside the bucket "
            f"retention window (~30 days) or not yet published"
        )
    return key, valid_time


# ---------------------------------------------------------------------------
# NLDI bbox-sampling -> list of COMIDs + per-reach geometry.
# ---------------------------------------------------------------------------


def _nldi_snap_point(lon: float, lat: float) -> int | None:
    """Snap (lon, lat) to nearest NHDPlus reach via NLDI; return COMID or None.

    Errors are swallowed silently and return None so a failed sample point
    doesn't abort the whole bbox discovery (the twin's contract).
    """
    url = f"{_NLDI_BASE}/linked-data/comid/position?coords=POINT({lon}%20{lat})"
    try:
        body = _http_get(url, timeout=_NLDI_TIMEOUT).decode("utf-8")
    except NWMStreamflowUpstreamError:
        return None
    try:
        obj = json.loads(body)
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
        obj = json.loads(body)
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


def _discover_comids_in_bbox(bbox: tuple[float, float, float, float]) -> list[int]:
    """Sample a 5x5 grid inside bbox, snap each point to NHDPlus, dedupe.

    Returns a list of COMIDs (NHDPlus v2.1 identifiers). Capped at
    ``_MAX_REACHES`` so even a dense urban bbox cannot exhaust NLDI.
    """
    west, south, east, north = bbox
    found: set[int] = set()
    for i in range(_NLDI_SAMPLE_GRID):
        for j in range(_NLDI_SAMPLE_GRID):
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
# netCDF -> streamflow lookup.
# ---------------------------------------------------------------------------


def _load_streamflow_by_feature(nc_path: str) -> tuple[dict[int, float], _dt.datetime]:
    """Open the NWM channel_rt netCDF; return {feature_id -> streamflow_cms} + valid_time."""
    try:
        import numpy as np
        import xarray as xr
    except ImportError as exc:
        raise NWMStreamflowUpstreamError(f"xarray / numpy not available: {exc}") from exc

    try:
        ds = xr.open_dataset(nc_path, engine="netcdf4")
    except Exception:
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
        flow = ds["streamflow"]
        if "time" in flow.dims:
            flow = flow.isel(time=0)
        feature_ids = np.asarray(ds["feature_id"].values, dtype=np.int64)
        flows = np.asarray(flow.values, dtype=np.float64)
        if feature_ids.shape != flows.shape:
            raise NWMStreamflowUpstreamError(
                f"streamflow shape {flows.shape} != feature_id shape {feature_ids.shape}"
            )
        valid_time = _dt.datetime.now(_dt.timezone.utc)  # fallback
        if "time" in ds.coords:
            try:
                t = ds["time"].values
                t0 = t.item(0) if hasattr(t, "item") else t[0]
                if hasattr(t0, "astype"):
                    valid_time = _dt.datetime(
                        1970, 1, 1, tzinfo=_dt.timezone.utc
                    ) + _dt.timedelta(seconds=int(t0.astype("int64") / 1_000_000_000))
            except Exception:
                pass
        return dict(zip(feature_ids.tolist(), flows.tolist())), valid_time
    finally:
        try:
            ds.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# The composite fetch (delegate body): S3 netCDF + NLDI sample -> point features.
# ---------------------------------------------------------------------------


def _fetch_nwm_features(
    bbox: tuple[float, float, float, float],
    product: str,
    valid_time_dt: _dt.datetime | None,
    forecast_hour: int,
) -> tuple[list[dict[str, Any]], _dt.datetime, int]:
    """End-to-end: NWM channel_rt + NLDI bbox sample -> (GeoJSON point features,
    resolved valid_time, discovered-COMID count).

    OWNS every network round (the sanctioned delegate socket impurity):
    resolve + download the netCDF, parse the {feature_id: streamflow} lookup, sample
    the 5x5 NLDI grid, fetch reach geometry, and JOIN in-process. Honest-empty is a
    typed ``NWMStreamflowEmptyError`` (no fabricated layer), never a header-only FGB.
    """
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

    # 4. Join streamflow + geometry; build GeoJSON point features.
    vt_iso = derived_valid_time.isoformat()
    feats: list[dict[str, Any]] = []
    for comid in bbox_comids:
        flow_val = flow_by_id.get(comid)
        if flow_val is None:
            continue
        coords = _nldi_get_reach_geometry(comid)
        if coords is None or not coords:
            continue
        mid = coords[len(coords) // 2]
        feats.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [float(mid[0]), float(mid[1])]},
                "properties": {
                    "feature_id": int(comid),
                    "streamflow_cms": float(flow_val),
                    "valid_time": vt_iso,
                    "product": product,
                },
            }
        )

    if not feats:
        raise NWMStreamflowEmptyError(
            f"no matched (NHDPlus COMID, streamflow, geometry) tuples for bbox={bbox}; "
            f"discovered {len(bbox_comids)} COMIDs but none had both streamflow "
            f"data and resolvable geometry"
        )

    flows = [f["properties"]["streamflow_cms"] for f in feats]
    logger.info(
        "fetch_noaa_nwm_streamflow: built %d feature rows; min=%.4f, max=%.4f, "
        "mean=%.4f m^3/s",
        len(feats),
        min(flows),
        max(flows),
        sum(flows) / len(flows),
    )
    return feats, derived_valid_time, len(bbox_comids)


# ---------------------------------------------------------------------------
# HOOK: delegate_validate -- CONUS + short_range + valid_time gate (pre-cache).
# ---------------------------------------------------------------------------


@register_hook("nwm_streamflow.validate")
def validate_nwm_streamflow(spec: Any, params: dict[str, Any]) -> None:
    """Pre-cache input gate: CONUS-intersect, short_range fhour rule, valid_time parse.

    The router's generic param validation (enum ``product`` membership, ``bbox``
    shape/range, ``forecast_hour`` int range [0, 18]) has already run; this hook adds
    the cross-param + domain checks it cannot express, all typed
    ``NWMStreamflowInputError`` (``NWM_STREAMFLOW_INPUT_ERROR``).
    """
    product = params.get("product", "analysis_assim")
    forecast_hour = int(params.get("forecast_hour", 0) or 0)
    if product == "short_range" and forecast_hour == 0:
        raise NWMStreamflowInputError(
            "short_range requires forecast_hour >= 1 (f000 is not published "
            "for the channel_rt streamflow stream)"
        )
    bbox = params.get("bbox")
    if bbox is not None:
        _validate_conus_bbox(tuple(float(v) for v in bbox))
    # Parse valid_time pre-cache so a bad ISO string fails as a typed input error
    # BEFORE the cache key / network (the twin parsed it in its body pre-fetch).
    _parse_valid_time(params.get("valid_time"))


# ---------------------------------------------------------------------------
# HOOK: delegate -- own the composite fetch; record provenance; return features.
# ---------------------------------------------------------------------------


@register_hook("nwm_streamflow.read")
def read_nwm_streamflow(
    spec: Any, params: dict[str, Any], *, timeout_s: float
) -> list[dict[str, Any]]:
    """Own the NWM+NLDI composite; RECORD fetch-time provenance; return features."""
    bbox = tuple(float(v) for v in params["bbox"])
    product = str(params.get("product", "analysis_assim"))
    forecast_hour = int(params.get("forecast_hour", 0) or 0)
    valid_time_dt = _parse_valid_time(params.get("valid_time"))

    feats, resolved_valid_time, n_comids = _fetch_nwm_features(
        bbox, product, valid_time_dt, forecast_hour
    )

    record_provenance(
        {
            "reference_time": resolved_valid_time.isoformat(),
            "product": product,
            "reach_count": len(feats),
            "nldi_comids_discovered": int(n_comids),
        }
    )
    return feats


# ---------------------------------------------------------------------------
# HOOK: envelope -- twin layer_id/name + reference-time/reach provenance replay.
# ---------------------------------------------------------------------------


@register_hook("nwm_streamflow.envelope")
def envelope_nwm_streamflow(
    spec: Any,
    params: dict[str, Any],
    layer: Any,
    data: bytes | None,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the twin's exact layer_id / name + the fetch-time provenance."""
    product = str(params.get("product", "analysis_assim"))
    forecast_hour = int(params.get("forecast_hour", 0) or 0)
    valid_time = params.get("valid_time")
    q_bbox = tuple(float(v) for v in params["bbox"])

    bbox_tag = f"{q_bbox[0]:.4f}-{q_bbox[1]:.4f}-{q_bbox[2]:.4f}-{q_bbox[3]:.4f}"
    vt_tag = valid_time if valid_time is not None else "latest"
    seed = hashlib.sha256(
        f"{bbox_tag}-{product}-{vt_tag}-{forecast_hour}".encode("utf-8")
    ).hexdigest()[:8]

    name = (
        f"NWM streamflow -- {product} "
        f"({'latest' if valid_time is None else valid_time}"
        f"{f' +f{forecast_hour:03d}' if product == 'short_range' else ''})"
    )

    prov = provenance or {}
    return {
        "layer_id": f"nwm-streamflow-{product}-{seed}",
        "name": name,
        "product": product,
        "reference_time": prov.get("reference_time"),
        "reach_count": int(prov.get("reach_count", 0)),
        "nldi_comids_discovered": int(prov.get("nldi_comids_discovered", 0)),
    }
