"""SCHISM results-mesh producer -- write ``outputs.json``, publish via the seam.

The SCHISM temporal legs (tidal_hydro, pahm_surge, coupled_waves, baroclinic)
return typed PEAK raster COG(s) (the map anchor + narration carrier) and, alongside
them, the native result netCDF sibling (out2d ``elevation``/``sigWaveHeight`` or the
3D ``salinity`` column -- every frame, every dataset group) that QGIS/MDAL animates
directly. No per-step rasterization: the out2d/salinity netCDF IS the temporal
artifact (MDAL exposes each dataset group's timesteps to the Temporal Controller --
proven live in ADR 0286 gate #1).

This is the emit-on-solve producer half for those legs (ADR 0286, replicating the
TELEMAC ADR 0283 pattern exactly): the agent-side postprocess is agent-side (no
image law binds this leg), so the composer -- acting as its own worker -- writes
``outputs.json`` (the peak entry/entries + the ``kind="mesh"`` netCDF entry,
``crs_authid`` because the mesh sibling carries no CRS the plugin can read), then the
SEAM (``build_layers_from_outputs(frames_only=True)``) owns publication of the mesh
layer. The composer keeps its OWN typed peak(s) (with the narration scalars on them);
the seam skips the peak entries under ``frames_only`` so the same COGs are never
registered twice. Best-effort by contract: a write/read/emit miss degrades to
peak-only, never sinks the run ("failure retracts nothing").

The seam mesh layer matches name/style/role/crs/uri/bbox field-for-field with a
hand-wired ``publish_input_layer`` mesh emit; only the ``layer_id`` STEM may
diverge (the seam mints ``model-results-mesh-{run_id}``; a hand-wired site could
mint ``schism-mesh-{run_id}`` / ``schism-wave-mesh-{run_id}`` /
``schism-baroclinic-mesh-{run_id}``). The layer_id is an idempotence/dedup key;
web temporal grouping rides the ``name`` token (``detectSequentialGroups``), NOT
the layer_id, so a stem swap must render identically.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Sequence

from trid3nt_contracts.execution import LayerURI

logger = logging.getLogger("trid3nt_server.workflows.schism.results_mesh_seam")

__all__ = ["publish_results_mesh_via_seam", "RESULTS_MESH_QUANTITY"]

#: The generic quantity a native-mesh temporal entry carries (ADR 0283/0286).
#: Resolves to the ``mesh_grid`` style preset in the seam's quantity->style registry
#: -- consistent across every SCHISM leg (the out2d/salinity netCDF carries every
#: dataset group, so the mesh layer is a whole-results animation, not one field).
RESULTS_MESH_QUANTITY: str = "model_results"


def _build_entries(
    *,
    peak_layers: Sequence[LayerURI],
    mesh_uri: str,
    mesh_name: str,
    crs_authid: str | None,
) -> list[dict[str, Any]]:
    """Peak entry/entries (whole-run record, seam-skipped) + the mesh netCDF entry."""
    from trid3nt_contracts.outputs_manifest import build_entry

    entries: list[dict[str, Any]] = []
    for peak in peak_layers:
        bbox = list(peak.bbox) if getattr(peak, "bbox", None) else None
        entries.append(
            build_entry(
                kind="raster",
                # seam-skipped under frames_only -- quantity is only a record here;
                # the composer publishes its own typed peak with real styling.
                quantity=getattr(peak, "style_preset", None) or "schism_result",
                name=peak.name,
                uri=peak.uri,
                units=getattr(peak, "units", None) or None,
                bbox=bbox,
            )
        )
    entries.append(
        build_entry(
            kind="mesh",
            quantity=RESULTS_MESH_QUANTITY,
            name=mesh_name,
            uri=mesh_uri,
            # crs_authid rides the entry ONLY when the mesh is georeferenced; the
            # idealized QuarterAnnulus mesh is planar (crs_authid=None) and the
            # plugin renders it in a local frame.
            crs_authid=(crs_authid or None),
        )
    )
    return entries


def _write_and_read_mesh_layers(
    *,
    run_id: str,
    engine: str,
    peak_layers: Sequence[LayerURI],
    mesh_uri: str,
    mesh_name: str,
    crs_authid: str | None,
) -> list[LayerURI]:
    """Write ``outputs.json`` then read it back into the seam's mesh LayerURIs.

    Runs off the event loop (a small S3 PUT + GET + pure build). The seam's
    ``build_layers_from_outputs(frames_only=True)`` builds ONLY the temporal
    artifacts -- for a SCHISM leg that is the mesh layer (the peak entries are
    skipped, the composer keeps its typed peaks). Returns ``[]`` on any miss.
    """
    import types as _types

    from trid3nt_server.emission.outputs_seam import (
        build_layers_from_outputs,
        read_outputs_manifest,
    )
    from trid3nt_server.workflows.shared.outputs_manifest_io import (
        write_outputs_manifest,
    )

    entries = _build_entries(
        peak_layers=peak_layers,
        mesh_uri=mesh_uri,
        mesh_name=mesh_name,
        crs_authid=crs_authid,
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
    peak_layers: Sequence[LayerURI],
    mesh_uri: str,
    mesh_name: str,
    crs_authid: str | None,
) -> int:
    """Write ``outputs.json`` + emit the results-mesh layer through the seam.

    ``peak_layers`` are the RAW postprocess peak COG(s) (their ``s3://`` uris land in
    the whole-run record, seam-skipped under frames_only). ``mesh_uri`` is the full
    ``s3://`` uri of the result netCDF sibling (out2d ``out2d_1.nc`` / baroclinic
    ``salinity_1.nc``); ``mesh_name`` is the EXACT byte-equivalent web token the
    hand-wired emit used; ``crs_authid`` is ``"EPSG:4326"`` for a georeferenced solve
    or ``None`` for the idealized QuarterAnnulus mesh. Returns the number of mesh
    layers emitted (0 on any degrade). NEVER raises.
    """
    try:
        mesh_layers = await asyncio.to_thread(
            _write_and_read_mesh_layers,
            run_id=run_id,
            engine=engine,
            peak_layers=peak_layers,
            mesh_uri=mesh_uri,
            mesh_name=mesh_name,
            crs_authid=crs_authid,
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
