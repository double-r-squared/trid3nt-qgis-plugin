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

from . import register_tool
from ..tool_arg_normalizer import coerce_bbox_value
from ..workflows.model_seismic_hazard_scenario import (
    OpenQuakeWorkflowError,
    model_seismic_hazard_scenario,
)
from ..workflows.postprocess_openquake import PostprocessOpenQuakeError

logger = logging.getLogger("trid3nt_server.tools.run_openquake_tool")

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

    Builds a classical-PSHA OpenQuake deck (a ``job.ini`` + a seismic source model
    + a single-GMPE logic tree) over a regular site grid covering the AOI, runs the
    OpenQuake engine headless on AWS Batch, rasterizes the per-site hazard value at
    the requested probability of exceedance onto a COG, and returns a
    ``SeismicHazardLayerURI`` carrying the hazard map + the narration scalars. The
    resulting ground-motion hazard is the canonical input to the Pelicun building
    damage/impact path.

    REAL faults vs synthetic source (task #199): the composer automatically fetches
    the REAL active-fault traces (GEM Global Active Faults) that intersect the AOI
    and, when present, builds a physics-based ``simpleFaultSource`` model so the
    hazard PEAKS ON the actual fault traces (and refines the site grid to resolve
    that gradient). When NO mapped active fault intersects the AOI, it falls back
    to a synthetic Gutenberg-Richter area source over the AOI. The returned layer
    reports which path was used in ``source_model_kind`` (``"real-fault"`` /
    ``"synthetic-area"``) + ``source_model_note`` — narrate those HONESTLY and
    NEVER claim real faults when the run fell back to the synthetic source.

    Use this when:
        - The user asks for seismic / earthquake HAZARD, a probabilistic
          seismic-hazard map, a ground-motion / PGA / spectral-acceleration map,
          or the ground motion with some chance of exceedance over a window
          (e.g. "10% in 50 years", the 475-year return-period hazard).
        - Setting up the ground-motion input for an earthquake building-damage
          (Pelicun) assessment.

    Do NOT use this for:
        - Surface-water / riverine / coastal flooding (``run_model_flood_scenario``
          = SFINCS), urban/pluvial flooding (``run_swmm_urban_flood``), or
          groundwater contamination (``run_modflow_job``).
        - Estimating building damage/losses (that is the Pelicun impact tool —
          this produces the hazard INPUT it consumes).
        - Cancelling a running hazard calc (use the WS ``cancel`` envelope).

    Params:
        bbox: AOI as ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326
            (lon-first). A regular site grid is laid over it.
        imt: Intensity Measure Type. ``"PGA"`` (DEFAULT, Peak Ground Acceleration
            in g — the Pelicun fragility input), ``"PGV"`` (cm/s), or
            ``"SA(<period>)"`` such as ``"SA(0.3)"`` / ``"SA(1.0)"``.
        poe: probability of exceedance for the hazard map, in (0, 1). Default
            0.10. With ``investigation_time_years=50`` this is the standard "10%
            in 50 years" (475-year return period) engineering hazard map.
        investigation_time_years: the PoE window, years (> 0). Default 50.
        site_grid_spacing_km: requested PSHA site-grid spacing, km (> 0).
            Default 5. Keep the AOI modest — OpenQuake is RAM-hungry, so a fine
            spacing over a wide AOI is coarsened to fit the budget.
        max_distance_km: maximum source-to-site integration distance, km (> 0).
            Default 300.
        gmpe: the ground-motion prediction equation class name (a single-branch
            logic tree for v0.1). Default ``"BooreAtkinson2008"``.
        a_value / b_value: Gutenberg-Richter recurrence (seismicity rate +
            magnitude-frequency slope) of the demo area source. Defaults 4.0 /
            1.0 (demo values, not a site-specific source model).
        min_magnitude / max_magnitude: the magnitude range of the demo source.
            Defaults 5.0 / 7.5.
        compute_class: FR-CE-3 compute class. Default ``"standard"`` (OpenQuake
            should size up for a larger site grid).

    Returns:
        On success: a ``SeismicHazardLayerURI`` (a ``LayerURI`` subtype) — the
        emitter appends it to ``session-state.loaded_layers`` and the map renders
        the hazard COG. It carries ``max_hazard_value`` + ``hazard_area_km2`` +
        ``return_period_years`` + ``n_sites`` + ``source_model_kind`` /
        ``source_model_note`` (Invariant 1 — the agent narrates these typed
        fields, never invents them; in particular the real-vs-synthetic source
        narration MUST come from ``source_model_kind``, not a free claim).

        On failure: a dict with ``status="error"`` + ``error_code`` +
        ``error_message`` so the LLM narrates the failure honestly (no layer).

    FR-DC-6: ``cacheable=False`` + ``ttl_class="live-no-cache"`` +
    ``source_class="workflow_dispatch"`` — the cache shim is NOT invoked.
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
