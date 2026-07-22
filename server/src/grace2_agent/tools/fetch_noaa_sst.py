"""``fetch_noaa_sst`` atomic tool  --  NOAA daily sea-surface-temperature COG.

Fetches a daily sea-surface-temperature (SST) field for a bbox from the NOAA
Coral Reef Watch (CRW) operational daily 5 km product and returns a single-band
float32 COG in degrees Celsius (style ``sst_celsius``). Optionally returns the
SST ANOMALY (observed minus climatology, degrees C, diverging ramp) instead.

SST is the canonical ocean-surface-temperature layer: it drives coral-bleaching
heat-stress monitoring, marine heatwaves, hurricane-intensification potential
(warm water = fuel), and fisheries / ecosystem context. The CRW daily product is
global, gap-filled (no cloud holes), updated daily with a ~1-day latency, and
keyless.

Data source
===========

NOAA Coral Reef Watch Operational Daily Near-Real-Time Global 5 km Satellite
Coral Bleaching Monitoring Products, served via the NOAA CoastWatch ERDDAP
griddap endpoint (keyless, no API key):

    catalog:  https://coastwatch.pfeg.noaa.gov/erddap
    dataset:  NOAA_DHW   (longitude already -180..180; latitude descending)
    vars:     CRW_SST          (sea surface temperature, Celsius)
              CRW_SSTANOMALY   (SST anomaly vs climatology, Celsius)
    grid:     0.05 deg (~5 km), global, daily, 1985-present (~1 day latency)

The griddap ``.nc`` query subsets a SINGLE day + the requested lat/lon window
server-side, so a metro-scale ocean bbox returns a few KB of NetCDF rather than
the whole global grid. We open it with xarray (netcdf4 engine), squeeze the
single time slice, and re-emit a single-band float32 COG in EPSG:4326.

Land is masked (the CRW product carries ocean pixels only); a fully-land bbox
returns an all-NaN window, which is surfaced as an honest typed
``SSTNoDataError`` rather than a fabricated layer.

Honesty (data-source fallback norm): a date outside the coverage window
(ERDDAP 404 "greater than the axis maximum"), an out-of-ocean bbox (all-NaN), or
an upstream / parse failure each raise a distinct typed error  --  never a
fabricated layer.

FR-CE-8 / FR-DC-3/4: routed through ``read_through`` so identical
``(bbox, date, variable)`` calls reuse the cached SST COG in the
``static-30d`` / ``noaa_sst`` cache prefix. (CRW daily values are finalized once
published; a 30-day TTL matches the other historical griddap fetchers.)

``supports_global_query=False``  --  the COG is AOI-scoped (a bbox guardrail
caps the materialized grid). Tier-1 free (no API key). Heavy emit-free sync
network + raster work  --  should run via ``asyncio.to_thread`` (add to
``_ALWAYS_OFFLOAD_SYNC_TOOLS``) so it never stalls the WebSocket heartbeat.
"""

from __future__ import annotations

import datetime as _dt
import logging
import math
import os
import tempfile
from typing import Any

from grace2_contracts.execution import LayerURI
from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import read_through

__all__ = [
    "fetch_noaa_sst",
    "estimate_payload_mb",
    "SSTError",
    "SSTInputError",
    "SSTNoDataError",
    "SSTUpstreamError",
]

logger = logging.getLogger("grace2_agent.tools.fetch_noaa_sst")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class SSTError(RuntimeError):
    """Base class for fetch_noaa_sst failures.

    ``error_code`` maps to the WebSocket A.6 error frame emitted by the agent
    surface; ``retryable`` guides FR-AS-11 retry logic.
    """

    error_code: str = "NOAA_SST_ERROR"
    retryable: bool = True


class SSTInputError(SSTError):
    """Bad inputs (malformed / out-of-range / too-large bbox, bad date, unknown variable)."""

    error_code = "NOAA_SST_INPUT_ERROR"
    retryable = False


class SSTNoDataError(SSTError):
    """No SST data covers the bbox on the requested date.

    Honest no-data signal (data-source fallback norm): a fully-land bbox
    (all-NaN ocean window) or a date outside the CRW coverage window. Never
    fabricate a layer.
    """

    error_code = "NOAA_SST_NO_DATA"
    retryable = False


class SSTUpstreamError(SSTError):
    """An ERDDAP request / NetCDF parse / COG write failed."""

    error_code = "NOAA_SST_UPSTREAM_ERROR"
    retryable = True


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: NOAA CoastWatch ERDDAP base + the CRW operational daily 5 km dataset.
#: ``NOAA_DHW`` is already on a -180..180 longitude grid (no _Lon0360 needed).
_ERDDAP_BASE = "https://coastwatch.pfeg.noaa.gov/erddap"
_DATASET_ID = "NOAA_DHW"

#: Selectable variables -> (erddap var, units, style preset, role, friendly name).
_VARIABLES: dict[str, tuple[str, str, str, str, str]] = {
    "sst": (
        "CRW_SST",
        "degrees Celsius",
        "sst_celsius",
        "primary",
        "NOAA Sea-Surface Temperature",
    ),
    "anomaly": (
        "CRW_SSTANOMALY",
        "degrees Celsius",
        "sst_anomaly",
        "primary",
        "NOAA SST Anomaly",
    ),
}

#: CRW 5 km native grid spacing (deg). Used only for the payload estimate.
_NATIVE_DEG = 0.05

#: CRW coverage starts 1985-01-01 (operational daily). Earliest selectable date.
_COVERAGE_START = _dt.date(1985, 1, 1)

#: bbox area guardrail (deg^2). The COG is AOI-scoped; a huge ocean bbox at
#: 5 km would materialize a large grid. ~25 deg^2 ~ a regional sea / large
#: gulf  --  generous for an SST context layer, still bounded.
_MAX_BBOX_DEG2 = 25.0

#: 4-dp bbox quantization (~11 m) for cache-key stability  --  finer than the
#: 5 km grid so distinct AOIs never collide.
_BBOX_DECIMALS = 4

#: HTTP timeout (s). ERDDAP can be slow under load; generous but bounded.
_HTTP_TIMEOUT_S = 90.0


# ---------------------------------------------------------------------------
# AtomicToolMetadata.
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="fetch_noaa_sst",
    ttl_class="static-30d",
    source_class="noaa_sst",
    cacheable=True,
    supports_global_query=False,
    payload_mb_estimator_name="estimate_payload_mb",
)


# ---------------------------------------------------------------------------
# Payload estimator (Wave 1.5 chat-warning gate).
# ---------------------------------------------------------------------------


def estimate_payload_mb(
    bbox: tuple[float, float, float, float] | None = None,
    **_kw: Any,
) -> float:
    """Estimate emitted SST COG size in MB.

    A single-band float32 DEFLATE-COG at 5 km is tiny: a 1 deg^2 ocean window is
    only ~20x20 px. Even a 25 deg^2 regional sea is ~100x100 px (well under a
    MB). Scale loosely with bbox area, floored small.
    """
    if bbox is None:
        return 0.5
    try:
        west, south, east, north = bbox
        sq_deg = max(0.0, (east - west)) * max(0.0, (north - south))
    except (TypeError, ValueError):
        return 0.5
    # ~ (deg / 0.05)^2 float32 px, DEFLATE ~ 0.5x; tiny grids. The CRW 5 km
    # SST COG is genuinely small (a 25 deg^2 regional sea is only ~0.02 MB), so
    # the floor is small too -- the scaling stays monotonic with area.
    cells = sq_deg / (_NATIVE_DEG * _NATIVE_DEG)
    return max(0.01, cells * 4.0 * 0.5 / (1024.0 * 1024.0))


# ---------------------------------------------------------------------------
# Input helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    if not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
        raise SSTInputError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in bbox):
        raise SSTInputError(f"bbox contains non-finite / non-numeric values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise SSTInputError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise SSTInputError(f"bbox lat out of [-90,90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise SSTInputError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )
    area = (max_lon - min_lon) * (max_lat - min_lat)
    if area > _MAX_BBOX_DEG2:
        raise SSTInputError(
            f"bbox area {area:.2f} deg^2 exceeds {_MAX_BBOX_DEG2} deg^2 guardrail "
            "for fetch_noaa_sst (SST is AOI-scoped; narrow the bbox)."
        )


def _round_bbox(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    return tuple(round(float(v), _BBOX_DECIMALS) for v in bbox)  # type: ignore[return-value]


def _resolve_variable(variable: str | None) -> str:
    """Map a user variable token to a known key, raising on unknown."""
    if variable is None:
        return "sst"
    v = str(variable).strip().lower()
    if v in _VARIABLES:
        return v
    # Friendly aliases.
    if v in ("sea_surface_temperature", "temperature", "temp", "crw_sst"):
        return "sst"
    if v in ("sst_anomaly", "sstanomaly", "crw_sstanomaly", "anom"):
        return "anomaly"
    raise SSTInputError(
        f"unknown variable {variable!r}; choose one of "
        f"{sorted(_VARIABLES)} (or 'anomaly' for the SST anomaly)."
    )


def _parse_date(date: str | None) -> _dt.date:
    """Parse an optional ``YYYY-MM-DD`` date; default = the most recent likely day.

    CRW publishes daily with ~1 day latency, so the default targets ``today-1``;
    if that day is not yet published the fetch backs off to earlier days.
    """
    if date is None or str(date).strip() == "":
        return _dt.datetime.now(_dt.timezone.utc).date() - _dt.timedelta(days=1)
    s = str(date).strip()
    # Accept a full ISO datetime too; take the date part.
    s = s.split("T")[0]
    try:
        d = _dt.date.fromisoformat(s)
    except ValueError as exc:
        raise SSTInputError(
            f"date must be 'YYYY-MM-DD'; got {date!r} ({exc})"
        ) from exc
    if d < _COVERAGE_START:
        raise SSTInputError(
            f"date {d.isoformat()} precedes NOAA CRW coverage start "
            f"({_COVERAGE_START.isoformat()})."
        )
    today = _dt.datetime.now(_dt.timezone.utc).date()
    if d > today:
        raise SSTInputError(
            f"date {d.isoformat()} is in the future (today is {today.isoformat()})."
        )
    return d


def _build_griddap_url(erddap_var: str, bbox: tuple[float, float, float, float], date: _dt.date) -> str:
    """Construct the ERDDAP griddap ``.nc`` single-day bbox-subset URL.

    NOAA_DHW latitude DESCENDS (89.975 -> -89.975), so the latitude constraint
    is written high:low (north:south) to match the axis direction; longitude
    ascends (low:high). The time index is the requested day at 12:00:00Z (the
    CRW daily nominal timestamp).
    """
    west, south, east, north = bbox
    ts = f"{date.isoformat()}T12:00:00Z"
    # var[(time)][(lat_hi):(lat_lo)][(lon_lo):(lon_hi)] -- lat descending.
    sel = f"{erddap_var}[({ts})][({north}):({south})][({west}):({east})]"
    return f"{_ERDDAP_BASE}/griddap/{_DATASET_ID}.nc?{sel}"


# ---------------------------------------------------------------------------
# Core: ERDDAP griddap subset -> single-band float32 COG bytes.
# ---------------------------------------------------------------------------


def _fetch_griddap_nc(url: str, date: _dt.date) -> bytes:
    """GET the ERDDAP ``.nc`` subset, translating coverage/HTTP errors.

    A 404 with "greater than the axis maximum" (date not yet published) is an
    honest no-data condition; other non-200s are upstream errors.
    """
    import httpx

    try:
        with httpx.Client(follow_redirects=True, timeout=_HTTP_TIMEOUT_S) as client:
            resp = client.get(url)
    except Exception as exc:  # noqa: BLE001  --  translate any transport error
        raise SSTUpstreamError(
            f"NOAA CRW ERDDAP request failed (url={url[:160]!r}): {exc}"
        ) from exc

    if resp.status_code == 200:
        return resp.content

    body = resp.text[:300].replace("\n", " ")
    # ERDDAP signals an out-of-range axis / empty result with 404 + a descriptive
    # body. Treat "no matching results" / "axis maximum" as honest no-data.
    low = body.lower()
    if resp.status_code == 404 and (
        "no matching results" in low or "axis maximum" in low or "axis minimum" in low
    ):
        raise SSTNoDataError(
            f"NOAA CRW has no SST for date={date.isoformat()} "
            f"(ERDDAP: {body})"
        )
    raise SSTUpstreamError(
        f"NOAA CRW ERDDAP returned HTTP {resp.status_code}: {body}"
    )


def _nc_to_cog_bytes(
    nc_bytes: bytes,
    erddap_var: str,
    units: str,
    variable_key: str,
    bbox: tuple[float, float, float, float],
    date: _dt.date,
) -> bytes:
    """Open the griddap NetCDF, squeeze the day, and write a float32 COG.

    Raises ``SSTNoDataError`` when the ocean window is entirely land (all-NaN),
    and ``SSTUpstreamError`` on any parse / write failure.
    """
    try:
        import numpy as np
        import rioxarray  # noqa: F401  --  registers the .rio accessor
        import xarray as xr
    except ImportError as exc:  # pragma: no cover  --  deps present in venv
        raise SSTUpstreamError(
            f"xarray / rioxarray / numpy not available: {exc}"
        ) from exc

    tmp_nc: str | None = None
    out_path: str | None = None
    try:
        fd, tmp_nc = tempfile.mkstemp(suffix=".nc", prefix="grace2_sst_")
        with os.fdopen(fd, "wb") as f:
            f.write(nc_bytes)

        try:
            ds = xr.open_dataset(tmp_nc, engine="netcdf4")
        except Exception as exc:  # noqa: BLE001
            raise SSTUpstreamError(
                f"could not parse NOAA CRW NetCDF subset: {exc}"
            ) from exc

        try:
            if erddap_var not in ds.variables:
                raise SSTUpstreamError(
                    f"NOAA CRW subset missing variable {erddap_var!r} "
                    f"(have {list(ds.data_vars)})"
                )
            da = ds[erddap_var]
            # Drop the singleton time dim if present.
            for tdim in ("time",):
                if tdim in da.dims:
                    da = da.squeeze(tdim, drop=True)

            lat_dim = next(
                (d for d in da.dims if d in ("latitude", "lat", "y")), None
            )
            lon_dim = next(
                (d for d in da.dims if d in ("longitude", "lon", "x")), None
            )
            if lat_dim is None or lon_dim is None:
                raise SSTUpstreamError(
                    f"NOAA CRW DataArray missing lat/lon dims; dims={da.dims}"
                )
            if da.size == 0 or any(s == 0 for s in da.shape):
                raise SSTNoDataError(
                    f"NOAA CRW returned an empty window for bbox={bbox} on "
                    f"{date.isoformat()} (no grid cells intersect the AOI)."
                )

            arr = np.asarray(da.values, dtype="float32")
            if not np.isfinite(arr).any():
                raise SSTNoDataError(
                    f"NOAA CRW SST is all-NaN over bbox={bbox} on "
                    f"{date.isoformat()} (the AOI is land / outside the ocean mask)."
                )

            # Rename to the rioxarray x/y convention and set CRS. CRW lat
            # DESCENDS (north-up) already, so do NOT sortby -- a north-up COG
            # needs row 0 = northernmost (negative y-step), matching the
            # job-0086 geographic-correctness lesson.
            rename: dict[str, str] = {}
            if lat_dim != "y":
                rename[lat_dim] = "y"
            if lon_dim != "x":
                rename[lon_dim] = "x"
            if rename:
                da = da.rename(rename)
            da = da.astype("float32").rio.write_crs("EPSG:4326")
            da.attrs["units"] = units
            da.attrs["source"] = "NOAA Coral Reef Watch (NOAA_DHW)"
            da.attrs["variable"] = variable_key
            da.attrs["date"] = date.isoformat()
            da.attrs["tool"] = "fetch_noaa_sst"

            fd2, out_path = tempfile.mkstemp(suffix=".tif", prefix="grace2_sst_")
            os.close(fd2)
            try:
                da.rio.to_raster(
                    out_path,
                    driver="COG",
                    dtype="float32",
                    compress="DEFLATE",
                    nodata=float("nan"),
                )
            except Exception:  # noqa: BLE001  --  fall back to plain GTiff
                da.rio.to_raster(
                    out_path,
                    driver="GTiff",
                    dtype="float32",
                    compress="DEFLATE",
                    nodata=float("nan"),
                )
            with open(out_path, "rb") as fh:
                cog_bytes = fh.read()

            logger.info(
                "fetch_noaa_sst: var=%s date=%s bbox=%s -> %d-byte COG "
                "(min=%.3f max=%.3f mean=%.3f valid=%d/%d)",
                variable_key,
                date.isoformat(),
                bbox,
                len(cog_bytes),
                float(np.nanmin(arr)),
                float(np.nanmax(arr)),
                float(np.nanmean(arr)),
                int(np.isfinite(arr).sum()),
                arr.size,
            )
            return cog_bytes
        finally:
            try:
                ds.close()
            except Exception:  # noqa: BLE001
                pass
    except SSTError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SSTUpstreamError(
            f"NOAA CRW SST COG build failed for bbox={bbox}: {exc}"
        ) from exc
    finally:
        for p in (tmp_nc, out_path):
            if p is not None:
                try:
                    os.unlink(p)
                except OSError:
                    pass


def _fetch_sst_cog_bytes(
    bbox: tuple[float, float, float, float],
    variable_key: str,
    date: _dt.date,
) -> bytes:
    """End-to-end: griddap subset -> COG bytes for ``(bbox, variable, date)``."""
    erddap_var, units, _style, _role, _name = _VARIABLES[variable_key]
    url = _build_griddap_url(erddap_var, bbox, date)
    nc_bytes = _fetch_griddap_nc(url, date)
    return _nc_to_cog_bytes(nc_bytes, erddap_var, units, variable_key, bbox, date)


# ---------------------------------------------------------------------------
# Registered atomic tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    # Annotations: readOnlyHint=True, openWorldHint=True (NOAA CoastWatch ERDDAP
    # public API), destructiveHint=False, idempotentHint=True (cache-deduped).
    open_world_hint=True,
)
def fetch_noaa_sst(
    bbox: tuple[float, float, float, float],
    date: str | None = None,
    variable: str | None = "sst",
    # job-0164: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch a daily NOAA sea-surface-temperature (SST) COG for a bbox.

    **What it does:** Subsets the NOAA Coral Reef Watch operational daily 5 km
    global SST product (via the keyless NOAA CoastWatch ERDDAP griddap endpoint)
    to a single day + the requested ocean ``bbox``, and returns a single-band
    float32 COG in degrees Celsius (``style_preset="sst_celsius"``). Pass
    ``variable="anomaly"`` for the SST ANOMALY (observed minus climatology,
    degrees C, diverging blue-red ramp ``sst_anomaly``).

    The CRW product is global, gap-filled (no cloud holes), updated daily with
    ~1 day latency, and covers 1985-present  --  the go-to "how warm is the ocean
    here" / marine-heatwave / coral-bleaching-stress / hurricane-fuel layer.

    **When to use:**
    - User wants ocean / sea-surface temperature for an area or a date
      ("show SST in the Gulf", "how warm is the water off Florida").
    - SST ANOMALY / marine-heatwave context (``variable="anomaly"``): how far
      above or below normal today's water is.
    - Hurricane-intensification context (warm SST = storm fuel), coral-bleaching
      heat stress, fisheries / ecosystem temperature context.

    **When NOT to use:**
    - LAND-surface temperature  --  this is OCEAN ONLY; a land bbox returns an
      honest no-data error (CRW masks land).
    - Air / 2 m temperature  --  use ``fetch_era5_reanalysis`` /
      ``fetch_gridmet`` (those are atmospheric, not sea-surface).
    - Sub-5 km coastal detail  --  CRW is 5 km; for finer SST a higher-res
      product (e.g. MUR 1 km) would be a separate fetcher.

    **Parameters:**
    - ``bbox`` (tuple): ``(min_lon, min_lat, max_lon, max_lat)`` EPSG:4326.
      Required. AOI-scoped (<= 25 deg^2). Must cover ocean.
    - ``date`` (str, optional): ``"YYYY-MM-DD"`` UTC day. Default: the most
      recent likely-published day (today-1); 1985-present.
    - ``variable`` (str, default ``"sst"``): ``"sst"`` for sea-surface
      temperature, ``"anomaly"`` for the SST anomaly vs climatology (both C).

    **Returns:** A ``LayerURI`` (``layer_type="raster"``, ``role="primary"``,
    ``units="degrees Celsius"``) pointing at a single-band float32 COG in the
    ``static-30d``/``noaa_sst`` cache prefix.
    ``style_preset="sst_celsius"`` (or ``"sst_anomaly"`` for the anomaly).

    **Data source:** NOAA Coral Reef Watch Operational Daily 5 km
    (``NOAA_DHW``; ``CRW_SST`` / ``CRW_SSTANOMALY``) via the NOAA CoastWatch
    ERDDAP griddap endpoint. Tier-1 free (no API key).

    FR-CE-8: routed through ``read_through`` so identical ``(bbox, date,
    variable)`` calls reuse the cached SST COG.
    """
    _validate_bbox(bbox)
    q_bbox = _round_bbox(bbox)
    variable_key = _resolve_variable(variable)
    d = _parse_date(date)

    erddap_var, units, style_preset, role, friendly = _VARIABLES[variable_key]

    params = {
        "bbox": list(q_bbox),
        "date": d.isoformat(),
        "variable": variable_key,
        "dataset": _DATASET_ID,
    }

    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="tif",
        fetch_fn=lambda: _fetch_sst_cog_bytes(q_bbox, variable_key, d),
    )
    assert result.uri is not None, (
        "fetch_noaa_sst is cacheable; uri must be set by read_through"
    )

    return LayerURI(
        layer_id=(
            f"noaa-sst-{variable_key}-{d.isoformat()}-"
            f"{q_bbox[0]:.3f}-{q_bbox[1]:.3f}-{q_bbox[2]:.3f}-{q_bbox[3]:.3f}"
        ),
        name=f"{friendly} ({d.isoformat()})",
        layer_type="raster",
        uri=result.uri,
        style_preset=style_preset,
        role=role,
        units=units,
        bbox=q_bbox,
    )
