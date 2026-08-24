"""Engine template ``landlab_hacks_law_scaling`` - Landlab Hack's-law basin
length-area scaling diagnostic (HackCalculator).

A distinct question CLASS (per the capability-naming rule): does this basin's
longest-flow-path vs drainage-area scaling follow Hack's law, and how does the
fitted exponent compare to the classic ~0.5-0.6? A chart-led diagnostic on the
already-computed FlowAccumulator fields. It is its OWN registered engine TEMPLATE
(engine="landlab", tier="template").

``landlab_hacks_law_scaling(...)`` runs the deterministic fetch DEM -> stage ->
solve -> postprocess chain: FlowAccumulator + HackCalculator over the AOI DEM,
and returns a ``LandlabHacksLawLayerURI`` (the log-styled drainage-area backdrop)
plus the largest fitted basin vector and a length-vs-area log-log scatter chart
with the fitted exponent. Landlab runs OFF-BOX in the local-exec / Batch solver
seam.

Determinism boundary (Invariant 1): every number the agent narrates comes from
the typed ``LandlabHacksLawLayerURI`` fields the worker / postprocess computed.
The DEM is REAL; the diagnostic reads deterministic engine outputs.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.landlab_contracts import (
    LandlabHacksLawLayerURI,
    LandlabRunArgs,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.gates.input_review import gate_input_review
from trid3nt_server.tools.tool_arg_normalizer import coerce_bbox_value
from trid3nt_server.tools import register_tool
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
    DRAINAGE_AREA_STYLE_PRESET,
    PostprocessLandlabError,
    build_hacks_law_chart_spec,
    postprocess_landlab_hacks_law,
)
from trid3nt_server.workflows.landlab.run_landlab import LandlabWorkflowError
from trid3nt_server.workflows.landlab.susceptibility.susceptibility import (
    LandslideWorkflowError,
)
from trid3nt_server.emission.pipeline_emitter import current_emitter, substep

logger = logging.getLogger(
    "trid3nt_server.workflows.landlab.hacks_law.hacks_law"
)

__all__ = [
    "landlab_hacks_law_scaling",
    "model_landlab_hacks_law_scaling",
    "HacksLawWorkflowError",
]


class HacksLawWorkflowError(RuntimeError):
    """Raised on a fatal composer failure (carries an open-set ``error_code``)."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


TEMPLATE_CARD = TemplateCard(
    question=(
        "does this basin follow Hack's law - the longest-flow-path vs "
        "drainage-area scaling exponent and how it compares to the classic "
        "~0.5-0.6 (Landlab HackCalculator; log-log scatter + basin vector)"
    ),
    required_inputs=["bbox"],
    knobs="target_resolution_m",
)

_METADATA = AtomicToolMetadata(
    name="landlab_hacks_law_scaling",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="landlab",
    tier="template",
    resolution_specs=(LANDLAB_RES_SPEC,),
)


@register_tool(
    _METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def landlab_hacks_law_scaling(
    bbox: tuple[float, float, float, float] | list[float] | str | None = None,
    target_resolution_m: float = 30.0,
    compute_class: str = "standard",
    input_mode: str | None = None,
    **_extra_ignored: Any,
) -> LandlabHacksLawLayerURI | dict[str, Any]:
    """Fit Hack's law (length vs drainage area) for the basins in a DEM.

    Fidelity: Landlab HackCalculator (ChannelProfiler-based) on a real AOI DEM; a
    geomorphic scaling diagnostic, not a calibrated model.
    Data: the DEM is REAL (USGS 3DEP via seam-1). The fit reads deterministic
    FlowAccumulator outputs - no synthetic data.
    Off-scope: drainage area / channel network map -> landlab_flow_accumulation;
    DEM routability / fill depth -> landlab_dem_conditioning; height above
    drainage -> landlab_hand_wetness.

    Use this when: the user asks about HACK'S LAW, the length-area (or
    length-drainage-area) SCALING of a basin, the Hack exponent, or basin
    geomorphic scaling over an AOI.

    Params:
        bbox: watershed / catchment AOI, EPSG:4326.
        target_resolution_m: grid cell size, m (default 30).
        compute_class: compute class (default "standard").
        input_mode: run-mode lever. "user_gated" reviews the resolution before the
            solve; "auto" (default) proceeds labeled.

    Returns:
        On success: ``LandlabHacksLawLayerURI`` - the drainage-area backdrop COG,
        with ``hack_exponent``, ``hack_coefficient``, ``largest_basin_area_km2``,
        ``n_basins``. The fitted basin vector + a log-log scatter chart are emitted
        alongside.
        On failure: ``{"status": "error", "error_code", "error_message"}``.
        Not cached (``cacheable=False``).
    """
    if bbox is None:
        return {
            "status": "error",
            "error_code": "LANDLAB_PARAMS_INCOMPLETE",
            "error_message": (
                "landlab_hacks_law_scaling requires a bbox "
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

    source_note = "DEM: USGS 3DEP (fetched). Hack fit reads deterministic engine outputs."
    _review = await gate_input_review(
        tool_name="landlab_hacks_law_scaling",
        mode=input_mode,
        entries=[],
        params={"target_resolution_m": target_resolution_m},
    )
    if _review.cancelled:
        return {
            "status": "error",
            "error_code": "USER_INPUT_CANCELLED",
            "error_message": f"landlab_hacks_law_scaling {_review.cancel_reason}",
        }
    target_resolution_m = float(
        _review.params.get("target_resolution_m", target_resolution_m)
    )

    try:
        run_args = LandlabRunArgs(
            bbox=tuple(coerced),  # type: ignore[arg-type]
            analysis="hacks_law",
            target_resolution_m=float(target_resolution_m),
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "error",
            "error_code": "LANDLAB_PARAMS_INVALID",
            "error_message": f"invalid Landlab Hack's-law arguments: {exc}",
        }

    logger.info(
        "landlab_hacks_law_scaling bbox=%s res=%.1fm",
        run_args.bbox,
        run_args.target_resolution_m,
    )

    try:
        primary = await model_landlab_hacks_law_scaling(
            run_args, compute_class=compute_class, source_note=source_note
        )
        logger.info(
            "landlab_hacks_law_scaling complete layer_id=%s exponent=%.4f "
            "n_basins=%d uri=%s",
            primary.layer_id,
            primary.hack_exponent,
            primary.n_basins,
            primary.uri,
        )
        return primary
    except asyncio.CancelledError:
        raise
    except (
        LandlabWorkflowError,
        PostprocessLandlabError,
        LandslideWorkflowError,
        HacksLawWorkflowError,
    ) as exc:
        logger.warning(
            "landlab_hacks_law_scaling failed: %s (%s)",
            getattr(exc, "error_code", "?"),
            exc,
        )
        return {
            "status": "error",
            "error_code": getattr(exc, "error_code", "LANDLAB_INTERNAL_ERROR"),
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("landlab_hacks_law_scaling unexpected failure")
        return {
            "status": "error",
            "error_code": "LANDLAB_INTERNAL_ERROR",
            "error_message": str(exc),
        }


async def model_landlab_hacks_law_scaling(
    run_args: LandlabRunArgs,
    *,
    dem_path: str | None = None,
    run_id: str | None = None,
    compute_class: str = "standard",
    source_note: str | None = None,
    synthetic_inputs: list[SyntheticInput] | None = None,
) -> LandlabHacksLawLayerURI:
    """Compose the Hack's-law diagnostic chain end-to-end (OFF-BOX lane).

    Returns the drainage-area ``LandlabHacksLawLayerURI``; emits the fitted basin
    vector + the log-log scatter chart as side effects on the bound emitter.
    """
    emitter = current_emitter()
    solve = await stage_solve_download(
        run_args, dem_path=dem_path, run_id=run_id, compute_class=compute_class
    )
    basin_cog = (solve.secondary_cogs or {}).get("basin")

    try:
        async with substep(current_emitter(), "postprocess_landlab"):
            layers, metrics = await asyncio.to_thread(
                postprocess_landlab_hacks_law,
                solve.local_field,
                run_id=solve.run_id,
                result=solve.result_block,
                basin_cog_path=basin_cog,
            )
    finally:
        cleanup_solve(solve)

    if not layers:
        raise HacksLawWorkflowError(
            "LANDLAB_NO_LAYERS",
            "postprocess_landlab_hacks_law produced no drainage-area layer",
        )

    raw_primary = layers[0]
    context_layers = layers[1:]

    async with substep(current_emitter(), "publish_layer"):
        primary = await asyncio.to_thread(
            publish_raster_layer, raw_primary, default_style=DRAINAGE_AREA_STYLE_PRESET
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

    if emitter is not None:
        for ctx in context_layers:
            try:
                await emitter.add_loaded_layer(ctx)
            except Exception as exc:  # noqa: BLE001 - non-fatal
                logger.debug("could not add fitted-basin vector: %s", exc)

    await emit_landlab_chart(
        emitter,
        build_hacks_law_chart_spec(
            metrics.get("scatter") or [],
            exponent=float(metrics.get("hack_exponent", primary.hack_exponent)),
            coefficient=float(metrics.get("hack_coefficient", primary.hack_coefficient)),
        ),
        title="Hack's law (length vs drainage area)",
        caption=(
            "Longest-flow-path length vs drainage area on log-log axes with the "
            "fitted L = C * A**h line - the Hack exponent against the classic "
            "~0.5-0.6."
        ),
        source_uri=primary.uri,
    )
    await emit_zoom_to(emitter, solve.bbox)
    return primary
