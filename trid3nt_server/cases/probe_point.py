"""Deterministic map-click point probe: every raster on a case, at one point.

Honesty floor: a point outside an extent, on nodata, or on an unreadable layer is an
honest null entry, never dropped and never zero-filled.
"""
from __future__ import annotations

import asyncio
import logging
import tempfile
from datetime import datetime, timezone
from typing import Any

from trid3nt_server.tools.processing.extract_timeseries_at_point.extract_timeseries_at_point import detect_frame_sequences
from trid3nt_server.tools.processing.query_point_hazard.query_point_hazard import (
    layers_from_case,
    resolve_point,
    sample_raster_at_point,
    stage_layer_local,
)

__all__ = [
    "probe_point_at",
    "ProbePointError",
    "ProbePointInputError",
    "ProbePointCaseNotFoundError",
    "MAX_PROBE_LAYERS",
]

logger = logging.getLogger("trid3nt_server.cases.probe_point")

#: Max raster layers opened per probe click. A case accumulates loaded layers
#: over a long session and a probe is a synchronous point-and-wait UI action,
#: so the cap is honest rather than a silent drop: the response carries
#: ``truncated: true`` when the case has more.
MAX_PROBE_LAYERS = 40


# ---------------------------------------------------------------------------
# Typed errors.
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Sync per-layer/per-frame sampling (wrapped in asyncio.to_thread by callers).
# ---------------------------------------------------------------------------


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
            "probe_point: layer %r unreadable at point: %s", name, exc
        )
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


# ---------------------------------------------------------------------------
# Core.
# ---------------------------------------------------------------------------


async def probe_point_at(case_id: str, lon: float, lat: float) -> dict[str, Any]:
    """Sample every raster layer and frame sequence on a case at one point.

    A case with zero raster layers is NOT an error: the click succeeded and
    there was nothing to sample."""
    # A detected animation-frame stack collapses into ONE result entry carrying
    # a series, rather than N single-value rows. Vector layers are not sampled
    # at all -- a point probe of a vector needs a different query shape -- and
    # are simply absent from the results.
    q_lon, q_lat, _label = resolve_point(lon, lat, None, ProbePointInputError)
    if not case_id or not str(case_id).strip():
        raise ProbePointInputError("missing or empty `case_id`")
    resolved_case = str(case_id).strip()

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
