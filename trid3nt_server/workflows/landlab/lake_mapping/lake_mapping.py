"""Engine template ``landlab_lake_mapping`` - Landlab lake extent + depth
mapping (LakeMapperBarnes with lake tracking).

A distinct question CLASS (per the capability-naming rule): where do closed
depressions pond into lakes, and how deep are they? The per-cell LAKE DEPTH
within mapped lakes + the lake EXTENT vector are the deliverables. It is its OWN
registered engine TEMPLATE (engine="landlab", tier="template"). Shares the
LakeMapperBarnes plumbing with ``landlab_dem_conditioning`` (the fill-depth
diagnostic), reading its lake-tracking outputs.

``landlab_lake_mapping(...)`` runs the deterministic fetch DEM -> stage -> solve
-> postprocess chain and returns a ``LandlabLakeMappingLayerURI`` (the lake-depth
raster) plus a lake-extent vector. Landlab runs OFF-BOX in the local-exec / Batch
solver seam.

Determinism boundary (Invariant 1): every number the agent narrates comes from
the typed ``LandlabLakeMappingLayerURI`` fields the worker / postprocess
computed. The DEM is REAL; the fill mode is a deterministic engine setting.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.landlab_contracts import (
    DEFAULT_MIN_LAKE_AREA_M2,
    DEFAULT_MIN_LAKE_DEPTH_M,
    LandlabLakeMappingLayerURI,
    LandlabRunArgs,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.gates.input_review import gate_input_review
from trid3nt_server.data.tool_arg_normalizer import coerce_bbox_value
from trid3nt_server.data import register_tool
from trid3nt_server.workflows.landlab._composer_common import (
    LANDLAB_RES_SPEC,
    cleanup_solve,
    emit_zoom_to,
    publish_raster_layer,
    stage_solve_download,
)
from trid3nt_server.workflows.landlab._template_card import TemplateCard
from trid3nt_server.workflows.landlab.postprocess_landlab import (
    LAKE_DEPTH_STYLE_PRESET,
    PostprocessLandlabError,
    postprocess_landlab_lake_mapping,
)
from trid3nt_server.workflows.landlab.run_landlab import LandlabWorkflowError
from trid3nt_server.workflows.landlab.susceptibility.susceptibility import (
    LandslideWorkflowError,
)
from trid3nt_server.emission.pipeline_emitter import current_emitter, substep

logger = logging.getLogger(
    "trid3nt_server.workflows.landlab.lake_mapping.lake_mapping"
)

__all__ = [
    "landlab_lake_mapping",
    "model_landlab_lake_mapping",
    "LakeMappingWorkflowError",
]


class LakeMappingWorkflowError(RuntimeError):
    """Raised on a fatal composer failure (carries an open-set ``error_code``)."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


TEMPLATE_CARD = TemplateCard(
    question=(
        "where do closed depressions pond into lakes on this DEM and how deep - "
        "the lake extent + per-cell lake-depth layer (Landlab LakeMapperBarnes)"
    ),
    required_inputs=["bbox"],
    knobs="fill_flat, target_resolution_m, min_lake_depth_m, min_lake_area_m2",
)

_METADATA = AtomicToolMetadata(
    name="landlab_lake_mapping",
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
async def landlab_lake_mapping(
    bbox: tuple[float, float, float, float] | list[float] | str | None = None,
    fill_flat: bool = True,
    target_resolution_m: float = 30.0,
    min_lake_depth_m: float = DEFAULT_MIN_LAKE_DEPTH_M,
    min_lake_area_m2: float = DEFAULT_MIN_LAKE_AREA_M2,
    compute_class: str = "standard",
    input_mode: str | None = None,
    **_extra_ignored: Any,
) -> LandlabLakeMappingLayerURI | dict[str, Any]:
    """Map topographic closed basins / potential impoundments from a DEM
    (LakeMapperBarnes fill-depth).

    What this IS: a TERRAIN diagnostic - the closed basins in the bare-earth DEM
    that WOULD impound water (their fill depth + extent), NOT a map of existing
    waterbodies. An existing water surface (a reservoir at pool, a lake) is
    recorded in the DEM as a FLAT surface, so its fill depth is ~0 and it is NOT
    detected here; the DEM only reveals the basin below the water line where the
    terrain is dry or the pool is drawn down. For EXISTING lakes/reservoirs use
    fetch_nhd_waterbodies (the mapped-hydrography source), not this template.
    Fidelity: Landlab LakeMapperBarnes on a real AOI DEM; a terrain-ponding
    diagnostic, not a hydrologic reservoir model. LakeMapperBarnes maps EVERY
    closed depression, so depth/area floors (min_lake_depth_m / min_lake_area_m2)
    discriminate real basins from DEM noise pits; ``n_lakes_raw`` (mapped) vs
    ``n_lakes_kept`` (after filtering) are reported.
    Data: the DEM is REAL (USGS 3DEP via seam-1). The fill mode (fill_flat) and
    discrimination floors are deterministic engine settings - no synthetic data.
    Off-scope: existing waterbodies -> fetch_nhd_waterbodies; routability /
    fill-depth conditioning -> landlab_dem_conditioning; drainage area / channel
    network -> landlab_flow_accumulation; overland-flow inundation depth ->
    landlab_overland_flow_timeseries.

    Use this when: the user asks which terrain CLOSED BASINS would pond water /
    where the DEM has potential impoundments, for a basin extent map, or basin
    fill depth / storage over an AOI. NOT for locating existing lakes/reservoirs.

    Params:
        bbox: AOI, EPSG:4326.
        fill_flat: True fills each lake flat; False fills to a slight downslope
            incline (default True).
        target_resolution_m: grid cell size, m (default 30).
        min_lake_depth_m: keep a depression as a real lake only if its deepest
            point is at least this deep, m (default 1.0). Lower to catch shallow
            ponds; raise to keep only deep lakes.
        min_lake_area_m2: keep a depression as a real lake only if its surface
            area is at least this large, m^2 (default 10000, ~11 cells at 30 m).
            Lower to catch small ponds; raise to keep only large lakes.
        compute_class: compute class (default "standard").
        input_mode: run-mode lever. "user_gated" presents the fill setting for
            review; "auto" (default) proceeds labeled.

    Returns:
        On success: ``LandlabLakeMappingLayerURI`` - the lake-depth COG, with
        ``n_lakes`` (kept), ``n_lakes_raw``, ``n_lakes_kept``,
        ``total_lake_area_km2``, ``total_lake_volume_m3``, ``max_lake_depth_m``.
        A lake-extent vector is emitted alongside.
        On failure: ``{"status": "error", "error_code", "error_message"}``.
        Not cached (``cacheable=False``).
    """
    if bbox is None:
        return {
            "status": "error",
            "error_code": "LANDLAB_PARAMS_INCOMPLETE",
            "error_message": (
                "landlab_lake_mapping requires a bbox "
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

    source_note = (
        "Maps TOPOGRAPHIC closed basins / potential impoundments from the "
        "bare-earth DEM (where terrain WOULD pond water), NOT existing "
        "waterbodies: a reservoir/lake at pool is a FLAT surface in the DEM and "
        "is NOT detected - use fetch_nhd_waterbodies for existing waterbodies. "
        f"DEM: USGS 3DEP (fetched). Fill mode: fill_flat={fill_flat}. Basin "
        f"discrimination floors: min_lake_depth_m={min_lake_depth_m}, "
        f"min_lake_area_m2={min_lake_area_m2} - deterministic engine settings, "
        "not synthetic data."
    )
    _review = await gate_input_review(
        tool_name="landlab_lake_mapping",
        mode=input_mode,
        entries=[],
        params={"fill_flat": fill_flat},
    )
    if _review.cancelled:
        return {
            "status": "error",
            "error_code": "USER_INPUT_CANCELLED",
            "error_message": f"landlab_lake_mapping {_review.cancel_reason}",
        }
    fill_flat = bool(_review.params.get("fill_flat", fill_flat))

    try:
        run_args = LandlabRunArgs(
            bbox=tuple(coerced),  # type: ignore[arg-type]
            analysis="lake_mapping",
            target_resolution_m=float(target_resolution_m),
            fill_flat=bool(fill_flat),
            min_lake_depth_m=float(min_lake_depth_m),
            min_lake_area_m2=float(min_lake_area_m2),
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "error",
            "error_code": "LANDLAB_PARAMS_INVALID",
            "error_message": f"invalid Landlab lake-mapping arguments: {exc}",
        }

    logger.info(
        "landlab_lake_mapping bbox=%s fill_flat=%s res=%.1fm",
        run_args.bbox,
        fill_flat,
        run_args.target_resolution_m,
    )

    try:
        primary = await model_landlab_lake_mapping(
            run_args, compute_class=compute_class, source_note=source_note
        )
        logger.info(
            "landlab_lake_mapping complete layer_id=%s n_lakes=%d (raw=%s kept=%s) "
            "area=%.4g km2 max_depth=%.3f m uri=%s",
            primary.layer_id,
            primary.n_lakes,
            primary.n_lakes_raw,
            primary.n_lakes_kept,
            primary.total_lake_area_km2,
            primary.max_lake_depth_m,
            primary.uri,
        )
        return primary
    except asyncio.CancelledError:
        raise
    except (
        LandlabWorkflowError,
        PostprocessLandlabError,
        LandslideWorkflowError,
        LakeMappingWorkflowError,
    ) as exc:
        logger.warning(
            "landlab_lake_mapping failed: %s (%s)",
            getattr(exc, "error_code", "?"),
            exc,
        )
        return {
            "status": "error",
            "error_code": getattr(exc, "error_code", "LANDLAB_INTERNAL_ERROR"),
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("landlab_lake_mapping unexpected failure")
        return {
            "status": "error",
            "error_code": "LANDLAB_INTERNAL_ERROR",
            "error_message": str(exc),
        }


async def model_landlab_lake_mapping(
    run_args: LandlabRunArgs,
    *,
    dem_path: str | None = None,
    run_id: str | None = None,
    compute_class: str = "standard",
    source_note: str | None = None,
    synthetic_inputs: list[SyntheticInput] | None = None,
) -> LandlabLakeMappingLayerURI:
    """Compose the lake extent + depth chain end-to-end (OFF-BOX lane).

    Returns the lake-depth ``LandlabLakeMappingLayerURI``; emits the lake-extent
    vector as a side effect on the bound emitter.
    """
    emitter = current_emitter()
    solve = await stage_solve_download(
        run_args, dem_path=dem_path, run_id=run_id, compute_class=compute_class
    )
    extent_cog = (solve.secondary_cogs or {}).get("lake_extent")

    try:
        async with substep(current_emitter(), "postprocess_landlab"):
            layers, _metrics = await asyncio.to_thread(
                postprocess_landlab_lake_mapping,
                solve.local_field,
                run_id=solve.run_id,
                result=solve.result_block,
                extent_cog_path=extent_cog,
            )
    finally:
        cleanup_solve(solve)

    if not layers:
        raise LakeMappingWorkflowError(
            "LANDLAB_NO_LAYERS",
            "postprocess_landlab_lake_mapping produced no lake-depth layer",
        )

    raw_primary = layers[0]
    context_layers = layers[1:]

    async with substep(current_emitter(), "publish_layer"):
        primary = await asyncio.to_thread(
            publish_raster_layer, raw_primary, default_style=LAKE_DEPTH_STYLE_PRESET
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
                logger.debug("could not add lake-extent vector: %s", exc)

    await emit_zoom_to(emitter, solve.bbox)
    return primary
