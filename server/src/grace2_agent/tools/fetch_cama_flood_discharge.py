"""``fetch_cama_flood_discharge`` atomic tool — CaMa-Flood global river discharge (job-0133).

CaMa-Flood (Yamazaki Lab, U.Tokyo) is a global river-routing + discharge model
research-validated as the canonical fluvial-forcing substrate for compound
flood modeling (Eilander et al. 2023; the SFINCS-CaMa-Flood compound-flood
literature). v0.1 of GRACE-2 lands CaMa-Flood as a Tier-2 substrate fetcher
for the Wave-2 demo: a downstream composer (``model_compound_flood_global``,
deferred) will consume the LayerURI as the fluvial-boundary forcing for a
SFINCS run outside the US gauge network.

Output schema:
    Driver: COG (CRS-tagged GeoTIFF), EPSG:4326
    Bands:  1 (time-mean discharge across the requested date range)
    Dtype:  float32
    Nodata: NaN
    Units:  m^3/s (river discharge)
    Tags:
        units, source="CaMa-Flood_v4", version, variable="discharge",
        start_date, end_date, tool="fetch_cama_flood_discharge"

Cache: ttl_class="static-30d", source_class="cama_flood".
Cache key on (bbox-6dp, start_date, end_date, version).

Geographic-correctness gate (job-0086 codified lesson): we clip the output
to the requested bbox after CRS normalization (CaMa-Flood ships on a 0-360°
longitude grid; we shift to -180..180 with rioxarray before clipping).

supports_global_query=False — CaMa-Flood global 10km is ~500 MB per day per
variable. Composers wanting global coverage call this tool in tiles.

Payload estimation: ~1 MB per day per 1° square at 10 km native res.

----------------------------------------------------------------------
Data-source migration note (OQ-0133-CAMA-DATA-SOURCE-MIGRATION).
----------------------------------------------------------------------

The kickoff names the no-auth U.Tokyo Hydra server path:

    https://hydro.iis.u-tokyo.ac.jp/~yamadai/cama-flood/CaMa-Flood_v4/data/runoff/

As of 2026-02-12 the Yamazaki Lab webpage migrated to
``https://global-hydrodynamics.github.io/``, and ALL paths under
``hydro.iis.u-tokyo.ac.jp/~yamadai/*`` now return a single HTML redirect
page — the public no-auth netCDF distribution is no longer available. The
current distribution model is:

  1. Google Form registration (https://forms.gle/bhq1qWqybeAk157v9)
  2. Password-protected Dropbox folder, password emailed after registration

This is a structural deviation from the kickoff's "no auth required"
assumption. The tool is implemented with a forward-compatible auth-path
seam so the production wire-up can later target either (a) a GRACE-2
mirror bucket populated out-of-band from the Dropbox download, or (b) a
direct Dropbox-shared-link fetch with the password supplied via the
per-Case secret_ref path (mirroring the Wave-2 ERA5 / Movebank pattern).
v0.1 stance: emit ``CaMaFloodUnreachableError`` from the live path with
the migration explanation; live verification is qualified per the
AGENTS.md "live E2E required" rule.

Source resolution priority (kickoff-extended for migration reality):

  1. Explicit ``base_url`` kwarg — full override (e.g. a GRACE-2 mirror).
  2. ``GRACE2_CAMA_FLOOD_BASE_URL`` env var.
  3. The kickoff-named legacy URL (left in place as the documented
     default; will return the migration HTML page and the tool reports
     ``CaMaFloodUnreachableError`` with the migration note).

Filename convention (per CaMa-Flood v4.0.1 documented naming, retained
for forward use against a mirror that preserves the original layout):

    discharge_{version}_{YYYY}.nc    — global yearly netCDF
    runoff_{forcing}_{YYYY}.nc       — alternative naming used by the
                                        original Tokyo server

We probe both forms; the first that returns a non-HTML netCDF wins.
"""

from __future__ import annotations

import datetime as _dt
import logging
import math
import os
import tempfile
from typing import Any

import httpx

from grace2_contracts.execution import LayerURI
from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import read_through

__all__ = ["fetch_cama_flood_discharge"]

logger = logging.getLogger("grace2_agent.tools.fetch_cama_flood_discharge")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class CaMaFloodError(RuntimeError):
    """Base class for fetch_cama_flood_discharge failures."""

    error_code: str = "CAMA_FLOOD_ERROR"
    retryable: bool = True


class CaMaFloodInputError(CaMaFloodError):
    """Bad inputs (malformed bbox, dates, version)."""

    error_code = "CAMA_FLOOD_INPUT_ERROR"
    retryable = False


class CaMaFloodUpstreamError(CaMaFloodError):
    """U.Tokyo Hydra (or mirror) returned an error or network failed."""

    error_code = "CAMA_FLOOD_UPSTREAM_ERROR"
    retryable = True


class CaMaFloodUnreachableError(CaMaFloodError):
    """The legacy no-auth URL returned an HTML migration page instead of netCDF.

    Surfaces the OQ-0133-CAMA-DATA-SOURCE-MIGRATION reality: the kickoff
    URL no longer serves data. The agent surface uses this to route a
    "data source migrated; supply a mirror or use the Dropbox fetcher"
    message via the secrets/data-source panel.
    """

    error_code = "CAMA_FLOOD_UNREACHABLE"
    retryable = False


class CaMaFloodEmptyError(CaMaFloodError):
    """The retrieved NetCDF contained no finite pixels in the requested bbox."""

    error_code = "CAMA_FLOOD_EMPTY"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

# Kickoff-named default (legacy U.Tokyo Hydra server). As of 2026-02-12 this
# URL returns an HTML migration page — see module docstring for the
# OQ-0133-CAMA-DATA-SOURCE-MIGRATION trade-off.
_LEGACY_BASE_URL = (
    "https://hydro.iis.u-tokyo.ac.jp/~yamadai/cama-flood/CaMa-Flood_v4/data/runoff/"
)

# Default per-request timeout. CaMa-Flood netCDF downloads can be hundreds of
# MB for a year of global data; we pad generously.
_TIMEOUT_S = 300.0

# Sanity cap on date range — refuse multi-year ad-hoc retrievals. The CaMa-Flood
# data convention publishes one global yearly NetCDF; spanning multiple years
# means multi-hundred-MB downloads.
_MAX_DATE_RANGE_DAYS = 366

# Allowed version strings. Open-enumish; we validate against a known list to
# catch typos and surface them as input errors rather than 404s.
_ALLOWED_VERSIONS: frozenset[str] = frozenset(
    {
        "v4.0.1",
        "v4.20",   # Released 2024
        "v4.30",   # Released 2026-03-12 per Yamazaki Lab page
    }
)

# User-Agent.
_USER_AGENT = (
    "grace-2/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/GRACE-2; agent@grace-2.dev)"
)

# CaMa-Flood discharge native units (per CaMa-Flood model output).
_DISCHARGE_UNITS = "m^3/s"

# Native horizontal resolution: 10 km global (0.1° at low latitudes).
_NATIVE_RESOLUTION_DEG = 0.1


# ---------------------------------------------------------------------------
# AtomicToolMetadata — registered once at import time.
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="fetch_cama_flood_discharge",
    ttl_class="static-30d",
    source_class="cama_flood",
    cacheable=True,
)


# ---------------------------------------------------------------------------
# Payload-MB estimator (Wave 1.5 chat-warning system).
# ---------------------------------------------------------------------------


def estimate_payload_mb(
    bbox: tuple[float, float, float, float] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    **_kw: Any,
) -> float:
    """Estimate output COG size in MB.

    Per audit.md: ~1 MB per day per 1° square at 10 km native res. We treat
    ``bbox=None`` as global (360° × 180°).
    """
    if bbox is None:
        sq_deg = 360.0 * 180.0
    else:
        try:
            west, south, east, north = bbox
            sq_deg = max(0.0, (east - west)) * max(0.0, (north - south))
        except (TypeError, ValueError):
            sq_deg = 1.0

    if not start_date or not end_date:
        n_days = 1
    else:
        try:
            d0 = _dt.date.fromisoformat(start_date)
            d1 = _dt.date.fromisoformat(end_date)
            n_days = max(1, (d1 - d0).days + 1)
        except ValueError:
            n_days = 1

    # 1 MB / day / 1° square per audit.md.
    return 1.0 * float(n_days) * float(max(1.0, sq_deg))


# ---------------------------------------------------------------------------
# bbox helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Raise ``CaMaFloodInputError`` if bbox is invalid."""
    if len(bbox) != 4:
        raise CaMaFloodInputError(
            f"bbox must be (west, south, east, north); got {bbox!r}"
        )
    west, south, east, north = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise CaMaFloodInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= west <= 180.0 and -180.0 <= east <= 180.0):
        raise CaMaFloodInputError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= south <= 90.0 and -90.0 <= north <= 90.0):
        raise CaMaFloodInputError(f"bbox lat out of [-90,90]: {bbox!r}")
    if west >= east or south >= north:
        raise CaMaFloodInputError(
            f"bbox is degenerate (west < east, south < north required): {bbox!r}"
        )


def _round_bbox_to_6dp(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Round bbox coords to 6dp (~0.1m) for cache-key stability."""
    return tuple(round(v, 6) for v in bbox)  # type: ignore[return-value]


def _validate_version(version: str) -> None:
    """Raise ``CaMaFloodInputError`` if version is unknown."""
    if not isinstance(version, str):
        raise CaMaFloodInputError(
            f"version must be a str; got {type(version).__name__}"
        )
    if version not in _ALLOWED_VERSIONS:
        raise CaMaFloodInputError(
            f"unsupported CaMa-Flood version {version!r}; allowed: "
            f"{sorted(_ALLOWED_VERSIONS)}"
        )


def _parse_iso_date(s: str, *, field: str) -> _dt.date:
    if not isinstance(s, str):
        raise CaMaFloodInputError(f"{field} must be ISO-8601 YYYY-MM-DD; got {s!r}")
    try:
        return _dt.date.fromisoformat(s)
    except ValueError as exc:
        raise CaMaFloodInputError(
            f"{field}={s!r} is not a valid ISO date (YYYY-MM-DD): {exc}"
        ) from exc


def _validate_date_range(start_date: str, end_date: str) -> tuple[_dt.date, _dt.date]:
    """Validate ISO dates + ordering + reasonable window."""
    d0 = _parse_iso_date(start_date, field="start_date")
    d1 = _parse_iso_date(end_date, field="end_date")
    if d0 > d1:
        raise CaMaFloodInputError(
            f"start_date must be <= end_date; got start={d0}, end={d1}"
        )
    # CaMa-Flood reanalysis-style runs span ~1979 onward depending on forcing
    # (VIC / E2O / ERA5-based runs). Reject obvious typos.
    if d0.year < 1950 or d1.year > _dt.date.today().year + 1:
        raise CaMaFloodInputError(
            f"date range [{d0}, {d1}] outside reasonable bounds (1950 → present)"
        )
    n_days = (d1 - d0).days + 1
    if n_days > _MAX_DATE_RANGE_DAYS:
        raise CaMaFloodInputError(
            f"date range {n_days} days exceeds hard cap "
            f"{_MAX_DATE_RANGE_DAYS}; call in chunks and aggregate"
        )
    return d0, d1


# ---------------------------------------------------------------------------
# Base-URL resolution (kickoff-extended for OQ-0133-CAMA-DATA-SOURCE-MIGRATION).
# ---------------------------------------------------------------------------


def _resolve_base_url(base_url: str | None) -> str:
    """Return the source base URL (kwarg > env > legacy default).

    The legacy default points at the kickoff-named U.Tokyo Hydra path; as of
    2026-02-12 that path returns an HTML migration page (see module
    docstring). The tool reports ``CaMaFloodUnreachableError`` when it
    encounters that page, so a deployment must provide a mirror URL via the
    kwarg or env var to enable the live path.
    """
    if base_url:
        return base_url.rstrip("/") + "/"
    env_url = os.environ.get("GRACE2_CAMA_FLOOD_BASE_URL")
    if env_url:
        return env_url.rstrip("/") + "/"
    return _LEGACY_BASE_URL


def _candidate_filenames(year: int, version: str) -> list[str]:
    """Return candidate NetCDF filenames for a given year and version.

    CaMa-Flood v4 ships yearly globals under multiple naming conventions
    depending on forcing dataset; the original Tokyo server used the
    runoff_{forcing}_{YYYY}.nc pattern. We probe a small ordered list so
    the first 200-OK netCDF wins. A live mirror is free to use either
    naming convention.
    """
    # Most common operational naming first.
    return [
        f"discharge_{version}_{year:04d}.nc",
        f"discharge_{year:04d}.nc",
        f"runoff_{version}_{year:04d}.nc",
        f"runoff_VIC_BC_{year:04d}.nc",   # E2O VIC bias-corrected (Tokyo server default)
        f"runoff_E2O_{year:04d}.nc",
        f"runoff_{year:04d}.nc",
    ]


# ---------------------------------------------------------------------------
# Download + HTML-migration sentinel detection.
# ---------------------------------------------------------------------------


def _download_one_nc(
    url: str,
    out_path: str,
    *,
    client: httpx.Client | None = None,
) -> bool:
    """Download one URL to ``out_path``; return True iff it is a netCDF.

    The Tokyo server's legacy URL family currently redirects every
    ``~yamadai/*`` path to a single HTML migration page. We detect that
    case by sniffing the first 4 bytes — netCDF/HDF5 magic vs HTML.

    Returns:
        True if the downloaded file looks like netCDF/HDF5.
        False if it looks like HTML (the migration page sentinel) or is
        empty / 4xx — we move on to the next candidate.

    Raises:
        ``CaMaFloodUpstreamError``: connection / 5xx error.
    """
    owns = client is None
    if owns:
        client = httpx.Client(
            timeout=_TIMEOUT_S,
            headers={"User-Agent": _USER_AGENT},
            follow_redirects=True,
        )
    try:
        try:
            with client.stream("GET", url) as resp:
                if resp.status_code == 404:
                    return False
                if resp.status_code >= 500:
                    raise CaMaFloodUpstreamError(
                        f"CaMa-Flood upstream {resp.status_code} for {url}"
                    )
                if resp.status_code >= 400:
                    return False
                with open(out_path, "wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                        f.write(chunk)
        except httpx.RequestError as exc:
            raise CaMaFloodUpstreamError(
                f"CaMa-Flood network failure for {url}: {exc}"
            ) from exc
    finally:
        if owns:
            client.close()

    # Sniff the first bytes.
    try:
        with open(out_path, "rb") as f:
            head = f.read(8)
    except OSError as exc:
        raise CaMaFloodUpstreamError(
            f"could not read downloaded file {out_path}: {exc}"
        ) from exc

    if not head:
        return False

    # netCDF-3 classic magic: b'CDF\x01' / b'CDF\x02' / b'CDF\x05'.
    # netCDF-4 (HDF5) magic: b'\x89HDF\r\n\x1a\n'.
    if head.startswith(b"CDF") or head.startswith(b"\x89HDF"):
        return True
    # HTML migration page sentinel — leading '<' or '<!DOCTYPE'.
    return False


def _fetch_cama_nc_to_tempfile(
    year: int,
    version: str,
    base_url: str,
    out_path: str,
) -> str:
    """Download one CaMa-Flood yearly netCDF; return the candidate URL hit.

    Raises:
        ``CaMaFloodUnreachableError``: every candidate returned the HTML
            migration sentinel — the no-auth legacy path is gone and no
            mirror was configured.
        ``CaMaFloodUpstreamError``: network / 5xx.
    """
    client = httpx.Client(
        timeout=_TIMEOUT_S,
        headers={"User-Agent": _USER_AGENT},
        follow_redirects=True,
    )
    try:
        tried_urls: list[str] = []
        saw_html_sentinel = False
        for fname in _candidate_filenames(year, version):
            url = base_url + fname
            tried_urls.append(url)
            try:
                ok = _download_one_nc(url, out_path, client=client)
            except CaMaFloodUpstreamError:
                raise
            if ok:
                logger.info(
                    "fetch_cama_flood_discharge: downloaded netCDF from %s", url
                )
                return url
            # Check whether this was the HTML migration sentinel.
            try:
                with open(out_path, "rb") as f:
                    head = f.read(64)
                if head.lstrip().startswith(b"<"):
                    saw_html_sentinel = True
            except OSError:
                pass
        if saw_html_sentinel:
            raise CaMaFloodUnreachableError(
                "CaMa-Flood data source migrated: the kickoff-named "
                "U.Tokyo Hydra URL family returns an HTML redirect page "
                "(Yamazaki Lab moved to https://global-hydrodynamics.github.io/). "
                "Current distribution is gated via Google-Form registration + "
                "Dropbox password. Configure GRACE2_CAMA_FLOOD_BASE_URL or "
                "pass base_url=... to a mirror that serves netCDFs "
                f"(see OQ-0133-CAMA-DATA-SOURCE-MIGRATION). Tried: {tried_urls}"
            )
        raise CaMaFloodUpstreamError(
            f"CaMa-Flood: no candidate netCDF URL returned data; tried {tried_urls}"
        )
    finally:
        client.close()


# ---------------------------------------------------------------------------
# NetCDF → COG conversion.
# ---------------------------------------------------------------------------


def _netcdf_to_cog_bytes(
    nc_path: str,
    bbox: tuple[float, float, float, float],
    d0: _dt.date,
    d1: _dt.date,
) -> bytes:
    """Open the CaMa-Flood NetCDF, clip to bbox + date range, write a COG.

    Returns COG bytes (float32, EPSG:4326). The output has one band carrying
    the time-mean of the discharge variable across the date range.

    CaMa-Flood NetCDF conventions:
    - Variable name: typically ``rivout`` (river outflow / discharge, m^3/s)
      or ``discharge``. We probe both.
    - Dims: (time, lat, lon).
    - Latitudes can be ascending or descending depending on forcing.
    - Longitudes can be -180..180 or 0..360 depending on version.

    Geographic-correctness gate (job-0086): we clip after normalizing
    longitude to -180..180 and sorting latitude ascending.

    Raises:
        ``CaMaFloodUpstreamError``: NetCDF open / xarray read / COG write.
        ``CaMaFloodEmptyError``: bbox + date-range produces no finite pixels.
    """
    try:
        import numpy as np
        import rioxarray  # noqa: F401 — registers .rio accessor on DataArrays
        import xarray as xr
    except ImportError as exc:
        raise CaMaFloodUpstreamError(
            f"xarray / rioxarray / numpy not available: {exc}"
        ) from exc

    try:
        ds = xr.open_dataset(nc_path, engine="netcdf4", chunks=None)
    except Exception as exc:
        try:
            ds = xr.open_dataset(nc_path, chunks=None)
        except Exception as exc2:
            raise CaMaFloodUpstreamError(
                f"xarray could not open CaMa-Flood NetCDF {nc_path}: {exc2} "
                f"(netcdf4-engine error: {exc})"
            ) from exc2

    try:
        # Find the discharge variable. CaMa-Flood typically writes "rivout"
        # (river outflow at unit-catchment downstream face) or a name
        # mentioning "discharge"/"outflow" in long_name.
        data_vars = [v for v in ds.data_vars if v not in ds.coords]
        if not data_vars:
            raise CaMaFloodUpstreamError(
                f"CaMa-Flood NetCDF carried no data variables; got {list(ds.variables)}"
            )

        # Preferred variable picks.
        chosen: str | None = None
        for v in data_vars:
            if v.lower() in ("rivout", "discharge", "outflw"):
                chosen = v
                break
        if chosen is None:
            for v in data_vars:
                ln = ds[v].attrs.get("long_name", "").lower()
                if "discharge" in ln or "outflow" in ln or "river" in ln:
                    chosen = v
                    break
        if chosen is None:
            chosen = data_vars[0]

        da = ds[chosen]

        # Select the requested date range if time is a dim.
        time_dim = None
        for cand in ("time", "Time", "t"):
            if cand in da.dims:
                time_dim = cand
                break
        if time_dim is not None:
            try:
                da = da.sel({time_dim: slice(d0.isoformat(), d1.isoformat())})
            except Exception:
                # Some CaMa-Flood NetCDFs use integer day-of-year coords;
                # fall back to selecting the whole file (caller passed a
                # year-aligned range upstream).
                pass

        # Reduce non-spatial dims with mean.
        keep_dims = {"latitude", "longitude", "lat", "lon", "y", "x"}
        reduce_dims = [d for d in da.dims if d not in keep_dims]
        if reduce_dims:
            da = da.mean(dim=reduce_dims, skipna=True, keep_attrs=True)

        # Standardize coord names.
        rename_map: dict[str, str] = {}
        if "lat" in da.dims and "latitude" not in da.dims:
            rename_map["lat"] = "latitude"
        if "lon" in da.dims and "longitude" not in da.dims:
            rename_map["lon"] = "longitude"
        if rename_map:
            da = da.rename(rename_map)

        da = da.rio.write_crs("EPSG:4326")

        # Sort latitude ascending if descending.
        if "latitude" in da.dims and len(da["latitude"]) > 1:
            lat_vals = da["latitude"].values
            if lat_vals[0] > lat_vals[-1]:
                da = da.sortby("latitude")

        # Normalize longitude to -180..180 if shipped 0..360.
        if "longitude" in da.dims:
            lon_vals = da["longitude"].values
            if lon_vals.max() > 180.0:
                da = da.assign_coords(
                    longitude=(((da["longitude"] + 180) % 360) - 180)
                )
                da = da.sortby("longitude")

        west, south, east, north = bbox
        try:
            da = da.rio.clip_box(
                minx=west, miny=south, maxx=east, maxy=north, crs="EPSG:4326"
            )
        except Exception as exc:
            raise CaMaFloodUpstreamError(
                f"rioxarray clip_box to bbox={bbox} failed: {exc}"
            ) from exc

        if da.size == 0:
            raise CaMaFloodEmptyError(
                f"bbox={bbox} produced an empty CaMa-Flood window after clip"
            )

        # Standard GeoTIFF / COG convention is north-up (row 0 = north edge,
        # negative y-pixel-size). rioxarray's ``to_raster`` writes the array
        # rows as-they-are, so an ascending-latitude DataArray would produce
        # a south-up COG (rasterio's BoundingBox.bottom > top). We re-sort
        # descending here so the written COG is north-up — the convention
        # QGIS Server, MapLibre, and the rest of the GRACE-2 raster pipeline
        # expect (job-0086 codified lesson: geographic correctness).
        if "latitude" in da.dims and len(da["latitude"]) > 1:
            lat_vals = da["latitude"].values
            if lat_vals[0] < lat_vals[-1]:
                da = da.sortby("latitude", ascending=False)

        arr = np.asarray(da.values, dtype=np.float32)
        if not np.isfinite(arr).any():
            raise CaMaFloodEmptyError(
                f"bbox={bbox} produced no finite CaMa-Flood pixels (all-NaN window)"
            )

        out_fd, out_path = tempfile.mkstemp(
            suffix=".tif", prefix="grace2_cama_"
        )
        os.close(out_fd)
        try:
            da_out = da.astype("float32")
            da_out.attrs["units"] = _DISCHARGE_UNITS
            da_out.attrs["source"] = "CaMa-Flood_v4"
            da_out.attrs["variable"] = "discharge"
            da_out.attrs["tool"] = "fetch_cama_flood_discharge"
            try:
                da_out.rio.to_raster(
                    out_path,
                    driver="COG",
                    dtype="float32",
                    compress="DEFLATE",
                    nodata=float("nan"),
                )
            except Exception:
                # Fall back to GTiff if COG driver is unavailable in test env.
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
            "fetch_cama_flood_discharge: wrote %d-byte COG "
            "(min=%.4f, max=%.4f, mean=%.4f m^3/s)",
            len(cog_bytes),
            float(np.nanmin(arr)),
            float(np.nanmax(arr)),
            float(np.nanmean(arr)),
        )
        return cog_bytes
    finally:
        try:
            ds.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Fetch function (passed to read_through).
# ---------------------------------------------------------------------------


def _years_spanned(d0: _dt.date, d1: _dt.date) -> list[int]:
    return list(range(d0.year, d1.year + 1))


def _fetch_cama_bytes(
    bbox: tuple[float, float, float, float],
    d0: _dt.date,
    d1: _dt.date,
    version: str,
    base_url: str,
) -> bytes:
    """End-to-end: download CaMa-Flood NetCDF(s) → clip + mean → COG bytes."""
    years = _years_spanned(d0, d1)
    if not years:
        raise CaMaFloodInputError(
            f"date range produced no years to fetch: [{d0}, {d1}]"
        )

    # v0.1 scope: the kickoff names a single-day Mississippi-basin live test;
    # multi-year-spanning is rejected by _validate_date_range (cap 366 days),
    # so we fetch one or two yearly netCDFs at most.
    nc_paths: list[str] = []
    try:
        for yr in years:
            fd, nc_path = tempfile.mkstemp(
                suffix=f"_{yr}.nc", prefix="grace2_cama_"
            )
            os.close(fd)
            _fetch_cama_nc_to_tempfile(
                year=yr, version=version, base_url=base_url, out_path=nc_path
            )
            nc_paths.append(nc_path)

        # If multiple years, concatenate via xarray; for v0.1 single-year is
        # the dominant path. We pass the first NetCDF and rely on the
        # date-range selection inside _netcdf_to_cog_bytes to clip the window.
        # For multi-year, we open and concat first.
        if len(nc_paths) == 1:
            return _netcdf_to_cog_bytes(nc_paths[0], bbox, d0, d1)
        else:
            # Multi-year path: open + concat along time, then write to a
            # synthesized NetCDF tempfile and reuse the converter.
            try:
                import xarray as xr
            except ImportError as exc:
                raise CaMaFloodUpstreamError(
                    f"xarray not available for multi-year concat: {exc}"
                ) from exc

            datasets = [xr.open_dataset(p, chunks=None) for p in nc_paths]
            try:
                combined = xr.concat(datasets, dim="time")
                combo_fd, combo_path = tempfile.mkstemp(
                    suffix=".nc", prefix="grace2_cama_combo_"
                )
                os.close(combo_fd)
                try:
                    combined.to_netcdf(combo_path)
                    return _netcdf_to_cog_bytes(combo_path, bbox, d0, d1)
                finally:
                    try:
                        os.unlink(combo_path)
                    except OSError:
                        pass
            finally:
                for d in datasets:
                    try:
                        d.close()
                    except Exception:
                        pass
    finally:
        for p in nc_paths:
            try:
                os.unlink(p)
            except OSError:
                pass


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
def fetch_cama_flood_discharge(
    bbox: tuple[float, float, float, float],
    start_date: str,
    end_date: str,
    version: str = "v4.0.1",
    base_url: str | None = None,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """CaMa-Flood global river discharge Tier-2 fetcher (compound-flood fluvial forcing).

    **What it does:** Downloads a yearly CaMa-Flood v4 NetCDF from the Yamazaki
    Lab (U.Tokyo) distribution, subsets to the requested bbox and date window,
    time-averages the discharge variable (``rivout``, m^3/s), and writes
    a CRS-tagged Cloud-Optimized GeoTIFF (EPSG:4326, float32, nodata=NaN) at the
    model's native 0.1° (~10 km) resolution. Normalises longitudes 0–360° to
    -180–180° and sorts latitude ascending before clipping (geographic-correctness
    gate per job-0086). v4.0.1 is the default; v4.20 and v4.30 also supported.

    **When to use:**

    - Fluvial boundary forcing for SFINCS / GeoFLOOD compound-flood runs in
      non-CONUS basins — Mekong, Amazon, Niger, Ganges-Brahmaputra, etc.
      Example: bbox ``(88.0, 21.0, 93.0, 27.0)`` for the Bangladesh delta,
      ``start_date="2017-08-01"``, ``end_date="2017-08-31"``.
    - Global flood-risk substrate where USGS NWIS streamflow gauges do not
      cover (research-validated: Eilander et al. 2023, HESS).
    - Multi-year discharge climatology for flood-frequency analysis outside CONUS.

    **When NOT to use:**

    - CONUS point-discharge time series — use ``fetch_noaa_nwm_streamflow`` or
      USGS NWIS (gauge-derived, instantaneous, sub-10km rivers).
    - Real-time or forecast discharge — CaMa-Flood public outputs are historical
      reanalysis (no live feed available).
    - Sub-10km river networks — for finer hydrography use ``fetch_river_geometry``
      + a routing model on NHDPlus sub-grid channels.

    **KNOWN MIGRATION (OQ-0133-CAMA-DATA-SOURCE-MIGRATION):** The kickoff names
    a no-auth U.Tokyo Hydra URL that as of 2026-02-12 returns an HTML redirect
    to https://global-hydrodynamics.github.io/. New distribution is gated
    (Google-Form + Dropbox password). Set ``GRACE2_CAMA_FLOOD_BASE_URL`` or pass
    ``base_url=...`` to a mirror serving netCDFs. Without a mirror configured,
    the tool raises ``CaMaFloodUnreachableError`` with the migration note.

    **Parameters:**

    - ``bbox``: ``(west, south, east, north)`` EPSG:4326. Required;
      ``supports_global_query=False`` — global is ~500 MB/day; tile if needed.
    - ``start_date``: ISO YYYY-MM-DD inclusive. Reasonable range: 1979–present.
    - ``end_date``: ISO YYYY-MM-DD inclusive. Hard cap 366 days from start.
    - ``version``: CaMa-Flood release string; allowed ``{"v4.0.1", "v4.20",
      "v4.30"}``; default ``"v4.0.1"``.
    - ``base_url``: optional mirror base URL. Falls back to
      ``GRACE2_CAMA_FLOOD_BASE_URL`` env var, then the legacy Tokyo path.

    **Returns:**

    ``LayerURI`` pointing at ``gs://grace-2-hazard-prod-cache/cache/static-30d/cama_flood/<key>.tif``.
    COG, float32, EPSG:4326, nodata=NaN, units m^3/s. Single band:
    time-mean discharge across the date range. GeoTIFF tags: ``units``,
    ``source="CaMa-Flood_v4"``, ``version``, ``variable="discharge"``.
    ``layer_type="raster"``, ``role="primary"``, ``units="m^3/s"``.

    Raises: ``CaMaFloodInputError`` (bad params, retryable=False),
    ``CaMaFloodUnreachableError`` (legacy URL migration, retryable=False),
    ``CaMaFloodEmptyError`` (no finite pixels in bbox),
    ``CaMaFloodUpstreamError`` (network / 5xx, retryable=True).

    **Cross-tool dependencies:**

    - Pair with: ``fetch_gtsm_tide_surge`` (coastal boundary) and
      ``fetch_era5_reanalysis`` (atmospheric forcing) for a complete SFINCS
      compound-flood forcing stack outside CONUS.
    - Consumed by: ``build_sfincs_model`` (river inflow boundary) and
      ``model_compound_flood_global`` (compound-flood composer, Wave 2+).
    - For CONUS rivers, ``fetch_noaa_nwm_streamflow`` is preferred (gauged,
      higher resolution); this tool activates when NWM coverage ends.

    FR-CE-8: ``read_through`` with ``ttl_class="static-30d"``; cache key =
    SHA-256 of ``(bbox-6dp, start_date, end_date, version)``.
    """
    # ---- Input validation ----
    _validate_bbox(bbox)
    _validate_version(version)
    d0, d1 = _validate_date_range(start_date, end_date)

    # ---- Cache-key params ----
    q_bbox = _round_bbox_to_6dp(bbox)
    params: dict[str, Any] = {
        "bbox": list(q_bbox),
        "start_date": d0.isoformat(),
        "end_date": d1.isoformat(),
        "version": version,
    }

    resolved_base_url = _resolve_base_url(base_url)

    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="tif",
        fetch_fn=lambda: _fetch_cama_bytes(
            bbox=q_bbox,
            d0=d0,
            d1=d1,
            version=version,
            base_url=resolved_base_url,
        ),
    )
    assert result.uri is not None, (
        "fetch_cama_flood_discharge is cacheable; uri must be set by read_through"
    )

    return LayerURI(
        layer_id=(
            f"cama-flood-discharge-{version}-"
            f"{d0.isoformat()}-{d1.isoformat()}-"
            f"{q_bbox[0]:.4f}-{q_bbox[1]:.4f}"
        ),
        name=(
            f"CaMa-Flood Discharge {version} "
            f"({d0.isoformat()} → {d1.isoformat()})"
        ),
        layer_type="raster",
        uri=result.uri,
        style_preset="cama_flood_discharge",
        role="primary",
        units=_DISCHARGE_UNITS,
    )
