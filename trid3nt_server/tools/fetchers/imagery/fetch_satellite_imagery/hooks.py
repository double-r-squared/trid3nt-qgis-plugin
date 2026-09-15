"""satellite_imagery frames hooks: the SLIDER substrate, driven by the server's own index.

``frames_plan`` reads the viewer's product index, validates satellite, sector and product
against it, then windows the availability index by the satellite's own cadence -- a step
for geostationary, the pass list for polar -- into ordered plans; ``frame_bytes`` stitches
and reprojects ONE frame."""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from trid3nt_contracts.source_spec import SourceSpec

from ...imagery._satellite_slider import (
    _USER_AGENT,
    SliderEmptyError,
    SliderError,
    SliderUpstreamError,
    fetch_slider_timestamps,
    mosaic_to_cog_bytes,
    pick_zoom_for_aoi,
    registered_sectors,
    usable_zoom,
    sector_registration,
    stitch_slider_mosaic,
    ts_int_to_datetime,
    ts_int_to_iso,
)
from ..._router.errors import router_empty_error, router_input_error, router_upstream_error
from ..._router.hooks import FrameDegraded, FramePlan, frame_windows, register_hook

logger = logging.getLogger(__name__)

__all__ = [
    "frames_plan",
    "frame_bytes",
    "PRODUCT_INDEX_URL",
    "MAX_FRAMES",
    "load_index",
    "sector_products",
    "is_geostationary",
    "_parse_utc",
    "_is_daytime_pass",
    "_step_frames",
    "_pass_frames",
]

#: The viewer's own definition file: one ``var json = {...};`` assignment whose body is
#: strict JSON, carrying every satellite, sector, product, tile size and native cadence
#: the service serves. It is the ONLY list of products; a map written here would drift.
PRODUCT_INDEX_URL = "https://rammb-slider.cira.colostate.edu/js/define-products.js"

#: How long a parsed index is reused before it is re-read: the file changes when a
#: product is added to the viewer, which is far slower than a session.
_INDEX_TTL_S = 3600.0

#: Local-solar-time window (hours) treated as a DAY pass.
_DAY_LST_START_H = 6.0
_DAY_LST_END_H = 19.0

#: A step boundary is accepted this far early, so stamp jitter never drops a frame that
#: is a cadence apart in every way that matters.
_STEP_TOLERANCE_S = 30.0

MAX_FRAMES: int = int(os.environ.get("TRID3NT_MAX_SATELLITE_FRAMES", "144"))

_BBOX_QUANTIZE_DP = 6

#: (fetched_at, index) or None.
_INDEX_CACHE: tuple[float, dict[str, Any]] | None = None


def _extract_json_object(text: str) -> dict[str, Any]:
    """Pull the ``var json = {...}`` object out of the definition file by brace matching.
    A truncated or restructured file raises ValueError rather than half-parsing."""
    # Braces inside string literals are skipped: the product descriptions are prose and
    # HTML, and one brace in one of them would otherwise close the object early.
    start = text.index("{", text.index("var json"))
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        char = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start : i + 1])
    raise ValueError("unbalanced braces in the SLIDER product index")


def load_index(sc: str) -> dict[str, Any]:
    """The per-satellite index from the server, re-read once its reuse window lapses.
    Raises the typed upstream error when the file cannot be read or parsed."""
    global _INDEX_CACHE
    now = time.monotonic()
    if _INDEX_CACHE is not None and now - _INDEX_CACHE[0] < _INDEX_TTL_S:
        return _INDEX_CACHE[1]
    try:
        resp = requests.get(
            PRODUCT_INDEX_URL,
            headers={"User-Agent": _USER_AGENT},
            timeout=30.0,
            allow_redirects=True,
        )
        resp.raise_for_status()
        satellites = _extract_json_object(resp.text)["satellites"]
    except (requests.RequestException, ValueError, KeyError) as exc:
        raise router_upstream_error(
            sc, f"SLIDER product index unreadable ({PRODUCT_INDEX_URL}): {exc}"
        )
    if not isinstance(satellites, dict) or not satellites:
        raise router_upstream_error(
            sc, f"SLIDER product index lists no satellites ({PRODUCT_INDEX_URL})"
        )
    _INDEX_CACHE = (now, satellites)
    return satellites


def sector_products(sat_entry: dict[str, Any], sector: str) -> list[str]:
    """The products the server serves for ONE sector: the satellite's list minus that
    sector's own exclusions. An entry whose title is a rule of dashes is a menu heading
    in the viewer, not a product, and is dropped."""
    missing = set(sat_entry["sectors"][sector].get("missing_products") or ())
    return [
        name
        for name, product in (sat_entry.get("products") or {}).items()
        if name not in missing
        and not str(product.get("product_title", "")).startswith("-")
    ]


def is_geostationary(sat_entry: dict[str, Any]) -> bool:
    """True iff the server states a fixed sub-satellite longitude for this bird, which
    is what separates a stepped cadence from a list of overpasses."""
    return any(
        "lon0" in (sector.get("lat_lon_query") or {})
        for sector in sat_entry["sectors"].values()
    )


def _parse_utc(spec: SourceSpec, value: Any) -> datetime:
    """Parse an ISO-8601 string or datetime to aware UTC, accepting a trailing 'Z', a
    space separator or a bare date. An unparseable value raises the typed INPUT error."""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip().replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise router_input_error(
            spec.error_code_prefix,
            f"could not parse UTC time {value!r}; use ISO-8601 "
            "(e.g. '2026-06-22T13:30:00Z')",
            spec.input_error_suffix,
        )
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _is_daytime_pass(ts_int: int, aoi_center_lon: float) -> bool:
    """True iff the overpass falls in local daylight at the AOI longitude."""
    when = ts_int_to_datetime(ts_int)
    utc_hours = when.hour + when.minute / 60.0 + when.second / 3600.0
    lst = (utc_hours + aoi_center_lon / 15.0) % 24.0
    return _DAY_LST_START_H <= lst < _DAY_LST_END_H


def _step_frames(in_window: list[int], step_minutes: float) -> list[int]:
    """Thin an ascending stamp list to one frame per requested step."""
    gap = timedelta(minutes=step_minutes) - timedelta(seconds=_STEP_TOLERANCE_S)
    kept: list[int] = []
    for ts in in_window:
        if not kept or ts_int_to_datetime(ts) - ts_int_to_datetime(kept[-1]) >= gap:
            kept.append(ts)
    return kept


def _pass_frames(
    in_window: list[int], aoi_center_lon: float, *, day_only: bool
) -> list[int]:
    """The overpasses themselves, optionally only those in local daylight."""
    if not day_only:
        return list(in_window)
    return [ts for ts in in_window if _is_daytime_pass(ts, aoi_center_lon)]


def _cap(frames: list[int], cap: int) -> list[int]:
    """Even-subsample to ``cap`` frames, endpoints kept."""
    n = len(frames)
    if n <= cap:
        return frames
    picked = [frames[round(i * (n - 1) / (cap - 1))] for i in range(cap)]
    kept = list(dict.fromkeys(picked))
    logger.info(
        "satellite_imagery: %d in-window frames exceed cap=%d; subsampling to %d.",
        n, cap, len(kept),
    )
    return kept


def _round_bbox(bbox: Any) -> tuple[float, float, float, float]:
    return tuple(round(float(v), _BBOX_QUANTIZE_DP) for v in bbox)  # type: ignore[return-value]


def _pick(
    spec: SourceSpec, params: dict[str, Any], index: dict[str, Any]
) -> tuple[str, str, str, dict[str, Any]]:
    """Resolve satellite, sector and product against the server's index, defaulting the
    product to the sector's own, and register the sector on the ground. Anything the
    server does not serve, or does not let this substrate place on the ground, raises
    the typed INPUT error listing what it does."""
    sc, suffix = spec.error_code_prefix, spec.input_error_suffix
    satellite = str(params.get("satellite") or "").strip().lower()
    if satellite not in index:
        raise router_input_error(
            sc, f"unknown satellite={satellite!r}; the server serves: {sorted(index)}",
            suffix,
        )
    sat_entry = index[satellite]
    sector = str(params.get("sector") or "").strip().lower()
    if sector not in sat_entry["sectors"]:
        raise router_input_error(
            sc,
            f"unknown sector={sector!r} for {satellite}; the server serves: "
            f"{sorted(sat_entry['sectors'])}",
            suffix,
        )
    registration = sector_registration(satellite, sector, sat_entry)
    if registration is None:
        raise router_input_error(
            sc,
            f"{satellite}/{sector} cannot be placed on the ground: the server publishes "
            "no projection for it and nothing has measured one, so an AOI crop would be "
            f"a guess. Registered sectors: {registered_sectors(index)}",
            suffix,
        )
    served = sector_products(sat_entry, sector)
    product = str(
        params.get("product") or sat_entry["sectors"][sector].get("default_product") or ""
    ).strip().lower()
    if product not in served:
        raise router_input_error(
            sc,
            f"unknown product={product!r} for {satellite}/{sector}; the server serves: "
            f"{sorted(served)}",
            suffix,
        )
    return satellite, sector, product, registration


@register_hook("satellite_imagery.frames_plan")
def frames_plan(spec: SourceSpec, params: dict[str, Any]) -> list[FramePlan]:
    """Window the availability index by the satellite's own cadence into ordered plans.
    A window of one instant is the single frame nearest it; a window that matched no
    frame raises the typed EMPTY."""
    sc, suffix = spec.error_code_prefix, spec.input_error_suffix
    bbox = _round_bbox(params["bbox"])
    index = load_index(sc)
    satellite, sector, product, registration = _pick(spec, params, index)
    sat_entry = index[satellite]
    sector_entry = sat_entry["sectors"][sector]
    geostationary = is_geostationary(sat_entry)

    step_minutes = params.get("step_minutes")
    day_only = params.get("day_only")
    if geostationary and day_only is not None:
        raise router_input_error(
            sc,
            f"day_only filters polar overpasses; {satellite} is geostationary and "
            "scans on a clock -- use step_minutes",
            suffix,
        )
    if not geostationary and step_minutes is not None:
        raise router_input_error(
            sc,
            f"step_minutes sets a geostationary cadence; {satellite} is polar and its "
            "cadence is its pass list -- use day_only to keep daylight passes",
            suffix,
        )

    end_dt = _parse_utc(spec, params["end_utc"]) if params.get("end_utc") else datetime.now(timezone.utc)
    start_dt = _parse_utc(spec, params["start_utc"]) if params.get("start_utc") else end_dt
    if start_dt > end_dt:
        raise router_input_error(
            sc,
            f"start_utc ({start_dt.isoformat()}) is after end_utc ({end_dt.isoformat()})",
            suffix,
        )

    try:
        available = fetch_slider_timestamps(satellite, sector, product)
    except SliderError as exc:
        raise router_upstream_error(sc, str(exc))

    native = sector_entry.get("defaults", {}).get("minutes_between_images")
    if native is None:
        raise router_upstream_error(
            sc, f"the index states no cadence for {satellite}/{sector}"
        )
    native = float(native)
    if start_dt == end_dt:
        # One instant is one frame: the scan nearest it, and only if the index holds
        # one within a cadence -- a snap across an outage would answer a different
        # question than the one asked.
        nearest = min(
            available, key=lambda ts: abs(ts_int_to_datetime(ts) - end_dt), default=None
        )
        frames = [nearest] if nearest is not None and abs(
            ts_int_to_datetime(nearest) - end_dt
        ) <= timedelta(minutes=native) else []
    else:
        in_window = [
            ts for ts in available if start_dt <= ts_int_to_datetime(ts) <= end_dt
        ]
        if geostationary:
            frames = _step_frames(in_window, float(step_minutes or native))
        else:
            frames = _pass_frames(
                in_window,
                (bbox[0] + bbox[2]) / 2.0,
                day_only=True if day_only is None else bool(day_only),
            )
    frames = _cap(frames, MAX_FRAMES)
    if not frames:
        raise router_empty_error(
            sc,
            f"no {satellite}/{sector}/{product} frames in window "
            f"{start_dt.isoformat()}..{end_dt.isoformat()}; the index carries "
            f"{len(available)} timestamps",
            spec.empty_error_suffix,
        )

    try:
        zoom = usable_zoom(satellite, sector, product, frames[0],
                           pick_zoom_for_aoi(registration, sector_entry, bbox))
    except SliderError as exc:
        raise router_upstream_error(sc, str(exc))
    windows = frame_windows([ts_int_to_iso(ts) for ts in frames])
    plans: list[FramePlan] = []
    for frame_no, ts_int in enumerate(frames, start=1):
        iso = ts_int_to_iso(ts_int)
        valid_from, valid_to = windows[frame_no - 1]
        plans.append(
            FramePlan(
                cache_params={
                    "bbox": list(bbox),
                    "satellite": satellite,
                    "sector": sector,
                    "product": product,
                    "ts_int": ts_int,
                    "zoom": zoom,
                    "tile_size": int(sector_entry["tile_size"]),
                    "registration": registration,
                },
                name=f"{satellite.upper()} {product} step {frame_no} {iso}",
                layer_id=(
                    f"slider-{satellite}-{sector}-{product}-{ts_int}"
                    f"-{bbox[0]:.3f}-{bbox[1]:.3f}"
                ),
                bbox=bbox,
                valid_from=valid_from,
                valid_to=valid_to,
            )
        )
    return plans


@register_hook("satellite_imagery.frame_bytes")
def frame_bytes(spec: SourceSpec, params: dict[str, Any], frame: FramePlan) -> bytes:
    """Stitch and reproject ONE frame to an RGB COG. FrameDegraded on an empty or
    upstream-failed frame, so the executor records and drops that one."""
    cp = frame.cache_params
    bbox = tuple(cp["bbox"])  # type: ignore[assignment]
    try:
        rgb, mosaic_extent = stitch_slider_mosaic(
            cp["satellite"], cp["sector"], cp["product"], cp["ts_int"], cp["zoom"],
            bbox, cp["registration"], cp["tile_size"]
        )
        return mosaic_to_cog_bytes(
            rgb, mosaic_extent, cp["registration"]["crs"], bbox)
    except (SliderEmptyError, SliderUpstreamError) as exc:
        raise FrameDegraded(str(exc)) from exc
