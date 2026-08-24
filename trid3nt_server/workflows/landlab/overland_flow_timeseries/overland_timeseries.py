"""Engine template ``landlab_overland_flow_timeseries`` - Landlab de Almeida
overland flow written frame by frame over the storm.

A distinct question CLASS from ``landlab_susceptibility(analysis="overland_flow")``
(per the capability-naming rule): instead of only the peak surface-water depth,
show how inundation GROWS and RECEDES frame by frame during the storm - the
time-stepped animation output. It is its OWN registered engine TEMPLATE
(engine="landlab", tier="template").

``landlab_overland_flow_timeseries(...)`` runs the deterministic fetch DEM ->
stage -> solve -> postprocess chain: OverlandFlow stepped over a NOAA Atlas-14
design storm while snapshotting depth every ``output_interval_s``, and returns a
``LandlabOverlandTimeseriesLayerURI`` (the peak-depth raster) plus per-interval
depth animation frames (the center-scrubber temporal group) and a depth-vs-time
chart at the maximum-depth cell. Landlab runs OFF-BOX in the local-exec / Batch
solver seam.

Determinism boundary (Invariant 1): every number the agent narrates comes from
the typed ``LandlabOverlandTimeseriesLayerURI`` fields the worker / postprocess
computed. The DEM is REAL; the triggering rainfall is the real NOAA Atlas-14
design storm.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.landlab_contracts import (
    DEFAULT_OUTPUT_INTERVAL_S,
    LandlabOverlandTimeseriesLayerURI,
    LandlabRunArgs,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.gates.input_review import gate_input_review
from trid3nt_server.tools.tool_arg_normalizer import coerce_bbox_value
from trid3nt_server.tools import register_tool
from trid3nt_server.emission.outputs_seam import (
    build_layers_from_outputs,
    read_outputs_manifest,
)
from trid3nt_server.workflows.landlab._composer_common import (
    LANDLAB_RES_SPEC,
    cleanup_solve,
    emit_landlab_chart,
    emit_zoom_to,
    publish_raster_layer,
    stage_solve_download,
)
from trid3nt_server.workflows.landlab._template_card import TemplateCard
from trid3nt_server.workflows.landlab.postprocess_landlab import (
    OVERLAND_STYLE_PRESET,
    PostprocessLandlabError,
    build_overland_hydrograph_chart_spec,
    postprocess_landlab_overland_timeseries,
)
from trid3nt_server.workflows.landlab.run_landlab import LandlabWorkflowError
from trid3nt_server.workflows.landlab.susceptibility.susceptibility import (
    _DEFAULT_TRIGGER_DURATION_HR,
    LandslideWorkflowError,
    _atlas14_design_storm_mm,
)
from trid3nt_server.emission.pipeline_emitter import current_emitter, substep

logger = logging.getLogger(
    "trid3nt_server.workflows.landlab.overland_flow_timeseries.overland_timeseries"
)

__all__ = [
    "landlab_overland_flow_timeseries",
    "model_landlab_overland_flow_timeseries",
    "OverlandTimeseriesWorkflowError",
]


class OverlandTimeseriesWorkflowError(RuntimeError):
    """Raised on a fatal composer failure (carries an open-set ``error_code``)."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


TEMPLATE_CARD = TemplateCard(
    question=(
        "how storm inundation GROWS and RECEDES frame by frame over a DEM - the "
        "time-stepped overland-flow depth animation plus the peak-depth raster "
        "and a depth-vs-time hydrograph at the deepest cell (Landlab OverlandFlow)"
    ),
    required_inputs=["bbox"],
    knobs=(
        "output_interval_s, rainfall_return_period_yr, storm_duration_hr, "
        "rainfall_intensity_mm_hr, target_resolution_m, condition_dem"
    ),
)

_METADATA = AtomicToolMetadata(
    name="landlab_overland_flow_timeseries",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="landlab",
    tier="template",
    resolution_specs=(LANDLAB_RES_SPEC,),
)

_DEFAULT_RETURN_PERIOD_YR: int = 100


@register_tool(
    _METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def landlab_overland_flow_timeseries(
    bbox: tuple[float, float, float, float] | list[float] | str | None = None,
    rainfall_intensity_mm_hr: float | None = None,
    storm_duration_hr: float = 2.0,
    rainfall_return_period_yr: int = _DEFAULT_RETURN_PERIOD_YR,
    output_interval_s: float = DEFAULT_OUTPUT_INTERVAL_S,
    target_resolution_m: float = 30.0,
    condition_dem: bool = False,
    compute_class: str = "standard",
    input_mode: str | None = None,
    **_extra_ignored: Any,
) -> LandlabOverlandTimeseriesLayerURI | dict[str, Any]:
    """Route a storm over a DEM as a time-stepped overland-flow depth animation.

    Fidelity: Landlab de Almeida OverlandFlow on a real AOI DEM; a planning-grade
    time-resolved inundation surface, not a calibrated hydraulic model.
    Data: the DEM is REAL (USGS 3DEP via seam-1). The triggering rainfall is the
    real NOAA Atlas-14 design storm (rainfall_return_period_yr / storm_duration_hr)
    - ``rainfall_intensity_mm_hr`` is DERIVED from it when unset; a failed lookup
    STOPS with a typed gate (never a baked default).
    Off-scope: peak-only overland depth -> landlab_susceptibility(analysis=
    "overland_flow"); storm infiltration / runoff partition ->
    landlab_green_ampt_overland_flow; riverine/coastal inundation -> sfincs_flood.

    Use this when: the user wants a TIME-STEPPED / animated overland-flow depth,
    to watch inundation grow and recede frame by frame, or a depth-vs-time
    hydrograph over a catchment.

    Params:
        bbox: catchment AOI, EPSG:4326.
        rainfall_intensity_mm_hr: overland rainfall intensity, mm/hr. Unset ->
            DERIVED from the Atlas-14 design storm (depth / storm_duration_hr).
        storm_duration_hr: storm / simulation duration, hours (default 2); also
            the Atlas-14 lookup duration.
        rainfall_return_period_yr: design-storm return period, years (default 100).
        output_interval_s: seconds between depth snapshots (default 300).
        target_resolution_m: grid cell size, m (default 30).
        condition_dem: OPT-IN depression-fill of the DEM before routing (default
            False -- the raw-DEM response is the default). Set True to route the
            storm over a pit-filled surface so flow traces connected valleys
            instead of ponding in the DEM's closed depressions.
        compute_class: compute class (default "standard").
        input_mode: run-mode lever. "user_gated" presents the resolved triggering
            rainfall for review before the solve; "auto" (default) proceeds
            labeled.

    Returns:
        On success: ``LandlabOverlandTimeseriesLayerURI`` - the peak-depth COG,
        with ``wet_area_fraction``, ``max_depth_m``, ``n_frames``,
        ``time_to_peak_s``. Per-interval depth animation frames + a depth-vs-time
        chart are emitted alongside.
        On failure: ``{"status": "error", "error_code", "error_message"}``.
        Not cached (``cacheable=False``).
    """
    if bbox is None:
        return {
            "status": "error",
            "error_code": "LANDLAB_PARAMS_INCOMPLETE",
            "error_message": (
                "landlab_overland_flow_timeseries requires a bbox "
                "(min_lon, min_lat, max_lon, max_lat) in EPSG:4326."
            ),
        }
    coerced = coerce_bbox_value(bbox)
    if coerced is None:
        return {
            "status": "error",
            "error_code": "LANDLAB_PARAMS_INVALID",
            "error_message": f"invalid bbox: {bbox!r}",
        }

    _dur_hr = (
        float(storm_duration_hr)
        if storm_duration_hr is not None
        else _DEFAULT_TRIGGER_DURATION_HR
    )
    provenance: list[SyntheticInput] = []
    _rainfall_label = ""
    if rainfall_intensity_mm_hr is None:
        _depth_mm = await asyncio.to_thread(
            _atlas14_design_storm_mm, tuple(coerced), int(rainfall_return_period_yr), _dur_hr
        )
        if _depth_mm is None:
            return {
                "status": "error",
                "error_code": "LANDLAB_RAINFALL_INPUT_REQUIRED",
                "error_message": (
                    f"The NOAA Atlas-14 design-storm lookup failed for this AOI "
                    f"({rainfall_return_period_yr}-yr / {_dur_hr:.1f}-hr), so the "
                    f"triggering rainfall is not fabricated. Retry with an explicit "
                    f"rainfall_intensity_mm_hr - or an AOI within Atlas-14 coverage "
                    f"(CONUS / PR / USVI)."
                ),
            }
        rainfall_intensity_mm_hr = round(_depth_mm / max(_dur_hr, 1e-6), 2)
        _rainfall_label = (
            f"overland rainfall intensity {rainfall_intensity_mm_hr:.1f} mm/hr "
            f"(NOAA Atlas-14 {rainfall_return_period_yr}-yr/{_dur_hr:.1f}-hr design "
            f"storm, {_depth_mm:.1f} mm total)"
        )
        provenance.append(
            SyntheticInput(
                param="rainfall_intensity_mm_hr",
                value=rainfall_intensity_mm_hr,
                units="mm/hr",
                basis="derived",
                real_source_if_any="lookup_precip_return_period (NOAA Atlas-14)",
                note=f"{rainfall_return_period_yr}-yr/{_dur_hr:.1f}-hr design storm",
            )
        )
    else:
        _rainfall_label = "triggering rainfall: user-supplied"
        provenance.append(SyntheticInput(param="rainfall_intensity_mm_hr", basis="user"))
    _conditioning_note = (
        "DEM depression-filled before routing (connected valley flow)"
        if condition_dem
        else "raw DEM (unconditioned; flow may pond in sink pits)"
    )
    source_note = f"{_rainfall_label}. {_conditioning_note}"

    _review = await gate_input_review(
        tool_name="landlab_overland_flow_timeseries",
        mode=input_mode,
        entries=provenance,
        params={
            "rainfall_intensity_mm_hr": rainfall_intensity_mm_hr,
            "output_interval_s": output_interval_s,
        },
    )
    if _review.cancelled:
        return {
            "status": "error",
            "error_code": "USER_INPUT_CANCELLED",
            "error_message": f"landlab_overland_flow_timeseries {_review.cancel_reason}",
        }
    provenance = _review.entries
    rainfall_intensity_mm_hr = float(
        _review.params.get("rainfall_intensity_mm_hr", rainfall_intensity_mm_hr)
    )
    output_interval_s = float(_review.params.get("output_interval_s", output_interval_s))

    try:
        run_args = LandlabRunArgs(
            bbox=tuple(coerced),  # type: ignore[arg-type]
            analysis="overland_flow_timeseries",
            target_resolution_m=float(target_resolution_m),
            rainfall_intensity_mm_hr=float(rainfall_intensity_mm_hr),
            storm_duration_hr=float(_dur_hr),
            output_interval_s=float(output_interval_s),
            condition_dem=bool(condition_dem),
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "error",
            "error_code": "LANDLAB_PARAMS_INVALID",
            "error_message": f"invalid Landlab overland-timeseries arguments: {exc}",
        }

    logger.info(
        "landlab_overland_flow_timeseries bbox=%s rain=%.1fmm/hr dur=%.2fh "
        "interval=%.0fs res=%.1fm",
        run_args.bbox,
        run_args.rainfall_intensity_mm_hr,
        run_args.storm_duration_hr,
        run_args.output_interval_s,
        run_args.target_resolution_m,
    )

    try:
        primary = await model_landlab_overland_flow_timeseries(
            run_args,
            compute_class=compute_class,
            source_note=source_note,
            synthetic_inputs=provenance,
        )
        logger.info(
            "landlab_overland_flow_timeseries complete layer_id=%s max_depth=%.4f m "
            "frames=%d uri=%s",
            primary.layer_id,
            primary.max_depth_m,
            primary.n_frames,
            primary.uri,
        )
        return primary
    except asyncio.CancelledError:
        raise
    except (
        LandlabWorkflowError,
        PostprocessLandlabError,
        LandslideWorkflowError,
        OverlandTimeseriesWorkflowError,
    ) as exc:
        logger.warning(
            "landlab_overland_flow_timeseries failed: %s (%s)",
            getattr(exc, "error_code", "?"),
            exc,
        )
        return {
            "status": "error",
            "error_code": getattr(exc, "error_code", "LANDLAB_INTERNAL_ERROR"),
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("landlab_overland_flow_timeseries unexpected failure")
        return {
            "status": "error",
            "error_code": "LANDLAB_INTERNAL_ERROR",
            "error_message": str(exc),
        }


async def model_landlab_overland_flow_timeseries(
    run_args: LandlabRunArgs,
    *,
    dem_path: str | None = None,
    run_id: str | None = None,
    compute_class: str = "standard",
    source_note: str | None = None,
    synthetic_inputs: list[SyntheticInput] | None = None,
) -> LandlabOverlandTimeseriesLayerURI:
    """Compose the time-stepped overland-flow chain end-to-end (OFF-BOX lane).

    Returns the peak-depth ``LandlabOverlandTimeseriesLayerURI``; emits the
    per-interval animation frames + the depth-vs-time chart as side effects.
    """
    emitter = current_emitter()
    solve = await stage_solve_download(
        run_args, dem_path=dem_path, run_id=run_id, compute_class=compute_class
    )

    frame_cogs = {
        tok: path
        for tok, path in (solve.secondary_cogs or {}).items()
        if tok.startswith("depth_step_")
    }

    try:
        async with substep(current_emitter(), "postprocess_landlab"):
            layers, metrics = await asyncio.to_thread(
                postprocess_landlab_overland_timeseries,
                solve.local_field,
                run_id=solve.run_id,
                result=solve.result_block,
                frame_cogs_by_token=frame_cogs,
            )
    finally:
        cleanup_solve(solve)

    if not layers:
        raise OverlandTimeseriesWorkflowError(
            "LANDLAB_NO_LAYERS",
            "postprocess_landlab_overland_timeseries produced no depth layer",
        )

    raw_primary = layers[0]

    async with substep(current_emitter(), "publish_layer"):
        primary = await asyncio.to_thread(
            publish_raster_layer, raw_primary, default_style=OVERLAND_STYLE_PRESET
        )

    if tuple(primary.bbox or ()) != tuple(solve.bbox):
        primary = primary.model_copy(update={"bbox": tuple(solve.bbox)})
    _upd: dict[str, Any] = {}
    if source_note is not None:
        _upd["source_note"] = source_note
    if synthetic_inputs:
        _upd["synthetic_inputs"] = list(synthetic_inputs)
    if _upd:
        primary = primary.model_copy(update=_upd)

    # --- Per-interval animation frames (EMIT-ON-SOLVE SEAM, ADR 0282) ---------
    # postprocess_landlab_overland_timeseries wrote the peak + EVERY snapshot's
    # depth COG to outputs.json host-side. The SEAM owns the TEMPORAL FRAMES
    # (frames_only=True -> it skips the peak entry; the typed peak above stays the
    # composer-built return). Each frame is published through the render chokepoint
    # + emitted as a center-scrubber temporal group (the "Overland depth step N"
    # name token rides through). Absent/unreadable outputs.json -> no frames (an
    # honest peak-only degrade). No worker-image staleness -- the writer runs
    # agent-side (exec-from-source worker + host postprocess).
    if emitter is not None:
        seam_frames = await asyncio.to_thread(
            _read_overland_frame_layers, solve.run_id, tuple(solve.bbox)
        )
        for frame in seam_frames:
            pub = await asyncio.to_thread(
                publish_raster_layer, frame, default_style=OVERLAND_STYLE_PRESET
            )
            try:
                await emitter.add_loaded_layer(pub)
            except Exception as exc:  # noqa: BLE001 - non-fatal
                logger.debug("could not add overland depth frame: %s", exc)

    await emit_landlab_chart(
        emitter,
        build_overland_hydrograph_chart_spec(metrics.get("max_cell_series") or []),
        title="Depth vs time (deepest cell)",
        caption=(
            "Surface-water depth over the storm at the cell that reached the peak "
            "depth - the overland-flow hydrograph."
        ),
        source_uri=primary.uri,
    )
    await emit_zoom_to(emitter, solve.bbox)
    return primary


def _read_overland_frame_layers(
    run_id: str, bbox: tuple[float, float, float, float]
) -> list[Any]:
    """Read ``outputs.json`` -> the SEAM's overland-depth frame layers (frames-only).

    The emit-on-solve fork (ADR 0282): the postprocess wrote the peak + every
    snapshot's depth COG to ``outputs.json`` host-side. This builds the CONTEXT
    frame layers via ``build_layers_from_outputs`` with ``frames_only=True`` (the
    peak entry is skipped -- the composer keeps its own typed peak). Returns
    ``[]`` on an absent / unreadable / unknown-schema manifest (an honest
    peak-only degrade) -- never raises.
    """
    import types as _types

    manifest = read_outputs_manifest(_types.SimpleNamespace(run_id=run_id))
    if manifest is None:
        logger.info(
            "model_landlab_overland_flow_timeseries: no outputs.json for "
            "run_id=%s -- peak-only (no animation frames).",
            run_id,
        )
        return []
    seam = build_layers_from_outputs(
        manifest, run_id=run_id, bbox=tuple(bbox), frames_only=True
    )
    return [lyr for lyr in seam.layers if lyr.role == "context"]
