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
from ..spec import record_shape

logger = logging.getLogger(
    "trid3nt_server.tools.fetchers._router.executors.animation_frames"
)

__all__ = ["execute"]


def execute(
    spec: SourceSpec, params: dict[str, Any], metadata: AtomicToolMetadata
) -> list[LayerURI]:

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
                record_shape=record_shape(spec),
            )
        except FrameDegraded as exc:
            # A degraded frame is recorded and dropped: never a silent gap, never fatal alone.
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
                # A frame MAY override the row-level preset; None falls back to it.
                style=frame.style or spec.output.style,
                role=spec.output.role,
                units=spec.normalize.units,
                bbox=frame.bbox,
                # The frame's own validity window becomes the layer's fixed temporal range.
                valid_from=frame.valid_from,
                valid_to=frame.valid_to,
            )
        )

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
