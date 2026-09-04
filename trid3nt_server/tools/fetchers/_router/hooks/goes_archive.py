"""goes_archive frames hooks: the netcdf_cf_object per-frame mode.

Folds fetch_goes_archive_animation + fetch_goes_active_fire onto shape:
animation_frames. The router owns the per-frame read_through loop + honesty floor +
LayerURI emission; these two hooks own the source-specific steps over the shared
``imagery._goes_archive_core`` substrate (S3 window list + CF-scaled MCMIPC netCDF
band read + Fire-Temperature / true-color / hotspot / baked composite):

- ``frames_plan`` -- resolve the window, list the in-window ``ABI-L2-MCMIPC`` S3
  keys (anonymous public archive), even-subsample to the source cap, and build the
  ordered per-frame plans. Each frame's ``cache_params`` are byte-identical to the
  twin's per-frame params (so the fold reuses any already-cached frame); the opaque
  MCMIPC S3 key + the raw (unrounded) fetch args ride in the out-of-cache-key
  ``fetch_context``.
- ``frame_bytes`` -- download ONE MCMIPC netCDF and composite the requested product
  to COG bytes via ``core._fetch_archive_frame_cog_bytes``. Raises
  :class:`FrameDegraded` to skip a single empty / off-disk / upstream-failed frame
  (honesty floor: the executor records + drops it, and raises the typed EMPTY only
  when EVERY frame degrades).

Two twins, ONE hook pair: the ``ingest.archive.mode`` flag selects the ``full``
band-selectable archive-animation surface vs the ``hotspots`` split-window
active-fire surface (fixed ``fire_hotspots`` band, distinct cap / default window /
cache-param shape / label). ASCII only.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ...imagery import _goes_archive_core as core
from ...imagery._goes_common import _normalize_satellite
from ..errors import router_empty_error, router_input_error, router_upstream_error
from . import FrameDegraded, FramePlan, frame_windows, register_hook

logger = logging.getLogger(
    "trid3nt_server.tools.fetchers._router.hooks.goes_archive"
)

__all__ = ["frames_plan", "frame_bytes"]


# --------------------------------------------------------------------------- #
# Pure helpers.
# --------------------------------------------------------------------------- #


def _round_bbox(bbox: Any) -> tuple[float, float, float, float]:
    """Quantize the (already router-validated) bbox to 6dp for cache-key stability."""
    return tuple(round(float(v), core._BBOX_QUANTIZE_DP) for v in bbox)  # type: ignore[return-value]


def _resolve_satellite(spec: SourceSpec, satellite: Any) -> str:
    """Normalize the GOES spelling zoo -> canonical bird, then gate to the archive set.

    ``_normalize_satellite`` raises the loud GOESInputError for a genuinely-unknown
    bird; a valid GOES bird not in the raw-archive set raises the source's typed
    ``*_INPUT_INVALID`` (byte-identical to the twins' GOESArchiveInputError, which
    both stamped ``GOES_ARCHIVE_INPUT_INVALID``).
    """
    sat = _normalize_satellite(str(satellite))
    if sat not in core.GOES_ARCHIVE_SATELLITES:
        raise router_input_error(
            spec.error_code_prefix,
            f"unknown satellite={sat!r}; allowed: {list(core.GOES_ARCHIVE_SATELLITES)}",
            spec.input_error_suffix,
        )
    return sat


def _resolve_band(spec: SourceSpec, band: Any) -> str:
    """Alias-normalize (natural_color / geocolor_raw -> true_color) + gate to ARCHIVE_BANDS."""
    if isinstance(band, str):
        band = core._BAND_ALIASES.get(band, band)
    if band not in core.ARCHIVE_BANDS:
        raise router_input_error(
            spec.error_code_prefix,
            f"unknown band/product={band!r}; the raw-archive path supports "
            f"{list(core.ARCHIVE_BANDS)} (proprietary CIRA GeoColor -- use "
            "fetch_goes_animation for the recent GeoColor loop)",
            spec.input_error_suffix,
        )
    return band


def _resolve_window(
    spec: SourceSpec, params: dict[str, Any], default_window_min: int
) -> tuple[datetime, datetime]:
    """Default the window to the most-recent ``default_window_min`` minutes ending now."""
    now = datetime.now(timezone.utc)
    end_raw = params.get("end_utc")
    start_raw = params.get("start_utc")
    end_dt = core._parse_utc(end_raw) if end_raw else now
    start_dt = (
        core._parse_utc(start_raw)
        if start_raw
        else (end_dt - timedelta(minutes=default_window_min))
    )
    if start_dt >= end_dt:
        raise router_input_error(
            spec.error_code_prefix,
            f"start_utc ({start_dt.isoformat()}) must be before end_utc "
            f"({end_dt.isoformat()})",
            spec.input_error_suffix,
        )
    return start_dt, end_dt


def _thresh(value: Any, fallback: float) -> float:
    """Resolve a threshold param (None / absent -> the module default float)."""
    if value is None:
        return float(fallback)
    return float(value)


# --------------------------------------------------------------------------- #
# frames_plan: the pre-loop resolve.
# --------------------------------------------------------------------------- #


@register_hook("goes_archive.frames_plan")
def frames_plan(spec: SourceSpec, params: dict[str, Any]) -> list[FramePlan]:
    """List + window + subsample the MCMIPC archive keys into ordered per-frame plans."""
    sc = spec.error_code_prefix
    cfg = (spec.ingest or {}).get("archive", {})
    mode = cfg.get("mode", "full")
    max_frames = int(cfg.get("max_frames", core.MAX_ARCHIVE_FRAMES))
    default_window_min = int(cfg.get("default_window_minutes", 390))

    q_bbox = _round_bbox(params["bbox"])
    satellite = _resolve_satellite(spec, params.get("satellite", "goes-18"))
    start_dt, end_dt = _resolve_window(spec, params, default_window_min)

    # List the in-window MCMIPC keys (anonymous public S3), map twin error types.
    try:
        pairs = core._list_archive_keys_in_window(satellite, start_dt, end_dt)
    except core.GOESArchiveInputError as exc:
        raise router_input_error(sc, str(exc), spec.input_error_suffix) from exc
    except core.GOESArchiveUpstreamError as exc:
        raise router_upstream_error(sc, str(exc)) from exc
    if not pairs:
        raise router_empty_error(
            sc,
            f"no MCMIPC frames in the noaa-{satellite.replace('-', '')} archive for "
            f"window {core._iso_z(start_dt)}..{core._iso_z(end_dt)} -- the date may "
            f"pre-date the {satellite} operational record or fall in an ingest gap",
            spec.empty_error_suffix,
        )
    keys_only = [k for _, k in pairs]
    kept = set(core._select_window_keys(keys_only, cap=max_frames))
    frames = [(t, k) for (t, k) in pairs if k in kept]

    sat_label = satellite.upper()

    if mode == "hotspots":
        # fetch_goes_active_fire surface: fixed fire_hotspots band, own cache params.
        af_c07 = round(_thresh(params.get("bt_c07_min_k"), core.FIRE_BT_C07_MIN_K), 3)
        af_diff = round(_thresh(params.get("bt_diff_min_k"), core.FIRE_BT_DIFF_MIN_K), 3)
        plans: list[FramePlan] = []
        windows = frame_windows([core._iso_z(t) for t, _key in frames])
        for frame_no, (t, key) in enumerate(frames, start=1):
            iso = core._iso_z(t)
            ts_tag = t.strftime("%Y%m%d%H%M%S")
            valid_from, valid_to = windows[frame_no - 1]
            plans.append(
                FramePlan(
                    cache_params={
                        "bbox": list(q_bbox),
                        "product": "fire_hotspots",
                        "satellite": satellite,
                        "ts_start": ts_tag,
                        "bt_c07_min_k": af_c07,
                        "bt_diff_min_k": af_diff,
                        "tool": "fetch_goes_active_fire",
                    },
                    name=f"GOES Active Fire step {frame_no} {iso} ({sat_label})",
                    layer_id=f"goes-activefire-{ts_tag}-{q_bbox[0]:.3f}-{q_bbox[1]:.3f}",
                    bbox=q_bbox,
                    valid_from=valid_from,
                    valid_to=valid_to,
                    fetch_context={
                        "key": key,
                        "band": "fire_hotspots",
                        "bt_c07_min_k": af_c07,
                        "bt_diff_min_k": af_diff,
                        "res_deg": core._OUT_RES_DEG,
                    },
                    style={"kind": "continuous"},
                )
            )
        return plans

    # Default (mode == "full"): the band-selectable archive-animation surface.
    band = _resolve_band(spec, params.get("band", "fire_temperature"))
    bt_c07 = _thresh(params.get("bt_c07_min_k"), core.FIRE_BT_C07_MIN_K)
    bt_diff = _thresh(params.get("bt_diff_min_k"), core.FIRE_BT_DIFF_MIN_K)
    tcr = params.get("true_color_res_deg")
    res_deg = core._resolve_res_deg(band, float(tcr) if tcr is not None else None)

    product_label = core._PRODUCT_LABELS[band]
    product_slug = core._PRODUCT_ID_SLUGS[band]

    plans = []
    windows = frame_windows([core._iso_z(t) for t, _key in frames])
    for frame_no, (t, key) in enumerate(frames, start=1):
        iso = core._iso_z(t)
        ts_tag = t.strftime("%Y%m%d%H%M%S")
        valid_from, valid_to = windows[frame_no - 1]
        cache_params: dict[str, Any] = {
            "bbox": list(q_bbox),
            "product": band,
            "satellite": satellite,
            "ts_start": ts_tag,
            "gamma": 1,
            "res_deg": round(res_deg, 6),
        }
        if band in ("fire_hotspots", "fire_baked"):
            cache_params["bt_c07_min_k"] = round(bt_c07, 3)
            cache_params["bt_diff_min_k"] = round(bt_diff, 3)
        plans.append(
            FramePlan(
                cache_params=cache_params,
                name=f"GOES {product_label} step {frame_no} {iso} ({sat_label})",
                layer_id=(
                    f"goes-arch-{product_slug}-{ts_tag}-"
                    f"{q_bbox[0]:.3f}-{q_bbox[1]:.3f}"
                ),
                bbox=q_bbox,
                valid_from=valid_from,
                valid_to=valid_to,
                fetch_context={
                    "key": key,
                    "band": band,
                    "bt_c07_min_k": bt_c07,
                    "bt_diff_min_k": bt_diff,
                    "res_deg": res_deg,
                },
                style={"kind": "continuous"},
            )
        )
    return plans


# --------------------------------------------------------------------------- #
# frame_bytes: the per-frame COG builder.
# --------------------------------------------------------------------------- #


@register_hook("goes_archive.frame_bytes")
def frame_bytes(spec: SourceSpec, params: dict[str, Any], frame: FramePlan) -> bytes:
    """Download ONE MCMIPC netCDF -> the requested product COG bytes (via the core builder).

    Raises :class:`FrameDegraded` for an empty / off-disk / upstream-failed single
    frame (the executor records + drops it; the honesty floor raises the typed EMPTY
    only when EVERY frame degrades) -- byte-for-byte the twins' per-frame skip.
    """
    ctx = frame.fetch_context
    satellite = frame.cache_params["satellite"]
    key = ctx["key"]
    bbox = tuple(frame.cache_params["bbox"])  # type: ignore[assignment]
    try:
        return core._fetch_archive_frame_cog_bytes(
            satellite,
            key,
            bbox,
            ctx["band"],
            ctx["bt_c07_min_k"],
            ctx["bt_diff_min_k"],
            ctx["res_deg"],
        )
    except (core.GOESArchiveEmptyError, core.GOESArchiveUpstreamError) as exc:
        raise FrameDegraded(str(exc)) from exc
