"""A field over time becomes an animation: the results mesh, through the seam.

The result file is the TEMPORAL artifact: this writes the ``kind="mesh"`` entry
beside the published layer of the same quantity, and the seam owns publication of
the mesh layer. The layer's own entry is skipped there, so one COG is never
registered twice. Best-effort."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from trid3nt_contracts.execution import LayerURI

logger = logging.getLogger("trid3nt_server.workflows.publishing.animation")

__all__ = ["publish_results_mesh_via_seam"]

#: The quantity a native-mesh temporal entry carries. Resolves to a ``kind="mesh"``
#: style row naming the run's own dataset group: the result file carries every
#: variable, so the mesh layer is a whole-results animation, not one field.
RESULTS_MESH_QUANTITY: str = "model_results"


def _peak_range(peak_layer: LayerURI) -> tuple[float, float] | None:
    """The PUBLISHED max-over-time range, off the peak layer's resolved key.

    One scale per quantity; ``None`` when the peak has no resolved range."""
    legend = getattr(peak_layer, "legend", None)
    lo, hi = getattr(legend, "vmin", None), getattr(legend, "vmax", None)
    return None if lo is None or hi is None else (float(lo), float(hi))


def _mesh_layer_name(reach_name: str) -> str:
    """The scrubber group token for the results-mesh layer."""
    return f"Model results (time series): {reach_name}"


def _build_entries(
    *,
    run_id: str,
    peak_layer: LayerURI | None,
    peak_quantity: str,
    mesh_group: str,
    mesh_uri: str,
    mesh_epsg: int,
    reach_name: str,
    reference_time: str | None,
) -> list[dict[str, Any]]:
    """The layer's entry (the whole-run record, seam-skipped) + the mesh entry.

    No layer of the quantity: the mesh entry alone, scaled by the seam itself."""
    from trid3nt_contracts.outputs_manifest import build_entry

    entries: list[dict[str, Any]] = []
    units = getattr(peak_layer, "units", None) or None
    published = None
    if peak_layer is not None:
        bbox = list(peak_layer.bbox) if getattr(peak_layer, "bbox", None) else None
        entries.append(
            build_entry(
                kind="raster",
                quantity=peak_quantity,
                name=peak_layer.name,
                uri=peak_layer.uri,
                units=units,
                bbox=bbox,
            )
        )
        published = _peak_range(peak_layer)
    entries.append(
        build_entry(
            kind="mesh",
            quantity=RESULTS_MESH_QUANTITY,
            name=_mesh_layer_name(reach_name),
            uri=mesh_uri,
            units=units,
            crs_authid=f"EPSG:{int(mesh_epsg)}",
            reference_time=reference_time,
            dataset_group=mesh_group,
            band_stats=({"p2": published[0], "p98": published[1]}
                        if published is not None else None),
        )
    )
    return entries


def _write_and_read_mesh_layers(
    *,
    run_id: str,
    engine: str,
    peak_layer: LayerURI | None,
    peak_quantity: str,
    mesh_group: str,
    mesh_basename: str,
    mesh_epsg: int,
    reach_name: str,
    reference_time: str | None,
) -> list[LayerURI]:
    """Write ``outputs.json`` then read it back into the seam's mesh LayerURIs.

    Runs off the event loop; ``[]`` on any miss."""
    from trid3nt_server import storage
    from trid3nt_server.render.outputs_seam import (
        build_layers_from_outputs,
        read_outputs_manifest,
    )
    from .manifest import write_outputs_manifest
    import types as _types

    runs_bucket = storage.runs_bucket()
    mesh_uri = f"s3://{runs_bucket}/{run_id}/{mesh_basename}"
    entries = _build_entries(
        run_id=run_id,
        peak_layer=peak_layer,
        peak_quantity=peak_quantity,
        mesh_group=mesh_group,
        mesh_uri=mesh_uri,
        mesh_epsg=mesh_epsg,
        reach_name=reach_name,
        reference_time=reference_time,
    )
    write_outputs_manifest(run_id=run_id, engine=engine, entries=entries)

    manifest = read_outputs_manifest(_types.SimpleNamespace(run_id=run_id))
    if manifest is None:
        logger.info(
            "animation: no readable outputs.json for run_id=%s - peak-only "
            "(no results mesh).",
            run_id,
        )
        return []
    seam = build_layers_from_outputs(manifest, run_id=run_id, frames_only=True)
    return [lyr for lyr in seam.layers if lyr.layer_type == "mesh"]


async def publish_results_mesh_via_seam(
    emitter: Any,
    *,
    run_id: str,
    engine: str,
    peak_layer: LayerURI | None,
    peak_quantity: str,
    mesh_group: str,
    mesh_basename: str,
    mesh_epsg: int,
    reach_name: str,
    reference_time: str | None = None,
) -> int:
    """Write ``outputs.json`` + emit the results-mesh layer through the seam.

    Returns the number of mesh layers emitted, 0 on any degrade. NEVER raises."""
    # ``reference_time`` is the ISO-8601 UTC instant the file's seconds are
    # counted from, without which the scrubber reads 1900. ``mesh_group`` is the
    # variable spelled the way the mesh reader reports it, because a mesh preset
    # paints ONE of the many groups the file carries and the reader binds it by
    # name.
    try:
        mesh_layers = await asyncio.to_thread(
            _write_and_read_mesh_layers,
            run_id=run_id,
            engine=engine,
            peak_layer=peak_layer,
            peak_quantity=peak_quantity,
            mesh_group=mesh_group,
            mesh_basename=mesh_basename,
            mesh_epsg=mesh_epsg,
            reach_name=reach_name,
            reference_time=reference_time,
        )
    except Exception as exc:  # noqa: BLE001 -- the results mesh is a bonus
        logger.warning(
            "animation: outputs.json write/read failed for run_id=%s "
            "(%s: %s) - peak-only degrade.",
            run_id,
            type(exc).__name__,
            exc,
        )
        return 0
    if emitter is None or not mesh_layers:
        return 0
    from trid3nt_server.render.layer_uri_emit import publish_input_layer

    emitted = 0
    for layer in mesh_layers:
        try:
            await publish_input_layer(emitter, layer, role="context")
            emitted += 1
            logger.info(
                "animation: emitted results mesh layer_id=%s uri=%s",
                layer.layer_id,
                layer.uri,
            )
        except Exception as exc:  # noqa: BLE001 -- a mesh emit never sinks the run
            logger.warning(
                "animation: results mesh emit skipped id=%s (%s: %s)",
                layer.layer_id,
                type(exc).__name__,
                exc,
            )
    return emitted
