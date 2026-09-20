"""``probe_point`` - every raster layer on a case read at ONE Point.

A stack of animation frames collapses into one series rather than N rows, and a
case with no rasters is an empty read rather than a refusal. Honesty floor: a
point outside an extent, on nodata, or on an unreadable layer is a null entry
carrying its reason, never dropped and never zero-filled.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import tempfile
from datetime import datetime, timezone
from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.inputs.point import point as ingest_point
from trid3nt_server.tools import register_tool

__all__ = [
    "probe_point",
    "ProbePointError",
    "ProbePointInputError",
    "ProbePointCaseNotFoundError",
    "MAX_PROBE_LAYERS",
    "detect_frame_sequences",
    "layers_from_case",
    "parse_frame_token",
    "resolve_case_id",
    "sample_raster_at_point",
    "stage_layer_local",
]

logger = logging.getLogger(__name__)

#: Max raster layers opened per probe click. A case accumulates loaded layers
#: over a long session and a probe is a synchronous point-and-wait UI action,
#: so the cap is honest rather than a silent drop: the response carries
#: ``truncated: true`` when the case has more.
MAX_PROBE_LAYERS = 40




class ProbePointError(RuntimeError):
    """Base class for probe_point_at failures."""

    error_code: str = "PROBE_POINT_ERROR"
    retryable: bool = True


class ProbePointInputError(ProbePointError):
    """Bad inputs (missing case_id, invalid/out-of-range lon/lat)."""

    error_code = "PROBE_POINT_INPUT_INVALID"
    retryable = False


class ProbePointCaseNotFoundError(ProbePointError):
    """No such case, or the persistence backend is unavailable."""

    error_code = "PROBE_POINT_CASE_NOT_FOUND"
    retryable = False


# The case-layer read seam: which case, what layers it holds, how one is
# materialized and sampled. Every point read over a case goes through here.


def resolve_case_id(case_id: Any,
                    error_cls: type[Exception] = ProbePointCaseNotFoundError) -> str:
    """``case_id`` param wins; else the turn's bound Case; else typed error."""
    if case_id is not None and str(case_id).strip():
        return str(case_id).strip()
    try:
        from trid3nt_server.render.pipeline_emitter import current_turn_case

        bound = current_turn_case()
    except Exception:  # noqa: BLE001
        bound = None
    if bound:
        return str(bound)
    raise error_cls(
        "no case_id was supplied and no Case is bound to the current turn; "
        "pass case_id explicitly.")


async def layers_from_case(
    case_id: str,
    not_found_cls: type[Exception] = ProbePointCaseNotFoundError,
) -> tuple[list[dict[str, Any]], list[float] | None, str, Any]:
    """``(layer dicts, case bbox, case title, case doc)`` for ``case_id``, read
    from the Case doc's persisted ``loaded_layer_summaries``."""
    from trid3nt_server.telemetry import get_persistence

    try:
        persistence = get_persistence()
    except Exception:  # noqa: BLE001
        persistence = None
    if persistence is None:
        raise not_found_cls(
            f"cannot look up case {case_id!r}: the persistence backend is not "
            "available from this process.")
    case = await persistence.get_case(case_id)
    if case is None:
        raise not_found_cls(f"case {case_id!r} not found.")
    layers = [dict(entry) for entry in (case.loaded_layer_summaries or [])]
    bbox = list(case.bbox) if getattr(case, "bbox", None) else None
    return layers, bbox, getattr(case, "title", None) or case_id, case


def stage_layer_local(uri: str, tmpdir: str, label: str) -> str:
    """Materialize an ``s3://`` or local layer uri to a local path; a failure
    raises, for the caller to record as a per-layer entry."""
    from trid3nt_server.tools._uri_util import _strip_query

    resolved = uri
    if resolved.startswith("s3://"):
        from trid3nt_server.tools.cache import read_object_bytes_s3

        name = resolved.rstrip("/").rsplit("/", 1)[-1] or f"{label}.bin"
        local = os.path.join(tmpdir, f"{label}_{name}")
        with open(local, "wb") as f:
            f.write(read_object_bytes_s3(resolved))
        return local
    if resolved.startswith(("gs://", "http://", "https://")):
        raise ValueError(
            f"layer uri scheme not supported for point sampling: {resolved!r}")
    probe = _strip_query(resolved)
    if not os.path.exists(probe):
        raise FileNotFoundError(f"layer uri is not a readable local file: {uri!r}")
    return probe


def sample_raster_at_point(
    local_path: str, lon: float, lat: float
) -> tuple[float | None, str | None, str | None]:
    """``(value, note, units)`` for band 1 at an EPSG:4326 point; a point off the
    extent or on nodata is None with a note, and a read failure raises."""
    import rasterio
    from rasterio.warp import transform as warp_transform
    from rasterio.windows import Window

    with rasterio.open(local_path) as src:
        units = (
            src.tags().get("units")
            or (src.units[0] if src.units and src.units[0] else None)
        )
        x, y = lon, lat
        if src.crs is not None and str(src.crs).upper() != "EPSG:4326":
            xs, ys = warp_transform("EPSG:4326", src.crs, [lon], [lat])
            x, y = float(xs[0]), float(ys[0])
        row, col = src.index(x, y)
        if not (0 <= row < src.height and 0 <= col < src.width):
            return None, "point outside the layer extent", units
        value = float(
            src.read(1, window=Window(col, row, 1, 1)).astype("float64")[0, 0]
        )
        nodata = src.nodata
    if not math.isfinite(value) or (
        nodata is not None
        and math.isfinite(float(nodata))
        and value == float(nodata)
    ):
        return None, "nodata at this point", units
    return value, None, units


# Frame-token parsing.
#
# The token read here is an ANALYSIS over the layers a case already holds, not
# the map's clock: presentation reads the valid_from / valid_to window a frame
# declares. A case persisted before that window existed carries only the name,
# which is why the token read stays.

_FRAME_PATTERNS: tuple[tuple[re.Pattern[str], Any], ...] = (
    # Forecast lead hour: "F+01h", "f+12h", "F+1 h", "+06h"
    (
        re.compile(r"\bf?\+?\s*(\d{1,3})\s*h\b", re.IGNORECASE),
        lambda m: f"F+{int(m.group(1)):02d}h",
    ),
    # Hour token: "hour 3", "hr 06", "h12"
    (
        re.compile(r"\bh(?:ou)?r?\s*\+?(\d{1,3})\b", re.IGNORECASE),
        lambda m: f"hr {int(m.group(1))}",
    ),
    # Step/frame/index: "step 4", "frame 02", "idx 3", "index 12"
    (
        re.compile(r"\b(?:step|frame|idx|index)\s*\+?(\d{1,4})\b", re.IGNORECASE),
        lambda m: f"step {int(m.group(1))}",
    ),
    (
        re.compile(r"\bt\s*\+\s*(\d{1,4})\b", re.IGNORECASE),
        lambda m: f"t+{int(m.group(1))}",
    ),
    (re.compile(r"#\s*(\d{1,4})\b"), lambda m: f"#{int(m.group(1))}"),
    # Day token: "day 1", "d+3"
    (
        re.compile(r"\bd(?:ay)?\s*\+?(\d{1,3})\b", re.IGNORECASE),
        lambda m: f"day {int(m.group(1))}",
    ),
)

#: ISO-8601 UTC valid-time substring, e.g. "2026-06-22T18:05:00Z". When a frame
#: name carries BOTH a step token and an ISO valid-time (the satellite
#: fire-animation convention), the ISO becomes the per-frame LABEL and is
#: stripped from the grouping stem.
_ISO_TIME_RX = re.compile(r"\b(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})(?::\d{2})?Z?\b")

_STEM_EDGE_PUNCT = re.compile(r"^[\s,(\-]+|[\s,(\-]+$")


def parse_frame_token(name: str) -> dict[str, Any] | None:
    """``{"value": int, "label": str, "stem": str}`` for a lead-time, step or
    index token in a layer name, else None."""
    if not name:
        return None
    for rx, label_fn in _FRAME_PATTERNS:
        m = rx.search(name)
        if m is None:
            continue
        value = int(m.group(1))
        body = name[: m.start()] + name[m.end():]
        iso = _ISO_TIME_RX.search(body)
        if iso:
            body = body.replace(iso.group(0), " ")
        stem = _STEM_EDGE_PUNCT.sub("", re.sub(r"\s+", " ", body)).strip().lower()
        frame_label = f"{iso.group(1)} {iso.group(2)}Z" if iso else label_fn(m)
        return {"value": value, "label": frame_label, "stem": stem}
    return None


def detect_frame_sequences(
    layers: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """``{stem: [{layer, value, label}, ...]}`` ordered by token value; only a stem
    with two or more strictly-increasing members forms a sequence."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for layer in layers:
        if layer.get("layer_type") != "raster":
            continue
        token = parse_frame_token(str(layer.get("name") or ""))
        if token is None:
            continue
        grouped.setdefault(token["stem"], []).append(
            {"layer": layer, "value": token["value"], "label": token["label"]}
        )

    sequences: dict[str, list[dict[str, Any]]] = {}
    for stem, members in grouped.items():
        if len(members) < 2:
            continue
        members.sort(key=lambda m: m["value"])
        values = [m["value"] for m in members]
        if all(b > a for a, b in zip(values, values[1:])):
            sequences[stem] = members
    return sequences


# Sync per-layer/per-frame sampling (wrapped in asyncio.to_thread by callers).


def _sample_single_layer(
    layer: dict[str, Any], lon: float, lat: float, tmpdir: str, tag: str
) -> dict[str, Any]:
    """SYNC: stage and sample one non-series raster layer at a point.

    Never raises: outside-extent and nodata carry a ``note``, an unreadable
    layer an ``error``, and both leave ``value`` null."""
    name = str(layer.get("name") or layer.get("layer_id") or tag)
    entry: dict[str, Any] = {
        "layer_id": str(layer.get("layer_id") or ""),
        "name": name,
        "value": None,
        "units": layer.get("units"),
    }
    uri = str(layer.get("uri") or "")
    try:
        if not uri:
            raise ValueError("layer has no uri")
        local = stage_layer_local(uri, tmpdir, tag)
        value, note, tag_units = sample_raster_at_point(local, lon, lat)
        entry["value"] = value
        if entry["units"] is None and tag_units:
            entry["units"] = tag_units
        if note:
            entry["note"] = note
    except Exception as exc:  # noqa: BLE001 -- honest per-layer entry
        entry["error"] = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "probe_point: layer %r unreadable at point: %s", name, exc)
    return entry


def _sample_series_member(
    layer: dict[str, Any], label: str, lon: float, lat: float, tmpdir: str, tag: str
) -> tuple[dict[str, Any], str | None]:
    """SYNC: stage and sample one frame of a detected sequence at a point.

    ``units`` is the frame's OWN units, so the caller can pick the first
    non-null one for the whole series."""
    entry: dict[str, Any] = {"label": label, "value": None}
    units: str | None = None
    uri = str(layer.get("uri") or "")
    try:
        if not uri:
            raise ValueError("frame layer has no uri")
        local = stage_layer_local(uri, tmpdir, tag)
        value, note, tag_units = sample_raster_at_point(local, lon, lat)
        entry["value"] = value
        if note:
            entry["note"] = note
        units = layer.get("units") or tag_units
    except Exception as exc:  # noqa: BLE001 -- honest per-frame entry
        entry["error"] = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "probe_point: frame %r unreadable at point: %s", label, exc
        )
    return entry, units




_METADATA = AtomicToolMetadata(
    name="probe_point",
    ttl_class="live-no-cache",
    source_class=None,
    cacheable=False,
)


@register_tool(
    _METADATA,
    read_only_hint=True,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
)
async def probe_point(
    point: Any = None,
    case_id: str | None = None,
    # absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """READ every raster layer on the case at ONE point, frames included.

    ROUTING: "what do the layers say here", "read everything at the point I
    clicked", "probe this spot", "what is the value at this point over the
    animation". `point` takes any form a point arrives in - a canvas pick, a
    (lon, lat) pair, a "lat,lon" string or a selected point layer; geocode a
    place name first. A stack of animation frames comes back as ONE series, not
    N rows.

    Do NOT use for: vector layers, which a point read skips.

    Returns the point, the case, and one result per raster: its value, units and
    any note. A layer outside its extent, on nodata or unreadable is a null with
    its reason, never a fabricated number; a case with no rasters reads empty.
    """
    # Vector layers are not sampled at all -- a point probe of a vector needs a
    # different query shape -- and are simply absent from the results.
    picked = await ingest_point(point, label="probe point",
                                code=ProbePointInputError.error_code)
    if picked is None:
        raise ProbePointInputError(
            "probe_point needs a point - a pick, a (lon, lat) pair or a point "
            "layer; geocode a place name first")
    q_lon, q_lat = picked.lon, picked.lat
    resolved_case = resolve_case_id(case_id, ProbePointCaseNotFoundError)

    layers, _bbox, _title, _case = await layers_from_case(
        resolved_case, ProbePointCaseNotFoundError
    )

    raster_layers = [l for l in layers if l.get("layer_type") == "raster"]
    truncated = len(raster_layers) > MAX_PROBE_LAYERS
    capped = raster_layers[:MAX_PROBE_LAYERS]

    sequences = detect_frame_sequences(capped)
    stem_by_layer_id: dict[int, str] = {}
    for stem, members in sequences.items():
        for m in members:
            stem_by_layer_id[id(m["layer"])] = stem

    results: list[dict[str, Any]] = []
    emitted_stems: set[str] = set()
    # The sync boto3 / rasterio work goes off the loop per layer and per frame:
    # this module is called directly on the agent's asyncio loop, with no outer
    # executor, so blocking here would stall the WS heartbeat.
    with tempfile.TemporaryDirectory(prefix="trid3nt_probe_point_") as tmpdir:
        for idx, layer in enumerate(capped):
            stem = stem_by_layer_id.get(id(layer))
            if stem is not None:
                if stem in emitted_stems:
                    continue
                emitted_stems.add(stem)
                members = sequences[stem]
                series_out: list[dict[str, Any]] = []
                units: str | None = None
                layer_ids: list[str] = []
                for i, member in enumerate(members):
                    entry, units_hint = await asyncio.to_thread(
                        _sample_series_member,
                        member["layer"],
                        member["label"],
                        q_lon,
                        q_lat,
                        tmpdir,
                        f"seq{idx}_{i}",
                    )
                    if units is None and units_hint:
                        units = units_hint
                    series_out.append(entry)
                    layer_ids.append(str(member["layer"].get("layer_id") or ""))
                results.append(
                    {
                        "name": stem,
                        "series": series_out,
                        "units": units,
                        "layer_ids": layer_ids,
                    }
                )
            else:
                entry = await asyncio.to_thread(
                    _sample_single_layer, layer, q_lon, q_lat, tmpdir, f"layer{idx}"
                )
                results.append(entry)

    logger.info(
        "probe_point: case=%s point=(%.5f, %.5f) rasters=%d results=%d truncated=%s",
        resolved_case,
        q_lon,
        q_lat,
        len(raster_layers),
        len(results),
        truncated,
    )
    return {
        "status": "ok",
        "point": {"lon": q_lon, "lat": q_lat},
        "case_id": resolved_case,
        "results": results,
        "truncated": truncated,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }
