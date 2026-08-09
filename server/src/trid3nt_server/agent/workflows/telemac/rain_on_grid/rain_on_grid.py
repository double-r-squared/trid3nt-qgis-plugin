"""Engine template ``telemac_rain_on_grid`` -- TELEMAC-2D rainfall-runoff on a
delineated watershed (ADR 0196; SCS-CN infiltration, ADR 0195).

The LLM-facing exposure of the TELEMAC-2D rain-on-grid engine: a design storm
falls on a REAL delineated catchment (not a river reach), infiltrates by the SCS
curve-number method (per-node CN2 from NLCD land cover, antecedent-moisture
knob), and the excess runs off overland to the pour point -- producing an OUTLET
HYDROGRAPH + a max flood-depth map. This is the flash-flood / rainfall-runoff
sibling of the surface-transport ``telemac_river_dye`` and the coastal
``sfincs_flood``.

Pipeline (deterministic, composed here):
  1. resolve the AOI + pour point (geocode a place, or an explicit bbox + point);
  2. ``acquire_watershed_mesh`` (ADR 0196 Decision 1) -- delineate the catchment,
     mesh its interior refined by distance-to-river, project to UTM, write the
     BOTTOM SELAFIN (a user mesh slots in via ``use_supplied_mesh``);
  3. ``fetch_landcover`` (NLCD) sampled at the mesh nodes -> per-node CN2 +
     Manning fields (``cn_infiltration`` Table-1 analog; the ``curve_number`` knob
     overrides CN uniformly; the steep-slope Huang correction is baked in here
     because the engine branch is compiled off);
  4. ``select_runoff_path`` -- constant design storm -> the NATIVE SCS-CN model
     (RAINFALL-RUNOFF MODEL=1 + FORMATTED DATA FILE 2 CN2 map + AMC); a real
     MRMS/AORC ``mrms_window`` -> the NATIVE TIME-VARYING path (ADR 0206): the
     gross hourly hyetograph drives the SCS-CN per-timestep via a per-case
     RAINDEF=3 FORTRAN FILE (FORMATTED DATA FILE 1), resolving the hydrograph
     SHAPE the constant-rain build could not (no engine rebuild);
  5. stage the mesh + node fields + manifest and dispatch the generic
     ``run_solver`` seam (mode=rain_on_grid -> the worker's ``rog_build`` deck);
  6. ``postprocess_telemac_wse`` rasterizes the peak WATER DEPTH to a COG; the
     outlet hydrograph + runoff volume + continuity ride in the metrics;
  7. the full-results SELAFIN (``r2d_rog.slf`` -- all frames, all variables) is
     published as a ``layer_type="mesh"`` case layer alongside the depth COG (ADR
     0208), so QGIS/MDAL animates it natively with the temporal controller.

Registered ``engine="telemac", tier="template"``, ``cacheable=False`` +
``ttl_class="live-no-cache"`` + ``source_class="workflow_dispatch"`` (FR-DC-6,
mirroring ``telemac_river_dye``). TELEMAC is LOCAL-DOCKER / worker-image only, so
the composer always dispatches through ``run_solver``.

Applicability envelope (Godara, Bruland and Alfredsen 2024, Front. Water
6:1384205 -- ADR 0195): rain-on-grid reproduces SINGLE-STORM flash-flood events
(~10-20 h) in small steep catchments. Multi-peak / sustained rain-on-snow is NOT
reproduced (infiltrated water is permanently lost, no subsurface return flow ->
inter-peak baseflow is missed). TELEMAC-2D's triangular mesh is stable on steep
terrain (a paper finding vs HEC-RAS's structured grid). US-only via our fetchers;
Coweeta Creek NC is the US steep gauged replication site.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.agent.tools import register_tool
from trid3nt_server.agent.tool_arg_normalizer import coerce_bbox_value

logger = logging.getLogger(
    "trid3nt_server.agent.workflows.telemac.rain_on_grid.rain_on_grid")

__all__ = ["telemac_rain_on_grid", "TelemacRainOnGridError", "model_telemac_rain_on_grid"]


class TelemacRainOnGridError(RuntimeError):
    """A typed rain-on-grid failure (never a silent dead-end)."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


_TELEMAC_RAIN_ON_GRID_METADATA = AtomicToolMetadata(
    name="telemac_rain_on_grid",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="telemac",
    tier="template",
)


@register_tool(
    _TELEMAC_RAIN_ON_GRID_METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def telemac_rain_on_grid(
    location: str | None = None,
    bbox: tuple[float, float, float, float] | list[float] | str | None = None,
    pour_point: tuple[float, float] | list[float] | str | None = None,
    curve_number: float | None = None,
    antecedent_moisture: str = "normal",
    design_storm_mm_per_hr: float = 25.0,
    storm_duration_hr: float = 6.0,
    sim_duration_hr: float | None = None,
    mrms_window: str | None = None,
    observed_gauge_id: str | None = None,
    mesh_uri: str | None = None,
    compute_class: str = "medium",
    **_extra_ignored: Any,
) -> Any:
    """FLASH FLOOD from RAIN falling on a WATERSHED: rainfall-runoff, an OUTLET HYDROGRAPH + a flood-DEPTH map.

    Fidelity: TELEMAC-2D full shallow-water RAIN-ON-GRID with SCS curve-number
    infiltration (per-node CN from NLCD land cover) on a delineated catchment
    meshed from a real 3DEP DEM; planning-grade single-storm flash-flood demo, not
    a calibrated rainfall-runoff model. Single-storm ~10-20 h events only (no
    baseflow / no snow / no multi-peak return flow).

    THE tool for "how much runoff / peak discharge from a storm over this
    watershed", "rain falls on the catchment and floods the valley", "rainfall-
    runoff hydrograph at the outlet", "flash flood from an intense storm on a
    basin". Rain-fed OVERLAND flow on the whole catchment (NOT a channel dye
    plume -> ``telemac_river_dye``; NOT coastal/pluvial inundation depth ->
    ``sfincs_flood``; NOT urban pipe drainage -> ``swmm_urban_flood``).

    Knobs: ``antecedent_moisture`` ("dry"/"normal"/"wet" = SCS AMC I/II/III) is
    the dominant infiltration lever; ``curve_number`` overrides CN uniformly;
    ``design_storm_mm_per_hr`` + ``storm_duration_hr`` set the constant design
    storm; ``mrms_window`` ("start/end" dates) drives the REAL time-varying
    hourly hyetograph instead (native SCS-CN per-timestep, improved peak SHAPE +
    timing over a constant storm; residual lag is forcing/mesh-bound, and there
    is still no subsurface return flow); ``observed_gauge_id`` wires NSE/R2 vs a
    USGS-NWIS gauge.

    Params:
        location: place naming the catchment (geocoded). Supply this OR ``bbox``.
        bbox: OPTIONAL AOI ``(min_lon,min_lat,max_lon,max_lat)`` EPSG:4326.
        pour_point: OPTIONAL ``(lon, lat)`` catchment outlet; defaults to the AOI
            centroid's lowest snapped stream cell.
        curve_number: uniform CN2 override (else NLCD-distributed).
        antecedent_moisture: "dry" | "normal" | "wet" (SCS AMC I/II/III).
        design_storm_mm_per_hr: constant storm intensity (native SCS-CN path).
        storm_duration_hr: rain-on duration (h).
        sim_duration_hr: total sim length (h); defaults to storm_duration_hr.
        observed_gauge_id: USGS NWIS gauge id for the NSE/R2 overlay.
        mesh_uri: OPTIONAL user-supplied watershed SELAFIN (skips delineation).
    """
    return await model_telemac_rain_on_grid(
        location=location, bbox=bbox, pour_point=pour_point,
        curve_number=curve_number, antecedent_moisture=antecedent_moisture,
        design_storm_mm_per_hr=design_storm_mm_per_hr,
        storm_duration_hr=storm_duration_hr, sim_duration_hr=sim_duration_hr,
        mrms_window=mrms_window, observed_gauge_id=observed_gauge_id,
        mesh_uri=mesh_uri, compute_class=compute_class,
    )


_AMC = {"dry": 1, "normal": 2, "wet": 3, "i": 1, "ii": 2, "iii": 3,
        "1": 1, "2": 2, "3": 3}


async def model_telemac_rain_on_grid(
    *,
    location: str | None,
    bbox: Any,
    pour_point: Any,
    curve_number: float | None,
    antecedent_moisture: str,
    design_storm_mm_per_hr: float,
    storm_duration_hr: float,
    sim_duration_hr: float | None,
    mrms_window: str | None,
    observed_gauge_id: str | None,
    mesh_uri: str | None,
    compute_class: str,
) -> Any:
    """Deterministic rain-on-grid composer (geocode -> mesh -> CN -> solve ->
    depth COG). Inlined here (the ``telemac_river_dye`` analogue)."""
    import asyncio

    from trid3nt_contracts import new_ulid
    from trid3nt_server.agent.tools import TOOL_REGISTRY
    from trid3nt_server.agent.workflows.telemac.rain_on_grid import mesh_acquisition as MA
    from trid3nt_server.agent.workflows.telemac.rain_on_grid.cn_infiltration import (
        select_runoff_path,
    )

    amc = _AMC.get(str(antecedent_moisture).strip().lower(), 2)

    # --- Stage 1: resolve pour point, THEN the analysis AOI ----------------- #
    # The pour point is resolved FIRST: when it is supplied the analysis AOI is
    # derived FROM it (a generous catchment-containing buffer), never from the
    # geocoded place bbox. A place bbox names a TOWN and need not contain the
    # UPSTREAM catchment (ADR 0196 live bug: 'Otto, NC' delineated a 20-cell
    # sliver because the town box clipped the Coweeta catchment mid-hillslope).
    if isinstance(pour_point, str):
        pour_point = coerce_bbox_value(pour_point)
    pp: tuple[float, float] | None = (
        tuple(float(v) for v in pour_point) if pour_point is not None else None)

    aoi = coerce_bbox_value(bbox) if bbox is not None else None
    if aoi is None and pp is not None:
        aoi = _aoi_from_pour_point(pp)
    if aoi is None:
        if not location:
            raise TelemacRainOnGridError(
                "TELEMAC_ROG_NO_AOI",
                "supply a location (geocoded) or an explicit bbox.")
        geo = await asyncio.to_thread(
            TOOL_REGISTRY["geocode_location"].fn, query=location)
        aoi = coerce_bbox_value(getattr(geo, "bbox", None) or geo["bbox"])
    aoi = tuple(float(v) for v in aoi)
    if pp is None:
        pp = ((aoi[0] + aoi[2]) / 2.0, (aoi[1] + aoi[3]) / 2.0)

    run_tag = new_ulid()
    rundir = Path(os.environ.get("TRID3NT_RUNS_DIR", "/tmp")) / f"rog-stage-{run_tag}"
    rundir.mkdir(parents=True, exist_ok=True)

    # --- Stage 2: acquire the watershed mesh -------------------------------- #
    # Provenance line stamped onto the result when a case mesh was consumed or a
    # mismatched one was skipped (ADR 0200 precondition gate).
    mesh_gate_note: str | None = None
    if mesh_uri:
        mesh = MA.use_supplied_mesh(
            mesh_path=mesh_uri, pour_point=pp,
            utm_epsg=int(_guess_utm_epsg(pp)), outlet_lonlat=pp)
    else:
        # Precondition gate (ADR 0200): if this case already holds an engine-
        # compatible mesh (built explicitly by generate_mesh), offer to solve on
        # it instead of delineating a fresh one. Accepted -> the supplied-mesh
        # path; declined/absent/incompatible -> the delineation below, unchanged.
        supplied, mesh_gate_note = await _mesh_precondition_gate(pp, rundir)
        if supplied is not None:
            mesh = supplied
        else:
            # Warm the geo stack in the MAIN thread first: shapely/geopandas have a
            # thread-first-import circular-import race, and acquire_watershed_mesh
            # runs in a worker thread (it is the first geopandas import on a fresh
            # daemon). Importing here forces shapely to fully initialize on the
            # main thread before the offload.
            import geopandas as _gpd  # noqa: F401
            mesh = await asyncio.to_thread(
                MA.acquire_watershed_mesh, pour_point=pp, bbox=aoi,
                output_dir=str(rundir), min_edge_length_m=40.0,
                max_edge_length_m=300.0)

    # --- Stage 3: NLCD -> per-node CN2 + Manning ---------------------------- #
    node_cn2, node_manning = await asyncio.to_thread(
        _sample_node_fields, mesh, aoi, curve_number)

    # --- Stage 4: rain forcing + runoff-path decision (in the envelope) ------ #
    # A real MRMS/AORC window -> the TRUE time-varying hyetograph drives the
    # native SCS-CN per-timestep (ADR 0206 native_hyetograph path). Otherwise the
    # constant design storm is used (the historical native path). The design-storm
    # knobs stay for un-dated / hypothetical storms.
    hyeto_blocks: list | None = None
    if mrms_window:
        hyeto_blocks, hyeto_series, sim_from_hyeto = await asyncio.to_thread(
            _fetch_hyetograph_blocks, aoi, mrms_window,
            float((sim_duration_hr or 0.0)) * 3600.0)
        decision = select_runoff_path(hyetograph_mm=hyeto_series)
        sim_s = sim_from_hyeto
    else:
        decision = select_runoff_path(
            constant_intensity_mm_per_hr=float(design_storm_mm_per_hr))
        sim_s = float((sim_duration_hr or storm_duration_hr)) * 3600.0

    # --- Stage 5: stage inputs + manifest, dispatch run_solver -------------- #
    layer = await _stage_solve_postprocess(
        mesh=mesh, node_cn2=node_cn2, node_manning=node_manning,
        amc=amc, curve_number=curve_number,
        design_storm_mm_per_hr=float(design_storm_mm_per_hr),
        sim_s=sim_s, runoff_path=decision.path, pour_point=pp,
        observed_gauge_id=observed_gauge_id, compute_class=compute_class,
        reach_name=(location or "watershed"), run_tag=run_tag,
        hyetograph_blocks=hyeto_blocks)
    # Stamp the mesh provenance (consumed a case mesh / skipped an incompatible
    # one) onto the result envelope so the assumptions line narrates it honestly.
    if mesh_gate_note and layer is not None:
        try:
            from trid3nt_contracts.common import SyntheticInput
            basis = "user" if mesh.provenance == "user_supplied" else "derived"
            layer.synthetic_inputs = list(layer.synthetic_inputs) + [
                SyntheticInput(param="mesh_domain", value=mesh_gate_note,
                               basis=basis, real_source_if_any="generate_mesh")]
        except Exception:  # noqa: BLE001 -- provenance stamping is never fatal
            logger.debug("mesh-gate note stamp skipped", exc_info=True)
    return layer


async def _mesh_precondition_gate(pp, rundir):
    """Offer this case's mesh to the rain-on-grid solve (ADR 0200).

    Returns ``(WatershedMesh | None, note | None)``: a fully-populated
    ``WatershedMesh`` when a case mesh was discovered, engine-compatible, and
    accepted (auto-default or user-approved); ``None`` when there is no usable
    mesh, an incompatible one was skipped, or the user declined -- the caller then
    delineates a fresh catchment. NEVER raises into the solve path (a discovery /
    staging fault degrades to fresh authoring with a logged warning)."""
    import asyncio

    from trid3nt_server.agent.workflows.mesh.precondition_gate import (
        gate_supplied_mesh, materialize_supplied_mesh,
    )
    from trid3nt_server.agent.workflows.telemac.rain_on_grid import (
        mesh_acquisition as MA,
    )

    try:
        from trid3nt_server.emission.pipeline_emitter import current_emitter
        emitter = current_emitter()
        loaded_mesh_uris = (
            [ly.uri for ly in emitter.loaded_layers
             if getattr(ly, "layer_type", None) == "mesh"]
            if emitter is not None else [])
        s3 = None
        try:
            from trid3nt_server.agent.tools.simulation.solver.solver import (
                _get_s3_client,
            )
            s3 = _get_s3_client()
        except Exception:  # noqa: BLE001 -- sidecar fallback is optional
            s3 = None
        decision = await gate_supplied_mesh(
            tool_name="telemac_rain_on_grid", engine="telemac", input_mode=None,
            loaded_mesh_uris=loaded_mesh_uris, s3_client=s3)
        if not decision.use or decision.artifact is None:
            return None, decision.note
        # Accepted: stage the .slf (solver geometry) + .2dm (node parsing) locally
        # and build the full supplied mesh (offloaded -- s3 + parse are blocking).
        art = decision.artifact

        def _materialize():
            slf_local = materialize_supplied_mesh(
                art, str(rundir), s3, engine="telemac")
            twodm_local = str(Path(rundir) / "supplied_mesh.2dm")
            bkt, key = art.display_uri[len("s3://"):].split("/", 1)
            s3.download_file(bkt, key, twodm_local)
            return MA.use_supplied_mesh_2dm(
                twodm_path=twodm_local, slf_path=slf_local,
                utm_epsg=int(art.utm_epsg), pour_point=tuple(pp),
                outlet_lonlat=art.outlet_lonlat,
                area_km2=float((art.provenance or {}).get("area_km2") or 0.0),
                catchment_geojson=None)

        mesh = await asyncio.to_thread(_materialize)
        logger.info(
            "rog: consuming case mesh %r (%d elements) instead of delineating",
            art.name, art.element_count)
        return mesh, decision.note
    except Exception as exc:  # noqa: BLE001 -- gate must never break the solve
        logger.warning(
            "rog mesh precondition gate failed (%s); delineating fresh", exc,
            exc_info=True)
        return None, None


#: Half-side (deg) of the pour-point-derived AOI. +-0.14 deg -> a 0.28-deg box,
#: comfortably under the 0.3-deg watershed-primitive D8 clamp, generous enough to
#: contain a single catchment upstream of the outlet (the delineation truncates
#: at the box edge, so this must OVER-cover, never clip mid-hillslope).
_ROG_POUR_BUFFER_DEG: float = 0.14


def _aoi_from_pour_point(pp: tuple[float, float]) -> tuple[float, float, float, float]:
    """Derive a catchment-containing analysis AOI centered on the pour point.

    Used when a pour point is supplied but no explicit bbox: the geocoded place
    bbox names a town and need not contain the UPSTREAM catchment, so the AOI is
    a generous buffer around the OUTLET instead. Clamped to valid lon/lat."""
    lon, lat = float(pp[0]), float(pp[1])
    b = _ROG_POUR_BUFFER_DEG
    return (
        max(lon - b, -180.0),
        max(lat - b, -90.0),
        min(lon + b, 180.0),
        min(lat + b, 90.0),
    )


def _guess_utm_epsg(lonlat: tuple[float, float]) -> int:
    lon, lat = float(lonlat[0]), float(lonlat[1])
    return (32600 if lat >= 0 else 32700) + int((lon + 180.0) // 6.0) + 1


def _sample_node_fields(mesh: Any, aoi: tuple, curve_number: float | None):
    """NLCD sampled at the mesh nodes -> (CN2, Manning) per node (sandbox parity)."""
    import numpy as np

    from trid3nt_server.agent.tools import TOOL_REGISTRY
    from trid3nt_server.agent.tools.cache import read_object_bytes_s3
    from trid3nt_server.agent.workflows.telemac.rain_on_grid.mesh_acquisition import (
        _sample_raster_at_nodes, assemble_node_fields,
    )

    points_ll = np.asarray(mesh.meta["points_lonlat"], dtype=float)
    lc = TOOL_REGISTRY["fetch_landcover"].fn(
        bbox=list(aoi), dataset="nlcd_2021", resolution_m=30)
    lc_uri = lc["uri"] if isinstance(lc, dict) else getattr(lc, "uri")
    tmp = Path(mesh.slf_path).parent / "nlcd.tif"
    tmp.write_bytes(
        read_object_bytes_s3(lc_uri) if str(lc_uri).startswith("s3://")
        else Path(lc_uri).read_bytes())
    nlcd = [int(round(v)) for v in _sample_raster_at_nodes(tmp, points_ll)]
    return assemble_node_fields(
        node_nlcd=nlcd, uniform_cn=curve_number, slopes_m_per_m=None,
        steep_slope_correction=False)


def _fetch_hyetograph_blocks(aoi, window: str, sim_s_hint: float):
    """AOI-mean hourly hyetograph -> block list [[t_end_s, gross_mm], ...].

    ``window`` is ``"YYYY-MM-DD/YYYY-MM-DD"`` (or ``start..end``). Fetches the
    hourly AORC accumulation over the catchment AOI (the pre-2020-capable
    product; MRMS only covers ~2020-10+), builds one 3600-s block per hour, and
    returns (blocks, per-hour mm list, sim_seconds). The sim length is the
    hyetograph span unless ``sim_s_hint`` is larger."""
    from trid3nt_server.agent.tools import TOOL_REGISTRY

    sep = "/" if "/" in window else (".." if ".." in window else None)
    if not sep:
        raise TelemacRainOnGridError(
            "TELEMAC_ROG_BAD_WINDOW",
            f"mrms_window must be 'start/end' dates; got {window!r}.")
    a, b = [s.strip() for s in window.split(sep, 1)]
    d = TOOL_REGISTRY["fetch_aorc_precip"].fn(
        bbox=[float(v) for v in aoi], start_date=a, end_date=b)
    d = d if isinstance(d, dict) else getattr(d, "__dict__", {})
    mm = [max(0.0, float(v)) for v in d["precip_mm"]]
    if len(mm) < 2:
        raise TelemacRainOnGridError(
            "TELEMAC_ROG_EMPTY_HYETO",
            f"AORC returned {len(mm)} hourly steps for {window!r}; need >= 2.")
    blocks = [[float((i + 1) * 3600), round(mm[i], 5)] for i in range(len(mm))]
    sim_s = max(float(sim_s_hint or 0.0), float(len(mm) * 3600))
    return blocks, mm, sim_s


async def _stage_solve_postprocess(
    *, mesh, node_cn2, node_manning, amc, curve_number, design_storm_mm_per_hr,
    sim_s, runoff_path, pour_point, observed_gauge_id, compute_class,
    reach_name, run_tag, hyetograph_blocks=None,
):
    """Upload the mesh + node fields, dispatch run_solver, and rasterize the peak
    depth COG. Mirrors the ``telemac_river_dye`` run_solver seam."""
    import asyncio

    from trid3nt_server.agent.tools.simulation.solver.solver import (
        run_solver, wait_for_completion,
    )
    from trid3nt_server.agent.workflows.telemac.postprocess_telemac import (
        postprocess_telemac_wse,
    )
    from trid3nt_server.agent.workflows.telemac.run_telemac import TELEMAC_SOLVER_NAME

    # write node fields next to the mesh, upload the three worker inputs.
    slf_dir = Path(mesh.slf_path).parent
    (slf_dir / "node_cn2.txt").write_text(
        "\n".join(f"{v:.3f}" for v in node_cn2) + "\n")
    (slf_dir / "node_manning.txt").write_text(
        "\n".join(f"{v:.3f}" for v in node_manning) + "\n")
    cache_bucket = (os.environ.get("TRID3NT_CACHE_BUCKET") or "").strip()
    if not cache_bucket:
        raise TelemacRainOnGridError(
            "TELEMAC_ROG_STAGING_FAILED",
            "TRID3NT_CACHE_BUCKET must be set to stage the rain-on-grid inputs.")
    from trid3nt_server.agent.tools.simulation.solver.solver import _get_s3_client
    s3 = _get_s3_client()
    inputs = []
    for name, src in (("watershed.slf", mesh.slf_path),
                      ("node_cn2.txt", str(slf_dir / "node_cn2.txt")),
                      ("node_manning.txt", str(slf_dir / "node_manning.txt"))):
        key = f"telemac_rog/{run_tag}/{name}"
        s3.put_object(Bucket=cache_bucket, Key=key, Body=Path(src).read_bytes())
        inputs.append({"gs_uri": f"s3://{cache_bucket}/{key}", "dest": name})

    reach = {
        "name": reach_name,
        "mode": "rain_on_grid",
        "watershed_slf": "watershed.slf",
        "runoff_path": runoff_path,
        "amc_condition": int(amc),
        "rain_intensity_mm_per_hr": float(design_storm_mm_per_hr),
        "node_cn2_file": "node_cn2.txt",
        "node_manning_file": "node_manning.txt",
        "outlet_lonlat": list(mesh.outlet_lonlat),
        "n_outlet_nodes": 8,
        "duration_s": float(sim_s),
        "time_step_s": 3.0,
        "graphic_period": 200,
    }
    if curve_number is not None:
        reach["curve_number"] = float(curve_number)
    if observed_gauge_id:
        reach["observed_gauge_id"] = str(observed_gauge_id)
    if hyetograph_blocks:
        # ADR 0206: the gross hourly hyetograph drives the native SCS-CN
        # per-timestep (RAINDEF=3 FORTRAN FILE staged worker-side).
        reach["rain_hyetograph_blocks"] = hyetograph_blocks
    manifest = {
        "reach": reach, "run_id": run_tag, "inputs": inputs, "telemac_args": [],
        "outputs": ["r2d_rog.slf", "rog_geometry.slf", "rog_max_fields.slf",
                    "rog_outlet_hydrograph.json", "full_listing.log",
                    "telemac_metrics.json"],
    }
    key = f"telemac_rog/{run_tag}/manifest.json"
    s3.put_object(Bucket=cache_bucket, Key=key,
                  Body=json.dumps(manifest, indent=2).encode("utf-8"),
                  ContentType="application/json")
    manifest_uri = f"s3://{cache_bucket}/{key}"

    handle = run_solver(solver=TELEMAC_SOLVER_NAME, model_setup_uri=manifest_uri,
                        compute_class=compute_class)
    run_result = await wait_for_completion(handle, timeout_s=86400.0)
    if getattr(run_result, "status", None) != "complete":
        raise TelemacRainOnGridError(
            "TELEMAC_ROG_RUN_FAILED",
            f"rain-on-grid solve did not complete (status="
            f"{getattr(run_result, 'status', None)}, "
            f"error={getattr(run_result, 'error_message', '')}).")

    batch_run_id = getattr(run_result, "run_id", None) or handle.run_id
    slf_path = await asyncio.to_thread(_download_rog_result, batch_run_id)
    layers, _metrics = await asyncio.to_thread(
        postprocess_telemac_wse, slf_path, run_id=batch_run_id,
        mesh_epsg=int(mesh.utm_epsg), reach_name=reach_name, quantity="depth",
        mesh_frame_note="rain-on-grid peak water depth (UTM mesh frame)")
    if not layers:
        raise TelemacRainOnGridError(
            "TELEMAC_ROG_NO_LAYER",
            "postprocess produced no depth layer (dry catchment?).")
    # ADR 0208 (NATE full-results ask): publish the full-results SELAFIN
    # (r2d_rog.slf -- ALL frames, ALL variables) as a case mesh layer alongside the
    # peak-depth COG, so QGIS/MDAL animates it with the temporal controller.
    await _publish_full_results_mesh(
        batch_run_id, mesh_epsg=int(mesh.utm_epsg), reach_name=reach_name)
    return layers[0]


async def _publish_full_results_mesh(
    run_id: str, *, mesh_epsg: int, reach_name: str
) -> None:
    """Publish r2d_rog.slf as a ``layer_type="mesh"`` case layer (best-effort).

    The full-results SELAFIN (every frame, every variable) is a native MDAL mesh:
    QGIS opens it directly and its time steps drive the temporal controller (no
    per-frame COGs, no plugin change -- the 0200 mesh-layer seam). It rides the
    runs-bucket object the depth COG was rasterized from (no re-upload), stamped in
    the mesh's own UTM CRS. NEVER fails the run.

    Payload scaling: a 6 h / 37-frame run is ~4 MB; the SELAFIN grows ~linearly
    with frames x nodes, so a multi-day / high-graphic-period run can reach tens of
    MB (still MDAL-streamable via /vsicurl/, but sizeable to download)."""
    from trid3nt_contracts.execution import LayerURI

    from trid3nt_server.agent.tools.simulation.solver.solver import _get_runs_bucket
    from trid3nt_server.emission.layer_uri_emit import publish_input_layer
    from trid3nt_server.emission.pipeline_emitter import current_emitter

    emitter = current_emitter()
    if emitter is None:
        return
    try:
        mesh_uri = f"s3://{_get_runs_bucket()}/{run_id}/r2d_rog.slf"
        mesh_layer = LayerURI(
            layer_id=f"rog-results-{run_id}",
            name=f"Model results (time series): {reach_name}",
            layer_type="mesh", uri=mesh_uri, style_preset="mesh_grid",
            role="context", bbox=None, crs_authid=f"EPSG:{int(mesh_epsg)}")
        await publish_input_layer(emitter, mesh_layer, role="context")
        logger.info("rog: published full-results mesh layer %s", mesh_uri)
    except Exception as exc:  # noqa: BLE001 -- full-results layer is a bonus
        logger.warning("rog full-results mesh layer emit skipped: %s", exc)


def _download_rog_result(run_id: str) -> str:
    """Download r2d_rog.slf from the runs bucket to a local temp path."""
    import tempfile

    from trid3nt_server.agent.tools.simulation.solver.solver import _get_s3_client

    runs_bucket = (os.environ.get("TRID3NT_RUNS_BUCKET") or "").strip()
    s3 = _get_s3_client()
    dst = Path(tempfile.mkdtemp()) / "r2d_rog.slf"
    s3.download_file(runs_bucket, f"{run_id}/r2d_rog.slf", str(dst))
    return str(dst)
