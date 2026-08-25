"""The emit-on-solve SEAM consumer -- ``outputs.json`` -> published ``LayerURI``s.

ADR 0280 item 4. A solver leg writes an append-only ``outputs.json`` manifest
under its run prefix (``trid3nt_contracts.outputs_manifest``, ``schema_version``
1); this module reads it at completion and turns every entry into the same
registered, styled, legend-stashed ``LayerURI`` the register-only
``publish_manifest.json`` path produced -- but keyed on the entry's physical
``quantity`` instead of a worker-baked ``style_preset``.

Routing (Section 5):
  * ``raster`` with NO ``t``          -> ONE standalone layer (role ``primary``).
  * ``raster`` with ``t``, sharing a  -> a TEMPORAL GROUP (frames in ``t`` order,
    ``quantity`` with siblings           role ``context``, the EXACT web grouping
                                         token preserved so ``detectSequentialGroups``
                                         forms the scrubber exactly as today).
  * ``vector``                        -> a vector layer.
  * ``mesh``                          -> a native SELAFIN ``layer_type="mesh"``
                                         layer (role ``context``, ``crs_authid``
                                         from the entry, ``bbox=None``); MDAL
                                         animates every frame from the one file.
  * ``scalar``                        -> parse + validate, log-only in v1.

Byte-equivalence with ``register_manifest_layers`` (the migration bar, Section
7.1): the emitted layer-event stream -- ``name``, ``layer_id`` (modulo run-id),
``style_preset``, the resolved ``&rescale=..&colormap_name=..`` params + the
data-driven legend, ``bbox``, ``role``, ``units``, and the temporal-group
membership -- is IDENTICAL to what the register path renders for the same solved
output. Styling resolves through ``quantity_styles.resolve_style_preset`` (a
registered quantity -> its pinned registry preset, so ``band_stats`` is NOT
consulted for it -- e.g. ``flood_depth`` -> ``continuous_flood_depth`` -> the
pinned ``0,3`` / ``ylgnbu``); an UNREGISTERED quantity degrades to the honest
neutral ramp, which is the ONE place a lazy per-COG stats touch happens (only
when the entry carries no ``band_stats``).

Idempotence (Section 5.2): ``layer_id`` is minted deterministically from
``(quantity, t-ordinal, run_id)`` so a re-poll of an already-published entry
resolves to the SAME id and is a no-op on ``observe_published_layer``.

MISSING / unknown-schema manifest -> ``read_outputs_manifest`` returns ``None``
and the caller runs its existing path unchanged (legacy engines byte-unchanged).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.outputs_manifest import (
    OutputEntry,
    OutputsManifest,
    parse_outputs_manifest,
)

from trid3nt_server.emission.publish import (
    _band1_percentile_rescale,
    _read_raster_bytes,
    _stash_legend_for_uri,
    legend_for_published_layer,
    style_params_from_band_stats,
)
from trid3nt_server.emission.quantity_styles import resolve_style_preset
from trid3nt_server.emission.uri_registry import observe_published_layer

__all__ = [
    "PublishedFrame",
    "SeamPublishResult",
    "build_layers_from_outputs",
    "read_outputs_manifest",
]

logger = logging.getLogger("trid3nt_server.emission.outputs_seam")


@dataclass(frozen=True)
class PublishedFrame:
    """Replay metadata for one published entry (ADR 0280 item 7).

    Carried ALONGSIDE the emitted ``LayerURI`` so the persistence layer can stamp
    the optional ``t`` / ``group_id`` / seam-resolved ``style_preset`` onto the
    case-layer record; a Case reopen rebuilds the temporal group from these
    without re-polling ``outputs.json`` (which may be GC'd). Purely additive --
    the live-emitted ``LayerURI`` itself is byte-identical to the register path.
    """

    layer_id: str
    quantity: str
    t: float | None
    group_id: str | None
    style_preset: str
    uri: str


@dataclass
class SeamPublishResult:
    """The seam's register-only outcome -- a drop-in for ``ManifestRegisterResult``.

    ``layers`` is ordered [standalone/primary layers..., then each temporal
    group's frames in ``t`` order...] so a caller splits by ``role`` exactly as
    it does for the register path. ``frames`` is the parallel replay metadata
    (item 7). ``mesh_count`` / ``scalar_count`` record the log-only kinds.
    """

    layers: list[LayerURI] = field(default_factory=list)
    frames: list[PublishedFrame] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    mesh_count: int = 0
    scalar_count: int = 0
    unknown_quantity_count: int = 0


# --------------------------------------------------------------------------- #
# Manifest read (the byte-identical no-op on a missing/unknown manifest).
# --------------------------------------------------------------------------- #
def read_outputs_manifest(run_result: Any) -> OutputsManifest | None:
    """Read + schema-gate ``outputs.json`` from a completed run's prefix.

    Resolves ``s3://<runs_bucket>/<run_id>/outputs.json`` (the same prefix
    ``RunResult.output_uri`` points at) and parses it through the tolerant,
    schema-gated reader. Returns ``None`` -- the byte-identical no-op the caller
    treats as "run its existing publish path" -- when the object is absent, the
    completion read fails, or the body carries an unknown ``schema_version`` /
    foreign ``kind``. NEVER raises.
    """
    run_id = getattr(run_result, "run_id", None)
    if not run_id:
        return None
    try:
        from trid3nt_server.workflows.solver.solver import (
            _get_runs_bucket,
            _read_object_bytes,
        )
        from trid3nt_contracts.outputs_manifest import OUTPUTS_MANIFEST_BASENAME

        runs_bucket = _get_runs_bucket()
        uri = f"s3://{runs_bucket}/{run_id}/{OUTPUTS_MANIFEST_BASENAME}"
        raw = _read_object_bytes(uri)
    except Exception as exc:  # noqa: BLE001 -- absent/unreadable -> no-op fallback
        logger.info(
            "outputs_seam: no readable outputs.json for run_id=%s (%s: %s) -- "
            "seam is a no-op; caller runs its existing path.",
            run_id,
            type(exc).__name__,
            exc,
        )
        return None
    try:
        manifest = parse_outputs_manifest(raw)
    except ValueError as exc:
        logger.warning(
            "outputs_seam: outputs.json schema-gate rejected run_id=%s (%s) -- "
            "seam is a no-op; caller runs its existing path.",
            run_id,
            exc,
        )
        return None
    logger.info(
        "outputs_seam: parsed outputs.json run_id=%s engine=%s entries=%d",
        run_id,
        manifest.engine,
        len(manifest.entries),
    )
    return manifest


# --------------------------------------------------------------------------- #
# Layer id / grouping.
# --------------------------------------------------------------------------- #
def _quantity_base(quantity: str) -> str:
    """Deterministic layer-id / group stem from a physical quantity.

    ``flood_depth`` -> ``flood-depth`` (underscore -> hyphen). Reproduces the
    register path's ``layer_id_stem`` family so byte-equivalence of ``layer_id``
    (modulo run-id) holds: a non-temporal raster -> ``{base}-peak``, the Nth
    temporal frame -> ``{base}-frame-{N:02d}``.
    """
    return (quantity or "").strip().lower().replace("_", "-")


def _style_and_legend(
    entry: OutputEntry, *, style_preset: str
) -> tuple[str, Any, bool]:
    """Resolve ``(style_params, legend, needed_cog_touch)`` for one raster entry.

    Mirrors ``register_manifest_layers._register_one_layer`` exactly. ``band_stats``
    comes from the entry when the producer precomputed it (docker workers -- the
    register-only-no-COG-read fast path); when ABSENT and the quantity is
    UNREGISTERED (neutral ramp needs a real range), the seam does the ONE lazy
    per-COG percentile touch (Section 5.2). A registry preset never needs stats.
    """
    bs = entry.band_stats
    is_categorical = bool(bs.is_categorical) if bs else False
    is_rgba = bool(bs.is_rgba) if bs else False
    p2 = bs.p2 if bs else None
    p98 = bs.p98 if bs else None
    needed_cog_touch = False

    # Lazy stats touch: ONLY when the producer gave us nothing AND the preset is
    # the neutral fallback (an unregistered quantity), so a registered/pinned
    # quantity (the common docker case) never re-reads the COG.
    if (
        bs is None
        and style_preset == "neutral_ramp"
        and not (is_categorical or is_rgba)
    ):
        try:
            raw = _read_raster_bytes(entry.uri)
            rescale = _band1_percentile_rescale(raw)  # "&rescale=lo,hi&colormap_name=viridis" | None
            if rescale:
                # Reuse the generic path: hand the parsed lo/hi back through
                # style_params_from_band_stats as p2/p98 so the resulting string
                # is byte-identical to the register path's generic fallback.
                from trid3nt_server.emission.publish import (
                    _parse_style_params,
                )

                lo, hi, _cmap = _parse_style_params(rescale)
                p2, p98 = lo, hi
                needed_cog_touch = True
        except Exception as exc:  # noqa: BLE001 -- degrade to the safe default
            logger.warning(
                "outputs_seam: lazy stats touch failed for %s (%s: %s) -- "
                "neutral ramp falls back to the safe default.",
                entry.uri,
                type(exc).__name__,
                exc,
            )

    style_params = style_params_from_band_stats(
        style_preset,
        is_categorical=is_categorical,
        is_rgba=is_rgba,
        p2=p2,
        p98=p98,
        layer_uri=entry.uri,
    )
    legend = None
    try:
        if style_params:
            legend = legend_for_published_layer(
                style_preset,
                entry.uri,
                style_params,
                units=entry.units or None,
                raster_bytes=b"",  # register-only contract: NO COG download here
            )
        _stash_legend_for_uri(entry.uri, legend)
    except Exception as exc:  # noqa: BLE001 -- legend never blocks a register
        logger.debug(
            "outputs_seam legend stash skipped (%s: %s)", type(exc).__name__, exc
        )
    return style_params, legend, needed_cog_touch


def _build_raster_layer(
    entry: OutputEntry,
    *,
    run_id: str,
    layer_id: str,
    role: str,
    bbox: tuple[float, float, float, float] | None,
) -> LayerURI:
    """Register + build ONE raster ``LayerURI`` (register-path byte parity)."""
    style_preset, _fallback = resolve_style_preset(entry.quantity)
    # Resolves style params + STASHES the data-driven legend side-band keyed by
    # the raw COG uri (the register-path transport: the pipeline emitter lifts it
    # by ``layer.uri`` in add_loaded_layer). The returned LayerURI therefore
    # carries legend=None -- byte-identical to register_manifest_layers, which
    # never attaches the legend to the LayerURI itself.
    _style_and_legend(entry, style_preset=style_preset)

    observe_published_layer(layer_id, gcs_uri=entry.uri)

    entry_bbox: tuple[float, float, float, float] | None = None
    if entry.bbox and len(entry.bbox) == 4:
        entry_bbox = (
            float(entry.bbox[0]),
            float(entry.bbox[1]),
            float(entry.bbox[2]),
            float(entry.bbox[3]),
        )
    layer = LayerURI(
        layer_id=layer_id,
        name=entry.name,  # EXACT web grouping token -- never rename.
        layer_type="raster",
        uri=entry.uri,
        style_preset=style_preset,
        role=role,  # type: ignore[arg-type]
        units=entry.units or None,
        bbox=entry_bbox or bbox,
    )
    logger.info(
        "outputs_seam: registered layer_id=%s name=%r quantity=%s preset=%s uri=%s",
        layer_id,
        entry.name,
        entry.quantity,
        style_preset,
        entry.uri,
    )
    return layer


# --------------------------------------------------------------------------- #
# The consumer.
# --------------------------------------------------------------------------- #
def build_layers_from_outputs(
    manifest: OutputsManifest,
    *,
    run_id: str,
    bbox: tuple[float, float, float, float] | None = None,
    frames_only: bool = False,
) -> SeamPublishResult:
    """Turn a parsed ``outputs.json`` into registered ``LayerURI``s + replay meta.

    Pure given the active dispatch registry (``observe_published_layer`` is a
    no-op outside a dispatch ContextVar -- exactly why registration stays
    agent-side). Does NO heavy I/O for registered quantities that carry
    ``band_stats`` (the register-only fast path); the only COG touch is the
    unregistered-quantity neutral-ramp fallback.

    ``frames_only`` (the M-class ruling, ADR 0282 OPTION a): when True, the seam
    owns the TEMPORAL FRAMES ONLY -- standalone rasters (the peak/final field) and
    vectors are NOT built or registered. The composer keeps its own typed peak
    layer (with the narration scalars on it) and never consumes the seam's peak
    entry, so the same COG uri is never registered twice. ``outputs.json`` still
    carries the peak entry for completeness (a whole-run record); the seam simply
    skips it. A ``kind="mesh"`` entry IS the temporal artifact (ADR 0283), so it is
    ALWAYS built (under frames_only too). Default False = the S-class behaviour
    (seam owns all publication).
    """
    result = SeamPublishResult()

    # Split raster entries into non-temporal (standalone) and temporal (grouped
    # by quantity). Non-raster kinds route per Section 5. Under ``frames_only``
    # the standalone/vector buckets stay empty (the peak stays composer-built).
    standalone: list[OutputEntry] = []
    temporal_by_quantity: dict[str, list[OutputEntry]] = {}
    vectors: list[OutputEntry] = []
    meshes: list[OutputEntry] = []
    for entry in manifest.entries:
        if entry.kind == "raster":
            if entry.t is None:
                if not frames_only:
                    standalone.append(entry)
            else:
                temporal_by_quantity.setdefault(entry.quantity, []).append(entry)
        elif entry.kind == "vector":
            if not frames_only:
                vectors.append(entry)
        elif entry.kind == "mesh":
            # The mesh sibling IS the TEMPORAL artifact (MDAL animates every frame
            # from the one SELAFIN), so it is built even under frames_only -- only
            # the standalone peak raster + vectors are skipped there.
            result.mesh_count += 1
            meshes.append(entry)
        elif entry.kind == "scalar":
            result.scalar_count += 1
            logger.info(
                "outputs_seam: scalar entry (log-only v1) quantity=%s name=%r",
                entry.quantity,
                entry.name,
            )

    # --- Standalone rasters (peak/final field): role primary. ---
    for entry in standalone:
        preset, is_fallback = resolve_style_preset(entry.quantity)
        if is_fallback:
            result.unknown_quantity_count += 1
        layer_id = f"{_quantity_base(entry.quantity)}-peak-{run_id}"
        layer = _build_raster_layer(
            entry, run_id=run_id, layer_id=layer_id, role="primary", bbox=bbox
        )
        result.layers.append(layer)
        result.frames.append(
            PublishedFrame(
                layer_id=layer_id,
                quantity=entry.quantity,
                t=None,
                group_id=None,
                style_preset=preset,
                uri=entry.uri,
            )
        )

    # --- Temporal groups (frames): role context, ordered by t (immutable-once-
    # written ordering + the supersede fallback take the LAST entry per (q,t)). ---
    for quantity, entries in temporal_by_quantity.items():
        # Dedup on t keeping the last (Section 2 supersede), then sort ascending.
        by_t: dict[float, OutputEntry] = {}
        for e in entries:
            by_t[float(e.t)] = e  # last writer wins for a repeated (quantity, t)
        ordered = [by_t[t] for t in sorted(by_t)]
        base = _quantity_base(quantity)
        group_id = f"{base}-{run_id}"
        for ordinal, entry in enumerate(ordered, start=1):
            preset, is_fallback = resolve_style_preset(entry.quantity)
            if is_fallback:
                result.unknown_quantity_count += 1
            layer_id = f"{base}-frame-{ordinal:02d}-{run_id}"
            layer = _build_raster_layer(
                entry, run_id=run_id, layer_id=layer_id, role="context", bbox=bbox
            )
            result.layers.append(layer)
            result.frames.append(
                PublishedFrame(
                    layer_id=layer_id,
                    quantity=quantity,
                    t=float(entry.t) if entry.t is not None else None,
                    group_id=group_id,
                    style_preset=preset,
                    uri=entry.uri,
                )
            )

    # --- Vector layers. ---
    for entry in vectors:
        preset, is_fallback = resolve_style_preset(entry.quantity)
        if is_fallback:
            result.unknown_quantity_count += 1
        layer_id = f"{_quantity_base(entry.quantity)}-{run_id}"
        observe_published_layer(layer_id, gcs_uri=entry.uri)
        entry_bbox: tuple[float, float, float, float] | None = None
        if entry.bbox and len(entry.bbox) == 4:
            entry_bbox = (
                float(entry.bbox[0]),
                float(entry.bbox[1]),
                float(entry.bbox[2]),
                float(entry.bbox[3]),
            )
        result.layers.append(
            LayerURI(
                layer_id=layer_id,
                name=entry.name,
                layer_type="vector",
                uri=entry.uri,
                style_preset=preset,
                role="primary",
                units=entry.units or None,
                bbox=entry_bbox or bbox,
            )
        )
        result.frames.append(
            PublishedFrame(
                layer_id=layer_id,
                quantity=entry.quantity,
                t=None,
                group_id=None,
                style_preset=preset,
                uri=entry.uri,
            )
        )

    # --- Native mesh siblings (SELAFIN, ADR 0283): role context. ---
    # The mesh sibling is a native MDAL temporal artifact (QGIS animates its
    # dataset groups directly -- no per-frame COGs). It is NOT routed through the
    # raster styling seam (no COG touch): the plugin's ``_add_mesh`` drives the
    # dataset-group/CRS. ``crs_authid`` rides the entry (a SELAFIN carries no CRS).
    # ``bbox`` stays None (MDAL derives the extent from the mesh) -- never the
    # composer AOI, so it is byte-identical to the bespoke composer emit it
    # supersedes. layer_id is minted off the quantity (``{base}-mesh-{run_id}``)
    # for idempotence, standardized on the physical quantity like the raster stems.
    for entry in meshes:
        preset, is_fallback = resolve_style_preset(entry.quantity)
        if is_fallback:
            result.unknown_quantity_count += 1
        layer_id = f"{_quantity_base(entry.quantity)}-mesh-{run_id}"
        observe_published_layer(layer_id, gcs_uri=entry.uri)
        entry_bbox: tuple[float, float, float, float] | None = None
        if entry.bbox and len(entry.bbox) == 4:
            entry_bbox = (
                float(entry.bbox[0]),
                float(entry.bbox[1]),
                float(entry.bbox[2]),
                float(entry.bbox[3]),
            )
        result.layers.append(
            LayerURI(
                layer_id=layer_id,
                name=entry.name,
                layer_type="mesh",
                uri=entry.uri,
                style_preset=preset,
                role="context",
                units=entry.units or None,
                bbox=entry_bbox,
                crs_authid=entry.crs_authid or None,
            )
        )
        result.frames.append(
            PublishedFrame(
                layer_id=layer_id,
                quantity=entry.quantity,
                t=float(entry.t) if entry.t is not None else None,
                group_id=None,
                style_preset=preset,
                uri=entry.uri,
            )
        )
        logger.info(
            "outputs_seam: registered MESH layer_id=%s name=%r quantity=%s "
            "preset=%s crs=%s uri=%s",
            layer_id,
            entry.name,
            entry.quantity,
            preset,
            entry.crs_authid,
            entry.uri,
        )

    logger.info(
        "outputs_seam: built %d layer(s) run_id=%s (standalone=%d temporal_groups=%d "
        "vectors=%d mesh=%d scalar=%d unknown_quantity=%d)",
        len(result.layers),
        run_id,
        len(standalone),
        len(temporal_by_quantity),
        len(vectors),
        result.mesh_count,
        result.scalar_count,
        result.unknown_quantity_count,
    )
    return result
