"""glm frames hooks: GOES GLM group-energy-density point-gridding.

Folds fetch_glm_lightning onto shape: animation_frames. The DEFAULT output is now
an ORDERED ``list[LayerURI]`` of group-energy-density (GED) frames -- the
single-accumulation case is a ONE-frame list, and ``accumulation_window_s`` fans a
window into N scrubber-steppable frames (STOP dissolved: the frames-list
shape carries the single-vs-list variant for free, the single case is just N=1).

The router owns the per-frame read_through loop + honesty floor + LayerURI emission;
these two hooks own the GLM-specific steps:

- ``frames_plan`` -- normalize the satellite, resolve the window, split it into
  accumulation buckets (ONE bucket in single mode), even-subsample to the frame cap,
  and build the ordered per-frame plans (each with the twin's byte-identical
  cache_params + the ``step <N>`` scrubber name-token + layer_id + bbox). No network:
  each bucket's own S3 listing happens in frame_bytes.
- ``frame_bytes`` -- for ONE bucket window: list the GLM-L2-LCFA granules (anonymous
  NOAA S3), download them, bin GROUP energy onto the ABI-co-registered EPSG:4326 grid
  via numpy.add.at (GLM lat/lon carry parallax -> bin directly, NEVER warp), bake the
  purple log-ramp RGBA, and serialize to COG bytes. Raises :class:`FrameDegraded` for
  a bucket with no granules / no in-AOI groups / an upstream failure (the executor
  records + drops it; the honesty floor raises the typed EMPTY only when EVERY bucket
  degrades -- so a single empty window still surfaces as a hard typed no-data).

The GLM point-gridding math (bin + purple ramp) is bespoke to this source, so it
lives here; the shared ABI grid + RGBA COG writer are reused from
``imagery._goes_archive_core`` so the GED overlay co-registers pixel-for-pixel with
the GOES ABI products. ASCII only.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ...imagery._goes_archive_core import (
    _OUT_RES_DEG,
    _grid_for_bbox,
    _iso_z,
    _parse_utc,
    _rgba_array_to_cog_bytes,
    _round_bbox,
)
from ...imagery._goes_common import (
    GOESInputError,
    _normalize_satellite,
)
from ..errors import router_input_error
from . import FrameDegraded, FramePlan, register_hook

logger = logging.getLogger("trid3nt_server.tools.fetchers._router.hooks.glm")

__all__ = ["frames_plan", "frame_bytes"]


# --------------------------------------------------------------------------- #
# Constants (byte-identical to the twin).
# --------------------------------------------------------------------------- #
#: GLM Level-2 Lightning Cluster-Filter Algorithm product (events/groups/flashes).
_GLM_PRODUCT = "GLM-L2-LCFA"

#: satellite -> public anonymous NOAA S3 bucket.
_GLM_SATELLITE_BUCKETS = {
    "goes-19": "noaa-goes19",  # GOES-East (current operational)
    "goes-18": "noaa-goes18",  # GOES-West (current operational)
    "goes-16": "noaa-goes16",  # GOES-East (historical, pre-2025-04)
    "goes-17": "noaa-goes17",  # GOES-West (historical)
}

#: Default accumulation window when start/end are omitted (minutes ending "now").
_DEFAULT_WINDOW_MIN = 5
#: Single-frame window cap (minutes); longer spans should use accumulation_window_s.
_MAX_SINGLE_WINDOW_MIN = 20
#: Hard safety cap on granules fetched for ONE frame (~20 s/granule -> ~60 min).
_MAX_GLM_GRANULES = 180
#: Minimum accumulation bucket (one ~20 s LCFA granule).
_MIN_ACCUM_S = 20
#: Cap on emitted animation frames (even-subsampled, endpoints kept).
MAX_GLM_FRAMES = 24

#: Style preset -- the per-frame GROUPING key for the web scrubber (identical across
#: frames); the COG is baked RGBA so publish_layer's passthrough ignores it for styling.
_STYLE = {"kind": "continuous"}

_PRODUCT_LABEL = "GLM Lightning GED"
_ID_TAG = "glm-ged"

#: Purple log-ramp tuning (validated against the Florida tropical-cyclone GLM scene).
GED_FJ_CEILING = 500.0  # fJ -> top of the ramp (white/pink head over convective cores)
GED_FJ_FLOOR = 1.0      # fJ -> bottom of the visible ramp (faint violet)

#: GLM/ABI share the ``_s<YYYYDDDHHMMSSf>`` (14-digit) start-time naming convention.
_GLM_KEY_START_RE = re.compile(r"_s(\d{14})")


# --------------------------------------------------------------------------- #
# Local typed signals (bucket-level; frame_bytes maps them to FrameDegraded).
# --------------------------------------------------------------------------- #
class _GLMEmpty(RuntimeError):
    """A bucket window had no granules OR no lightning groups inside the AOI."""


class _GLMUpstream(RuntimeError):
    """An S3 listing / granule download / read failure for a bucket."""


# --------------------------------------------------------------------------- #
# GLM S3 access (anonymous / public NOAA archive).
# --------------------------------------------------------------------------- #
def _glm_s3_client() -> Any:
    """Anonymous (UNSIGNED) boto3 S3 client for the public ``noaa-goesNN`` buckets."""
    from ..._public_s3 import public_s3_client

    return public_s3_client("us-east-1")


def _glm_key_start_datetime(key: str) -> datetime | None:
    """Parse the ``_s<YYYYDDDHHMMSSf>`` granule start-time -> aware UTC (or None)."""
    m = _GLM_KEY_START_RE.search(key)
    if not m:
        return None
    s = m.group(1)  # 14 digits: YYYYDDDHHMMSSf
    try:
        year = int(s[0:4])
        doy = int(s[4:7])
        hour = int(s[7:9])
        minute = int(s[9:11])
        second = int(s[11:13])
        base = datetime(year, 1, 1, tzinfo=timezone.utc) + timedelta(days=doy - 1)
        return base.replace(hour=hour, minute=minute, second=second)
    except (ValueError, IndexError, OverflowError):
        return None


def _glm_hour_prefixes(start_dt: datetime, end_dt: datetime) -> list[str]:
    """Hour-bucket S3 prefixes ``GLM-L2-LCFA/YYYY/DOY/HH/`` covering [start, end)."""
    prefixes: list[str] = []
    t = start_dt.replace(minute=0, second=0, microsecond=0)
    while t < end_dt:
        doy = t.timetuple().tm_yday
        prefixes.append(f"{_GLM_PRODUCT}/{t.year}/{doy:03d}/{t.hour:02d}/")
        t = t + timedelta(hours=1)
    if not prefixes:  # degenerate sub-hour window -- list the start hour
        doy = start_dt.timetuple().tm_yday
        prefixes.append(f"{_GLM_PRODUCT}/{start_dt.year}/{doy:03d}/{start_dt.hour:02d}/")
    return prefixes


def _list_glm_keys_in_window(
    satellite: str, start_dt: datetime, end_dt: datetime
) -> list[tuple[datetime, str]]:
    """List ``GLM-L2-LCFA`` granules whose start-time falls in [start, end), ascending."""
    bucket = _GLM_SATELLITE_BUCKETS[satellite]
    s3 = _glm_s3_client()
    out: list[tuple[datetime, str]] = []
    for prefix in _glm_hour_prefixes(start_dt, end_dt):
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": 1000}
            if token:
                kwargs["ContinuationToken"] = token
            try:
                resp = s3.list_objects_v2(**kwargs)
            except Exception as exc:  # noqa: BLE001 -- upstream S3 listing failure
                raise _GLMUpstream(f"GLM listing failed for s3://{bucket}/{prefix}: {exc}") from exc
            for obj in resp.get("Contents", []):
                key = obj["Key"]
                t = _glm_key_start_datetime(key)
                if t is not None and start_dt <= t < end_dt:
                    out.append((t, key))
            if resp.get("IsTruncated"):
                token = resp.get("NextContinuationToken")
            else:
                break
    out.sort(key=lambda p: p[0])
    return out


def _fetch_glm_groups(satellite: str, keys: list[str]) -> tuple[Any, Any, Any]:
    """Download granules -> concatenated finite (group_lat, group_lon, group_energy)."""
    import os
    import tempfile

    import netCDF4  # type: ignore[import-untyped]
    import numpy as np

    bucket = _GLM_SATELLITE_BUCKETS[satellite]
    s3 = _glm_s3_client()
    lats: list[Any] = []
    lons: list[Any] = []
    engs: list[Any] = []
    with tempfile.TemporaryDirectory(prefix="trid3nt_glm_") as td:
        for key in keys:
            dst = os.path.join(td, key.split("/")[-1])
            try:
                s3.download_file(bucket, key, dst)
            except Exception as exc:  # noqa: BLE001 -- upstream download failure
                raise _GLMUpstream(f"GLM granule download failed for s3://{bucket}/{key}: {exc}") from exc
            try:
                with netCDF4.Dataset(dst) as ds:
                    lats.append(np.asarray(ds.variables["group_lat"][:], dtype=np.float64))
                    lons.append(np.asarray(ds.variables["group_lon"][:], dtype=np.float64))
                    engs.append(np.asarray(ds.variables["group_energy"][:], dtype=np.float64))
            except Exception as exc:  # noqa: BLE001 -- corrupt/unreadable granule
                raise _GLMUpstream(f"GLM granule read failed for {key}: {exc}") from exc
    if not lats:
        empty = np.array([], dtype=np.float64)
        return empty, empty, empty
    lat = np.concatenate(lats)
    lon = np.concatenate(lons)
    eng = np.concatenate(engs)
    finite = np.isfinite(lat) & np.isfinite(lon) & np.isfinite(eng)
    return lat[finite], lon[finite], eng[finite]


def _bin_ged(
    lat: Any, lon: Any, eng: Any, bbox: tuple[float, float, float, float],
    width: int, height: int,
) -> tuple[Any, int]:
    """Bin GROUP energy (J) onto the EPSG:4326 grid via ``numpy.add.at``.

    GLM lat/lon are POINTS carrying parallax; bin them DIRECTLY (never warp). Row 0
    is the TOP (max_lat) because ``_grid_for_bbox`` builds a north-up transform.
    Returns ``(ged_joules (H,W) float64, n_groups_inside_bbox)``.
    """
    import numpy as np

    min_lon, min_lat, max_lon, max_lat = bbox
    inb = (lon >= min_lon) & (lon < max_lon) & (lat >= min_lat) & (lat < max_lat)
    lon_i, lat_i, eng_i = lon[inb], lat[inb], eng[inb]

    col = ((lon_i - min_lon) / _OUT_RES_DEG).astype(np.int64)
    row = ((max_lat - lat_i) / _OUT_RES_DEG).astype(np.int64)
    np.clip(col, 0, width - 1, out=col)
    np.clip(row, 0, height - 1, out=row)

    ged_j = np.zeros((height, width), dtype=np.float64)
    np.add.at(ged_j, (row, col), eng_i)
    return ged_j, int(inb.sum())


def _purple_ramp(t: Any) -> tuple[Any, Any, Any]:
    """t in [0,1] -> (r,g,b) on a deep-violet -> magenta -> white-pink ramp."""
    import numpy as np

    t = np.clip(t, 0.0, 1.0)
    r = 60 + t * (255 - 60)
    g = 0 + np.clip((t - 0.45) / 0.55, 0, 1) * 235  # green joins only near the top -> white
    b = 130 + t * (255 - 130)
    return r, g, b


def _ged_to_purple_rgba(ged_j: Any) -> Any:
    """GED (J) -> (4, H, W) uint8 RGBA; zeros are fully transparent (alpha 0)."""
    import numpy as np

    ged_fj = ged_j * 1e15  # Joules -> femtojoules
    lit = ged_fj > 0
    lo, hi = np.log10(GED_FJ_FLOOR), np.log10(GED_FJ_CEILING)
    with np.errstate(divide="ignore"):
        logv = np.log10(np.maximum(ged_fj, 1e-6))
    t = (logv - lo) / (hi - lo)
    r, g, b = _purple_ramp(t)

    z = np.zeros_like(ged_fj)
    red = np.where(lit, r, z)
    grn = np.where(lit, g, z)
    blu = np.where(lit, b, z)
    alpha = np.where(lit, np.clip(120 + t * 135, 120, 255), 0.0)
    rgba = np.stack([red, grn, blu, alpha], axis=0)
    return np.clip(np.rint(rgba), 0, 255).astype(np.uint8)


def _even_subsample(items: list[Any], cap: int) -> list[Any]:
    """Even-subsample a list down to ``cap`` (endpoints kept). Pure."""
    import numpy as np

    if len(items) <= cap:
        return items
    idx = np.unique(np.rint(np.linspace(0, len(items) - 1, cap)).astype(int))
    return [items[int(i)] for i in idx]


def _fetch_glm_ged_cog_bytes(
    satellite: str,
    bbox: tuple[float, float, float, float],
    start_dt: datetime,
    end_dt: datetime,
) -> bytes:
    """List + download GLM granules in [start, end), bin GED, bake purple RGBA -> COG bytes.

    Raises ``_GLMEmpty`` when the window has no granules OR no lightning groups inside
    the AOI (the per-bucket honesty floor -- never a blank overlay).
    """
    keys_times = _list_glm_keys_in_window(satellite, start_dt, end_dt)
    if not keys_times:
        raise _GLMEmpty(
            f"no {_GLM_PRODUCT} granules in {_GLM_SATELLITE_BUCKETS[satellite]} for "
            f"window {_iso_z(start_dt)}..{_iso_z(end_dt)} -- the date may pre-date the "
            f"{satellite} GLM record or fall in an ingest gap"
        )
    keys = [k for _, k in keys_times]
    if len(keys) > _MAX_GLM_GRANULES:
        logger.warning(
            "glm: window %s..%s has %d granules; capping at %d",
            _iso_z(start_dt), _iso_z(end_dt), len(keys), _MAX_GLM_GRANULES,
        )
        keys = keys[:_MAX_GLM_GRANULES]

    lat, lon, eng = _fetch_glm_groups(satellite, keys)
    transform, width, height = _grid_for_bbox(bbox)  # 0.02 deg ~2 km, ABI-co-registered
    ged_j, n_in = _bin_ged(lat, lon, eng, bbox, width, height)
    if n_in == 0:
        raise _GLMEmpty(
            f"no GLM lightning groups detected inside the AOI for window "
            f"{_iso_z(start_dt)}..{_iso_z(end_dt)} ({int(lat.size)} groups full-disk, "
            f"0 inside bbox) -- the storm may be outside this AOI or electrically quiet"
        )
    rgba = _ged_to_purple_rgba(ged_j)
    logger.info(
        "glm: %s %s..%s -> %d granules, %d groups in AOI, %d lit cells on %dx%d grid",
        satellite, _iso_z(start_dt), _iso_z(end_dt), len(keys), n_in,
        int((ged_j > 0).sum()), height, width,
    )
    return _rgba_array_to_cog_bytes(rgba, transform, width, height)


# --------------------------------------------------------------------------- #
# frames-plan helpers.
# --------------------------------------------------------------------------- #
def _resolve_satellite(spec: SourceSpec, satellite: Any) -> str:
    """Normalize the GOES spelling zoo -> canonical bird, then gate to the GLM set.

    Re-wraps the shared normalizer's GOESInputError as the source's typed
    ``*_INPUT_INVALID`` (byte-identical to the twin's GLMInputError re-wrap: the
    base GOES error type never leaks out of the GLM surface).
    """
    sc, sfx = spec.error_code_prefix, spec.input_error_suffix
    try:
        sat = _normalize_satellite(str(satellite))
    except GOESInputError as exc:
        raise router_input_error(sc, str(exc), sfx) from exc
    if sat not in _GLM_SATELLITE_BUCKETS:
        raise router_input_error(
            sc, f"unknown satellite={sat!r}; allowed: {list(_GLM_SATELLITE_BUCKETS)}", sfx
        )
    return sat


def _resolve_window(spec: SourceSpec, params: dict[str, Any]) -> tuple[datetime, datetime]:
    """Resolve [start, end): defaults to the most-recent ~5 min ending now."""
    sc, sfx = spec.error_code_prefix, spec.input_error_suffix
    now = datetime.now(timezone.utc)
    end_raw = params.get("end_utc")
    start_raw = params.get("start_utc")
    end_dt = _parse_utc(end_raw) if end_raw else now
    start_dt = (
        _parse_utc(start_raw) if start_raw
        else end_dt - timedelta(minutes=_DEFAULT_WINDOW_MIN)
    )
    if start_dt >= end_dt:
        raise router_input_error(
            sc,
            f"start_utc ({_iso_z(start_dt)}) must be before end_utc ({_iso_z(end_dt)})",
            sfx,
        )
    return start_dt, end_dt


# --------------------------------------------------------------------------- #
# frames_plan: the pre-loop resolve (single mode -> ONE frame).
# --------------------------------------------------------------------------- #
@register_hook("glm.frames_plan")
def frames_plan(spec: SourceSpec, params: dict[str, Any]) -> list[FramePlan]:
    """Split the window into accumulation buckets -> ordered per-frame plans.

    Single mode (``accumulation_window_s`` unset) yields ONE bucket (the whole
    window) -> a one-frame list, the new default contract. No network.
    """
    sc, sfx = spec.error_code_prefix, spec.input_error_suffix
    q_bbox = _round_bbox(params["bbox"])
    satellite = _resolve_satellite(spec, params.get("satellite", "goes-19"))
    start_dt, end_dt = _resolve_window(spec, params)
    sat_label = satellite.upper()

    acc = params.get("accumulation_window_s")
    if acc is None:
        # Single accumulated frame (default) -> a ONE-frame list.
        if (end_dt - start_dt) > timedelta(minutes=_MAX_SINGLE_WINDOW_MIN):
            raise router_input_error(
                sc,
                f"single-frame window {_iso_z(start_dt)}..{_iso_z(end_dt)} exceeds "
                f"{_MAX_SINGLE_WINDOW_MIN} min; shorten it, or set accumulation_window_s "
                "to fan the span into a multi-frame animation",
                sfx,
            )
        buckets = [(start_dt, end_dt)]
    else:
        acc_s = int(acc)
        if acc_s < _MIN_ACCUM_S:
            raise router_input_error(
                sc,
                f"accumulation_window_s must be >= {_MIN_ACCUM_S} s (one LCFA granule); "
                f"got {acc_s}",
                sfx,
            )
        buckets = []
        t = start_dt
        while t < end_dt:
            b_end = min(t + timedelta(seconds=acc_s), end_dt)
            buckets.append((t, b_end))
            t = b_end
        if len(buckets) > MAX_GLM_FRAMES:
            logger.info(
                "glm: %d accumulation buckets -> even-subsampling to %d frames",
                len(buckets), MAX_GLM_FRAMES,
            )
            buckets = _even_subsample(buckets, MAX_GLM_FRAMES)

    plans: list[FramePlan] = []
    for frame_no, (b_start, b_end) in enumerate(buckets, start=1):
        iso = _iso_z(b_start)
        ts_tag = b_start.strftime("%Y%m%d%H%M%S")
        plans.append(
            FramePlan(
                cache_params={
                    "bbox": list(q_bbox),
                    "satellite": satellite,
                    "product": "glm_ged",
                    "start_utc": _iso_z(b_start),
                    "end_utc": _iso_z(b_end),
                    "ramp_fj": [GED_FJ_FLOOR, GED_FJ_CEILING],
                    "res_deg": _OUT_RES_DEG,
                    "tool": "fetch_glm_lightning",
                },
                name=f"{_PRODUCT_LABEL} step {frame_no} {iso} ({sat_label})",
                layer_id=(
                    f"{_ID_TAG}-{satellite}-{ts_tag}-{q_bbox[0]:.3f}-{q_bbox[1]:.3f}"
                ),
                bbox=q_bbox,
                fetch_context={"satellite": satellite, "start_utc": b_start, "end_utc": b_end},
            )
        )
    return plans


# --------------------------------------------------------------------------- #
# frame_bytes: the per-bucket COG builder.
# --------------------------------------------------------------------------- #
@register_hook("glm.frame_bytes")
def frame_bytes(spec: SourceSpec, params: dict[str, Any], frame: FramePlan) -> bytes:
    """Build ONE bucket's GED COG bytes; a no-lightning / failed bucket -> FrameDegraded.

    The executor records + drops a degraded frame; the honesty floor raises the typed
    EMPTY only when EVERY bucket degrades -- so a single empty window (one-frame list)
    still surfaces as a hard typed no-data, byte-for-byte the twin's per-bucket skip.
    """
    ctx = frame.fetch_context
    bbox = tuple(frame.cache_params["bbox"])  # type: ignore[assignment]
    try:
        return _fetch_glm_ged_cog_bytes(
            ctx["satellite"], bbox, ctx["start_utc"], ctx["end_utc"]
        )
    except (_GLMEmpty, _GLMUpstream) as exc:
        raise FrameDegraded(str(exc)) from exc
