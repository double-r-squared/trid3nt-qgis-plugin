"""The RAIN-ON-GRID front of TELEMAC: a catchment in, an outlet hydrograph out.

The reach family meshes a corridor along a flowline and the open-water family
lays a grid over an extent. A rain-on-grid domain is neither: it is a DELINEATED
CATCHMENT, bounded by the terrain that drains to one point, meshed in its
interior and refined toward the channel network. The generation itself is not a
TELEMAC fact and lives in the shared mesh front
(``workflows/mesh/watershed.py``); what lives HERE is only what is TELEMAC about
a rain-on-grid run - the BOTTOM SELAFIN the solver reads its geometry from, the
per-node fields the SCS-CN model reads (``FORMATTED DATA FILE 2``), the manifest
the worker's ``rain_on_grid`` mode dispatches on, and the depth-plus-hydrograph
deliverable the question is answered with.

Everything past the primary layer is best-effort by contract, exactly as in the
other two families: a missing hydrograph or an unpublished results mesh never
voids a solve.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Mapping

from trid3nt_contracts import new_ulid
from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.telemac_contracts import (
    TELEMAC_WSE_STYLE_PRESET,
    TelemacRainOnGridLayerURI,
)

from trid3nt_server.workflows.lib import DeclarativeError, Step
from trid3nt_server.workflows.mesh import watershed as W
from trid3nt_server.workflows.mesh.telemac_build import write_bottom_selafin
from trid3nt_server.workflows.mesh.tool import declaration_plan_value
from trid3nt_server.workflows.shared.aoi import aoi_slug
from trid3nt_server.workflows.shared.publish_product_layer import publish_product_layer

logger = logging.getLogger("trid3nt_server.workflows.telemac.steps.rain_on_grid")

__all__ = [
    "AcquireCatchment",
    "Catchment",
    "RainOnGrid",
    "RainOnGridError",
    "SolveRainOnGrid",
    "acquire_catchment",
    "build_catchment_mesh",
    "node_infiltration_fields",
    "publish_rain_on_grid_products",
    "resolve_rain_event",
    "solve_rain_on_grid",
    "write_bottom_selafin",
    "write_rain_on_grid_deck",
]

_STEPS = "trid3nt_server.workflows.telemac.steps"

#: The worker manifest key its entrypoint dispatches ``mode="rain_on_grid"`` on,
#: the staging prefix, the result file and the artifacts the supervisor uploads.
_MODE = "rain_on_grid"
_STAGE_PREFIX = "telemac_rog"
_RESULT = "r2d_rog.slf"
_OUTPUTS = ["r2d_rog.slf", "rog_geometry.slf", "rog_max_fields.slf",
            "rog_outlet_hydrograph.json", "full_listing.log", "telemac_metrics.json"]
_HYDROGRAPH = "rog_outlet_hydrograph.json"

#: Wall-clock ceiling on one rain-on-grid solve. A real catchment is tens of
#: thousands of elements over hours of simulated time at a 3 s step, which is an
#: HOURS-class solve - an order of magnitude past the open-water front's flat
#: hour. The number is a bound on the wait, not an estimate of the run: it exists
#: so a wedged container becomes a typed failure instead of a daemon that never
#: returns.
_SOLVE_TIMEOUT_S = 86400.0

#: Seconds in an hour, spelled once so no expression in this module spells it again.
_HOUR_S = 3600.0

#: The engine key the mesh precondition gate checks a case mesh's geometry against.
_ENGINE = "telemac"


class RainOnGridError(DeclarativeError):
    """A rain-on-grid catchment could not be acquired, staged, solved or read."""

    error_code = "TELEMAC_ROG_FAILED"


# --------------------------------------------------------------------------- #
# 1. acquire: the outlet, and the analysis window around it.
# --------------------------------------------------------------------------- #
async def acquire_catchment(*, location: str | None, bbox: Any,
                            pour_point: Any, half_deg: float,
                            default_name: str = "watershed",
                            code_prefix: str = "TELEMAC_ROG") -> dict[str, Any]:
    """Resolve the outlet and the AOI the catchment is delineated INSIDE.

    Order matters and is the opposite of every other domain here: the POUR POINT
    comes first and the AOI is derived FROM it, because a geocoded place bbox
    names a town and need not contain the upstream catchment. The live bug was
    'Otto, NC' clipping the Coweeta basin mid-hillslope into a 20-cell sliver.

    An explicit ``bbox`` still wins - it is the user's own extent, and squaring a
    different one off around the outlet would model a domain nobody asked for. A
    ``location`` names the run and nothing else here; the catchment's shape is the
    terrain's answer, not the geocoder's.
    """
    from trid3nt_server.workflows.lib import user_input

    point = user_input.lonlat_point(pour_point, label="pour_point",
                                    code=f"{code_prefix}_PARAMS_INVALID")
    if point is None:
        # Unreachable through the plan (the draw gate refuses first), and stated
        # anyway: an outlet decides the entire catchment, so a missing one is a
        # refusal rather than a centroid nobody chose.
        raise RainOnGridError(
            "the catchment has no pour point, and an outlet is never invented: "
            "the point decides which basin is modelled at all.",
            error_code=f"{code_prefix}_PARAMS_INCOMPLETE")

    extent = (tuple(float(v) for v in bbox) if bbox is not None
              else W.catchment_aoi(point, half_deg))
    name = str(location).strip() if (location and str(location).strip()) \
        else default_name
    return {"bbox": extent, "name": name,
            "slug": aoi_slug(name, default=default_name),
            "pour_point": [point[0], point[1]],
            "aoi_basis": "user bbox" if bbox is not None else
                         f"a +-{float(half_deg):g} deg buffer around the outlet"}


def AcquireCatchment(*, location: Any, bbox: Any, pour_point: Any,  # noqa: N802
                     half_deg: float, default_name: str = "watershed",
                     code_prefix: str = "TELEMAC_ROG") -> Step:
    """Outlet + AOI -> the modelled world. Refines the domain for everything after."""
    return Step(runner=f"{_STEPS}.rain_on_grid.acquire_catchment", stage="acquire",
                kwargs={"location": location, "bbox": bbox, "pour_point": pour_point,
                        "half_deg": half_deg, "default_name": default_name,
                        "code_prefix": code_prefix}).overrides_domain()


# --------------------------------------------------------------------------- #
# 2. mesh: the catchment, triangulated, with a BOTTOM SELAFIN beside it.
# --------------------------------------------------------------------------- #
def _refuse_declared_edits(declaration: Any) -> None:
    """Refuse an ask this path cannot honour whole.

    A declared edit is part of the ask, and this generation runs the catchment
    strategy directly rather than through a mesh session, so there is no chain to
    prefix. Refused BY NAME rather than dropped: an edit that silently did nothing
    reads as a lever that shaped the mesh.
    """
    from trid3nt_server.workflows.mesh.meshers import MeshToolError

    if declaration.edits:
        raise MeshToolError(
            "MESH_DECLARED_EDIT_UNSUPPORTED",
            f"the catchment mesh ask declares the edits "
            f"{[e.action for e in declaration.edits]}, and this template builds its "
            "catchment through the delineation strategy rather than a mesh session, "
            "so no chain exists to apply them to. Build the mesh with build_mesh "
            "and hand it to this run instead.")


async def _adopt_case_mesh(rundir: Path, pour_point: tuple[float, float],
                           slug: str) -> tuple[Any, str | None]:
    """Offer a mesh this CASE already holds; ``(mesh | None, note | None)``.

    Mesh creation is an explicit user act (a standalone ``build_mesh`` call),
    never auto-guessed inside a model template - so when the declared slot is
    unfilled the template ASKS rather than assuming. Accepted, the case mesh is
    adopted end to end; declined, absent or incompatible, the catchment is
    delineated fresh. NEVER raises into the solve path: a discovery fault degrades
    to fresh authoring with a logged warning, because a mesh offer is a
    convenience and losing it must not lose the run.
    """
    from trid3nt_server.workflows.mesh.precondition_gate import (
        gate_supplied_mesh, materialize_supplied_mesh,
    )

    try:
        from trid3nt_server.emission.pipeline_emitter import current_emitter
        from trid3nt_server.workflows.solver.solver import _get_s3_client

        emitter = current_emitter()
        loaded = ([ly.uri for ly in emitter.loaded_layers
                   if getattr(ly, "layer_type", None) == "mesh"]
                  if emitter is not None else [])
        s3 = _get_s3_client()
        decision = await gate_supplied_mesh(
            tool_name="telemac_rain_on_grid", engine=_ENGINE, input_mode=None,
            loaded_mesh_uris=loaded, s3_client=s3)
        if not decision.use or decision.artifact is None:
            return None, decision.note
        art = decision.artifact

        def _materialize():
            slf_local = materialize_supplied_mesh(art, str(rundir), s3, engine=_ENGINE)
            twodm_local = str(rundir / "supplied_mesh.2dm")
            bucket, key = art.display_uri[len("s3://"):].split("/", 1)
            s3.download_file(bucket, key, twodm_local)
            return W.adopt_supplied_mesh_2dm(
                twodm_path=twodm_local, slug=slug, utm_epsg=int(art.utm_epsg),
                pour_point=pour_point, outlet_lonlat=art.outlet_lonlat,
                area_km2=float((art.provenance or {}).get("area_km2") or 0.0),
                source_path=slf_local, note=decision.note)

        mesh = await asyncio.to_thread(_materialize)
        logger.info("rog: solving on the case mesh %r (%d elements)",
                    art.name, art.element_count)
        return mesh, decision.note
    except Exception as exc:  # noqa: BLE001 - a mesh OFFER never breaks the solve
        logger.warning("rog: the case-mesh offer failed (%s); delineating fresh",
                       exc, exc_info=True)
        return None, None


async def build_catchment_mesh(*, mesh: dict[str, Any], supplied: Any,
                               bed_dem: dict[str, Any],
                               rivers: dict[str, Any] | None) -> dict[str, Any]:
    """The catchment mesh, however it was acquired, plus the solver geometry file.

    THE SLATE: a mesh SUPPLIED on this invocation is taken as-is and nothing here
    has an opinion about it. Only when the slot is unfilled does the template ask
    whether to adopt a mesh this case already holds, and only when that is
    declined or absent does it generate one - a labeled fallback, never a stance.

    Every knob the generation reads comes off the REBUILT declaration, so the
    mesher's own declared defaults are what stand when the template named nothing.
    """
    from trid3nt_server.workflows.mesh.tool import declaration_from_plan_value

    declaration = declaration_from_plan_value(mesh)
    _refuse_declared_edits(declaration)
    fields = declaration.spec.fields
    aoi = dict(fields["extent"])
    min_edge_m = float(fields["min_edge_length_m"])
    max_edge_m = float(fields["max_edge_length_m"])
    grade = float(fields["grade"])
    max_iter = int(fields["max_iter"])
    snap_search_cells = int(fields["snap_search_cells"])

    rundir = Path(os.environ.get("TRID3NT_RUNS_DIR", "/tmp")) / f"rog-{new_ulid()}"
    rundir.mkdir(parents=True, exist_ok=True)
    point = (float(aoi["pour_point"][0]), float(aoi["pour_point"][1]))
    slug = str(aoi["slug"])

    mesh = None
    if supplied:
        uri = supplied if isinstance(supplied, str) else str(
            (supplied or {}).get("uri") or "")
        mesh = await asyncio.to_thread(
            _stage_supplied_mesh, uri, rundir, slug, point)
    if mesh is None:
        mesh, _note = await _adopt_case_mesh(rundir, point, slug)
    if mesh is None:
        # Warm the geo stack on the MAIN thread first: shapely and geopandas have a
        # thread-first-import circular-import race, and the generation below runs in
        # a worker thread - on a fresh daemon it would be the first geopandas import
        # in the process.
        import geopandas as _gpd  # noqa: F401

        mesh = await asyncio.to_thread(
            W.generate_catchment_mesh,
            pour_point=point, bbox=tuple(aoi["bbox"]), slug=slug,
            output_dir=str(rundir), bed_dem=bed_dem, rivers=rivers,
            min_edge_length_m=float(min_edge_m), max_edge_length_m=float(max_edge_m),
            grade=float(grade), max_iter=int(max_iter),
            snap_search_cells=int(snap_search_cells))

    # The MODELLED extent in 4326 - the mesh's own node bounds, not the analysis
    # window it was cut from. The delineated basin is a fraction of the buffer the
    # search ran in, so the AOI would point the camera at mostly-unmodelled ground
    # and would give an animation check an extent the frames never covered.
    lonlat = mesh.points_lonlat
    if lonlat is not None:
        import numpy as _np

        pts = _np.asarray(lonlat, dtype=float)
        bounds = [float(pts[:, 0].min()), float(pts[:, 1].min()),
                  float(pts[:, 0].max()), float(pts[:, 1].max())]
    else:
        bounds = list(aoi["bbox"])

    slf_path = mesh.source_path
    if not slf_path:
        slf_path = str(rundir / "watershed.slf")
        await asyncio.to_thread(write_bottom_selafin, slf_path, mesh.points_utm,
                                mesh.cells, mesh.bed_elev)
    return {"mesh": mesh, "slf_path": slf_path, "rundir": str(rundir),
            "name": str(aoi["name"]), "slug": slug, "bbox": list(aoi["bbox"]),
            # The two declared artifacts narrate themselves SEPARATELY: which bed
            # the nodes were sampled from is a different fact from what the mesh
            # was refined toward, and one row cannot carry both.
            "bed_note": str((bed_dem or {}).get("note") or ""),
            "bed_source": str((bed_dem or {}).get("source") or ""),
            "rivers_note": str((rivers or {}).get("note") or ""),
            "rivers_source": str((rivers or {}).get("source") or ""),
            "utm_epsg": int(mesh.utm_epsg), "area_km2": float(mesh.area_km2),
            "node_count": mesh.node_count, "element_count": mesh.element_count,
            "outlet_lonlat": list(mesh.outlet_lonlat),
            "lonlat_bounds": bounds,
            "provenance": mesh.provenance, "notes": list(mesh.notes),
            "min_edge_m": float(min_edge_m), "max_edge_m": float(max_edge_m)}


def _stage_supplied_mesh(uri: str, rundir: Path, slug: str,
                         point: tuple[float, float]) -> Any:
    """Bring a supplied mesh local and adopt it. Refuses typed on an unusable one."""
    from trid3nt_server.tools.cache import read_object_bytes_s3

    if not uri:
        return None
    local = rundir / Path(uri).name
    local.write_bytes(read_object_bytes_s3(uri) if uri.startswith("s3://")
                      else Path(uri).read_bytes())
    if local.suffix.lower() == ".2dm":
        return W.adopt_supplied_mesh_2dm(
            twodm_path=str(local), slug=slug,
            utm_epsg=W.utm_epsg_for(point[0], point[1]), pour_point=point,
            note=f"solved on the mesh supplied for this invocation ({local.name})")
    return W.adopt_supplied_mesh(
        mesh_path=str(local), slug=slug,
        utm_epsg=W.utm_epsg_for(point[0], point[1]), pour_point=point,
        note=f"solved on the mesh supplied for this invocation ({local.name})")


# --------------------------------------------------------------------------- #
# 3. prep: what each node infiltrates and how rough it is.
# --------------------------------------------------------------------------- #
async def node_infiltration_fields(*, mesh: dict[str, Any],
                                   landcover: dict[str, Any],
                                   curve_number: float | None,
                                   steep_slope_correction: bool,
                                   antecedent_moisture: Any) -> dict[str, Any]:
    """Per-node CN2 + Manning n, sampled from land cover at the mesh nodes.

    The infiltration surface the engine's own SCS-CN model reads out of
    ``FORMATTED DATA FILE 2``. A uniform ``curve_number`` overrides the CN field
    and ONLY the CN field: roughness is a separate physical property, so every
    node still takes its land-cover Manning n.

    ``steep_slope_correction`` applies the Huang (2006) rational correction to the
    CN field HERE, before the file is written, because the branch that would have
    done it inside the engine is compiled off in the installed 9.0.0 build. The
    slopes come from the mesh's own piecewise-linear bed - the discretization the
    solver sees - never from a finer raster the run does not resolve.
    """
    from trid3nt_server.workflows.telemac.rain_on_grid.cn_infiltration import (
        amc_condition_for, landcover_cn_manning, node_curve_numbers,
    )

    handle = mesh["mesh"]
    if handle.points_lonlat is None:
        raise RainOnGridError(
            "the supplied mesh carries no readable node coordinates, so the "
            "per-node curve numbers cannot be sampled at them. Supply the mesh as "
            "a .2dm (nodes and bed readable) rather than as a bare SELAFIN.",
            error_code="TELEMAC_ROG_SUPPLIED_MESH_UNREADABLE")

    def _sample() -> tuple[list[float], list[float], list[int]]:
        from trid3nt_server.tools.cache import read_object_bytes_s3

        uri = str(landcover["uri"])
        local = Path(mesh["rundir"]) / "landcover.tif"
        local.write_bytes(read_object_bytes_s3(uri) if uri.startswith("s3://")
                          else Path(uri).read_bytes())
        codes = [int(round(v)) for v in
                 W.sample_raster_at_nodes(local, handle.points_lonlat)]
        slopes = (list(W.node_slopes_from_mesh(handle.points_utm, handle.cells,
                                               handle.bed_elev))
                  if steep_slope_correction else None)
        manning = [landcover_cn_manning(c)[1] for c in codes]
        cn2 = node_curve_numbers(codes, uniform_cn=curve_number,
                                 slopes_m_per_m=slopes,
                                 steep_slope_correction=bool(steep_slope_correction))
        return cn2, manning, codes

    node_cn2, node_manning, codes = await asyncio.to_thread(_sample)
    amc = amc_condition_for(antecedent_moisture)
    logger.info("rog infiltration: %d nodes, %d land-cover classes, AMC=%d, "
                "uniform_cn=%s, steep_slope=%s", len(codes), len(set(codes)), amc,
                curve_number, bool(steep_slope_correction))
    return {"node_cn2": node_cn2, "node_manning": node_manning,
            "amc_condition": int(amc), "curve_number": curve_number,
            "steep_slope_correction": bool(steep_slope_correction),
            "landcover_classes": sorted(set(codes)),
            "note": str(landcover.get("note") or "")}


# --------------------------------------------------------------------------- #
# 4. forcing: the rain that falls on it.
# --------------------------------------------------------------------------- #
def resolve_rain_event(*, window: str | None, intensity_mm_per_hr: float,
                       storm_duration_hr: float, sim_duration_hr: float | None,
                       fallback: tuple[str, ...] = ()) -> dict[str, Any]:
    """The storm, as either a real hourly hyetograph or a constant design rate.

    TWO RUNGS, and the ladder is the run's own record of which one answered. A
    dated ``window`` fetches the hourly AORC accumulation over the catchment and
    the run is driven by the REAL intensity structure, which is what resolves the
    hydrograph SHAPE. With no window the storm is a constant design rate over a
    declared duration - a hypothetical, and labeled as one.

    AORC rather than MRMS despite the argument's history: MRMS only covers
    ~2020-10 onward, and a replication window that predates it would silently
    return nothing.
    """
    from trid3nt_server.tools import TOOL_REGISTRY

    rungs = " -> ".join(fallback) if fallback else "aorc_hourly -> design_storm"
    if not window:
        return {
            "kind": "design_storm", "blocks": None, "series": None,
            "intensity_mm_per_hr": float(intensity_mm_per_hr),
            "duration_s": float(sim_duration_hr if sim_duration_hr
                                else storm_duration_hr) * _HOUR_S,
            "duration_basis": "user" if sim_duration_hr else "storm",
            "note": (f"a CONSTANT design storm of {float(intensity_mm_per_hr):g} mm/h "
                     f"over {float(storm_duration_hr):g} h - a hypothetical event, "
                     f"not a record. Ladder {rungs}."),
        }
    bbox = W._domain_bbox("the rain hyetograph")
    sep = "/" if "/" in window else (".." if ".." in window else None)
    if not sep:
        raise RainOnGridError(
            f"the rain window must be 'start/end' dates; got {window!r}.",
            error_code="TELEMAC_ROG_BAD_WINDOW")
    start, end = [s.strip() for s in window.split(sep, 1)]
    payload = TOOL_REGISTRY["fetch_aorc_precip"].fn(
        bbox=[float(v) for v in bbox], start_date=start, end_date=end)
    payload = payload if isinstance(payload, dict) else getattr(payload, "__dict__", {})
    mm = [max(0.0, float(v)) for v in payload["precip_mm"]]
    if len(mm) < 2:
        raise RainOnGridError(
            f"AORC returned {len(mm)} hourly steps for {window!r}; a hyetograph "
            "needs at least two. Widen the window or run the design storm.",
            error_code="TELEMAC_ROG_EMPTY_HYETO")
    blocks = [[float((i + 1) * _HOUR_S), round(mm[i], 5)] for i in range(len(mm))]
    asked_s = float(sim_duration_hr or 0.0) * _HOUR_S
    span_s = float(len(mm) * _HOUR_S)
    return {
        "kind": "hyetograph", "blocks": blocks, "series": mm,
        "intensity_mm_per_hr": float(intensity_mm_per_hr),
        "duration_s": max(asked_s, span_s),
        "duration_basis": "user" if asked_s > span_s else "hyetograph",
        "window": window, "total_mm": round(sum(mm), 3),
        "note": (f"the REAL hourly AORC hyetograph over {window} - {len(mm)} steps, "
                 f"{sum(mm):.3g} mm total. Ladder {rungs}."),
    }


def _soil_store_spin_up(*, window: str, capacity_mm: float, recovery_hr: float,
                        antecedent_days: int) -> float:
    """Initial soil-store level V0 (mm), spun up over the REAL antecedent rain.

    V0 IS the integrated antecedent wetness the catchment carries into the event -
    the dynamic state that replaces a per-event AMC choice. The store dynamics are
    the worker's own (Michel 2005), so the spin-up and the run are one continuous
    model rather than two that agree by inspection.
    """
    import datetime as _dt
    import math as _math

    from trid3nt_server.tools import TOOL_REGISTRY

    bbox = W._domain_bbox("the antecedent rainfall")
    sep = "/" if "/" in window else (".." if ".." in window else None)
    if not sep:
        raise RainOnGridError(
            f"the rain window must be 'start/end' dates; got {window!r}.",
            error_code="TELEMAC_ROG_BAD_WINDOW")
    start = window.split(sep, 1)[0].strip()
    start_d = _dt.date.fromisoformat(start[:10])
    ant_start = (start_d - _dt.timedelta(days=int(antecedent_days))).isoformat()
    payload = TOOL_REGISTRY["fetch_aorc_precip"].fn(
        bbox=[float(v) for v in bbox], start_date=ant_start, end_date=start)
    payload = payload if isinstance(payload, dict) else getattr(payload, "__dict__", {})
    capacity, tau, level = float(capacity_mm), float(recovery_hr), 0.0
    for value in payload.get("precip_mm", []):
        mm = max(0.0, float(value))
        fill = min(1.0, max(0.0, level / capacity))
        level += mm - (1.0 - (1.0 - fill) ** 2) * mm
        level -= level * (1.0 - _math.exp(-1.0 / tau))
    return round(min(level, capacity), 4)


# --------------------------------------------------------------------------- #
# 5. author: the worker manifest.
# --------------------------------------------------------------------------- #
async def write_rain_on_grid_deck(
    *,
    catchment: dict[str, Any],
    infiltration: dict[str, Any],
    rain: dict[str, Any],
    mesh_resolution_m: float | None = None,
    time_step_s: float,
    outlet_node_count: int,
    output_interval_min: float | None = None,
    soil_store: bool = False,
    soil_store_capacity_mm: float | None = None,
    soil_recovery_hr: float,
    soil_spinup_days: int,
) -> dict[str, Any]:
    """Serialize the approved sheet into the worker's rain-on-grid manifest.

    The returned value carries what SOLVES it - the mode the entrypoint dispatches
    on, the inputs to stage, the artifacts to bring back - so the dispatch needs to
    know nothing about rain.

    The output CADENCE is computed here rather than worker-side because the time
    step is the template's own: ``graphic_period`` is a count of steps, and the
    only party that knows how many steps make a minute is the one that declared the
    step. Unasked, the worker's own default stands untouched.
    """
    from trid3nt_server.workflows.telemac.rain_on_grid.cn_infiltration import (
        select_runoff_path,
    )

    if soil_store:
        # The store integrates a real antecedent history, so it cannot run on a
        # hypothetical design storm - and it needs a retention capacity to
        # calibrate against. Both are refused HERE, before any staging.
        if rain["kind"] != "hyetograph":
            raise RainOnGridError(
                "soil_store needs a real rain window (the hyetograph plus its "
                "antecedent history); it cannot run on a hypothetical design storm.",
                error_code="TELEMAC_ROG_SOIL_STORE_NEEDS_WINDOW")
        if soil_store_capacity_mm is None:
            raise RainOnGridError(
                "soil_store needs soil_store_capacity_mm, the retention capacity S "
                "(mm) the store is calibrated on.",
                error_code="TELEMAC_ROG_SOIL_STORE_NO_CAPACITY")

    decision = (select_runoff_path(hyetograph_mm=rain["series"])
                if rain["kind"] == "hyetograph"
                else select_runoff_path(
                    constant_intensity_mm_per_hr=rain["intensity_mm_per_hr"]))

    run_tag = new_ulid()
    config: dict[str, Any] = {
        "name": str(catchment["name"]),
        "mode": _MODE,
        "watershed_slf": "watershed.slf",
        "runoff_path": decision.path,
        "amc_condition": int(infiltration["amc_condition"]),
        "rain_intensity_mm_per_hr": float(rain["intensity_mm_per_hr"]),
        "node_cn2_file": "node_cn2.txt",
        "node_manning_file": "node_manning.txt",
        "outlet_lonlat": list(catchment["outlet_lonlat"]),
        "n_outlet_nodes": int(outlet_node_count),
        "duration_s": float(rain["duration_s"]),
        "time_step_s": float(time_step_s),
        # A count of solver steps between written frames. None keeps the worker's
        # own byte-identical default rather than restating it as a number here.
        "graphic_period": (max(1, round(float(output_interval_min) * 60.0
                                        / float(time_step_s)))
                           if output_interval_min is not None else 200),
    }
    if infiltration["curve_number"] is not None:
        config["curve_number"] = float(infiltration["curve_number"])
    if rain["kind"] == "hyetograph":
        # The gross hourly hyetograph drives the engine's own SCS-CN per timestep
        # through the RAINDEF=3 FORTRAN file the worker stages.
        config["rain_hyetograph_blocks"] = rain["blocks"]
    if soil_store:
        level = await asyncio.to_thread(
            _soil_store_spin_up, window=str(rain["window"]),
            capacity_mm=float(soil_store_capacity_mm),
            recovery_hr=float(soil_recovery_hr),
            antecedent_days=int(soil_spinup_days))
        config.update({
            "soil_store": True,
            "soil_store_capacity_mm": float(soil_store_capacity_mm),
            "soil_store_recovery_h": float(soil_recovery_hr),
            "soil_store_init_mm": float(level),
        })
    return {
        "config": config,
        "run_tag": run_tag,
        "result_basename": _RESULT,
        "outputs": list(_OUTPUTS),
        "catchment": catchment,
        "infiltration": infiltration,
        "rain": rain,
        "runoff_path": decision.path,
        "runoff_reason": decision.reason,
        "mesh_size_m": float(catchment["min_edge_m"]),
        "mesh_resolution_asked_m": mesh_resolution_m,
        "domain_name": str(catchment["name"]),
        "utm_epsg": int(catchment["utm_epsg"]),
    }


# --------------------------------------------------------------------------- #
# 6. solve.
# --------------------------------------------------------------------------- #
def _stage_inputs(deck: dict[str, Any]) -> str:
    """Upload the mesh + node fields + manifest; return the manifest ``s3://`` URI."""
    from trid3nt_server.workflows.solver.solver import _get_s3_client

    bucket = (os.environ.get("TRID3NT_CACHE_BUCKET") or "").strip()
    if not bucket:
        raise RainOnGridError(
            "TRID3NT_CACHE_BUCKET must be set to stage the rain-on-grid inputs.",
            error_code="TELEMAC_ROG_STAGING_FAILED")
    catchment, infiltration = deck["catchment"], deck["infiltration"]
    rundir = Path(catchment["rundir"])
    (rundir / "node_cn2.txt").write_text(
        "\n".join(f"{v:.3f}" for v in infiltration["node_cn2"]) + "\n")
    (rundir / "node_manning.txt").write_text(
        "\n".join(f"{v:.3f}" for v in infiltration["node_manning"]) + "\n")

    run_tag = deck["run_tag"]
    s3 = _get_s3_client()
    inputs = []
    for name, src in (("watershed.slf", catchment["slf_path"]),
                      ("node_cn2.txt", str(rundir / "node_cn2.txt")),
                      ("node_manning.txt", str(rundir / "node_manning.txt"))):
        key = f"{_STAGE_PREFIX}/{run_tag}/{name}"
        s3.put_object(Bucket=bucket, Key=key, Body=Path(src).read_bytes())
        inputs.append({"gs_uri": f"s3://{bucket}/{key}", "dest": name})

    manifest = {"reach": deck["config"], "run_id": run_tag, "inputs": inputs,
                "telemac_args": [], "outputs": deck["outputs"]}
    key = f"{_STAGE_PREFIX}/{run_tag}/manifest.json"
    s3.put_object(Bucket=bucket, Key=key,
                  Body=json.dumps(manifest, indent=2).encode("utf-8"),
                  ContentType="application/json")
    return f"s3://{bucket}/{key}"


async def solve_rain_on_grid(*, deck: dict[str, Any],
                             compute_class: str = "medium") -> dict[str, Any]:
    """Stage the deck, dispatch it to the worker's rain-on-grid mode, and wait.

    The returned ``uri`` is the result SELAFIN under the run prefix - what a ledger
    replay probes, so a resumed rerun can only skip the solve while the solved
    artifact is still there. The UTM zone comes from the DECK rather than from the
    worker's metrics: this mesh is projected agent-side, so the zone is a fact the
    template already knows and the worker never learns.
    """
    from trid3nt_server.workflows.solver.solver import _get_runs_bucket

    from .open_water import dispatch_and_wait
    from .solve import read_run_metrics

    manifest_uri = await asyncio.to_thread(_stage_inputs, deck)
    logger.info("rog staged manifest run_tag=%s name=%s -> %s", deck["run_tag"],
                deck["config"]["name"], manifest_uri)

    run_result, batch_run_id = await dispatch_and_wait(
        solver=_solver_name(), manifest_uri=manifest_uri,
        compute_class=compute_class, label=_MODE, timeout_s=_SOLVE_TIMEOUT_S,
        grid_resolution_m=deck.get("mesh_size_m"),
        active_cell_count=deck["catchment"].get("element_count"))
    if run_result is None or run_result.status != "complete":
        raise RainOnGridError(
            "the rain-on-grid solve did not complete "
            f"(status={getattr(run_result, 'status', None)}, "
            f"error_code={getattr(run_result, 'error_code', None)}): "
            f"{getattr(run_result, 'error_message', '') or ''}",
            error_code="TELEMAC_ROG_RUN_FAILED")
    metrics = await asyncio.to_thread(read_run_metrics, batch_run_id)
    return {"run_id": batch_run_id,
            "uri": f"s3://{_get_runs_bucket()}/{batch_run_id}/{_RESULT}",
            "utm_epsg": int(deck["utm_epsg"]), "metrics": metrics}


def _solver_name() -> str:
    from trid3nt_server.workflows.telemac.run_telemac import TELEMAC_SOLVER_NAME

    return TELEMAC_SOLVER_NAME


# --------------------------------------------------------------------------- #
# 7. publish.
# --------------------------------------------------------------------------- #
def _read_hydrograph(run_id: str) -> dict[str, Any]:
    """The outlet hydrograph the worker wrote; ``{}`` on any miss (best-effort)."""
    from trid3nt_server.workflows.solver.solver import _get_runs_bucket, _get_s3_client

    try:
        body = _get_s3_client().get_object(
            Bucket=_get_runs_bucket(), Key=f"{run_id}/{_HYDROGRAPH}")["Body"].read()
        loaded = json.loads(body.decode("utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except Exception as exc:  # noqa: BLE001 - the hydrograph is a bonus, not the run
        logger.info("rog: outlet hydrograph unreadable for %s: %s", run_id, exc)
        return {}


def _provenance(deck: Mapping[str, Any],
                metrics: Mapping[str, Any]) -> list[SyntheticInput]:
    """The physically dominant inputs, as rows the layer carries.

    Which STORM drove it, which infiltration path ran, where the bed came from and
    whether the mesh was generated or handed in - every one of them a fact the
    answer is meaningless without, so each is stated rather than assumed.
    """
    catchment, infiltration, rain = deck["catchment"], deck["infiltration"], deck["rain"]
    rows = [
        SyntheticInput(
            param="rain_event", value=str(rain["kind"]), basis=(
                "fetched" if rain["kind"] == "hyetograph" else "default_demo"),
            consequence="scenario",
            real_source_if_any=("NOAA AORC hourly analysis"
                                if rain["kind"] == "hyetograph" else None),
            note=str(rain["note"])),
        SyntheticInput(
            param="sim_duration_hr",
            value=round(float(rain["duration_s"]) / _HOUR_S, 3), units="h",
            basis="user" if rain["duration_basis"] == "user" else "derived",
            consequence="numerical",
            note=("the window you asked for" if rain["duration_basis"] == "user"
                  else "the fetched hyetograph's own span"
                  if rain["duration_basis"] == "hyetograph"
                  else "the design storm's own duration")),
        SyntheticInput(
            param="antecedent_moisture", value=int(infiltration["amc_condition"]),
            basis="user", consequence="physics",
            note=("the SCS antecedent-moisture condition the curve numbers are "
                  "converted under - the dominant infiltration lever")),
        SyntheticInput(
            param="runoff_path", value=str(deck["runoff_path"]), basis="derived",
            consequence="physics", note=str(deck["runoff_reason"])),
        SyntheticInput(
            param="mesh_domain",
            value=f"{catchment['element_count']} elements over "
                  f"{float(catchment['area_km2']):.3g} km2",
            basis="user" if catchment["provenance"] == "supplied" else "derived",
            consequence="numerical",
            real_source_if_any=("build_mesh"
                                if catchment["provenance"] == "supplied" else None),
            note=("solved on a mesh SUPPLIED for this invocation"
                  if catchment["provenance"] == "supplied"
                  else "the catchment was delineated at the pour point and meshed "
                       "for this run")),
    ]
    if infiltration["curve_number"] is not None:
        rows.append(SyntheticInput(
            param="curve_number", value=float(infiltration["curve_number"]),
            basis="user", consequence="physics",
            note=("a UNIFORM curve number overriding the land-cover-distributed "
                  "field; roughness is still per-node")))
    if catchment.get("bed_note"):
        rows.append(SyntheticInput(
            param="mesh_bed", value=str(catchment["bed_source"]), basis="fetched",
            consequence="physics",
            real_source_if_any="USGS 3DEP / Copernicus GLO-30",
            note=str(catchment["bed_note"])))
    if catchment.get("rivers_note"):
        rows.append(SyntheticInput(
            param="mesh_sizing_source", value=str(catchment["rivers_source"]),
            basis="fetched", consequence="numerical",
            note=str(catchment["rivers_note"])))
    return rows


def _honesty_note(deck: Mapping[str, Any], metrics: Mapping[str, Any],
                  product_note: str | None) -> str:
    """What the RUN was, prefixed by what the LAYER is.

    The applicability envelope is part of the sentence, not a footnote: rain-on-
    grid reproduces single-storm flash floods in small steep catchments and does
    NOT carry baseflow, because infiltrated water is permanently lost.
    """
    catchment, rain = deck["catchment"], deck["rain"]
    spacing = metrics.get("dx_m") or deck["mesh_size_m"]
    return (
        (f"{product_note} " if product_note else "")
        + "Planning-grade rainfall-runoff SCREENING: TELEMAC-2D shallow water over a "
        f"{float(catchment['area_km2']):.3g} km2 catchment delineated at the pour "
        f"point and triangulated at {float(spacing):g} m minimum edge "
        f"({catchment['element_count']} elements), infiltrating by the SCS "
        "curve-number method with per-node curve numbers from land cover. Driven by "
        + str(rain["note"]).rstrip(".").split(" - ")[0]
        + ". The raster is the peak water DEPTH envelope over the run; the animation "
        "plays from the native rain-on-grid SELAFIN. Single-storm events only: "
        "infiltrated water is permanently lost, so there is no subsurface return "
        "flow and no inter-peak baseflow. Not a calibrated rainfall-runoff model.")


async def publish_rain_on_grid_products(*, deck: dict[str, Any],
                                        solve: dict[str, Any],
                                        ) -> TelemacRainOnGridLayerURI:
    """Postprocess the solved catchment into its published layers + scalars."""
    from trid3nt_server.emission.pipeline_emitter import current_emitter
    from trid3nt_server.workflows.telemac.postprocess_telemac import (
        postprocess_telemac_wse,
    )
    from trid3nt_server.workflows.telemac.results_mesh_seam import (
        publish_results_mesh_via_seam,
    )

    from .open_water import download_open_water_result, mesh_sizing_provenance

    emitter = current_emitter()
    run_id, utm_epsg = solve["run_id"], int(solve["utm_epsg"])
    metrics = dict(solve.get("metrics") or {})
    catchment = deck["catchment"]
    name = str(deck["domain_name"])

    slf_path = await asyncio.to_thread(
        download_open_water_result, run_id, deck["result_basename"],
        error_code="TELEMAC_ROG_OUTPUT_MISSING")
    try:
        layers, _pmetrics = await asyncio.to_thread(
            postprocess_telemac_wse, slf_path, run_id=run_id,
            mesh_epsg=utm_epsg, reach_name=name, quantity="depth",
            mesh_frame_note="rain-on-grid peak water depth (UTM mesh frame)")
    finally:
        Path(slf_path).unlink(missing_ok=True)
    if not layers:
        raise RainOnGridError(
            "the postprocess produced no depth layer: no node in the catchment was "
            "ever wet, so the storm generated no ponded water to map.",
            error_code="TELEMAC_ROG_NO_LAYER")
    raw = layers[0]

    hydrograph = await asyncio.to_thread(_read_hydrograph, run_id)
    rainfall = metrics.get("source_volume_m3")
    runoff = metrics.get("outflow_volume_m3")
    scalars: dict[str, Any] = {
        "catchment_area_km2": round(float(catchment["area_km2"]), 4),
        "peak_discharge_m3s": metrics.get("peak_discharge_m3s"),
        "peak_discharge_time_s": metrics.get("peak_time_s"),
        "rainfall_volume_m3": rainfall,
        "runoff_volume_m3": runoff,
        # A ratio, not a percentage, and only when there was rain to divide by:
        # a runoff coefficient over zero rainfall is a number with no meaning.
        "runoff_coefficient": (round(float(runoff) / float(rainfall), 6)
                               if rainfall and runoff is not None
                               and float(rainfall) > 0.0 else None),
        "max_depth_peak_m": metrics.get("max_depth_peak_m"),
        "max_velocity_peak_ms": metrics.get("max_velocity_peak_ms"),
        "continuity_rel_error": metrics.get("continuity_rel_error"),
        "runoff_path": deck["runoff_path"],
        "amc_condition": int(deck["infiltration"]["amc_condition"]),
        "rain_intensity_mm_per_hr": float(deck["rain"]["intensity_mm_per_hr"]),
        "outlet_hydrograph_t_s": [float(v) for v in (hydrograph.get("t_s") or [])] or None,
        "outlet_hydrograph_q_m3s": [float(v) for v in (hydrograph.get("q_m3s") or [])]
                                   or None,
        "mesh_node_count": int(catchment["node_count"]) or None,
        "mesh_element_count": int(catchment["element_count"]) or None,
        "mesh_size_m": float(deck["mesh_size_m"]),
        "mesh_resolution_label": (
            f"catchment TIN, {float(deck['mesh_size_m']):g} m minimum edge to "
            f"{float(catchment['max_edge_m']):g} m, refined toward the channel "
            f"network ({catchment['element_count']} elements)"),
        "catchment_provenance": str(catchment["provenance"]),
        "catchment_name": name,
        "domain_bbox": [float(v) for v in catchment["lonlat_bounds"]],
    }
    typed = TelemacRainOnGridLayerURI(**raw.model_dump(), **scalars)
    published = await publish_product_layer(
        typed, style_preset=TELEMAC_WSE_STYLE_PRESET,
        update={
            # The published raster is in the mesh's UTM metres, so the postprocess
            # leaves it without a zoom-to extent; the DOMAIN's own 4326 bounds are
            # known here and the camera follows the domain.
            "bbox": tuple(catchment["lonlat_bounds"]),
            "fallback_note": _honesty_note(deck, metrics, raw.fallback_note),
            "synthetic_inputs": (
                _provenance(deck, metrics)
                + mesh_sizing_provenance(deck.get("mesh_resolution_asked_m"), metrics)),
            # The run prefix travels WITH the layer so the skeleton writes this
            # run's own chart spec and answer metrics under it.
            "run_id": run_id,
        })

    # EMIT-ON-SOLVE: outputs.json carries the peak entry plus the SELAFIN mesh
    # entry, and the seam owns publication of the temporal artifact. ``raw`` (the
    # unpublished s3 COG) is what the whole-run record points at, as on the other
    # two families.
    await publish_results_mesh_via_seam(
        emitter, run_id=run_id, engine="telemac", peak_layer=raw,
        peak_quantity="flood_depth", mesh_basename=deck["result_basename"],
        mesh_epsg=utm_epsg, reach_name=name)

    logger.info("rog complete run_id=%s catchment=%s area=%.4g km2 peak_q=%s "
                "peak_depth=%s continuity=%s uri=%s", run_id, name,
                float(catchment["area_km2"]), published.peak_discharge_m3s,
                published.max_depth_peak_m, published.continuity_rel_error,
                published.uri)
    return published


# --------------------------------------------------------------------------- #
# The step constructors, as the facade and the template bind them.
# --------------------------------------------------------------------------- #
class Catchment:
    """The catchment mesh and its node fields, as declared steps."""

    @staticmethod
    def mesh(*, mesh: Any, supplied: Any, bed_dem: Any, rivers: Any) -> Step:
        """Delineate and triangulate the catchment - or adopt the mesh handed in.

        The DECLARATION travels WHOLE - its mesher, its kind, every field the
        router checked against the ``watershed`` mesher and the edits the template
        declared on it - as the plain mapping the interpreter binds late-bound
        reads inside. Nothing about the ask is restated here.
        """
        return Step(runner=f"{_STEPS}.rain_on_grid.build_catchment_mesh", stage="mesh",
                    kwargs={"mesh": declaration_plan_value(mesh),
                            "supplied": supplied,
                            "bed_dem": bed_dem, "rivers": rivers})

    @staticmethod
    def infiltration(*, mesh: Any, landcover: Any, curve_number: Any,
                     steep_slope_correction: Any, antecedent_moisture: Any) -> Step:
        """Per-node curve numbers and Manning n - the infiltration surface."""
        return Step(runner=f"{_STEPS}.rain_on_grid.node_infiltration_fields",
                    stage="prep",
                    kwargs={"mesh": mesh, "landcover": landcover,
                            "curve_number": curve_number,
                            "steep_slope_correction": steep_slope_correction,
                            "antecedent_moisture": antecedent_moisture})


class SolveRainOnGrid:
    """The rain-on-grid solve step. The plan's consequential node."""

    @staticmethod
    def telemac(*, deck: Any, compute_class: Any) -> Step:
        return Step(runner=f"{_STEPS}.rain_on_grid.solve_rain_on_grid", stage="solve",
                    kwargs={"deck": deck, "compute_class": compute_class},
                    consequential=True)


class RainOnGrid:
    """The rain-on-grid author + read steps, as the facade binds them."""

    @staticmethod
    def deck(**kwargs: Any) -> Step:
        return Step(runner=f"{_STEPS}.rain_on_grid.write_rain_on_grid_deck",
                    stage="author", kwargs=kwargs)

    @staticmethod
    def products(*, deck: Any, solve: Any) -> Step:
        return Step(runner=f"{_STEPS}.rain_on_grid.publish_rain_on_grid_products",
                    stage="publish", kwargs={"deck": deck, "solve": solve})
