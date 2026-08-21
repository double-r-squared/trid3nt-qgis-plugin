"""Engine template ``geoclaw_amr_refinement_regions`` - a GeoClaw run whose
adaptive-mesh refinement is driven by EXPLICIT lat/lon/time region windows rather
than default error-based flagging alone.

A distinct question CLASS from ``geoclaw_inundation`` (per the capability-naming
rule): this asks HOW the AMR mesh is controlled - the user supplies a list of
region windows, each forcing a lat/lon box to a minimum/maximum AMR level over a
time interval. GeoClaw's ``regiondata.regions`` combines overlapping regions by
the MAX of the covering windows' min/max levels, so a window can hold a subregion
at a fixed fine level for a chosen interval (e.g. resolve a harbour only while the
wave is arriving) while ``flag2refine`` error estimation still governs elsewhere.

Rides the EXISTING GeoClaw inundation deck surface: it configures the run with the
explicit ``amr_regions`` threaded onto the setrun ``regiondata`` block, runs the
SAME fetch -> deck -> solve -> postprocess chain (``model_geoclaw_inundation``),
and returns the peak-inundation ``GeoClawDepthLayerURI``.

Determinism boundary (Invariant 1): every number the agent narrates
(``max_depth_m`` / ``flooded_area_km2`` / ``max_inundation_m`` / ``arrival_time_s``)
comes from the typed ``GeoClawDepthLayerURI`` fields the worker / postprocess
computed - never free-generated.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.geoclaw_contracts import (
    AmrRegionWindow,
    GeoClawDepthLayerURI,
    GeoClawRunArgs,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.gates.input_review import gate_input_review
from trid3nt_server.data.tool_arg_normalizer import coerce_bbox_value
from trid3nt_server.emission.pipeline_emitter import current_turn_drawn_geometry
from trid3nt_server.data import register_tool
from trid3nt_server.workflows.geoclaw._template_card import TemplateCard
from trid3nt_server.workflows.geoclaw.inundation.inundation import (
    GeoClawComposerError,
    model_geoclaw_inundation,
)
from trid3nt_server.workflows.geoclaw.postprocess_geoclaw import (
    PostprocessGeoClawError,
)
from trid3nt_server.workflows.geoclaw.run_geoclaw import (
    GEOCLAW_OFFSHORE_SCENARIOS,
    GeoClawWorkflowError,
)
from trid3nt_server.workflows.shared.roughness_resolve import resolve_overland_manning

logger = logging.getLogger(
    "trid3nt_server.workflows.geoclaw.amr_regions.amr_regions"
)

__all__ = ["geoclaw_amr_refinement_regions"]


#: Curated door-listing card. One-line question + the real required inputs + a
#: knobs summary.
TEMPLATE_CARD = TemplateCard(
    question=(
        "control GeoClaw AMR refinement with EXPLICIT lat/lon/time region windows "
        "(force a box to a min/max mesh level over an interval) instead of relying "
        "on default error flagging alone"
    ),
    required_inputs=["bbox", "amr_regions"],
    knobs=(
        "amr_regions=[{min_level,max_level,t_start_s,t_end_s,min_lon,max_lon,"
        "min_lat,max_lat}], scenario, amr_levels, source_lonlat, source_magnitude, "
        "sim_duration_s, output_frames"
    ),
)


_METADATA = AtomicToolMetadata(
    name="geoclaw_amr_refinement_regions",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="geoclaw",
    tier="template",
)


@register_tool(
    _METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def geoclaw_amr_refinement_regions(
    bbox: tuple[float, float, float, float] | list[float] | str | None = None,
    amr_regions: list[dict[str, Any]] | None = None,
    scenario: str = "tsunami",
    source_lonlat: tuple[float, float] | list[float] | None = None,
    source_magnitude: float = 8.0,
    dam_break_depth_m: float = 10.0,
    sim_duration_s: float = 3600.0,
    output_frames: int = 24,
    amr_levels: int = 3,
    manning_n: float | None = None,
    sea_level_m: float = 0.0,
    compute_class: str = "standard",
    input_mode: str | None = None,
    window_basis: str = "prompt_interpreted",
    # absorb LLM-invented kwargs + the server confirm gate's injected confirmed=True.
    **_extra_ignored: Any,
) -> GeoClawDepthLayerURI | dict[str, Any]:
    """Run GeoClaw with explicit AMR refinement region windows (region-based flagging).

    Fidelity: GeoClaw adaptive-mesh finite-volume shallow-water run-up whose mesh
    refinement is FORCED by the supplied lat/lon/time regions (a min/max level per
    box+interval), layered on the engine default region tiers; planning-grade.
    Data: the topo/bathy DEM is REAL (fetch_topobathy -> fetch_dem). For a tsunami
    the source is a synthetic Okada displacement from source_lonlat +
    source_magnitude.
    Off-scope: the plain peak-inundation map with default flagging ->
    geoclaw_inundation; the coastal gauge waveform -> geoclaw_tsunami_gauge_timeseries;
    spatially-varying friction -> geoclaw_regional_manning_friction.

    Use this when: the user wants to CONTROL where/when/how finely the AMR mesh
    refines - pin a harbour or a stretch of coast to a fixed level for a time
    window, cap an offshore box coarse to save cost, or contrast explicit regions
    against default error-based refinement.

    Params:
        bbox: computational-domain AOI, EPSG:4326 (min_lon, min_lat, max_lon, max_lat).
        amr_regions: REQUIRED list of region windows; each is a dict with keys
            min_level, max_level, t_start_s, t_end_s, min_lon, max_lon, min_lat,
            max_lat. Each window forces its box to [min_level, max_level] over
            [t_start_s, t_end_s]. Appended AFTER the engine default tiers.
        scenario: driver family ("tsunami"|"dam_break"|"surge"; default "tsunami").
        source_lonlat: source location; unset -> AOI centroid (dam_break) or the
            composer offshore placement (tsunami).
        source_magnitude: synthetic-source Mw for a tsunami (default 8.0).
        dam_break_depth_m: raised-column height for dam_break (default 10.0).
        sim_duration_s: simulated time, seconds (default 3600).
        output_frames: animation frame count (default 24).
        amr_levels: maximum AMR levels available to the regions (default 3).
        manning_n: single global bottom-friction coefficient. Default None ->
            for dam_break / surge (land-dominated / mixed-coastal run-up),
            DERIVED from NLCD land cover over the AOI (area-weighted mean of the
            SFINCS Manning table, the same resolution ``geoclaw_inundation`` /
            ``geoclaw_storm_surge`` use), or REFUSES if NLCD cannot serve; for
            tsunami (offshore -- GEOCLAW_OFFSHORE_SCENARIOS, deep-ocean
            propagation), the published Chow (1959) open-water standard 0.025 is
            used (NLCD has no ocean coverage). Supply a value for a calibrated run.
        sea_level_m: still-water datum (default 0.0).
        compute_class: compute class (default "standard").
        input_mode: review lever ("auto"|"user_gated"; None -> session
            default). In "user_gated" the resolved windows are presented for review
            BEFORE the solve; in "auto" they ride the assumptions block labeled.
        window_basis: provenance class for the windows ("prompt_interpreted" when the
            model derived the box from the prompt, "user" when explicit coordinates /
            a drawn geometry were supplied). A user-drawn region bound to the turn
            (the QGIS dock 'Draw region' rubber-band) OVERRIDES the model
            proposal here -- it replaces ``amr_regions`` with one finest-level window
            over the whole sim and forces "user". The LLM-derived path defaults to
            "prompt_interpreted" so an invented window is visible for review.

    Returns:
        On success: ``GeoClawDepthLayerURI`` - the peak-inundation COG + depth
        scalars + arrival time. On failure: ``{"status": "error", "error_code",
        "error_message"}``. Not cached (``cacheable=False``).
    """
    if bbox is None:
        return {
            "status": "error",
            "error_code": "GEOCLAW_PARAMS_INCOMPLETE",
            "error_message": (
                "geoclaw_amr_refinement_regions requires a bbox "
                "(min_lon, min_lat, max_lon, max_lat) in EPSG:4326."
            ),
        }
    # --- draw-a-geometry supply path -------------------------------- #
    # A user-drawn region (the QGIS dock 'Draw region' rubber-band, bound to this
    # turn as ``drawn_geometry``) OVERRIDES the model's prompt-interpreted window
    # proposal: build ONE refinement window from its bbox at the finest level
    # over the whole sim window and stamp ``window_basis="user"``. The WHERE-to-
    # refine input then rides the gate as an EXPLICIT user choice, not an LLM
    # guess. Absent a drawn geometry the model-supplied ``amr_regions`` +
    # ``window_basis`` flow through unchanged.
    _drawn = current_turn_drawn_geometry()
    _drawn_bbox = _drawn.get("bbox") if isinstance(_drawn, dict) else None
    if isinstance(_drawn_bbox, (list, tuple)) and len(_drawn_bbox) == 4:
        db = [float(v) for v in _drawn_bbox]
        amr_regions = [
            {
                "min_level": int(amr_levels),
                "max_level": int(amr_levels),
                "t_start_s": 0.0,
                "t_end_s": float(sim_duration_s),
                "min_lon": db[0],
                "min_lat": db[1],
                "max_lon": db[2],
                "max_lat": db[3],
            }
        ]
        window_basis = "user"

    if not amr_regions:
        return {
            "status": "error",
            "error_code": "GEOCLAW_PARAMS_INCOMPLETE",
            "error_message": (
                "geoclaw_amr_refinement_regions requires amr_regions: a non-empty "
                "list of {min_level, max_level, t_start_s, t_end_s, min_lon, "
                "max_lon, min_lat, max_lat} windows (or a drawn region from the "
                "QGIS dock)."
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

    src = None
    if source_lonlat is not None:
        src = (float(source_lonlat[0]), float(source_lonlat[1]))

    try:
        windows = [
            w if isinstance(w, AmrRegionWindow) else AmrRegionWindow(**dict(w))
            for w in amr_regions
        ]
    except Exception as exc:  # noqa: BLE001 -- pydantic ValidationError / coercion
        return {
            "status": "error",
            "error_code": "GEOCLAW_PARAMS_INVALID",
            "error_message": f"invalid amr_regions window: {exc}",
        }

    # --- law 9 (ADR 0296 completion): resolve bottom-friction Manning's n, split
    # by domain character -- the IDENTICAL rule geoclaw_inundation applies (ADR
    # 0296): dam_break / surge are LAND-DOMINATED / mixed-coastal (NLCD covers the
    # real land cover, including its own "Open Water" class over any coastal water
    # in the AOI) -> NLCD area-weighted derivation (resolve_overland_manning).
    # tsunami is OFFSHORE (GEOCLAW_OFFSHORE_SCENARIOS): no NLCD coverage, so the
    # published Chow (1959) 0.025 open-water standard is kept, now loudly labeled
    # (consequence="numerical", not "physics" -- an established universal
    # constant, not an invented site-specific value).
    _scenario_l = str(scenario).strip().lower()
    _manning_offshore = _scenario_l in GEOCLAW_OFFSHORE_SCENARIOS
    _manning_res = None
    _manning_provenance: list[SyntheticInput] = []
    if _manning_offshore:
        if manning_n is not None:
            _manning_n_for_gate = float(manning_n)
            _manning_provenance.append(SyntheticInput(
                param="manning_n", value=_manning_n_for_gate, units="s/m^(1/3)",
                basis="user", note="caller-supplied bottom-friction Manning's n.",
            ))
        else:
            _manning_n_for_gate = 0.025
            _manning_provenance.append(SyntheticInput(
                param="manning_n", value=_manning_n_for_gate, units="s/m^(1/3)",
                basis="default_demo", consequence="numerical",
                note=(
                    "offshore seabed friction: NLCD has no deep-ocean coverage; "
                    "the published Chow (1959) open-water standard (n=0.025, the "
                    "same value manning_mapping.csv assigns NLCD class 11 Open "
                    "Water) is used. Supply manning_n for a calibrated value."
                ),
            ))
    else:
        _manning_res = await resolve_overland_manning(
            coerced, manning_n, param_name="manning_n",
        )
        _manning_provenance.append(_manning_res.entry)
        _manning_n_for_gate = _manning_res.manning_n  # may be None (unresolved)

    # --- input-review gate: the AMR windows are the consequential,
    # model-invented input on this template (they place WHERE the mesh refines), so
    # they must ride the review gate -- the LLM cannot silently invent window
    # placement. Each resolved window is a provenance entry; ``window_basis`` marks
    # whether the model derived it from the prompt ("prompt_interpreted") or it was
    # an explicit/drawn geometry ("user"). In auto mode the entries ride the
    # assumptions block labeled; in user_gated mode the run HOLDS for review before
    # the solve. A headless direct-call has no live session -> the gate fails open.
    _basis = "user" if str(window_basis).strip().lower() == "user" else "prompt_interpreted"
    _window_entries = [
        SyntheticInput(
            param=f"amr_window_{i + 1}",
            value=(
                f"L{w.min_level}-{w.max_level} "
                f"lon[{w.min_lon:.4f},{w.max_lon:.4f}] "
                f"lat[{w.min_lat:.4f},{w.max_lat:.4f}] "
                f"t[{w.t_start_s:.0f},{w.t_end_s:.0f}]s"
            ),
            basis=_basis,  # type: ignore[arg-type]
            note="explicit AMR refinement window (forces the box to the level range)",
        )
        for i, w in enumerate(windows)
    ]
    _review = await gate_input_review(
        tool_name="geoclaw_amr_refinement_regions",
        mode=input_mode,
        entries=_window_entries + _manning_provenance,
        params={"manning_n": _manning_n_for_gate},
    )
    if _review.cancelled:
        return {
            "status": "error",
            "error_code": "USER_INPUT_CANCELLED",
            "error_message": (
                f"geoclaw_amr_refinement_regions {_review.cancel_reason}"
            ),
        }
    _mn_reviewed = _review.params.get("manning_n")
    effective_manning_n = float(_mn_reviewed) if _mn_reviewed is not None else None
    if effective_manning_n is None:
        # Unresolved land-dominated Manning's n (NLCD could not serve) survived to
        # here -- auto mode already refuses via the physics-consequence gate
        # above; this is the user_gated backstop (a "proceed" reply cannot make a
        # None friction coefficient runnable). Mirrors the geoclaw_inundation
        # precedent (ADR 0296).
        return {
            "status": "error",
            "error_code": "GEOCLAW_PHYSICS_INPUT_REQUIRED",
            "error_message": (
                str(_manning_res.entry.note) if _manning_res is not None
                else "geoclaw_amr_refinement_regions: manning_n could not be resolved."
            ),
        }

    try:
        run_args = GeoClawRunArgs(
            bbox=tuple(coerced),  # type: ignore[arg-type]
            scenario=scenario,  # type: ignore[arg-type]
            source_lonlat=src,
            source_magnitude=float(source_magnitude),
            dam_break_depth_m=float(dam_break_depth_m),
            sim_duration_s=float(sim_duration_s),
            output_frames=int(output_frames),
            amr_levels=int(amr_levels),
            manning_n=float(effective_manning_n),
            sea_level_m=float(sea_level_m),
            amr_regions=windows,
        )
    except Exception as exc:  # noqa: BLE001 -- pydantic ValidationError / coercion
        return {
            "status": "error",
            "error_code": "GEOCLAW_PARAMS_INVALID",
            "error_message": f"invalid GeoClaw amr-regions arguments: {exc}",
        }

    logger.info(
        "geoclaw_amr_refinement_regions bbox=%s scenario=%s amr_levels=%d regions=%d",
        run_args.bbox,
        run_args.scenario,
        run_args.amr_levels,
        len(run_args.amr_regions),
    )

    try:
        primary = await model_geoclaw_inundation(
            run_args,
            compute_class=compute_class,
        )
        # Stamp the reviewed windows onto the result provenance so the assumptions
        # block narrates WHERE the mesh was refined (and with what basis) -- what was
        # reviewed == what ran.
        if _review.entries:
            primary.synthetic_inputs = list(primary.synthetic_inputs) + list(
                _review.entries
            )
        logger.info(
            "geoclaw_amr_refinement_regions complete layer_id=%s max_depth_m=%.4g "
            "arrival_s=%s uri=%s",
            primary.layer_id,
            primary.max_depth_m,
            primary.arrival_time_s,
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
            "geoclaw_amr_refinement_regions failed: %s (%s)",
            getattr(exc, "error_code", "?"),
            exc,
        )
        return {
            "status": "error",
            "error_code": getattr(exc, "error_code", "GEOCLAW_INTERNAL_ERROR"),
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001 -- defensive catch-all
        logger.exception("geoclaw_amr_refinement_regions unexpected failure")
        return {
            "status": "error",
            "error_code": "GEOCLAW_INTERNAL_ERROR",
            "error_message": str(exc),
        }
