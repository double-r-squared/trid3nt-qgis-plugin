"""Agent on-box generic output-quantity executor (STEP 2; DEFAULT-OFF).

The agent-side executor for the declarative output-quantity registry
(``grace2_contracts.output_quantities``). It walks ``get_output_registry(engine)``,
runs each spec's reader, routes the typed ``FieldResult`` to the shared STEP-1
plumbing, assembles an in-memory ``PublishManifest``, and hands it to the ONE
register-only registrar (``register_manifest_layers``). This is the executor the
audit's "generic output-quantity publisher" abstraction is built on: an engine
adds a published field by registering an ``OutputQuantitySpec``, not by writing a
bespoke postprocess.

Routing:

  - ``RasterField``  -> ``cog_io.write_cog_4326_from_grid`` (+ upload) -> ONE
    manifest raster layer (the spec's name / style_preset / role / units).
  - ``TimeseriesField`` -> ``frames.emit_timeseries_layers`` (the shared corrupt
    -frame-degrades-to-peak + "< 2 never groups" guards) -> a PEAK manifest layer
    (role "primary", "Peak <q>") + N frame layers (role "context", "<q> step N").
  - ``ScalarField`` -> merged into the manifest's top-level ``metrics`` (no layer).

DEFAULT-OFF: the registry is an EMPTY scaffold today (STEP 3 migrates engines),
so ``publish_quantities`` of any engine produces an empty manifest + registers
nothing - decks are byte-identical. The executor is importable, typed, and
unit-tested against a FAKE registry now; the per-engine migration is STEP 3.

NO worker wiring (STEP 4): this runs ON THE AGENT (it reuses the on-box cog_io +
frames + the agent-only ``register_manifest_layers``). The worker executor is a
separate, gated STEP-4 deliverable.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Iterable

from grace2_contracts.output_quantities import (
    OutputQuantitySpec,
    RasterField,
    ScalarField,
    TimeseriesField,
    get_output_registry,
)
from grace2_contracts.publish_manifest import (
    MANIFEST_SCHEMA_VERSION,
    PublishManifest,
    PublishManifestBandStats,
    PublishManifestLayer,
)

from . import cog_io, frames

logger = logging.getLogger("grace2_agent.workflows.publish_quantities")

__all__ = [
    "QuantityExecError",
    "build_quantities_manifest",
    "publish_quantities",
]


class QuantityExecError(RuntimeError):
    """Raised when a REQUIRED quantity's read/write/upload fails irrecoverably.

    Carries ``quantity_id`` + ``stage`` so the workflow can narrate which output
    quantity failed. A TIMESERIES frame failure does NOT raise (it degrades to
    peak-only via the shared frames guard); a PEAK or single-raster failure does.
    """

    def __init__(
        self, quantity_id: str, *, stage: str, message: str
    ) -> None:
        super().__init__(message)
        self.quantity_id = quantity_id
        self.stage = stage


# Signature the executor uses to upload a staged COG (the engine passes its own
# scheme-aware shim, e.g. ``postprocess_swmm._upload_cog_to_runs_bucket``).
UploadFn = Callable[..., str]


def _build_raster_layer(
    spec: OutputQuantitySpec,
    rf: RasterField,
    *,
    run_id: str,
    upload: UploadFn,
    dest_filename: str,
    name: str | None = None,
    role: str | None = None,
    frame_no: int | None = None,
) -> PublishManifestLayer:
    """Write+upload ONE RasterField and build its manifest layer entry.

    Raises ``cog_io.CogIoError`` on a write/upload failure (the caller decides
    whether to re-raise as fatal - peak/single - or swallow - a frame).
    """
    cog = cog_io.write_cog_4326_from_grid(
        rf.grid,
        src_crs=rf.src_crs,
        src_transform=rf.src_transform,
        reproject=rf.reproject,
        mask=rf.mask,
        crs_roundtrip_guard=rf.crs_roundtrip_guard,
    )
    bbox = cog_io.cog_bbox_4326(cog)
    try:
        uri = upload(cog, run_id, None, dest_filename=dest_filename)
    finally:
        cog_io.safe_unlink(cog)

    stem = spec.quantity_id
    if frame_no is not None:
        layer_id_stem = frames.frame_layer_id(stem, frame_no, run_id="").rstrip("-")
    else:
        layer_id_stem = frames.peak_layer_id(stem, run_id="").rstrip("-")

    return PublishManifestLayer(
        layer_id_stem=layer_id_stem,
        name=name or spec.name,
        layer_type="raster",
        role=role or spec.role,
        style_preset=spec.style_preset,
        units=spec.units,
        cog_uri=uri,
        frame_no=frame_no,
        bbox=list(bbox) if bbox else None,
        has_overviews=True,
        band_stats=PublishManifestBandStats(),
        metrics=dict(rf.metrics or {}),
    )


def build_quantities_manifest(
    engine: str,
    *,
    run_id: str,
    upload: UploadFn,
    reader_ctx: Any = None,
    specs: Iterable[OutputQuantitySpec] | None = None,
    enabled: Callable[[OutputQuantitySpec], bool] | None = None,
) -> PublishManifest:
    """Walk the registry, run readers, assemble the in-memory ``PublishManifest``.

    Args:
        engine: the engine token (``get_output_registry`` key) when ``specs`` is
            None.
        run_id: the run id the COGs key under.
        upload: the scheme-aware COG uploader (engine shim;
            ``upload(cog_path, run_id, runs_bucket=None, *, dest_filename=...)``).
        reader_ctx: opaque context passed to each ``spec.reader(reader_ctx)`` (the
            run output handles the engine reader needs). The reader returns a
            ``FieldResult``.
        specs: explicit spec iterable (tests pass a fake registry); defaults to
            ``get_output_registry(engine)``.
        enabled: optional gate ``(spec) -> bool``; defaults to ``spec.default_on``
            (DEFAULT-OFF - a spec is published only when opted in).

    Returns a ``PublishManifest`` (schema_version 1) whose ``layers`` are the
    raster + timeseries layers and whose ``metrics`` aggregates the ScalarFields +
    the peak RasterField metrics. NEVER bumps the manifest schema_version.

    Raises ``QuantityExecError`` only on a PEAK / single-raster read-write-upload
    failure (a frame failure degrades to peak-only via the frames guard).
    """
    if specs is None:
        specs = get_output_registry(engine)
    if enabled is None:
        enabled = lambda s: s.default_on  # noqa: E731 - DEFAULT-OFF gate

    layers: list[PublishManifestLayer] = []
    metrics: dict[str, Any] = {}

    for spec in specs:
        if not enabled(spec):
            logger.info(
                "publish_quantities: quantity %s is DEFAULT-OFF (not enabled); "
                "skipping (engine=%s run_id=%s)",
                spec.quantity_id,
                engine,
                run_id,
            )
            continue
        if spec.reader is None:
            logger.info(
                "publish_quantities: quantity %s has no bound reader (scaffold "
                "entry); skipping honestly (engine=%s)",
                spec.quantity_id,
                engine,
            )
            continue

        result = spec.reader(reader_ctx)

        if isinstance(result, ScalarField):
            metrics.update(result.values or {})
            continue

        if isinstance(result, RasterField):
            try:
                layer = _build_raster_layer(
                    spec,
                    result,
                    run_id=run_id,
                    upload=upload,
                    dest_filename=f"{spec.quantity_id}_peak.tif",
                )
            except cog_io.CogIoError as exc:
                raise QuantityExecError(
                    spec.quantity_id, stage=exc.stage, message=exc.message
                ) from exc
            layers.append(layer)
            # The single-raster (peak) metrics feed the run aggregates too.
            metrics.update(result.metrics or {})
            continue

        if isinstance(result, TimeseriesField):
            _append_timeseries_layers(
                spec, result, run_id=run_id, upload=upload, layers=layers,
                metrics=metrics,
            )
            continue

        logger.warning(
            "publish_quantities: quantity %s reader returned an unrecognized "
            "FieldResult %r; skipping",
            spec.quantity_id,
            type(result).__name__,
        )

    return PublishManifest(
        schema_version=MANIFEST_SCHEMA_VERSION,
        engine=engine,
        run_id=run_id,
        status="ok",
        frame_count=sum(1 for layer in layers if layer.frame_no is not None),
        metrics=metrics,
        layers=layers,
    )


def _append_timeseries_layers(
    spec: OutputQuantitySpec,
    tf: TimeseriesField,
    *,
    run_id: str,
    upload: UploadFn,
    layers: list[PublishManifestLayer],
    metrics: dict[str, Any],
) -> None:
    """Emit the PEAK layer (always) + the animation-frame layers (degrade-safe).

    The PEAK is published unconditionally (a TimeseriesField always has a
    representative peak). The frames ride the shared ``frames.emit_timeseries_layers``
    corrupt-frame-degrades-to-peak + "< 2 never groups" guards: a frame failure
    abandons the frame set (peak still stands), never sinking the run.
    """
    # --- PEAK (layers[0] of this quantity) ---
    try:
        peak_layer = _build_raster_layer(
            spec,
            tf.peak,
            run_id=run_id,
            upload=upload,
            dest_filename=f"{spec.quantity_id}_peak.tif",
            name=frames.peak_layer_name(tf.quantity_label),
            role="primary",
        )
    except cog_io.CogIoError as exc:
        raise QuantityExecError(
            spec.quantity_id, stage=exc.stage, message=exc.message
        ) from exc
    layers.append(peak_layer)
    metrics.update(tf.peak.metrics or {})

    # --- frames (degrade-safe via the shared guard) ---
    written: list[PublishManifestLayer] = []

    def _write_frame(frame_no: int, raw_idx: int) -> frames.EmittedFrame:
        rf = tf.read_step(raw_idx)
        layer = _build_raster_layer(
            spec,
            rf,
            run_id=run_id,
            upload=upload,
            dest_filename=frames.frame_dest_filename(spec.quantity_id, frame_no),
            name=frames.frame_name(frame_no, tf.quantity_label),
            role="context",
            frame_no=frame_no,
        )
        written.append(layer)
        return frames.EmittedFrame(
            frame_no=frame_no,
            uri=layer.cog_uri,
            bbox=tuple(layer.bbox) if layer.bbox else None,
            metrics=layer.metrics,
        )

    def _on_degrade(exc: Exception) -> None:
        logger.warning(
            "publish_quantities: quantity %s frame emit failed (%s); degrading to "
            "peak-only (no animation group) run_id=%s",
            spec.quantity_id,
            exc,
            run_id,
        )

    def _cleanup() -> None:
        # The frame COGs are already uploaded + the local temp unlinked in
        # _build_raster_layer; the manifest layers are in-memory only, so the
        # degrade path simply drops the accumulated frame entries.
        written.clear()

    emitted = frames.emit_timeseries_layers(
        tf.n_steps,
        write_frame=_write_frame,
        on_degrade=_on_degrade,
        cleanup=_cleanup,
    )
    # ``written`` holds the manifest layers that survived the guard; ``emitted``
    # is the parallel EmittedFrame list (length matches when not degraded).
    if emitted:
        layers.extend(written)


def publish_quantities(
    engine: str,
    *,
    run_id: str,
    upload: UploadFn,
    register_manifest_layers: Callable[..., Any],
    reader_ctx: Any = None,
    specs: Iterable[OutputQuantitySpec] | None = None,
    enabled: Callable[[OutputQuantitySpec], bool] | None = None,
    bbox: tuple[float, float, float, float] | None = None,
) -> Any:
    """Build the quantities manifest + register every layer (the full executor).

    ``register_manifest_layers`` is the ONE registrar
    (``register_published_manifest.register_manifest_layers``) - passed in so the
    executor stays unit-testable against a FAKE registrar. Returns whatever the
    registrar returns (a ``ManifestRegisterResult`` in production).

    DEFAULT-OFF: with the empty scaffold registry this builds an empty manifest
    and the registrar registers nothing (byte-identical decks).
    """
    manifest = build_quantities_manifest(
        engine,
        run_id=run_id,
        upload=upload,
        reader_ctx=reader_ctx,
        specs=specs,
        enabled=enabled,
    )
    return register_manifest_layers(manifest, run_id=run_id, bbox=bbox)
