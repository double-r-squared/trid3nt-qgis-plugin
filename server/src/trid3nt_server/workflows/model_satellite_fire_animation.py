"""``model_satellite_fire_animation`` workflow -- satellite fire-animation composer (fire demos S5/J5, generalized).

ONE generalized composer for BOTH fire-animation demos (GOES geostationary +
JPSS/VIIRS polar). It chains:

    geocode_location(incident_name)                -> a place bbox (precise OR a
                                                      coarse state-level snap)
    fetch_wfigs_incident(name [, state])           -> ADDITIVE context only: an
                                                      authoritative point + bbox
                                                      WHEN one is on record. It
                                                      is NEVER a gate -- a no-
                                                      match does not stop the run.
      -> if NEITHER a precise place NOR an incident pins a TIGHT AOI, LOCALIZE
         FROM THE DATA: fetch_firms_active_fire over the (broad) region + window,
         cluster the hot pixels, and derive a TIGHT AOI bbox from the densest
         cluster -- the hotspots ARE the fire (NATE: "recreate" = BUILD from OUR
         endpoints, never force a named external record).
      -> emit the AOI bbox + a snap-to-AOI map zoom EARLY -- BEFORE the review
         gate -- so the user sees WHERE first.
      -> derive the (start_utc, end_utc) window
      -> peek the SLIDER frame list (NO imagery fetched yet)
      -> STOP at a bbox/window REVIEW gate (review-gated, like
         model_news_event_ingest): return the AOI bbox + the planned frame list
         + a human-readable summary so the user can SEE + ADJUST the bbox and the
         window BEFORE all frames are fetched (the #154 confirm / granularity-
         gate philosophy, applied at the workflow layer).
    -- on confirm=True --
      -> dispatch the RIGHT imagery fetcher per path via the TOOL_REGISTRY, each
         run in asyncio.to_thread (NEVER block the asyncio loop):
           * GOES (default): fetch_goes_blend_animation -- pulls BOTH co-temporal
             GeoColor + Fire Temperature frames per timestep and BLENDS them into
             ONE composite RGB frame (GeoColor base + active-fire glow, the CIRA
             "GeoColor and Fire Temperature" look) -> ONE scrubber group, not two.
           * GOES (single product requested): fetch_goes_animation for that one
             product -> one un-blended group.
           * JPSS polar: fetch_viirs_day_fire -> one Day Fire group.
      -> the fetchers emit per-frame LayerURIs in the postprocess_flood SHAPE
         (distinct keys + shared style_preset + a '<PRODUCT> step <N> <ISO-time>
         (<sat>)' NAME token + identical bbox), so detectSequentialGroups +
         SequenceScrubber animate them with NO web change
      -> overlay fetch_firms_active_fire (historical date) + fetch_nifc_fire_
         perimeters as static co-registered layers
      -> publish every layer via publish_layer (TiTiler) in asyncio.to_thread

Honesty floor: a run that produced NO imagery frames does NOT report status=ok --
it returns ``status="empty"`` with an honest message. The imagery is the real
CIRA SLIDER product at the real cadence; georeferencing is the approximate
sector-extent mapping documented in tools/_satellite_slider.py.

Registry discipline (kickoff hard rule): every atomic tool is dispatched via
``TOOL_REGISTRY[name].fn`` -- never imported and called directly.

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
    "model_satellite_fire_animation",
    "run_model_satellite_fire_animation",
    "SatelliteFireAnimationError",
    "SatelliteFireAnimationInputError",
    "SUPPORTED_PRODUCTS",
    "GOES_PRODUCTS",
    "VIIRS_PRODUCTS",
    "_product_to_fetcher",
    "_default_window_for_product",
    "_compose_review_text",
    "_geocode_is_coarse",
    "_read_firms_points",
    "_densest_hotspot_bbox",
    "_localize_from_firms",
]

logger = logging.getLogger("trid3nt_server.workflows.model_satellite_fire_animation")


# --------------------------------------------------------------------------- #
# Typed errors (FR-AS-11)
# --------------------------------------------------------------------------- #


class SatelliteFireAnimationError(RuntimeError):
    """Base class for model_satellite_fire_animation failures."""

    error_code: str = "SAT_FIRE_ANIM_ERROR"
    retryable: bool = False


class SatelliteFireAnimationInputError(SatelliteFireAnimationError):
    """Caller passed a bad product / window / incident name."""

    error_code = "SAT_FIRE_ANIM_INPUT_INVALID"
    retryable = False


# --------------------------------------------------------------------------- #
# Product routing
# --------------------------------------------------------------------------- #

#: GOES geostationary products -> fetch_goes_animation.
GOES_PRODUCTS: tuple[str, ...] = ("geocolor", "fire_temperature")

#: The two GOES products the blend composites (base = true-color GeoColor,
#: overlay = active-fire Fire Temperature). When BOTH are requested (the default)
#: the GOES path folds them into ONE blended scrubber via fetch_goes_blend_
#: animation instead of emitting two separate per-product groups.
_BLEND_BASE_PRODUCT: str = "geocolor"
_BLEND_FIRE_PRODUCT: str = "fire_temperature"

#: JPSS/VIIRS polar products -> fetch_viirs_day_fire.
VIIRS_PRODUCTS: tuple[str, ...] = ("day_fire",)

SUPPORTED_PRODUCTS: tuple[str, ...] = GOES_PRODUCTS + VIIRS_PRODUCTS

#: Cap on the number of per-day FIRMS queries the data-driven localization runs
#: across a window (a VIIRS demo can request a 4-day window; capping keeps the
#: fallback cheap while still pooling enough hot pixels to find the fire).
_FIRMS_LOCALIZE_MAX_DAYS: int = 6

#: Continental-US fallback region used to bound the FIRMS data search only when
#: NEITHER a geocode NOR a WFIGS incident NOR an explicit bbox produced any
#: region at all (a degenerate input). It is a search region, never the AOI: the
#: hotspot cluster derived inside it is the AOI.
_CONUS_FALLBACK_BBOX: tuple[float, float, float, float] = (
    -125.0,
    24.0,
    -66.0,
    50.0,
)


def _product_to_fetcher(product: str) -> str:
    """Map a product to the registered imagery-fetcher tool name.

    GOES geostationary products (geocolor / fire_temperature) route to
    ``fetch_goes_animation``; the JPSS/VIIRS polar Day Fire product routes to
    ``fetch_viirs_day_fire``. Raises ``SatelliteFireAnimationInputError`` for an
    unknown product.
    """
    if product in GOES_PRODUCTS:
        return "fetch_goes_animation"
    if product in VIIRS_PRODUCTS:
        return "fetch_viirs_day_fire"
    raise SatelliteFireAnimationInputError(
        f"unknown product={product!r}; allowed: {list(SUPPORTED_PRODUCTS)}"
    )


def _is_polar_product(product: str) -> bool:
    return product in VIIRS_PRODUCTS


# --------------------------------------------------------------------------- #
# Window derivation
# --------------------------------------------------------------------------- #


def _parse_utc(value: Any) -> datetime | None:
    """Parse an ISO-8601 string / datetime -> aware UTC, or None for a falsy value."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
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
            raise SatelliteFireAnimationInputError(
                f"could not parse UTC time {value!r}; use ISO-8601 "
                "(e.g. '2026-06-22T13:30:00Z')"
            )
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _default_window_for_product(
    product: str,
    discovery_iso: str | None,
    end_utc: datetime | None,
) -> tuple[datetime, datetime]:
    """Derive a (start, end) window from the product family + the discovery floor.

    - GOES (intra-day): a ~6.5h window ending at ``end`` (default: the discovery
      day's ~20:00Z, else now), the CIRA loop length.
    - VIIRS (multi-day): a 4-day window ending at ``end`` (default: now).

    The WFIGS FireDiscoveryDateTime, when present, is the sanity floor: the start
    never precedes it. ``end`` defaults to now when unspecified.
    """
    now = datetime.now(timezone.utc)
    end = end_utc or now
    if _is_polar_product(product):
        start = end - timedelta(days=4)
    else:
        start = end - timedelta(hours=6, minutes=30)
    disc = _parse_utc(discovery_iso) if discovery_iso else None
    if disc is not None and start < disc:
        start = disc
    if start >= end:
        # Degenerate floor (discovery after end): widen the end past the floor.
        end = start + (timedelta(days=4) if _is_polar_product(product) else timedelta(hours=6, minutes=30))
    return start, end


# --------------------------------------------------------------------------- #
# Review-text composition (deterministic)
# --------------------------------------------------------------------------- #


def _compose_review_text(
    incident_name: str,
    bbox: tuple[float, float, float, float],
    products: list[str],
    start: datetime,
    end: datetime,
    frame_counts: dict[str, int],
) -> str:
    """Build the human-readable bbox/window REVIEW summary (deterministic)."""
    lines: list[str] = []
    lines.append(f"Satellite fire-animation plan -- {incident_name}")
    lines.append(
        f"AOI bbox: ({bbox[0]:.4f}, {bbox[1]:.4f}, {bbox[2]:.4f}, {bbox[3]:.4f}) EPSG:4326"
    )
    lines.append(
        f"Time window (UTC): {start.strftime('%Y-%m-%dT%H:%M:%SZ')} "
        f"-> {end.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    )
    for product in products:
        n = frame_counts.get(product, 0)
        lines.append(f"  - {product}: {n} frame(s) planned")
    lines.append(
        "Review the AOI bbox and the time window before all frames are fetched; "
        "adjust them and re-run, or confirm to fetch + animate."
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Registry helpers
# --------------------------------------------------------------------------- #


def _registry_fn(name: str) -> Any:
    """Resolve ``name`` -> the registered tool callable (registry-as-source rule)."""
    entry = TOOL_REGISTRY.get(name)
    if entry is None:
        raise SatelliteFireAnimationError(
            f"required atomic tool {name!r} is not registered "
            f"(known: {sorted(TOOL_REGISTRY)[:8]}...)"
        )
    return entry.fn


def _peek_frame_count(
    product: str,
    bbox: tuple[float, float, float, float],
    start: datetime,
    end: datetime,
) -> int:
    """Count planned frames for a product over the window WITHOUT fetching imagery.

    Reads only the SLIDER JSON time index (cheap) + applies the same window /
    day-filter / cap the fetcher will. Pure-ish (one small JSON GET). Returns 0
    and logs on any upstream hiccup -- the review gate stays informative even if
    the index is briefly unreachable.
    """
    try:
        if _is_polar_product(product):
            from ..tools.fetch_viirs_day_fire import (
                DAY_FIRE_PRODUCT_SLUG,
                _build_pass_list,
            )
            from ..tools._satellite_slider import fetch_slider_timestamps

            all_ts = fetch_slider_timestamps("jpss", "conus", DAY_FIRE_PRODUCT_SLUG)
            center_lon = (bbox[0] + bbox[2]) / 2.0
            return len(_build_pass_list(all_ts, start, end, center_lon, day_only=True))
        else:
            from ..tools.fetch_goes_animation import (
                _band_to_slider_product,
                _build_frame_list,
            )
            from ..tools._satellite_slider import fetch_slider_timestamps

            slug = _band_to_slider_product(product)
            all_ts = fetch_slider_timestamps("goes-18", "conus", slug)
            return len(_build_frame_list(all_ts, start, end))
    except Exception as exc:  # noqa: BLE001 -- review-gate peek is best-effort
        logger.warning(
            "model_satellite_fire_animation: frame-count peek for %s failed (%s)",
            product,
            exc,
        )
        return 0


# --------------------------------------------------------------------------- #
# Resolution helpers (FIX A): geocode-first + WFIGS-as-additive-context.
# --------------------------------------------------------------------------- #


def _coerce_bbox(value: Any) -> tuple[float, float, float, float] | None:
    """Coerce a 4-element bbox-like into a float tuple, or ``None`` if unusable.

    Accepts a tuple/list of 4 numerics; returns ``None`` for anything else
    (empty, wrong length, non-numeric). Does NOT validate ordering -- callers
    that need a valid AOI pass it to the fetchers, which validate.
    """
    if not isinstance(value, (tuple, list)) or len(value) != 4:
        return None
    try:
        return (float(value[0]), float(value[1]), float(value[2]), float(value[3]))
    except (TypeError, ValueError):
        return None


async def _safe_geocode(
    query: str,
    pipeline_emitter: "PipelineEmitter | None",
) -> dict[str, Any] | None:
    """Geocode ``query`` via ``geocode_location`` (best-effort, never raises out).

    Returns the geocode dict (which may be a COARSE state-snap carrying
    ``fallback_reason`` / ``source == "state-bbox-fallback"``) or ``None`` on any
    failure / missing tool. The caller inspects ``_geocode_is_coarse`` to decide
    whether the place pinned the fire or we must localize from the data. Runs the
    sync tool in ``asyncio.to_thread`` (no-loop-blocking norm).
    """
    if pipeline_emitter is not None:
        step = await pipeline_emitter.add_step(
            name=f"Locate place: {query}", tool_name="geocode_location"
        )
        await pipeline_emitter.mark_running(step)
    else:
        step = None
    try:
        geocode_fn = _registry_fn("geocode_location")
        geo = await asyncio.to_thread(geocode_fn, query)
    except Exception as exc:  # noqa: BLE001 -- geocode is additive, never a gate
        logger.warning(
            "model_satellite_fire_animation: geocode_location(%r) failed (%s); "
            "will fall back to WFIGS / FIRMS localization",
            query,
            exc,
        )
        if pipeline_emitter is not None and step is not None:
            await pipeline_emitter.mark_complete(step)
        return None
    if pipeline_emitter is not None and step is not None:
        await pipeline_emitter.mark_complete(step)
    return geo if isinstance(geo, dict) else None


async def _safe_wfigs(
    incident_name: str,
    state: str | None,
    pipeline_emitter: "PipelineEmitter | None",
) -> dict[str, Any] | None:
    """Look an incident up in WFIGS as ADDITIVE context (best-effort, never gates).

    NATE directive: ``fetch_wfigs_incident`` is additive context only -- NEVER a
    gate. A no-match / upstream hiccup returns ``None`` (the caller proceeds with
    the geocode / FIRMS-derived AOI); it never raises out of the workflow. Runs
    the sync tool in ``asyncio.to_thread`` (no-loop-blocking norm).
    """
    if pipeline_emitter is not None:
        step = await pipeline_emitter.add_step(
            name=f"Look up incident: {incident_name}",
            tool_name="fetch_wfigs_incident",
        )
        await pipeline_emitter.mark_running(step)
    else:
        step = None
    try:
        wfigs_fn = _registry_fn("fetch_wfigs_incident")
        incident = await asyncio.to_thread(wfigs_fn, incident_name, state)
    except Exception as exc:  # noqa: BLE001 -- additive context, never a gate
        logger.info(
            "model_satellite_fire_animation: WFIGS lookup for %r returned no "
            "authoritative incident (%s); proceeding from geocode / FIRMS data",
            incident_name,
            exc,
        )
        if pipeline_emitter is not None and step is not None:
            # Not a failure of the workflow -- mark complete so the timeline is
            # honest (no incident on record, we build from data instead).
            await pipeline_emitter.mark_complete(step)
        return None
    if pipeline_emitter is not None and step is not None:
        await pipeline_emitter.mark_complete(step)
    return incident if isinstance(incident, dict) else None


# --------------------------------------------------------------------------- #
# Data-driven localization (FIX A): derive the AOI from FIRMS hot pixels when a
# place / incident does not resolve to a TIGHT bbox.
# --------------------------------------------------------------------------- #
#
# NATE directive: "recreate" = BUILD the case from OUR endpoints, never force a
# named external record. When geocode_location snaps coarsely (state-level) and
# no WFIGS incident resolves, the FIRE ITSELF is still in the data -- a real
# ~17k-acre fire HAS FIRMS hot pixels. So we fetch FIRMS over the broad region +
# the requested window, cluster the returned hot pixels on a coarse grid, and
# derive a TIGHT AOI bbox from the densest cluster. The hotspots ARE the fire.


#: Coarse-snap geocode signals (set by data_fetch.geocode_location's state-snap
#: fallback): a ``state-bbox-fallback`` source OR a present ``fallback_reason``.
#: Either means "no precise match, snapped to the full state" -- too broad to be
#: a fire AOI, so we localize from the data instead.
def _geocode_is_coarse(geo: dict[str, Any] | None) -> bool:
    """True iff a ``geocode_location`` result is a coarse state-level snap.

    A coarse snap carries ``source == "state-bbox-fallback"`` and/or a non-empty
    ``fallback_reason`` (the honest "snapped to the full state" note). A precise
    Nominatim match has neither. ``None`` (no geocode at all) counts as coarse so
    the caller falls through to the data-driven path.
    """
    if not geo or not isinstance(geo, dict):
        return True
    if geo.get("source") == "state-bbox-fallback":
        return True
    if geo.get("fallback_reason"):
        return True
    return False


def _read_firms_points(layer: Any) -> list[tuple[float, float]]:
    """Read (lon, lat) points from a FIRMS FlatGeobuf ``LayerURI``.

    Best-effort: reads the FGB the fetcher produced (via pyogrio/geopandas) and
    returns the detection point list. Returns ``[]`` on any read failure (a
    missing optional dep, an unreadable URI, a 0-feature layer) so the caller
    degrades honestly rather than crashing -- localization is a fallback path.
    Only a local-file or already-materialized cache URI is read here; a remote
    object store URI that geopandas cannot open yields ``[]``.
    """
    uri = getattr(layer, "uri", None)
    if not uri or not isinstance(uri, str):
        return []
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - dep present in the agent env
        logger.warning(
            "model_satellite_fire_animation: geopandas unavailable for FIRMS "
            "localization (%s)",
            exc,
        )
        return []
    try:
        gdf = gpd.read_file(uri)
    except Exception as exc:  # noqa: BLE001 -- unreadable URI degrades to []
        logger.warning(
            "model_satellite_fire_animation: could not read FIRMS layer %r for "
            "localization (%s)",
            uri,
            exc,
        )
        return []
    pts: list[tuple[float, float]] = []
    for geom in getattr(gdf, "geometry", []):
        if geom is None or getattr(geom, "is_empty", True):
            continue
        try:
            pts.append((float(geom.x), float(geom.y)))
        except Exception:  # noqa: BLE001 -- skip non-point / bad geometry
            continue
    return pts


def _densest_hotspot_bbox(
    points: list[tuple[float, float]],
    pad_deg: float = 0.1,
    cell_deg: float = 0.1,
) -> tuple[float, float, float, float] | None:
    """Cluster (lon, lat) hot pixels on a coarse grid -> a TIGHT AOI bbox.

    Buckets the points into ``cell_deg`` grid cells, finds the densest cell, then
    grows the cluster to the 3x3 neighbourhood of that cell (so a fire that
    straddles a cell boundary is captured whole), takes the min/max of the points
    in that neighbourhood, and pads the result by ``pad_deg`` (clamped to valid
    ranges). Returns ``None`` for an empty point list (caller honesty-floors).

    Deterministic: ties on cell count break on the lowest (col, row) index so the
    chosen cluster is stable for tests. This is the data-driven AOI: the densest
    hotspot cluster IS the fire.
    """
    if not points:
        return None

    def _cell(lon: float, lat: float) -> tuple[int, int]:
        import math

        return (int(math.floor(lon / cell_deg)), int(math.floor(lat / cell_deg)))

    counts: dict[tuple[int, int], int] = {}
    for lon, lat in points:
        c = _cell(lon, lat)
        counts[c] = counts.get(c, 0) + 1
    # Densest cell; deterministic tie-break on lowest (col, row).
    best_cell = min(counts, key=lambda c: (-counts[c], c[0], c[1]))
    bc, br = best_cell
    # Grow to the 3x3 neighbourhood so a boundary-straddling fire is captured.
    neigh = {
        (bc + dc, br + dr) for dc in (-1, 0, 1) for dr in (-1, 0, 1)
    }
    cluster = [
        (lon, lat) for (lon, lat) in points if _cell(lon, lat) in neigh
    ]
    if not cluster:  # pragma: no cover - best_cell is always in neigh
        cluster = points
    lons = [p[0] for p in cluster]
    lats = [p[1] for p in cluster]
    pad = max(0.01, float(pad_deg))
    min_lon = max(-180.0, min(lons) - pad)
    max_lon = min(180.0, max(lons) + pad)
    min_lat = max(-90.0, min(lats) - pad)
    max_lat = min(90.0, max(lats) + pad)
    # Guard a degenerate single-point cluster (min==max) -- pad guarantees span.
    return (
        round(min_lon, 6),
        round(min_lat, 6),
        round(max_lon, 6),
        round(max_lat, 6),
    )


async def _localize_from_firms(
    region_bbox: tuple[float, float, float, float],
    start_dt: datetime,
    end_dt: datetime,
    pipeline_emitter: "PipelineEmitter | None",
) -> tuple[float, float, float, float] | None:
    """Localize a fire AOI from FIRMS hot pixels over a broad region + window.

    Fetches FIRMS detections over ``region_bbox`` for the requested window (one
    fetch per acquisition day across the window, capped), pools the points, and
    derives a TIGHT bbox from the densest hotspot cluster. Returns ``None`` when
    no hot pixels are found (no fire detected -> the caller keeps the region
    bbox + honesty-floors). All heavy sync work runs in ``asyncio.to_thread``
    (no-loop-blocking norm).
    """
    if pipeline_emitter is not None:
        step = await pipeline_emitter.add_step(
            name="Localize fire from FIRMS hot pixels",
            tool_name="fetch_firms_active_fire",
        )
        await pipeline_emitter.mark_running(step)
    else:
        step = None

    try:
        firms_fn = _registry_fn("fetch_firms_active_fire")
    except SatelliteFireAnimationError as exc:
        logger.warning(
            "model_satellite_fire_animation: fetch_firms_active_fire not "
            "registered; cannot localize (%s)",
            exc,
        )
        if pipeline_emitter is not None and step is not None:
            await pipeline_emitter.mark_failed(
                step, "FIRMS_LOCALIZE_UNAVAILABLE", str(exc)
            )
        return None

    # One FIRMS query per acquisition day across the window (the historical-date
    # positional forces a single day each), capped so a wide window stays cheap.
    days: list[str] = []
    cur = start_dt
    while cur <= end_dt and len(days) < _FIRMS_LOCALIZE_MAX_DAYS:
        days.append(cur.strftime("%Y-%m-%d"))
        cur = cur + timedelta(days=1)
    if not days:
        days = [start_dt.strftime("%Y-%m-%d")]

    all_points: list[tuple[float, float]] = []
    for day in days:
        try:
            # source VIIRS_NOAA20_NRT (JPSS sibling); date forces that one day.
            layer = await asyncio.to_thread(
                firms_fn, region_bbox, 1, "VIIRS_NOAA20_NRT", day
            )
        except Exception as exc:  # noqa: BLE001 -- one bad day must not sink it
            logger.warning(
                "model_satellite_fire_animation: FIRMS localize day=%s failed "
                "(%s)",
                day,
                exc,
            )
            continue
        pts = await asyncio.to_thread(_read_firms_points, layer)
        all_points.extend(pts)

    bbox = _densest_hotspot_bbox(all_points)
    if bbox is None:
        logger.info(
            "model_satellite_fire_animation: FIRMS localize found no hot pixels "
            "over region=%s window=%s..%s",
            region_bbox,
            days[0],
            days[-1],
        )
        if pipeline_emitter is not None and step is not None:
            # Not a failure -- honest "no detections"; mark complete so the
            # timeline reads truthfully (the caller keeps the region bbox).
            await pipeline_emitter.mark_complete(step)
        return None

    logger.info(
        "model_satellite_fire_animation: FIRMS localize derived AOI %s from %d "
        "hot pixel(s) over %d day(s)",
        bbox,
        len(all_points),
        len(days),
    )
    if pipeline_emitter is not None and step is not None:
        await pipeline_emitter.mark_complete(step)
    return bbox


# --------------------------------------------------------------------------- #
# The workflow
# --------------------------------------------------------------------------- #


async def model_satellite_fire_animation(
    incident_name: str,
    products: list[str] | None = None,
    state: str | None = None,
    start_utc: str | None = None,
    end_utc: str | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    satellite: str | None = None,
    confirm: bool = False,
    overlay_firms: bool = True,
    overlay_perimeters: bool = True,
    *,
    pipeline_emitter: "PipelineEmitter | None" = None,
) -> dict[str, Any]:
    """Compose a satellite fire animation (GOES or JPSS) with a bbox/window review gate.

    Two phases:

    1. ``confirm=False`` (default) -- PLAN + REVIEW: resolve the incident,
       derive the AOI bbox + window, peek the planned frame count per product,
       and STOP, returning ``status="review"`` with the bbox + window + frame
       counts + a deterministic review summary. The user sees + adjusts the bbox
       and window BEFORE any imagery is fetched.
    2. ``confirm=True`` -- EXECUTE: dispatch the right imagery fetcher per product
       (each frame fetch in asyncio.to_thread), overlay FIRMS (historical date) +
       NIFC perimeters, publish every layer via TiTiler, and return
       ``status="ok"`` (or ``status="empty"`` if no imagery frames were produced
       -- the honesty floor).

    Args:
        incident_name: the named fire incident (e.g. "Iron", "Santa Rosa Island").
        products: imagery products to animate. Defaults: GOES
            ["geocolor", "fire_temperature"] unless a polar product is named.
            Allowed: "geocolor", "fire_temperature" (GOES), "day_fire" (VIIRS).
        state: optional US state filter for the incident lookup ("UT"/"US-UT").
        start_utc / end_utc: ISO-8601 UTC window bounds (override the defaults).
        bbox: optional AOI override (else derived from the incident point).
        satellite: optional satellite override ("goes-18"/"goes-19" for GOES;
            "suomi-npp"/"noaa-20"/"noaa-21"/"all" for VIIRS).
        confirm: False = stop at the review gate; True = fetch + publish.
        overlay_firms / overlay_perimeters: include the static co-registered
            FIRMS hot-pixel + NIFC perimeter overlays (confirm phase only).
        pipeline_emitter: optional live progress emitter.

    Returns:
        A JSON-compatible dict. Review phase: ``{status:"review", incident,
        bbox, start_utc, end_utc, products, frame_counts, presentation_text}``.
        Execute phase: ``{status:"ok"|"empty", incident, bbox, start_utc,
        end_utc, layers:[...], frame_counts, n_frames, n_overlays, message}``.
    """
    if not isinstance(incident_name, str) or not incident_name.strip():
        raise SatelliteFireAnimationInputError(
            f"incident_name must be a non-empty string; got {incident_name!r}"
        )
    # NATE 2026-06-26: animation frames never rendered because the registered
    # wrapper passes pipeline_emitter=None, so the per-frame add_loaded_layer
    # emit (the step the working flood composer does) never ran. Bind the LIVE
    # current_emitter() here, exactly like model_flood_scenario.py, so confirm
    # runs emit each published frame into session-state loaded_layers.
    from ..pipeline_emitter import current_emitter

    pipeline_emitter = pipeline_emitter or current_emitter()
    products = list(products) if products else list(GOES_PRODUCTS)
    for p in products:
        if p not in SUPPORTED_PRODUCTS:
            raise SatelliteFireAnimationInputError(
                f"product {p!r} not in {list(SUPPORTED_PRODUCTS)}"
            )
    if not products:
        raise SatelliteFireAnimationInputError("at least one product is required")

    # --- Stage 1 (FIX A): resolve a starting region + the discovery floor.
    #
    # "Recreate" = BUILD the case from OUR endpoints, never force a named record.
    # Order: (a) geocode the supplied location, (b) try WFIGS as ADDITIVE context
    # only (never a gate), (c) if neither yields a TIGHT AOI, LOCALIZE FROM THE
    # DATA (FIRMS hot pixels). An explicit ``bbox`` arg always wins.
    geo = await _safe_geocode(incident_name, pipeline_emitter)
    incident = await _safe_wfigs(incident_name, state, pipeline_emitter)

    # The (possibly broad) REGION used to localize from data + as the floor AOI:
    # explicit bbox > a TIGHT WFIGS incident bbox > the geocode bbox (even a
    # coarse state snap -- it bounds the FIRMS search) > a continental fallback.
    region_bbox = _coerce_bbox(bbox)
    wfigs_bbox = _coerce_bbox(incident.get("bbox")) if incident else None
    geo_bbox = _coerce_bbox(geo.get("bbox")) if geo else None
    if region_bbox is None:
        region_bbox = wfigs_bbox or geo_bbox or _CONUS_FALLBACK_BBOX

    # The discovery floor comes from WFIGS when present (additive); else None.
    discovery_iso = incident.get("fire_discovery_datetime") if incident else None

    # Window FIRST (the data-localization fetch needs it).
    end_dt_arg = _parse_utc(end_utc)
    primary_product = products[0]
    start_dt, end_dt = _default_window_for_product(
        primary_product, discovery_iso, end_dt_arg
    )
    start_override = _parse_utc(start_utc)
    if start_override is not None:
        start_dt = start_override
    if end_dt_arg is not None:
        end_dt = end_dt_arg
    if start_dt >= end_dt:
        raise SatelliteFireAnimationInputError(
            f"start_utc ({start_dt.isoformat()}) must be before end_utc "
            f"({end_dt.isoformat()})"
        )

    # --- Decide the AOI. An explicit bbox or a TIGHT WFIGS incident bbox is
    # authoritative. Otherwise, when the geocode is COARSE (state-level snap) and
    # no incident resolved, the place did not pin the fire -- so DERIVE the AOI
    # FROM THE DATA: cluster FIRMS hot pixels over the region + window. The
    # hotspots ARE the fire AOI. A precise (tight) geocode is used as-is.
    aoi_source = "geocode"
    if _coerce_bbox(bbox) is not None:
        resolved_bbox = _coerce_bbox(bbox)
        aoi_source = "bbox-override"
    elif wfigs_bbox is not None:
        resolved_bbox = wfigs_bbox
        aoi_source = "wfigs-incident"
    elif geo is not None and not _geocode_is_coarse(geo) and geo_bbox is not None:
        resolved_bbox = geo_bbox
        aoi_source = "geocode"
    else:
        # No tight place + no incident -> localize from the data.
        firms_bbox = await _localize_from_firms(
            region_bbox, start_dt, end_dt, pipeline_emitter
        )
        if firms_bbox is not None:
            resolved_bbox = firms_bbox
            aoi_source = "firms-hotspots"
        else:
            # FIRMS found nothing either -- keep the (broad) region bbox so the
            # review gate still shows WHERE we looked; the honesty floor handles
            # the empty-imagery case downstream.
            resolved_bbox = region_bbox
            aoi_source = "region-fallback"

    assert resolved_bbox is not None  # every branch sets it
    resolved_bbox = (
        float(resolved_bbox[0]),
        float(resolved_bbox[1]),
        float(resolved_bbox[2]),
        float(resolved_bbox[3]),
    )

    # --- Emit the AOI bbox + a snap-to-AOI map zoom EARLY -- BEFORE the review
    # gate -- so the user sees WHERE first (responsive-design / invariant 8).
    if pipeline_emitter is not None:
        try:
            await pipeline_emitter.emit_map_command(
                "zoom-to", {"bbox": list(resolved_bbox)}
            )
        except Exception as exc:  # noqa: BLE001 -- a UX verb, never a gate
            logger.warning(
                "model_satellite_fire_animation: early AOI zoom-to emit failed "
                "(%s)",
                exc,
            )

    # --- Stage 2: peek planned frames over the resolved AOI + window.
    frame_counts: dict[str, int] = {}
    for product in products:
        frame_counts[product] = await asyncio.to_thread(
            _peek_frame_count, product, resolved_bbox, start_dt, end_dt
        )

    start_iso = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_iso = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    # --- Review gate: STOP unless confirmed (Invariant 9; the #154 philosophy). ---
    if not confirm:
        display_name = str(
            (incident.get("incident_name") if incident else None) or incident_name
        )
        review_text = _compose_review_text(
            display_name,
            resolved_bbox,
            products,
            start_dt,
            end_dt,
            frame_counts,
        )
        return {
            "status": "review",
            "incident": incident,
            "bbox": list(resolved_bbox),
            "aoi_source": aoi_source,
            "start_utc": start_iso,
            "end_utc": end_iso,
            "products": products,
            "frame_counts": frame_counts,
            "presentation_text": review_text,
            "message": (
                "Review the AOI bbox and time window. Re-run with confirm=true "
                "(optionally adjusting bbox/start_utc/end_utc) to fetch + animate."
            ),
        }

    # --- Stage 3 (confirmed): dispatch the imagery fetcher(s) per the plan. ---
    #
    # The GOES path FOLDS the co-temporal GeoColor + Fire Temperature pair into
    # ONE blended scrubber: when BOTH GOES products are requested (the default),
    # dispatch fetch_goes_blend_animation ONCE -> one composite group (GeoColor
    # base + active-fire glow), NOT two separate groups. A SINGLE GOES product
    # (only one of the two requested) still emits that one un-blended group via
    # fetch_goes_animation. VIIRS day_fire stays a single polar product.
    all_layers: list[LayerURI] = []
    per_product_frames: dict[str, int] = {}

    goes_requested = [p for p in products if p in GOES_PRODUCTS]
    polar_requested = [p for p in products if _is_polar_product(p)]
    blend_goes = (
        _BLEND_BASE_PRODUCT in goes_requested and _BLEND_FIRE_PRODUCT in goes_requested
    )

    # (a) GOES blended path: one fetch_goes_blend_animation -> one group.
    if blend_goes:
        blend_frames = await _dispatch_goes_blend(
            resolved_bbox,
            satellite or "goes-18",
            start_iso,
            end_iso,
            pipeline_emitter,
        )
        all_layers.extend(blend_frames)
        # Attribute the blended frame count to BOTH source products (each
        # contributed one frame per blended frame) so frame_counts stays honest
        # for the two requested products.
        for p in (_BLEND_BASE_PRODUCT, _BLEND_FIRE_PRODUCT):
            per_product_frames[p] = len(blend_frames)

    # (b) Single GOES product (un-blended): only one of the two requested. When
    # the pair was blended in (a), there is nothing un-blended left to emit.
    for product in [] if blend_goes else goes_requested:
        frames = await _dispatch_single_goes(
            resolved_bbox, product, satellite or "goes-18", start_iso, end_iso, pipeline_emitter
        )
        per_product_frames[product] = len(frames)
        all_layers.extend(frames)

    # (c) VIIRS / polar products: unchanged single-product fetch.
    for product in polar_requested:
        frames = await _dispatch_polar(
            resolved_bbox, product, satellite or "all", start_iso, end_iso, pipeline_emitter
        )
        per_product_frames[product] = len(frames)
        all_layers.extend(frames)

    # --- Stage 4 (confirmed): static co-registered overlays (best-effort). ---
    overlay_layers: list[LayerURI] = []
    overlay_date = start_dt.strftime("%Y-%m-%d")
    if overlay_firms:
        firms_layer = await _safe_overlay_firms(resolved_bbox, overlay_date, pipeline_emitter)
        if firms_layer is not None:
            overlay_layers.append(firms_layer)
    if overlay_perimeters:
        nifc_layer = await _safe_overlay_perimeters(resolved_bbox, pipeline_emitter)
        if nifc_layer is not None:
            overlay_layers.append(nifc_layer)

    # --- Stage 5 (confirmed): publish every layer via TiTiler (to_thread). ---
    published = await _publish_layers(all_layers + overlay_layers, pipeline_emitter)

    # NATE 2026-06-26: EMIT each published frame into session-state loaded_layers
    # (mirrors model_flood_scenario.py ~3774). _publish_layers returns
    # {layer_id: published uri} for the frames it could publish; build a NEW
    # LayerURI copy with uri=<published uri> and add_loaded_layer it so the map
    # actually renders the animation. HONESTY FLOOR: only emit frames whose
    # publish returned a renderable uri -- an http(s) tile url or, since the
    # TiTiler exit / QGIS-native swap, the raw s3:// COG uri (the plugin reads
    # it via /vsicurl/). Anything else (empty/error strings, gs://, file://)
    # is skipped (never added). When current_emitter() is None (direct/smoke/
    # unit test without an emitter) emission is skipped; the {id: uri} map is
    # still returned for the summary.
    if pipeline_emitter is not None:
        for layer in all_layers + overlay_layers:
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
                    "model_satellite_fire_animation: add_loaded_layer(%s) failed (%s)",
                    layer.layer_id,
                    exc,
                )

    n_frames = len(all_layers)
    # Honesty floor: no imagery frames -> NOT ok.
    if n_frames == 0:
        return {
            "status": "empty",
            "incident": incident,
            "bbox": list(resolved_bbox),
            "start_utc": start_iso,
            "end_utc": end_iso,
            "products": products,
            "frame_counts": per_product_frames,
            "n_frames": 0,
            "n_overlays": len(overlay_layers),
            "layers": [],
            "message": (
                "No imagery frames were produced for the requested products over "
                "the AOI and window (no SLIDER coverage / AOI off-grid). Nothing "
                "to animate -- adjust the bbox, window, or product and re-run."
            ),
        }

    return {
        "status": "ok",
        "incident": incident,
        "bbox": list(resolved_bbox),
        "start_utc": start_iso,
        "end_utc": end_iso,
        "products": products,
        "frame_counts": per_product_frames,
        "n_frames": n_frames,
        "n_overlays": len(overlay_layers),
        "layers": [_layer_summary(layer, published) for layer in all_layers + overlay_layers],
        "message": (
            (
                f"Animated {n_frames} blended GeoColor + Fire Temperature frame(s) "
                f"(one scrubber) with {len(overlay_layers)} overlay(s) for "
                f"{incident_name}."
            )
            if blend_goes
            else (
                f"Animated {n_frames} frame(s) across {len(products)} product(s) "
                f"with {len(overlay_layers)} overlay(s) for {incident_name}."
            )
        ),
    }


# --------------------------------------------------------------------------- #
# Stage-3 dispatch helpers (one per imagery path; each runs the heavy sync
# fetcher in asyncio.to_thread so the asyncio loop / WS heartbeat never blocks).
# --------------------------------------------------------------------------- #


async def _dispatch_goes_blend(
    bbox: tuple[float, float, float, float],
    satellite: str,
    start_iso: str,
    end_iso: str,
    pipeline_emitter: "PipelineEmitter | None",
) -> list[LayerURI]:
    """Dispatch fetch_goes_blend_animation -> ONE blended GeoColor+Fire Temperature group.

    Fetches BOTH co-temporal GOES products per timestep and blends them into one
    composite RGB frame (CIRA look), returning a single ordered scrubber group.
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
            fetcher, bbox, satellite, "conus", start_iso, end_iso
        )
    except Exception as exc:  # noqa: BLE001 -- an empty blend must not crash the run
        if pipeline_emitter is not None and step is not None:
            await pipeline_emitter.mark_failed(
                step, "IMAGERY_FETCH_FAILED", f"{fetcher_name} failed: {exc}"
            )
        logger.warning(
            "model_satellite_fire_animation: %s produced no blended frames (%s)",
            fetcher_name,
            exc,
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
    start_iso: str,
    end_iso: str,
    pipeline_emitter: "PipelineEmitter | None",
) -> list[LayerURI]:
    """Dispatch fetch_goes_animation for ONE GOES product (un-blended single group)."""
    fetcher_name = _product_to_fetcher(product)
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
            fetcher, bbox, product, satellite, "conus", start_iso, end_iso
        )
    except Exception as exc:  # noqa: BLE001 -- one empty product must not sink the rest
        if pipeline_emitter is not None and step is not None:
            await pipeline_emitter.mark_failed(
                step, "IMAGERY_FETCH_FAILED", f"{fetcher_name} failed: {exc}"
            )
        logger.warning(
            "model_satellite_fire_animation: %s for product=%s produced no frames (%s)",
            fetcher_name,
            product,
            exc,
        )
        return []
    frame_list = list(frames) if isinstance(frames, list) else [frames]
    if pipeline_emitter is not None and step is not None:
        await pipeline_emitter.mark_complete(step)
    return frame_list


async def _dispatch_polar(
    bbox: tuple[float, float, float, float],
    product: str,
    satellite: str,
    start_iso: str,
    end_iso: str,
    pipeline_emitter: "PipelineEmitter | None",
) -> list[LayerURI]:
    """Dispatch fetch_viirs_day_fire for ONE JPSS/VIIRS polar product (unchanged)."""
    fetcher_name = _product_to_fetcher(product)
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
            fetcher, bbox, satellite, product, "conus", start_iso, end_iso
        )
    except Exception as exc:  # noqa: BLE001 -- one empty product must not sink the rest
        if pipeline_emitter is not None and step is not None:
            await pipeline_emitter.mark_failed(
                step, "IMAGERY_FETCH_FAILED", f"{fetcher_name} failed: {exc}"
            )
        logger.warning(
            "model_satellite_fire_animation: %s for product=%s produced no frames (%s)",
            fetcher_name,
            product,
            exc,
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
    """Fetch the FIRMS historical-date hot-pixel overlay (best-effort)."""
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
        logger.warning("model_satellite_fire_animation: FIRMS overlay failed (%s)", exc)
        if pipeline_emitter is not None and step is not None:
            await pipeline_emitter.mark_failed(step, "FIRMS_OVERLAY_FAILED", str(exc))
        return None
    if pipeline_emitter is not None and step is not None:
        await pipeline_emitter.mark_complete(step)
    return layer if isinstance(layer, LayerURI) else None


async def _safe_overlay_perimeters(
    bbox: tuple[float, float, float, float],
    pipeline_emitter: "PipelineEmitter | None",
) -> LayerURI | None:
    """Fetch the NIFC perimeter overlay (best-effort)."""
    if pipeline_emitter is not None:
        step = await pipeline_emitter.add_step(
            name="Overlay NIFC perimeters", tool_name="fetch_nifc_fire_perimeters"
        )
        await pipeline_emitter.mark_running(step)
    else:
        step = None
    try:
        nifc_fn = _registry_fn("fetch_nifc_fire_perimeters")
        layer = await asyncio.to_thread(nifc_fn, bbox)
    except Exception as exc:  # noqa: BLE001 -- overlay is non-fatal
        logger.warning("model_satellite_fire_animation: NIFC overlay failed (%s)", exc)
        if pipeline_emitter is not None and step is not None:
            await pipeline_emitter.mark_failed(step, "NIFC_OVERLAY_FAILED", str(exc))
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
    layers. Publish failures are non-fatal (the COG/FGB still exists at its
    cache URI); they are logged and skipped so a publish hiccup does not sink the
    whole animation. On AWS publish_layer can fail until QGIS-on-AWS lands -- the
    frames are still cached + the LayerURIs are still returned.
    """
    published: dict[str, str] = {}
    try:
        publish_fn = _registry_fn("publish_layer")
    except SatelliteFireAnimationError:
        logger.warning("model_satellite_fire_animation: publish_layer not registered; skipping publish")
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
                "model_satellite_fire_animation: publish_layer(%s) failed (%s)",
                layer.layer_id,
                exc,
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
    name="run_model_satellite_fire_animation",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)


@register_tool(_RUN_METADATA)
async def run_model_satellite_fire_animation(
    incident_name: str,
    products: list[str] | None = None,
    state: str | None = None,
    start_utc: str | None = None,
    end_utc: str | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    satellite: str | None = None,
    confirm: bool = False,
    overlay_firms: bool = True,
    overlay_perimeters: bool = True,
    # job-0164: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Recreate a CIRA-style satellite fire animation (GOES or JPSS/VIIRS), review-gated.

    Composes the full fire-animation pipeline: resolve the named incident
    (NIFC/WFIGS) -> AOI bbox + time window -> per-frame satellite imagery
    (GOES-18 GeoColor + Fire Temperature BLENDED into one composite loop for an
    intra-day 5-minute animation -- GeoColor base with the active fire glowing on
    top, the CIRA "GeoColor and Fire Temperature" look -- OR JPSS/VIIRS Day Fire
    for a multi-day irregular polar series) -> a scrubbable animation, with FIRMS
    hot pixels + the NIFC perimeter overlaid. STOPS at a bbox/window REVIEW gate
    first so the user sees + can adjust the AOI and the window BEFORE all frames
    are fetched.

    When to use:
        - "Recreate the CIRA GOES fire animation of the fires near Eureka Utah."
        - "Recreate the JPSS VIIRS Day Fire animation of the Santa Rosa Island
          fire over the four days it grew."
        - Any "pull the news on this fire and animate it from satellite imagery"
          request. Pick GOES-18 / 5-minute for an intra-day loop; pick
          JPSS/VIIRS Day Fire for a multi-day timelapse. ALWAYS hit the review
          gate (confirm=false) first.

    When NOT to use:
        - A single most-recent satellite frame (use fetch_goes_satellite).
        - Active-fire detections only (fetch_firms_active_fire) or perimeters only
          (fetch_nifc_fire_perimeters) with no animation.
        - A flood / surge / seismic scenario (use the matching run_model_* engine).

    Params:
        incident_name: the named fire incident (e.g. "Iron", "Santa Rosa Island").
        products: list of imagery products. GOES: "geocolor", "fire_temperature";
            VIIRS: "day_fire". Default ["geocolor", "fire_temperature"] (GOES) --
            when BOTH GOES products are present they are BLENDED into ONE composite
            scrubber (GeoColor base + active-fire glow); request a single product
            (e.g. ["geocolor"]) for one un-blended group.
        state: optional US state filter for the incident lookup ("UT"/"US-UT").
        start_utc / end_utc: ISO-8601 UTC window bounds. Defaults: GOES ~6.5h,
            VIIRS ~4 days, never before the WFIGS discovery time.
        bbox: optional AOI override [min_lon, min_lat, max_lon, max_lat].
        satellite: GOES "goes-18"/"goes-19"; VIIRS "suomi-npp"/"noaa-20"/
            "noaa-21"/"all".
        confirm: false (default) = stop at the review gate and return the bbox +
            window + planned frame counts; true = fetch + publish + animate.
        overlay_firms / overlay_perimeters: include the static co-registered
            FIRMS hot-pixel + NIFC perimeter overlays (confirm phase).

    Returns:
        Review phase (confirm=false): a dict with status="review", the AOI bbox,
        the time window, the planned per-product frame counts, and a
        presentation_text the UI shows for approval.
        Execute phase (confirm=true): a dict with status="ok" (or "empty" if no
        imagery frames were produced -- the honesty floor), the bbox, window,
        the published layer summaries, and frame/overlay counts.

    Cross-tool dependencies:
        Upstream (step chain): fetch_wfigs_incident -> fetch_goes_animation /
        fetch_viirs_day_fire (per product, per frame) -> fetch_firms_active_fire
        (historical date) + fetch_nifc_fire_perimeters (overlays) -> publish_layer.
    """
    return await model_satellite_fire_animation(
        incident_name=incident_name,
        products=products,
        state=state,
        start_utc=start_utc,
        end_utc=end_utc,
        bbox=bbox,
        satellite=satellite,
        confirm=confirm,
        overlay_firms=overlay_firms,
        overlay_perimeters=overlay_perimeters,
        pipeline_emitter=None,
    )
