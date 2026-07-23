"""``fetch_hrrr_smoke`` atomic tool — NOAA HRRR-Smoke forecast (Wave 4.10 job-A13).
"""

from __future__ import annotations

import datetime as _dt
import logging
import math
import os
import tempfile
from typing import Any

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.cache import read_through

__all__ = ["fetch_hrrr_smoke"]

logger = logging.getLogger("trid3nt_server.tools.fetchers.weather.fetch_hrrr_smoke")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class HRRRSmokeError(RuntimeError):
    """Base class for fetch_hrrr_smoke failures.

    ``error_code`` maps to the WebSocket A.6 error frame emitted by the agent
    surface. ``retryable`` guides FR-AS-11 retry logic.
    """

    error_code: str = "HRRR_SMOKE_ERROR"
    retryable: bool = True


class HRRRSmokeInputError(HRRRSmokeError):
    """Bad inputs (unknown variable, malformed bbox, forecast_hour out of range)."""

    error_code = "HRRR_SMOKE_INPUT_ERROR"
    retryable = False


class HRRRSmokeUpstreamError(HRRRSmokeError):
    """S3 listing / zarr read / xarray decode / network failure (retryable)."""

    error_code = "HRRR_SMOKE_UPSTREAM_ERROR"
    retryable = True


class HRRRSmokeEmptyError(HRRRSmokeError):
    """bbox is outside the HRRR CONUS LCC extent or produced no finite pixels."""

    error_code = "HRRR_SMOKE_EMPTY"
    retryable = False


class HRRRSmokeNotAvailableError(HRRRSmokeError):
    """Requested cycle/forecast_hour combination is not yet published on S3.

    HRRR cycles post ~1–1.5 h after the cycle hour. The fetcher walks backward
    looking for a cycle whose forecast slice is published; this surfaces when
    the search exhausts the backstop window (default 6 h) without finding one.
    """

    error_code = "HRRR_SMOKE_NOT_AVAILABLE"
    retryable = True


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

_BUCKET = "hrrrzarr"
_KIND_FCST = "fcst"

# variable → (level_group, s3_var_name, native_units, friendly_units)
_VARIABLE_SPEC: dict[str, tuple[str, str, str, str]] = {
    "near_surface_smoke": (
        "8m_above_ground",
        "MASSDEN",
        "kg m-3",
        "kg m-3",
    ),
    "smoke_column_mass": (
        "entire_atmosphere_single_layer",
        "COLMD",
        "kg m-2",
        "kg m-2",
    ),
    "aerosol_optical_depth": (
        "entire_atmosphere_single_layer",
        "AOTK",
        "1",
        "dimensionless",
    ),
}

# HRRR LCC projection string (NCEP/EMC standard, shared with fetch_hrrr_forecast).
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

# HRRR forecast horizons (identical to standard HRRR).
_MAX_FORECAST_HOUR_STANDARD = 18
_MAX_FORECAST_HOUR_EXTENDED = 48
_EXTENDED_CYCLES = {0, 6, 12, 18}

# Cycle backstop window: when walking backward looking for a published cycle,
# go no more than this many hours back before giving up.
_CYCLE_BACKSTOP_HOURS = 6

# HRRR-Smoke fill value (verified from .zmetadata).
_FILL_VALUE = -9999.0


# ---------------------------------------------------------------------------
# AtomicToolMetadata — registered once at import time.
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="fetch_hrrr_smoke",
    ttl_class="dynamic-1h",
    source_class="hrrr_smoke",
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

    HRRR-Smoke native resolution is 3 km on the HRRR LCC grid (1799 × 1059
    = ~1.9 M cells). The MASSDEN / COLMD / AOTK arrays are stored as float64
    in the mirror (~15 MB raw, ~3 MB DEFLATE-compressed) for a single
    time-slice; we cast to float32 on output so a single-variable single-
    time-step COG over full CONUS lands around 5 MB compressed.

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
    """Raise ``HRRRSmokeInputError`` if bbox is invalid or non-CONUS."""
    if len(bbox) != 4:
        raise HRRRSmokeInputError(
            f"bbox must be (west, south, east, north); got {bbox!r}"
        )
    west, south, east, north = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise HRRRSmokeInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= west <= 180.0 and -180.0 <= east <= 180.0):
        raise HRRRSmokeInputError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= south <= 90.0 and -90.0 <= north <= 90.0):
        raise HRRRSmokeInputError(f"bbox lat out of [-90,90]: {bbox!r}")
    if west >= east or south >= north:
        raise HRRRSmokeInputError(
            f"bbox is degenerate (west < east, south < north required): {bbox!r}"
        )
    # CONUS-only soft gate: refuse bboxes that lie entirely outside CONUS.
    if (
        east < _CONUS_LON_MIN
        or west > _CONUS_LON_MAX
        or north < _CONUS_LAT_MIN
        or south > _CONUS_LAT_MAX
    ):
        raise HRRRSmokeInputError(
            f"bbox={bbox} lies outside HRRR-Smoke CONUS coverage "
            f"(~{_CONUS_LON_MIN}..{_CONUS_LON_MAX} lon, "
            f"~{_CONUS_LAT_MIN}..{_CONUS_LAT_MAX} lat). HRRR-Smoke is CONUS-only; "
            f"supports_global_query=False."
        )


def _round_bbox_to_6dp(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Round bbox coords to 6dp (~0.1 m) for cache-key stability."""
    return tuple(round(v, 6) for v in bbox)  # type: ignore[return-value]


def _validate_variable(variable: str) -> None:
    """Raise ``HRRRSmokeInputError`` for unsupported variable names."""
    if not isinstance(variable, str):
        raise HRRRSmokeInputError(
            f"variable must be a str; got {type(variable).__name__}"
        )
    if variable not in _VARIABLE_SPEC:
        raise HRRRSmokeInputError(
            f"unsupported HRRR-Smoke variable {variable!r}; allowed: "
            f"{sorted(_VARIABLE_SPEC)}"
        )


def _validate_forecast_hour(forecast_hour: int, cycle_hour: int) -> None:
    """Raise ``HRRRSmokeInputError`` if forecast_hour exceeds cycle horizon."""
    if not isinstance(forecast_hour, int):
        raise HRRRSmokeInputError(
            f"forecast_hour must be int; got {type(forecast_hour).__name__}"
        )
    if forecast_hour < 0:
        raise HRRRSmokeInputError(
            f"forecast_hour must be >= 0; got {forecast_hour}"
        )
    max_h = (
        _MAX_FORECAST_HOUR_EXTENDED
        if cycle_hour in _EXTENDED_CYCLES
        else _MAX_FORECAST_HOUR_STANDARD
    )
    if forecast_hour > max_h:
        raise HRRRSmokeInputError(
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

    HRRR-Zarr's nested layout (verified live 2026-06-09 against MASSDEN/COLMD/AOTK):

        <cycle>.zarr/
            <level>/                       <- group
                <s3_var>/                  <- outer_group  (carries coords)
                    forecast_period
                    forecast_reference_time
                    projection_x_coordinate
                    projection_y_coordinate
                    time
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
    ``HRRRSmokeNotAvailableError`` if the backstop is exhausted.
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
                "HRRR-Smoke cycle resolved: %sz %s (walked back %d h from target)",
                candidate.strftime("%Y%m%d_%H"),
                level,
                back,
            )
            return candidate.date(), candidate.hour
    raise HRRRSmokeNotAvailableError(
        f"no HRRR-Smoke cycle published within {_CYCLE_BACKSTOP_HOURS} h backstop "
        f"of target {target_cycle.isoformat()}; S3 mirror may be lagging."
    )


# ---------------------------------------------------------------------------
# Zarr → GeoTIFF (LCC → EPSG:4326).
# ---------------------------------------------------------------------------


def _zarr_slice_to_geotiff_bytes(
    cycle_date: _dt.date,
    cycle_hour: int,
    variable: str,
    forecast_hour: int,
    bbox: tuple[float, float, float, float],
) -> bytes:
    """Open the HRRR-Smoke Zarr slice, reproject to EPSG:4326, clip to bbox, write COG bytes.

    Raises:
        ``HRRRSmokeUpstreamError``: zarr open / xarray decode failure.
        ``HRRRSmokeEmptyError``: bbox produced no finite pixels after clip.
    """
    try:
        import fsspec  # noqa: F401 — required for the get_mapper
        import numpy as np
        import rioxarray  # noqa: F401 — registers .rio accessor
        import xarray as xr
    except ImportError as exc:
        raise HRRRSmokeUpstreamError(
            f"required deps not available (fsspec / xarray / rioxarray / numpy): {exc}"
        ) from exc

    level, s3_var, native_units, _friendly = _VARIABLE_SPEC[variable]

    outer_path, inner_path = _build_zarr_paths(
        cycle_date, cycle_hour, level, s3_var
    )

    try:
        import fsspec

        from trid3nt_server.tools.fetchers._public_s3 import public_s3fs_kwargs
        outer_mapper = fsspec.get_mapper(outer_path, **public_s3fs_kwargs("us-west-1"))
        inner_mapper = fsspec.get_mapper(inner_path, **public_s3fs_kwargs("us-west-1"))
        ds_outer = xr.open_zarr(outer_mapper, consolidated=False)
        ds_inner = xr.open_zarr(inner_mapper, consolidated=False)
    except Exception as exc:  # noqa: BLE001
        raise HRRRSmokeUpstreamError(
            f"failed to open HRRR-Smoke zarr at {outer_path} / {inner_path}: {exc}"
        ) from exc

    try:
        ds = xr.merge([ds_outer, ds_inner], compat="override")
        if s3_var not in ds.data_vars:
            raise HRRRSmokeUpstreamError(
                f"variable {s3_var!r} not present in merged zarr; "
                f"data_vars={list(ds.data_vars)}"
            )

        # Pick the forecast lead time. For ``fcst`` zarrs, the time axis carries
        # 48 (or 18) hourly slices starting at cycle_hour+1. We expose
        # forecast_hour=0 as "earliest available slice"; forecast_hour=N as the
        # Nth lead. Some cycles (00/06/12/18z) carry 48 entries; standard cycles
        # carry 18. We clamp by index.
        time_len = int(ds.sizes.get("time", 0))
        if time_len == 0:
            raise HRRRSmokeUpstreamError(
                "HRRR-Smoke zarr 'time' dim is empty; cycle may be partially published"
            )

        # Map forecast_hour (1..18 or 0..48) to time index. For ``fcst`` zarrs
        # the time array is sorted ascending starting at cycle+1h; index 0 ==
        # +1 h forecast. We accept forecast_hour=0 by aliasing to index 0
        # (the +1 h forecast — closest available analog; no analysis-time slice
        # exists in the ``fcst`` zarr).
        idx = max(0, min(time_len - 1, forecast_hour - 1 if forecast_hour > 0 else 0))
        da = ds[s3_var].isel(time=idx)

        # Replace the documented fill value with NaN so reprojection + clip see
        # missing data correctly. The MASSDEN/COLMD/AOTK arrays use -9999.0 as
        # fill (per .zmetadata). xarray's open_zarr does not always honor
        # fill_value attrs for masking when consolidated=False, so we mask
        # explicitly.
        try:
            da = da.where(da != _FILL_VALUE)
        except Exception:  # noqa: BLE001 — defensive
            pass

        # Tag CRS + write spatial dim names so rioxarray can reproject.
        da = da.rename(
            {"projection_x_coordinate": "x", "projection_y_coordinate": "y"}
        )
        da.rio.write_crs(_HRRR_PROJ4, inplace=True)
        da.rio.set_spatial_dims(x_dim="x", y_dim="y", inplace=True)

        # Reproject to EPSG:4326 (clipping in source CRS is faster but would
        # require projecting the bbox corners through the LCC inverse;
        # reprojecting the field and then clipping in WGS84 is the simpler,
        # accuracy-safe path for small-ish CONUS bboxes).
        try:
            da_4326 = da.rio.reproject("EPSG:4326")
        except Exception as exc:  # noqa: BLE001
            raise HRRRSmokeUpstreamError(
                f"rioxarray reproject HRRR-Smoke LCC → EPSG:4326 failed: {exc}"
            ) from exc

        west, south, east, north = bbox
        try:
            da_clipped = da_4326.rio.clip_box(
                minx=west, miny=south, maxx=east, maxy=north, crs="EPSG:4326"
            )
        except Exception as exc:  # noqa: BLE001
            raise HRRRSmokeEmptyError(
                f"bbox={bbox} produced an empty HRRR-Smoke window after clip: {exc}"
            ) from exc

        if da_clipped.size == 0:
            raise HRRRSmokeEmptyError(
                f"bbox={bbox} produced an empty HRRR-Smoke window after clip"
            )

        arr = np.asarray(da_clipped.values, dtype=np.float32)
        if not np.isfinite(arr).any():
            raise HRRRSmokeEmptyError(
                f"bbox={bbox} produced no finite HRRR-Smoke pixels (all-NaN window)"
            )

        # Write COG.
        out_fd, out_path = tempfile.mkstemp(suffix=".tif", prefix="trid3nt_hrrr_smoke_")
        os.close(out_fd)
        try:
            da_out = da_clipped.astype("float32")
            da_out.attrs["units"] = native_units
            da_out.attrs["source"] = "HRRR-Smoke_hrrrzarr"
            da_out.attrs["variable"] = variable
            da_out.attrs["s3_var"] = s3_var
            da_out.attrs["cycle"] = f"{cycle_date.isoformat()}T{cycle_hour:02d}:00Z"
            da_out.attrs["forecast_hour"] = forecast_hour
            da_out.attrs["tool"] = "fetch_hrrr_smoke"
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
            "fetch_hrrr_smoke wrote %d-byte COG (variable=%s cycle=%s "
            "F%03d shape=%s min=%.3e max=%.3e mean=%.3e)",
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
    finally:
        try:
            ds_outer.close()
            ds_inner.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Fetch function (passed to read_through).
# ---------------------------------------------------------------------------


def _fetch_hrrr_smoke_bytes(
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
def fetch_hrrr_smoke(
    bbox: tuple[float, float, float, float],
    variable: str = "near_surface_smoke",
    forecast_hour: int = 1,
    cycle: str | None = None,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """NOAA HRRR-Smoke 3 km smoke / aerosol forecast — Tier-1 CONUS fetcher.

    What it does: returns a CRS-tagged GeoTIFF (EPSG:4326) of a single
    HRRR-Smoke forecast field at a single forecast lead time, reprojected
    from the native Lambert Conformal Conic grid and clipped to the
    requested CONUS bbox. Wraps the University of Utah CHPC HRRR-Zarr S3
    mirror (anonymous, Tier-1 free, no API key) that carries NCEP/EMC's
    operational HRRR-Smoke run — smoke emissions sourced from NESDIS HMS
    satellite fire detections, plume-rise via Freitas et al. (2006),
    advected on the 3 km HRRR dynamical core. Resolves the most-recently
    published HRRR cycle whose forecast at the requested lead time is on
    S3, walking up to 6 h backward if the in-progress cycle has not yet
    posted (mirror lag ~1.0–1.5 h).

    When to use:
    - User asks about "wildfire smoke" / "smoke plume" / "air quality from
      the fire" forecasts anywhere in CONUS ("how bad will the smoke be in
      Denver tomorrow?", "show the smoke plume from the wildfire in
      Oregon", "is California's smoke reaching Nevada by tonight?").
    - Driving an air-quality / public-health overlay or a fire-impact
      composite layered with ``fetch_nifc_fire_perimeters`` (active
      perimeters), ``fetch_firms_active_fire`` (satellite hot spots), or
      ``fetch_mtbs_burn_severity`` (post-fire severity).
    - "Forecast smoke + observed weather" comparisons against
      ``fetch_asos_metar`` (visibility, sky conditions) for plausibility.
    - Short-term (≤ 18 h on most cycles, ≤ 48 h on 00/06/12/18z cycles)
      US smoke-transport decision-support overlay.

    When NOT to use:
    - Standard weather forecasts (temperature, wind, precip) — use
      ``fetch_hrrr_forecast``. HRRR-Smoke shares the same dynamical core
      but this tool exposes only smoke / aerosol diagnostics.
    - Observed surface particulate (PM2.5 / PM10 / AOD) — HRRR-Smoke is
      model output. There is no HRRR-Smoke "observation"; use EPA AirNow
      (future tool) or NASA MODIS AOD (future) for measured aerosols.
    - Active fire detection — use ``fetch_firms_active_fire`` (NASA VIIRS/
      MODIS hot spots) or ``fetch_goes_satellite`` (GOES-East/West fire
      products). HRRR-Smoke ingests these as forcing; it does not produce
      them.
    - Fire perimeters — use ``fetch_nifc_fire_perimeters`` (active) or
      ``fetch_mtbs_burn_severity`` (historical).
    - Outside CONUS (Mexico interior, Caribbean except direct CONUS
      spillover, Alaska, Hawaii, the open Pacific or Atlantic) —
      HRRR-Smoke is CONUS-only; bbox outside coverage raises
      ``HRRRSmokeInputError``.
    - Long-range climatology / multi-day transport beyond 48 h — those
      leads are not in HRRR-Smoke.

    Parameters:
        bbox: ``(west, south, east, north)`` in EPSG:4326 (WGS84 decimal
            degrees). Must intersect CONUS coverage (~lon -134..-60,
            lat 21..53). Example: ``(-124.5, 41.0, -120.0, 46.3)`` for a
            northern California / southern Oregon wildfire footprint.
        variable: one of:
            - ``"near_surface_smoke"`` (kg m-3, 8 m AGL smoke mass density —
              the "what you'd breathe" layer; multiply by ~10⁹ for µg m-3)
            - ``"smoke_column_mass"`` (kg m-2, vertically-integrated smoke
              column — total atmospheric smoke load)
            - ``"aerosol_optical_depth"`` (dimensionless, 550 nm AOD —
              proxy for satellite-observable smoke opacity).
            Default ``"near_surface_smoke"``.
        forecast_hour: integer forecast lead time in hours (1 = +1 h from
            cycle start). Range 1–18 for standard cycles, 1–48 for the
            extended 00/06/12/18z cycles. ``0`` is accepted and aliased to
            the +1 h slice (no analysis-time slice exists in the ``fcst``
            zarr). Default ``1``.
        cycle: optional ISO-8601 cycle timestamp like
            ``"2026-06-09T00:00:00Z"`` to pin a specific cycle. Default
            ``None`` → use the most recent published cycle (walks backward
            up to 6 h from current UTC).

    Returns:
        A ``LayerURI`` pointing at a COG in the cache bucket
        ``s3://trid3nt-cache/cache/dynamic-1h/hrrr_smoke/<key>.tif``
        carrying the requested variable's forecast slice, EPSG:4326,
        float32, NaN nodata. ``layer_type="raster"``, ``role="primary"``,
        ``units`` per the variable (``"kg m-3"``, ``"kg m-2"``, or ``"1"``).
        Downstream consumers (``publish_layer``, ``compute_zonal_statistics``,
        ``clip_raster_to_polygon``) read the COG and treat it as a
        single-band scalar field.

    Cross-tool dependencies:
        - Consumes nothing (Tier-1 substrate fetcher; no upstream tool).
        - Feeds: ``publish_layer`` (visualization on the web map),
          ``compute_zonal_statistics`` (aggregate to admin boundaries or
          county / ZIP code area-weighted exposure),
          ``clip_raster_to_polygon`` (further sub-clip to fire footprint
          or admin polygon), ``clip_raster_to_bbox`` (bbox-only sub-clip).
        - Composes with: ``fetch_nifc_fire_perimeters`` (active wildfire
          polygons — overlay smoke plume on the burning area),
          ``fetch_firms_active_fire`` (VIIRS/MODIS hot spots — overlay
          ignition sources), ``fetch_mtbs_burn_severity`` (historical
          burn footprints for context), ``fetch_asos_metar`` (surface
          visibility for validation), ``fetch_gridmet`` (RH / wind speed
          context for fire-weather), ``fetch_administrative_boundaries``
          (clip to a county or state for air-quality reporting).

    Raises:
        ``HRRRSmokeInputError``: bad bbox / variable / forecast_hour
            (retryable=False).
        ``HRRRSmokeEmptyError``: bbox falls outside HRRR coverage or
            yields all-NaN after clip (retryable=False).
        ``HRRRSmokeNotAvailableError``: requested cycle is not yet on S3
            within the 6 h backstop (retryable=True).
        ``HRRRSmokeUpstreamError``: S3 / zarr / reprojection failure
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
        raise HRRRSmokeUpstreamError(
            f"fsspec not available: {exc}"
        ) from exc

    try:
        from trid3nt_server.tools.fetchers._public_s3 import public_s3fs_kwargs
        fs = fsspec.filesystem("s3", **public_s3fs_kwargs("us-west-1"))
    except Exception as exc:  # noqa: BLE001
        raise HRRRSmokeUpstreamError(
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
            raise HRRRSmokeInputError(
                f"cycle must be ISO-8601 UTC (e.g. '2026-06-09T00:00:00Z'); "
                f"got {cycle!r}: {exc}"
            ) from exc
        if target_cycle.tzinfo is None:
            target_cycle = target_cycle.replace(tzinfo=_dt.timezone.utc)

    # Validate forecast_hour against the resolved cycle hour.
    _validate_forecast_hour(forecast_hour, target_cycle.hour)

    level, s3_var, native_units, _friendly = _VARIABLE_SPEC[variable]
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
        fetch_fn=lambda: _fetch_hrrr_smoke_bytes(
            cycle_date=cycle_date,
            cycle_hour=cycle_hour,
            variable=variable,
            forecast_hour=forecast_hour,
            bbox=q_bbox,
        ),
    )
    assert result.uri is not None, (
        "fetch_hrrr_smoke is cacheable; uri must be set by read_through"
    )

    cycle_label = f"{cycle_date.isoformat()}T{cycle_hour:02d}:00Z"
    return LayerURI(
        layer_id=(
            f"hrrr-smoke-{variable.replace('_', '-')}-"
            f"{cycle_date.strftime('%Y%m%d')}-{cycle_hour:02d}z-"
            f"f{forecast_hour:03d}-"
            f"{q_bbox[0]:.4f}-{q_bbox[1]:.4f}"
        ),
        name=(
            f"HRRR-Smoke — {variable.replace('_', ' ').title()} "
            f"(cycle {cycle_label}, F{forecast_hour:03d})"
        ),
        layer_type="raster",
        uri=result.uri,
        style_preset=f"hrrr_smoke_{variable}",
        role="primary",
        units=native_units,
    )
