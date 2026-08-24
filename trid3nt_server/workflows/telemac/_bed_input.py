"""Shared in-worker bed-bathymetry input surfacing for the TELEMAC wave modules.

The emit-on-fetch router seam surfaces every AGENT-SIDE router fetch of
renderable data, but a bed sampled INSIDE a solver container never touches
``route()``. The ARTEMIS (agitation) + TOMAWAC (wave_field) workers write the
lake-datum bed they solved on as ``bed_bathymetry.tif`` (a 4326 COG) next to the
result and record ``bed_cog`` in telemac_metrics.json; this rides that object
through ``publish_raster_input_cog`` (NO re-upload) as a role=context input. It is
the wave-module analogue of ``river_dye._surface_bed_bathymetry_input`` (the
reference in-worker bed surface), factored to one place for the two lake modules.
"""

from __future__ import annotations

import logging
from typing import Any

from trid3nt_contracts import new_ulid

logger = logging.getLogger("trid3nt_server.workflows.telemac._bed_input")


async def surface_in_worker_bed_input(
    emitter: Any,
    *,
    run_metrics: dict[str, Any],
    run_id: str,
    name: str,
    layer_id_prefix: str = "input-lake-bed",
) -> bool:
    """BEST-EFFORT: surface an in-worker-sampled bed COG as a role=context input.

    Reads ``bed_cog`` from the worker run envelope and rides that existing s3 object
    through ``publish_raster_input_cog`` (continuous_dem ramp). NEVER raises -- a
    missing/failed bed COG (older image / write failure) surfaces nothing and never
    voids the solve. ``name`` is the caller-built provenance label ("Input: lake
    bed bathymetry (...)").
    """
    if emitter is None:
        return False
    bed_cog = (run_metrics or {}).get("bed_cog")
    if not bed_cog:
        return False  # worker wrote none (idealized bed / older image / write failed)
    try:
        from trid3nt_server.emission.layer_uri_emit import publish_raster_input_cog
        from trid3nt_server.workflows.solver.solver import _get_runs_bucket

        cog_uri = f"s3://{_get_runs_bucket()}/{run_id}/{bed_cog}"
        return await publish_raster_input_cog(
            emitter,
            cog_uri=cog_uri,
            layer_id=f"{layer_id_prefix}-{new_ulid()}",
            name=name,
            style_preset="continuous_dem",
            role="context",
        )
    except Exception as exc:  # noqa: BLE001 - input surfacing is NEVER fatal
        logger.warning(
            "surface_in_worker_bed_input: non-fatal failure (bed input absent; "
            "the solve is unaffected): %s", exc,
        )
        return False
