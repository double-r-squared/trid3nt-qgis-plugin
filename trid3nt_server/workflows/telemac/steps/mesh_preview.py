"""The fast mesh-only preview behind the approve-mesh gate - no solve.

Runs the same reach front the plan runs (geocode -> flowline -> mid-reach seed),
stages a ``mesh_only`` manifest, and runs the fast container (gmsh, no DEM, no
solve) so every number on the card comes from the ACTUAL mesh. Reusing the plan's
own steps is what makes the approved solve reproduce the previewed mesh: there is
one seed derivation, not a mirror of one that can drift from it.

Raises on any failure - the gate caller fails OPEN (card skipped, the tool runs
and raises its own typed errors on the same fault).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from trid3nt_contracts import new_ulid

from trid3nt_server.workflows.lib import Domain
from trid3nt_server.workflows.lib.domain import bind_domain, reset_domain

from .deck import normalize_bank_source, stage_manifest
from .errors import TelemacDyeScenarioError
from .reach import (
    MESH_NODE_CAP,
    SOLVE_TIME_BUDGET_S,
    coerce_lonlat_point,
    estimate_telemac_solve_seconds,
    fetch_reach_flowline,
    geocode_reach,
    reach_seed,
    suggest_mesh_size_m,
    suggest_time_step_s,
)
from .solve import raise_if_banks_unavailable, raise_if_reach_degenerate, read_run_metrics

logger = logging.getLogger("trid3nt_server.workflows.telemac.steps.mesh_preview")

__all__ = ["preview_telemac_mesh"]

#: Defaults the preview falls back to when the RAW pre-dispatch params omit a knob.
#: They mirror the workflow's declared scenario defaults; the preview sees args
#: before any door has run, so it cannot read the resolved sheet.
_DEFAULT_REACH_KM, _DEFAULT_WIDTH_M, _DEFAULT_SIM_S = 6.0, 60.0, 3600.0

#: A healthy mesh-only run is ~10-40 s; this bounds a hung gmsh so a broken
#: preview cannot park the turn before the gate falls open.
_PREVIEW_TIMEOUT_S = 240


def _float_or(value: Any, default: float, lo: float, hi: float) -> float:
    try:
        out = float(value) if value is not None else default
    except (TypeError, ValueError):
        out = default
    return min(max(out, lo), hi)


async def preview_telemac_mesh(params: dict[str, Any], *,
                               emitter: Any = None) -> dict[str, Any]:
    """Build (only) the mesh and return the REAL gate stats.

    Emits the triangle wireframe as a role="input" layer with a zoom-to BEFORE
    the card appears, so the user approves a mesh they can see.
    """
    from trid3nt_contracts.execution import LayerURI

    from trid3nt_server.workflows.solver.solver import (
        _get_runs_bucket,
        run_solver,
        wait_for_completion,
    )
    from trid3nt_server.tools.tool_arg_normalizer import coerce_bbox_value
    from trid3nt_server.emission.layer_uri_emit import publish_input_layer
    from trid3nt_server.emission.pipeline_emitter import current_emitter
    from trid3nt_server.workflows.telemac.run_telemac import TELEMAC_SOLVER_NAME

    location = params.get("location")
    raw_bbox = params.get("bbox")
    coerced_bbox = None
    if raw_bbox is not None:
        cb = coerce_bbox_value(raw_bbox)
        if cb is not None:
            coerced_bbox = tuple(cb)
        elif isinstance(raw_bbox, str) and any(c.isalpha() for c in raw_bbox) \
                and not (location and str(location).strip()):
            location = raw_bbox  # a place name in the bbox field
    if location and str(location).strip():
        coerced_bbox = None  # location wins, exactly as the tool resolves it
    elif coerced_bbox is None:
        raise ValueError("preview_telemac_mesh: no location/bbox in params")

    # The window is the tool's declared reach_length_km bound. A narrower one here
    # would preview a DIFFERENT reach than the approved solve builds.
    reach_length_km = _float_or(params.get("reach_length_km"), _DEFAULT_REACH_KM,
                                0.5, 15.0)
    channel_width_m = _float_or(params.get("channel_width_m"), _DEFAULT_WIDTH_M,
                                10.0, 1500.0)
    sim_duration_s = _float_or(params.get("sim_duration_s"), _DEFAULT_SIM_S,
                               600.0, 14400.0)
    override_m = params.get("mesh_resolution_m")

    # Pre-gate params are RAW model args, and this builder's contract is to fail
    # OPEN, so a malformed release point is treated as absent here and REFUSED by
    # the tool itself a moment later.
    try:
        release_pair = coerce_lonlat_point(
            [params.get("release_lon"), params.get("release_lat")]
            if params.get("release_lon") is not None
            or params.get("release_lat") is not None else None)
    except TelemacDyeScenarioError as exc:
        logger.info("telemac preview: release point unusable (%s) - previewing "
                    "without it; the tool refuses it typed", exc)
        release_pair = None

    reach = await geocode_reach(location=location, bbox=coerced_bbox)
    token = bind_domain(Domain(bbox=reach["bbox"], label=reach["name"]))
    try:
        rivers = await fetch_reach_flowline(
            prefetched=params.get("river_geometry_uri"))
        seed = await reach_seed(reach=reach, rivers=rivers)
    finally:
        reset_domain(token)

    mesh_size_m, mesh_node_estimate, mesh_resolution_label = suggest_mesh_size_m(
        reach_length_km=reach_length_km, channel_width_m=channel_width_m,
        resolution=str(params.get("mesh_resolution") or "auto"),
        override_m=float(override_m) if override_m else None)
    time_step_s = suggest_time_step_s(mesh_size_m)
    deck: dict[str, Any] = {
        "name": reach["slug"],
        "seed_lon": round(seed["lon"], 6),
        "seed_lat": round(seed["lat"], 6),
        **({"river_name": reach["river_name"]} if reach.get("river_name") else {}),
        # Call-provided release coords seed the worker's centerline here AND in the
        # approved solve, so the reach the user approves is the reach that solves.
        **({"release_lon": round(release_pair[0], 6),
            "release_lat": round(release_pair[1], 6),
            "seed_from_release": True} if release_pair is not None else {}),
        "nav_direction": "DM",
        "distance_km": reach_length_km,
        "channel_width_m": channel_width_m,
        "bank_source": normalize_bank_source(params.get("bank_source")),
        "mesh_size_m": mesh_size_m,
        "time_step_s": time_step_s,
    }

    # The budget floor estimates nodes from the STATED width, but real-bank meshing
    # follows the MEASURED river - a reach stated at 150 m and really 1400 m wide
    # previewed 295k nodes against the cap. After the first build, if the measured
    # node count blows either budget, re-derive h from the measured density
    # (nodes ~ 1/h^2) and rebuild ONCE at the honest edge length.
    for attempt in (1, 2):
        run_tag = new_ulid()
        manifest_uri = await asyncio.to_thread(stage_manifest, deck, run_tag,
                                               mesh_only=True)
        logger.info("preview_telemac_mesh dispatch run_tag=%s seed=(%.5f,%.5f) "
                    "h=%.3g dt=%.3g", run_tag, seed["lon"], seed["lat"],
                    mesh_size_m, time_step_s)
        handle = run_solver(solver=TELEMAC_SOLVER_NAME,
                            model_setup_uri=manifest_uri, compute_class="small")
        run_result = await wait_for_completion(handle, poll_interval_s=3,
                                               timeout_s=_PREVIEW_TIMEOUT_S)
        mesh_run_id = getattr(run_result, "run_id", None) or handle.run_id
        if run_result is None or run_result.status != "complete":
            # A nhd_area preview with no NHDArea coverage surfaces the typed,
            # retryable banks gate at the approve-mesh surface too, not only after
            # the (fail-open) solve.
            metrics = await asyncio.to_thread(read_run_metrics, mesh_run_id)
            raise_if_banks_unavailable(metrics)
            raise_if_reach_degenerate(metrics)
            raise TelemacDyeScenarioError(
                "TELEMAC_MESH_BUILD_FAILED",
                "mesh-only preview run did not complete "
                f"(status={getattr(run_result, 'status', None)}).")

        m = await asyncio.to_thread(_read_mesh_metrics, mesh_run_id)
        npoin, nelem = int(m.get("npoin") or 0), int(m.get("nelem") or 0)
        bbox4326 = m.get("bbox4326")
        if npoin <= 0:
            raise TelemacDyeScenarioError(
                "TELEMAC_MESH_BUILD_FAILED",
                f"mesh-only preview metrics carry no node count (run {mesh_run_id}).")
        if attempt == 1:
            h_needed = _honest_edge_length(mesh_size_m, npoin, sim_duration_s)
            if h_needed > mesh_size_m * 1.05:
                logger.warning("preview_telemac_mesh: measured %d nodes at h=%.3g "
                               "breaks the budget (cap %d nodes / %ds solve) - "
                               "rebuilding once at h=%.3g", npoin, mesh_size_m,
                               MESH_NODE_CAP, int(SOLVE_TIME_BUDGET_S), h_needed)
                mesh_size_m = round(h_needed, 1)
                time_step_s = suggest_time_step_s(mesh_size_m)
                deck["mesh_size_m"], deck["time_step_s"] = mesh_size_m, time_step_s
                continue
        break

    # current_emitter() is NOT bound in the pre-dispatch gate context - the server
    # passes state.emitter in.
    if emitter is None:
        emitter = current_emitter()
    preview_layer = LayerURI(
        layer_id=f"telemac-mesh-preview-{mesh_run_id}",
        name=f"Mesh preview ({mesh_size_m:g} m edges, {npoin:,} nodes)",
        layer_type="vector",
        uri=f"s3://{_get_runs_bucket()}/{mesh_run_id}/mesh_preview.geojson",
        style_preset="nhdplus_flowlines",  # a known line preset -> sane wireframe
        role="input",
        bbox=tuple(bbox4326) if bbox4326 else None)
    emitted = await publish_input_layer(emitter, preview_layer)
    logger.info("preview_telemac_mesh wireframe emit: emitter=%s emitted=%s layer=%s",
                "bound" if emitter is not None else "NONE", emitted,
                preview_layer.layer_id)
    if emitter is not None and bbox4326:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(bbox4326)})
        except Exception as exc:  # noqa: BLE001 -- preview zoom is best-effort
            logger.warning("preview_telemac_mesh zoom-to failed: %s", exc)

    return {
        "run_id": mesh_run_id,
        "mesh_size_m": float(mesh_size_m),
        "time_step_s": float(time_step_s),
        "npoin": npoin,
        "nelem": nelem,
        "edge_mean_m": m.get("edge_mean_m"),
        "est_solve_seconds": estimate_telemac_solve_seconds(
            npoin, sim_duration_s, time_step_s),
        "resolution_label": mesh_resolution_label,
        "node_estimate": mesh_node_estimate,
        "location_name": reach["name"],
        "bbox": bbox4326,
        "wireframe_capped": bool(m.get("wireframe_capped")),
        "bank_source": str(m.get("bank_source") or "constant_ribbon"),
        "bank_width_mean_m": m.get("bank_width_mean_m"),
    }


def _read_mesh_metrics(run_id: str) -> dict[str, Any]:
    from trid3nt_server.workflows.solver.solver import (
        _get_runs_bucket,
        _get_s3_client,
    )

    obj = _get_s3_client().get_object(Bucket=_get_runs_bucket(),
                                      Key=f"{run_id}/telemac_metrics.json")
    loaded = json.loads(obj["Body"].read().decode("utf-8"))
    return loaded if isinstance(loaded, dict) else {}


def _honest_edge_length(mesh_size_m: float, npoin: int, sim_duration_s: float) -> float:
    """The coarsest h that respects BOTH the node cap and the wall-clock target."""
    h = mesh_size_m
    if npoin > MESH_NODE_CAP * 1.15:
        h = max(h, mesh_size_m * (npoin / MESH_NODE_CAP) ** 0.5)
    for _ in range(4):  # dt(h) is piecewise; a few passes converge
        n_pred = npoin * (mesh_size_m / h) ** 2
        est = estimate_telemac_solve_seconds(int(n_pred), sim_duration_s,
                                             suggest_time_step_s(h))
        if est <= SOLVE_TIME_BUDGET_S:
            break
        h *= (est / SOLVE_TIME_BUDGET_S) ** 0.5
    return h
