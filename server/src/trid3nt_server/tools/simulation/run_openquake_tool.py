"""Atomic tool ``run_seismic_hazard_psha`` — OpenQuake Engine probabilistic
seismic-hazard (PSHA) (sprint-17).

The LLM-facing exposure of the OpenQuake classical-PSHA engine (the multi-hazard
workbench's seismic driver, pairing with the existing Pelicun impact path).
``run_seismic_hazard_psha(...)`` takes the ``OpenQuakeRunArgs`` parameters, runs
the deterministic assemble -> stage -> Batch-solve -> postprocess chain
(``workflows/model_seismic_hazard_scenario.py``), and returns a
``SeismicHazardLayerURI`` the emitter loads onto the map (it subclasses
``LayerURI`` so the ``emit_tool_call`` ``add_loaded_layer`` gate fires).

This is the OpenQuake analogue of ``run_swmm_urban_flood`` (SWMM) /
``run_modflow_job`` (MODFLOW) / ``run_model_flood_scenario`` (SFINCS). Like those
wrappers it declares ``cacheable=False`` + ``ttl_class="live-no-cache"`` +
``source_class="workflow_dispatch"`` (FR-DC-6 — workflow exposure surface; never
touches the cache shim). Confirmation before consequence (Invariant 9 — a solver
run) is enforced by the server confirmation hook around this tool.

OpenQuake is CLOUD-ONLY (the engine is RAM-hungry ~2 GB/thread and ships as a
containerized CLI), so unlike SWMM there is no in-process lane — the composer
always dispatches to AWS Batch via the generic run_solver seam.

Determinism boundary (Invariant 1): every hazard number the agent narrates comes
from the typed ``SeismicHazardLayerURI.max_hazard_value`` / ``.hazard_area_km2`` /
``.return_period_years`` fields the postprocess computed — never free-generated.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from trid3nt_contracts.openquake_contracts import (
    OpenQuakeRunArgs,
    SeismicHazardLayerURI,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tool_arg_normalizer import coerce_bbox_value
from trid3nt_server.workflows.model_seismic_hazard_scenario import (
    OpenQuakeWorkflowError,
    model_seismic_hazard_scenario,
)
from trid3nt_server.workflows.postprocess_openquake import PostprocessOpenQuakeError

logger = logging.getLogger("trid3nt_server.tools.simulation.run_openquake_tool")

__all__ = ["run_seismic_hazard_psha", "RunOpenQuakeError"]


class RunOpenQuakeError(RuntimeError):
    """Raised when the OpenQuake chain fails fatally before producing a layer.

    Carries the open-set ``error_code`` propagated from the failing stage so the
    agent emitter renders a typed error frame."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


_RUN_SEISMIC_HAZARD_PSHA_METADATA = AtomicToolMetadata(
    name="run_seismic_hazard_psha",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)


@register_tool(
    _RUN_SEISMIC_HAZARD_PSHA_METADATA,
    # readOnlyHint=False (runs a solver writing output COG artifacts),
    # openWorldHint=False (containerized OpenQuake CLI + intra-cloud object store),
    # destructiveHint=False (writes go to a new runs/ prefix),
    # idempotentHint=False (each call mints a new run_id + COG keys).
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def run_seismic_hazard_psha(
    bbox: tuple[float, float, float, float] | list[float] | str | None = None,
    imt: str = "PGA",
    poe: float = 0.10,
    investigation_time_years: float = 50.0,
    site_grid_spacing_km: float = 5.0,
    max_distance_km: float = 300.0,
    gmpe: str = "BooreAtkinson2008",
    a_value: float = 4.0,
    b_value: float = 1.0,
    min_magnitude: float = 5.0,
    max_magnitude: float = 7.5,
    compute_class: str = "standard",
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> SeismicHazardLayerURI | dict[str, Any]:
    """Run a probabilistic seismic-hazard (PSHA) calculation over an AOI.

    Use this when: the user asks for seismic/earthquake HAZARD, a
    probabilistic seismic-hazard map, PGA/spectral-acceleration map, or
    "10% in 50 years" (475-yr) ground motion -- also the canonical
    ground-motion INPUT to a Pelicun earthquake damage assessment. Builds
    a classical-PSHA OpenQuake deck over a site grid; when a REAL active
    fault (GEM Global Active Faults) intersects the AOI it builds a
    physics-based fault source (hazard peaks on the trace), else falls
    back to a synthetic Gutenberg-Richter area source -- the returned
    ``source_model_kind`` ("real-fault"/"synthetic-area") must be narrated
    HONESTLY, never claim real faults on a synthetic fallback. Do NOT use
    for: surface-water/riverine/coastal flooding
    (``run_model_flood_scenario``), urban/pluvial (``run_swmm_urban_flood``),
    groundwater (``run_modflow_job``); estimating building damage itself
    (this produces the Pelicun hazard INPUT, not the damage tool).

    Params:
        bbox: AOI, EPSG:4326; a regular site grid is laid over it.
        imt: ``"PGA"`` (default, g), ``"PGV"`` (cm/s), or ``"SA(<period>)"``.
        poe: probability of exceedance (0,1), default 0.10.
        investigation_time_years: PoE window, default 50.
        site_grid_spacing_km: default 5 (coarsened for wide AOIs --
            OpenQuake is RAM-hungry).
        max_distance_km: source-to-site integration distance, default 300.
        gmpe: ground-motion prediction equation, default
            "BooreAtkinson2008".
        a_value/b_value: demo Gutenberg-Richter recurrence, default 4.0/1.0.
        min_magnitude/max_magnitude: demo source range, default 5.0/7.5.
        compute_class: default "standard".

    Returns:
        On success: ``SeismicHazardLayerURI`` with ``max_hazard_value``,
        ``hazard_area_km2``, ``return_period_years``, ``n_sites``,
        ``source_model_kind``, ``source_model_note``.
        On failure: ``{"status": "error", "error_code", "error_message"}``.
        Not cached (``cacheable=False``).
    """
    if bbox is None:
        return {
            "status": "error",
            "error_code": "OQ_PARAMS_INCOMPLETE",
            "error_message": (
                "run_seismic_hazard_psha requires a bbox "
                "(min_lon, min_lat, max_lon, max_lat) in EPSG:4326."
            ),
        }
    coerced = coerce_bbox_value(bbox)
    if coerced is None:
        return {
            "status": "error",
            "error_code": "OQ_PARAMS_INVALID",
            "error_message": (
                f"invalid bbox (expected 4 numbers min_lon,min_lat,max_lon,max_lat): "
                f"{bbox!r}"
            ),
        }
    try:
        run_args = OpenQuakeRunArgs(
            bbox=tuple(coerced),  # type: ignore[arg-type]
            imt=str(imt),
            poe=float(poe),
            investigation_time_years=float(investigation_time_years),
            site_grid_spacing_km=float(site_grid_spacing_km),
            max_distance_km=float(max_distance_km),
            gmpe=str(gmpe),
            a_value=float(a_value),
            b_value=float(b_value),
            min_magnitude=float(min_magnitude),
            max_magnitude=float(max_magnitude),
        )
    except Exception as exc:  # noqa: BLE001 — pydantic ValidationError or coercion
        return {
            "status": "error",
            "error_code": "OQ_PARAMS_INVALID",
            "error_message": f"invalid OpenQuake run arguments: {exc}",
        }

    logger.info(
        "run_seismic_hazard_psha bbox=%s imt=%s poe=%.4g inv_time=%.0fyr "
        "grid=%.1fkm gmpe=%s",
        run_args.bbox,
        run_args.imt,
        run_args.poe,
        run_args.investigation_time_years,
        run_args.site_grid_spacing_km,
        run_args.gmpe,
    )

    try:
        layer = await model_seismic_hazard_scenario(
            run_args,
            compute_class=compute_class,
        )
        logger.info(
            "run_seismic_hazard_psha complete layer_id=%s max_hazard=%.4g "
            "hazard_area_km2=%.6g return_period=%.0fyr uri=%s",
            layer.layer_id,
            layer.max_hazard_value,
            layer.hazard_area_km2,
            layer.return_period_years,
            layer.uri,
        )
        return layer
    except asyncio.CancelledError:
        raise
    except (OpenQuakeWorkflowError, PostprocessOpenQuakeError) as exc:
        logger.warning("run_seismic_hazard_psha failed: %s (%s)", exc.error_code, exc)
        return {
            "status": "error",
            "error_code": exc.error_code,
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001 — defensive catch-all
        logger.exception("run_seismic_hazard_psha unexpected failure")
        return {
            "status": "error",
            "error_code": "OQ_INTERNAL_ERROR",
            "error_message": str(exc),
        }
