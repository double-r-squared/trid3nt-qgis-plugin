"""``extract_timeseries_at_point`` atomic tool -- point series over case animation frames.

Given a location (lon/lat pair OR a geocoded place name -- the same resolution
seam as ``query_point_hazard``) and a time-stepped raster SEQUENCE loaded on
the current Case (animation frames: a SFINCS flood-depth step stack, GOES
frame stack, HRRR forecast hours, ...), sample EVERY frame at the point and
return the ordered series ``[(timestamp_or_index_label, value), ...]``.

How frame sequences are represented
===================================

Animation frames live in ``CaseSummary.loaded_layer_summaries`` as SIBLING
raster layers whose NAMES differ only in a monotonic frame token -- exactly
what the web LayerPanel's sequential-layer grouping detects client-side
(``web/src/LayerPanel.tsx`` ``parseFrameToken`` / ``detectSequentialGroups``).
This module ports that detection: the token patterns (``F+03h``, ``hr 6``,
``step 4`` / ``frame 02`` / ``idx 3``, ``t+2``, ``#3``, ``day 1``) and the
ISO-8601 valid-time label preference are the same, and grouping is equally
conservative -- a group forms only from >= 2 raster layers sharing a stem
with strictly-increasing token values.

Honesty (data-source fallback norm)
===================================

- A Case with no layers, or no detectable frame sequence, raises the typed
  ``NoFrameSequenceError`` (listing any near-miss stems) -- the agent
  narrates the honest absence instead of inventing a series.
- A frame that cannot be read, or where the point is outside/nodata, is an
  honest ``value=None`` entry with its reason; the series is never
  gap-filled.

``cacheable=False`` (``ttl_class="live-no-cache"``): depends on the live Case
layer list.
"""

from __future__ import annotations

import logging
import re
import tempfile
from datetime import datetime, timezone
from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .query_point_hazard import (
    NoCaseBoundError,
    layers_from_case,
    resolve_case_id,
    resolve_point,
    sample_raster_at_point,
    stage_layer_local,
)

__all__ = [
    "extract_timeseries_at_point",
    "parse_frame_token",
    "detect_frame_sequences",
    "TimeseriesError",
    "TimeseriesInputError",
    "NoFrameSequenceError",
    "TimeseriesUpstreamError",
]

logger = logging.getLogger("trid3nt_server.tools.extract_timeseries_at_point")


# ---------------------------------------------------------------------------
# Typed errors (FR-AS-11).
# ---------------------------------------------------------------------------


class TimeseriesError(RuntimeError):
    """Base class for extract_timeseries_at_point failures."""

    error_code: str = "TIMESERIES_ERROR"
    retryable: bool = True


class TimeseriesInputError(TimeseriesError):
    """Bad inputs (no/invalid location, geocode miss)."""

    error_code = "TIMESERIES_INPUT_INVALID"
    retryable = False


class NoFrameSequenceError(TimeseriesError):
    """The Case has no detectable time-stepped raster sequence -- honest miss."""

    error_code = "TIMESERIES_NO_FRAME_SEQUENCE"
    retryable = False


class TimeseriesUpstreamError(TimeseriesError):
    """Persistence lookup / staging infrastructure failed."""

    error_code = "TIMESERIES_UPSTREAM_ERROR"
    retryable = True


# ---------------------------------------------------------------------------
# Metadata.
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="extract_timeseries_at_point",
    ttl_class="live-no-cache",
    source_class=None,
    cacheable=False,
)


# ---------------------------------------------------------------------------
# Frame-token parsing -- a Python port of web/src/LayerPanel.tsx
# ``parseFrameToken`` (keep the two in lockstep; the web side is the
# user-visible grouping this tool must agree with).
# ---------------------------------------------------------------------------

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
    """Parse a monotonic frame token out of a layer name.

    Returns ``{"value": int, "label": str, "stem": str}`` or ``None`` when no
    lead-time / step / index token is present. Mirrors the web's
    ``parseFrameToken`` so the tool groups the same series the LayerPanel
    scrubber shows.
    """
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
    """Group the Case's RASTER layers into frame sequences by shared stem.

    Conservative (the web convention): only stems with >= 2 members whose
    token values are strictly increasing after sorting form a sequence.
    Returns ``{stem: [ {layer, value, label}, ... ordered ]}``.
    """
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


# ---------------------------------------------------------------------------
# Registered tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    read_only_hint=True,
    open_world_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
)
async def extract_timeseries_at_point(
    lon: float | None = None,
    lat: float | None = None,
    place: str | None = None,
    layer: str | None = None,
    case_id: str | None = None,
    # job-0164: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Extract a time series at a point from the Case's animation-frame stack.

    **What it does:** Finds the time-stepped raster SEQUENCE loaded on the
    Case (animation frames -- sibling raster layers whose names share a stem
    and differ only in a monotonic ``step N`` / ``F+03h`` / ``t+2`` / ISO
    valid-time token, the same series the map scrubber groups), samples every
    frame at the given location, and returns the ordered series
    ``[(timestamp_or_index, value), ...]``.

    **When to use:**
    - "How does the flood depth at my house evolve over the simulation?"
    - "Plot the water level at the pier over time" after an animated solve
      (SFINCS steps, SWMM overland depth, GOES/GLM frame stacks, HRRR hours).

    **When NOT to use:**
    - A single-time value across all layers -- use ``query_point_hazard``.
    - Frames of a RUN not yet loaded on the Case -- use ``list_run_frames``
      + the sandbox instead.
    - Station/gauge observations (use the fetch_* observation tools).

    **Parameters:**
    - ``lon`` / ``lat``: explicit EPSG:4326 coordinates (both required when
      used; wins over ``place``).
    - ``place``: free-text place name to geocode.
    - ``layer``: optional sequence selector matched against the sequence stem
      (e.g. ``"flood depth"``). Default: the Case's largest sequence (others
      are listed in ``available_sequences``).
    - ``case_id``: the Case to read. Default: the turn's bound Case.

    **Returns:** dict with ``sequence`` (the stem matched), ``frame_count``,
    ``series`` (ordered ``[{index, label, value, note?, error?}, ...]`` --
    ``label`` is the frame's ISO valid-time when the name carries one, else
    the step/lead token), ``units`` (when known), ``location``,
    ``available_sequences`` (all detected stems), ``sampled_count``,
    ``computed_at``. Unreadable/nodata frames are honest ``value=None``
    entries -- the series is never gap-filled.

    **Errors (FR-AS-11):** ``TimeseriesInputError`` (no/invalid location),
    ``NoCaseBoundError`` (no case), ``NoFrameSequenceError`` (the Case has no
    detectable frame sequence, or none matches ``layer``),
    ``TimeseriesUpstreamError`` (persistence unavailable / case not found).
    """
    q_lon, q_lat, label = resolve_point(lon, lat, place, TimeseriesInputError)
    resolved_case = resolve_case_id(case_id, NoCaseBoundError)
    layers, _case_bbox, case_title, _case = await layers_from_case(
        resolved_case, TimeseriesUpstreamError
    )

    if not layers:
        raise NoFrameSequenceError(
            f"case {resolved_case!r} has no loaded layers -- no animation "
            "frames to sample."
        )

    sequences = detect_frame_sequences(layers)
    if not sequences:
        raster_names = [
            str(l.get("name") or "?") for l in layers if l.get("layer_type") == "raster"
        ]
        raise NoFrameSequenceError(
            f"case {resolved_case!r} has no detectable time-stepped raster "
            "sequence (>= 2 sibling frames sharing a name stem with a "
            "monotonic step/hour/time token). Raster layers present: "
            f"{', '.join(raster_names[:12]) or 'none'}."
        )

    if layer is not None and str(layer).strip():
        want = re.sub(r"\s+", " ", str(layer)).strip().lower()
        matches = {s: m for s, m in sequences.items() if want in s or s in want}
        if not matches:
            raise NoFrameSequenceError(
                f"no frame sequence matches {layer!r}; available sequences: "
                f"{', '.join(sorted(sequences))}."
            )
        stem = max(matches, key=lambda s: len(matches[s]))
        members = matches[stem]
    else:
        stem = max(sequences, key=lambda s: len(sequences[s]))
        members = sequences[stem]

    series: list[dict[str, Any]] = []
    units: str | None = None
    sampled = 0
    with tempfile.TemporaryDirectory(prefix="trid3nt_timeseries_") as tmpdir:
        for i, member in enumerate(members):
            frame_layer = member["layer"]
            entry: dict[str, Any] = {
                "index": member["value"],
                "label": member["label"],
                "value": None,
            }
            uri = str(frame_layer.get("uri") or "")
            try:
                if not uri:
                    raise ValueError("frame layer has no uri")
                local = stage_layer_local(uri, tmpdir, f"frame{i}")
                value, note, tag_units = sample_raster_at_point(local, q_lon, q_lat)
                entry["value"] = value
                if note:
                    entry["note"] = note
                if units is None:
                    units = frame_layer.get("units") or tag_units
                if value is not None:
                    sampled += 1
            except Exception as exc:  # noqa: BLE001 -- honest per-frame entry
                entry["error"] = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "extract_timeseries_at_point: frame %r unreadable: %s",
                    frame_layer.get("name"),
                    exc,
                )
            series.append(entry)

    logger.info(
        "extract_timeseries_at_point: case=%s stem=%r frames=%d sampled=%d "
        "point=(%.5f, %.5f)",
        resolved_case,
        stem,
        len(series),
        sampled,
        q_lon,
        q_lat,
    )
    return {
        "sequence": stem,
        "case_id": resolved_case,
        "case_title": case_title,
        "frame_count": len(series),
        "series": series,
        "units": units,
        "location": {"lon": q_lon, "lat": q_lat, "label": label},
        "available_sequences": sorted(sequences),
        "sampled_count": sampled,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }
