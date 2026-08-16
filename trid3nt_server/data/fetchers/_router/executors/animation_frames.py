"""animation_frames executor: the FRAMES-LIST output shape.

Selected when a spec declares ``shape: animation_frames``. Unlike every other
executor (which returns ONE ``bytes`` body the ``route()`` read_through caches into
ONE LayerURI), an animation source is an ORDERED, per-timestamp sequence: each
frame is its OWN cache entry and its OWN ``LayerURI``, so this executor owns the
per-frame ``read_through`` loop and returns ``list[LayerURI]`` directly (``route()``
returns the list, no top-level read_through).

The two PURE-ish source hooks own the source-specific steps; the executor owns the
loop, the cache, the per-frame graceful-degrade + honesty floor, and the LayerURI
emission:

- ``hooks.frames_plan(spec, params) -> list[FramePlan]`` -- the pre-loop resolve:
  fetch the timestamp index, window it, subsample, (optionally) filter, and return
  the ordered per-frame plans (each with its cache_params + scrubber name-token +
  layer_id + bbox). Raises the source's typed EMPTY when the window matched no
  frames (honesty floor: an empty window is a hard typed no-data, never [] success).
- ``hooks.frame_bytes(spec, params, frame) -> bytes`` -- build ONE frame's COG
  bytes (the SLIDER tile-stitch mosaic, or a post-stitch blend). It raises
  ``FrameDegraded`` to skip a single transparent / off-swath / upstream-failed
  frame; the executor records the skip and drops it.

Honesty floor: a run that produced NO frames (every frame degraded) raises the
source's typed EMPTY error naming the last degradation -- never a silent empty list,
never a fabricated success.
"""

from __future__ import annotations

import logging
from typing import Any

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.source_spec import SourceSpec
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from ....cache import read_through
from ..errors import router_empty_error
from ..hooks import FrameDegraded, FramePlan, resolve_hook

logger = logging.getLogger(
    "trid3nt_server.data.fetchers._router.executors.animation_frames"
)

__all__ = ["execute"]


def execute(
    spec: SourceSpec, params: dict[str, Any], metadata: AtomicToolMetadata
) -> list[LayerURI]:
    """Drive the per-frame read_through loop and emit an ordered ``list[LayerURI]``."""
    frames_plan = resolve_hook(spec.hooks.frames_plan)  # type: ignore[union-attr]
    frame_bytes = resolve_hook(spec.hooks.frame_bytes)  # type: ignore[union-attr]

    frames: list[FramePlan] = frames_plan(spec, params)

    layers: list[LayerURI] = []
    n_degraded = 0
    last_note: str | None = None
    for frame in frames:
        try:
            result = read_through(
                metadata=metadata,
                params=frame.cache_params,
                ext=spec.output.ext,
                fetch_fn=lambda f=frame: frame_bytes(spec, params, f),
            )
        except FrameDegraded as exc:
            # A single degraded frame (transparent / off-swath / upstream-failed) is
            # recorded and dropped -- never a silent gap, never fatal on its own.
            n_degraded += 1
            last_note = str(exc)
            logger.warning(
                "animation_frames: %s frame %s degraded (%s)",
                spec.name,
                frame.cache_params.get("ts_int"),
                exc,
            )
            continue
        assert result.uri is not None, "animation frame is cacheable; uri must be set"
        layers.append(
            LayerURI(
                layer_id=frame.layer_id,
                name=frame.name,
                layer_type=spec.output.layer_type,
                uri=result.uri,
                # A frame MAY override the spec-level preset (the archive
                # source's per-band goes_rgb_animation vs goes_fire_hotspots_rgba);
                # None falls back to the spec preset (no-op for single-preset sources).
                style_preset=frame.style_preset or spec.output.style_preset,
                role=spec.output.role,
                units=spec.normalize.units,
                bbox=frame.bbox,
            )
        )

    # Honesty floor: a run that produced NO frames is not success.
    if not layers:
        raise router_empty_error(
            spec.error_code_prefix,
            f"{spec.name}: every one of {len(frames)} frames was empty/failed over "
            f"the AOI" + (f": {last_note}" if last_note else ""),
            spec.empty_error_suffix,
        )
    logger.info(
        "animation_frames: %s emitted %d frames (%d degraded skipped)",
        spec.name,
        len(layers),
        n_degraded,
    )
    return layers
