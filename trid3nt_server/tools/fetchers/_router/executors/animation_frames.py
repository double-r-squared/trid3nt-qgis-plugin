"""animation_frames executor: the FRAMES-LIST output shape.

An animation source is an ORDERED per-timestamp sequence, so each frame is its own
cache entry and its own ``LayerURI``. A run that produced NO frames raises the
source's typed EMPTY error, never a silent empty list."""

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
    "trid3nt_server.tools.fetchers._router.executors.animation_frames"
)

__all__ = ["execute"]


def execute(
    spec: SourceSpec, params: dict[str, Any], metadata: AtomicToolMetadata
) -> list[LayerURI]:
    """Drive the per-frame read_through loop and emit an ordered ``list[LayerURI]``."""

    # The two source hooks own the source-specific steps and nothing else:
    # ``frames_plan`` is the pre-loop resolve (fetch the timestamp index, window,
    # subsample, filter) returning the ordered per-frame plans, and raising the
    # source's typed EMPTY when the window matched no frames; ``frame_bytes`` builds
    # ONE frame's COG bytes and raises ``FrameDegraded`` to skip a single transparent,
    # off-swath or upstream-failed frame. The executor owns the loop, the cache, the
    # per-frame degrade and the LayerURI emission.
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
                style=frame.style or spec.output.style,
                role=spec.output.role,
                units=spec.normalize.units,
                bbox=frame.bbox,
                # The frame's own validity window, declared by the plan that
                # already held the instants: the map stamps it as this layer's
                # fixed temporal range and the temporal controller plays the
                # sequence with no name to parse.
                valid_from=frame.valid_from,
                valid_to=frame.valid_to,
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
