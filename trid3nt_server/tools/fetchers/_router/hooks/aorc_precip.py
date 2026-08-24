"""fetch_aorc_precip record hook: NOAA AORC v1.1 hourly precipitation.

The Analysis Of Record for Calibration (AORC) v1.1 is a ~800 m (30 arc-second)
gridded, hourly CONUS weather record spanning 1979-02 to ~10 days before present,
published as per-year cloud-optimized Zarr stores on the public AWS bucket
``noaa-nws-aorc-v1-1-1km`` (us-east-1, anonymous). Total precipitation is the
``APCP_surface`` variable (kg m-2 == mm, one-hour accumulation ending at the top
of each hour).

Deliverable: an AREA-MEAN HYETOGRAPH -- the hourly precipitation series averaged
over the request bbox (the rain-on-grid forcing series for a small basin) plus the
window accumulation stats. It is a bare structured JSON dict (``shape: record``),
not a renderable layer, so the router runs this hook through the pure-record path
(no build_request) and the hook OWNS the Zarr socket (the sanctioned library
impurity, mirroring the library_delegate rasters). ``_open_year`` is the single
injectable I/O seam so the windowing / mean / dict-shaping logic is unit-tested
offline against a synthetic xarray Dataset.

AORC complements ``fetch_mrms_qpe`` (MRMS QPE Pass2 begins ~2020-10): AORC is the
pre-2020 / any-historical-year precipitation forcing MRMS cannot reach.
"""

from __future__ import annotations

import datetime as _dt
import logging
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..errors import (
    RouterError,
    router_empty_error,
    router_not_available_error,
    router_upstream_error,
)
from . import register_hook

logger = logging.getLogger(
    "trid3nt_server.tools.fetchers._router.hooks.aorc_precip"
)

#: Public AWS Open Data bucket (NODD), per-year Zarr stores ``<year>.zarr``.
_BUCKET = "noaa-nws-aorc-v1-1-1km"
_REGION = "us-east-1"
#: The total-precipitation variable (kg m-2 == mm, hourly accumulation).
_PRECIP_VAR = "APCP_surface"
#: Coverage floor: CONUS record starts 1979-02-01.
_COVERAGE_START = _dt.date(1979, 2, 1)


def _open_year(year: int) -> Any:
    """Open one AORC year Zarr store as an xarray Dataset (anonymous S3).

    The single injectable I/O seam: tests monkeypatch this to return a synthetic
    Dataset so the windowing / mean / dict logic runs offline. Any library failure
    propagates; ``build_record`` maps it to a typed upstream error.
    """
    import s3fs
    import xarray as xr

    from ..._public_s3 import public_endpoint

    # Pin the REAL AWS endpoint (anonymous) and skip the fsspec instance cache: the
    # local build sets AWS_ENDPOINT_URL at MinIO, and a cached MinIO-pointed s3fs
    # instance otherwise hijacks this public-bucket read (empty store -> zarr
    # GroupNotFound). Cloud behaviour is unchanged (there the env var is unset and
    # the pinned endpoint equals the default).
    fs = s3fs.S3FileSystem(
        anon=True,
        client_kwargs={"endpoint_url": public_endpoint(_REGION)},
        skip_instance_cache=True,
    )
    store = s3fs.S3Map(root=f"{_BUCKET}/{year}.zarr", s3=fs, check=False)
    return xr.open_zarr(store, consolidated=True)


def _bbox_subset(da: Any, w: float, s: float, e: float, n: float) -> Any:
    """Select the bbox cells; snap to the nearest single cell when the slice is empty.

    AORC latitude/longitude are ascending, so ``slice(low, high)`` selects the
    intersecting cells. A bbox narrower than the ~0.0083 deg grid that falls
    between cell centres yields an empty slice; fall back to the nearest cell so a
    point-scale AOI still returns a series.
    """
    sub = da.sel(longitude=slice(w, e), latitude=slice(s, n))
    if sub.sizes.get("latitude", 0) == 0 or sub.sizes.get("longitude", 0) == 0:
        sub = da.sel(
            longitude=(w + e) / 2.0, latitude=(s + n) / 2.0, method="nearest"
        ).expand_dims(["latitude", "longitude"])
    return sub


@register_hook("aorc_precip.build_record")
def build_record(
    spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]
) -> dict[str, Any]:
    """Build the AOI-mean hyetograph + accumulation record over the request window.

    Pure-record path: ``bodies`` is empty; the hook opens each spanned AORC year
    store, selects the bbox + time window, averages over space into the hourly
    hyetograph, and sums to the window accumulation. Raises the source's typed
    NOT_AVAILABLE (window outside coverage), EMPTY (no hours in window), or UPSTREAM
    (Zarr/S3 failure) errors -- never a fabricated success.
    """
    import numpy as np

    sc = spec.error_code_prefix
    bbox = params.get("bbox")
    w, s, e, n = (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
    start = _dt.date.fromisoformat(str(params["start_date"]))
    end = _dt.date.fromisoformat(str(params["end_date"]))

    today = _dt.date.today()
    if start < _COVERAGE_START:
        raise router_not_available_error(
            sc,
            f"start_date {start.isoformat()} precedes AORC coverage "
            f"({_COVERAGE_START.isoformat()})",
        )
    # AORC creation runs a ~10-day lag; a window entirely in that tail has no data.
    if start > today - _dt.timedelta(days=10):
        raise router_not_available_error(
            sc,
            f"start_date {start.isoformat()} is within the AORC ~10-day publication "
            "lag; AORC is not real-time (use fetch_mrms_qpe / fetch_hrrr_forecast)",
        )

    # Window is inclusive of the end day's 23:00 hour.
    t0 = f"{start.isoformat()}T00:00:00"
    t1 = f"{end.isoformat()}T23:00:00"

    mean_times: list[str] = []
    mean_vals: list[float] = []
    cell_accum: Any = None  # per-cell running sum (2-D) across the spanned years
    n_cells = 0
    try:
        for year in range(start.year, end.year + 1):
            ds = _open_year(year)
            try:
                da = ds[_PRECIP_VAR]
                sub = _bbox_subset(da, w, s, e, n).sel(time=slice(t0, t1))
                if sub.sizes.get("time", 0) == 0:
                    continue
                n_cells = int(sub.sizes.get("latitude", 1)) * int(
                    sub.sizes.get("longitude", 1)
                )
                # Lazy spatial mean -> 1-D hyetograph; dask streams the chunks.
                series = sub.mean(dim=("latitude", "longitude")).compute()
                times = np.asarray(series["time"].values)
                vals = np.asarray(series.values, dtype="float64")
                for ts, v in zip(times, vals):
                    mean_times.append(np.datetime_as_string(ts, unit="h") + ":00")
                    mean_vals.append(float(v) if np.isfinite(v) else 0.0)
                year_cell_sum = sub.sum(dim="time").compute().values.astype("float64")
                cell_accum = (
                    year_cell_sum
                    if cell_accum is None
                    else cell_accum + year_cell_sum
                )
            finally:
                try:
                    ds.close()
                except Exception:  # noqa: BLE001 -- best-effort store close
                    pass
    except RouterError:
        raise
    except Exception as exc:  # noqa: BLE001 -- any Zarr/S3 failure -> typed upstream
        raise router_upstream_error(
            sc, f"AORC Zarr read failed: {type(exc).__name__}: {exc}"
        )

    if not mean_vals:
        raise router_empty_error(
            sc,
            f"no AORC hours in {start.isoformat()}..{end.isoformat()} over bbox "
            f"{[w, s, e, n]}",
            spec.empty_error_suffix,
        )

    total_mm = float(np.nansum(mean_vals))
    peak_idx = int(np.nanargmax(mean_vals))
    accum = np.asarray(cell_accum, dtype="float64") if cell_accum is not None else None
    return {
        "source": "NOAA AORC v1.1 (Analysis of Record for Calibration)",
        "variable": _PRECIP_VAR,
        "units": "mm",
        "bbox": [w, s, e, n],
        "start": start.isoformat(),
        "end": end.isoformat(),
        "n_hours": len(mean_vals),
        "n_cells": n_cells,
        "times": mean_times,
        "precip_mm": [round(v, 4) for v in mean_vals],
        "total_mm": round(total_mm, 3),
        "peak_mm_per_hr": round(float(mean_vals[peak_idx]), 4),
        "peak_time": mean_times[peak_idx],
        "cell_accumulation_mm": (
            {
                "min": round(float(np.nanmin(accum)), 3),
                "max": round(float(np.nanmax(accum)), 3),
                "mean": round(float(np.nanmean(accum)), 3),
            }
            if accum is not None
            else None
        ),
    }
