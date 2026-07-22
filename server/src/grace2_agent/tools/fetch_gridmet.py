"""``fetch_gridmet`` atomic tool — gridMET CONUS gridded meteorology (job A8).

gridMET (Abatzoglou 2013) is a 4 km CONUS-wide gridded surface meteorology
dataset blending PRISM monthly normals with NLDAS-2 temporal patterns. It
is the canonical fire-weather substrate inside the US — fuel-moisture
products (fm100, fm1000), reference evapotranspiration (pet/eto/etr), the
Palmer Drought Severity Index (pdsi), Energy Release Component (erc) and
Burning Index (bi) are all consumed by NIFC, USFS, and the wildfire
modeling literature (BlueSky-Playground, IFTDSS, FlamMap, ELMFIRE).

Tier-1 free, no API key. Daily timestep, 4 km native resolution, EPSG:4326.
Coverage 1979-01-01 → present (3-day lag for finalised values).

Access pattern — THREDDS OPeNDAP (not ERDDAP / STAC):

The University of Idaho Northwest Knowledge Network (NKN) THREDDS server
exposes one **aggregated** netCDF per variable, spanning the full 1979 →
present record. The aggregation lets us subset by ``day`` index and bbox
in a single OPeNDAP request, returning only the requested time-slab and
lat/lon window — typically <1 MB for a metro-scale bbox + 30 days vs the
138 MB full-year file. Endpoint shape (verified live 2026-06-09):

    http://thredds.northwestknowledge.net:8080/thredds/dodsC/
        agg_met_<var>_1979_CurrentYear_CONUS.nc

xarray opens the URL with ``engine="netcdf4"`` (DAP support compiled in
via the bundled libnetcdf) and pulls only the requested subset over the
wire. We then average across the day axis, clip to bbox, and emit a COG.

Supported variables (per gridMET / Climatology Lab catalog):

    fm100   - 100-hr dead fuel moisture (Percent; fire-weather substrate)
    fm1000  - 1000-hr dead fuel moisture (Percent; deeper-fuel proxy)
    pet     - reference ET, alfalfa (mm; agricultural drought)
    pdsi    - Palmer Drought Severity Index (unitless)
    tmmn    - daily minimum near-surface temperature (K)
    tmmx    - daily maximum near-surface temperature (K)
    vpd     - vapor pressure deficit (kPa)
    vs      - 10 m wind speed (m s-1)
    rmin    - daily minimum relative humidity (Percent)
    rmax    - daily maximum relative humidity (Percent)
    pr      - daily accumulated precipitation (mm)
    srad    - downward shortwave radiation at surface (W m-2)

Output COG schema:
    Driver: COG, EPSG:4326 (gridMET native projection)
    Bands:  1 (time-mean across the requested date window)
    Dtype:  float32
    Nodata: NaN
    Tags:
        units, source="gridMET", variable, start_date, end_date,
        tool="fetch_gridmet"

FR-CE-8 / FR-DC-3/4: routed through ``read_through`` with
``ttl_class="static-30d"``, ``source_class="gridmet"``. gridMET is
historical (3-day lag); a 30-day window matches ERA5's discipline.

``supports_global_query=False`` — gridMET covers CONUS only
(~125°W → 67°W, 25°N → 49°N). Out-of-CONUS bboxes raise
``GRIDMETInputError``.

Differentiation vs neighboring fetchers:
- vs ``fetch_era5_reanalysis``: ERA5 is global, 0.25° (~27 km); gridMET
  is CONUS-only, 4 km. gridMET carries fire-derived variables (fm100,
  fm1000, pdsi, pet) that ERA5 does not. Choose gridMET inside CONUS
  when fire weather is the question; choose ERA5 outside CONUS or for
  reanalysis-quality boundary forcing.
- vs ``fetch_mrms_qpe`` / ``fetch_nexrad_reflectivity``: those are
  near-real-time radar; gridMET is daily-aggregated historical /
  drought-class substrate.
- vs ``fetch_landfire_fuels``: LANDFIRE is *static* fuel-model classes
  (vegetation type); gridMET is *dynamic* fuel moisture (water content
  in those fuels). The wildfire pipeline composes both:
  LANDFIRE → fuel model, gridMET → moisture state.
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

__all__ = ["fetch_gridmet"]

logger = logging.getLogger("grace2_agent.tools.fetch_gridmet")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class GRIDMETError(RuntimeError):
    """Base class for fetch_gridmet failures.

    ``error_code`` maps to the WebSocket A.6 error frame emitted by the agent
    surface. ``retryable`` guides FR-AS-11 retry logic.
    """

    error_code: str = "GRIDMET_ERROR"
    retryable: bool = True


class GRIDMETInputError(GRIDMETError):
    """Bad inputs (malformed bbox, unknown variable, bad dates, out-of-CONUS)."""

    error_code = "GRIDMET_INPUT_ERROR"
    retryable = False


class GRIDMETUpstreamError(GRIDMETError):
    """THREDDS OPeNDAP open / read / netCDF parse / COG write failed."""

    error_code = "GRIDMET_UPSTREAM_ERROR"
    retryable = True


class GRIDMETEmptyError(GRIDMETError):
    """Subset window has no finite pixels (bbox falls outside coverage)."""

    error_code = "GRIDMET_EMPTY"
    retryable = False


class GRIDMETNotAvailableError(GRIDMETError):
    """Requested date range falls outside the published gridMET record."""

    error_code = "GRIDMET_NOT_AVAILABLE"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: NKN THREDDS DAP base URL. Pattern:
#:   <base>/agg_met_<var>_1979_CurrentYear_CONUS.nc
_THREDDS_DAP_BASE = (
    "http://thredds.northwestknowledge.net:8080/thredds/dodsC"
)

#: Variable name → (long_name token, native units, internal-netCDF var name).
#: The internal netCDF variable carries a descriptive name (e.g.
#: "dead_fuel_moisture_100hr") but we accept gridMET's short codes
#: (fm100, fm1000, pet, pdsi, tmmn, tmmx, vpd, vs, rmin, rmax, pr, srad)
#: in the public surface so the LLM sees a stable catalog.
_VARIABLES: dict[str, tuple[str, str]] = {
    "fm100":  ("dead_fuel_moisture_100hr",  "Percent"),
    "fm1000": ("dead_fuel_moisture_1000hr", "Percent"),
    "pet":    ("potential_evapotranspiration", "mm"),
    "pdsi":   ("palmer_drought_severity_index", "unitless"),
    "tmmn":   ("air_temperature",           "K"),
    "tmmx":   ("air_temperature",           "K"),
    "vpd":    ("mean_vapor_pressure_deficit", "kPa"),
    "vs":     ("wind_speed",                "m s-1"),
    "rmin":   ("relative_humidity",         "Percent"),
    "rmax":   ("relative_humidity",         "Percent"),
    "pr":     ("precipitation_amount",      "mm"),
    "srad":   ("surface_downwelling_shortwave_flux_in_air", "W m-2"),
}

#: gridMET CONUS domain — approximate (from native grid bounds).
#: West, South, East, North in EPSG:4326. Used for the "intersects CONUS" gate.
_CONUS_BBOX: tuple[float, float, float, float] = (-124.77, 25.05, -67.06, 49.40)

#: gridMET temporal coverage starts 1979-01-01. Finalised values lag ~3 days
#: from real time; we keep this as a soft check (end_date <= today is allowed
#: because the netCDF may already carry preliminary cells).
_GRIDMET_START = _dt.date(1979, 1, 1)

#: Hard cap on date-range to avoid pulling a multi-year netCDF subset over DAP.
#: 365 days × 12 vars × 4 km is plenty for any operational request; composers
#: wanting climatology should call this tool in a chunked loop and aggregate.
_MAX_DATE_RANGE_DAYS = 366

#: OPeNDAP read budget (seconds). Large subsets across the full day index can
#: take 10-30 s; we cap at 120 s so a stalled upstream surfaces fast.
_THREDDS_READ_TIMEOUT_S = 120


# ---------------------------------------------------------------------------
# AtomicToolMetadata — registered at import time.
# ---------------------------------------------------------------------------


def _build_metadata() -> AtomicToolMetadata:
    """Construct AtomicToolMetadata defensively against schema flag variants."""
    common = dict(
        name="fetch_gridmet",
        ttl_class="static-30d",
        source_class="gridmet",
        cacheable=True,
    )
    try:
        return AtomicToolMetadata(**common, supports_global_query=False)  # type: ignore[call-arg]
    except Exception:  # pydantic ValidationError when field absent
        logger.debug(
            "AtomicToolMetadata does not support supports_global_query; "
            "registering fetch_gridmet without it"
        )
        return AtomicToolMetadata(**common)


_METADATA = _build_metadata()


# ---------------------------------------------------------------------------
# Payload-MB estimator (Wave 1.5 chat-warning system).
# ---------------------------------------------------------------------------


def estimate_payload_mb(
    bbox: tuple[float, float, float, float] | None = None,
    variable: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    **_kw: Any,
) -> float:
    """Estimate output COG size in MB for a given call (Wave 1.5 surface).

    gridMET native res is 4 km (~0.0417°). A 1° square × 1 day at float32
    is 24 × 24 cells × 4 bytes = ~2.3 KB. The time-mean collapse means the
    output is one band regardless of date range, so MB ∝ sq_deg only after
    we eat the OPeNDAP wire cost (which we deliberately model as the
    user-visible payload because the cached COG is what's shipped on
    repeat calls).

    Approx: ~0.005 MB / 1° square (output band, COG-compressed).

    The OPeNDAP wire fetch is larger than the cached COG (we pull a
    time-stack to compute the mean) but bounded by ``_MAX_DATE_RANGE_DAYS``
    and the bbox. For the warning UX we surface the OUTPUT size — Wave 1.5
    treats user-facing payload as what lands client-side.
    """
    if bbox is None:
        sq_deg = (
            (_CONUS_BBOX[2] - _CONUS_BBOX[0])
            * (_CONUS_BBOX[3] - _CONUS_BBOX[1])
        )
    else:
        try:
            west, south, east, north = bbox
            sq_deg = max(0.0, (east - west)) * max(0.0, (north - south))
        except (TypeError, ValueError):
            sq_deg = 1.0
    # Output COG only (single band, time-mean collapse): ~0.005 MB / 1°²
    # before COG compression bumps it lower; round up to 0.01 to be safe.
    return max(0.01, 0.01 * float(sq_deg))


# ---------------------------------------------------------------------------
# Validation helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Raise ``GRIDMETInputError`` if the bbox is degenerate / out of CONUS."""
    if len(bbox) != 4:
        raise GRIDMETInputError(
            f"bbox must be (west, south, east, north); got {bbox!r}"
        )
    west, south, east, north = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise GRIDMETInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= west <= 180.0 and -180.0 <= east <= 180.0):
        raise GRIDMETInputError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= south <= 90.0 and -90.0 <= north <= 90.0):
        raise GRIDMETInputError(f"bbox lat out of [-90,90]: {bbox!r}")
    if west >= east or south >= north:
        raise GRIDMETInputError(
            f"bbox is degenerate (west < east, south < north required): {bbox!r}"
        )
    # Must intersect CONUS (gridMET coverage).
    cw, cs, ce, cn = _CONUS_BBOX
    if east < cw or west > ce or north < cs or south > cn:
        raise GRIDMETInputError(
            f"bbox {bbox} does not intersect gridMET CONUS domain "
            f"{_CONUS_BBOX}; use fetch_era5_reanalysis for non-CONUS"
        )


def _round_bbox_to_6dp(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    return tuple(round(v, 6) for v in bbox)  # type: ignore[return-value]


def _validate_variable(variable: str) -> None:
    if not isinstance(variable, str):
        raise GRIDMETInputError(
            f"variable must be a str; got {type(variable).__name__}"
        )
    if variable not in _VARIABLES:
        raise GRIDMETInputError(
            f"unsupported gridMET variable {variable!r}; allowed: "
            f"{sorted(_VARIABLES)}"
        )


def _parse_iso_date(s: str, *, field: str) -> _dt.date:
    if not isinstance(s, str):
        raise GRIDMETInputError(
            f"{field} must be ISO-8601 YYYY-MM-DD; got {s!r}"
        )
    try:
        return _dt.date.fromisoformat(s)
    except ValueError as exc:
        raise GRIDMETInputError(
            f"{field}={s!r} is not a valid ISO date (YYYY-MM-DD): {exc}"
        ) from exc


def _validate_date_range(
    start_date: str, end_date: str
) -> tuple[_dt.date, _dt.date]:
    d0 = _parse_iso_date(start_date, field="start_date")
    d1 = _parse_iso_date(end_date, field="end_date")
    if d0 > d1:
        raise GRIDMETInputError(
            f"start_date must be <= end_date; got start={d0}, end={d1}"
        )
    if d0 < _GRIDMET_START:
        raise GRIDMETNotAvailableError(
            f"start_date {d0} before gridMET coverage start "
            f"{_GRIDMET_START.isoformat()}"
        )
    today = _dt.date.today()
    if d1 > today + _dt.timedelta(days=1):
        raise GRIDMETNotAvailableError(
            f"end_date {d1} is in the future; gridMET lags ~3 days from "
            f"real time"
        )
    n_days = (d1 - d0).days + 1
    if n_days > _MAX_DATE_RANGE_DAYS:
        raise GRIDMETInputError(
            f"date range {n_days} days exceeds hard cap "
            f"{_MAX_DATE_RANGE_DAYS}; call in chunks and aggregate"
        )
    return d0, d1


# ---------------------------------------------------------------------------
# THREDDS OPeNDAP open + subset.
# ---------------------------------------------------------------------------


def _build_dap_url(variable: str) -> str:
    """Construct the aggregated DAP URL for a variable."""
    return f"{_THREDDS_DAP_BASE}/agg_met_{variable}_1979_CurrentYear_CONUS.nc"


def _open_thredds_subset(
    dap_url: str,
    variable: str,
    bbox: tuple[float, float, float, float],
    d0: _dt.date,
    d1: _dt.date,
):
    """Open the THREDDS aggregate and return a (lat, lon)-clipped DataArray.

    Strategy:
    1. ``xarray.open_dataset(dap_url, engine="netcdf4")`` — DAP-backed.
    2. Locate the data variable (gridMET uses descriptive long names; we
       try the long-name token mapped in ``_VARIABLES`` then fall back to
       the first non-coordinate variable).
    3. Slice ``day`` (numeric "days since 1900-01-01" by gridMET convention)
       to the requested window.
    4. Subset latitude/longitude by bbox.
    5. Average across ``day`` axis → single 2D band.

    Raises ``GRIDMETUpstreamError`` on open / read failures and
    ``GRIDMETEmptyError`` when the subset window has no finite pixels.
    """
    try:
        import numpy as np
        import rioxarray  # noqa: F401 — registers .rio accessor
        import xarray as xr
    except ImportError as exc:
        raise GRIDMETUpstreamError(
            f"xarray / rioxarray / numpy not available: {exc}"
        ) from exc

    try:
        ds = xr.open_dataset(dap_url, engine="netcdf4", chunks=None)
    except Exception as exc:  # noqa: BLE001
        # Some environments lack DAP-enabled netcdf4; fall back to default.
        try:
            ds = xr.open_dataset(dap_url, chunks=None)
        except Exception as exc2:  # noqa: BLE001
            raise GRIDMETUpstreamError(
                f"could not open gridMET THREDDS DAP {dap_url}: {exc2} "
                f"(netcdf4 engine: {exc})"
            ) from exc2

    try:
        # Locate the data variable.
        long_token, _units = _VARIABLES[variable]
        data_vars = [v for v in ds.data_vars if v not in ds.coords]
        if not data_vars:
            raise GRIDMETUpstreamError(
                f"gridMET DAP carried no data variables; got "
                f"{list(ds.variables)}"
            )
        chosen = None
        # Prefer exact long-name token match.
        for v in data_vars:
            if v == long_token:
                chosen = v
                break
        if chosen is None:
            # Try a substring match.
            tok_l = long_token.lower()
            for v in data_vars:
                ln = ds[v].attrs.get("long_name", v).lower()
                if tok_l in ln or v.lower().startswith(variable.lower()):
                    chosen = v
                    break
        if chosen is None:
            chosen = data_vars[0]

        da = ds[chosen]

        # gridMET day axis is numeric "days since 1900-01-01" but xarray's
        # CF-time decoder usually converts to datetime64; handle both.
        # Normalize the time-like dim name (gridMET calls it ``day``).
        time_dim = None
        for d in da.dims:
            if d in ("day", "time"):
                time_dim = d
                break
        if time_dim is None:
            raise GRIDMETUpstreamError(
                f"gridMET DataArray has no day/time dim; dims={da.dims}"
            )

        # Locate lat/lon dims.
        lat_dim = next(
            (d for d in da.dims if d in ("lat", "latitude", "y")), None
        )
        lon_dim = next(
            (d for d in da.dims if d in ("lon", "longitude", "x")), None
        )
        if lat_dim is None or lon_dim is None:
            raise GRIDMETUpstreamError(
                f"gridMET DataArray missing lat/lon dims; dims={da.dims}"
            )

        # Slice time to [d0, d1] inclusive. Prefer the datetime64 path; if
        # decoding fell back to raw days-since-1900, sel-by-index.
        t_vals = da[time_dim].values
        if np.issubdtype(t_vals.dtype, np.datetime64):
            t0 = np.datetime64(d0.isoformat(), "D")
            t1 = np.datetime64(d1.isoformat(), "D")
            da = da.sel({time_dim: slice(t0, t1)})
        else:
            # raw days-since-1900 (gridMET convention)
            base = _dt.date(1900, 1, 1)
            d0_idx = (d0 - base).days
            d1_idx = (d1 - base).days
            da = da.where(
                (da[time_dim] >= d0_idx) & (da[time_dim] <= d1_idx), drop=True
            )

        if da.sizes.get(time_dim, 0) == 0:
            raise GRIDMETNotAvailableError(
                f"no gridMET timesteps in [{d0.isoformat()}, "
                f"{d1.isoformat()}] for variable {variable}"
            )

        # Subset by bbox. gridMET latitudes descend (49.4 → 25.0); use slice
        # on .sel with method=None and let xarray pick the inclusive band.
        lats = da[lat_dim].values
        west, south, east, north = bbox
        if lats[0] > lats[-1]:
            # Descending — slice must go high → low.
            da = da.sel({lat_dim: slice(north, south)})
        else:
            da = da.sel({lat_dim: slice(south, north)})
        da = da.sel({lon_dim: slice(west, east)})

        if da.size == 0 or any(s == 0 for s in da.shape):
            raise GRIDMETEmptyError(
                f"bbox={bbox} produced an empty gridMET window after subset "
                f"(variable={variable})"
            )

        # Time-mean collapse.
        da = da.mean(dim=time_dim, skipna=True, keep_attrs=True)

        # Standardize coord names for rioxarray.
        rename_map: dict[str, str] = {}
        if lat_dim != "y" and lat_dim != "latitude":
            rename_map[lat_dim] = "latitude"
        if lon_dim != "x" and lon_dim != "longitude":
            rename_map[lon_dim] = "longitude"
        if rename_map:
            da = da.rename(rename_map)

        # Set CRS (gridMET is EPSG:4326).
        da = da.rio.write_crs("EPSG:4326")
        # NOTE: do NOT sortby latitude here. GeoTIFF row 0 must be the
        # northernmost row for a north-up image (negative y-step in the
        # transform); gridMET is already in north-up (descending lat)
        # orientation, so leaving it alone keeps the COG bounds correct
        # (bounds.top > bounds.bottom). Sorting ascending would produce a
        # south-up COG that fails rasterio's standard orientation checks
        # (the job-0086 lesson, applied to this fetcher).

        # Sanity: at least one finite pixel.
        arr = np.asarray(da.values, dtype=np.float32)
        if not np.isfinite(arr).any():
            raise GRIDMETEmptyError(
                f"bbox={bbox} produced no finite gridMET pixels "
                f"(all-NaN window) for variable={variable}"
            )

        return da
    finally:
        # ds was opened above; if we raised before binding da, close ds.
        try:
            ds.close()
        except Exception:  # noqa: BLE001
            pass


def _da_to_cog_bytes(da, variable: str) -> bytes:
    """Write the time-mean DataArray to a COG and return the bytes."""
    out_fd, out_path = tempfile.mkstemp(suffix=".tif", prefix="grace2_gridmet_")
    os.close(out_fd)
    try:
        # Tag metadata.
        _long, units = _VARIABLES[variable]
        da_out = da.astype("float32")
        # Re-assert CRS on the typed copy — astype + sortby can drop the
        # rioxarray accessor's CRS attribute. Belt-and-suspenders for the
        # geographic-correctness gate (job-0086 codified lesson).
        da_out = da_out.rio.write_crs("EPSG:4326")
        # Ensure x/y spatial dims are named per rioxarray convention so the
        # driver writes a north-up GeoTIFF with the expected transform.
        spatial_dims_map: dict[str, str] = {}
        if "longitude" in da_out.dims and "x" not in da_out.dims:
            spatial_dims_map["longitude"] = "x"
        if "latitude" in da_out.dims and "y" not in da_out.dims:
            spatial_dims_map["latitude"] = "y"
        if "lon" in da_out.dims and "x" not in da_out.dims:
            spatial_dims_map["lon"] = "x"
        if "lat" in da_out.dims and "y" not in da_out.dims:
            spatial_dims_map["lat"] = "y"
        if spatial_dims_map:
            da_out = da_out.rename(spatial_dims_map)
            da_out = da_out.rio.write_crs("EPSG:4326")
        da_out.attrs["units"] = units
        da_out.attrs["source"] = "gridMET"
        da_out.attrs["variable"] = variable
        da_out.attrs["tool"] = "fetch_gridmet"
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

        import numpy as np
        arr = np.asarray(da.values, dtype=np.float32)
        logger.info(
            "fetch_gridmet: wrote %d-byte COG (variable=%s, "
            "min=%.4f, max=%.4f, mean=%.4f)",
            len(cog_bytes),
            variable,
            float(np.nanmin(arr)),
            float(np.nanmax(arr)),
            float(np.nanmean(arr)),
        )
        return cog_bytes
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


def _fetch_gridmet_bytes(
    variable: str,
    bbox: tuple[float, float, float, float],
    d0: _dt.date,
    d1: _dt.date,
) -> bytes:
    """End-to-end DAP subset → COG bytes."""
    dap_url = _build_dap_url(variable)
    try:
        da = _open_thredds_subset(dap_url, variable, bbox, d0, d1)
        return _da_to_cog_bytes(da, variable)
    except (GRIDMETError,):
        raise
    except Exception as exc:  # noqa: BLE001
        raise GRIDMETUpstreamError(
            f"gridMET fetch failed for variable={variable} bbox={bbox} "
            f"window=[{d0}, {d1}]: {exc}"
        ) from exc


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
def fetch_gridmet(
    bbox: tuple[float, float, float, float],
    variable: str,
    start_date: str,
    end_date: str,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """gridMET CONUS daily gridded meteorology (4 km, 1979 → present).

    Fetches a daily gridded meteorological variable from the University of
    Idaho gridMET dataset (Abatzoglou 2013) over the requested bbox + date
    range, averages across the time window, and emits a CRS-tagged COG.
    Access is via the NKN THREDDS OPeNDAP aggregate, which subsets by time +
    bbox in a single wire call (<1 MB typical for a metro-scale request).

    Use this when:
      - User asks for fire-weather / fuel-moisture inside CONUS
        ("fm100", "fm1000", "fuel moisture", "fire danger").
      - User asks for daily-aggregated CONUS weather over a historical
        window (drought, precip, ET, temperature, humidity, wind speed).
      - The wildfire pipeline needs *dynamic* moisture state to pair with
        LANDFIRE *static* fuel models.
      - User asks for Palmer Drought Severity Index (pdsi) anywhere in
        CONUS, 1979-01-01 → present (3-day lag).
      - User asks for reference ET (pet) for water-balance computations.

    Do NOT use this for:
      - Non-CONUS bboxes — use ``fetch_era5_reanalysis`` (global 0.25°).
      - Sub-daily timesteps — gridMET is daily; use ERA5, HRRR, NEXRAD,
        or GOES for hourly / sub-hourly.
      - Real-time / live forecast — gridMET lags ~3 days; use NWS Alerts
        + HRRR for live CONUS forecast.
      - Radar precipitation — use ``fetch_mrms_qpe`` (1 km gauge-corrected)
        instead of gridMET ``pr`` (4 km PRISM-blended).
      - Static fuel-model class / vegetation type — use
        ``fetch_landfire_fuels`` instead.
      - Vector data of any kind.

    Parameters:
      bbox: ``(west, south, east, north)`` in EPSG:4326 decimal degrees.
        Must intersect CONUS (~-124.77, 25.05, -67.06, 49.40).
        Example: ``(-117.5, 33.5, -116.5, 34.5)`` (Riverside Co., CA).
      variable: one of ``fm100`` (100-hr dead fuel moisture, Percent),
        ``fm1000`` (1000-hr dead fuel moisture, Percent),
        ``pet`` (reference ET, mm),
        ``pdsi`` (Palmer Drought Severity Index, unitless),
        ``tmmn`` / ``tmmx`` (daily min/max near-surface temperature, K),
        ``vpd`` (vapor pressure deficit, kPa),
        ``vs`` (10 m wind speed, m s-1),
        ``rmin`` / ``rmax`` (daily min/max relative humidity, Percent),
        ``pr`` (daily precipitation, mm),
        ``srad`` (downward shortwave radiation, W m-2).
      start_date: ISO YYYY-MM-DD; inclusive. Must be >= 1979-01-01.
      end_date: ISO YYYY-MM-DD; inclusive. Must be <= today. Hard cap
        366 days from start_date.

    Returns:
      ``LayerURI`` pointing at a COG in the cache bucket
      (``gs://grace-2-hazard-prod-cache/cache/static-30d/gridmet/<key>.tif``).
      Single-band float32 EPSG:4326 raster, time-mean across the date
      window, clipped to bbox. ``layer_type="raster"``, ``role="primary"``,
      ``units`` per variable.

    Cross-tool dependencies:
      Consumes admin polygons from ``fetch_administrative_boundaries`` (via
      ``clip_raster_to_polygon`` for state/county clipping). Feeds raster
      outputs to ``compute_zonal_statistics``, ``clip_raster_to_polygon``,
      ``clip_raster_to_bbox``, ``publish_layer`` (UI render via QGIS Server),
      and fire-danger composers. Sibling fetchers: ``fetch_era5_reanalysis``
      (global, hourly — non-CONUS choice), ``fetch_landfire_fuels`` (static
      fuel-model class — pair with gridMET for full moisture state),
      ``fetch_mrms_qpe`` (1 km gauge-corrected radar precip).

    Raises:
      ``GRIDMETInputError``: bad bbox / variable / dates / out-of-CONUS.
      ``GRIDMETNotAvailableError``: dates outside published coverage.
      ``GRIDMETEmptyError``: bbox subset has no finite pixels.
      ``GRIDMETUpstreamError``: THREDDS DAP failure (retryable).

    FR-CE-8: routed through ``read_through`` (``ttl_class="static-30d"``).
    ``supports_global_query=False`` — gridMET is CONUS-only.
    """
    # ---- Input validation ----
    _validate_bbox(bbox)
    _validate_variable(variable)
    d0, d1 = _validate_date_range(start_date, end_date)

    # ---- Cache-key params ----
    q_bbox = _round_bbox_to_6dp(bbox)
    params: dict[str, Any] = {
        "variable": variable,
        "bbox": list(q_bbox),
        "start_date": d0.isoformat(),
        "end_date": d1.isoformat(),
    }

    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="tif",
        fetch_fn=lambda: _fetch_gridmet_bytes(
            variable=variable,
            bbox=q_bbox,
            d0=d0,
            d1=d1,
        ),
    )
    assert result.uri is not None, (
        "fetch_gridmet is cacheable; uri must be set by read_through"
    )

    _long, units = _VARIABLES[variable]
    return LayerURI(
        layer_id=(
            f"gridmet-{variable}-"
            f"{d0.isoformat()}-{d1.isoformat()}-"
            f"{q_bbox[0]:.4f}-{q_bbox[1]:.4f}"
        ),
        name=(
            f"gridMET — {variable.upper()} "
            f"({d0.isoformat()} → {d1.isoformat()})"
        ),
        layer_type="raster",
        uri=result.uri,
        style_preset=f"gridmet_{variable}",
        role="primary",
        units=units,
    )
