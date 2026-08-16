"""Copernicus CDS library-delegate hooks: ERA5 + GTSM.

The CDS/``cdsapi`` client owns the request-poll-download socket, so both CDS
sources fold onto the ``library_delegate`` executor: the router keeps
params / gates / stamps / cache / typed-errors, and these hooks own the ONE
sanctioned impurity -- the ``cdsapi.Client.retrieve`` call under a declared
wall-clock timeout (``ingest.delegate.timeout_s``). Two sources share this module:

- ``era5.read``  -> ``(array, transform, crs)`` for the raster COG writer
  (single CDS-native variable, OR the derived ``10m_wind_speed`` = two retrieves
  combined by ``hypot(u, v)``).
- ``gtsm.read``  -> ``list[GeoJSON feature]`` for the vector FGB writer
  (one Point per in-bbox gauge carrying the inline ``time_series_csv``).

Each source also declares a pure ``delegate_validate`` hook running pre-cache /
pre-network (bbox + variable/output + date-range gates, byte-identical to the
twins' ``_validate_*`` helpers).

KEY RESOLUTION (per-source): ``api_key`` kwarg -> str ``secret_ref`` -> the
``TRID3NT_COPERNICUS_CDS_API_KEY`` env var -> ``None`` (cdsapi falls back to
``~/.cdsapirc``). ``None`` is NOT an error: the cdsapi Client constructor raises
its own "Missing/incomplete configuration file" when no credential exists, and
the classifier below maps that to the source's ``*_MISSING_KEY`` (the credential
card). Live-positive requires a resolvable key; the offline surface is
missing-key + input-validation parity (no key is ever registered here).
"""

from __future__ import annotations

import csv
import datetime as _dt
import io
import logging
import math
import os
import tempfile
import zipfile
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..errors import (
    RouterError,
    router_empty_error,
    router_input_error,
    router_upstream_error,
)
from . import register_hook

logger = logging.getLogger(
    "trid3nt_server.data.fetchers._router.hooks.cds"
)

# --------------------------------------------------------------------------- #
# Shared CDS constants.
# --------------------------------------------------------------------------- #

_DEFAULT_CDS_URL = "https://cds.climate.copernicus.eu/api"
_KEY_ENV = "TRID3NT_COPERNICUS_CDS_API_KEY"

#: The missing-``~/.cdsapirc`` / no-config phrase family (ERA5 twin's list). The
#: cdsapi Client constructor raises "Missing/incomplete configuration file:
#: <path>/.cdsapirc" when no credential is discoverable; these catch that (+ the
#: close variants) WITHOUT over-matching a transient/queue/network upstream error.
_MISSING_KEY_CDS_PHRASES: tuple[str, ...] = (
    ".cdsapirc",
    "missing/incomplete configuration",
    "missing or incomplete configuration",
    "incomplete configuration file",
    "no api key configured",
    "no api key found",
    "credentials not configured",
    "no credentials found",
)

# --------------------------------------------------------------------------- #
# ERA5 variable tables (single-level hourly reanalysis).
# --------------------------------------------------------------------------- #

_ERA5_DATASET = "reanalysis-era5-single-levels"
_ERA5_CDS_VARIABLES: frozenset[str] = frozenset(
    {
        "10m_u_component_of_wind",
        "10m_v_component_of_wind",
        "2m_temperature",
        "total_precipitation",
        "runoff",
        "significant_height_of_combined_wind_waves_and_swell",
    }
)
_ERA5_DERIVED_WIND_SPEED = "10m_wind_speed"
_ERA5_WIND_SPEED_COMPONENTS = ("10m_u_component_of_wind", "10m_v_component_of_wind")
_ERA5_ALLOWED_VARIABLES: frozenset[str] = _ERA5_CDS_VARIABLES | {_ERA5_DERIVED_WIND_SPEED}
_ERA5_MAX_DATE_RANGE_DAYS = 366

# --------------------------------------------------------------------------- #
# GTSM output tables (Global Tide and Surge Model v3.0).
# --------------------------------------------------------------------------- #

_GTSM_DATASET = "sis-water-level-change-timeseries-cmip6"
_GTSM_ALLOWED_OUTPUTS: frozenset[str] = frozenset({"water_level", "surge_only"})
_GTSM_OUTPUT_TO_CDS_VARIABLE: dict[str, str] = {
    "water_level": "total_water_level",
    "surge_only": "storm_surge_residual",
}
_GTSM_MAX_DATE_RANGE_DAYS = 366


# --------------------------------------------------------------------------- #
# Shared helpers: key resolution + the timeout-guarded retrieve + classify.
# --------------------------------------------------------------------------- #


def _resolve_key(params: dict[str, Any]) -> str | None:
    """Resolve the CDS key: api_key kwarg -> str secret_ref -> env var -> None.

    Returns ``None`` when every path misses (NOT an error): cdsapi falls back to
    ``~/.cdsapirc`` and, absent that, raises the missing-config error the retrieve
    classifier maps to the source's ``*_MISSING_KEY``. A str ``secret_ref`` is a
    ref/shortcut passed verbatim (the ebird keyed precedent).
    """
    api_key = params.get("api_key")
    if api_key:
        return str(api_key)
    secret_ref = params.get("secret_ref")
    if isinstance(secret_ref, str) and secret_ref:
        return secret_ref
    env_key = os.environ.get(_KEY_ENV)
    if env_key:
        return env_key
    return None


def _cds_retrieve_with_timeout(
    spec: SourceSpec,
    *,
    dataset: str,
    request: dict[str, Any],
    out_path: str,
    api_key: str | None,
    timeout_s: float,
    missing_phrases: tuple[str, ...],
) -> None:
    """Run ``cdsapi.Client.retrieve`` under a wall-clock watchdog; classify failures.

    cdsapi has no native timeout, so the retrieve runs in a daemon worker thread
    joined with a deadline (the twins' exact watchdog). On the failure path the
    caught exception message is classified in priority order: MISSING-KEY (no
    credential configured) -> AUTH (a key present but rejected) -> generic
    retryable UPSTREAM -- via the shared ``router_*_error`` factories so the A.6
    code is the twin's ``<PREFIX>_MISSING_KEY`` / ``_AUTH_ERROR`` / ``_UPSTREAM_ERROR``.
    """
    import threading

    sc = spec.error_code_prefix
    try:
        import cdsapi  # type: ignore[import-not-found]
    except ImportError as exc:
        raise router_upstream_error(sc, f"cdsapi package not available: {exc}")

    err_box: dict[str, BaseException] = {}

    def _do_retrieve() -> None:
        try:
            client_kwargs: dict[str, Any] = {"quiet": True}
            if _DEFAULT_CDS_URL:
                client_kwargs["url"] = _DEFAULT_CDS_URL
            if api_key:
                client_kwargs["key"] = api_key
            client = cdsapi.Client(**client_kwargs)
            client.retrieve(dataset, request, out_path)
        except BaseException as exc:  # noqa: BLE001 -- ferried to the caller thread
            err_box["err"] = exc

    t = threading.Thread(target=_do_retrieve, daemon=True)
    t.start()
    t.join(timeout=timeout_s)
    if t.is_alive():
        raise router_upstream_error(
            sc,
            f"CDS retrieve exceeded {timeout_s}s wall-clock budget; "
            f"the CDS job may still be queued server-side.",
        )
    if "err" not in err_box:
        return
    exc = err_box["err"]
    msg = str(exc)
    low = msg.lower()
    if missing_phrases and any(phrase in low for phrase in missing_phrases):
        raise router_input_error(sc, f"No Copernicus CDS API key is configured (cdsapi: {msg[:200]})", "MISSING_KEY")
    if any(tok in low for tok in ("401", "403", "authentication", "unauthorized")):
        raise router_input_error(sc, f"CDS API rejected the key: {msg[:200]}", "AUTH_ERROR")
    if "no api key" in low or ("missing" in low and "key" in low):
        raise router_input_error(sc, f"CDS API key not available: {msg[:200]}", "MISSING_KEY")
    raise router_upstream_error(sc, f"CDS retrieve failed: {msg[:200]}")


def _validate_bbox(sc: str, bbox: Any, suffix: str) -> tuple[float, float, float, float]:
    """Shared CDS bbox gate (both twins' ``_validate_bbox``, byte-identical)."""
    if not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
        raise router_input_error(sc, f"bbox must be (west, south, east, north); got {bbox!r}", suffix)
    try:
        west, south, east, north = (float(v) for v in bbox)
    except (TypeError, ValueError):
        raise router_input_error(sc, f"bbox must be 4 floats; got {bbox!r}", suffix)
    if not all(math.isfinite(v) for v in (west, south, east, north)):
        raise router_input_error(sc, f"bbox contains non-finite values: {bbox!r}", suffix)
    if not (-180.0 <= west <= 180.0 and -180.0 <= east <= 180.0):
        raise router_input_error(sc, f"bbox lon values out of [-180, 180]: {bbox!r}", suffix)
    if not (-90.0 <= south <= 90.0 and -90.0 <= north <= 90.0):
        raise router_input_error(sc, f"bbox lat values out of [-90, 90]: {bbox!r}", suffix)
    if west >= east or south >= north:
        raise router_input_error(sc, f"bbox is degenerate (min must be < max on both axes): {bbox!r}", suffix)
    return (west, south, east, north)


def _parse_iso(sc: str, s: Any, field: str, suffix: str) -> _dt.date:
    if not isinstance(s, str):
        raise router_input_error(sc, f"{field} must be ISO-8601 YYYY-MM-DD; got {s!r}", suffix)
    try:
        return _dt.date.fromisoformat(s)
    except ValueError as exc:
        raise router_input_error(sc, f"{field}={s!r} is not a valid ISO date (YYYY-MM-DD): {exc}", suffix)


# --------------------------------------------------------------------------- #
# ERA5.
# --------------------------------------------------------------------------- #


@register_hook("era5.validate")
def era5_validate(spec: SourceSpec, params: dict[str, Any]) -> None:
    """Pre-cache ERA5 input gate (bbox + variable + date-range), the twin's helpers."""
    sc = spec.error_code_prefix
    sfx = spec.input_error_suffix
    _validate_bbox(sc, params.get("bbox"), sfx)
    variable = params.get("variable")
    if not isinstance(variable, str) or variable not in _ERA5_ALLOWED_VARIABLES:
        raise router_input_error(
            sc, f"unsupported ERA5 variable {variable!r}; allowed: {sorted(_ERA5_ALLOWED_VARIABLES)}", sfx)
    d0 = _parse_iso(sc, params.get("start_date"), "start_date", sfx)
    d1 = _parse_iso(sc, params.get("end_date"), "end_date", sfx)
    if d0 > d1:
        raise router_input_error(sc, f"start_date must be <= end_date; got start={d0}, end={d1}", sfx)
    if d0.year < 1940 or d1.year > _dt.date.today().year + 1:
        raise router_input_error(sc, f"date range [{d0}, {d1}] outside ERA5 coverage (1940 -> present)", sfx)
    n_days = (d1 - d0).days + 1
    if n_days > _ERA5_MAX_DATE_RANGE_DAYS:
        raise router_input_error(
            sc, f"date range {n_days} days exceeds hard cap {_ERA5_MAX_DATE_RANGE_DAYS}; call in chunks and aggregate", sfx)


def _era5_build_request(variable: str, bbox: tuple[float, float, float, float], d0: _dt.date, d1: _dt.date) -> dict[str, Any]:
    west, south, east, north = bbox
    years: set[str] = set()
    months: set[str] = set()
    days: set[str] = set()
    cur = d0
    one = _dt.timedelta(days=1)
    while cur <= d1:
        years.add(f"{cur.year:04d}")
        months.add(f"{cur.month:02d}")
        days.add(f"{cur.day:02d}")
        cur += one
    return {
        "product_type": "reanalysis",
        "variable": variable,
        "year": sorted(years),
        "month": sorted(months),
        "day": sorted(days),
        "time": [f"{h:02d}:00" for h in range(24)],
        "area": [north, west, south, east],  # N, W, S, E -- CDS convention
        "format": "netcdf",
    }


def _era5_netcdf_to_da(spec: SourceSpec, nc_path: str, cds_variable: str, bbox: tuple[float, float, float, float]) -> Any:
    """CDS NetCDF -> a single 2D lat-ascending DataArray clipped to bbox (twin parity)."""
    import numpy as np
    import rioxarray  # noqa: F401 -- registers the .rio accessor
    import xarray as xr

    sc = spec.error_code_prefix
    try:
        ds = xr.open_dataset(nc_path, engine="netcdf4", chunks=None)
    except Exception as exc:  # noqa: BLE001
        try:
            ds = xr.open_dataset(nc_path, chunks=None)
        except Exception as exc2:  # noqa: BLE001
            raise router_upstream_error(sc, f"xarray could not open CDS NetCDF {nc_path}: {exc2} (netcdf4-engine error: {exc})")
    try:
        data_vars = [v for v in ds.data_vars if v not in ds.coords]
        if not data_vars:
            raise router_upstream_error(sc, f"CDS NetCDF carried no data variables; got {list(ds.variables)}")
        chosen = data_vars[0]
        target_token = cds_variable.replace("_", " ").lower()
        for v in data_vars:
            if target_token in ds[v].attrs.get("long_name", "").lower():
                chosen = v
                break
        da = ds[chosen]
        keep_dims = {"latitude", "longitude", "lat", "lon", "y", "x"}
        reduce_dims = [d for d in da.dims if d not in keep_dims]
        if reduce_dims:
            da = da.mean(dim=reduce_dims, skipna=True, keep_attrs=True)
        rename_map: dict[str, str] = {}
        if "lat" in da.dims and "latitude" not in da.dims:
            rename_map["lat"] = "latitude"
        if "lon" in da.dims and "longitude" not in da.dims:
            rename_map["lon"] = "longitude"
        if rename_map:
            da = da.rename(rename_map)
        da = da.rio.write_crs("EPSG:4326")
        if "latitude" in da.dims and len(da["latitude"]) > 1:
            lat_vals = da["latitude"].values
            if lat_vals[0] > lat_vals[-1]:
                da = da.sortby("latitude")
        if "longitude" in da.dims:
            lon_vals = da["longitude"].values
            if lon_vals.max() > 180.0:
                da = da.assign_coords(longitude=(((da["longitude"] + 180) % 360) - 180))
                da = da.sortby("longitude")
        west, south, east, north = bbox
        try:
            da = da.rio.clip_box(minx=west, miny=south, maxx=east, maxy=north, crs="EPSG:4326")
        except Exception as exc:  # noqa: BLE001
            raise router_upstream_error(sc, f"rioxarray clip_box to bbox={bbox} failed: {exc}")
        if da.size == 0:
            raise router_empty_error(sc, f"bbox={bbox} produced an empty ERA5 window after clip", spec.empty_error_suffix)
        arr = np.asarray(da.values, dtype=np.float32)
        if not np.isfinite(arr).any():
            raise router_empty_error(sc, f"bbox={bbox} produced no finite ERA5 pixels (all-NaN window)", spec.empty_error_suffix)
        return da.compute()
    finally:
        try:
            ds.close()
        except Exception:  # noqa: BLE001
            pass


def _da_to_array_transform(da: Any) -> tuple[Any, Any, Any]:
    """A lat-ascending DataArray -> north-up ``(array, affine, crs)`` for the COG writer."""
    import numpy as np
    import rasterio.transform as rtransform

    arr = np.asarray(da.values, dtype="float32")
    lat = np.asarray(da["latitude"].values, dtype="float64")
    lon = np.asarray(da["longitude"].values, dtype="float64")
    # North-up: row 0 must be the northernmost lat. _netcdf_to_da sorts lat
    # ascending, so flip to make the array north-up for the negative-y transform.
    if lat.size >= 2 and lat[0] < lat[-1]:
        arr = arr[::-1, :]
    transform = rtransform.from_bounds(
        float(lon.min()), float(lat.min()), float(lon.max()), float(lat.max()),
        arr.shape[1], arr.shape[0],
    )
    return arr, transform, "EPSG:4326"


@register_hook("era5.read")
def era5_read(spec: SourceSpec, params: dict[str, Any], *, timeout_s: float) -> tuple[Any, Any, Any]:
    """CDS retrieve(s) -> NetCDF(s) -> north-up ``(array, transform, crs)``.

    A CDS-native variable = one retrieve -> one array. The derived
    ``10m_wind_speed`` = two retrieves (U + V 10 m components) combined into the
    elementwise magnitude ``hypot(u, v)`` on the shared ERA5 0.25 deg grid.
    """
    import numpy as np
    import xarray as xr

    variable = str(params["variable"])
    bbox = tuple(float(v) for v in params["bbox"])  # type: ignore[assignment]
    d0 = _dt.date.fromisoformat(str(params["start_date"]))
    d1 = _dt.date.fromisoformat(str(params["end_date"]))
    api_key = _resolve_key(params)

    def _retrieve(cds_var: str) -> str:
        fd, path = tempfile.mkstemp(suffix=".nc", prefix="trid3nt_era5_cds_")
        os.close(fd)
        _cds_retrieve_with_timeout(
            spec, dataset=_ERA5_DATASET, request=_era5_build_request(cds_var, bbox, d0, d1),
            out_path=path, api_key=api_key, timeout_s=timeout_s, missing_phrases=_MISSING_KEY_CDS_PHRASES,
        )
        return path

    if variable == _ERA5_DERIVED_WIND_SPEED:
        u_var, v_var = _ERA5_WIND_SPEED_COMPONENTS
        u_path = v_path = None
        try:
            u_path = _retrieve(u_var)
            v_path = _retrieve(v_var)
            da_u = _era5_netcdf_to_da(spec, u_path, u_var, bbox)
            da_v = _era5_netcdf_to_da(spec, v_path, v_var, bbox)
            speed = xr.apply_ufunc(np.hypot, da_u, da_v, keep_attrs=False).astype("float32")
            speed = speed.rio.write_crs("EPSG:4326")
            arr = np.asarray(speed.values, dtype=np.float32)
            if not np.isfinite(arr).any():
                raise router_empty_error(spec.error_code_prefix, f"bbox={bbox} produced no finite ERA5 wind-speed pixels (all-NaN)", spec.empty_error_suffix)
            return _da_to_array_transform(speed)
        finally:
            for p in (u_path, v_path):
                if p:
                    try:
                        os.unlink(p)
                    except OSError:
                        pass

    nc_path = None
    try:
        nc_path = _retrieve(variable)
        da = _era5_netcdf_to_da(spec, nc_path, variable, bbox)
        return _da_to_array_transform(da)
    finally:
        if nc_path:
            try:
                os.unlink(nc_path)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# GTSM.
# --------------------------------------------------------------------------- #


@register_hook("gtsm.validate")
def gtsm_validate(spec: SourceSpec, params: dict[str, Any]) -> None:
    """Pre-cache GTSM input gate (bbox + output + date-range), the twin's helpers."""
    sc = spec.error_code_prefix
    sfx = spec.input_error_suffix
    _validate_bbox(sc, params.get("bbox"), sfx)
    output = params.get("output", "water_level")
    if not isinstance(output, str) or output not in _GTSM_ALLOWED_OUTPUTS:
        raise router_input_error(sc, f"unsupported GTSM output {output!r}; allowed: {sorted(_GTSM_ALLOWED_OUTPUTS)}", sfx)
    d0 = _parse_iso(sc, params.get("start_date"), "start_date", sfx)
    d1 = _parse_iso(sc, params.get("end_date"), "end_date", sfx)
    if d0 > d1:
        raise router_input_error(sc, f"start_date must be <= end_date; got start={d0}, end={d1}", sfx)
    if d0.year < 1950 or d1.year > _dt.date.today().year + 1:
        raise router_input_error(sc, f"date range [{d0}, {d1}] outside GTSM coverage (1950 -> present)", sfx)
    n_days = (d1 - d0).days + 1
    if n_days > _GTSM_MAX_DATE_RANGE_DAYS:
        raise router_input_error(
            sc, f"date range {n_days} days exceeds hard cap {_GTSM_MAX_DATE_RANGE_DAYS}; call in chunks and aggregate", sfx)


def _gtsm_build_request(output: str, d0: _dt.date, d1: _dt.date) -> dict[str, Any]:
    years: set[str] = set()
    months: set[str] = set()
    cur = d0
    one = _dt.timedelta(days=1)
    while cur <= d1:
        years.add(f"{cur.year:04d}")
        months.add(f"{cur.month:02d}")
        cur += one
    return {
        "experiment": ["reanalysis"],
        "variable": [_GTSM_OUTPUT_TO_CDS_VARIABLE[output]],
        "year": sorted(years),
        "month": sorted(months),
        "temporal_aggregation": ["hourly"],
        "format": "zip",
    }


def _gtsm_extract_netcdfs(spec: SourceSpec, zip_path: str) -> list[str]:
    sc = spec.error_code_prefix
    tmpdir = tempfile.mkdtemp(prefix="trid3nt_gtsm_zip_")
    extracted: list[str] = []
    names: list[str] = []
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            for member in names:
                if not member.lower().endswith(".nc"):
                    continue
                safe_name = os.path.basename(member)
                if not safe_name:
                    continue
                target = os.path.join(tmpdir, safe_name)
                with zf.open(member) as src, open(target, "wb") as dst:
                    dst.write(src.read())
                extracted.append(target)
    except zipfile.BadZipFile as exc:
        raise router_upstream_error(sc, f"CDS returned a malformed ZIP archive: {exc}")
    if not extracted:
        raise router_upstream_error(sc, f"CDS ZIP archive carried no .nc files (members={names})")
    return extracted


def _gtsm_pick_coord(ds: Any, candidates: tuple[str, ...]) -> str | None:
    for name in candidates:
        if name in ds.variables or name in ds.coords:
            return name
    return None


def _gtsm_pick_gauge_id(ds: Any, station_dim: str, index: int) -> str:
    for name in ("station_id", "stations", "id", "station_name", "name"):
        if name in ds.variables:
            try:
                raw = ds[name].isel({station_dim: index}).values
                if hasattr(raw, "item"):
                    raw = raw.item()
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                s = str(raw).strip()
                if s:
                    return s
            except Exception:  # noqa: BLE001
                continue
    return f"GTSM-{index:06d}"


def _gtsm_iso(t: Any) -> str:
    try:
        import numpy as np
        import pandas as pd  # type: ignore[import-not-found]
    except ImportError:
        return str(t)
    try:
        ts = pd.Timestamp(t) if not isinstance(t, np.datetime64) else pd.Timestamp(t)
        if ts.tz is None:
            ts = ts.tz_localize("UTC")
        return ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:  # noqa: BLE001
        return str(t)


def _gtsm_netcdf_to_records(spec: SourceSpec, nc_paths: list[str], bbox: tuple[float, float, float, float], output: str) -> list[dict[str, Any]]:
    import numpy as np
    import xarray as xr

    sc = spec.error_code_prefix
    west, south, east, north = bbox
    try:
        if len(nc_paths) == 1:
            ds = xr.open_dataset(nc_paths[0], chunks=None)
        else:
            ds = xr.open_mfdataset(sorted(nc_paths), combine="by_coords", chunks=None, parallel=False)
    except Exception as exc:  # noqa: BLE001
        raise router_upstream_error(sc, f"xarray could not open GTSM NetCDF(s) {nc_paths!r}: {exc}")
    try:
        lon_name = _gtsm_pick_coord(ds, ("station_x_coordinate", "lon", "longitude", "x"))
        lat_name = _gtsm_pick_coord(ds, ("station_y_coordinate", "lat", "latitude", "y"))
        if lon_name is None or lat_name is None:
            raise router_upstream_error(sc, f"GTSM NetCDF lacks station lon/lat coordinates; variables={list(ds.variables)}")
        lons = np.asarray(ds[lon_name].values, dtype=np.float64)
        lats = np.asarray(ds[lat_name].values, dtype=np.float64)
        lons_norm = np.where(lons > 180.0, lons - 360.0, lons)
        mask = (lons_norm >= west) & (lons_norm <= east) & (lats >= south) & (lats <= north)
        in_bbox_idx = np.flatnonzero(mask)
        if in_bbox_idx.size == 0:
            raise router_empty_error(sc, f"bbox={bbox} contains no GTSM gauges (network has {lons.size} stations globally)", spec.empty_error_suffix)
        target_cds_var = _GTSM_OUTPUT_TO_CDS_VARIABLE[output]
        coord_names = set(ds.coords)
        data_var = None
        for name in (target_cds_var, "waterlevel", "water_level", "surge"):
            if name in ds.data_vars:
                data_var = name
                break
        if data_var is None:
            data_vars = [v for v in ds.data_vars if v not in coord_names and v not in {lon_name, lat_name}]
            if not data_vars:
                raise router_upstream_error(sc, f"GTSM NetCDF lacks a recognizable water-level data variable; variables={list(ds.variables)}")
            data_var = data_vars[0]
        da = ds[data_var]
        time_name = _gtsm_pick_coord(ds, ("time", "datetime"))
        if time_name is None or time_name not in da.dims:
            raise router_upstream_error(sc, f"GTSM NetCDF data var {data_var!r} lacks a time dim; dims={list(da.dims)}")
        station_dim_candidates = [d for d in da.dims if d != time_name]
        if len(station_dim_candidates) != 1:
            raise router_upstream_error(sc, f"could not identify GTSM station dim for var {data_var!r}; dims={list(da.dims)}")
        station_dim = station_dim_candidates[0]
        time_strs = [_gtsm_iso(t) for t in ds[time_name].values]
        records: list[dict[str, Any]] = []
        for raw_idx in in_bbox_idx:
            i = int(raw_idx)
            try:
                series = np.asarray(da.isel({station_dim: i}).values, dtype=np.float64)
            except Exception as exc:  # noqa: BLE001
                logger.warning("gtsm.read: failed to isel station %d: %s", i, exc)
                continue
            if not np.isfinite(series).any():
                continue
            records.append({
                "gauge_id": _gtsm_pick_gauge_id(ds, station_dim, i),
                "lon": float(lons_norm[i]),
                "lat": float(lats[i]),
                "times": time_strs,
                "values": [float(v) for v in series],
            })
        if not records:
            raise router_empty_error(sc, f"bbox={bbox} matched {in_bbox_idx.size} gauge(s) but all carried all-NaN time series", spec.empty_error_suffix)
        return records
    finally:
        try:
            ds.close()
        except Exception:  # noqa: BLE001
            pass


def _gtsm_records_to_features(records: list[dict[str, Any]], output: str) -> list[dict[str, Any]]:
    """Per-gauge records -> GeoJSON Point features (the shared vector_fgb writer's shape)."""
    import numpy as np

    features: list[dict[str, Any]] = []
    for rec in records:
        buf = io.StringIO()
        writer = csv.writer(buf)
        finite_values: list[float] = []
        for ts, val in zip(rec["times"], rec["values"], strict=False):
            if val is None:
                continue
            try:
                v = float(val)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(v):
                continue
            writer.writerow([ts, f"{v:.6f}"])
            finite_values.append(v)
        if not finite_values:
            continue
        times = rec["times"]
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [rec["lon"], rec["lat"]]},
            "properties": {
                "gauge_id": rec["gauge_id"],
                "lon": rec["lon"],
                "lat": rec["lat"],
                "time_start": times[0] if times else "",
                "time_end": times[-1] if times else "",
                "n_timesteps": len(finite_values),
                "wl_min_m": float(np.nanmin(finite_values)),
                "wl_max_m": float(np.nanmax(finite_values)),
                "wl_mean_m": float(np.nanmean(finite_values)),
                "output": output,
                "time_series_csv": buf.getvalue(),
            },
        })
    return features


@register_hook("gtsm.read")
def gtsm_read(spec: SourceSpec, params: dict[str, Any], *, timeout_s: float) -> list[dict[str, Any]]:
    """CDS retrieve -> ZIP -> NetCDF(s) -> bbox-subset -> GeoJSON gauge features."""
    bbox = tuple(float(v) for v in params["bbox"])  # type: ignore[assignment]
    d0 = _dt.date.fromisoformat(str(params["start_date"]))
    d1 = _dt.date.fromisoformat(str(params["end_date"]))
    output = str(params.get("output", "water_level"))
    api_key = _resolve_key(params)

    zip_fd, zip_path = tempfile.mkstemp(suffix=".zip", prefix="trid3nt_gtsm_cds_")
    os.close(zip_fd)
    nc_paths: list[str] = []
    try:
        _cds_retrieve_with_timeout(
            spec, dataset=_GTSM_DATASET, request=_gtsm_build_request(output, d0, d1),
            out_path=zip_path, api_key=api_key, timeout_s=timeout_s, missing_phrases=(),
        )
        nc_paths = _gtsm_extract_netcdfs(spec, zip_path)
        records = _gtsm_netcdf_to_records(spec, nc_paths, bbox, output)
        return _gtsm_records_to_features(records, output)
    finally:
        try:
            os.unlink(zip_path)
        except OSError:
            pass
        for p in nc_paths:
            try:
                os.unlink(p)
            except OSError:
                pass
        if nc_paths:
            try:
                os.rmdir(os.path.dirname(nc_paths[0]))
            except OSError:
                pass
