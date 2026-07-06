"""``fetch_hrrr_forecast`` atomic tool — NOAA HRRR short-term forecast (Wave 4.10 job-A2).

Wraps the NOAA HRRR (High-Resolution Rapid Refresh) 3 km hourly weather
forecast via the University of Utah CHPC HRRR-Zarr mirror published on the
public anonymous AWS S3 bucket ``s3://hrrrzarr/``. Returns a CRS-tagged
GeoTIFF (EPSG:4326) carrying a single forecast field — 2 m temperature,
10 m U/V wind components, or surface accumulated precipitation — at a
single forecast lead time, reprojected from HRRR's native Lambert Conformal
Conic grid and clipped to the requested CONUS bbox.

Research-validated as the operational US short-term weather forecast (NCEP /
EMC convection-resolving model, hourly run cadence, 18 h forecast horizon on
every cycle and 48 h horizon on 00/06/12/18 UTC cycles). The HRRR-Zarr
mirror (Blaylock et al., 2022 *J. Open Source Soft.*) is the canonical
analysis-ready alternative to the NOMADS GRIB2 distribution — chunked
``.zarr`` over S3 sidesteps cfgrib's brittleness and gives single-variable
sub-bbox slices for a small fraction of the full grid's bandwidth.

Mirror layout (verified 2026-06-09 against live S3):

    bucket prefix:   ``s3://hrrrzarr/sfc/<YYYYMMDD>/<YYYYMMDD>_<HH>z_<kind>.zarr/``
                     where ``<kind>`` ∈ {``anl``, ``fcst``}
    level group:     ``<level>/`` — e.g. ``2m_above_ground``, ``10m_above_ground``, ``surface``
    variable group:  ``<level>/<VAR>/`` — outer group carries coord arrays
                     (time, forecast_period, forecast_reference_time,
                     projection_x_coordinate, projection_y_coordinate)
    inner group:     ``<level>/<VAR>/<level>/`` — group whose single child
                     array is the actual data
    data array:      ``<level>/<VAR>/<level>/<VAR>`` — float32 chunked array
                     dims (time, projection_y_coordinate, projection_x_coordinate)
                     shape (48, 1059, 1799) for ``fcst``, (1, 1059, 1799) for ``anl``

The data lives in a doubly-nested group: opening the outer ``<level>/<VAR>``
group gives the coord arrays (and ``forecast_period``/``forecast_reference_time``
as scalars on the time axis) but no data; opening the inner
``<level>/<VAR>/<level>/`` group gives a Dataset whose only ``data_var`` is
the float32 array. We merge the two with ``xarray.merge(compat="override")``
per Blaylock et al.'s documented pattern.  See
``OQ-A2-HRRR-ZARR-DOUBLE-NEST`` for upstream design rationale (chunking
heuristic from CHPC).

Supported variables (single-level surface; all available at every hour):

    Variable name             Group              S3 var  Units   Description
    ---------------------------------------------------------------------------
    "2m_temperature"          2m_above_ground    TMP     K       air temp @ 2 m
    "10m_wind_speed"          10m_above_ground   UGRD+VGRD m s-1 wind SPEED @ 10 m
    "10m_u_wind"              10m_above_ground   UGRD    m s-1   east wind @ 10 m
    "10m_v_wind"              10m_above_ground   VGRD    m s-1   north wind @ 10 m
    "surface_precip_1hr"      surface            APCP_1hr_acc_fcst kg m-2  1-h accum precip

(``kg m-2`` ≡ mm of liquid-water-equivalent precipitation.)

``"10m_wind_speed"`` is a DERIVED variable: the fetcher pulls BOTH the
UGRD (east) and VGRD (north) component slices for the same cycle / forecast
hour / bbox and writes the elementwise magnitude
``sqrt(u^2 + v^2)`` (m s-1) as the single-band COG (NaN nodata preserved).
This is the natural answer to a generic "wind forecast" request — a single
positive wind-speed magnitude field — versus the lone signed U/V components,
which are only useful when direction matters. The derived variable carries
its own cache key and stamps ``style_preset='wind_speed'`` so the publish
registry renders it 0–25 m s-1 viridis.

HRRR projection (constant across every cycle):

    +proj=lcc +lat_1=38.5 +lat_2=38.5 +lat_0=38.5 +lon_0=-97.5
    +x_0=0 +y_0=0 +R=6371229 +units=m +no_defs

(Lambert Conformal Conic over a 6 371 229 m sphere, NCEP/EMC standard.)
Grid extent on the LCC plane: x ∈ [-2 697 520, +2 696 480] m,
y ∈ [-1 587 306, +1 586 694] m at 3 km spacing — fully covers CONUS plus
the Great Lakes / immediate offshore. Outputs are reprojected to EPSG:4326
via ``rioxarray.reproject`` before clipping to bbox.

Cycle latency: a given ``<HH>z`` cycle's full forecast is published to S3
roughly 1.0–1.5 h after the cycle hour (00z cycle becomes complete around
01:30 UTC). The fetcher selects the most recent cycle whose forecast at
the requested ``forecast_hour`` lead is already published, walking backward
in 1-h steps if the requested cycle is not yet posted.

FR-TA-2 atomic tool. FR-CE-8 / FR-DC-3/4: routed through ``read_through``
with ``ttl_class="dynamic-1h"`` — HRRR updates hourly and an in-flight
forecast supersedes any prior cycle's same-valid-time slice within ~1 h.

Cache key composition: ``(bbox-6dp, variable, cycle_date, cycle_hour,
forecast_hour)``. The cycle identifiers are present so two adjacent cycles
predicting the same valid time map to distinct cache entries (their
forecasts can differ); downstream UX may dedup by valid_time if desired.

``supports_global_query=False`` — HRRR is CONUS-only. A bbox outside the
CONUS LCC extent raises ``HRRRForecastEmptyError``.

Tier-1 (free; no auth). NOAA Big Data Program S3 anonymous access.
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

__all__ = ["fetch_hrrr_forecast"]

logger = logging.getLogger("grace2_agent.tools.fetch_hrrr_forecast")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class HRRRForecastError(RuntimeError):
    """Base class for fetch_hrrr_forecast failures.

    ``error_code`` maps to the WebSocket A.6 error frame emitted by the agent
    surface. ``retryable`` guides FR-AS-11 retry logic.
    """

    error_code: str = "HRRR_FORECAST_ERROR"
    retryable: bool = True


class HRRRForecastInputError(HRRRForecastError):
    """Bad inputs (unknown variable, malformed bbox, forecast_hour out of range)."""

    error_code = "HRRR_FORECAST_INPUT_ERROR"
    retryable = False


class HRRRForecastUpstreamError(HRRRForecastError):
    """S3 listing / zarr read / xarray decode / network failure (retryable)."""

    error_code = "HRRR_FORECAST_UPSTREAM_ERROR"
    retryable = True


class HRRRForecastEmptyError(HRRRForecastError):
    """bbox is outside the HRRR CONUS LCC extent or produced no finite pixels."""

    error_code = "HRRR_FORECAST_EMPTY"
    retryable = False


class HRRRForecastNotAvailableError(HRRRForecastError):
    """Requested cycle/forecast_hour combination is not yet published on S3.

    HRRR cycles post ~1–1.5 h after the cycle hour. The fetcher walks backward
    looking for a cycle whose forecast slice is published; this surfaces when
    the search exhausts the backstop window (default 6 h) without finding one.
    """

    error_code = "HRRR_FORECAST_NOT_AVAILABLE"
    retryable = True


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

_BUCKET = "hrrrzarr"
_KIND_FCST = "fcst"

# (display_variable, level_group, s3_var_name, native_units)
_VARIABLE_SPEC: dict[str, tuple[str, str, str]] = {
    # variable → (level_group, s3_var_name, native_units)
    "2m_temperature": ("2m_above_ground", "TMP", "K"),
    "10m_u_wind": ("10m_above_ground", "UGRD", "m s-1"),
    "10m_v_wind": ("10m_above_ground", "VGRD", "m s-1"),
    "surface_precip_1hr": ("surface", "APCP_1hr_acc_fcst", "kg m-2"),
}

# Derived variable: wind SPEED magnitude = sqrt(u^2 + v^2) over the 10 m
# UGRD/VGRD components. Not present in _VARIABLE_SPEC because it has no single
# S3 array — the fetcher pulls both component slices and combines them. The
# components it depends on are named here so the fetch path can resolve them.
_DERIVED_WIND_SPEED = "10m_wind_speed"
_WIND_SPEED_COMPONENTS = ("10m_u_wind", "10m_v_wind")
_WIND_SPEED_UNITS = "m s-1"
_WIND_SPEED_STYLE_PRESET = "wind_speed"

# Every variable the tool accepts, derived ones included. Used by validation,
# the catalog enum, and the LayerURI builder.
_SUPPORTED_VARIABLES: tuple[str, ...] = (
    *_VARIABLE_SPEC.keys(),
    _DERIVED_WIND_SPEED,
)

# HRRR LCC projection string (NCEP/EMC standard).
_HRRR_PROJ4 = (
    "+proj=lcc +lat_1=38.5 +lat_2=38.5 +lat_0=38.5 +lon_0=-97.5 "
    "+x_0=0 +y_0=0 +R=6371229 +units=m +no_defs"
)

# CONUS LCC grid extent (verified live 2026-06-09 from coord arrays).
_HRRR_X_MIN = -2_697_520.0
_HRRR_X_MAX = +2_696_480.0
_HRRR_Y_MIN = -1_587_306.0
_HRRR_Y_MAX = +1_586_694.0

# Approximate CONUS WGS84 envelope (loose; covers HRRR LCC bounding region).
_CONUS_LON_MIN = -134.0
_CONUS_LON_MAX = -60.0
_CONUS_LAT_MIN = 21.0
_CONUS_LAT_MAX = 53.0

# HRRR forecast horizons.
# Standard cycles (every hour) publish 18-h horizon.
# 00 / 06 / 12 / 18 UTC cycles publish 48-h horizon.
_MAX_FORECAST_HOUR_STANDARD = 18
_MAX_FORECAST_HOUR_EXTENDED = 48
_EXTENDED_CYCLES = {0, 6, 12, 18}

# Cycle backstop window: when walking backward looking for a published cycle,
# go no more than this many hours back before giving up.
_CYCLE_BACKSTOP_HOURS = 6


# ---------------------------------------------------------------------------
# AtomicToolMetadata — registered once at import time.
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="fetch_hrrr_forecast",
    ttl_class="dynamic-1h",
    source_class="hrrr",
    cacheable=True,
)


# ---------------------------------------------------------------------------
# Payload-MB estimator (Wave 1.5 chat-warning system).
# ---------------------------------------------------------------------------


def estimate_payload_mb(
    bbox: tuple[float, float, float, float] | None = None,
    variable: str | None = None,
    forecast_hour: int | None = None,
    **_kw: Any,
) -> float:
    """Estimate output GeoTIFF size in MB for a given call (Wave 1.5 surface).

    HRRR native resolution is 3 km; the CONUS LCC grid is 1799 × 1059
    = ~1.9 M cells at float32 (≈7.6 MB raw, ~3 MB DEFLATE-compressed) for
    a single time step + single variable.

    We scale by the bbox fraction of CONUS area. A full-CONUS slice lands
    around 5 MB; a 1° × 1° bbox lands around 0.05 MB. Used by the
    tool-payload-warning envelope (Wave 1.5 chat-warning system).

    A bbox of ``None`` is illegal here (this tool declares
    ``supports_global_query=False``) but we still return a sane number so
    the estimator never raises.
    """
    if bbox is None:
        return 5.0
    try:
        west, south, east, north = bbox
        sq_deg = max(0.0, (east - west)) * max(0.0, (north - south))
    except (TypeError, ValueError):
        return 5.0

    # Approximate CONUS area: ~74° × ~32° ≈ 2368 sq° (loose bound including
    # Great Lakes / coastal margin captured by HRRR).
    conus_sq_deg = 74.0 * 32.0
    full_conus_mb = 5.0
    frac = min(1.0, sq_deg / conus_sq_deg)
    # Floor at 0.05 MB so tiny bboxes still report a non-zero payload.
    return max(0.05, full_conus_mb * frac)


# ---------------------------------------------------------------------------
# Validation helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Raise ``HRRRForecastInputError`` if bbox is invalid or non-CONUS."""
    if len(bbox) != 4:
        raise HRRRForecastInputError(
            f"bbox must be (west, south, east, north); got {bbox!r}"
        )
    west, south, east, north = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise HRRRForecastInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= west <= 180.0 and -180.0 <= east <= 180.0):
        raise HRRRForecastInputError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= south <= 90.0 and -90.0 <= north <= 90.0):
        raise HRRRForecastInputError(f"bbox lat out of [-90,90]: {bbox!r}")
    if west >= east or south >= north:
        raise HRRRForecastInputError(
            f"bbox is degenerate (west < east, south < north required): {bbox!r}"
        )
    # CONUS-only soft gate: refuse bboxes that lie entirely outside CONUS.
    if (
        east < _CONUS_LON_MIN
        or west > _CONUS_LON_MAX
        or north < _CONUS_LAT_MIN
        or south > _CONUS_LAT_MAX
    ):
        raise HRRRForecastInputError(
            f"bbox={bbox} lies outside HRRR CONUS coverage "
            f"(~{_CONUS_LON_MIN}..{_CONUS_LON_MAX} lon, "
            f"~{_CONUS_LAT_MIN}..{_CONUS_LAT_MAX} lat). HRRR is CONUS-only; "
            f"supports_global_query=False."
        )


def _round_bbox_to_6dp(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Round bbox coords to 6dp (~0.1 m) for cache-key stability."""
    return tuple(round(v, 6) for v in bbox)  # type: ignore[return-value]


def _validate_variable(variable: str) -> None:
    """Raise ``HRRRForecastInputError`` for unsupported variable names."""
    if not isinstance(variable, str):
        raise HRRRForecastInputError(
            f"variable must be a str; got {type(variable).__name__}"
        )
    if variable not in _SUPPORTED_VARIABLES:
        raise HRRRForecastInputError(
            f"unsupported HRRR variable {variable!r}; allowed: "
            f"{sorted(_SUPPORTED_VARIABLES)}"
        )


def _validate_forecast_hour(forecast_hour: int, cycle_hour: int) -> None:
    """Raise ``HRRRForecastInputError`` if forecast_hour exceeds cycle horizon."""
    if not isinstance(forecast_hour, int):
        raise HRRRForecastInputError(
            f"forecast_hour must be int; got {type(forecast_hour).__name__}"
        )
    if forecast_hour < 0:
        raise HRRRForecastInputError(
            f"forecast_hour must be >= 0; got {forecast_hour}"
        )
    max_h = (
        _MAX_FORECAST_HOUR_EXTENDED
        if cycle_hour in _EXTENDED_CYCLES
        else _MAX_FORECAST_HOUR_STANDARD
    )
    if forecast_hour > max_h:
        raise HRRRForecastInputError(
            f"forecast_hour={forecast_hour} exceeds the {cycle_hour:02d}z cycle "
            f"horizon (max {max_h} h). 00/06/12/18z cycles publish 48 h; "
            f"all other cycles publish {_MAX_FORECAST_HOUR_STANDARD} h."
        )


# ---------------------------------------------------------------------------
# S3 / Zarr helpers.
# ---------------------------------------------------------------------------


def _cycle_key(cycle_date: _dt.date, cycle_hour: int) -> str:
    """Return the YYYYMMDD_HHz_fcst.zarr path component for a cycle."""
    return f"{cycle_date.strftime('%Y%m%d')}_{cycle_hour:02d}z_{_KIND_FCST}.zarr"


def _build_zarr_paths(
    cycle_date: _dt.date,
    cycle_hour: int,
    level: str,
    s3_var: str,
) -> tuple[str, str]:
    """Build the (outer_group, inner_group) S3 paths for one (cycle, variable) combo.

    HRRR-Zarr's nested layout (verified live 2026-06-09):

        <cycle>.zarr/
            <level>/                       <- group
                <s3_var>/                  <- outer_group  (carries coords)
                    forecast_period        (zarr array, dim=time)
                    forecast_reference_time (zarr array, scalar)
                    projection_x_coordinate (zarr array, dim=x)
                    projection_y_coordinate (zarr array, dim=y)
                    time                   (zarr array, dim=time)
                    <level>/               <- inner_group  (carries data array)
                        <s3_var>           (zarr ARRAY, dims=(time, y, x))

    We open ``outer_group`` and ``inner_group`` as xarray datasets and merge
    them; the data array materializes inside the merged result as the
    leaf ``s3_var`` variable.
    """
    date_str = cycle_date.strftime("%Y%m%d")
    cycle = _cycle_key(cycle_date, cycle_hour)
    base = f"{_BUCKET}/sfc/{date_str}/{cycle}/{level}/{s3_var}"
    outer = f"s3://{base}"
    inner = f"s3://{base}/{level}"
    return outer, inner


def _s3_exists(fs: Any, path_no_proto: str) -> bool:
    """Return True iff the s3 path exists (cheap probe)."""
    try:
        return bool(fs.exists(path_no_proto))
    except Exception:  # noqa: BLE001 — treat any S3 error as "doesn't exist"
        return False


def _resolve_cycle(
    fs: Any,
    target_cycle: _dt.datetime,
    level: str,
    s3_var: str,
    forecast_hour: int,
) -> tuple[_dt.date, int]:
    """Walk backward from target_cycle looking for a published cycle.

    HRRR posts cycles ~1.0–1.5 h after their cycle hour. The caller passes a
    candidate cycle; if its forecast Zarr is not yet on S3, we step back 1 h
    at a time up to ``_CYCLE_BACKSTOP_HOURS`` total.

    Returns ``(cycle_date, cycle_hour)``. Raises
    ``HRRRForecastNotAvailableError`` if the backstop is exhausted.
    """
    for back in range(0, _CYCLE_BACKSTOP_HOURS + 1):
        candidate = target_cycle - _dt.timedelta(hours=back)
        # Skip cycles where the requested forecast_hour exceeds the horizon.
        if candidate.hour not in _EXTENDED_CYCLES and forecast_hour > _MAX_FORECAST_HOUR_STANDARD:
            continue
        cycle_path = (
            f"{_BUCKET}/sfc/{candidate.strftime('%Y%m%d')}/"
            f"{_cycle_key(candidate.date(), candidate.hour)}/{level}/{s3_var}"
        )
        if _s3_exists(fs, cycle_path):
            logger.info(
                "HRRR cycle resolved: %sz %s (walked back %d h from target)",
                candidate.strftime("%Y%m%d_%H"),
                level,
                back,
            )
            return candidate.date(), candidate.hour
    raise HRRRForecastNotAvailableError(
        f"no HRRR cycle published within {_CYCLE_BACKSTOP_HOURS} h backstop "
        f"of target {target_cycle.isoformat()}; S3 mirror may be lagging."
    )


# ---------------------------------------------------------------------------
# Zarr → GeoTIFF (LCC → EPSG:4326).
# ---------------------------------------------------------------------------


def _open_component_4326(
    cycle_date: _dt.date,
    cycle_hour: int,
    component_variable: str,
    forecast_hour: int,
    bbox: tuple[float, float, float, float],
) -> Any:
    """Open ONE plain HRRR-Zarr component, reproject to EPSG:4326, clip to bbox.

    Returns the clipped ``xarray.DataArray`` (float32-valued, EPSG:4326) for the
    requested single-array ``component_variable`` (one of the keys in
    ``_VARIABLE_SPEC``). Factored out of ``_zarr_slice_to_geotiff_bytes`` so the
    derived ``10m_wind_speed`` variable can open both wind components and combine
    them on the same EPSG:4326 grid before writing a single COG.

    Raises:
        ``HRRRForecastUpstreamError``: zarr open / xarray decode / reproject failure.
        ``HRRRForecastEmptyError``: bbox produced an empty window after clip.
    """
    import fsspec
    import rioxarray  # noqa: F401 — registers .rio accessor
    import xarray as xr

    level, s3_var, _units = _VARIABLE_SPEC[component_variable]
    outer_path, inner_path = _build_zarr_paths(cycle_date, cycle_hour, level, s3_var)

    try:
        from ._public_s3 import public_s3fs_kwargs
        outer_mapper = fsspec.get_mapper(outer_path, **public_s3fs_kwargs("us-west-1"))
        inner_mapper = fsspec.get_mapper(inner_path, **public_s3fs_kwargs("us-west-1"))
        ds_outer = xr.open_zarr(outer_mapper, consolidated=False)
        ds_inner = xr.open_zarr(inner_mapper, consolidated=False)
    except Exception as exc:  # noqa: BLE001
        raise HRRRForecastUpstreamError(
            f"failed to open HRRR zarr at {outer_path} / {inner_path}: {exc}"
        ) from exc

    try:
        ds = xr.merge([ds_outer, ds_inner], compat="override")
        if s3_var not in ds.data_vars:
            raise HRRRForecastUpstreamError(
                f"variable {s3_var!r} not present in merged zarr; "
                f"data_vars={list(ds.data_vars)}"
            )

        # Pick the forecast lead time. For ``fcst`` zarrs, the time axis carries
        # 48 (or 18) hourly slices starting at cycle_hour+1. We expose
        # forecast_hour=0 as "analysis-time slice"; forecast_hour=N as the Nth
        # available lead.  Some cycles (extended 00/06/12/18z) carry 48 entries;
        # standard cycles carry 18 entries. We clamp by index.
        time_len = int(ds.sizes.get("time", 0))
        if time_len == 0:
            raise HRRRForecastUpstreamError(
                "HRRR zarr 'time' dim is empty; cycle may be partially published"
            )

        # Map forecast_hour (1..18 or 0..48) to time index. For ``fcst`` zarrs
        # the time array is sorted ascending starting at cycle+1h; index 0 ==
        # +1 h forecast. We accept forecast_hour=0 by aliasing to index 0
        # (the +1 h forecast — closest available analog).
        idx = max(0, min(time_len - 1, forecast_hour - 1 if forecast_hour > 0 else 0))
        da = ds[s3_var].isel(time=idx)

        # Tag CRS + write spatial dim names so rioxarray can reproject.
        da = da.rename(
            {"projection_x_coordinate": "x", "projection_y_coordinate": "y"}
        )
        da.rio.write_crs(_HRRR_PROJ4, inplace=True)
        da.rio.set_spatial_dims(x_dim="x", y_dim="y", inplace=True)

        # Reproject to EPSG:4326 (clipping in source CRS is faster but we'd
        # need to project the bbox corners through the LCC inverse; reprojecting
        # the field and then clipping in WGS84 is the simpler, accuracy-safe
        # path for small-ish CONUS bboxes).
        try:
            da_4326 = da.rio.reproject("EPSG:4326")
        except Exception as exc:  # noqa: BLE001
            raise HRRRForecastUpstreamError(
                f"rioxarray reproject HRRR LCC → EPSG:4326 failed: {exc}"
            ) from exc

        west, south, east, north = bbox
        try:
            da_clipped = da_4326.rio.clip_box(
                minx=west, miny=south, maxx=east, maxy=north, crs="EPSG:4326"
            )
        except Exception as exc:  # noqa: BLE001
            raise HRRRForecastEmptyError(
                f"bbox={bbox} produced an empty HRRR window after clip: {exc}"
            ) from exc

        if da_clipped.size == 0:
            raise HRRRForecastEmptyError(
                f"bbox={bbox} produced an empty HRRR window after clip"
            )

        # Materialize values now while the zarr datasets are still open, so the
        # caller can safely close ds_outer / ds_inner in the finally block.
        return da_clipped.compute()
    finally:
        try:
            ds_outer.close()
            ds_inner.close()
        except Exception:  # noqa: BLE001
            pass


def _write_da_to_cog_bytes(
    da_out: Any,
    *,
    variable: str,
    units: str,
    cycle_date: _dt.date,
    cycle_hour: int,
    forecast_hour: int,
    bbox: tuple[float, float, float, float],
) -> bytes:
    """Stamp metadata on ``da_out`` and write it as a float32 COG, returning bytes.

    Raises ``HRRRForecastEmptyError`` if the array carries no finite pixels.
    """
    import numpy as np

    arr = np.asarray(da_out.values, dtype=np.float32)
    if not np.isfinite(arr).any():
        raise HRRRForecastEmptyError(
            f"bbox={bbox} produced no finite HRRR pixels (all-NaN window)"
        )

    out_fd, out_path = tempfile.mkstemp(suffix=".tif", prefix="grace2_hrrr_")
    os.close(out_fd)
    try:
        da_out = da_out.astype("float32")
        da_out.attrs["units"] = units
        da_out.attrs["source"] = "HRRR_hrrrzarr"
        da_out.attrs["variable"] = variable
        da_out.attrs["cycle"] = f"{cycle_date.isoformat()}T{cycle_hour:02d}:00Z"
        da_out.attrs["forecast_hour"] = forecast_hour
        da_out.attrs["tool"] = "fetch_hrrr_forecast"
        try:
            da_out.rio.to_raster(
                out_path,
                driver="COG",
                dtype="float32",
                compress="DEFLATE",
                nodata=float("nan"),
            )
        except Exception:  # noqa: BLE001 — fall back to GTiff if COG fails
            da_out.rio.to_raster(
                out_path,
                driver="GTiff",
                dtype="float32",
                compress="DEFLATE",
                nodata=float("nan"),
            )

        with open(out_path, "rb") as f:
            cog_bytes = f.read()
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass

    logger.info(
        "fetch_hrrr_forecast wrote %d-byte COG (variable=%s cycle=%s "
        "F%03d shape=%s min=%.3f max=%.3f mean=%.3f)",
        len(cog_bytes),
        variable,
        f"{cycle_date.strftime('%Y%m%d')}_{cycle_hour:02d}z",
        forecast_hour,
        tuple(arr.shape),
        float(np.nanmin(arr)),
        float(np.nanmax(arr)),
        float(np.nanmean(arr)),
    )
    return cog_bytes


def _zarr_slice_to_geotiff_bytes(
    cycle_date: _dt.date,
    cycle_hour: int,
    variable: str,
    forecast_hour: int,
    bbox: tuple[float, float, float, float],
) -> bytes:
    """Open the HRRR-Zarr slice(s), reproject to EPSG:4326, clip to bbox, write COG bytes.

    For a plain single-array variable this opens one component and writes it
    directly. For the derived ``10m_wind_speed`` it opens BOTH the UGRD and VGRD
    component slices for the same cycle / forecast hour / bbox and writes the
    elementwise magnitude ``sqrt(u^2 + v^2)`` (NaN preserved) as the single-band
    float32 COG, units ``m s-1``.

    Raises:
        ``HRRRForecastUpstreamError``: zarr open / xarray decode failure.
        ``HRRRForecastEmptyError``: bbox produced no finite pixels after clip.
    """
    try:
        import fsspec  # noqa: F401 — required for the get_mapper
        import numpy as np  # noqa: F401 — used by helpers
        import rioxarray  # noqa: F401 — registers .rio accessor
        import xarray as xr  # noqa: F401
    except ImportError as exc:
        raise HRRRForecastUpstreamError(
            f"required deps not available (fsspec / xarray / rioxarray / numpy): {exc}"
        ) from exc

    if variable == _DERIVED_WIND_SPEED:
        import numpy as np

        u_var, v_var = _WIND_SPEED_COMPONENTS
        da_u = _open_component_4326(
            cycle_date, cycle_hour, u_var, forecast_hour, bbox
        )
        da_v = _open_component_4326(
            cycle_date, cycle_hour, v_var, forecast_hour, bbox
        )

        # Align the two component grids defensively. Both ride the identical
        # HRRR LCC grid and undergo the identical reproject + clip, so they are
        # already coincident; xarray binary ops align on coords regardless.
        # Magnitude = sqrt(u^2 + v^2). NaN in either component propagates as NaN
        # (np.hypot preserves NaN), preserving the nodata mask.
        speed = xr.apply_ufunc(np.hypot, da_u, da_v, keep_attrs=False)
        speed = speed.astype("float32")
        # apply_ufunc drops the rio CRS accessor state on some xarray versions;
        # re-stamp CRS + spatial dims so to_raster writes a georeferenced COG.
        speed.rio.write_crs("EPSG:4326", inplace=True)
        try:
            speed.rio.set_spatial_dims(x_dim="x", y_dim="y", inplace=True)
        except Exception:  # noqa: BLE001 — dims already named x/y in most paths
            pass

        return _write_da_to_cog_bytes(
            speed,
            variable=variable,
            units=_WIND_SPEED_UNITS,
            cycle_date=cycle_date,
            cycle_hour=cycle_hour,
            forecast_hour=forecast_hour,
            bbox=bbox,
        )

    da_clipped = _open_component_4326(
        cycle_date, cycle_hour, variable, forecast_hour, bbox
    )
    return _write_da_to_cog_bytes(
        da_clipped,
        variable=variable,
        units=_VARIABLE_SPEC[variable][2],
        cycle_date=cycle_date,
        cycle_hour=cycle_hour,
        forecast_hour=forecast_hour,
        bbox=bbox,
    )


# ---------------------------------------------------------------------------
# Fetch function (passed to read_through).
# ---------------------------------------------------------------------------


def _fetch_hrrr_bytes(
    cycle_date: _dt.date,
    cycle_hour: int,
    variable: str,
    forecast_hour: int,
    bbox: tuple[float, float, float, float],
) -> bytes:
    """End-to-end: open zarr → reproject → clip → write COG bytes."""
    return _zarr_slice_to_geotiff_bytes(
        cycle_date=cycle_date,
        cycle_hour=cycle_hour,
        variable=variable,
        forecast_hour=forecast_hour,
        bbox=bbox,
    )


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
def fetch_hrrr_forecast(
    bbox: tuple[float, float, float, float],
    variable: str = "2m_temperature",
    forecast_hour: int = 1,
    cycle: str | None = None,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """NOAA HRRR 3 km short-term weather forecast — Tier-1 CONUS fetcher.

    What it does: returns a CRS-tagged GeoTIFF (EPSG:4326) of a single HRRR
    forecast field at a single forecast lead time, reprojected from the
    native Lambert Conformal Conic grid and clipped to the requested CONUS
    bbox. Wraps the University of Utah CHPC HRRR-Zarr mirror on AWS S3
    (anonymous, Tier-1 free, no API key). Resolves the most-recently
    published HRRR cycle whose forecast at the requested lead time is on
    S3, walking up to 6 h backward if the in-progress cycle has not yet
    posted (the NOAA Big Data Program mirror lags the cycle hour by
    ~1.0–1.5 h).

    When to use:
    - User asks for "the forecast for the next few hours" anywhere in CONUS
      ("what's the wind looking like in Tampa tomorrow morning?", "is rain
      coming through Denver this afternoon?", "show me HRRR temperature
      forecast for Chicago").
    - A GENERIC wind request ("wind forecast", "how windy will it be",
      "show me the wind", "wind speed over Houston") → use
      ``variable="10m_wind_speed"``. This returns a SINGLE positive wind-speed
      magnitude field (``sqrt(u^2 + v^2)``, m s-1) — the natural answer to "how
      windy". Only reach for the lone signed ``10m_u_wind`` / ``10m_v_wind``
      components when the user explicitly needs wind DIRECTION or a vector
      component (e.g. onshore/offshore decomposition).
    - Driving a hazard model with near-real-time meteorological forcing on
      a US-side bbox: wind input to SFINCS or HEC-RAS storm surge, precip
      input to SFINCS pluvial, temperature for snow/fire-weather context.
    - "Observed vs forecast" comparisons against ASOS/METAR (or RAWS for
      fire weather).
    - Any short-term (≤ 18 h, or ≤ 48 h on 00/06/12/18z cycles) US weather
      decision-support overlay.

    When NOT to use:
    - Historical / reanalysis weather — use ``fetch_era5_reanalysis``
      (global, 0.25°, 1940–present) instead. HRRR-Zarr archives ~2016+
      cycles but the tool exposes only the current operational forecast.
    - Outside CONUS (Mexico interior, Caribbean except direct CONUS spillover,
      Alaska, Hawaii, the open Pacific or Atlantic) — HRRR is CONUS-only;
      bbox outside the coverage envelope raises ``HRRRForecastInputError``.
      Use ``fetch_era5_reanalysis`` for global, ECMWF AIFS/IFS for global
      forecast.
    - Observed precipitation accumulation — use ``fetch_mrms_qpe``
      (1 km gauge-corrected). HRRR's APCP is model-predicted; MRMS is the
      observation.
    - Observed radar reflectivity — use ``fetch_nexrad_reflectivity``.
    - Watches / warnings — use ``fetch_nws_alerts_conus`` or
      ``fetch_nws_event`` instead. Those carry the official advisory text;
      HRRR carries raw model output.
    - Hourly forecast horizons beyond 18 h on non-extended cycles, or
      beyond 48 h on extended cycles — those leads are not in HRRR.

    Parameters:
        bbox: ``(west, south, east, north)`` in EPSG:4326 (WGS84 decimal
            degrees). Must intersect CONUS coverage (~lon -134..-60,
            lat 21..53). Example: ``(-82.4, 26.3, -81.6, 26.9)`` for the
            Fort Myers / Lee County area.
        variable: one of ``"2m_temperature"`` (K), ``"10m_wind_speed"``
            (m s-1, DERIVED wind-speed magnitude ``sqrt(u^2 + v^2)`` — the
            answer to a generic "wind" request), ``"10m_u_wind"`` (m s-1,
            east component), ``"10m_v_wind"`` (m s-1, north component),
            ``"surface_precip_1hr"`` (kg m-2 = mm liquid-water equivalent,
            1-h accumulation). Default ``"2m_temperature"``. For any
            non-directional wind question pass ``"10m_wind_speed"``.
        forecast_hour: integer forecast lead time in hours (1 = +1 h from
            cycle start). Range 1–18 for standard cycles, 1–48 for the
            extended 00/06/12/18z cycles. ``0`` is accepted and aliased to
            the +1 h slice (HRRR analysis-time slices live in a separate
            ``_anl.zarr`` not exposed here). Default ``1``.
        cycle: optional ISO-8601 cycle timestamp like ``"2026-06-09T00:00:00Z"``
            to pin a specific cycle. Default ``None`` → use the most recent
            published cycle (walks backward up to 6 h from current UTC).

    Returns:
        A ``LayerURI`` pointing at a COG in the cache bucket
        ``gs://grace-2-hazard-prod-cache/cache/dynamic-1h/hrrr/<key>.tif``
        carrying the requested variable's forecast slice, EPSG:4326,
        float32, NaN nodata. ``layer_type="raster"``, ``role="primary"``,
        ``units`` per the variable (``"K"``, ``"m s-1"``, ``"kg m-2"``).
        Downstream consumers (``publish_layer``, ``compute_zonal_statistics``,
        SFINCS forcing composers) read the COG and treat it as a single-band
        scalar field.

    Cross-tool dependencies:
        - Consumes nothing (Tier-1 substrate fetcher; no upstream tool).
        - Feeds: ``publish_layer`` (visualization on the web map),
          ``compute_zonal_statistics`` (aggregate to admin boundaries),
          ``model_flood_scenario`` and downstream SFINCS composers (precip
          / wind forcing), ``clip_raster_to_polygon`` (further sub-clip),
          ``aggregate_claims_across_sources`` (combine with MRMS, ERA5,
          NWS alerts for compound claims).

    Raises:
        ``HRRRForecastInputError``: bad bbox / variable / forecast_hour
            (retryable=False).
        ``HRRRForecastEmptyError``: bbox falls outside HRRR coverage or
            yields all-NaN after clip (retryable=False).
        ``HRRRForecastNotAvailableError``: requested cycle is not yet on
            S3 within the 6 h backstop (retryable=True).
        ``HRRRForecastUpstreamError``: S3 / zarr / reprojection failure
            (retryable=True).

    FR-CE-8: routed through ``read_through`` with ``ttl_class="dynamic-1h"``
    so identical ``(bbox, variable, cycle, forecast_hour)`` calls reuse
    the cached COG within the hourly window. The cache key includes the
    cycle identifier so distinct cycles predicting the same valid time
    map to distinct cache entries.
    """
    # ---- Input validation ----
    _validate_bbox(bbox)
    _validate_variable(variable)

    # ---- Cycle resolution ----
    # Lazy import s3fs / fsspec so the deep tests can monkeypatch fs.
    try:
        import fsspec
    except ImportError as exc:
        raise HRRRForecastUpstreamError(
            f"fsspec not available: {exc}"
        ) from exc

    try:
        from ._public_s3 import public_s3fs_kwargs
        fs = fsspec.filesystem("s3", **public_s3fs_kwargs("us-west-1"))
    except Exception as exc:  # noqa: BLE001
        raise HRRRForecastUpstreamError(
            f"s3 filesystem init failed (is s3fs installed?): {exc}"
        ) from exc

    if cycle is None:
        target_cycle = _dt.datetime.now(_dt.timezone.utc).replace(
            minute=0, second=0, microsecond=0
        )
    else:
        try:
            # Accept trailing "Z" for ISO-8601 UTC.
            target_cycle = _dt.datetime.fromisoformat(cycle.replace("Z", "+00:00"))
        except (ValueError, AttributeError) as exc:
            raise HRRRForecastInputError(
                f"cycle must be ISO-8601 UTC (e.g. '2026-06-09T00:00:00Z'); "
                f"got {cycle!r}: {exc}"
            ) from exc
        if target_cycle.tzinfo is None:
            target_cycle = target_cycle.replace(tzinfo=_dt.timezone.utc)

    # Validate forecast_hour against the resolved cycle hour. (Resolution
    # may bump us to an earlier hour, but the user-supplied cycle's hour is
    # the right one to validate against.)
    _validate_forecast_hour(forecast_hour, target_cycle.hour)

    # Resolve the cycle against a concrete on-S3 component. The derived
    # ``10m_wind_speed`` has no single S3 array, so probe its UGRD component
    # (publishing is atomic per cycle — if UGRD is posted, VGRD is too).
    if variable == _DERIVED_WIND_SPEED:
        level, s3_var, _units = _VARIABLE_SPEC[_WIND_SPEED_COMPONENTS[0]]
        result_units = _WIND_SPEED_UNITS
        result_style_preset = _WIND_SPEED_STYLE_PRESET
    else:
        level, s3_var, _units = _VARIABLE_SPEC[variable]
        result_units = _VARIABLE_SPEC[variable][2]
        result_style_preset = f"hrrr_{variable}"
    cycle_date, cycle_hour = _resolve_cycle(
        fs=fs,
        target_cycle=target_cycle,
        level=level,
        s3_var=s3_var,
        forecast_hour=forecast_hour,
    )

    # ---- Cache-key params ----
    q_bbox = _round_bbox_to_6dp(bbox)
    params: dict[str, Any] = {
        "variable": variable,
        "bbox": list(q_bbox),
        "cycle_date": cycle_date.isoformat(),
        "cycle_hour": cycle_hour,
        "forecast_hour": forecast_hour,
    }

    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="tif",
        fetch_fn=lambda: _fetch_hrrr_bytes(
            cycle_date=cycle_date,
            cycle_hour=cycle_hour,
            variable=variable,
            forecast_hour=forecast_hour,
            bbox=q_bbox,
        ),
    )
    assert result.uri is not None, (
        "fetch_hrrr_forecast is cacheable; uri must be set by read_through"
    )

    cycle_label = f"{cycle_date.isoformat()}T{cycle_hour:02d}:00Z"
    return LayerURI(
        layer_id=(
            f"hrrr-{variable.replace('_', '-')}-"
            f"{cycle_date.strftime('%Y%m%d')}-{cycle_hour:02d}z-"
            f"f{forecast_hour:03d}-"
            f"{q_bbox[0]:.4f}-{q_bbox[1]:.4f}"
        ),
        name=(
            f"HRRR Forecast — {variable.replace('_', ' ').title()} "
            f"(cycle {cycle_label}, F{forecast_hour:03d})"
        ),
        layer_type="raster",
        uri=result.uri,
        style_preset=result_style_preset,
        role="primary",
        units=result_units,
    )
