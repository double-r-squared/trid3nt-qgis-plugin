"""``model_goes_fire_animation`` workflow -- UNATTENDED GOES fire animation (fire-demo Track A).

A GOES-only fire-animation composer that runs to completion WITHOUT parking the
conversation. NATE was frustrated that the generalized
``model_satellite_fire_animation`` composer STOPS at a bbox/window review gate
and asks the user to confirm the window before fetching. This workflow is the
unattended sibling: given a bbox + a requested window (or "use the most recent
available"), it

  1. reads the SLIDER time index for each requested GOES product
     (``fetch_slider_timestamps`` -- the authoritative availability list),
  2. AUTO-SNAPS the requested window to the nearest AVAILABLE frames (so a window
     that is slightly off from, or wider than, what SLIDER actually carries still
     yields frames -- it never parks asking the user to re-pick the window), and
  3. PROCEEDS straight to fetch + publish: it dispatches the GOES imagery fetcher
     (the blended GeoColor + Fire Temperature composite by default, or a single
     product if only one is requested), emits the frames in the postprocess_flood
     FRAME SHAPE (distinct per-frame keys + shared ``style_preset`` + a "GOES
     <product> step <N> <ISO>" name token + identical bbox) so the existing web
     SequenceScrubber / detectSequentialGroups animate them with ZERO web change,
  4. adds the FIRMS active-fire detections as a co-registered STATIC overlay, and
  5. publishes every layer via TiTiler.

It STOPS ONLY for a real failure: it raises a typed honest error
(``GOESFireAnimEmptyError``) when NOTHING is available -- the SLIDER index is
empty for every requested product, OR every fetched frame was empty/off-grid.
That is the honesty floor: an animation with no frames NEVER reports status=ok.

This is the GOES analogue of postprocess_flood's frame seam (the per-frame COG
contract) chained onto the GOES SLIDER fetchers. It reuses the
``fetch_goes_blend_animation`` / ``fetch_goes_animation`` tools as-is (Track B+C
owns those) and the ``fetch_firms_active_fire`` overlay; it dispatches every
atomic tool through ``TOOL_REGISTRY[name].fn`` (registry-as-source rule) and runs
the heavy emit-free fetch in ``asyncio.to_thread`` (no-loop-blocking norm).

ASCII only.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from grace2_contracts.execution import LayerURI
from grace2_contracts.tool_registry import AtomicToolMetadata

from ..tools import TOOL_REGISTRY, register_tool

if TYPE_CHECKING:
    from ..pipeline_emitter import PipelineEmitter

__all__ = [
    "model_goes_fire_animation",
    "run_model_goes_fire_animation",
    "GOESFireAnimError",
    "GOESFireAnimInputError",
    "GOESFireAnimEmptyError",
    "GOES_FIRE_PRODUCTS",
    "DEFAULT_GOES_WINDOW",
    "_parse_utc",
    "_snap_window_to_available",
    "_resolve_default_window",
]

logger = logging.getLogger("grace2_agent.workflows.model_goes_fire_animation")


# --------------------------------------------------------------------------- #
# Typed errors (FR-AS-11)
# --------------------------------------------------------------------------- #


class GOESFireAnimError(RuntimeError):
    """Base class for model_goes_fire_animation failures."""

    error_code: str = "GOES_FIRE_ANIM_ERROR"
    retryable: bool = False


class GOESFireAnimInputError(GOESFireAnimError):
    """Caller passed a bad product / window / bbox."""

    error_code = "GOES_FIRE_ANIM_INPUT_INVALID"
    retryable = False


class GOESFireAnimEmptyError(GOESFireAnimError):
    """NOTHING is available: no SLIDER frames at all for any requested product,
    or every fetched frame was empty/off-grid (the honesty floor)."""

    error_code = "GOES_FIRE_ANIM_EMPTY"
    retryable = False


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

#: The two GOES SLIDER products this composer animates (geostationary CONUS).
GOES_FIRE_PRODUCTS: tuple[str, ...] = ("geocolor", "fire_temperature")

#: The two GOES products the blend composites (base = true-color GeoColor,
#: overlay = active-fire Fire Temperature). When BOTH are requested (the default)
#: the run FOLDS them into ONE blended scrubber via fetch_goes_blend_animation.
_BLEND_BASE_PRODUCT: str = "geocolor"
_BLEND_FIRE_PRODUCT: str = "fire_temperature"

#: The default GOES animation window length (~6.5h -- the CIRA intra-day loop).
DEFAULT_GOES_WINDOW: timedelta = timedelta(hours=6, minutes=30)

#: How far OUTSIDE the requested window we will reach to snap to the nearest
#: available frame before giving up. A SLIDER index that carries timestamps but
#: NONE inside the requested window is snapped to the nearest available frame(s)
#: within this tolerance, then widened to a default-length window around them, so
#: a slightly-off / stale requested window still animates instead of parking.
_SNAP_TOLERANCE: timedelta = timedelta(days=2)


# --------------------------------------------------------------------------- #
# Window parsing + auto-snap (the core unattended behavior)
# --------------------------------------------------------------------------- #


def _parse_utc(value: Any) -> datetime | None:
    """Parse an ISO-8601 string / datetime -> aware UTC, or None for a falsy value.

    Accepts a trailing 'Z', '+00:00', a space or 'T' separator, and a bare date.
    Raises ``GOESFireAnimInputError`` for an unparseable non-empty value.
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
            raise GOESFireAnimInputError(
                f"could not parse UTC time {value!r}; use ISO-8601 "
                "(e.g. '2026-06-22T13:30:00Z')"
            )
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _resolve_default_window(
    start_dt: datetime | None,
    end_dt: datetime | None,
    discovery_iso: str | None = None,
) -> tuple[datetime, datetime]:
    """Resolve the REQUESTED window from the optional start/end + a discovery floor.

    - Both given: that window verbatim.
    - End only: a ~6.5h window ending at end.
    - Start only: a ~6.5h window starting at start.
    - Neither: a ~6.5h window ending now ("use the most recent available").

    The WFIGS FireDiscoveryDateTime (when present) is a sanity floor: the start
    never precedes it. Pure (no network) -- the REQUESTED window; the auto-snap
    against SLIDER availability happens separately.
    """
    now = datetime.now(timezone.utc)
    if start_dt is not None and end_dt is not None:
        start, end = start_dt, end_dt
    elif end_dt is not None:
        start, end = end_dt - DEFAULT_GOES_WINDOW, end_dt
    elif start_dt is not None:
        start, end = start_dt, start_dt + DEFAULT_GOES_WINDOW
    else:
        start, end = now - DEFAULT_GOES_WINDOW, now

    disc = _parse_utc(discovery_iso) if discovery_iso else None
    if disc is not None and start < disc:
        start = disc
    if start >= end:
        end = start + DEFAULT_GOES_WINDOW
    return start, end


def _snap_window_to_available(
    timestamps_int: list[int],
    start_dt: datetime,
    end_dt: datetime,
) -> tuple[datetime, datetime, list[int]] | None:
    """AUTO-SNAP a requested window to the nearest AVAILABLE SLIDER frames.

    ``timestamps_int`` is the ascending SLIDER ``timestamps_int`` list (14-digit
    YYYYMMDDHHMMSS ints). This is the heart of the unattended behavior: instead of
    parking and asking the user to re-pick the window when the requested span does
    not line up with what SLIDER actually carries, we SNAP the window to the real
    availability and proceed.

    Returns ``(snapped_start, snapped_end, in_window_ts)`` where ``in_window_ts``
    is the ascending list of timestamp ints inside the snapped window, OR ``None``
    when the index is EMPTY (nothing to snap to -> the caller honesty-floors).

    Snapping rules (deterministic, pure -- no network):

    1. Frames already inside [start, end]: keep that window AS-IS (no snap needed)
       -- the common "the window is fine" path.
    2. Frames exist but NONE inside the window: snap to the nearest available
       timestamp to the window (clamp the window onto real data). If that nearest
       frame is within ``_SNAP_TOLERANCE`` of the requested window, widen a
       default-length window around it (anchored so the snapped frame is the
       endpoint nearest the original request) and re-window. This rescues a
       slightly-stale or slightly-off requested window.
    3. Frames exist but ALL are far outside the tolerance: fall back to the most
       recent ``DEFAULT_GOES_WINDOW`` of available frames (best-effort "use the
       most recent available" rather than parking).
    """
    ts = sorted(int(t) for t in timestamps_int)
    if not ts:
        return None

    def _to_dt(t: int) -> datetime:
        s = f"{t:014d}"
        return datetime(
            int(s[0:4]), int(s[4:6]), int(s[6:8]),
            int(s[8:10]), int(s[10:12]), int(s[12:14]),
            tzinfo=timezone.utc,
        )

    in_window = [t for t in ts if start_dt <= _to_dt(t) <= end_dt]
    if in_window:
        # Rule 1: the requested window already lines up with real data.
        return start_dt, end_dt, in_window

    # Rule 2/3: no frame inside the window -> snap to the nearest available.
    # Find the available timestamp nearest the requested window (distance to the
    # interval, 0 if inside -- but we already know none are inside).
    def _dist_to_window(t: int) -> timedelta:
        d = _to_dt(t)
        if d < start_dt:
            return start_dt - d
        return d - end_dt

    nearest = min(ts, key=_dist_to_window)
    nearest_dt = _to_dt(nearest)
    gap = _dist_to_window(nearest)

    if gap <= _SNAP_TOLERANCE:
        # Rule 2: widen a default-length window so the nearest available frame is
        # the endpoint closest to the original request, then re-window.
        if nearest_dt < start_dt:
            # Available data sits BEFORE the request -> nearest is the new end.
            snap_end = nearest_dt
            snap_start = snap_end - DEFAULT_GOES_WINDOW
        else:
            # Available data sits AFTER the request -> nearest is the new start.
            snap_start = nearest_dt
            snap_end = snap_start + DEFAULT_GOES_WINDOW
    else:
        # Rule 3: too far -> the most-recent default window of available frames.
        latest_dt = _to_dt(ts[-1])
        snap_end = latest_dt
        snap_start = latest_dt - DEFAULT_GOES_WINDOW
        logger.info(
            "model_goes_fire_animation: requested window %s..%s is > %s from any "
            "available frame; snapping to the most-recent %s of available data "
            "(%s..%s).",
            start_dt.isoformat(), end_dt.isoformat(), _SNAP_TOLERANCE,
            DEFAULT_GOES_WINDOW, snap_start.isoformat(), snap_end.isoformat(),
        )

    snapped = [t for t in ts if snap_start <= _to_dt(t) <= snap_end]
    if not snapped:
        # Defensive: a default-length window around the nearest frame should
        # always include it; if a sparse index still misses, keep just the
        # nearest frame so the run proceeds with at least one real timestamp.
        snapped = [nearest]
        snap_start = snap_end = nearest_dt
    logger.info(
        "model_goes_fire_animation: auto-snapped requested window %s..%s -> "
        "%s..%s (%d available frame(s)); proceeding WITHOUT a confirm gate.",
        start_dt.isoformat(), end_dt.isoformat(),
        snap_start.isoformat(), snap_end.isoformat(), len(snapped),
    )
    return snap_start, snap_end, snapped


# --------------------------------------------------------------------------- #
# Registry helpers
# --------------------------------------------------------------------------- #


def _registry_fn(name: str) -> Any:
    """Resolve ``name`` -> the registered tool callable (registry-as-source rule)."""
    entry = TOOL_REGISTRY.get(name)
    if entry is None:
        raise GOESFireAnimError(
            f"required atomic tool {name!r} is not registered "
            f"(known: {sorted(TOOL_REGISTRY)[:8]}...)"
        )
    return entry.fn


async def _read_slider_timestamps(product: str, satellite: str, sector: str) -> list[int]:
    """Read the SLIDER availability list for a product (off-loop).

    Reads ``fetch_slider_timestamps`` in ``asyncio.to_thread`` (it does a small
    JSON GET -- still I/O, so it must NOT run on the loop). Returns ``[]`` on any
    upstream hiccup so a single index miss does not crash the whole run (the other
    product, or the honesty floor, still applies).
    """
    try:
        from ..tools._satellite_slider import fetch_slider_timestamps
        from ..tools.fetch_goes_animation import _band_to_slider_product

        slug = _band_to_slider_product(product)
        return await asyncio.to_thread(
            fetch_slider_timestamps, satellite, sector, slug
        )
    except Exception as exc:  # noqa: BLE001 -- one index miss must not sink the run
        logger.warning(
            "model_goes_fire_animation: SLIDER time index for %s (%s/%s) failed "
            "(%s); treating as no availability for this product",
            product, satellite, sector, exc,
        )
        return []


# --------------------------------------------------------------------------- #
# The workflow
# --------------------------------------------------------------------------- #


async def model_goes_fire_animation(
    bbox: tuple[float, float, float, float],
    products: list[str] | None = None,
    satellite: str = "goes-18",
    sector: str = "conus",
    start_utc: str | None = None,
    end_utc: str | None = None,
    discovery_iso: str | None = None,
    overlay_firms: bool = True,
    firms_date: str | None = None,
    *,
    pipeline_emitter: "PipelineEmitter | None" = None,
) -> dict[str, Any]:
    """Animate a GOES fire loop UNATTENDED -- auto-snap the window, fetch, publish.

    Unlike ``model_satellite_fire_animation`` (which STOPS at a bbox/window review
    gate), this composer NEVER parks: it reads the SLIDER availability, auto-snaps
    the requested window onto the nearest available frames, and proceeds straight
    to fetch + publish. It raises ``GOESFireAnimEmptyError`` ONLY when NOTHING is
    available (the honesty floor).

    Args:
        bbox: AOI ``(min_lon, min_lat, max_lon, max_lat)`` EPSG:4326. Required.
        products: GOES products to animate. Default ``["geocolor",
            "fire_temperature"]``; when BOTH are present they are BLENDED into ONE
            composite scrubber (the CIRA look). A single product emits one
            un-blended group. Allowed: "geocolor", "fire_temperature".
        satellite: "goes-18" (West) or "goes-19" (East). Default "goes-18".
        sector: SLIDER sector slug. Default "conus".
        start_utc / end_utc: ISO-8601 UTC window bounds. When omitted, a ~6.5h
            window ending at the most recent available time is used; whatever is
            requested is AUTO-SNAPPED to the nearest available SLIDER frames.
        discovery_iso: optional WFIGS FireDiscoveryDateTime floor (start never
            precedes it).
        overlay_firms: add the FIRMS active-fire detections as a co-registered
            static overlay (default True).
        firms_date: optional historical ``YYYY-MM-DD`` for the FIRMS overlay (the
            past day to overlay); defaults to the snapped-window start day.
        pipeline_emitter: optional live progress emitter.

    Returns:
        ``{status:"ok", bbox, start_utc, end_utc, requested_start_utc,
        requested_end_utc, products, n_frames, n_overlays, snapped, layers:[...],
        message}`` on success. Raises ``GOESFireAnimEmptyError`` when nothing is
        available.
    """
    # --- validate inputs (typed errors, not crashes). ---
    if not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
        raise GOESFireAnimInputError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    try:
        q_bbox = (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
    except (TypeError, ValueError) as exc:
        raise GOESFireAnimInputError(f"bbox values must be numeric: {bbox!r}") from exc

    products = list(products) if products else list(GOES_FIRE_PRODUCTS)
    for p in products:
        if p not in GOES_FIRE_PRODUCTS:
            raise GOESFireAnimInputError(
                f"product {p!r} not in {list(GOES_FIRE_PRODUCTS)} "
                "(GOES geostationary only)"
            )
    if not products:
        raise GOESFireAnimInputError("at least one GOES product is required")
    if satellite not in ("goes-18", "goes-19"):
        raise GOESFireAnimInputError(
            f"satellite {satellite!r} not in ('goes-18', 'goes-19')"
        )

    # --- resolve the REQUESTED window. ---
    req_start = _parse_utc(start_utc)
    req_end = _parse_utc(end_utc)
    if req_start is not None and req_end is not None and req_start >= req_end:
        raise GOESFireAnimInputError(
            f"start_utc ({req_start.isoformat()}) must be before end_utc "
            f"({req_end.isoformat()})"
        )
    requested_start, requested_end = _resolve_default_window(
        req_start, req_end, discovery_iso
    )

    # --- Emit the AOI snap-to map zoom EARLY so the user sees WHERE first. ---
    if pipeline_emitter is not None:
        try:
            await pipeline_emitter.emit_map_command(
                "zoom-to", {"bbox": list(q_bbox)}
            )
        except Exception as exc:  # noqa: BLE001 -- a UX verb, never a gate
            logger.warning(
                "model_goes_fire_animation: early AOI zoom-to emit failed (%s)", exc
            )

    # --- AUTO-SNAP the window to the nearest AVAILABLE SLIDER frames. ---
    #
    # Read availability for every requested product (the blended path anchors on
    # the GeoColor base). The snap uses the UNION of available timestamps so the
    # window lands on a real frame for at least one product; the per-product
    # fetchers re-window independently downstream.
    if pipeline_emitter is not None:
        snap_step = await pipeline_emitter.add_step(
            name="Snap window to available GOES frames",
            tool_name="fetch_slider_timestamps",
        )
        await pipeline_emitter.mark_running(snap_step)
    else:
        snap_step = None

    union_ts: set[int] = set()
    for product in products:
        ts = await _read_slider_timestamps(product, satellite, sector)
        union_ts.update(int(t) for t in ts)

    snapped = _snap_window_to_available(
        sorted(union_ts), requested_start, requested_end
    )
    if snapped is None:
        # NOTHING available across every requested product -> honesty floor.
        if pipeline_emitter is not None and snap_step is not None:
            await pipeline_emitter.mark_failed(
                snap_step,
                GOESFireAnimEmptyError.error_code,
                "no SLIDER frames available for any requested GOES product",
            )
        raise GOESFireAnimEmptyError(
            f"no GOES SLIDER frames are available for {satellite}/{sector} "
            f"products {products} -- nothing to animate. The SLIDER time index "
            "returned no timestamps for any requested product."
        )
    snap_start, snap_end, _in_window = snapped
    if pipeline_emitter is not None and snap_step is not None:
        await pipeline_emitter.mark_complete(snap_step)

    start_iso = snap_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_iso = snap_end.strftime("%Y-%m-%dT%H:%M:%SZ")
    did_snap = (snap_start != requested_start) or (snap_end != requested_end)

    # --- Dispatch the GOES imagery fetcher(s) per the plan (each off-loop). ---
    #
    # Default (both products) -> ONE blended GeoColor + Fire Temperature scrubber
    # via fetch_goes_blend_animation. A single product -> one un-blended group via
    # fetch_goes_animation.
    all_layers: list[LayerURI] = []
    per_product_frames: dict[str, int] = {}
    blend = (
        _BLEND_BASE_PRODUCT in products and _BLEND_FIRE_PRODUCT in products
    )

    if blend:
        frames = await _dispatch_goes_blend(
            q_bbox, satellite, sector, start_iso, end_iso, pipeline_emitter
        )
        all_layers.extend(frames)
        for p in (_BLEND_BASE_PRODUCT, _BLEND_FIRE_PRODUCT):
            per_product_frames[p] = len(frames)
    else:
        for product in products:
            frames = await _dispatch_single_goes(
                q_bbox, product, satellite, sector, start_iso, end_iso,
                pipeline_emitter,
            )
            per_product_frames[product] = len(frames)
            all_layers.extend(frames)

    # --- FIRMS co-registered STATIC overlay (best-effort). ---
    overlay_layers: list[LayerURI] = []
    if overlay_firms:
        overlay_date = firms_date or snap_start.strftime("%Y-%m-%d")
        firms_layer = await _safe_overlay_firms(q_bbox, overlay_date, pipeline_emitter)
        if firms_layer is not None:
            overlay_layers.append(firms_layer)

    # --- Publish every layer via TiTiler (off-loop). ---
    published = await _publish_layers(all_layers + overlay_layers, pipeline_emitter)

    n_frames = len(all_layers)
    # Honesty floor: timestamps existed but every fetched frame was empty/off-grid.
    if n_frames == 0:
        raise GOESFireAnimEmptyError(
            f"GOES SLIDER frames were available for {satellite}/{sector} over "
            f"{start_iso}..{end_iso} but every fetched frame was empty/off-grid "
            "for the AOI -- nothing to animate. Adjust the bbox (it may fall off "
            "the imagery grid) and re-run."
        )

    return {
        "status": "ok",
        "bbox": list(q_bbox),
        "satellite": satellite,
        "sector": sector,
        "start_utc": start_iso,
        "end_utc": end_iso,
        "requested_start_utc": requested_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "requested_end_utc": requested_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "snapped": did_snap,
        "products": products,
        "frame_counts": per_product_frames,
        "n_frames": n_frames,
        "n_overlays": len(overlay_layers),
        "layers": [
            _layer_summary(layer, published) for layer in all_layers + overlay_layers
        ],
        "message": (
            (
                f"Animated {n_frames} blended GeoColor + Fire Temperature frame(s) "
                f"(one scrubber) with {len(overlay_layers)} FIRMS overlay(s) over "
                f"{start_iso}..{end_iso}"
                + (
                    " (auto-snapped from the requested window to the nearest "
                    "available frames)."
                    if did_snap
                    else "."
                )
            )
            if blend
            else (
                f"Animated {n_frames} frame(s) across {len(products)} GOES "
                f"product(s) with {len(overlay_layers)} FIRMS overlay(s) over "
                f"{start_iso}..{end_iso}"
                + (
                    " (auto-snapped to the nearest available frames)."
                    if did_snap
                    else "."
                )
            )
        ),
    }


# --------------------------------------------------------------------------- #
# Dispatch helpers (each runs the heavy emit-free sync fetcher in
# asyncio.to_thread so the asyncio loop / WS heartbeat never blocks).
# --------------------------------------------------------------------------- #


async def _dispatch_goes_blend(
    bbox: tuple[float, float, float, float],
    satellite: str,
    sector: str,
    start_iso: str,
    end_iso: str,
    pipeline_emitter: "PipelineEmitter | None",
) -> list[LayerURI]:
    """Dispatch fetch_goes_blend_animation -> ONE blended GeoColor+Fire Temperature group.

    The heavy per-frame fetch + raster blend runs in asyncio.to_thread (off-loop).
    A failure / empty run returns ``[]`` (the caller honesty-floors the whole run).
    """
    fetcher_name = "fetch_goes_blend_animation"
    fetcher = _registry_fn(fetcher_name)
    if pipeline_emitter is not None:
        step = await pipeline_emitter.add_step(
            name="Fetch GeoColor + Fire Temperature blended frames",
            tool_name=fetcher_name,
        )
        await pipeline_emitter.mark_running(step)
    else:
        step = None
    try:
        frames = await asyncio.to_thread(
            fetcher, bbox, satellite, sector, start_iso, end_iso
        )
    except Exception as exc:  # noqa: BLE001 -- an empty blend must not crash the run
        if pipeline_emitter is not None and step is not None:
            await pipeline_emitter.mark_failed(
                step, "IMAGERY_FETCH_FAILED", f"{fetcher_name} failed: {exc}"
            )
        logger.warning(
            "model_goes_fire_animation: %s produced no blended frames (%s)",
            fetcher_name, exc,
        )
        return []
    frame_list = list(frames) if isinstance(frames, list) else [frames]
    if pipeline_emitter is not None and step is not None:
        await pipeline_emitter.mark_complete(step)
    return frame_list


async def _dispatch_single_goes(
    bbox: tuple[float, float, float, float],
    product: str,
    satellite: str,
    sector: str,
    start_iso: str,
    end_iso: str,
    pipeline_emitter: "PipelineEmitter | None",
) -> list[LayerURI]:
    """Dispatch fetch_goes_animation for ONE GOES product (un-blended single group)."""
    fetcher_name = "fetch_goes_animation"
    fetcher = _registry_fn(fetcher_name)
    if pipeline_emitter is not None:
        step = await pipeline_emitter.add_step(
            name=f"Fetch {product} frames", tool_name=fetcher_name
        )
        await pipeline_emitter.mark_running(step)
    else:
        step = None
    try:
        frames = await asyncio.to_thread(
            fetcher, bbox, product, satellite, sector, start_iso, end_iso
        )
    except Exception as exc:  # noqa: BLE001 -- one empty product must not sink the rest
        if pipeline_emitter is not None and step is not None:
            await pipeline_emitter.mark_failed(
                step, "IMAGERY_FETCH_FAILED", f"{fetcher_name} failed: {exc}"
            )
        logger.warning(
            "model_goes_fire_animation: %s for product=%s produced no frames (%s)",
            fetcher_name, product, exc,
        )
        return []
    frame_list = list(frames) if isinstance(frames, list) else [frames]
    if pipeline_emitter is not None and step is not None:
        await pipeline_emitter.mark_complete(step)
    return frame_list


# --------------------------------------------------------------------------- #
# Overlay + publish helpers
# --------------------------------------------------------------------------- #


async def _safe_overlay_firms(
    bbox: tuple[float, float, float, float],
    date_iso: str,
    pipeline_emitter: "PipelineEmitter | None",
) -> LayerURI | None:
    """Fetch the FIRMS historical-date hot-pixel overlay (best-effort, off-loop)."""
    if pipeline_emitter is not None:
        step = await pipeline_emitter.add_step(
            name="Overlay FIRMS hot pixels", tool_name="fetch_firms_active_fire"
        )
        await pipeline_emitter.mark_running(step)
    else:
        step = None
    try:
        firms_fn = _registry_fn("fetch_firms_active_fire")
        # VIIRS_NOAA20_NRT is the JPSS sibling; date forces the single past day.
        layer = await asyncio.to_thread(
            firms_fn, bbox, 1, "VIIRS_NOAA20_NRT", date_iso
        )
    except Exception as exc:  # noqa: BLE001 -- overlay is non-fatal
        logger.warning("model_goes_fire_animation: FIRMS overlay failed (%s)", exc)
        if pipeline_emitter is not None and step is not None:
            await pipeline_emitter.mark_failed(step, "FIRMS_OVERLAY_FAILED", str(exc))
        return None
    if pipeline_emitter is not None and step is not None:
        await pipeline_emitter.mark_complete(step)
    return layer if isinstance(layer, LayerURI) else None


async def _publish_layers(
    layers: list[LayerURI],
    pipeline_emitter: "PipelineEmitter | None",
) -> dict[str, str]:
    """Publish each layer via publish_layer (TiTiler) in asyncio.to_thread.

    Returns a map ``layer_id -> published WMS url`` for successfully-published
    layers. Publish failures are non-fatal (the COG/FGB still exists at its cache
    URI); they are logged and skipped so a publish hiccup does not sink the whole
    animation.
    """
    published: dict[str, str] = {}
    try:
        publish_fn = _registry_fn("publish_layer")
    except GOESFireAnimError:
        logger.warning(
            "model_goes_fire_animation: publish_layer not registered; "
            "skipping publish"
        )
        return published
    for layer in layers:
        try:
            url = await asyncio.to_thread(
                publish_fn,
                layer.uri,
                layer.layer_id,
                layer.style_preset,
            )
            if isinstance(url, str) and url:
                published[layer.layer_id] = url
        except Exception as exc:  # noqa: BLE001 -- publish is non-fatal
            logger.warning(
                "model_goes_fire_animation: publish_layer(%s) failed (%s)",
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
    name="run_model_goes_fire_animation",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)


@register_tool(_RUN_METADATA)
async def run_model_goes_fire_animation(
    bbox: tuple[float, float, float, float],
    products: list[str] | None = None,
    satellite: str = "goes-18",
    sector: str = "conus",
    start_utc: str | None = None,
    end_utc: str | None = None,
    discovery_iso: str | None = None,
    overlay_firms: bool = True,
    firms_date: str | None = None,
    # job-0164: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Animate a GOES fire loop UNATTENDED (auto-snap the window, fetch, publish -- NO confirm gate).

    Recreates a CIRA-style intra-day GOES fire animation as a real, in-app,
    scrubbable layer that runs to completion WITHOUT parking the conversation to
    ask the user to confirm the time window. Given an AOI bbox and a window (or
    "use the most recent available"), it reads the SLIDER availability,
    AUTO-SNAPS the window to the nearest AVAILABLE frames, then fetches + publishes
    the GOES-18 GeoColor + Fire Temperature imagery (BLENDED into one composite
    scrubber by default -- the CIRA "GeoColor and Fire Temperature" look), and
    overlays the FIRMS active-fire detections as a co-registered static layer.

    When to use:
        - "Animate the GOES fire loop over this AOI and window and just run it"
          (no parking to confirm the window).
        - "Recreate the CIRA GOES-18 GeoColor + Fire Temperature animation over
          this bbox" when the AOI is already known.
        - The unattended sibling of run_model_satellite_fire_animation: pick THIS
          when the AOI bbox is already resolved and the user does NOT want a
          window-confirm gate.

    When NOT to use:
        - The AOI is NOT yet known and you need the news/incident lookup + a
          bbox/window REVIEW gate first (use run_model_satellite_fire_animation).
        - A multi-day polar VIIRS Day Fire timelapse
          (run_model_satellite_fire_animation with products=["day_fire"]).
        - A single most-recent satellite frame (fetch_goes_satellite) or active-
          fire detections only (fetch_firms_active_fire).

    Params:
        bbox: AOI [min_lon, min_lat, max_lon, max_lat] EPSG:4326. Required.
        products: GOES products. Default ["geocolor", "fire_temperature"] -> ONE
            blended scrubber. A single product (e.g. ["geocolor"]) -> one
            un-blended group.
        satellite: "goes-18" (West) or "goes-19" (East). Default "goes-18".
        sector: SLIDER sector slug. Default "conus".
        start_utc / end_utc: ISO-8601 UTC window bounds. Omit for the most-recent
            ~6.5h available. Whatever is requested is AUTO-SNAPPED to the nearest
            available SLIDER frames -- the workflow never stops to ask.
        discovery_iso: optional WFIGS discovery-time floor.
        overlay_firms: add the FIRMS hot-pixel overlay (default true).
        firms_date: optional historical YYYY-MM-DD for the FIRMS overlay.

    Returns:
        A dict with status="ok", the AOI bbox, the (snapped) window, the requested
        window, a "snapped" flag, the per-product frame counts, the published layer
        summaries, and frame/overlay counts. Raises GOES_FIRE_ANIM_EMPTY only when
        NOTHING is available (the honesty floor).

    Cross-tool dependencies:
        Step chain: fetch_slider_timestamps (availability + auto-snap) ->
        fetch_goes_blend_animation / fetch_goes_animation (per frame) ->
        fetch_firms_active_fire (historical-date overlay) -> publish_layer.
    """
    return await model_goes_fire_animation(
        bbox=bbox,
        products=products,
        satellite=satellite,
        sector=sector,
        start_utc=start_utc,
        end_utc=end_utc,
        discovery_iso=discovery_iso,
        overlay_firms=overlay_firms,
        firms_date=firms_date,
        pipeline_emitter=None,
    )
