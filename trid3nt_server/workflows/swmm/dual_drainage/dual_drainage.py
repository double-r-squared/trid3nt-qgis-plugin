"""Engine template ``swmm_dual_drainage_coupling`` - couple the overland MAJOR
system with an imported piped MINOR system at inlets (the defining dual-drainage
feature; the practice-verification's "both halves").

Builds the DEM-synthesized quasi-2D overland mesh (``swmm_urban_flood`` machinery)
AND imports the real piped storm-drain network (``swmm_network_import`` machinery),
then merges them into ONE SWMM deck where each pipe junction exchanges flow with
the overland cell it sits in via an INLET orifice: surface flow drops into the
pipe, and a surcharging pipe backs water up onto the street. It returns the
OVERLAND peak-depth raster (street flooding) as the primary, the pipe network as
a context overlay coloured by surcharge/flooding, and the coupled scalars from
BOTH systems.

Determinism (invariant 1): every number narrated comes from the typed
``SWMMDualDrainageLayerURI`` scalars the postprocess/report computed. The DEM +
pipe network are REAL; the coupling inlets + per-junction sub-areas are LABELED
demo defaults (the labeled-degrade doctrine).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from pathlib import Path
from typing import Any

from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.swmm_contracts import SWMMDualDrainageLayerURI, SWMMRunArgs
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.data.tool_arg_normalizer import coerce_bbox_value
from trid3nt_server.data import register_tool
from trid3nt_server.mesh.swmm_network import (
    SWMMNetworkError,
    build_dual_drainage_inp,
    dual_drainage_network_to_geojson_4326,
    parse_network_features,
    read_network_response,
)
from trid3nt_server.workflows.swmm._template_card import TemplateCard
from trid3nt_server.emission.pipeline_emitter import (
    begin_substeps,
    current_emitter,
    substep,
)

logger = logging.getLogger(
    "trid3nt_server.workflows.swmm.dual_drainage.dual_drainage"
)

__all__ = ["swmm_dual_drainage_coupling", "model_swmm_dual_drainage"]


TEMPLATE_CARD = TemplateCard(
    question=(
        "COUPLED dual-drainage urban flood: the overland surface mesh EXCHANGES "
        "flow with an imported piped storm-drain network at inlets/catchbasins "
        "(surface drains into the pipes; a surcharging pipe floods the street back "
        "- the defining dual-drainage feature)"
    ),
    required_inputs=["bbox", "nodes_uri OR nodes_geojson (the pipe network)"],
    knobs=(
        "conduits_uri, nodes_geojson, conduits_geojson, return_period_yr, "
        "total_rain_depth_mm, storm_duration_hr, target_resolution_m, "
        "building_representation, inlet_opening_m"
    ),
)


_METADATA = AtomicToolMetadata(
    name="swmm_dual_drainage_coupling",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="swmm",
    tier="template",
)


@register_tool(
    _METADATA,
    read_only_hint=False,
    open_world_hint=True,
    destructive_hint=False,
    idempotent_hint=False,
)
async def swmm_dual_drainage_coupling(
    bbox: tuple[float, float, float, float] | list[float] | str | None = None,
    nodes_uri: str | None = None,
    conduits_uri: str | None = None,
    nodes_geojson: dict[str, Any] | None = None,
    conduits_geojson: dict[str, Any] | None = None,
    return_period_yr: int = 10,
    total_rain_depth_mm: float | None = None,
    storm_duration_hr: float = 2.0,
    rain_interval_min: int = 5,
    target_resolution_m: float = 10.0,
    building_representation: str = "drop",
    inlet_opening_m: float = 0.6,
    compute_class: str = "standard",
    input_mode: str | None = None,
    **_extra_ignored: Any,
) -> SWMMDualDrainageLayerURI | dict[str, Any]:
    """Run a COUPLED dual-drainage urban flood: overland surface mesh + imported piped sewer, exchanging flow at inlets.

    Fidelity: SWMM dynamic-wave routing of a quasi-2D overland mesh COUPLED to a
    real imported pipe network via inlet orifices - a planning-grade dual-drainage
    model (the practice-standard "major + minor system"), not a calibrated master
    plan.
    Data: the DEM + the pipe-network geometry are REAL. The inlet-capture openings,
    per-cell overland roughness, and any gap-filled pipe inverts are LABELED demo
    defaults. Off-scope: surface-only pluvial (no pipe network) -> swmm_urban_flood;
    import + solve a pipe network alone -> swmm_network_import.

    Use this when: the user wants BOTH the street/overland flooding AND the storm
    sewer response together - dual drainage, minor+major system, "does the pipe
    network relieve or worsen the surface flooding", catchbasin/inlet capture,
    pipe surcharge pushing water back onto the street.

    Params:
        bbox: AOI (min_lon,min_lat,max_lon,max_lat) EPSG:4326 - a city block /
            neighbourhood overlapping the pipe network.
        nodes_uri / conduits_uri: the pipe-network node + conduit layers (same
            accepted forms as swmm_network_import: s3:// / file:// / https geojson /
            ArcGIS FeatureServer layer). A single combined nodes_uri is split by
            geometry.
        nodes_geojson / conduits_geojson: inline GeoJSON alternative.
        return_period_yr / total_rain_depth_mm / storm_duration_hr /
            rain_interval_min: the Atlas-14 design storm (as swmm_urban_flood).
        target_resolution_m: overland cell size, m (adaptive-mesh budget may
            coarsen a large AOI).
        building_representation: "drop" (default) / "raise" / "roughness".
        inlet_opening_m: catchbasin/inlet capture opening, m (default 0.6) - the
            labeled surface<->sewer coupling size.
        compute_class: compute class (default "standard").
        input_mode: run-mode lever.

    Returns:
        On success: ``SWMMDualDrainageLayerURI`` - the overland peak-depth raster
        (with max_depth_m / flooded_area_km2 / n_buildings_affected) PLUS the
        coupled minor-system scalars (n_pipe_junctions / n_pipe_conduits / n_inlets
        / pipe_peak_outfall_flow_cms / n_pipe_flooded_nodes / n_pipe_surcharged_conduits).
        The pipe network vector is emitted alongside as context.
        On failure: {"status":"error","error_code","error_message"}.
    """
    if bbox is None:
        return {"status": "error", "error_code": "SWMM_PARAMS_INCOMPLETE",
                "error_message": "swmm_dual_drainage_coupling requires a bbox."}
    if not any([nodes_uri, nodes_geojson]):
        return {"status": "error", "error_code": "SWMM_NETWORK_PARAMS_INCOMPLETE",
                "error_message": ("swmm_dual_drainage_coupling requires a pipe network: "
                                  "nodes_uri (+ conduits_uri) or nodes_geojson.")}
    coerced = coerce_bbox_value(bbox)
    if coerced is None:
        return {"status": "error", "error_code": "SWMM_PARAMS_INVALID",
                "error_message": f"invalid bbox: {bbox!r}"}

    try:
        run_args = SWMMRunArgs(
            bbox=tuple(coerced),  # type: ignore[arg-type]
            return_period_yr=int(return_period_yr),
            total_rain_depth_mm=(float(total_rain_depth_mm) if total_rain_depth_mm else None),
            storm_duration_hr=float(storm_duration_hr),
            rain_interval_min=int(rain_interval_min),
            target_resolution_m=float(target_resolution_m),
            building_representation=building_representation,
        )
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error_code": "SWMM_PARAMS_INVALID",
                "error_message": f"invalid run arguments: {exc}"}

    try:
        result = await model_swmm_dual_drainage(
            run_args,
            nodes_uri=nodes_uri, conduits_uri=conduits_uri,
            nodes_geojson=nodes_geojson, conduits_geojson=conduits_geojson,
            inlet_opening_m=float(inlet_opening_m),
            compute_class=compute_class, input_mode=input_mode,
        )
        logger.info(
            "swmm_dual_drainage_coupling complete layer_id=%s max_depth=%.4g "
            "pipe_junc=%d inlets=%d pipe_peak=%.4g uri=%s",
            result.layer_id, result.max_depth_m, result.n_pipe_junctions,
            result.n_inlets, result.pipe_peak_outfall_flow_cms, result.uri,
        )
        return result
    except asyncio.CancelledError:
        raise
    except SWMMNetworkError as exc:
        return {"status": "error", "error_code": exc.error_code, "error_message": str(exc)}
    except Exception as exc:  # noqa: BLE001
        code = getattr(exc, "error_code", "SWMM_DUAL_DRAINAGE_INTERNAL_ERROR")
        logger.exception("swmm_dual_drainage_coupling failure")
        return {"status": "error", "error_code": code, "error_message": str(exc)}


# --------------------------------------------------------------------------- #
# The composer.
# --------------------------------------------------------------------------- #
async def model_swmm_dual_drainage(
    run_args: SWMMRunArgs,
    *,
    nodes_uri: str | None = None,
    conduits_uri: str | None = None,
    nodes_geojson: dict[str, Any] | None = None,
    conduits_geojson: dict[str, Any] | None = None,
    dem_path: str | None = None,
    inlet_opening_m: float = 0.6,
    compute_class: str = "standard",
    input_mode: str | None = None,
    run_id: str | None = None,
) -> SWMMDualDrainageLayerURI:
    """Compose the coupled dual-drainage chain: overland mesh + imported pipes ->
    one deck, solve, publish overland depth + pipe overlay."""
    from trid3nt_server.data.simulation.solver.solver import new_ulid
    from trid3nt_server.workflows.swmm.run_swmm import (
        build_and_stage_swmm_deck, run_swmm_local, SWMMStaging,
    )
    from trid3nt_server.workflows.swmm.postprocess_swmm import postprocess_swmm
    from trid3nt_server.workflows.swmm.urban_flood.urban_flood import (
        _atlas14_total_depth_mm, _enforce_min_urban_aoi, _fetch_buildings_for_urban,
        _fetch_dem_for_urban, _publish_peak_layer,
    )
    from trid3nt_server.workflows.swmm.network_import.network_import import (
        _resolve_network_layers,
    )

    emitter = current_emitter()
    rid = run_id or new_ulid()
    bbox = _enforce_min_urban_aoi(tuple(run_args.bbox))
    if emitter is not None:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(bbox)})
        except Exception:  # noqa: BLE001
            pass

    begin_substeps(emitter, 7 if dem_path is None else 6)

    # --- Step 1: DEM ---
    if dem_path is None:
        async with substep(emitter, "fetch_dem"):
            dem_path, dem_source = await asyncio.to_thread(
                _fetch_dem_for_urban, bbox)
    else:
        dem_source = "supplied"

    # --- Step 2: buildings (optional) ---
    async with substep(emitter, "fetch_buildings"):
        building_footprints = await asyncio.to_thread(_fetch_buildings_for_urban, bbox)

    # --- Step 3: precip ---
    depth_mm = run_args.total_rain_depth_mm
    if depth_mm is None:
        async with substep(emitter, "lookup_precip_return_period"):
            depth_mm = await asyncio.to_thread(
                _atlas14_total_depth_mm, bbox, run_args.return_period_yr, run_args.storm_duration_hr
            )
        if depth_mm is None:
            raise SWMMNetworkError(
                "SWMM_PRECIP_LOOKUP_FAILED",
                message=("Atlas-14 design-storm lookup failed for this AOI; retry with "
                         "an explicit total_rain_depth_mm."),
            )
    eff_args = run_args.model_copy(update={"total_rain_depth_mm": float(depth_mm)})

    # --- Step 4: build overland mesh + load/parse pipe network + couple ---
    async with substep(emitter, "build_dual_drainage_deck"):
        staging, dd, combined_staging, network_source = await asyncio.to_thread(
            _build_coupled_deck, eff_args, dem_path, building_footprints,
            nodes_uri, conduits_uri, nodes_geojson, conduits_geojson,
            inlet_opening_m, rid,
        )

    # --- Step 5: solve the combined deck (reuses run_swmm_local isolation) ---
    async with substep(emitter, "solve_dual_drainage"):
        run = await asyncio.to_thread(run_swmm_local, combined_staging)

    # --- Step 6: overland postprocess (depth COGs) ---
    async with substep(emitter, "postprocess_swmm"):
        layers, metrics = await asyncio.to_thread(
            postprocess_swmm, run, combined_staging.build, run_id=rid,
            building_footprints=building_footprints,
        )
    if not layers:
        raise SWMMNetworkError("SWMM_NO_LAYERS", message="coupled solve produced no depth layer")
    raw_peak = layers[0]

    # --- Step 7: publish overland peak + read the pipe response + emit overlay ---
    async with substep(emitter, "publish_layer"):
        peak = await asyncio.to_thread(_publish_peak_layer, raw_peak, rid)

    # --- emit-on-solve seam frames (ADR 0282): the depth animation group -------
    # postprocess_swmm wrote the peak + every per-frame depth COG to outputs.json
    # host-side; the seam (frames_only) builds the CONTEXT frame layers (the peak
    # stays the composer-built typed layer). Absent outputs.json -> no frames (an
    # honest peak-only degrade). Reuses the urban_flood publish+emit chokepoint.
    from trid3nt_server.workflows.swmm.urban_flood.urban_flood import (
        _emit_frame_layers,
        _read_swmm_frame_layers,
    )

    _dd_frames = await asyncio.to_thread(_read_swmm_frame_layers, rid, bbox)
    await _emit_frame_layers(emitter, _dd_frames, rid)

    resp = read_network_response(
        run.rpt_path, node_filter=set(dd.pipe_node_coords),
        conduit_filter={e[0] for e in dd.pipe_conduit_endpoints},
        outfall_filter=set(dd.pipe_outfall_names),
    )

    # pipe network overlay (context vector)
    pipe_layer = await asyncio.to_thread(_publish_pipe_overlay, dd, resp, rid)
    if emitter is not None and pipe_layer is not None:
        try:
            await emitter.add_loaded_layer(pipe_layer)
        except Exception as exc:  # noqa: BLE001
            logger.warning("dual_drainage: pipe overlay emit failed: %s", exc)

    # assemble the coupled primary (overland depth + minor-system scalars)
    result = SWMMDualDrainageLayerURI(
        **{k: getattr(peak, k) for k in (
            "layer_id", "name", "layer_type", "uri", "style_preset", "role")},
        bbox=tuple(bbox),
        max_depth_m=float(getattr(peak, "max_depth_m", 0.0)),
        flooded_area_km2=float(getattr(peak, "flooded_area_km2", 0.0)),
        n_buildings_affected=int(getattr(peak, "n_buildings_affected", 0)),
        fallback_note=(
            f"Coupled dual drainage: overland mesh ({dd.n_surface_cells} cells) + "
            f"imported {network_source} ({dd.n_pipe_junctions} junctions, "
            f"{dd.n_pipe_conduits} conduits) exchanging flow at {dd.n_inlets} inlets."
        ),
        synthetic_inputs=[
            SyntheticInput(param="pipe_network_geometry", value="real", basis="fetched",
                           real_source_if_any=network_source,
                           note="imported storm-drain nodes + conduits (minor system)"),
            SyntheticInput(param="inlet_capture", value=f"{inlet_opening_m} m opening",
                           basis="default_demo", consequence="physics",
                           note="fixed inlet orifice; real catchbasins carry a capture curve"),
            SyntheticInput(param="total_rain_depth_mm", value=round(float(depth_mm), 1),
                           units="mm", basis="fetched",
                           note=f"{run_args.return_period_yr}-yr/{run_args.storm_duration_hr:.0f}-hr storm"),
        ],
        n_pipe_junctions=dd.n_pipe_junctions,
        n_pipe_conduits=dd.n_pipe_conduits,
        n_pipe_outfalls=dd.n_pipe_outfalls,
        n_inlets=dd.n_inlets,
        pipe_peak_outfall_flow_cms=resp["peak_outfall_flow_cms"],
        n_pipe_flooded_nodes=len(resp["flooded_nodes"]),
        n_pipe_surcharged_conduits=len(resp["surcharged_conduits"]),
        n_inverts_filled=dd.n_inverts_filled,
        n_topology_snapped=dd.n_topology_snapped,
    )
    if emitter is not None:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(bbox)})
        except Exception:  # noqa: BLE001
            pass
    return result


def _build_coupled_deck(
    eff_args, dem_path, building_footprints, nodes_uri, conduits_uri,
    nodes_geojson, conduits_geojson, inlet_opening_m, rid,
):
    """Sync: build overland mesh, load+parse pipes, merge into a coupled deck."""
    from trid3nt_server.workflows.swmm.run_swmm import (
        build_and_stage_swmm_deck, SWMMStaging,
    )
    from trid3nt_server.workflows.swmm.network_import.network_import import (
        _resolve_network_layers,
    )

    staging = build_and_stage_swmm_deck(
        eff_args, dem_path=dem_path, building_footprints=building_footprints,
        run_id=rid, enable_autoscale=True,
    )
    nodes_fc, conduits_fc, network_source = _resolve_network_layers(
        nodes_uri, conduits_uri, nodes_geojson, conduits_geojson
    )
    parsed = parse_network_features(nodes_fc, conduits_fc, dem_path=dem_path)
    combined_inp = str(Path(staging.inp_path).with_name("coupled.inp"))
    dd = build_dual_drainage_inp(
        staging.build, parsed, out_inp_path=combined_inp,
        inlet_opening_m=float(inlet_opening_m),
    )
    # repoint the staging build at the combined deck so run_swmm_local solves it.
    combined_build = dataclasses.replace(staging.build, inp_path=combined_inp)
    combined_staging = SWMMStaging(
        run_id=staging.run_id, inp_path=combined_inp, build=combined_build,
        run_args=eff_args, building_footprints=building_footprints,
    )
    return staging, dd, combined_staging, network_source


def _publish_pipe_overlay(dd, resp, rid) -> LayerURI | None:
    from trid3nt_server.data.simulation.solver.solver import _get_runs_bucket, _get_s3_client

    fc = dual_drainage_network_to_geojson_4326(dd, resp)
    if not fc.get("features"):
        return None
    try:
        bucket = _get_runs_bucket()
        key = f"{rid}/pipe_network.geojson"
        _get_s3_client().put_object(
            Bucket=bucket, Key=key, Body=json.dumps(fc).encode("utf-8"),
            ContentType="application/geo+json",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("dual_drainage: pipe overlay upload failed: %s", exc)
        return None
    return LayerURI(
        layer_id=f"swmm-pipe-network-{rid}",
        name=f"Storm-drain network ({dd.n_pipe_junctions} junctions, {dd.n_inlets} inlets)",
        layer_type="vector", uri=f"s3://{bucket}/{key}",
        style_preset="swmm_network", role="context", bbox=None,
    )
