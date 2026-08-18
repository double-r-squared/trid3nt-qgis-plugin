"""TELEMAC results-mesh producer -- write ``outputs.json``, publish via the seam.

The TELEMAC-2D legs (rain_on_grid, river_dye, coastal_tidal_surge) return a typed
PEAK raster COG (the map anchor + narration carrier) and, alongside it, the native
result SELAFIN sibling (every frame, every variable) that QGIS/MDAL animates
directly -- no per-frame COGs. The SELAFIN is the TEMPORAL artifact.

This is the emit-on-solve producer half for those legs (ADR 0283): the agent-side
postprocess writes ``outputs.json`` (the peak entry + the ``kind="mesh"`` SELAFIN
entry, ``crs_authid=EPSG:{utm}`` because a SELAFIN carries no CRS of its own), then
the SEAM (``build_layers_from_outputs(frames_only=True)``) owns publication of the
mesh layer. The composer keeps its OWN typed peak (with the narration scalars on
it); the seam skips the peak entry under ``frames_only`` so the same COG is never
registered twice -- the M-class fork (ADR 0282), now carrying a mesh instead of
frame COGs. Best-effort by contract: a write/read/emit miss degrades to peak-only,
never sinks the run ("failure retracts nothing").
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from trid3nt_contracts.execution import LayerURI

logger = logging.getLogger("trid3nt_server.workflows.telemac.results_mesh_seam")

__all__ = ["publish_results_mesh_via_seam"]

#: The generic quantity a native-mesh temporal entry carries (ADR 0283). Resolves
#: to the ``mesh_grid`` style preset in the seam's quantity->style registry --
#: consistent across all three TELEMAC legs (the SELAFIN carries every variable, so
#: the mesh layer is a whole-results animation, not one physical field).
RESULTS_MESH_QUANTITY: str = "model_results"


def _mesh_layer_name(reach_name: str) -> str:
    """The EXACT web/scrubber group token for the results-mesh layer.

    Byte-identical to the bespoke ``_publish_full_results_mesh`` name the seam
    supersedes (rain_on_grid), so the migration render stream is unchanged.
    """
    return f"Model results (time series): {reach_name}"


def _build_entries(
    *,
    run_id: str,
    peak_layer: LayerURI,
    peak_quantity: str,
    mesh_uri: str,
    mesh_epsg: int,
    reach_name: str,
) -> list[dict[str, Any]]:
    """Peak entry (whole-run record, seam-skipped) + the mesh SELAFIN entry."""
    from trid3nt_contracts.outputs_manifest import build_entry

    entries: list[dict[str, Any]] = []
    bbox = list(peak_layer.bbox) if getattr(peak_layer, "bbox", None) else None
    entries.append(
        build_entry(
            kind="raster",
            quantity=peak_quantity,
            name=peak_layer.name,
            uri=peak_layer.uri,
            units=getattr(peak_layer, "units", None) or None,
            bbox=bbox,
        )
    )
    entries.append(
        build_entry(
            kind="mesh",
            quantity=RESULTS_MESH_QUANTITY,
            name=_mesh_layer_name(reach_name),
            uri=mesh_uri,
            crs_authid=f"EPSG:{int(mesh_epsg)}",
        )
    )
    return entries


def _write_and_read_mesh_layers(
    *,
    run_id: str,
    engine: str,
    peak_layer: LayerURI,
    peak_quantity: str,
    mesh_basename: str,
    mesh_epsg: int,
    reach_name: str,
) -> list[LayerURI]:
    """Write ``outputs.json`` then read it back into the seam's mesh LayerURIs.

    Runs off the event loop (a small S3 PUT + GET + pure build). The seam's
    ``build_layers_from_outputs(frames_only=True)`` builds ONLY the temporal
    artifacts -- for a TELEMAC leg that is the mesh layer (the peak entry is
    skipped, the composer keeps its typed peak). Returns ``[]`` on any miss.
    """
    from trid3nt_server.data.simulation.solver.solver import _get_runs_bucket
    from trid3nt_server.emission.outputs_seam import (
        build_layers_from_outputs,
        read_outputs_manifest,
    )
    from trid3nt_server.workflows.shared.outputs_manifest_io import (
        write_outputs_manifest,
    )
    import types as _types

    runs_bucket = _get_runs_bucket()
    mesh_uri = f"s3://{runs_bucket}/{run_id}/{mesh_basename}"
    entries = _build_entries(
        run_id=run_id,
        peak_layer=peak_layer,
        peak_quantity=peak_quantity,
        mesh_uri=mesh_uri,
        mesh_epsg=mesh_epsg,
        reach_name=reach_name,
    )
    write_outputs_manifest(run_id=run_id, engine=engine, entries=entries)

    manifest = read_outputs_manifest(_types.SimpleNamespace(run_id=run_id))
    if manifest is None:
        logger.info(
            "results_mesh_seam: no readable outputs.json for run_id=%s -- peak-only "
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
    peak_layer: LayerURI,
    peak_quantity: str,
    mesh_basename: str,
    mesh_epsg: int,
    reach_name: str,
) -> int:
    """Write ``outputs.json`` + emit the results-mesh layer through the seam.

    ``peak_layer`` is the RAW postprocess peak (its ``s3://`` COG uri lands in the
    whole-run record). ``mesh_basename`` is the result SELAFIN basename under the
    run prefix (``r2d_rog.slf`` / ``r2d_river.slf`` / ``res_coastal.slf``);
    ``mesh_epsg`` is the reach UTM zone the SELAFIN is stamped with. Returns the
    number of mesh layers emitted (0 on any degrade). NEVER raises.
    """
    try:
        mesh_layers = await asyncio.to_thread(
            _write_and_read_mesh_layers,
            run_id=run_id,
            engine=engine,
            peak_layer=peak_layer,
            peak_quantity=peak_quantity,
            mesh_basename=mesh_basename,
            mesh_epsg=mesh_epsg,
            reach_name=reach_name,
        )
    except Exception as exc:  # noqa: BLE001 -- the results mesh is a bonus
        logger.warning(
            "results_mesh_seam: outputs.json write/read failed for run_id=%s "
            "(%s: %s) -- peak-only degrade.",
            run_id,
            type(exc).__name__,
            exc,
        )
        return 0
    if emitter is None or not mesh_layers:
        return 0
    from trid3nt_server.emission.layer_uri_emit import publish_input_layer

    emitted = 0
    for layer in mesh_layers:
        try:
            await publish_input_layer(emitter, layer, role="context")
            emitted += 1
            logger.info(
                "results_mesh_seam: emitted results mesh layer_id=%s uri=%s",
                layer.layer_id,
                layer.uri,
            )
        except Exception as exc:  # noqa: BLE001 -- a mesh emit never sinks the run
            logger.warning(
                "results_mesh_seam: results mesh emit skipped id=%s (%s: %s)",
                layer.layer_id,
                type(exc).__name__,
                exc,
            )
    return emitted
