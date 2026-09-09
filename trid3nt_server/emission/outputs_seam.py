"""The emit-on-solve seam consumer: ``outputs.json`` -> published ``LayerURI``s.

A solved product's style is DERIVED from its manifest entry - the ``kind`` picks
the preset shape, ``quantity`` and ``units`` parameterise it - so a worker never
bakes a style. A missing or unknown-schema manifest is a no-op, never an error.
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

from trid3nt_server.emission import presets
from trid3nt_server.emission.publish import (
    _read_raster_bytes,
    _stash_legend_for_uri,
    legend_for_published_layer,
)
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
    """Replay metadata for one published entry.

    Carried beside the ``LayerURI``, which carries no ``t`` or ``group_id`` itself.
    """

    layer_id: str
    quantity: str
    t: float | None
    group_id: str | None
    uri: str


@dataclass
class SeamPublishResult:
    """The seam's register-only outcome; ``frames`` runs parallel to ``layers``.

    ``layers`` is ordered standalone first, then each temporal group in ``t`` order.
    """

    layers: list[LayerURI] = field(default_factory=list)
    frames: list[PublishedFrame] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    mesh_count: int = 0
    scalar_count: int = 0


# --------------------------------------------------------------------------- #
# Manifest read (the byte-identical no-op on a missing/unknown manifest).
# --------------------------------------------------------------------------- #
def read_outputs_manifest(run_result: Any) -> OutputsManifest | None:
    """Read + schema-gate ``outputs.json`` from a completed run's prefix.

    ``None`` when absent, unreadable or of an unknown schema; never raises.
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
    """``flood_depth`` -> ``flood-depth``: the stem the register path uses too.

    ``layer_id`` stays byte-equivalent (modulo run-id) between the two paths.
    """
    return (quantity or "").strip().lower().replace("_", "-")


#: manifest kind -> the preset shape that draws it. A solved product declares
#: WHAT it produced; which of the four shapes paints it follows from that.
_KIND_BY_ENTRY: dict[str, str] = {
    "raster": "continuous", "vector": "reference", "mesh": "mesh"}


def quantity_label(quantity: str) -> str:
    """``flood_depth`` -> ``Flood depth``: the quantity, said out loud.

    No lookup table: the producer's own field name is the label's only source.
    """
    words = (quantity or "").strip().replace("-", " ").replace("_", " ").strip()
    return words[:1].upper() + words[1:] if words else "Value"


def entry_style(entry: OutputEntry) -> dict[str, Any]:
    """The style row a solved output derives from the product contract.

    Nothing here is keyed on a preset NAME, so nothing can be mislabelled by one.
    """
    row: dict[str, Any] = {
        "kind": _KIND_BY_ENTRY.get(entry.kind, "continuous"),
        "label": quantity_label(entry.quantity),
    }
    if entry.units:
        row["units"] = entry.units
    if entry.kind == "mesh":
        # The dataset group QGIS binds by: the one the entry declares, else the
        # quantity.
        row["dataset_group"] = entry.dataset_group or entry.quantity
        # A mesh has no band to read, so a range it is to be painted on has to
        # be DECLARED. The producer states the published max-over-time range
        # here, which is how the animating canvas ends up on the same scale as
        # the still - one scale per quantity, across both.
        bs = entry.band_stats
        if bs is not None and bs.p2 is not None and bs.p98 is not None:
            row["scale"] = {"policy": "fixed",
                            "range": [float(bs.p2), float(bs.p98)]}
    return row


def _entry_range(entry: OutputEntry) -> tuple[float, float] | None:
    """The RUN range this one raster contributes - its band stats, or one read.

    The read is the exception: producers usually precompute the stats.
    """
    bs = entry.band_stats
    if bs is not None and (bs.is_categorical or bs.is_rgba):
        return None
    if bs is not None and bs.p2 is not None and bs.p98 is not None:
        return (float(bs.p2), float(bs.p98))
    try:
        preset = presets.from_row(entry_style(entry))
        return presets.band_range_reader(_read_raster_bytes(entry.uri))(preset.scale)
    except Exception as exc:  # noqa: BLE001 -- degrade to the declared fallback
        logger.warning(
            "outputs_seam: the run-range read failed for %s (%s: %s) -- the "
            "preset's declared fallback range stands.",
            entry.uri, type(exc).__name__, exc)
        return None


def _run_ranges(manifest: OutputsManifest) -> dict[str, tuple[float, float] | None]:
    """ONE range per quantity, spanning the whole run - peak and every frame."""
    # The scope of a data-driven scale is the RUN, never the frame: resolving each
    # frame against its own values makes the same colour mean a different depth in
    # the next frame. The peak entry is in the span too, so the still and the
    # frames agree.
    contributions: dict[str, list[tuple[float, float] | None]] = {}
    for entry in manifest.entries:
        if entry.kind != "raster":
            continue
        contributions.setdefault(entry.quantity, []).append(_entry_range(entry))
    return {q: presets.shared_range(found) for q, found in contributions.items()}


def _build_raster_layer(
    entry: OutputEntry,
    *,
    run_id: str,
    layer_id: str,
    role: str,
    bbox: tuple[float, float, float, float] | None,
    shared: tuple[float, float] | None = None,
) -> LayerURI:
    """Register + build ONE raster ``LayerURI`` (register-path byte parity)."""
    style = entry_style(entry)
    # STASHES the resolved style keyed by the raw COG uri (the register-path
    # transport: the pipeline emitter lifts it by ``layer.uri`` in
    # add_loaded_layer). The returned LayerURI therefore carries legend=None --
    # byte-identical to register_manifest_layers, which never attaches the
    # legend to the LayerURI itself. ``raster_bytes=b""`` pins the register-only
    # contract: NO COG download here.
    bs = entry.band_stats
    try:
        _stash_legend_for_uri(entry.uri, legend_for_published_layer(
            style, entry.uri, units=entry.units or None, raster_bytes=b"",
            band_stats=(shared if shared is not None
                        else ((bs.p2, bs.p98) if bs else (None, None)))))
    except Exception as exc:  # noqa: BLE001 -- legend never blocks a register
        logger.debug("outputs_seam legend stash skipped (%s: %s)",
                     type(exc).__name__, exc)

    observe_published_layer(layer_id, uri=entry.uri)

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
        name=entry.name,  # EXACT grouping token the plugin matches on -- never rename.
        layer_type="raster",
        uri=entry.uri,
        style=style,
        quantity=entry.quantity,
        role=role,  # type: ignore[arg-type]
        units=entry.units or None,
        bbox=entry_bbox or bbox,
    )
    logger.info("outputs_seam: registered layer_id=%s name=%r quantity=%s uri=%s",
                layer_id, entry.name, entry.quantity, entry.uri)
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

    ``frames_only`` builds temporal entries alone, leaving the peak to the composer.
    """
    result = SeamPublishResult()
    #: ONE range per quantity over the whole run. Computed BEFORE any layer is
    #: built, because the peak and its frames have to be painted on it together.
    run_ranges = _run_ranges(manifest)

    # Split raster entries into non-temporal (standalone) and temporal (grouped
    # by quantity). Under ``frames_only`` the standalone/vector buckets stay
    # empty: the composer keeps its own peak layer, so the same COG uri is
    # never registered twice.
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
    # Every layer_id below is minted from (quantity, t-ordinal, run_id) and from
    # nothing else, so a re-poll of an already-published entry resolves to the
    # same id and lands as a no-op on ``observe_published_layer``.
    for entry in standalone:
        layer_id = f"{_quantity_base(entry.quantity)}-peak-{run_id}"
        layer = _build_raster_layer(
            entry, run_id=run_id, layer_id=layer_id, role="primary", bbox=bbox,
            shared=run_ranges.get(entry.quantity),
        )
        result.layers.append(layer)
        result.frames.append(
            PublishedFrame(
                layer_id=layer_id,
                quantity=entry.quantity,
                t=None,
                group_id=None,
                uri=entry.uri,
            )
        )

    # --- Temporal groups (frames): role context, ordered by t (immutable-once-
    # written ordering + the supersede fallback take the LAST entry per (q,t)). ---
    for quantity, entries in temporal_by_quantity.items():
        # Dedup on t keeping the last entry written (supersede), then sort ascending.
        by_t: dict[float, OutputEntry] = {}
        for e in entries:
            by_t[float(e.t)] = e  # last writer wins for a repeated (quantity, t)
        ordered = [by_t[t] for t in sorted(by_t)]
        base = _quantity_base(quantity)
        group_id = f"{base}-{run_id}"
        for ordinal, entry in enumerate(ordered, start=1):
            layer_id = f"{base}-frame-{ordinal:02d}-{run_id}"
            layer = _build_raster_layer(
                entry, run_id=run_id, layer_id=layer_id, role="context", bbox=bbox,
                shared=run_ranges.get(quantity),
            )
            result.layers.append(layer)
            result.frames.append(
                PublishedFrame(
                    layer_id=layer_id,
                    quantity=quantity,
                    t=float(entry.t) if entry.t is not None else None,
                    group_id=group_id,
                    uri=entry.uri,
                )
            )

    # --- Vector layers. ---
    for entry in vectors:
        layer_id = f"{_quantity_base(entry.quantity)}-{run_id}"
        observe_published_layer(layer_id, uri=entry.uri)
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
                style=entry_style(entry),
                quantity=entry.quantity,
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
                uri=entry.uri,
            )
        )

    # --- Native mesh siblings (SELAFIN): role context. ---
    # The mesh sibling is a native MDAL temporal artifact (QGIS animates its
    # dataset groups directly -- no per-frame COGs). It is NOT routed through the
    # raster styling seam (no COG touch): the plugin's ``_add_mesh`` drives the
    # dataset-group/CRS. ``crs_authid`` rides the entry (a SELAFIN carries no CRS).
    # ``bbox`` stays None -- MDAL derives the extent from the mesh, never the
    # composer AOI. layer_id is minted off the quantity (``{base}-mesh-{run_id}``)
    # for idempotence, matching the raster stems' naming.
    for entry in meshes:
        layer_id = f"{_quantity_base(entry.quantity)}-mesh-{run_id}"
        observe_published_layer(layer_id, uri=entry.uri)
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
                style=entry_style(entry),
                quantity=entry.quantity,
                role="context",
                units=entry.units or None,
                bbox=entry_bbox,
                crs_authid=entry.crs_authid or None,
                reference_time=entry.reference_time or None,
            )
        )
        result.frames.append(
            PublishedFrame(
                layer_id=layer_id,
                quantity=entry.quantity,
                t=float(entry.t) if entry.t is not None else None,
                group_id=None,
                uri=entry.uri,
            )
        )
        logger.info(
            "outputs_seam: registered MESH layer_id=%s name=%r quantity=%s "
            "crs=%s uri=%s",
            layer_id, entry.name, entry.quantity, entry.crs_authid, entry.uri)

    logger.info(
        "outputs_seam: built %d layer(s) run_id=%s (standalone=%d temporal_groups=%d "
        "vectors=%d mesh=%d scalar=%d)",
        len(result.layers),
        run_id,
        len(standalone),
        len(temporal_by_quantity),
        len(vectors),
        result.mesh_count,
        result.scalar_count,
    )
    return result
