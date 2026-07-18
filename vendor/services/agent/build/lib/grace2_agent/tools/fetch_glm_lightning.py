"""``fetch_glm_lightning`` -- GOES GLM optical-lightning group-energy-density fetcher.

A peer of the GOES ABI fetchers (``fetch_goes_satellite`` /
``fetch_goes_archive_animation`` / ``fetch_goes_active_fire``). Where those read the
ABI imager, this reads the **GLM** (Geostationary Lightning Mapper) ``GLM-L2-LCFA``
product from the public ``noaa-goesNN`` S3 archive (anonymous / no key), bins the
optical-lightning GROUP energy onto the SAME ~2 km EPSG:4326 grid the ABI products
use, and returns the **group-energy-density (GED)** as a TRANSPARENT purple RGBA
raster ``LayerURI`` -- only cells with detected lightning are opaque, everything
else is transparent so it overlays a basemap / true-color frame directly.

Why GED rather than raw counts: GLM reports discrete optical ``group`` detections,
each carrying a ``group_energy`` (Joules). Summing that energy per grid cell over a
short accumulation window yields a stable, physically meaningful "how electrically
active is this cell" field that reads like the CIRA GLM density imagery -- bright
violet/white over the convective cores, faint violet at the edges.

Grid co-registration: GED bins onto ``_grid_for_bbox`` at ``_OUT_RES_DEG`` (0.02 deg
~2 km) -- the IDENTICAL grid the ABI fire / true-color products land on -- so a GLM
overlay sits pixel-aligned over a GOES ABI base with no extra resample.

Rendering: the emitted COG is a 4-band RGBA (band 4 = alpha) with the purple
log-ramp BAKED in, so publish_layer's RGBA/multiband passthrough renders the colors
+ transparency directly (no colormap, no TiTiler autoscale, no new style preset
needed) -- mirrors ``fetch_goes_active_fire``'s transparent hotspot overlay.

Honesty floor: a window with no GLM granules OR no lightning groups inside the AOI
raises a typed error -- it never emits a blank/fabricated overlay.

ASCII only.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from grace2_contracts.execution import LayerURI
from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import read_through
from .fetch_goes_archive_animation import (
    _OUT_RES_DEG,
    _grid_for_bbox,
    _iso_z,
    _parse_utc,
    _rgba_array_to_cog_bytes,
    _round_bbox,
)
# Shared satellite-identifier normalizer (the single canonicalization seam in
# fetch_goes_satellite, the import-acyclic base of the GOES fetcher family). It
# maps every human/LLM spelling -- "GOES-18", "goes18", "G18", "GOES West", a
# bare "18" -- onto the canonical hyphenated "goes-NN" token that keys
# _GLM_SATELLITE_BUCKETS, or raises LOUD on a truly-unknown bird. We canonicalize
# the input through it BEFORE the allow-list check, then keep this tool's own
# membership check + GLMInputError for the valid-but-unsupported case.
from .fetch_goes_satellite import GOESInputError, _normalize_satellite

__all__ = [
    "fetch_glm_lightning",
    "estimate_payload_mb",
    "MAX_GLM_FRAMES",
]

logger = logging.getLogger("grace2_agent.tools.fetch_glm_lightning")

# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------
#: GLM Level-2 Lightning Cluster-Filter Algorithm product (events/groups/flashes).
_GLM_PRODUCT = "GLM-L2-LCFA"

#: satellite -> public anonymous NOAA S3 bucket. GOES-East default (GOES-19, the
#: operational East bird since 2025-04); GOES-18 is West; goes-16/17 historical.
_GLM_SATELLITE_BUCKETS = {
    "goes-19": "noaa-goes19",  # GOES-East (current operational)
    "goes-18": "noaa-goes18",  # GOES-West (current operational)
    "goes-16": "noaa-goes16",  # GOES-East (historical, pre-2025-04)
    "goes-17": "noaa-goes17",  # GOES-West (historical)
}

#: Default accumulation window when start/end are omitted (minutes ending "now").
_DEFAULT_WINDOW_MIN = 5
#: Single-frame window cap (minutes) -- bounds the granule download; longer spans
#: should use ``accumulation_window_s`` to fan out into an animation instead of one
#: giant accumulation (which would also blur the time evolution into a single image).
_MAX_SINGLE_WINDOW_MIN = 20
#: Hard safety cap on granules fetched for ONE frame (~20 s/granule -> ~60 min).
_MAX_GLM_GRANULES = 180
#: Minimum accumulation bucket (one ~20 s LCFA granule).
_MIN_ACCUM_S = 20
#: Cap on emitted animation frames (even-subsampled, endpoints kept).
MAX_GLM_FRAMES = 24

#: Style preset string. The COG is baked RGBA so publish_layer's RGBA passthrough
#: ignores it for styling; it is the per-frame GROUPING key for the web scrubber
#: (must be identical across animation frames) and tokenizes to nothing terrain-y.
_STYLE_PRESET = "glm_lightning"

_PRODUCT_LABEL = "GLM Lightning GED"
_ID_TAG = "glm-ged"

# Purple log-ramp tuning (validated in the local-first prototype against the
# Florida tropical-cyclone GLM scene): a fixed femtojoule ceiling keeps the ramp
# stable frame-to-frame (no flicker); the floor keeps faint cells faint.
GED_FJ_CEILING = 500.0  # fJ -> top of the ramp (white/pink head over convective cores)
GED_FJ_FLOOR = 1.0      # fJ -> bottom of the visible ramp (faint violet)

#: GLM/ABI share the ``_s<YYYYDDDHHMMSSf>`` (14-digit) start-time naming convention.
_GLM_KEY_START_RE = re.compile(r"_s(\d{14})")


# ---------------------------------------------------------------------------
# Typed errors (error_code + retryable -> WS A.6 error frame + FR-AS-11 retry).
# ---------------------------------------------------------------------------
class GLMError(RuntimeError):
    """Base GLM lightning fetcher error."""

    error_code: str = "GLM_ERROR"
    retryable: bool = True


class GLMBboxRequiredError(GLMError):
    error_code = "BBOX_REQUIRED"
    retryable = False


class GLMInputError(GLMError):
    error_code = "GLM_INPUT_INVALID"
    retryable = False


class GLMUpstreamError(GLMError):
    error_code = "GLM_UPSTREAM_ERROR"
    retryable = True


class GLMEmptyError(GLMError):
    error_code = "GLM_EMPTY"
    retryable = False


# ---------------------------------------------------------------------------
# Metadata + payload estimator.
# ---------------------------------------------------------------------------
_METADATA = AtomicToolMetadata(
    name="fetch_glm_lightning",
    ttl_class="dynamic-1h",
    source_class="goes_glm",
    cacheable=True,
    supports_global_query=False,
    payload_mb_estimator_name="estimate_payload_mb",
)


def estimate_payload_mb(
    bbox: tuple[float, float, float, float] | None = None,
    **_kw: Any,
) -> float:
    """Estimate emitted RGBA GED COG size in MB (scales with bbox area).

    A 4-band uint8 COG at 0.02 deg is ~0.04 MB/deg^2 raw, but the overlay is mostly
    transparent zeros (DEFLATE-compressed to a fraction). Keep a small, generous
    floor; GED overlays never approach the 25 MB chat-warn threshold.
    """
    if bbox is None:
        return 2.0
    try:
        west, south, east, north = bbox
        sq_deg = max(0.0, (east - west)) * max(0.0, (north - south))
    except (TypeError, ValueError):
        return 2.0
    return max(0.5, sq_deg * 0.02)


# ---------------------------------------------------------------------------
# Input validation (mirrors fetch_goes_satellite: own typed errors, bbox-first).
# ---------------------------------------------------------------------------
def _validate_glm_bbox(
    bbox: tuple[float, float, float, float] | None,
) -> tuple[float, float, float, float]:
    """Validate + normalize the bbox. Raises a typed error BEFORE any network call."""
    if bbox is None:
        raise GLMBboxRequiredError(
            "bbox is required for fetch_glm_lightning -- pass "
            "(min_lon, min_lat, max_lon, max_lat) in EPSG:4326; GLM full-disk "
            "downloads are far too large to bin without an AOI."
        )
    try:
        vals = [float(v) for v in bbox]
    except (TypeError, ValueError) as exc:
        raise GLMInputError(
            f"bbox must be 4 numbers (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        ) from exc
    if len(vals) != 4:
        raise GLMInputError(f"bbox must have exactly 4 values; got {len(vals)}: {vals}")
    min_lon, min_lat, max_lon, max_lat = vals
    if not (min_lon < max_lon and min_lat < max_lat):
        raise GLMInputError(
            f"bbox must satisfy min_lon<max_lon and min_lat<max_lat; got {vals}"
        )
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0
            and -90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise GLMInputError(f"bbox out of EPSG:4326 geographic range: {vals}")
    return (min_lon, min_lat, max_lon, max_lat)


# ---------------------------------------------------------------------------
# GLM S3 access (anonymous / public NOAA archive).
# ---------------------------------------------------------------------------
def _glm_s3_client() -> Any:
    """Anonymous (UNSIGNED) boto3 S3 client for the public ``noaa-goesNN`` buckets."""
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config

    return boto3.client(
        "s3", region_name="us-east-1", config=Config(signature_version=UNSIGNED)
    )


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
        prefixes.append(
            f"{_GLM_PRODUCT}/{start_dt.year}/{doy:03d}/{start_dt.hour:02d}/"
        )
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
                raise GLMUpstreamError(
                    f"GLM listing failed for s3://{bucket}/{prefix}: {exc}"
                ) from exc
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
    import netCDF4  # type: ignore[import-untyped]
    import numpy as np
    import os
    import tempfile

    bucket = _GLM_SATELLITE_BUCKETS[satellite]
    s3 = _glm_s3_client()
    lats: list[Any] = []
    lons: list[Any] = []
    engs: list[Any] = []
    with tempfile.TemporaryDirectory(prefix="grace2_glm_") as td:
        for key in keys:
            dst = os.path.join(td, key.split("/")[-1])
            try:
                s3.download_file(bucket, key, dst)
            except Exception as exc:  # noqa: BLE001 -- upstream download failure
                raise GLMUpstreamError(
                    f"GLM granule download failed for s3://{bucket}/{key}: {exc}"
                ) from exc
            try:
                with netCDF4.Dataset(dst) as ds:
                    lats.append(np.asarray(ds.variables["group_lat"][:], dtype=np.float64))
                    lons.append(np.asarray(ds.variables["group_lon"][:], dtype=np.float64))
                    engs.append(np.asarray(ds.variables["group_energy"][:], dtype=np.float64))
            except Exception as exc:  # noqa: BLE001 -- corrupt/unreadable granule
                raise GLMUpstreamError(
                    f"GLM granule read failed for {key}: {exc}"
                ) from exc
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


# ---------------------------------------------------------------------------
# Purple log-ramp colorizer (ported verbatim from the validated prototype).
# ---------------------------------------------------------------------------
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
    # any lit cell is at least ~50% opaque so it reads over a basemap; alpha ramps
    # in with energy so the faintest cells stay translucent.
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


# ---------------------------------------------------------------------------
# Core: one accumulation window -> baked RGBA GED COG bytes (the cache fetch_fn).
# ---------------------------------------------------------------------------
def _fetch_glm_ged_cog_bytes(
    satellite: str,
    bbox: tuple[float, float, float, float],
    start_dt: datetime,
    end_dt: datetime,
) -> bytes:
    """List + download GLM granules in [start, end), bin GED, bake purple RGBA -> COG bytes.

    Raises ``GLMEmptyError`` when the window has no granules OR no lightning groups
    inside the AOI (the honesty floor -- never a blank overlay).
    """
    keys_times = _list_glm_keys_in_window(satellite, start_dt, end_dt)
    if not keys_times:
        raise GLMEmptyError(
            f"no {_GLM_PRODUCT} granules in {_GLM_SATELLITE_BUCKETS[satellite]} for "
            f"window {_iso_z(start_dt)}..{_iso_z(end_dt)} -- the date may pre-date the "
            f"{satellite} GLM record or fall in an ingest gap"
        )
    keys = [k for _, k in keys_times]
    if len(keys) > _MAX_GLM_GRANULES:
        logger.warning(
            "fetch_glm_lightning: window %s..%s has %d granules; capping at %d "
            "(use a shorter window or accumulation_window_s for the full span)",
            _iso_z(start_dt), _iso_z(end_dt), len(keys), _MAX_GLM_GRANULES,
        )
        keys = keys[:_MAX_GLM_GRANULES]

    lat, lon, eng = _fetch_glm_groups(satellite, keys)
    transform, width, height = _grid_for_bbox(bbox)  # 0.02 deg ~2 km, ABI-co-registered
    ged_j, n_in = _bin_ged(lat, lon, eng, bbox, width, height)
    if n_in == 0:
        raise GLMEmptyError(
            f"no GLM lightning groups detected inside the AOI for window "
            f"{_iso_z(start_dt)}..{_iso_z(end_dt)} ({int(lat.size)} groups full-disk, "
            f"0 inside bbox) -- the storm may be outside this AOI or electrically quiet"
        )
    rgba = _ged_to_purple_rgba(ged_j)
    logger.info(
        "fetch_glm_lightning: %s %s..%s -> %d granules, %d groups in AOI, "
        "%d lit cells on %dx%d grid",
        satellite, _iso_z(start_dt), _iso_z(end_dt), len(keys), n_in,
        int((ged_j > 0).sum()), height, width,
    )
    return _rgba_array_to_cog_bytes(rgba, transform, width, height)


def _emit_ged_layer(
    satellite: str,
    q_bbox: tuple[float, float, float, float],
    b_start: datetime,
    b_end: datetime,
    name: str,
) -> LayerURI:
    """Cache-resolve one GED frame and wrap it as a raster ``LayerURI``."""
    ts_tag = b_start.strftime("%Y%m%d%H%M%S")
    params = {
        "bbox": list(q_bbox),
        "satellite": satellite,
        "product": "glm_ged",
        "start_utc": _iso_z(b_start),
        "end_utc": _iso_z(b_end),
        "ramp_fj": [GED_FJ_FLOOR, GED_FJ_CEILING],
        "res_deg": _OUT_RES_DEG,
        "tool": "fetch_glm_lightning",
    }
    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="tif",
        fetch_fn=lambda s=b_start, e=b_end: _fetch_glm_ged_cog_bytes(
            satellite, q_bbox, s, e
        ),
    )
    assert result.uri is not None, (
        "fetch_glm_lightning is cacheable; uri must be set by read_through"
    )
    return LayerURI(
        layer_id=f"{_ID_TAG}-{satellite}-{ts_tag}-{q_bbox[0]:.3f}-{q_bbox[1]:.3f}",
        name=name,
        layer_type="raster",
        uri=result.uri,
        style_preset=_STYLE_PRESET,
        role="context",
        units=None,
        bbox=q_bbox,
    )


# ---------------------------------------------------------------------------
# Registered atomic tool.
# ---------------------------------------------------------------------------
@register_tool(
    _METADATA,
    # readOnlyHint=True, openWorldHint=True (anonymous NOAA S3),
    # destructiveHint=False, idempotentHint=True (per-window cache dedupes).
    open_world_hint=True,
)
def fetch_glm_lightning(
    bbox: tuple[float, float, float, float],
    satellite: str = "goes-19",
    start_utc: str | None = None,
    end_utc: str | None = None,
    accumulation_window_s: int | None = None,
    # job-0164: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> LayerURI | list[LayerURI]:
    """Fetch GOES GLM optical-lightning GROUP-ENERGY-DENSITY as a transparent purple raster overlay.

    **What it does:** Reads the GOES **GLM** (Geostationary Lightning Mapper)
    ``GLM-L2-LCFA`` product from the public ``noaa-goesNN`` S3 archive (anonymous /
    no key), bins the optical-lightning GROUP energy (Joules) onto a ~2 km EPSG:4326
    grid -- the SAME grid the GOES ABI products use, so it co-registers pixel-for-
    pixel with a GOES satellite / fire base -- and returns the group-energy-density
    (GED) as a TRANSPARENT purple RGBA raster ``LayerURI``. Only cells with detected
    lightning are opaque (deep-violet -> magenta -> white-pink with energy);
    everything else is transparent so it overlays a basemap directly.

    **Why GED (not raw strike counts):** GLM reports discrete ``group`` detections,
    each carrying a real ``group_energy``. Summing that energy per cell over a short
    accumulation window is the stable, physically meaningful "how electrically active
    is this cell" field shown in CIRA GLM density imagery -- bright over the
    convective cores, faint at the edges.

    **When to use:**
    - "Show me the lightning / lightning activity over this storm right now."
    - "Where is the most electrically active part of this convective cluster?"
    - As a lightning-intensity overlay on top of a GOES true-color / IR base.

    **When NOT to use:**
    - Ground-based cloud-to-ground strike points (use a CG-network tool, not GLM).
    - A scrubbable ABI imagery loop (use ``fetch_goes_archive_animation``).

    **Parameters:**
    - ``bbox`` (tuple): ``(min_lon, min_lat, max_lon, max_lat)`` EPSG:4326. Required.
      Example (Florida convective cluster): ``(-83.5, 25.5, -79.5, 31.5)``.
    - ``satellite`` (str, default ``"goes-19"``): ``"goes-19"`` (East, current),
      ``"goes-18"`` (West, current), ``"goes-16"`` / ``"goes-17"`` (historical).
    - ``start_utc`` / ``end_utc`` (str): ISO-8601 UTC window bounds. When omitted,
      the most-recent ~5 min ending now is accumulated.
    - ``accumulation_window_s`` (int, optional): when set (e.g. ``60``), the window is
      split into per-bucket frames and an ORDERED ``list[LayerURI]`` animation is
      returned (``step <N>`` names the web scrubber animates); when omitted (default),
      the ENTIRE window is accumulated into a SINGLE ``LayerURI``.

    **Returns:** a single 4-band transparent RGBA GED COG ``LayerURI``
    (``layer_type="raster"``, ``role="context"``, ``style_preset="glm_lightning"``)
    by default; or an ordered ``list[LayerURI]`` (ascending UTC, ``step <N>`` names,
    identical preset + bbox) when ``accumulation_window_s`` is set. A window with no
    granules OR no lightning groups inside the AOI raises a typed error (honesty
    floor) -- it never emits a blank overlay.

    **Cross-tool dependencies:**
    - Pairs with: ``fetch_goes_archive_animation`` band ``true_color`` / ``fire_temperature``
      (the GOES base this lightning density overlays, on the identical grid).
    - Sibling fetchers: ``fetch_goes_satellite`` (single ABI frame),
      ``fetch_goes_active_fire`` (split-window hot pixels).
    """
    q_bbox = _round_bbox(_validate_glm_bbox(bbox))
    # Normalize-then-validate: canonicalize GOES-18 / goes18 / G18 / "GOES West"
    # / 18 etc. to the hyphenated "goes-NN" bucket-key token BEFORE the bucket
    # path or cache key is built. A truly-unknown bird fails LOUD; we re-wrap the
    # shared normalizer's base error as this tool's GLMInputError so the GOES base
    # error type never leaks out of this fetcher. We then KEEP this tool's own
    # membership check + GLMInputError for the valid-but-unsupported case (a real
    # GOES bird this GLM tool does not serve).
    try:
        satellite = _normalize_satellite(satellite)
    except GOESInputError as exc:
        raise GLMInputError(str(exc)) from exc
    if satellite not in _GLM_SATELLITE_BUCKETS:
        raise GLMInputError(
            f"unknown satellite={satellite!r}; allowed: "
            f"{list(_GLM_SATELLITE_BUCKETS)}"
        )

    now = datetime.now(timezone.utc)
    end_dt = _parse_utc(end_utc) if end_utc else now
    start_dt = (
        _parse_utc(start_utc) if start_utc
        else end_dt - timedelta(minutes=_DEFAULT_WINDOW_MIN)
    )
    if start_dt >= end_dt:
        raise GLMInputError(
            f"start_utc ({_iso_z(start_dt)}) must be before end_utc ({_iso_z(end_dt)})"
        )

    sat_label = satellite.upper()

    # ---- Single accumulated frame (default) ------------------------------------
    if accumulation_window_s is None:
        if (end_dt - start_dt) > timedelta(minutes=_MAX_SINGLE_WINDOW_MIN):
            raise GLMInputError(
                f"single-frame window {_iso_z(start_dt)}..{_iso_z(end_dt)} exceeds "
                f"{_MAX_SINGLE_WINDOW_MIN} min; shorten it, or set accumulation_window_s "
                f"to fan the span into an animation"
            )
        name = (
            f"{_PRODUCT_LABEL} {_iso_z(start_dt)}..{_iso_z(end_dt)} ({sat_label})"
        )
        return _emit_ged_layer(satellite, q_bbox, start_dt, end_dt, name)

    # ---- Multi-frame animation -------------------------------------------------
    acc = int(accumulation_window_s)
    if acc < _MIN_ACCUM_S:
        raise GLMInputError(
            f"accumulation_window_s must be >= {_MIN_ACCUM_S} s (one LCFA granule); "
            f"got {acc}"
        )
    buckets: list[tuple[datetime, datetime]] = []
    t = start_dt
    while t < end_dt:
        b_end = min(t + timedelta(seconds=acc), end_dt)
        buckets.append((t, b_end))
        t = b_end
    if len(buckets) > MAX_GLM_FRAMES:
        logger.info(
            "fetch_glm_lightning: %d accumulation buckets -> even-subsampling to %d frames",
            len(buckets), MAX_GLM_FRAMES,
        )
        buckets = _even_subsample(buckets, MAX_GLM_FRAMES)

    layers: list[LayerURI] = []
    n_empty = 0
    last_err: Exception | None = None
    for frame_no, (b_start, b_end) in enumerate(buckets, start=1):
        iso = _iso_z(b_start)
        name = f"{_PRODUCT_LABEL} step {frame_no} {iso} ({sat_label})"
        try:
            layers.append(_emit_ged_layer(satellite, q_bbox, b_start, b_end, name))
        except GLMEmptyError as exc:
            n_empty += 1
            last_err = exc
            logger.info("fetch_glm_lightning: no lightning in bucket %s skipped (%s)", iso, exc)
            continue
        except GLMUpstreamError as exc:
            n_empty += 1
            last_err = exc
            logger.warning("fetch_glm_lightning: bucket %s upstream-failed (%s)", iso, exc)
            continue

    if not layers:
        raise GLMEmptyError(
            f"GLM detected no lightning in any of {len(buckets)} accumulation bucket(s) "
            f"over the AOI for window {_iso_z(start_dt)}..{_iso_z(end_dt)}"
            + (f": {last_err}" if last_err else "")
        )
    logger.info(
        "fetch_glm_lightning: %d GED frame(s) (%d empty/failed) for %s window %s..%s",
        len(layers), n_empty, satellite, _iso_z(start_dt), _iso_z(end_dt),
    )
    return layers
