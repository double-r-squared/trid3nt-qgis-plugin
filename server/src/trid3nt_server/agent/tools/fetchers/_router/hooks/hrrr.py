"""HRRR-Zarr delegate hooks: the fsspec/xarray Zarr store owns the socket.

The NOAA HRRR / HRRR-Smoke forecast is published as a nested Zarr store on the
University of Utah CHPC S3 mirror (``hrrrzarr``). fsspec + xarray own the store
socket + the LCC coordinate arrays, so the router DELEGATES the read: a
``library_delegate`` raster spec whose ``hooks.delegate`` returns
``(array_2d_float32, affine_transform, crs)`` already in EPSG:4326 (the hook owns
the reproject + clip + the forecast's derived ``hypot(u,v)`` synthesis), and whose
``hooks.delegate_resolve`` walks the s3fs mirror backward for the newest published
cycle BEFORE ``read_through`` so the resolved cycle enters the cache key.

ONE shared module serves both ``fetch_hrrr_forecast`` and ``fetch_hrrr_smoke`` -- an
identical Zarr body; the per-source difference (the variable -> level/s3_var table,
the forecast-only derived ``10m_wind_speed``, the smoke-only ``-9999.0`` fill mask)
is declared in ``ingest.hrrr`` and read here. The HRRR-grid physical facts (the LCC
proj4, the CONUS envelope, the 18/48 h horizons, the 6 h cycle backstop) are the
same for both mirrors, so they stay module constants.
"""

from __future__ import annotations

import datetime as _dt
import logging
import math
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..errors import RouterUpstreamError, router_empty_error, router_input_error
from . import register_hook

logger = logging.getLogger(
    "trid3nt_server.agent.tools.fetchers._router.hooks.hrrr"
)

__all__ = ["resolve_cycle", "read_slice", "validate_inputs"]

# HRRR LCC projection (NCEP/EMC standard) + CONUS envelope + horizons -- the same
# physical grid for HRRR and HRRR-Smoke (twin ``_HRRR_PROJ4`` / ``_CONUS_*`` /
# horizon constants, verbatim). Kept module-level (not ingest) so both specs share
# one source of truth for the grid facts.
_HRRR_PROJ4 = (
    "+proj=lcc +lat_1=38.5 +lat_2=38.5 +lat_0=38.5 +lon_0=-97.5 "
    "+x_0=0 +y_0=0 +R=6371229 +units=m +no_defs"
)
_CONUS_LON_MIN = -134.0
_CONUS_LON_MAX = -60.0
_CONUS_LAT_MIN = 21.0
_CONUS_LAT_MAX = 53.0
_MAX_FORECAST_HOUR_STANDARD = 18
_MAX_FORECAST_HOUR_EXTENDED = 48
_EXTENDED_CYCLES = {0, 6, 12, 18}
_CYCLE_BACKSTOP_HOURS = 6
_BUCKET = "hrrrzarr"
_KIND_FCST = "fcst"
_REGION = "us-west-1"


def _hrrr_cfg(spec: SourceSpec) -> dict[str, Any]:
    return (spec.ingest or {}).get("hrrr") or {}


def _var_levels(spec: SourceSpec, variable: str) -> tuple[str, str]:
    """Return the ``(level_group, s3_var)`` for a plain (non-derived) variable."""
    variables = _hrrr_cfg(spec).get("variables") or {}
    entry = variables.get(variable)
    if not entry:
        raise router_input_error(
            spec.error_code_prefix,
            f"unsupported HRRR variable {variable!r}",
            spec.input_error_suffix,
        )
    return str(entry["level"]), str(entry["s3_var"])


def _probe_levels(spec: SourceSpec, variable: str) -> tuple[str, str]:
    """The ``(level, s3_var)`` used to PROBE the cycle for ``variable``.

    A derived variable (forecast ``10m_wind_speed``) has no single S3 array; its
    ``ingest.hrrr.derived`` entry names a ``probe`` component (publishing is atomic
    per cycle, so probing one component proves the cycle is posted).
    """
    derived = (_hrrr_cfg(spec).get("derived") or {}).get(variable)
    if derived:
        return _var_levels(spec, str(derived["probe"]))
    return _var_levels(spec, variable)


def _target_cycle(spec: SourceSpec, params: dict[str, Any]) -> _dt.datetime:
    """Resolve the user's ``cycle`` param (ISO-8601 UTC) or now() floored to the hour."""
    cycle = params.get("cycle")
    if not cycle:
        return _dt.datetime.now(_dt.timezone.utc).replace(
            minute=0, second=0, microsecond=0
        )
    try:
        target = _dt.datetime.fromisoformat(str(cycle).replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise router_input_error(
            spec.error_code_prefix,
            f"cycle must be ISO-8601 UTC (e.g. '2026-06-09T00:00:00Z'); got {cycle!r}: {exc}",
            spec.input_error_suffix,
        )
    if target.tzinfo is None:
        target = target.replace(tzinfo=_dt.timezone.utc)
    return target


def _cycle_key(cycle_date: _dt.date, cycle_hour: int) -> str:
    return f"{cycle_date.strftime('%Y%m%d')}_{cycle_hour:02d}z_{_KIND_FCST}.zarr"


def _zarr_paths(cycle_date: _dt.date, cycle_hour: int, level: str, s3_var: str) -> tuple[str, str]:
    """The ``(outer_group, inner_group)`` S3 zarr paths for a (cycle, variable)."""
    date_str = cycle_date.strftime("%Y%m%d")
    base = f"{_BUCKET}/sfc/{date_str}/{_cycle_key(cycle_date, cycle_hour)}/{level}/{s3_var}"
    return f"s3://{base}", f"s3://{base}/{level}"


# --------------------------------------------------------------------------- #
# delegate_validate: CONUS gate + forecast_hour horizon (pre-cache, offline)
# --------------------------------------------------------------------------- #


@register_hook("hrrr.validate")
def validate_inputs(spec: SourceSpec, params: dict[str, Any]) -> None:
    """Twin ``_validate_bbox`` CONUS gate + ``_validate_forecast_hour`` horizon.

    The router already ran the shared bbox finite/range/degenerate + the variable
    enum + the forecast_hour ``min: 0`` gate; this adds the two twin gates the
    declarative surface cannot express: the bbox-entirely-outside-CONUS refusal and
    the forecast_hour-vs-cycle-horizon ceiling (cross-param with the resolved-or-now
    cycle hour). Both are typed INPUT errors (byte-identical to the twin).
    """
    sc = spec.error_code_prefix
    sfx = spec.input_error_suffix
    bbox = params.get("bbox")
    if not bbox or len(bbox) != 4:
        raise router_input_error(sc, f"bbox must be (west, south, east, north); got {bbox!r}", sfx)
    west, south, east, north = (float(v) for v in bbox)
    if (east < _CONUS_LON_MIN or west > _CONUS_LON_MAX
            or north < _CONUS_LAT_MIN or south > _CONUS_LAT_MAX):
        raise router_input_error(
            sc,
            f"bbox={tuple(bbox)} lies outside HRRR CONUS coverage "
            f"(~{_CONUS_LON_MIN}..{_CONUS_LON_MAX} lon, {_CONUS_LAT_MIN}..{_CONUS_LAT_MAX} lat). "
            f"HRRR is CONUS-only; supports_global_query=False.",
            sfx,
        )
    forecast_hour = int(params.get("forecast_hour", 1))
    cycle_hour = _target_cycle(spec, params).hour
    max_h = _MAX_FORECAST_HOUR_EXTENDED if cycle_hour in _EXTENDED_CYCLES else _MAX_FORECAST_HOUR_STANDARD
    if forecast_hour > max_h:
        raise router_input_error(
            sc,
            f"forecast_hour={forecast_hour} exceeds the {cycle_hour:02d}z cycle horizon "
            f"(max {max_h} h). 00/06/12/18z cycles publish 48 h; all others {_MAX_FORECAST_HOUR_STANDARD} h.",
            sfx,
        )


# --------------------------------------------------------------------------- #
# delegate_resolve: s3fs backward cycle walk (socketed, pre-cache-key)
# --------------------------------------------------------------------------- #


@register_hook("hrrr.resolve_cycle")
def resolve_cycle(spec: SourceSpec, params: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
    """Walk the s3fs mirror backward for the newest published cycle (twin ``_resolve_cycle``).

    Returns ``{"cycle_date": <iso>, "cycle_hour": <int>}`` merged into params BEFORE
    ``read_through`` so the resolved cycle enters the cache key. Exhausting the 6 h
    backstop raises the twin's retryable NOT_AVAILABLE.
    """
    sc = spec.error_code_prefix
    variable = params["variable"]
    forecast_hour = int(params.get("forecast_hour", 1))
    level, s3_var = _probe_levels(spec, variable)
    target = _target_cycle(spec, params)

    import fsspec

    from trid3nt_server.agent.tools.fetchers._public_s3 import public_s3fs_kwargs

    fs = fsspec.filesystem("s3", **public_s3fs_kwargs(_REGION))

    def _exists(path_no_proto: str) -> bool:
        try:
            return bool(fs.exists(path_no_proto))
        except Exception:  # noqa: BLE001 -- any S3 error == "doesn't exist"
            return False

    for back in range(0, _CYCLE_BACKSTOP_HOURS + 1):
        candidate = target - _dt.timedelta(hours=back)
        if candidate.hour not in _EXTENDED_CYCLES and forecast_hour > _MAX_FORECAST_HOUR_STANDARD:
            continue
        probe = (
            f"{_BUCKET}/sfc/{candidate.strftime('%Y%m%d')}/"
            f"{_cycle_key(candidate.date(), candidate.hour)}/{level}/{s3_var}"
        )
        if _exists(probe):
            logger.info(
                "HRRR cycle resolved: %sz %s (walked back %d h from target)",
                candidate.strftime("%Y%m%d_%H"), level, back,
            )
            return {"cycle_date": candidate.date().isoformat(), "cycle_hour": candidate.hour}

    err = RouterUpstreamError(
        f"no HRRR cycle published within {_CYCLE_BACKSTOP_HOURS} h backstop of target "
        f"{target.isoformat()}; S3 mirror may be lagging."
    )
    err.error_code = f"{sc}_NOT_AVAILABLE"
    err.retryable = True
    raise err


# --------------------------------------------------------------------------- #
# delegate: open the Zarr slice(s) -> EPSG:4326 array (the hook owns the socket)
# --------------------------------------------------------------------------- #


def _open_component_4326(
    spec: SourceSpec,
    cycle_date: _dt.date,
    cycle_hour: int,
    variable: str,
    forecast_hour: int,
    bbox: tuple[float, float, float, float],
) -> Any:
    """Open ONE plain HRRR-Zarr component, reproject to EPSG:4326, clip to bbox.

    Returns the clipped, materialized ``xarray.DataArray`` (float32-valued,
    EPSG:4326). Raises the twin's typed UPSTREAM (open/decode/reproject) / EMPTY
    (empty window after clip).
    """
    import fsspec
    import rioxarray  # noqa: F401 -- registers the .rio accessor
    import xarray as xr

    sc = spec.error_code_prefix
    fill_value = _hrrr_cfg(spec).get("fill_value")
    level, s3_var = _var_levels(spec, variable)
    outer_path, inner_path = _zarr_paths(cycle_date, cycle_hour, level, s3_var)

    from trid3nt_server.agent.tools.fetchers._public_s3 import public_s3fs_kwargs

    try:
        outer_mapper = fsspec.get_mapper(outer_path, **public_s3fs_kwargs(_REGION))
        inner_mapper = fsspec.get_mapper(inner_path, **public_s3fs_kwargs(_REGION))
        ds_outer = xr.open_zarr(outer_mapper, consolidated=False)
        ds_inner = xr.open_zarr(inner_mapper, consolidated=False)
    except Exception as exc:  # noqa: BLE001
        raise RouterUpstreamError(f"failed to open HRRR zarr at {outer_path} / {inner_path}: {exc}")

    try:
        ds = xr.merge([ds_outer, ds_inner], compat="override")
        if s3_var not in ds.data_vars:
            raise RouterUpstreamError(
                f"variable {s3_var!r} not present in merged zarr; data_vars={list(ds.data_vars)}"
            )
        time_len = int(ds.sizes.get("time", 0))
        if time_len == 0:
            raise RouterUpstreamError("HRRR zarr 'time' dim is empty; cycle may be partially published")
        idx = max(0, min(time_len - 1, forecast_hour - 1 if forecast_hour > 0 else 0))
        da = ds[s3_var].isel(time=idx)

        if fill_value is not None:
            try:
                da = da.where(da != float(fill_value))
            except Exception:  # noqa: BLE001 -- defensive fill mask
                pass

        da = da.rename({"projection_x_coordinate": "x", "projection_y_coordinate": "y"})
        da.rio.write_crs(_HRRR_PROJ4, inplace=True)
        da.rio.set_spatial_dims(x_dim="x", y_dim="y", inplace=True)

        try:
            da_4326 = da.rio.reproject("EPSG:4326")
        except Exception as exc:  # noqa: BLE001
            raise RouterUpstreamError(f"rioxarray reproject HRRR LCC -> EPSG:4326 failed: {exc}")

        west, south, east, north = bbox
        try:
            da_clipped = da_4326.rio.clip_box(minx=west, miny=south, maxx=east, maxy=north, crs="EPSG:4326")
        except Exception as exc:  # noqa: BLE001
            raise router_empty_error(
                sc, f"bbox={tuple(bbox)} produced an empty HRRR window after clip: {exc}",
                spec.empty_error_suffix,
            )
        if da_clipped.size == 0:
            raise router_empty_error(
                sc, f"bbox={tuple(bbox)} produced an empty HRRR window after clip",
                spec.empty_error_suffix,
            )
        return da_clipped.compute()
    finally:
        try:
            ds_outer.close()
            ds_inner.close()
        except Exception:  # noqa: BLE001
            pass


@register_hook("hrrr.read")
def read_slice(spec: SourceSpec, params: dict[str, Any], *, timeout_s: float) -> tuple[Any, Any, Any]:
    """Open the HRRR-Zarr slice(s) -> ``(array_2d_float32, affine, EPSG:4326)``.

    For a plain single-array variable: one component, reprojected + clipped. For the
    forecast's derived ``10m_wind_speed``: both UGRD/VGRD components on the same
    EPSG:4326 grid combined via ``hypot(u, v)`` (NaN preserved). The shared COG writer
    serializes the returned array (float32, NaN nodata, DEFLATE) -- byte-parity with
    the twin's ``to_raster`` write.
    """
    import numpy as np

    sc = spec.error_code_prefix
    variable = params["variable"]
    forecast_hour = int(params.get("forecast_hour", 1))
    cycle_date = _dt.date.fromisoformat(params["cycle_date"])
    cycle_hour = int(params["cycle_hour"])
    bbox = tuple(float(v) for v in params["bbox"])

    derived = (_hrrr_cfg(spec).get("derived") or {}).get(variable)
    if derived and str(derived.get("op")) == "hypot":
        import xarray as xr

        u_var, v_var = (str(c) for c in derived["components"])
        da_u = _open_component_4326(spec, cycle_date, cycle_hour, u_var, forecast_hour, bbox)
        da_v = _open_component_4326(spec, cycle_date, cycle_hour, v_var, forecast_hour, bbox)
        speed = xr.apply_ufunc(np.hypot, da_u, da_v, keep_attrs=False).astype("float32")
        speed.rio.write_crs("EPSG:4326", inplace=True)
        try:
            speed.rio.set_spatial_dims(x_dim="x", y_dim="y", inplace=True)
        except Exception:  # noqa: BLE001 -- dims already x/y in most paths
            pass
        da_out = speed
    else:
        da_out = _open_component_4326(spec, cycle_date, cycle_hour, variable, forecast_hour, bbox)

    arr = np.asarray(da_out.values, dtype=np.float32)
    if not np.isfinite(arr).any():
        raise router_empty_error(
            sc, f"bbox={tuple(bbox)} produced no finite HRRR pixels (all-NaN window)",
            spec.empty_error_suffix,
        )
    return arr, da_out.rio.transform(), da_out.rio.crs
