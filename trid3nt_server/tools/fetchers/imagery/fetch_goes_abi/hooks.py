"""fetch_goes_abi hooks: the band-selected raw ABI archive, one COG per step.

``frames_plan`` lists the L2 product's in-window objects on the anonymous public
bucket and keeps the archived scan nearest each step instant; ``frame_bytes``
downloads ONE netCDF and writes the requested bands onto one grid, each band
carrying its number, its wavelength and its unit in the band description and the
layer carrying the instant and the subpoint the scan was observed from."""

# The opaque per-scan object key rides in the out-of-cache-key ``fetch_context``: a
# frame here is addressed by that key rather than by its timestamp.

from __future__ import annotations

import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..._router.errors import (
    router_empty_error,
    router_input_error,
    router_upstream_error,
)
from ..._router.hooks import (
    FrameDegraded,
    FramePlan,
    frame_windows,
    register_hook,
)
from .. import _goes_archive_core as core
from .._goes_common import (
    GOESError,
    _SATELLITE_BUCKETS,
    _download_to_tempfile,
)

logger = logging.getLogger(
    "trid3nt_server.tools.fetchers.imagery.fetch_goes_abi.hooks"
)

__all__ = [
    "frames_plan", "frame_bytes", "band_description", "band_units",
    "observation_tags",
]

#: Every ABI band's central wavelength in micrometres. The number is what the caller
#: asks for and the wavelength is what makes the band mean something, so both ride in
#: the band description the derives read back.
_BAND_MICRONS: dict[int, float] = {
    1: 0.47, 2: 0.64, 3: 0.86, 4: 1.37, 5: 1.6, 6: 2.2, 7: 3.9, 8: 6.2,
    9: 6.9, 10: 7.3, 11: 8.4, 12: 9.6, 13: 10.3, 14: 11.2, 15: 12.3, 16: 13.3,
}

#: ABI bands 1-6 are reflective and their CMI variable is a reflectance FACTOR, which
#: is dimensionless; 7-16 are emissive and theirs is a brightness temperature in
#: kelvin. One file mixes the two, so the unit belongs to the band, never to the layer.
_REFLECTIVE_MAX_BAND = 6

#: The MCMIPC product resamples all sixteen channels onto ONE 2 km grid, so every
#: band lands co-registered at the same cell size and a finer output grid would only
#: upsample.
_OUT_RES_DEG = core._OUT_RES_DEG


def band_description(band: int) -> str:
    """The band's description line: its ABI number, its wavelength and its quantity.
    A derive over an ABI layer finds the band it needs by the leading number here."""
    micron = _BAND_MICRONS[band]
    quantity = (
        "reflectance factor" if band <= _REFLECTIVE_MAX_BAND
        else "brightness temperature"
    )
    return f"ABI band {band} {micron:g} um {quantity}"


def band_units(band: int) -> str:
    """The band's unit: ``1`` for a dimensionless reflectance factor, ``K`` for a
    brightness temperature."""
    return "1" if band <= _REFLECTIVE_MAX_BAND else "K"


def _bands(spec: SourceSpec, params: dict[str, Any]) -> tuple[int, ...]:
    """The requested ABI band numbers, ascending. The router has already gated the
    values and sorted them; this only lands them back on the integers they are."""
    raw = params.get("bands") or []
    bands = tuple(int(round(float(b))) for b in raw)
    if not bands:
        raise router_input_error(
            spec.error_code_prefix,
            "bands is required: name the ABI band numbers to return, e.g. [7, 14] "
            "for the shortwave and longwave windows or [1, 2, 3] for the visible.",
            spec.input_error_suffix,
        )
    return bands


def _window(
    spec: SourceSpec, params: dict[str, Any], default_window_min: int
) -> tuple[datetime, datetime]:
    """The requested UTC window, defaulting to the most recent
    ``default_window_min`` minutes when the caller states neither bound."""
    now = datetime.now(timezone.utc)
    end_raw = params.get("end_utc")
    start_raw = params.get("start_utc")
    try:
        end_dt = core._parse_utc(end_raw) if end_raw else now
        start_dt = (
            core._parse_utc(start_raw) if start_raw
            else end_dt - timedelta(minutes=default_window_min)
        )
    except core.GOESArchiveInputError as exc:
        raise router_input_error(
            spec.error_code_prefix, str(exc), spec.input_error_suffix) from exc
    if start_dt >= end_dt:
        raise router_input_error(
            spec.error_code_prefix,
            f"start_utc ({core._iso_z(start_dt)}) must be before end_utc "
            f"({core._iso_z(end_dt)})",
            spec.input_error_suffix,
        )
    return start_dt, end_dt


def _at_step(
    pairs: list[tuple[datetime, str]],
    start_dt: datetime,
    step_minutes: int,
) -> list[tuple[datetime, str]]:
    """The archived scan NEAREST each step instant, ascending.

    The archive's own cadence is fixed, so a step is a request for a spacing rather
    than for instants that exist: each scan falls in the step slot it is closest to
    and the closest scan in a slot wins it, so a step finer than the cadence
    collapses back onto the native scans."""
    step = timedelta(minutes=max(1, int(step_minutes)))
    kept: dict[int, tuple[datetime, str]] = {}
    for moment, key in pairs:
        slot = int(round((moment - start_dt) / step))
        target = start_dt + slot * step
        held = kept.get(slot)
        if held is None or abs(moment - target) < abs(held[0] - target):
            kept[slot] = (moment, key)
    return [kept[slot] for slot in sorted(kept)]


@register_hook("goes_abi.frames_plan")
def frames_plan(spec: SourceSpec, params: dict[str, Any]) -> list[FramePlan]:
    """List the in-window archive objects and keep one per step, as ordered plans."""
    sc = spec.error_code_prefix
    cfg = (spec.ingest or {}).get("abi", {})
    max_frames = int(cfg.get("max_frames", core.MAX_ARCHIVE_FRAMES))
    default_window_min = int(cfg.get("default_window_minutes", 60))

    bbox = tuple(round(float(v), core._BBOX_QUANTIZE_DP) for v in params["bbox"])
    satellite = str(params.get("satellite", "goes-18"))
    bands = _bands(spec, params)
    start_dt, end_dt = _window(spec, params, default_window_min)

    try:
        pairs = core._list_archive_keys_in_window(satellite, start_dt, end_dt)
    except core.GOESArchiveInputError as exc:
        raise router_input_error(sc, str(exc), spec.input_error_suffix) from exc
    except core.GOESArchiveUpstreamError as exc:
        raise router_upstream_error(sc, str(exc)) from exc
    if not pairs:
        raise router_empty_error(
            sc,
            f"no {core._PRODUCT_PREFIX} objects in the {satellite} archive for window "
            f"{core._iso_z(start_dt)}..{core._iso_z(end_dt)} -- the window may pre-date "
            "the operational record of that bird or fall in an ingest gap",
            spec.empty_error_suffix,
        )

    frames = _at_step(pairs, start_dt, int(params.get("step_minutes", 10)))
    keys_kept = set(core._select_window_keys([k for _t, k in frames], cap=max_frames))
    frames = [(t, k) for t, k in frames if k in keys_kept]

    band_tag = "-".join(str(b) for b in bands)
    sat_label = satellite.upper()
    windows = frame_windows([core._iso_z(t) for t, _k in frames])
    plans: list[FramePlan] = []
    for frame_no, (t, key) in enumerate(frames, start=1):
        iso = core._iso_z(t)
        ts_tag = t.strftime("%Y%m%d%H%M%S")
        valid_from, valid_to = windows[frame_no - 1]
        plans.append(
            FramePlan(
                cache_params={
                    "bbox": list(bbox),
                    "satellite": satellite,
                    "bands": list(bands),
                    "ts_start": ts_tag,
                    "res_deg": round(_OUT_RES_DEG, 6),
                },
                name=(
                    f"GOES ABI bands {','.join(str(b) for b in bands)} "
                    f"step {frame_no} {iso} ({sat_label})"
                ),
                layer_id=f"goes-abi-{band_tag}-{ts_tag}-{bbox[0]:.3f}-{bbox[1]:.3f}",
                bbox=bbox,
                valid_from=valid_from,
                valid_to=valid_to,
                fetch_context={"key": key, "bands": bands},
            )
        )
    return plans


def observation_tags(nc_path: str) -> dict[str, str]:
    """The scan's instant and the satellite's subpoint longitude, off the netCDF.

    The file states no solar or view angle of its own, so a derive whose physics
    depends on the observation geometry computes the angles from these two."""
    import netCDF4  # type: ignore[import-not-found]

    with netCDF4.Dataset(nc_path) as ncds:
        return {
            "scan_time_utc": str(ncds.time_coverage_start),
            "satellite_subpoint_lon": str(
                float(ncds.variables["nominal_satellite_subpoint_lon"][...])),
        }


def _bands_to_cog_bytes(
    arrays: list[Any],
    bands: tuple[int, ...],
    out_transform: Any,
    width: int,
    height: int,
    tags: dict[str, str],
) -> bytes:
    """Write the per-band physical arrays to a multi-band float32 COG, each band
    described and tagged with its own unit, NaN carried as the nodata value, and the
    observation tags on the dataset."""
    import numpy as np
    import rasterio

    stack = np.stack([np.asarray(a, dtype=np.float32) for a in arrays], axis=0)
    out_fd, out_path = tempfile.mkstemp(suffix=".tif", prefix="trid3nt_abi_cog_")
    os.close(out_fd)
    try:
        profile = {
            "driver": "COG",
            "dtype": "float32",
            "count": len(bands),
            "height": height,
            "width": width,
            "crs": "EPSG:4326",
            "transform": out_transform,
            "compress": "DEFLATE",
            "nodata": float("nan"),
        }
        try:
            dst = rasterio.open(out_path, "w", **profile)
        except Exception as exc:  # noqa: BLE001 -- the COG driver may be unavailable
            logger.warning("fetch_goes_abi: COG write failed (%s); using GTiff", exc)
            profile["driver"] = "GTiff"
            profile["tiled"] = True
            dst = rasterio.open(out_path, "w", **profile)
        with dst:
            dst.write(stack)
            dst.update_tags(**tags)
            for i, band in enumerate(bands, start=1):
                dst.set_band_description(i, band_description(band))
                dst.update_tags(i, units=band_units(band), abi_band=str(band))
        with open(out_path, "rb") as f:
            return f.read()
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


@register_hook("goes_abi.frame_bytes")
def frame_bytes(spec: SourceSpec, params: dict[str, Any], frame: FramePlan) -> bytes:
    """Download ONE archive object and write the requested bands to COG bytes.
    Raises :class:`FrameDegraded` for an off-sector or upstream-failed scan, so the
    executor records and drops that one instead of failing the sequence."""
    import numpy as np

    bands: tuple[int, ...] = tuple(frame.fetch_context["bands"])
    bbox = tuple(frame.cache_params["bbox"])
    bucket = _SATELLITE_BUCKETS[frame.cache_params["satellite"]]
    url = f"https://{bucket}.s3.amazonaws.com/{frame.fetch_context['key']}"
    nc_path: str | None = None
    try:
        nc_path = _download_to_tempfile(url)
        out_transform, width, height = core._grid_for_bbox(bbox, _OUT_RES_DEG)
        arrays = [
            core._warp_band_to_physical(
                nc_path, f"CMI_C{band:02d}", out_transform, width, height)
            for band in bands
        ]
        if not any(np.isfinite(a).any() for a in arrays):
            raise FrameDegraded(
                f"bbox={bbox} produces no valid pixels in bands {list(bands)} "
                "(outside the scanned sector or behind the disk limb)")
        return _bands_to_cog_bytes(
            arrays, bands, out_transform, width, height, observation_tags(nc_path))
    except (core.GOESArchiveError, GOESError) as exc:
        raise FrameDegraded(str(exc)) from exc
    finally:
        if nc_path is not None:
            try:
                os.unlink(nc_path)
            except OSError:
                pass
