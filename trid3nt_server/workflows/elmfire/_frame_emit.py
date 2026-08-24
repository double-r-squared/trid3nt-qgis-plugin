"""ELMFIRE emit-on-solve composer fork (ADR 0288).

The producer (``postprocess_elmfire`` with ``write_frames_manifest=True``) writes
the hourly burned-extent frame stream to ``outputs.json`` agent-side -- each frame
is a per-hour threshold of the single solved time-of-arrival (ToA) raster
(``toa <= hour``), a LOSSLESS query of the run's complete spatiotemporal solution
(not an invented intermediate state). This module is the COMPOSER half: it reads
the manifest back with ``frames_only=True`` (the seam owns the TEMPORAL FRAMES
only -- the typed ``FireSpreadLayerURI`` peak + its narration scalars stay
composer-built), and emits each frame COG out-of-band through the render chokepoint
so the web ``detectSequentialGroups`` scrubber group forms.

The ELMFIRE analogue of ``modflow._frame_emit`` / ``hecras._frame_emit`` -- shared
by the frame-consuming fire composers (fire_spread + the spotting river-barrier
demo) so the read + publish + emit obligations live in ONE place. A fire run yields
ONE ``fire_arrival`` temporal group (the burned-extent animation).
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

logger = logging.getLogger("trid3nt_server.workflows.elmfire._frame_emit")

__all__ = [
    "read_elmfire_frame_layers",
    "emit_elmfire_frames",
    "read_and_emit_elmfire_frames",
]


def read_elmfire_frame_layers(
    run_id: str, bbox: tuple[float, float, float, float] | None
) -> list[LayerURI]:
    """Read ``outputs.json`` -> the SEAM's temporal frame LayerURIs (frames-only).

    ``postprocess_elmfire`` (``write_frames_manifest=True``) wrote the peak + every
    per-hour burned-extent COG to ``outputs.json`` agent-side. This reads it back and
    builds the CONTEXT frame layers via ``build_layers_from_outputs`` with
    ``frames_only=True`` (the peak entry -- ``t=None`` -- is skipped, so the composer
    keeps its own typed ToA peak and the primary COG uri is never registered twice).
    A fire run yields ONE ``fire_arrival`` temporal group. Returns ``[]`` on an absent
    / unreadable / unknown-schema manifest (an honest peak-only degrade) -- never
    raises. Runs off the event loop (a small object GET + pure build).
    """
    manifest = read_outputs_manifest(_types.SimpleNamespace(run_id=run_id))
    if manifest is None:
        logger.info(
            "elmfire frames: no outputs.json for run_id=%s -- peak-only (no "
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


async def emit_elmfire_frames(
    emitter: Any, frame_layers: list[LayerURI], run_id: str
) -> int:
    """Publish + emit per-hour burned-extent COGs so the web scrubber group forms.

    Each frame COG is routed through ``publish_layer`` (the render chokepoint) so it
    carries a renderable /tiles or WMS URL before ``add_loaded_layer``; without this
    every frame is a raw object-store COG the guardrail drops and the scrubber group
    never forms. The seam-resolved ``continuous_fire_arrival_hr`` preset + the
    ``"Burned area step N"`` name token ride through so the group forms with the
    peak's physical arrival-time colormap. A frame that fails to publish is HONESTLY
    DROPPED (its raw uri never renders); the remaining frames + the typed peak stay
    intact.

    The publish compute (COG upload + publish-status polls) is thread-offloaded
    (loop-safety norm); the ``add_loaded_layer`` emit stays on the loop. Returns the
    number emitted (0 when no emitter is bound -- the direct/smoke/test path). Never
    raises.
    """
    if not frame_layers or emitter is None:
        if frame_layers:
            logger.info(
                "elmfire frames: %d animation frames available but no emitter bound "
                "(direct/smoke/test) -- frames not emitted.",
                len(frame_layers),
            )
        return 0

    from trid3nt_server.tools.publish_layer.publish_layer import (
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
                    "elmfire frames: publish_layer FAILED for frame layer_id=%s "
                    "error_code=%s (%s) -- dropping this frame (its raw uri never "
                    "renders).",
                    lyr.layer_id,
                    getattr(exc, "error_code", "?"),
                    exc,
                )
                continue
            # Re-wrap as a plain LayerURI carrying the renderable url + the seam's
            # name token / style / context role (frames carry no narration scalars
            # -- the typed ToA peak drives narration).
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
                "elmfire frames: frame add_loaded_layer failed for %s: %s",
                emit_layer.layer_id,
                exc,
            )
    if emitted:
        logger.info(
            "elmfire frames: emitted %d/%d animation frames as the fire_arrival "
            "sequential group (run_id=%s)",
            emitted,
            len(frame_layers),
            run_id,
        )
    return emitted


async def read_and_emit_elmfire_frames(
    emitter: Any,
    *,
    run_id: str | None,
    bbox: tuple[float, float, float, float] | None,
) -> int:
    """Convenience: read the frame stream (off-loop) + emit it (best-effort).

    The composer entry point -- ``None`` run_id (a producer that wrote no manifest)
    or no emitter degrades to peak-only. Never raises: a frame read/publish/emit
    failure never sinks the typed ToA peak the composer already emitted.
    """
    if not run_id:
        return 0
    try:
        frame_layers = await asyncio.to_thread(
            read_elmfire_frame_layers, run_id, bbox
        )
        return await emit_elmfire_frames(emitter, frame_layers, run_id)
    except Exception as exc:  # noqa: BLE001 -- frames are additive, never fatal
        logger.warning(
            "elmfire frames: read/emit failed (non-fatal, peak intact) run_id=%s: %s",
            run_id,
            exc,
        )
        return 0
