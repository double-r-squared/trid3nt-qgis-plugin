"""HEC-RAS emit-on-solve composer fork (ADR 0287).

The producers (``postprocess_hecras`` for the 6.x lineage;
``model_hecras_flood_2d_rog`` for the 2025 managed lineage) write the per-step
depth frame stream to ``outputs.json`` host-side. This module is the COMPOSER
half: it reads the manifest back with ``frames_only=True`` (the seam owns the
TEMPORAL FRAMES only -- the typed ``HecrasDepthLayerURI`` peak + its narration
scalars stay composer-built), and emits each frame COG out-of-band through the
render chokepoint so the web ``detectSequentialGroups`` scrubber group forms.

The HEC-RAS analogue of ``modflow._frame_emit`` -- shared by all four depth-class
composers (riverine_flood, levee_breach, flood_2d inflow + RoG) so the read +
publish + emit obligations live in ONE place.
"""

from __future__ import annotations

import asyncio
import logging
import types as _types
from typing import Any

from trid3nt_contracts.execution import LayerURI

from trid3nt_server.emission.outputs_seam import (
    build_layers_from_outputs,
    read_outputs_manifest,
)

logger = logging.getLogger("trid3nt_server.workflows.hecras._frame_emit")

__all__ = ["read_hecras_frame_layers", "emit_hecras_frames", "read_and_emit_hecras_frames"]


def read_hecras_frame_layers(
    run_id: str, bbox: tuple[float, float, float, float] | None
) -> list[LayerURI]:
    """Read ``outputs.json`` -> the SEAM's temporal frame LayerURIs (frames-only).

    The producer wrote the peak + every per-step depth COG to ``outputs.json``. This
    reads it back and builds the CONTEXT frame layers via ``build_layers_from_outputs``
    with ``frames_only=True`` (the peak entry is skipped -- the composer keeps its own
    typed peak, so the same COG uri is never registered twice). A HEC-RAS run yields
    ONE ``flood_depth`` temporal group. Returns ``[]`` on an absent / unreadable /
    unknown-schema manifest (an honest peak-only degrade) -- never raises. Runs off
    the event loop (a small object GET + pure build).
    """
    manifest = read_outputs_manifest(_types.SimpleNamespace(run_id=run_id))
    if manifest is None:
        logger.info(
            "hecras frames: no outputs.json for run_id=%s -- peak-only (no "
            "animation frames).",
            run_id,
        )
        return []
    seam = build_layers_from_outputs(
        manifest,
        run_id=run_id,
        bbox=tuple(bbox) if bbox else None,
        frames_only=True,
    )
    return [lyr for lyr in seam.layers if lyr.role == "context"]


async def emit_hecras_frames(
    emitter: Any, frame_layers: list[LayerURI], run_id: str
) -> int:
    """Publish + emit per-step COGs out-of-band so the web scrubber group forms.

    Each frame COG is routed through ``publish_layer`` (the render chokepoint) so it
    carries a renderable /tiles or WMS URL before ``add_loaded_layer``; without this
    every frame is a raw object-store COG the guardrail drops and the scrubber group
    never forms. The seam-resolved ``continuous_flood_depth`` preset + the
    ``"Flood depth step N"`` name token ride through so the group forms with the
    peak's physical colormap. A frame that fails to publish is HONESTLY DROPPED (its
    raw uri never renders); the remaining frames + the typed peak stay intact.

    The publish compute is thread-offloaded (loop-safety norm); the
    ``add_loaded_layer`` emit stays on the loop. Returns the number emitted (0 when
    no emitter is bound -- the direct/smoke/test path). Never raises.
    """
    if not frame_layers or emitter is None:
        if frame_layers:
            logger.info(
                "hecras frames: %d animation frames available but no emitter bound "
                "(direct/smoke/test) -- frames not emitted.",
                len(frame_layers),
            )
        return 0

    from trid3nt_server.emission.publish import (
        PublishLayerError,
        publish_layer,
    )

    emitted = 0
    for lyr in frame_layers:
        if not (lyr.uri.startswith("gs://") or lyr.uri.startswith("s3://")):
            emit_layer: LayerURI = lyr
        else:
            try:
                frame_uri = await asyncio.to_thread(
                    publish_layer,
                    layer_uri=lyr.uri,
                    layer_id=lyr.layer_id,
                    style_preset=lyr.style_preset,
                )
            except PublishLayerError as exc:
                logger.warning(
                    "hecras frames: publish_layer FAILED for frame layer_id=%s "
                    "error_code=%s (%s) -- dropping this frame (its raw uri never "
                    "renders).",
                    lyr.layer_id,
                    getattr(exc, "error_code", "?"),
                    exc,
                )
                continue
            emit_layer = LayerURI(
                layer_id=lyr.layer_id,
                name=lyr.name,
                layer_type=lyr.layer_type,
                uri=frame_uri,
                style_preset=lyr.style_preset,
                role=lyr.role,
                units=lyr.units,
                bbox=lyr.bbox,
            )
        try:
            await emitter.add_loaded_layer(emit_layer)
            emitted += 1
        except Exception as exc:  # noqa: BLE001 -- never break the run
            logger.warning(
                "hecras frames: frame add_loaded_layer failed for %s: %s",
                emit_layer.layer_id,
                exc,
            )
    if emitted:
        logger.info(
            "hecras frames: emitted %d/%d animation frames as the flood_depth "
            "sequential group (run_id=%s)",
            emitted,
            len(frame_layers),
            run_id,
        )
    return emitted


async def read_and_emit_hecras_frames(
    emitter: Any,
    *,
    run_id: str | None,
    bbox: tuple[float, float, float, float] | None,
) -> int:
    """Convenience: read the frame stream (off-loop) + emit it (best-effort).

    The composer entry point -- ``None`` run_id (a legacy producer that wrote no
    manifest) or no emitter degrades to peak-only. Never raises: a frame
    read/publish/emit failure never sinks the typed peak the composer already
    emitted.
    """
    if not run_id:
        return 0
    try:
        frame_layers = await asyncio.to_thread(
            read_hecras_frame_layers, run_id, bbox
        )
        return await emit_hecras_frames(emitter, frame_layers, run_id)
    except Exception as exc:  # noqa: BLE001 -- frames are additive, never fatal
        logger.warning(
            "hecras frames: read/emit failed (non-fatal, peak intact) run_id=%s: %s",
            run_id,
            exc,
        )
        return 0
