"""``model_glm_lightning_animation`` -- DIRECT GOES-19 GLM lightning animation composer.

Recreates CIRA/RAMMB SLIDER's "visible + Group Energy Density" loop for a Gulf-of-
Mexico tropical cyclone, as a real in-app scrubbable layer. Over a daytime UTC
window it animates a **grayscale GOES-19 (GOES-East) ABI band-2 visible** base and
bakes a **GLM Group-Energy-Density purple/violet overlay** on top: GLM Level-2 LCFA
optical-lightning detections, gridded onto the SAME ABI ~2 km EPSG:4326 grid the
visible base lands on, accumulated into ~1-minute frames, displayed in femtojoules
on a log purple ramp. Lightning flickers as bright violet-to-white cells over the
grayscale storm, marching with the convection -- the canonical CIRA Gulf-cyclone
post (design doc: ``reports/design/glm_lightning_demo.md``).

DIRECT, NO NEWS STEP. Unlike ``model_satellite_fire_animation`` (which has a
news/incident front-half + a bbox/window review gate that the agent stumbled on),
this composer takes a **direct AOI bbox + UTC time window** and goes STRAIGHT to
fetch -> grid -> bake -> publish. There is NO ``model_news_event_ingest``, NO
NIFC/news lookup, NO geocode-from-news, NO SLIDER snap. The inputs ARE the AOI and
the window. (Its sibling ``model_goes_fire_animation`` is ALSO a direct no-news
entry, but it auto-snaps against the SLIDER availability index; this composer is
even more direct -- it reads the raw ``noaa-goes19`` S3 archive at the requested
window with no availability pre-pass.)

What it produces (the same FRAME SHAPE the web ``SequenceScrubber`` /
``detectSequentialGroups`` consume with ZERO web change -- distinct per-frame keys,
identical ``style_preset`` + bbox grouping key, a ``step <N> <ISO>`` name token):

  1. A **baked blend** scrubber: per 1-min frame, the purple GED is
     alpha-composited OVER the grayscale C02 visible base into ONE 3-band RGB COG
     (the CIRA look, one layer). Renders via publish_layer's multiband passthrough
     -- no new style preset.
  2. A **standalone purple GED overlay** scrubber (from the existing
     ``fetch_glm_lightning`` tool, ``accumulation_window_s=60``): a transparent
     4-band RGBA the user can toggle over ANY base. Lets the lightning be separated
     from the bake when desired.

Honesty floor (the render-chokepoint norm): a window with NO GLM granules, or no
lightning groups inside the AOI in ANY 1-min bucket, raises a typed empty error --
it NEVER emits a blank/fabricated animation. The visible base is best-effort: if a
frame's MCMIPC scan is missing/off-grid the lightning overlay still emits (degrades
to the standalone purple scrubber rather than failing).

Architecture: this is a deterministic workflow (Invariant 2) composing already-
cached atomic tools. It reuses the proven primitives verbatim --
``_grid_for_bbox`` / ``_warp_band_to_physical`` / ``rgb_array_to_cog_bytes`` /
``_bake_fire_over_base`` from ``fetch_goes_archive_animation`` and the GED binner +
purple ramp from ``fetch_glm_lightning`` -- and dispatches every atomic tool through
``TOOL_REGISTRY[name].fn`` (registry-as-source). The heavy emit-free S3 fetch +
raster bake runs in ``asyncio.to_thread`` (no-loop-blocking norm).

ASCII only.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from ..tools import TOOL_REGISTRY, register_tool

if TYPE_CHECKING:
    from ..pipeline_emitter import PipelineEmitter

__all__ = [
    "model_glm_lightning_animation",
    "run_model_glm_lightning_animation",
    "GLMAnimError",
    "GLMAnimInputError",
    "GLMAnimEmptyError",
    "DEFAULT_GLM_WINDOW",
    "DEFAULT_ACCUM_S",
    "MAX_GLM_ANIM_FRAMES",
    "_parse_utc",
    "_resolve_window",
    "_frame_buckets",
]

logger = logging.getLogger("trid3nt_server.workflows.model_glm_lightning_animation")


# --------------------------------------------------------------------------- #
# Typed errors (FR-AS-11: error_code + retryable -> WS A.6 error frame).
# --------------------------------------------------------------------------- #


class GLMAnimError(RuntimeError):
    """Base class for model_glm_lightning_animation failures."""

    error_code: str = "GLM_ANIM_ERROR"
    retryable: bool = False


class GLMAnimInputError(GLMAnimError):
    """Caller passed a bad bbox / window / satellite / accumulation."""

    error_code = "GLM_ANIM_INPUT_INVALID"
    retryable = False


class GLMAnimEmptyError(GLMAnimError):
    """NO lightning in any 1-min bucket over the AOI for the window (honesty floor)."""

    error_code = "GLM_ANIM_EMPTY"
    retryable = False


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

#: GOES-East is the Gulf satellite. GOES-19 is the current operational East bird
#: (since 2025-04); GOES-16 is the historical East. goes-18 (West) is the WRONG
#: sector for a Gulf TC -- allowed only so a caller can explicitly pick West.
_GLM_SATELLITES = ("goes-19", "goes-16", "goes-18", "goes-17")

#: Default GOES-East for the Gulf demo.
DEFAULT_SATELLITE = "goes-19"

#: The CIRA operational GED accumulation = 1 minute per frame (three 20 s LCFA
#: granules merged). The web scrubber animates these in order.
DEFAULT_ACCUM_S = 60

#: Default window length when only one bound (or neither) is given. A short demo
#: loop; a full multi-hour run is fanned out by passing an explicit wide window.
DEFAULT_GLM_WINDOW = timedelta(minutes=20)

#: Hard cap on emitted frames (each = one ~1-min GED frame + a base lookup). A
#: 4-hour 240-frame loop exceeds this -- such a run should fan out off-box (a
#: Batch/offload), see the workflow docstring's "Full-run note".
MAX_GLM_ANIM_FRAMES = 30

#: ABI visible base band (daytime). The MCMIPC netCDF carries every CMI band; we
#: read CMI_C02 (0.64 um red, the 0.5 km native visible) and render grayscale.
_VIS_BASE_VAR = "CMI_C02"
#: Night fallback band: C13 longwave window (10.3 um BT) -- cold cloud-tops bright.
#: Used when ``base_band="ir"`` (the design-doc "daylight window" night fallback).
_IR_BASE_VAR = "CMI_C13"

#: Grayscale gamma (g<1 brightens the dim visible reflectance so the storm pops).
_VIS_GAMMA = 1.0 / 1.5
#: C13 brightness-temperature stretch for the IR night base (K). Cold cloud-tops
#: (low BT) map to BRIGHT (inverted) so convective towers read white.
_IR_BT_RANGE_K = (200.0, 300.0)

#: The baked-blend products are 3-band RGB COGs rendered via publish_layer's
#: multiband passthrough. The style_preset is the per-frame GROUPING key for the
#: web scrubber (identical across frames) -- it is NOT a colormap.
_BAKED_STYLE_PRESET = "glm_lightning_baked"
_BAKED_LABEL = "GLM Lightning + Visible (G19)"
_BAKED_ID_TAG = "glm-baked"


# --------------------------------------------------------------------------- #
# Window parsing (pure -- no network). DIRECT: the inputs ARE the window.
# --------------------------------------------------------------------------- #


def _parse_utc(value: Any) -> datetime | None:
    """Parse an ISO-8601 string / datetime -> aware UTC, or None for a falsy value.

    Accepts a trailing 'Z', '+00:00', a space or 'T' separator, and a bare date.
    Raises ``GLMAnimInputError`` for an unparseable non-empty value.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, datetime):
        return (
            value.astimezone(timezone.utc)
            if value.tzinfo
            else value.replace(tzinfo=timezone.utc)
        )
    s = str(value).strip().replace("Z", "+00:00").replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(str(value).strip().replace(" ", "T", 1), fmt)
                break
            except ValueError:
                continue
        else:
            raise GLMAnimInputError(
                f"could not parse UTC time {value!r}; use ISO-8601 "
                "(e.g. '2025-07-05T18:00:00Z')"
            )
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _resolve_window(
    start_dt: datetime | None, end_dt: datetime | None
) -> tuple[datetime, datetime]:
    """Resolve the DIRECT window from the optional start/end (pure, no network).

    - Both given: that window verbatim (the direct case).
    - End only: a ``DEFAULT_GLM_WINDOW`` window ending at end.
    - Start only: a ``DEFAULT_GLM_WINDOW`` window starting at start.
    - Neither: a ``DEFAULT_GLM_WINDOW`` window ending now ("most recent").
    """
    now = datetime.now(timezone.utc)
    if start_dt is not None and end_dt is not None:
        start, end = start_dt, end_dt
    elif end_dt is not None:
        start, end = end_dt - DEFAULT_GLM_WINDOW, end_dt
    elif start_dt is not None:
        start, end = start_dt, start_dt + DEFAULT_GLM_WINDOW
    else:
        start, end = now - DEFAULT_GLM_WINDOW, now
    if start >= end:
        end = start + DEFAULT_GLM_WINDOW
    return start, end


def _frame_buckets(
    start_dt: datetime, end_dt: datetime, accum_s: int, cap: int = MAX_GLM_ANIM_FRAMES
) -> list[tuple[datetime, datetime]]:
    """Split [start, end) into ascending ``accum_s``-second buckets (1-min frames).

    Even-subsamples to ``cap`` frames (endpoints kept) when the span yields more,
    so a wide window never fans out into an unbounded number of frames.
    """
    buckets: list[tuple[datetime, datetime]] = []
    t = start_dt
    step = timedelta(seconds=accum_s)
    while t < end_dt:
        b_end = min(t + step, end_dt)
        buckets.append((t, b_end))
        t = b_end
    if len(buckets) <= cap:
        return buckets
    # even-subsample, endpoints kept.
    import numpy as np

    idx = np.unique(np.rint(np.linspace(0, len(buckets) - 1, cap)).astype(int))
    return [buckets[int(i)] for i in idx]


# --------------------------------------------------------------------------- #
# Registry helpers
# --------------------------------------------------------------------------- #


def _registry_fn(name: str) -> Any:
    """Resolve ``name`` -> the registered tool callable (registry-as-source rule)."""
    entry = TOOL_REGISTRY.get(name)
    if entry is None:
        raise GLMAnimError(
            f"required atomic tool {name!r} is not registered "
            f"(known sample: {sorted(TOOL_REGISTRY)[:6]}...)"
        )
    return entry.fn


def _iso_z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# Visible C02 grayscale base reader (reuses the proven archive primitives)
# --------------------------------------------------------------------------- #


def _grayscale_visible_base(
    satellite: str,
    bbox: tuple[float, float, float, float],
    when: datetime,
    base_band: str,
    transform: Any,
    width: int,
    height: int,
) -> Any:
    """Read the nearest ABI MCMIPC scan to ``when`` -> a grayscale ``(3,H,W)`` RGB base.

    DAYLIGHT path (``base_band="visible"``): read C02 reflectance (0..1), gamma-
    brighten, replicate to 3 grayscale channels. NIGHT fallback (``base_band="ir"``):
    read C13 brightness temperature, INVERT-stretch (cold cloud-tops -> bright).

    The MCMIPC scan is reprojected onto the IDENTICAL EPSG:4326 grid the GED bins
    onto (``transform``/``width``/``height`` from ``_grid_for_bbox``) so the base
    and the purple overlay are pixel-co-registered for the bake. Returns the base
    RGB ``(3,H,W)`` uint8, or raises if no scan is available / off-grid.
    """
    import os
    import tempfile

    import numpy as np

    from ..tools.fetchers.imagery.fetch_goes_archive_animation import (
        _SATELLITE_BUCKETS,
        _list_archive_keys_in_window,
        _warp_band_to_physical,
    )

    # Find the MCMIPC scan nearest ``when`` (ABI CONUS is ~5 min/scan). Widen the
    # search to +/- 6 min so a 1-min GED frame always finds a base scan.
    pad = timedelta(minutes=6)
    pairs = _list_archive_keys_in_window(satellite, when - pad, when + pad)
    if not pairs:
        raise GLMAnimError(
            f"no ABI MCMIPC visible-base scan within +/-6 min of {_iso_z(when)} "
            f"for {satellite}"
        )
    # nearest by start-time distance.
    t_key, key = min(pairs, key=lambda p: abs((p[0] - when).total_seconds()))
    bucket = _SATELLITE_BUCKETS[satellite]
    url = f"https://{bucket}.s3.amazonaws.com/{key}"

    from ..tools.fetchers.imagery.fetch_goes_satellite import _download_to_tempfile

    nc_path = _download_to_tempfile(url)
    try:
        if base_band == "ir":
            bt = _warp_band_to_physical(nc_path, _IR_BASE_VAR, transform, width, height)
            lo, hi = _IR_BT_RANGE_K
            # invert: cold (low BT) -> bright (1.0); warm -> dark (0.0).
            g01 = np.clip((hi - np.nan_to_num(bt, nan=hi)) / (hi - lo), 0.0, 1.0)
        else:
            refl = _warp_band_to_physical(
                nc_path, _VIS_BASE_VAR, transform, width, height
            )
            g01 = np.clip(np.nan_to_num(refl, nan=0.0), 0.0, 1.0) ** np.float32(
                _VIS_GAMMA
            )
    finally:
        try:
            os.unlink(nc_path)
        except OSError:
            pass

    gray = np.clip(np.rint(g01 * 255.0), 0, 255).astype(np.uint8)
    return np.stack([gray, gray, gray], axis=0)  # (3, H, W)


# --------------------------------------------------------------------------- #
# One frame: GLM GED purple overlay BAKED over the grayscale visible base
# --------------------------------------------------------------------------- #


def _bake_glm_frame_cog_bytes(
    satellite: str,
    bbox: tuple[float, float, float, float],
    b_start: datetime,
    b_end: datetime,
    base_band: str,
) -> tuple[bytes, dict[str, Any]]:
    """Build ONE baked frame: purple GED over grayscale C02 base -> 3-band RGB COG.

    Reuses the proven primitives end-to-end (no re-implemented algorithm):
      - ``_grid_for_bbox`` -> the shared ABI 2 km EPSG:4326 grid,
      - ``fetch_glm_lightning._list_glm_keys_in_window`` + ``_fetch_glm_groups`` +
        ``_bin_ged`` + ``_ged_to_purple_rgba`` -> the GED purple RGBA on that grid,
      - ``_grayscale_visible_base`` -> the C02 grayscale RGB base on that grid,
      - ``_bake_fire_over_base`` -> alpha-composite the purple over the base,
      - ``rgb_array_to_cog_bytes`` -> the 3-band RGB COG bytes.

    Raises ``glmmod.GLMEmptyError`` when the bucket has no granules / no in-AOI
    groups (the caller skips that frame). Returns ``(cog_bytes, stats)`` where stats
    carries the per-frame sanity numbers (n_groups, n_lit_cells, peak_fj, n_granules).
    """
    import numpy as np

    from ..tools.fetchers.weather import fetch_glm_lightning as glmmod
    from ..tools.fetchers.imagery.fetch_goes_archive_animation import (
        _bake_fire_over_base,
        _grid_for_bbox,
        rgb_array_to_cog_bytes,
    )

    transform, width, height = _grid_for_bbox(bbox)  # 0.02 deg ~2 km

    # --- GLM GED on the shared grid (the load-bearing net-new algorithm, reused) --
    keys_times = glmmod._list_glm_keys_in_window(satellite, b_start, b_end)
    if not keys_times:
        raise glmmod.GLMEmptyError(
            f"no GLM granules in {satellite} for bucket "
            f"{_iso_z(b_start)}..{_iso_z(b_end)}"
        )
    keys = [k for _, k in keys_times]
    lat, lon, eng = glmmod._fetch_glm_groups(satellite, keys)
    ged_j, n_in = glmmod._bin_ged(lat, lon, eng, bbox, width, height)
    if n_in == 0:
        raise glmmod.GLMEmptyError(
            f"no GLM groups inside the AOI for bucket "
            f"{_iso_z(b_start)}..{_iso_z(b_end)}"
        )
    purple_rgba = glmmod._ged_to_purple_rgba(ged_j)  # (4, H, W) uint8

    # --- grayscale visible base (best-effort: degrade to a black base on miss). --
    try:
        base_rgb = _grayscale_visible_base(
            satellite, bbox, b_start, base_band, transform, width, height
        )
    except Exception as exc:  # noqa: BLE001 -- a missing base must not sink the frame
        logger.warning(
            "model_glm_lightning_animation: visible base missing for %s (%s); "
            "baking lightning over a black base",
            _iso_z(b_start),
            exc,
        )
        base_rgb = np.zeros((3, height, width), dtype=np.uint8)

    baked = _bake_fire_over_base(base_rgb, purple_rgba)  # (3, H, W) uint8
    cog = rgb_array_to_cog_bytes(baked, transform, width, height)
    stats = {
        "n_granules": len(keys),
        "n_groups_in_aoi": int(n_in),
        "n_lit_cells": int((ged_j > 0).sum()),
        "peak_fj": float(ged_j.max() * 1e15),
        "granule_keys": [k.split("/")[-1] for k in keys],
    }
    return cog, stats


def _emit_baked_frame(
    satellite: str,
    bbox: tuple[float, float, float, float],
    b_start: datetime,
    b_end: datetime,
    base_band: str,
    name: str,
) -> tuple[LayerURI, dict[str, Any]]:
    """Cache-resolve one baked frame and wrap it as a raster ``LayerURI`` + stats."""
    from ..tools.cache import read_through
    from ..tools.fetchers.weather.fetch_glm_lightning import _METADATA as _GLM_METADATA

    ts_tag = b_start.strftime("%Y%m%d%H%M%S")
    captured: dict[str, Any] = {}

    def _fetch() -> bytes:
        cog, stats = _bake_glm_frame_cog_bytes(
            satellite, bbox, b_start, b_end, base_band
        )
        captured.update(stats)
        return cog

    params = {
        "bbox": list(bbox),
        "satellite": satellite,
        "product": "glm_baked",
        "base_band": base_band,
        "start_utc": _iso_z(b_start),
        "end_utc": _iso_z(b_end),
        "tool": "model_glm_lightning_animation",
    }
    result = read_through(
        metadata=_GLM_METADATA, params=params, ext="tif", fetch_fn=_fetch
    )
    assert result.uri is not None, "baked GLM frame is cacheable; uri must be set"
    layer = LayerURI(
        layer_id=f"{_BAKED_ID_TAG}-{satellite}-{ts_tag}-{bbox[0]:.3f}-{bbox[1]:.3f}",
        name=name,
        layer_type="raster",
        uri=result.uri,
        style_preset=_BAKED_STYLE_PRESET,
        role="context",
        units=None,
        bbox=bbox,
    )
    return layer, captured


# --------------------------------------------------------------------------- #
# The workflow
# --------------------------------------------------------------------------- #


async def model_glm_lightning_animation(
    bbox: tuple[float, float, float, float],
    start_utc: str | None = None,
    end_utc: str | None = None,
    satellite: str = DEFAULT_SATELLITE,
    accumulation_window_s: int = DEFAULT_ACCUM_S,
    base_band: str = "visible",
    storm_name: str | None = None,
    overlay_standalone_ged: bool = True,
    *,
    pipeline_emitter: "PipelineEmitter | None" = None,
) -> dict[str, Any]:
    """DIRECT GOES-19 GLM lightning animation: AOI + window -> fetch -> grid -> bake -> publish.

    NO news step, NO geocode, NO availability snap. The bbox + window ARE the
    inputs; the composer goes straight to the raw ``noaa-goes19`` archive.

    Per 1-min frame it bins the GLM Group-Energy-Density onto the ABI 2 km grid,
    reads the nearest C02 visible scan onto the SAME grid, and bakes the purple GED
    over the grayscale base -> ONE scrubbable RGB frame (the CIRA look). It also
    emits a standalone transparent purple GED overlay scrubber (toggle the
    lightning over any base) when ``overlay_standalone_ged`` is True.

    Args:
        bbox: AOI ``(min_lon, min_lat, max_lon, max_lat)`` EPSG:4326. Required.
        start_utc / end_utc: ISO-8601 UTC window bounds (the DIRECT window). When a
            bound is omitted a ~20-min window is used.
        satellite: default "goes-19" (GOES-East, the Gulf bird). "goes-16" is the
            historical East; "goes-18"/"goes-17" are West (wrong sector for a Gulf TC).
        accumulation_window_s: per-frame GED accumulation; default 60 (the CIRA
            1-min convention).
        base_band: "visible" (C02 daytime grayscale, default) or "ir" (C13 longwave
            night fallback, inverted so cold tops are bright).
        storm_name: optional label folded into the layer name (e.g. "Gulf TC").
        overlay_standalone_ged: also emit the separable transparent purple GED
            overlay scrubber (default True).
        pipeline_emitter: optional live progress emitter.

    Returns:
        ``{status:"ok", bbox, satellite, start_utc, end_utc, accumulation_window_s,
        base_band, n_frames, n_overlay_frames, frame_stats:[...], layers:[...],
        message}``. Raises ``GLMAnimEmptyError`` when NO bucket had in-AOI lightning
        (the honesty floor).
    """
    # NATE 2026-06-26: lightning animation frames never rendered because the
    # registered wrapper passes pipeline_emitter=None, so the per-frame
    # add_loaded_layer emit (the step the working flood composer does) never
    # ran. Bind the LIVE current_emitter() here, exactly like
    # model_flood_scenario.py, so confirmed runs emit each published frame into
    # session-state loaded_layers.
    from ..pipeline_emitter import current_emitter

    pipeline_emitter = pipeline_emitter or current_emitter()

    # --- validate inputs (typed errors, never a crash). ---
    if not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
        raise GLMAnimInputError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    try:
        q_bbox = (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
    except (TypeError, ValueError) as exc:
        raise GLMAnimInputError(f"bbox values must be numeric: {bbox!r}") from exc
    if not (q_bbox[0] < q_bbox[2] and q_bbox[1] < q_bbox[3]):
        raise GLMAnimInputError(
            f"bbox must satisfy min_lon<max_lon and min_lat<max_lat; got {q_bbox}"
        )
    if satellite not in _GLM_SATELLITES:
        raise GLMAnimInputError(
            f"satellite {satellite!r} not in {list(_GLM_SATELLITES)}"
        )
    if base_band not in ("visible", "ir"):
        raise GLMAnimInputError(
            f"base_band {base_band!r} not in ('visible', 'ir')"
        )
    accum_s = int(accumulation_window_s)
    if accum_s < 20:
        raise GLMAnimInputError(
            f"accumulation_window_s must be >= 20 s (one LCFA granule); got {accum_s}"
        )

    req_start = _parse_utc(start_utc)
    req_end = _parse_utc(end_utc)
    if req_start is not None and req_end is not None and req_start >= req_end:
        raise GLMAnimInputError(
            f"start_utc ({_iso_z(req_start)}) must be before end_utc "
            f"({_iso_z(req_end)})"
        )
    win_start, win_end = _resolve_window(req_start, req_end)
    start_iso, end_iso = _iso_z(win_start), _iso_z(win_end)
    sat_label = satellite.upper()
    label_stem = f"{_BAKED_LABEL}" + (f" -- {storm_name}" if storm_name else "")

    # --- Emit the AOI snap-to map zoom EARLY (UX verb, never a gate). ---
    if pipeline_emitter is not None:
        try:
            await pipeline_emitter.emit_map_command("zoom-to", {"bbox": list(q_bbox)})
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "model_glm_lightning_animation: early AOI zoom-to emit failed (%s)",
                exc,
            )

    buckets = _frame_buckets(win_start, win_end, accum_s)
    logger.info(
        "model_glm_lightning_animation: DIRECT run %s %s..%s -> %d x %ds frame(s), "
        "base=%s (NO news/geocode/snap)",
        satellite, start_iso, end_iso, len(buckets), accum_s, base_band,
    )

    # --- Per-frame: bake purple GED over the grayscale visible base (off-loop). --
    if pipeline_emitter is not None:
        bake_step = await pipeline_emitter.add_step(
            name="Bake GLM lightning over visible base (per 1-min frame)",
            tool_name="model_glm_lightning_animation",
        )
        await pipeline_emitter.mark_running(bake_step)
    else:
        bake_step = None

    baked_layers: list[LayerURI] = []
    frame_stats: list[dict[str, Any]] = []
    n_empty = 0
    last_err: Exception | None = None
    from ..tools.fetchers.weather import fetch_glm_lightning as glmmod

    for frame_no, (b_start, b_end) in enumerate(buckets, start=1):
        iso = _iso_z(b_start)
        name = f"{label_stem} step {frame_no} {iso} ({sat_label})"
        try:
            layer, stats = await asyncio.to_thread(
                _emit_baked_frame, satellite, q_bbox, b_start, b_end, base_band, name
            )
        except glmmod.GLMEmptyError as exc:
            n_empty += 1
            last_err = exc
            logger.info(
                "model_glm_lightning_animation: no lightning in bucket %s skipped (%s)",
                iso, exc,
            )
            continue
        except Exception as exc:  # noqa: BLE001 -- one bad frame must not sink the run
            n_empty += 1
            last_err = exc
            logger.warning(
                "model_glm_lightning_animation: bucket %s failed (%s)", iso, exc
            )
            continue
        baked_layers.append(layer)
        frame_stats.append({"frame": frame_no, "start_utc": iso, **stats})

    if not baked_layers:
        if pipeline_emitter is not None and bake_step is not None:
            await pipeline_emitter.mark_failed(
                bake_step, GLMAnimEmptyError.error_code, "no lightning in any bucket"
            )
        raise GLMAnimEmptyError(
            f"GLM detected no lightning in any of {len(buckets)} 1-min bucket(s) over "
            f"the AOI {list(q_bbox)} for window {start_iso}..{end_iso} ({satellite}). "
            "The storm may be outside this AOI, electrically quiet, or the date may "
            "pre-date the GLM record."
            + (f" Last error: {last_err}" if last_err else "")
        )
    if pipeline_emitter is not None and bake_step is not None:
        await pipeline_emitter.mark_complete(bake_step)

    # --- Standalone transparent purple GED overlay scrubber (separable). --------
    overlay_layers: list[LayerURI] = []
    if overlay_standalone_ged:
        overlay_layers = await _dispatch_standalone_ged(
            q_bbox, satellite, start_iso, end_iso, accum_s, pipeline_emitter
        )

    # --- Publish every layer via TiTiler (off-loop, non-fatal on failure). ------
    all_layers = baked_layers + overlay_layers
    published = await _publish_layers(all_layers, pipeline_emitter)

    # NATE 2026-06-26: EMIT each published frame into session-state loaded_layers
    # (mirrors model_flood_scenario.py ~3774). _publish_layers returns
    # {layer_id: published uri} for the frames it could publish; build a NEW
    # LayerURI copy with uri=<published uri> and add_loaded_layer it so the map
    # actually renders the lightning animation. HONESTY FLOOR: only emit frames
    # whose publish returned a renderable uri -- an http(s) tile url or, since
    # the TiTiler exit / QGIS-native swap, the raw s3:// COG uri (the plugin
    # reads it via /vsicurl/). Anything else (empty/error strings, gs://,
    # file://) is skipped (never added). When current_emitter() is None
    # (direct/smoke/unit test without an emitter) emission is skipped; the
    # {id: uri} map is still returned for the summary.
    if pipeline_emitter is not None:
        for layer in all_layers:
            published_url = published.get(layer.layer_id)
            if not (
                isinstance(published_url, str)
                and published_url.startswith(("http://", "https://", "s3://"))
            ):
                # Publish failed / returned a non-renderable value -> honest
                # skip; never emit a frame the plugin cannot fetch.
                continue
            emit_layer = LayerURI(
                layer_id=layer.layer_id,
                name=layer.name,
                layer_type=layer.layer_type,
                uri=published_url,
                style_preset=layer.style_preset,
                temporal=layer.temporal,
                role=layer.role,
                units=layer.units,
                bbox=layer.bbox,
            )
            try:
                await pipeline_emitter.add_loaded_layer(emit_layer)
            except Exception as exc:  # noqa: BLE001 -- a publish/emit hiccup is non-fatal
                logger.warning(
                    "model_glm_lightning_animation: add_loaded_layer(%s) failed (%s)",
                    layer.layer_id,
                    exc,
                )

    n_frames = len(baked_layers)
    peak_overall = max((s["peak_fj"] for s in frame_stats), default=0.0)
    return {
        "status": "ok",
        "bbox": list(q_bbox),
        "satellite": satellite,
        "start_utc": start_iso,
        "end_utc": end_iso,
        "accumulation_window_s": accum_s,
        "base_band": base_band,
        "storm_name": storm_name,
        "n_frames": n_frames,
        "n_empty_buckets": n_empty,
        "n_overlay_frames": len(overlay_layers),
        "peak_fj": peak_overall,
        "frame_stats": frame_stats,
        "layers": [_layer_summary(lyr, published) for lyr in all_layers],
        "message": (
            f"Animated {n_frames} baked GLM-lightning-over-visible frame(s) "
            f"(1-min GED, {base_band} base) over {start_iso}..{end_iso} for "
            f"{satellite}"
            + (f" ({storm_name})" if storm_name else "")
            + (
                f" plus a {len(overlay_layers)}-frame standalone purple GED overlay"
                if overlay_layers
                else ""
            )
            + f"; peak {peak_overall:.0f} fJ. DIRECT run -- no news/geocode step."
        ),
    }


# --------------------------------------------------------------------------- #
# Standalone GED overlay dispatch + publish helpers
# --------------------------------------------------------------------------- #


async def _dispatch_standalone_ged(
    bbox: tuple[float, float, float, float],
    satellite: str,
    start_iso: str,
    end_iso: str,
    accum_s: int,
    pipeline_emitter: "PipelineEmitter | None",
) -> list[LayerURI]:
    """Dispatch ``fetch_glm_lightning`` (accumulation_window_s) -> transparent GED scrubber.

    The standalone purple overlay the user can toggle over any base. A failure /
    empty run returns ``[]`` (the bake scrubber already carries the lightning, so a
    missing standalone overlay is non-fatal).
    """
    fetcher = _registry_fn("fetch_glm_lightning")
    if pipeline_emitter is not None:
        step = await pipeline_emitter.add_step(
            name="Fetch standalone purple GED overlay frames",
            tool_name="fetch_glm_lightning",
        )
        await pipeline_emitter.mark_running(step)
    else:
        step = None
    try:
        frames = await asyncio.to_thread(
            fetcher, bbox, satellite, start_iso, end_iso, accum_s
        )
    except Exception as exc:  # noqa: BLE001 -- standalone overlay is non-fatal
        logger.warning(
            "model_glm_lightning_animation: standalone GED overlay failed (%s)", exc
        )
        if pipeline_emitter is not None and step is not None:
            await pipeline_emitter.mark_failed(
                step, "GLM_OVERLAY_FAILED", f"fetch_glm_lightning failed: {exc}"
            )
        return []
    frame_list = list(frames) if isinstance(frames, list) else [frames]
    if pipeline_emitter is not None and step is not None:
        await pipeline_emitter.mark_complete(step)
    return frame_list


async def _publish_layers(
    layers: list[LayerURI], pipeline_emitter: "PipelineEmitter | None"
) -> dict[str, str]:
    """Publish each layer via publish_layer (TiTiler) in asyncio.to_thread.

    Returns ``{layer_id: published_url}``. Publish failures are non-fatal (the COG
    still exists at its cache URI) -- logged + skipped.
    """
    published: dict[str, str] = {}
    try:
        publish_fn = _registry_fn("publish_layer")
    except GLMAnimError:
        logger.warning(
            "model_glm_lightning_animation: publish_layer not registered; "
            "skipping publish"
        )
        return published
    for layer in layers:
        try:
            url = await asyncio.to_thread(
                publish_fn, layer.uri, layer.layer_id, layer.style_preset
            )
            if isinstance(url, str) and url:
                published[layer.layer_id] = url
        except Exception as exc:  # noqa: BLE001 -- publish is non-fatal
            logger.warning(
                "model_glm_lightning_animation: publish_layer(%s) failed (%s)",
                layer.layer_id, exc,
            )
    return published


def _layer_summary(layer: LayerURI, published: dict[str, str]) -> dict[str, Any]:
    """Compact JSON summary of one layer (the producing URI + any published URL)."""
    return {
        "layer_id": layer.layer_id,
        "name": layer.name,
        "layer_type": layer.layer_type,
        "style_preset": layer.style_preset,
        "role": layer.role,
        "uri": layer.uri,
        "published_url": published.get(layer.layer_id),
    }


# --------------------------------------------------------------------------- #
# LLM-exposed thin atomic-tool wrapper (workflow_dispatch source class)
# --------------------------------------------------------------------------- #


_RUN_METADATA = AtomicToolMetadata(
    name="run_model_glm_lightning_animation",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)


@register_tool(_RUN_METADATA)
async def run_model_glm_lightning_animation(
    bbox: tuple[float, float, float, float],
    start_utc: str | None = None,
    end_utc: str | None = None,
    satellite: str = DEFAULT_SATELLITE,
    accumulation_window_s: int = DEFAULT_ACCUM_S,
    base_band: str = "visible",
    storm_name: str | None = None,
    overlay_standalone_ged: bool = True,
    # job-0164: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Animate a GOES-19 GLM lightning loop DIRECT from an AOI + UTC window -- NO news step.

    Recreates the CIRA/RAMMB SLIDER "visible + Group Energy Density" lightning loop
    for a Gulf-of-Mexico tropical cyclone as a real, in-app, time-scrubbable layer.
    Over the requested daytime UTC window it animates a grayscale GOES-19 (GOES-East)
    ABI band-2 VISIBLE base and bakes a GLM Group-Energy-Density (GED) PURPLE/violet
    overlay on top -- GLM optical-lightning group energy gridded onto the ABI 2 km
    grid, accumulated per 1-minute frame, on a log purple ramp (femtojoules).
    Lightning flickers as bright violet-to-white cells over the grayscale storm,
    marching with the convection.

    DIRECT, ONE-SHOT, NO NEWS LOOKUP. This composer takes the AOI bbox + UTC window
    DIRECTLY and goes STRAIGHT to fetch -> grid -> bake -> publish. There is NO
    news/incident ingest, NO NIFC/news lookup, NO geocode-from-news, and NO
    availability/SLIDER snap. Pick THIS when you already have an AOI and a time
    window and want the lightning loop to just run.

    When to use:
        - "Show the lightning over this storm" / "animate the GLM Group Energy
          Density over this bbox and window" when the AOI is already known.
        - Recreate the CIRA Gulf-cyclone visible + GED loop for a pinned event.

    When NOT to use:
        - The AOI is NOT known and you need a news/geocode lookup first (there is no
          such path here by design -- resolve the bbox separately, then call this).
        - A GOES fire-temperature imagery loop (run_model_goes_fire_animation).
        - A single most-recent lightning snapshot (fetch_glm_lightning, no
          accumulation_window_s).

    Params:
        bbox: AOI [min_lon, min_lat, max_lon, max_lat] EPSG:4326. Required. Example
            (north-central Gulf): [-91.0, 25.0, -85.0, 30.0].
        start_utc / end_utc: ISO-8601 UTC window bounds (the DIRECT window, e.g.
            "2025-07-05T18:00:00Z" .. "2025-07-05T18:20:00Z"). Omit a bound for a
            ~20-min default window.
        satellite: default "goes-19" (GOES-East, the Gulf bird). "goes-16" historical
            East; "goes-18"/"goes-17" West (wrong sector for a Gulf TC).
        accumulation_window_s: per-frame GED accumulation seconds; default 60 (the
            CIRA 1-minute convention).
        base_band: "visible" (C02 daytime grayscale, default) or "ir" (C13 longwave
            night fallback; cold cloud-tops rendered bright).
        storm_name: optional label folded into the layer name (e.g. "Gulf TC").
        overlay_standalone_ged: also emit a separable transparent purple GED overlay
            scrubber the user can toggle over any base (default true).

    Returns:
        A dict with status="ok", the AOI bbox, satellite, the window, the per-frame
        GED stats (n_groups_in_aoi, n_lit_cells, peak_fj, granule_keys), the baked +
        overlay frame counts, and the published layer summaries. Raises
        GLM_ANIM_EMPTY only when NO 1-min bucket had in-AOI lightning (honesty floor).

    Cross-tool dependencies:
        Step chain (NO news/geocode prefix): per 1-min frame -> fetch_glm_lightning
        S3 primitives (list + read GLM granules) + ABI MCMIPC C02 visible scan ->
        bin GED + bake purple over grayscale base -> publish_layer; plus
        fetch_glm_lightning (accumulation_window_s) for the standalone GED overlay.
    """
    return await model_glm_lightning_animation(
        bbox=bbox,
        start_utc=start_utc,
        end_utc=end_utc,
        satellite=satellite,
        accumulation_window_s=accumulation_window_s,
        base_band=base_band,
        storm_name=storm_name,
        overlay_standalone_ged=overlay_standalone_ged,
        pipeline_emitter=None,
    )
