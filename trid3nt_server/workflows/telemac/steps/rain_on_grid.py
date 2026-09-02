"""The RAIN-ON-GRID front of TELEMAC: a catchment in, an outlet hydrograph out.

A rain-on-grid domain is a DELINEATED CATCHMENT - the terrain that drains to one
point - and the delineation is a chained tool while the triangulation is the one
mesh step. What lives HERE is only what is TELEMAC about a rain-on-grid run: the
per-node fields the SCS-CN model reads (``FORMATTED DATA FILE 2``), the case the
worker runs the authored steering file as, and the depth-plus-hydrograph
deliverable the question is answered with.

Everything past the primary layer is best-effort by contract, exactly as in the
other two families: a missing hydrograph or an unpublished results mesh never
voids a solve.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from trid3nt_contracts import new_ulid
from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.telemac_contracts import (
    TELEMAC_WSE_STYLE_PRESET,
    TelemacRainOnGridLayerURI,
)

from trid3nt_server.workflows.lib import DeclarativeError, Step
from trid3nt_server.workflows.mesh.shared.nodes import (
    node_slopes_from_mesh,
    sample_raster_at_nodes,
)
from trid3nt_server.workflows.shared.aoi import aoi_slug
from trid3nt_server.workflows.shared.layer_fields import layer_field
from trid3nt_server.workflows.shared.publish_product_layer import publish_product_layer

logger = logging.getLogger("trid3nt_server.workflows.telemac.steps.rain_on_grid")

__all__ = [
    "AcquireCatchment",
    "Infiltration",
    "RainOnGrid",
    "RainOnGridError",
    "SolveRainOnGrid",
    "acquire_catchment",
    "catchment_aoi",
    "mesh_nodes",
    "node_infiltration_fields",
    "publish_rain_on_grid_products",
    "resolve_rain_event",
    "solve_rain_on_grid",
    "write_rain_on_grid_deck",
]

_STEPS = "trid3nt_server.workflows.telemac.steps"

#: The names the run directory holds the catchment's files under. They are the
#: ``.cas``'s own GEOMETRY / BOUNDARY CONDITIONS / RESULTS / data-file statements,
#: so the steering file reads as the record of the run it is.
_STAGE_PREFIX = "telemac_rog"
_GEOMETRY_DEST = "rog.slf"
_BOUNDARY_DEST = "rog.cli"
_STEERING = "t2d_rog.cas"
_RESULT = "r2d_rog.slf"
_CN_MAP = "rog_cn_map.dat"
_FRICTION_LAWS = "rog_friction.tbl"
_ZONES_FILE = "rog_zones.dat"
_HYETOGRAPH = "rog_hyeto.txt"

#: Which telapy engine class runs a catchment, and the identity its row carries
#: in a run listing.
_MODULE = "telemac2d"
_FAMILY = "rain_on_grid"

#: The mesh boundary ROLE the outlet carries. TELEMAC prescribes a water level
#: with a free velocity there, which is the free exit a rain-fed catchment drains
#: through; the hydrograph is the flux across the nodes that took it.
_OUTLET_ROLE = "outflow"

#: Wall-clock ceiling on one rain-on-grid solve. A real catchment is tens of
#: thousands of elements over hours of simulated time at a 3 s step, which is an
#: HOURS-class solve - an order of magnitude past the open-water front's flat
#: hour. The number is a bound on the wait, not an estimate of the run: it exists
#: so a wedged container becomes a typed failure instead of a daemon that never
#: returns.
_SOLVE_TIMEOUT_S = 86400.0

#: Seconds in an hour, spelled once so no expression in this module spells it again.
_HOUR_S = 3600.0


class RainOnGridError(DeclarativeError):
    """A rain-on-grid catchment could not be acquired, staged, solved or read."""

    error_code = "TELEMAC_ROG_FAILED"


def catchment_aoi(pour_point: tuple[float, float],
                  half_deg: float) -> tuple[float, float, float, float]:
    """The analysis AOI a catchment is delineated inside, centred on its OUTLET.

    Centred on the outlet rather than on a geocoded place, because a place bbox
    names a TOWN and need not contain the UPSTREAM catchment. The delineation
    truncates at the box edge, so this must OVER-cover.
    """
    lon, lat = float(pour_point[0]), float(pour_point[1])
    b = float(half_deg)
    return (max(lon - b, -180.0), max(lat - b, -90.0),
            min(lon + b, 180.0), min(lat + b, 90.0))


def _domain_bbox(what: str) -> tuple[float, float, float, float]:
    """The extent a domain-implicit producer reads the world over."""
    from trid3nt_server.workflows.lib import current_domain

    domain = current_domain()
    if domain is None or domain.bbox is None:
        raise RainOnGridError(
            f"{what} cannot be fetched: no domain is bound. Resolve the AOI first.",
            error_code="TELEMAC_ROG_DOMAIN_UNBOUND")
    return tuple(float(v) for v in domain.bbox)  # type: ignore[return-value]


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
              else catchment_aoi(point, half_deg))
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
# 2. prep: what each node infiltrates and how rough it is.
# --------------------------------------------------------------------------- #
async def node_infiltration_fields(*, mesh: dict[str, Any],
                                   landcover: Any,
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

    The nodes are the ACCEPTED mesh's own, read off the artifact's display face:
    the field is written against the numbering the geometry file carries, so a
    curve number lands on the node it was sampled for.
    """
    from trid3nt_server.workflows.telemac.rain_on_grid.cn_infiltration import (
        amc_condition_for, landcover_cn_manning, node_curve_numbers,
    )

    def _sample() -> tuple[list[float], list[float], list[int]]:
        from trid3nt_server.tools.cache import read_object_bytes_s3

        points_utm, cells, bed, points_lonlat = mesh_nodes(mesh)
        rundir = Path(tempfile.mkdtemp(prefix="telemac-rog-cn-"))
        uri = str(layer_field(landcover, "uri"))
        local = rundir / "landcover.tif"
        local.write_bytes(read_object_bytes_s3(uri) if uri.startswith("s3://")
                          else Path(uri).read_bytes())
        codes = [int(round(v)) for v in
                 sample_raster_at_nodes(local, points_lonlat)]
        slopes = (list(node_slopes_from_mesh(points_utm, cells, bed))
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
            "note": str(layer_field(landcover, "fallback_note") or "")}


def mesh_nodes(mesh: Mapping[str, Any]) -> tuple[Any, Any, Any, Any]:
    """The accepted catchment mesh's nodes, or the refusal that names what is missing."""
    from trid3nt_server.workflows.mesh.shared.nodes import read_accepted_mesh_nodes

    utm_epsg = int(getattr(mesh.get("artifact"), "utm_epsg", 0) or 0)
    uri = str(mesh.get("display_uri") or "")
    if not uri or not utm_epsg:
        raise RainOnGridError(
            "the accepted mesh carries no display face or no projected zone, so "
            "its nodes cannot be read; the catchment mesh ask builds both.",
            error_code="TELEMAC_ROG_MESH_NOT_ACCEPTED")
    return read_accepted_mesh_nodes(uri, utm_epsg=utm_epsg)


# --------------------------------------------------------------------------- #
# 3. forcing: the rain that falls on it.
# --------------------------------------------------------------------------- #
def resolve_rain_event(*, window: str | None, intensity_mm_per_hr: float,
                       storm_duration_hr: float,
                       sim_duration_hr: float | None) -> dict[str, Any]:
    """The storm, as either a real hourly hyetograph or a constant design rate.

    A BRANCH ON THE ASK, not a fallback ladder: a dated ``window`` fetches the
    hourly AORC accumulation over the catchment and the run is driven by the REAL
    intensity structure, which is what resolves the hydrograph SHAPE. With no
    window the storm is a constant design rate over a declared duration - a
    hypothetical, and the returned ``note`` labels it as one.

    AORC rather than MRMS despite the argument's history: MRMS only covers
    ~2020-10 onward, and a replication window that predates it would silently
    return nothing.
    """
    from trid3nt_server.tools import TOOL_REGISTRY

    if not window:
        return {
            "kind": "design_storm", "blocks": None, "series": None,
            "intensity_mm_per_hr": float(intensity_mm_per_hr),
            "duration_s": float(sim_duration_hr if sim_duration_hr
                                else storm_duration_hr) * _HOUR_S,
            # How long it RAINS, as distinct from how long the run watches: a
            # window shorter than the run is what lets the recession limb appear.
            "rain_duration_s": float(storm_duration_hr) * _HOUR_S,
            "duration_basis": "user" if sim_duration_hr else "storm",
            "note": (f"a CONSTANT design storm of {float(intensity_mm_per_hr):g} mm/h "
                     f"over {float(storm_duration_hr):g} h - a hypothetical "
                     "event, not a record."),
        }
    bbox = _domain_bbox("the rain hyetograph")
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
                 f"{sum(mm):.3g} mm total."),
    }


# --------------------------------------------------------------------------- #
# 4. author: the steering file, the fields it names, and the case that runs it.
# --------------------------------------------------------------------------- #
def _mesh_field(mesh: Mapping[str, Any], name: str) -> str:
    """One field of the ACCEPTED mesh's record, or the refusal that names it."""
    uri = (mesh or {}).get(name)
    if not uri:
        raise RainOnGridError(
            f"the catchment mesh carries no {name}, so the accepted mesh cannot "
            f"be staged (mesh record: {sorted((mesh or {}))}).",
            error_code="TELEMAC_ROG_MESH_NOT_ACCEPTED")
    return str(uri)


def _outlet_boundary(mesh: Mapping[str, Any]) -> int:
    """WHICH numbered liquid boundary the declared OUTLET role is, 1-based.

    The solver numbers its liquid boundaries in the order the accepted topology
    recorded when the ``.cli`` was written, and it prints one flux per number in
    its own volume balance. So this is what turns "the outlet" into the series the
    hydrograph reads - the role, resolved against the numbering the engine uses.
    """
    from trid3nt_server.workflows.mesh.topology import read_topology

    topology = read_topology(_mesh_field(mesh, "topology_uri"))
    order = list(topology["liquid_boundary_order"])
    if _OUTLET_ROLE not in topology["roles"] or _OUTLET_ROLE not in order:
        raise RainOnGridError(
            f"no boundary node of the catchment mesh took the {_OUTLET_ROLE!r} "
            "role, so the basin has no outlet to drain through and no hydrograph "
            "to measure. Move the pour point onto the basin's own outlet, or mesh "
            "it finer so a boundary node reaches it.",
            error_code="TELEMAC_ROG_NO_OUTLET_NODES")
    return order.index(_OUTLET_ROLE) + 1


def _authoring_dir(run_tag: str) -> Path:
    rundir = Path(os.environ.get("TRID3NT_RUNS_DIR", "/tmp")) / f"telemac-{run_tag}"
    rundir.mkdir(parents=True, exist_ok=True)
    return rundir


async def write_rain_on_grid_deck(
    *,
    catchment: dict[str, Any],
    infiltration: dict[str, Any],
    rain: dict[str, Any],
    mesh_resolution_m: float | None = None,
    time_step_s: float,
    output_interval_min: float | None = None,
) -> dict[str, Any]:
    """Serialize the approved sheet into the run's own steering file + case.

    ``catchment`` is the ACCEPTED mesh: its geometry, its boundary conditions and
    the outlet role its pour point matched are what the solve runs on, so the deck
    is authored against the triangulation that was presented rather than an
    equivalent rebuild.

    The output CADENCE rides the deck in MINUTES and converts to a count of
    solver steps at the author, beside the keyword it is written into.
    """
    from trid3nt_server.workflows.telemac.rain_on_grid.cn_infiltration import (
        select_runoff_path,
    )

    from . import author
    from .open_water import case_section

    decision = (select_runoff_path(hyetograph_mm=rain["series"])
                if rain["kind"] == "hyetograph"
                else select_runoff_path(
                    constant_intensity_mm_per_hr=rain["intensity_mm_per_hr"]))
    time_varying = bool(decision.time_varying)

    artifact = catchment.get("artifact")
    utm_epsg = int(getattr(artifact, "utm_epsg", 0) or 0)
    probes = dict(getattr(artifact, "probes", None) or {})
    provenance = dict(catchment.get("provenance") or {})
    outlet_boundary = _outlet_boundary(catchment)
    mesh_size_m = float(catchment.get("min_edge_m") or mesh_resolution_m or 0.0)
    name = str(getattr(artifact, "name", None) or "watershed")

    deck: dict[str, Any] = {
        "name": name,
        "amc_condition": int(infiltration["amc_condition"]),
        "duration_s": float(rain["duration_s"]),
        "time_step_s": float(time_step_s),
        **({"output_interval_min": float(output_interval_min)}
           if output_interval_min is not None else {}),
    }
    if rain.get("rain_duration_s") is not None:
        deck["rain_duration_s"] = float(rain["rain_duration_s"])

    run_tag = new_ulid()
    rundir = _authoring_dir(run_tag)
    points_utm, _cells, _bed, _lonlat = await asyncio.to_thread(mesh_nodes, catchment)

    def _author() -> dict[str, Any]:
        author.write_cn_map(rundir, _CN_MAP, x=points_utm[:, 0], y=points_utm[:, 1],
                            cn2=infiltration["node_cn2"])
        friction = author.write_friction_files(
            rundir, laws_basename=_FRICTION_LAWS, zones_basename=_ZONES_FILE,
            manning_per_node=infiltration["node_manning"])
        hyeto = None
        if time_varying:
            friction.update(author.write_hyetograph_file(
                rundir, _HYETOGRAPH, blocks=rain["blocks"],
                duration_s=float(rain["duration_s"])))
            hyeto = _HYETOGRAPH
        author.author_rog_deck(
            rundir, deck=deck, geometry=_GEOMETRY_DEST, boundary=_BOUNDARY_DEST,
            results=_RESULT, cas_name=_STEERING, cn_map=_CN_MAP,
            friction_laws=_FRICTION_LAWS, zones_file=_ZONES_FILE,
            rain_mm_per_day=float(rain["intensity_mm_per_hr"]) * 24.0,
            runoff_path="native", hyetograph_file=hyeto)
        return friction

    authored_stats = await asyncio.to_thread(_author)
    authored = sorted(str(p.relative_to(rundir))
                      for p in rundir.rglob("*") if p.is_file())
    logger.info("rog deck authored: %s path=%s outlet_boundary=%d files=%s",
                _STEERING, decision.path, outlet_boundary, authored)

    return {
        "deck": deck,
        "run_tag": run_tag,
        "rundir": str(rundir),
        "case": case_section(
            module=_MODULE, steering=_STEERING, results=[_RESULT], family=_FAMILY,
            # The engine reaches RAINDEF=3 only through the user Fortran the
            # image bakes, so the run that needs it names it on both channels.
            user_fortran=author.RAINDEF3_USER_FORTRAN if time_varying else None,
            echo={"utm_epsg": utm_epsg,
                  "bbox": [round(float(v), 6)
                           for v in (getattr(artifact, "bbox", None) or ())],
                  "npoin": int(catchment.get("node_count") or 0),
                  "nelem": int(catchment.get("element_count") or 0),
                  "mesh_size_m": mesh_size_m,
                  # WHICH file carries the time series. The author wrote the
                  # RESULTS FILE statement, so the name is the server's; the
                  # worker copies it and measures the file it names.
                  "result_slf": _RESULT,
                  "bed_source": str(provenance.get("bed_source") or "staged")}),
        "outputs": [_RESULT, _GEOMETRY_DEST, _BOUNDARY_DEST, "full_listing.log",
                    "telemac_metrics.json", *authored],
        "authored": authored,
        "result_basename": _RESULT,
        "outlet_boundary": outlet_boundary,
        "catchment": catchment,
        "infiltration": infiltration,
        "rain": rain,
        "runoff_path": decision.path,
        "runoff_reason": decision.reason,
        "hyetograph_total_mm": authored_stats.get("hyetograph_total_mm"),
        "mesh_size_m": mesh_size_m,
        "mesh_max_edge_m": float((probes.get("edge_length_m") or {}).get("max") or 0.0),
        "area_km2": float(probes.get("area_km2") or 0.0),
        "lonlat_bounds": [float(v) for v in (getattr(artifact, "bbox", None) or ())],
        "mesh_resolution_asked_m": mesh_resolution_m,
        "domain_name": name,
        "utm_epsg": utm_epsg,
        "bed_source": str(provenance.get("bed_source") or "staged"),
        "bed_note": str(provenance.get("bed_fallback_note") or ""),
        "sizing_source": str(provenance.get("sizing_source") or ""),
        "domain_source": str(provenance.get("domain_source") or ""),
    }


# --------------------------------------------------------------------------- #
# 5. solve.
# --------------------------------------------------------------------------- #
def _stage_inputs(deck: dict[str, Any]) -> str:
    """Upload the mesh pair + the authored deck; return the manifest ``s3://`` URI."""
    from trid3nt_server.workflows.solver.solver import _get_s3_client

    bucket = (os.environ.get("TRID3NT_CACHE_BUCKET") or "").strip()
    if not bucket:
        raise RainOnGridError(
            "TRID3NT_CACHE_BUCKET must be set to stage the rain-on-grid inputs.",
            error_code="TELEMAC_ROG_STAGING_FAILED")
    catchment, run_tag = deck["catchment"], deck["run_tag"]
    rundir = Path(deck["rundir"])
    s3 = _get_s3_client()
    inputs = [
        {"gs_uri": _mesh_field(catchment, "slf_uri"), "dest": _GEOMETRY_DEST},
        {"gs_uri": _mesh_field(catchment, "cli_uri"), "dest": _BOUNDARY_DEST},
    ]
    for name in deck["authored"]:
        key = f"{_STAGE_PREFIX}/{run_tag}/{name}"
        s3.put_object(Bucket=bucket, Key=key, Body=(rundir / name).read_bytes())
        inputs.append({"gs_uri": f"s3://{bucket}/{key}", "dest": name})

    from .open_water import OpenWaterError, stage_telemac_manifest

    try:
        return stage_telemac_manifest(
            section="case", config=deck["case"], run_tag=run_tag,
            outputs=deck["outputs"], inputs=inputs, prefix=_STAGE_PREFIX)
    except OpenWaterError as exc:
        raise RainOnGridError(str(exc),
                              error_code="TELEMAC_ROG_STAGING_FAILED") from exc


async def solve_rain_on_grid(*, deck: dict[str, Any],
                             compute_class: str = "medium") -> dict[str, Any]:
    """Stage the deck, dispatch the authored case to the worker, and wait.

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
    logger.info("rog staged case run_tag=%s name=%s steering=%s -> %s",
                deck["run_tag"], deck["domain_name"], deck["case"]["steering"],
                manifest_uri)

    run_result, batch_run_id = await dispatch_and_wait(
        solver=_solver_name(), manifest_uri=manifest_uri,
        compute_class=compute_class, label=_FAMILY, timeout_s=_SOLVE_TIMEOUT_S,
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
# 6. publish.
# --------------------------------------------------------------------------- #
def _read_listing(run_id: str) -> str:
    """The solver listing the supervisor uploaded; ``""`` on any miss.

    Best-effort by the products contract: the closure the engine printed is a
    scalar the answer carries, never the reason a solved run has no layer.
    """
    from trid3nt_server.workflows.solver.solver import _get_runs_bucket, _get_s3_client

    try:
        body = _get_s3_client().get_object(
            Bucket=_get_runs_bucket(), Key=f"{run_id}/full_listing.log")["Body"].read()
        return body.decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001 - a missing listing costs one scalar
        logger.info("rog: listing unreadable for %s: %s", run_id, exc)
        return ""


def _rainfall_volume_m3(deck: Mapping[str, Any]) -> float | None:
    """What FELL on the catchment, in cubic metres, or ``None`` when unmeasured.

    Gross depth times the meshed area - the same area the runoff left through -
    so the coefficient is a ratio of two figures measured on one domain.
    """
    rain, area_km2 = deck["rain"], float(deck.get("area_km2") or 0.0)
    total_mm = (deck.get("hyetograph_total_mm") if rain["kind"] == "hyetograph"
                else float(rain["intensity_mm_per_hr"])
                * float(rain.get("rain_duration_s") or rain["duration_s"]) / _HOUR_S)
    if not area_km2 or total_mm is None:
        return None
    return round(float(total_mm) * 1.0e-3 * area_km2 * 1.0e6, 3)


def _provenance(deck: Mapping[str, Any]) -> list[SyntheticInput]:
    """The physically dominant inputs, as rows the layer carries.

    Which STORM drove it, which infiltration path ran, where the bed came from and
    whether the mesh was generated or handed in - every one of them a fact the
    answer is meaningless without, so each is stated rather than assumed.
    """
    infiltration, rain = deck["infiltration"], deck["rain"]
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
            value=f"{deck['catchment'].get('element_count') or 0} elements over "
                  f"{float(deck.get('area_km2') or 0.0):.3g} km2",
            basis="derived", consequence="numerical",
            real_source_if_any=str(deck.get("domain_source") or "") or None,
            note=("the catchment was delineated at the pour point and meshed for "
                  "this run")),
    ]
    if infiltration["curve_number"] is not None:
        rows.append(SyntheticInput(
            param="curve_number", value=float(infiltration["curve_number"]),
            basis="user", consequence="physics",
            note=("a UNIFORM curve number overriding the land-cover-distributed "
                  "field; roughness is still per-node")))
    rows.append(SyntheticInput(
        param="mesh_bed", value=str(deck.get("bed_source") or "staged"),
        basis="fetched", consequence="physics",
        real_source_if_any="USGS 3DEP",
        note=str(deck.get("bed_note") or
                 "the bare-earth bed the mesher painted every node from")))
    if deck.get("sizing_source"):
        rows.append(SyntheticInput(
            param="mesh_sizing_source", value=str(deck["sizing_source"]),
            basis="fetched", consequence="numerical",
            note="the channel network the mesh refinement was sized by distance to"))
    return rows


def _honesty_note(deck: Mapping[str, Any], metrics: Mapping[str, Any],
                  product_note: str | None, truncated: bool = False) -> str:
    """What the RUN was, prefixed by what the LAYER is.

    The applicability envelope is part of the sentence, not a footnote: rain-on-
    grid reproduces single-storm flash floods in small steep catchments and does
    NOT carry baseflow, because infiltrated water is permanently lost.

    A hydrograph still rising at the last sample gets its own sentence, because
    every number the run reports about the storm is then a floor rather than a
    measurement, and that is not a caveat a reader should have to derive from a
    time series.
    """
    rain = deck["rain"]
    spacing = metrics.get("mesh_size_m") or deck["mesh_size_m"]
    truncation = (
        " WINDOW-TRUNCATED: the outlet discharge was still RISING when the "
        "simulated window closed, so the peak, the runoff volume and the runoff "
        "coefficient are LOWER BOUNDS - simulate past the storm to close them."
        if truncated else "")
    return (
        (f"{product_note} " if product_note else "")
        + "Planning-grade rainfall-runoff SCREENING: TELEMAC-2D shallow water over a "
        f"{float(deck.get('area_km2') or 0.0):.3g} km2 catchment delineated at the "
        f"pour point and triangulated at {float(spacing):g} m minimum edge "
        f"({deck['catchment'].get('element_count') or 0} elements), infiltrating by "
        "the SCS curve-number method with per-node curve numbers from land cover. "
        "Driven by "
        + str(rain["note"]).rstrip(".").split(" - ")[0]
        + ". The raster is the peak water DEPTH envelope over the run; the animation "
        "plays from the native rain-on-grid SELAFIN. Single-storm events only: "
        "infiltrated water is permanently lost, so there is no subsurface return "
        "flow and no inter-peak baseflow. Not a calibrated rainfall-runoff model."
        + truncation)


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

    from .open_water import download_open_water_result
    from .run_reads import continuity_rel_error, outlet_hydrograph

    emitter = current_emitter()
    run_id, utm_epsg = solve["run_id"], int(solve["utm_epsg"])
    metrics = dict(solve.get("metrics") or {})
    catchment = deck["catchment"]
    name = str(deck["domain_name"])

    slf_path = await asyncio.to_thread(
        download_open_water_result, run_id, deck["result_basename"],
        error_code="TELEMAC_ROG_OUTPUT_MISSING")
    try:
        layers, pmetrics = await asyncio.to_thread(
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

    listing = await asyncio.to_thread(_read_listing, run_id)
    # The ANSWER is the hydrograph, and the ENGINE measured it: the flux across
    # the declared outlet is part of the solver's own water-volume balance, so the
    # run is narrated from the number it printed rather than from a second
    # integral computed over its output fields.
    hydrograph = await asyncio.to_thread(
        outlet_hydrograph, listing, boundary=deck["outlet_boundary"])
    rainfall = _rainfall_volume_m3(deck)
    runoff = hydrograph.get("runoff_volume_m3")
    scalars: dict[str, Any] = {
        "catchment_area_km2": round(float(deck.get("area_km2") or 0.0), 4),
        "peak_discharge_m3s": hydrograph.get("peak_discharge_m3s"),
        "peak_discharge_time_s": hydrograph.get("peak_discharge_time_s"),
        "peak_is_window_truncated": hydrograph.get("peak_is_window_truncated"),
        "rainfall_volume_m3": rainfall,
        "runoff_volume_m3": runoff,
        # A ratio, not a percentage, and only when there was rain to divide by:
        # a runoff coefficient over zero rainfall is a number with no meaning.
        "runoff_coefficient": (round(float(runoff) / float(rainfall), 6)
                               if rainfall and runoff is not None
                               and float(rainfall) > 0.0 else None),
        "max_depth_peak_m": pmetrics.get("wse_max_m"),
        "max_depth_p99_m": pmetrics.get("wse_p99_m"),
        "continuity_rel_error": continuity_rel_error(listing),
        "runoff_path": deck["runoff_path"],
        "amc_condition": int(deck["infiltration"]["amc_condition"]),
        "rain_intensity_mm_per_hr": float(deck["rain"]["intensity_mm_per_hr"]),
        "outlet_hydrograph_t_s": list(hydrograph.get("t_s") or ()) or None,
        "outlet_hydrograph_q_m3s": list(hydrograph.get("q_m3s") or ()) or None,
        "mesh_node_count": int(catchment.get("node_count") or 0) or None,
        "mesh_element_count": int(catchment.get("element_count") or 0) or None,
        "mesh_size_m": float(deck["mesh_size_m"]),
        "mesh_resolution_label": (
            f"catchment TIN, {float(deck['mesh_size_m']):g} m minimum edge to "
            f"{float(deck['mesh_max_edge_m']):g} m, refined toward the channel "
            f"network ({catchment.get('element_count') or 0} elements)"),
        "catchment_provenance": str(deck.get("domain_source") or ""),
        "catchment_name": name,
        "domain_bbox": [float(v) for v in deck["lonlat_bounds"]],
    }
    typed = TelemacRainOnGridLayerURI(**raw.model_dump(), **scalars)
    published = await publish_product_layer(
        typed, style_preset=TELEMAC_WSE_STYLE_PRESET,
        update={
            # The published raster is in the mesh's UTM metres, so the postprocess
            # leaves it without a zoom-to extent; the DOMAIN's own 4326 bounds are
            # known here and the camera follows the domain.
            "bbox": tuple(deck["lonlat_bounds"]),
            "fallback_note": _honesty_note(
                deck, metrics, raw.fallback_note,
                truncated=bool(scalars["peak_is_window_truncated"])),
            "synthetic_inputs": _provenance(deck),
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

    logger.info("rog complete run_id=%s catchment=%s area=%.4g km2 outlet_boundary=%d "
                "peak_q=%s peak_depth=%s continuity=%s uri=%s", run_id, name,
                float(deck.get("area_km2") or 0.0), deck["outlet_boundary"],
                published.peak_discharge_m3s, published.max_depth_peak_m,
                published.continuity_rel_error, published.uri)
    return published


# --------------------------------------------------------------------------- #
# The step constructors, as the facade and the template bind them.
# --------------------------------------------------------------------------- #
class Infiltration:
    """The catchment's node fields, as a declared step."""

    @staticmethod
    def fields(*, mesh: Any, landcover: Any, curve_number: Any,
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
