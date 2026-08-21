"""Engine template ``geoclaw_tsunami_gauge_timeseries`` - GeoClaw tsunami run
with a coastal water-level GAUGE time series (co-seismic subsidence visible).

A distinct question CLASS from ``geoclaw_inundation`` (per the capability-naming
rule): rather than the peak overland inundation MAP, this asks for the WATER-LEVEL
TIME SERIES at a coastal gauge -- the tsunami waveform (leading depression + run-up
peaks) and any co-seismic subsidence (the initial post-quake surface offset). It is
its OWN registered engine TEMPLATE (engine="geoclaw", tier="template"), NOT an enum
extension of ``geoclaw_inundation``.

``geoclaw_tsunami_gauge_timeseries(...)`` rides the EXISTING GeoClaw inundation deck
surface: it configures a tsunami run recording one coastal gauge, runs the SAME
fetch -> deck -> solve -> postprocess chain (``model_geoclaw_inundation`` with
``emit_gauge_series=True``), and returns the peak-inundation ``GeoClawDepthLayerURI``
now ALSO carrying the gauge scalars, plus a gauge surface-elevation time-series chart.

Determinism boundary (Invariant 1): every number the agent narrates
(``gauge_max_surface_elevation_m`` / ``gauge_min_surface_elevation_m`` /
``gauge_max_amplitude_m`` / ``gauge_coseismic_offset_m`` / ``gauge_max_depth_m`` +
the inundation scalars) comes from the typed ``GeoClawDepthLayerURI`` fields the
worker / postprocess computed -- never free-generated.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.geoclaw_contracts import GeoClawDepthLayerURI, GeoClawRunArgs
from trid3nt_contracts.tool_registry import AtomicToolMetadata, GateSpec

from trid3nt_server.data.tool_arg_normalizer import coerce_bbox_value
from trid3nt_server.data import register_tool
from trid3nt_server.workflows.geoclaw._template_card import TemplateCard
from trid3nt_server.workflows.geoclaw.inundation.inundation import (
    GeoClawComposerError,
    model_geoclaw_inundation,
)
from trid3nt_server.workflows.geoclaw.postprocess_geoclaw import (
    PostprocessGeoClawError,
)
from trid3nt_server.workflows.geoclaw.run_geoclaw import GeoClawWorkflowError

logger = logging.getLogger(
    "trid3nt_server.workflows.geoclaw.gauge_timeseries.gauge_timeseries"
)

__all__ = ["geoclaw_tsunami_gauge_timeseries"]


#: Curated door-listing card (the run_geoclaw door prefers this over signature
#: derivation). One-line question + the real required input + a knobs summary.
TEMPLATE_CARD = TemplateCard(
    question=(
        "coastal water-level TIME SERIES from a tsunami at a gauge point - the "
        "waveform (leading depression + run-up peaks) and co-seismic subsidence, "
        "plus the peak overland inundation (GeoClaw shallow-water run-up)"
    ),
    required_inputs=["bbox"],
    knobs=(
        "coastal_gauge_lonlat, source_lonlat, source_magnitude, sim_duration_s, "
        "amr_levels, output_frames"
    ),
)


_METADATA = AtomicToolMetadata(
    name="geoclaw_tsunami_gauge_timeseries",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="geoclaw",
    gate_spec=GateSpec(
        kind="solver",
        estimate_provider="trid3nt_server.gates.cards.solver_confirm:estimate_geoclaw",
        title="GeoClaw inundation",
        rationale="A consequential GeoClaw solve: confirm before the run.",
    ),
    tier="template",
)


@register_tool(
    _METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def geoclaw_tsunami_gauge_timeseries(
    bbox: tuple[float, float, float, float] | list[float] | str | None = None,
    coastal_gauge_lonlat: tuple[float, float] | list[float] | None = None,
    source_lonlat: tuple[float, float] | list[float] | None = None,
    source_magnitude: float = 8.0,
    tsunami_dtopo_uri: str | None = None,
    sim_duration_s: float = 3600.0,
    output_frames: int = 24,
    amr_levels: int = 2,
    manning_n: float | None = None,
    sea_level_m: float = 0.0,
    compute_class: str = "standard",
    # absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders). Also absorbs the
    # server confirm gate's injected ``confirmed=True``.
    **_extra_ignored: Any,
) -> GeoClawDepthLayerURI | dict[str, Any]:
    """Record a coastal water-level time series from a tsunami run (gauge waveform + co-seismic subsidence).

    Fidelity: GeoClaw adaptive-mesh finite-volume tsunami run-up recording one
    coastal gauge; planning-grade waveform, not a calibrated tide-gauge hindcast.
    Data: the topo/bathy DEM is REAL (fetch_topobathy -> fetch_dem). The tsunami
    source is a synthetic Okada seafloor displacement from source_lonlat +
    source_magnitude (or a prescribed tsunami_dtopo_uri). The gauge sits at
    coastal_gauge_lonlat (or a deterministic seaward-edge fallback).
    Off-scope: the peak inundation MAP (use geoclaw_inundation); dam-break / surge
    run-up -> geoclaw_inundation; spectral wave field -> swan_wave_field; pluvial /
    riverine flooding -> sfincs_flood.

    Use this when: the user wants the WATER-LEVEL TIME SERIES / WAVEFORM / MAREOGRAM
    at a coastal point during a tsunami, the wave arrival + amplitude at a gauge, or
    the co-seismic subsidence signal at the coast.

    Params:
        bbox: computational-domain AOI, EPSG:4326.
        coastal_gauge_lonlat: OPTIONAL (lon, lat) of the gauge; unset -> a
            deterministic seaward-edge fallback inside the domain.
        source_lonlat: tsunami source location; unset -> the AOI centroid.
        source_magnitude: synthetic-source Mw (default 8.0).
        tsunami_dtopo_uri: optional prescribed dtopo file (else synthetic Okada).
        sim_duration_s: simulated time, seconds (default 3600).
        output_frames: animation frame count (default 24).
        amr_levels: AMR refinement levels (default 2).
        manning_n: bottom-friction coefficient. Default None -> this template
            is ALWAYS offshore (a tsunami gauge run), so the published Chow
            (1959) open-water standard 0.025 is used (NLCD has no ocean
            coverage; never derived). Supply a value for a calibrated run.
        sea_level_m: still-water datum (default 0.0).
        compute_class: compute class (default "standard").

    Returns:
        On success: ``GeoClawDepthLayerURI`` -- the peak-inundation COG PLUS the
        gauge scalars (``gauge_max_surface_elevation_m``,
        ``gauge_min_surface_elevation_m``, ``gauge_max_amplitude_m``,
        ``gauge_coseismic_offset_m``, ``gauge_max_depth_m``). A gauge
        surface-elevation time-series chart is emitted alongside.
        On failure: ``{"status": "error", "error_code", "error_message"}``.
        Not cached (``cacheable=False``).
    """
    if bbox is None:
        return {
            "status": "error",
            "error_code": "GEOCLAW_PARAMS_INCOMPLETE",
            "error_message": (
                "geoclaw_tsunami_gauge_timeseries requires a bbox "
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

    # Default the tsunami source to the AOI centroid when the user gives none
    # (the same default the inundation tool applies for tsunami/surge).
    if source_lonlat is None:
        src = (
            0.5 * (float(coerced[0]) + float(coerced[2])),
            0.5 * (float(coerced[1]) + float(coerced[3])),
        )
    else:
        src = (float(source_lonlat[0]), float(source_lonlat[1]))

    gauge = None
    if coastal_gauge_lonlat is not None:
        gauge = (float(coastal_gauge_lonlat[0]), float(coastal_gauge_lonlat[1]))

    # --- law 9 (ADR 0296 completion): this template is ALWAYS offshore (a
    # tsunami gauge run -- GEOCLAW_OFFSHORE_SCENARIOS), so there is no
    # land-dominated leg to derive from NLCD. Label-only pass: the same
    # basis="default_demo" consequence="numerical" Chow (1959) provenance entry
    # geoclaw_inundation's tsunami branch carries, so the constant rides loudly
    # instead of silently (it previously carried NO SyntheticInput at all).
    if manning_n is not None:
        effective_manning_n = float(manning_n)
        _manning_entry = SyntheticInput(
            param="manning_n", value=effective_manning_n, units="s/m^(1/3)",
            basis="user", note="caller-supplied bottom-friction Manning's n.",
        )
    else:
        effective_manning_n = 0.025
        _manning_entry = SyntheticInput(
            param="manning_n", value=effective_manning_n, units="s/m^(1/3)",
            basis="default_demo", consequence="numerical",
            note=(
                "offshore seabed friction: NLCD has no deep-ocean coverage; "
                "the published Chow (1959) open-water standard (n=0.025, the "
                "same value manning_mapping.csv assigns NLCD class 11 Open "
                "Water) is used. Supply manning_n for a calibrated value."
            ),
        )

    try:
        run_args = GeoClawRunArgs(
            bbox=tuple(coerced),  # type: ignore[arg-type]
            scenario="tsunami",
            source_lonlat=src,
            source_magnitude=float(source_magnitude),
            tsunami_dtopo_uri=tsunami_dtopo_uri,
            sim_duration_s=float(sim_duration_s),
            output_frames=int(output_frames),
            amr_levels=int(amr_levels),
            manning_n=float(effective_manning_n),
            sea_level_m=float(sea_level_m),
            coastal_gauge_lonlat=gauge,
        )
    except Exception as exc:  # noqa: BLE001 -- pydantic ValidationError / coercion
        return {
            "status": "error",
            "error_code": "GEOCLAW_PARAMS_INVALID",
            "error_message": f"invalid GeoClaw gauge-timeseries arguments: {exc}",
        }

    logger.info(
        "geoclaw_tsunami_gauge_timeseries bbox=%s source=%s Mw=%.1f gauge=%s "
        "duration=%.0fs amr=%d",
        run_args.bbox,
        run_args.source_lonlat,
        run_args.source_magnitude,
        run_args.coastal_gauge_lonlat,
        run_args.sim_duration_s,
        run_args.amr_levels,
    )

    try:
        primary = await model_geoclaw_inundation(
            run_args,
            compute_class=compute_class,
            emit_gauge_series=True,
            synthetic_inputs=[_manning_entry],
        )
        logger.info(
            "geoclaw_tsunami_gauge_timeseries complete layer_id=%s "
            "gauge_max_amp_m=%s coseismic_offset_m=%s max_depth_m=%.4g uri=%s",
            primary.layer_id,
            primary.gauge_max_amplitude_m,
            primary.gauge_coseismic_offset_m,
            primary.max_depth_m,
            primary.uri,
        )
        return primary
    except asyncio.CancelledError:
        raise
    except (
        GeoClawWorkflowError,
        PostprocessGeoClawError,
        GeoClawComposerError,
    ) as exc:
        logger.warning(
            "geoclaw_tsunami_gauge_timeseries failed: %s (%s)",
            getattr(exc, "error_code", "?"),
            exc,
        )
        return {
            "status": "error",
            "error_code": getattr(exc, "error_code", "GEOCLAW_INTERNAL_ERROR"),
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001 -- defensive catch-all
        logger.exception("geoclaw_tsunami_gauge_timeseries unexpected failure")
        return {
            "status": "error",
            "error_code": "GEOCLAW_INTERNAL_ERROR",
            "error_message": str(exc),
        }
