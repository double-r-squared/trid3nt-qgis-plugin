"""Engine template ``geoclaw_inundation`` - GeoClaw (Clawpack) adaptive-mesh
shallow-water inundation engine (engine-door refactor - GEOCLAW slice; was
``run_geoclaw_inundation``).

The LLM-facing exposure of the GeoClaw shallow-water engine (tsunami run-up /
dam-break / surge run-up - a hazard family SFINCS/SWMM do not cover).
``geoclaw_inundation(...)`` takes the ``GeoClawRunArgs`` scenario/forcing
fields, runs the deterministic fetch -> stage -> solve -> postprocess chain
(``workflows/geoclaw/model_dambreak_geoclaw_scenario/``), and returns a
``GeoClawDepthLayerURI`` the emitter loads onto the map (it subclasses
``LayerURI`` so the ``emit_tool_call`` ``add_loaded_layer`` gate fires).

This is the GeoClaw analogue of ``swmm_urban_flood`` (SWMM) /
``modflow_contaminant_plume`` (MODFLOW) / ``sfincs_flood`` (SFINCS). It is a
registered engine TEMPLATE tagged ``engine="geoclaw", tier="template"`` -
EXCLUDED from the default retrieval pool and surfaced only by the ``run_geoclaw``
door's gate expansion (SELECT-THEN-CALL). Like the other templates it declares
``cacheable=False`` + ``ttl_class="live-no-cache"`` +
``source_class="workflow_dispatch"`` (FR-DC-6 - workflow exposure surface; never
touches the cache shim).

GeoClaw is CONTAINER-ONLY (the Clawpack Fortran lives in the worker container
image, never in the agent venv), so it always dispatches to a local Docker
solver container via the generic run_solver seam.

Determinism boundary (Invariant 1): every depth number the agent narrates comes
from the typed ``GeoClawDepthLayerURI.max_depth_m`` / ``.flooded_area_km2`` /
``.max_inundation_m`` fields the postprocess computed - never free-generated.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from trid3nt_contracts.geoclaw_contracts import (
    GeoClawDepthLayerURI,
    GeoClawRunArgs,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.agent.tools import register_tool
from trid3nt_server.agent.tool_arg_normalizer import coerce_bbox_value
from trid3nt_server.agent.workflows.geoclaw._template_card import TemplateCard
from trid3nt_server.agent.workflows.geoclaw.model_dambreak_geoclaw_scenario.model_dambreak_geoclaw_scenario import (
    GeoClawComposerError,
    model_dambreak_geoclaw_scenario,
)
from trid3nt_server.agent.workflows.geoclaw.postprocess_geoclaw import PostprocessGeoClawError
from trid3nt_server.agent.workflows.geoclaw.run_geoclaw import GeoClawWorkflowError

logger = logging.getLogger(
    "trid3nt_server.agent.workflows.geoclaw.inundation.inundation"
)

__all__ = ["geoclaw_inundation", "RunGeoClawError"]


class RunGeoClawError(RuntimeError):
    """Raised when the GeoClaw chain fails fatally before producing a layer."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


#: Curated door-listing card (the run_geoclaw door prefers this over signature
#: derivation). One-line question + the real required input + a knobs summary.
TEMPLATE_CARD = TemplateCard(
    question=(
        "peak inundation depth + a run-up animation for a TSUNAMI / DAM-BREAK / "
        "storm-SURGE run-up (GeoClaw adaptive-mesh finite-volume shallow water)"
    ),
    required_inputs=["bbox"],
    knobs=(
        "scenario (dam_break / tsunami / surge), sim_duration_s, dam_name, "
        "dam_break_depth_m, source_lonlat, source_magnitude, tsunami_dtopo_uri, "
        "surge_forcing_uri, output_frames, amr_levels, manning_n, sea_level_m, "
        "fault_strike_deg/dip/rake/depth_km, extra_topo_uris, "
        "coastal_gauge_lonlat, fgmax_arrival_tol_m"
    ),
)


_GEOCLAW_INUNDATION_METADATA = AtomicToolMetadata(
    name="geoclaw_inundation",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="geoclaw",
    tier="template",
)


@register_tool(
    _GEOCLAW_INUNDATION_METADATA,
    # readOnlyHint=False (runs a solver writing output COG artifacts),
    # openWorldHint=False (Batch worker + intra-cloud object store),
    # destructiveHint=False (writes go to a new runs/ prefix),
    # idempotentHint=False (each call mints a new run_id + COG keys).
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def geoclaw_inundation(
    bbox: tuple[float, float, float, float] | list[float] | str | None = None,
    scenario: str = "dam_break",
    sim_duration_s: float = 3600.0,
    dam_name: str | None = None,
    dam_break_depth_m: float | None = None,
    source_lonlat: tuple[float, float] | list[float] | None = None,
    source_magnitude: float = 8.0,
    tsunami_dtopo_uri: str | None = None,
    surge_forcing_uri: str | None = None,
    output_frames: int = 24,
    amr_levels: int = 2,
    manning_n: float = 0.025,
    sea_level_m: float = 0.0,
    fault_strike_deg: float | None = None,
    fault_dip_deg: float | None = None,
    fault_rake_deg: float | None = None,
    fault_depth_km: float | None = None,
    extra_topo_uris: list[str] | None = None,
    coastal_gauge_lonlat: tuple[float, float] | list[float] | None = None,
    fgmax_arrival_tol_m: float | None = None,
    compute_class: str = "standard",
    # absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> GeoClawDepthLayerURI | dict[str, Any]:
    """Run a GeoClaw (Clawpack) shallow-water inundation simulation over an AOI (TSUNAMI/DAM-BREAK/SURGE run-up).

    Fidelity: GeoClaw adaptive-mesh finite-volume run-up (tsunami / dam-break /
    surge); planning-grade run-up envelope, not a calibrated regulatory model.
    Data: for a DAM_BREAK the dam location + released-column height are resolved
    from the real USACE National Inventory of Dams (NID, ``fetch_usace_dams``) -
    by ``dam_name`` when given, else the NID dam nearest the AOI. When no NID dam
    covers the AOI (or a named dam is not found) the run STOPS with a typed
    ``GEOCLAW_DAM_INPUT_REQUIRED`` gate naming ``source_lonlat`` +
    ``dam_break_depth_m`` (never an invented centroid/height). Explicit
    ``source_lonlat`` + ``dam_break_depth_m`` bypass the NID lookup.
    Off-scope: pluvial / riverine / coastal compound flooding -> sfincs_flood;
    urban storm-sewer -> swmm_urban_flood; spectral wave field -> swan_wave_field.

    Use this when: the user wants a TSUNAMI, DAM BREAK/levee failure, or
    shallow-water storm-SURGE RUN-UP inundation depth + animation -- solves 2D
    nonlinear shallow-water equations with adaptive mesh refinement. Do NOT use
    for: rain-driven riverine/coastal compound flooding (``sfincs_flood``);
    urban/pluvial flooding (``swmm_urban_flood``); groundwater plumes
    (``modflow_contaminant_plume``).

    Params:
        bbox: computational-domain AOI, EPSG:4326.
        scenario: ``"dam_break"`` (default, raised water column at t=0),
            ``"tsunami"`` (seafloor-displacement source), or ``"surge"``
            (raised sea surface).
        sim_duration_s: simulated time, seconds (default 3600).
        dam_name: dam_break only, OPTIONAL name of the NID dam to model;
            when given the NID lookup filters to dams whose name contains
            it (nearest match wins). Unset -> the NID dam nearest the AOI.
        dam_break_depth_m: dam_break only, released column height (m).
            Unset -> the real NID ``DAM_HEIGHT`` of the resolved dam
            (feet -> m). An explicit value overrides the NID height.
        source_lonlat: driver-source location. dam_break: unset -> the
            resolved NID dam's coordinates (never the AOI centroid); an
            explicit value overrides. tsunami/surge: unset -> AOI centroid.
        source_magnitude: tsunami synthetic-source Mw (default 8.0).
        tsunami_dtopo_uri: optional prescribed dtopo file (else synthetic
            Okada source).
        surge_forcing_uri: optional sea-surface hydrograph CSV.
        output_frames: animation frame count (default 24).
        amr_levels: AMR refinement levels (default 2).
        manning_n: friction coefficient (default 0.025).
        sea_level_m: still-water datum (default 0.0).
        fault_strike_deg/fault_dip_deg/fault_rake_deg/fault_depth_km:
            optional user-gated Okada fault params (tsunami synthetic
            mode); unset substitutes a noted scenario default.
        extra_topo_uris: optional ordered coarse->fine DEM overlays.
        coastal_gauge_lonlat: optional point to record a water-surface
            time series.
        fgmax_arrival_tol_m: optional wet-cell threshold for arrival time
            (default 0.01m when unset).
        compute_class: compute class (default "standard").

    Returns:
        On success: ``GeoClawDepthLayerURI`` -- peak-depth COG plus
        out-of-band per-timestep scrubber animation, with ``max_depth_m``,
        ``flooded_area_km2``, ``max_inundation_m``.
        On failure: ``{"status": "error", "error_code", "error_message"}``.
        Not cached (``cacheable=False``).

    FR-DC-6: ``cacheable=False``, ``ttl_class="live-no-cache"``,
    ``source_class="workflow_dispatch"`` -- cache shim not invoked.
    """
    # --- Validate + coerce into the GeoClawRunArgs contract -----------------
    if bbox is None:
        return {
            "status": "error",
            "error_code": "GEOCLAW_PARAMS_INCOMPLETE",
            "error_message": (
                "geoclaw_inundation requires a bbox "
                "(min_lon, min_lat, max_lon, max_lat) in EPSG:4326."
            ),
        }
    coerced = coerce_bbox_value(bbox)
    if coerced is None:
        return {
            "status": "error",
            "error_code": "GEOCLAW_PARAMS_INVALID",
            "error_message": (
                f"invalid bbox (expected 4 numbers min_lon,min_lat,max_lon,max_lat): "
                f"{bbox!r}"
            ),
        }

    # --- Dam-break source provenance: real NID dam, or a typed input gate -------
    # For a dam_break the location + released-column height are physically
    # dominant; resolve them from the USACE National Inventory of Dams instead of
    # inventing an AOI-centroid + a baked 10 m column. A user who supplies BOTH
    # source_lonlat AND dam_break_depth_m bypasses the lookup (they chose). When
    # the NID has no dam for the AOI and the user did not supply both, STOP with a
    # typed gate naming the manual params - never a silent invented dam.
    effective_source_lonlat = source_lonlat
    effective_dam_depth = dam_break_depth_m
    dam_source_note: str | None = None
    if str(scenario).strip().lower() in ("dam_break", "dambreak", "dam-break"):
        _has_loc = source_lonlat is not None
        _has_height = dam_break_depth_m is not None
        if not (_has_loc and _has_height):
            from trid3nt_server.agent.workflows.geoclaw.nid_dams import resolve_nid_dam

            dam = await asyncio.to_thread(
                resolve_nid_dam, tuple(coerced), dam_name=dam_name
            )
            if dam is not None:
                if not _has_loc:
                    effective_source_lonlat = (dam.lon, dam.lat)
                if not _has_height:
                    effective_dam_depth = dam.height_m
                dam_source_note = dam.note()
                if _has_loc or _has_height:
                    dam_source_note += (
                        " (user-supplied "
                        + " + ".join(
                            n for n, ok in (("location", _has_loc), ("height", _has_height)) if ok
                        )
                        + " kept)."
                    )
            else:
                named = f" named {dam_name!r}" if dam_name else ""
                return {
                    "status": "error",
                    "error_code": "GEOCLAW_DAM_INPUT_REQUIRED",
                    "error_message": (
                        f"No USACE National Inventory of Dams (NID) dam{named} was "
                        f"found for this AOI, so the dam location + height are not "
                        f"fabricated. To run this dam-break, supply BOTH "
                        f"source_lonlat=(lon, lat) of the dam AND dam_break_depth_m "
                        f"(released-column height, m) - or pass a dam_name that "
                        f"exists in NID within the AOI."
                    ),
                }
        else:
            dam_source_note = "Dam location + released-column height are user-supplied (not NID-sourced)."
    if effective_dam_depth is None:
        # tsunami / surge ignore dam_break_depth_m; give the contract its default.
        effective_dam_depth = 10.0

    try:
        kwargs: dict[str, Any] = dict(
            bbox=tuple(coerced),  # type: ignore[arg-type]
            scenario=scenario,
            sim_duration_s=float(sim_duration_s),
            dam_break_depth_m=float(effective_dam_depth),
            source_magnitude=float(source_magnitude),
            output_frames=int(output_frames),
            amr_levels=int(amr_levels),
            manning_n=float(manning_n),
            sea_level_m=float(sea_level_m),
        )
        if effective_source_lonlat is not None:
            sl = list(effective_source_lonlat)
            if len(sl) == 2:
                kwargs["source_lonlat"] = (float(sl[0]), float(sl[1]))
        if tsunami_dtopo_uri:
            kwargs["tsunami_dtopo_uri"] = str(tsunami_dtopo_uri)
        if surge_forcing_uri:
            kwargs["surge_forcing_uri"] = str(surge_forcing_uri)
        # USER-GATED Okada fault overrides: thread ONLY the ones supplied so the
        # contract default (None) holds otherwise and the engine substitutes a
        # scenario default it surfaces (never silently fabricated).
        if fault_strike_deg is not None:
            kwargs["fault_strike_deg"] = float(fault_strike_deg)
        if fault_dip_deg is not None:
            kwargs["fault_dip_deg"] = float(fault_dip_deg)
        if fault_rake_deg is not None:
            kwargs["fault_rake_deg"] = float(fault_rake_deg)
        if fault_depth_km is not None:
            kwargs["fault_depth_km"] = float(fault_depth_km)
        if extra_topo_uris:
            kwargs["extra_topo_uris"] = [str(u) for u in extra_topo_uris if u]
        if coastal_gauge_lonlat is not None:
            cg = list(coastal_gauge_lonlat)
            if len(cg) == 2:
                kwargs["coastal_gauge_lonlat"] = (float(cg[0]), float(cg[1]))
        if fgmax_arrival_tol_m is not None:
            kwargs["fgmax_arrival_tol_m"] = float(fgmax_arrival_tol_m)
        run_args = GeoClawRunArgs(**kwargs)
    except Exception as exc:  # noqa: BLE001 -- pydantic ValidationError or coercion
        return {
            "status": "error",
            "error_code": "GEOCLAW_PARAMS_INVALID",
            "error_message": f"invalid GeoClaw run arguments: {exc}",
        }

    logger.info(
        "geoclaw_inundation bbox=%s scenario=%s duration=%.0fs frames=%d "
        "amr_levels=%d",
        run_args.bbox,
        run_args.scenario,
        run_args.sim_duration_s,
        run_args.output_frames,
        run_args.amr_levels,
    )

    try:
        peak = await model_dambreak_geoclaw_scenario(
            run_args,
            compute_class=compute_class,
            dam_source_note=dam_source_note,
        )
        logger.info(
            "geoclaw_inundation complete layer_id=%s scenario=%s "
            "max_depth_m=%.4g flooded_area_km2=%.6g max_inundation_m=%.4g uri=%s",
            peak.layer_id,
            peak.scenario,
            peak.max_depth_m,
            peak.flooded_area_km2,
            peak.max_inundation_m,
            peak.uri,
        )
        return peak
    except asyncio.CancelledError:
        raise
    except (
        GeoClawWorkflowError,
        PostprocessGeoClawError,
        GeoClawComposerError,
    ) as exc:
        logger.warning(
            "geoclaw_inundation failed: %s (%s)", exc.error_code, exc
        )
        return {
            "status": "error",
            "error_code": exc.error_code,
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001 -- defensive catch-all
        logger.exception("geoclaw_inundation unexpected failure")
        return {
            "status": "error",
            "error_code": "GEOCLAW_INTERNAL_ERROR",
            "error_message": str(exc),
        }
