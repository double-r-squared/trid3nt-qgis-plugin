"""Deterministic map-click point probe -- server-side core for
``POST /api/probe-point`` on the catalog HTTP listener (``tool_catalog_http.py``).

The TRID3NT QGIS plugin's dock has a "Probe" map tool: click the canvas and
the dock shows the value (or a mini time series, for an animation-frame
sequence) of every RASTER layer loaded on the current case at that point.
This is a DETERMINISTIC read -- no LLM in the loop, no turn, no pipeline
event -- so it is a plain function, NOT a ``@register_tool`` (an LLM-visible
tool would be dispatched by the model; this is driven directly by a map
click over cold HTTP, the same posture as ``/api/ingest-layer`` and
``/api/case-list``).

Reuses two existing seams rather than re-deriving them:

- ``query_point_hazard``'s case-layer enumeration
  (``layers_from_case``) + point-sampling primitives
  (``stage_layer_local`` -- which already unwraps a TiTiler tile TEMPLATE to
  its underlying COG via ``export_case_to_qgis._unwrap_tile_template`` --
  and ``sample_raster_at_point``, nearest-neighbour via rasterio).
- ``extract_timeseries_at_point``'s frame-sequence classifier
  (``detect_frame_sequences`` / ``parse_frame_token``) -- the SAME stem/token
  grouping the web LayerPanel scrubber and ``extract_timeseries_at_point``
  use, so a clicked point's "Flood depth step N" stack collapses into one
  series entry instead of N separate single-value rows.

Honesty (data-source fallback norm): a point outside a layer's extent, or on
nodata, is an honest ``value: null`` + ``note`` entry -- never dropped, never
zero-filled. A single unreadable layer/frame is a per-layer honest
``value: null`` + ``error`` entry; it never fails the whole probe.

Sync work (boto3 / rasterio) runs off the event loop via
``asyncio.to_thread`` per layer/frame -- this module is called directly on
the agent's asyncio loop (unlike an LLM tool dispatch, there is no outer
executor wrapping it), so blocking here would stall the WS heartbeat.

``MAX_PROBE_LAYERS`` caps the number of raster artifacts opened per click --
a case can accumulate many loaded layers over a long session, and a probe is
a synchronous point-and-wait UI action.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from datetime import datetime, timezone
from typing import Any

from .extract_timeseries_at_point import detect_frame_sequences
from .query_point_hazard import (
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

logger = logging.getLogger("grace2_agent.tools.probe_point")

#: Max raster layers opened per probe click (honest cap, not a silent drop --
#: the response carries ``truncated: true`` when the case has more).
MAX_PROBE_LAYERS = 40


# ---------------------------------------------------------------------------
# Typed errors (FR-AS-11) -- mirrors query_point_hazard's error shape.
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
    """SYNC: stage + sample one non-series raster layer at a point.

    Returns the honest per-layer result entry (``value: None`` + ``note`` for
    outside-extent/nodata, ``value: None`` + ``error`` for an unreadable
    layer) -- never raises.
    """
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
    """SYNC: stage + sample one frame of a detected sequence at a point.

    Returns ``(entry, units)`` -- ``units`` is the frame's own units (when
    readable), so the caller can pick the first non-null one for the series.
    """
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
    """Sample every raster layer (and detected frame sequence) on a case at
    one point -- the deterministic core behind ``POST /api/probe-point``.

    Non-series raster layers each become a single result entry
    ``{layer_id, name, value, units, note?, error?}``. Raster layers that
    ``extract_timeseries_at_point.detect_frame_sequences`` groups into an
    animation-frame stack collapse into ONE entry
    ``{name, series: [{label, value, note?, error?}, ...], units,
    layer_ids}`` -- the same stem/token grouping the web scrubber and
    ``extract_timeseries_at_point`` use. Vector layers are not sampled (a
    point probe of a vector needs a different query shape) and are simply
    absent from ``results``.

    Raises ``ProbePointInputError`` on a missing/invalid location,
    ``ProbePointCaseNotFoundError`` when the case does not exist or
    persistence is unavailable. A Case with zero raster layers is NOT an
    error -- it returns ``results: []`` (the click succeeded; there is
    nothing to sample).
    """
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
    with tempfile.TemporaryDirectory(prefix="grace2_probe_point_") as tmpdir:
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
