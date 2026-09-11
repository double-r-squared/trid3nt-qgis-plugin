"""The accepted mesh -> what the sheet is FILLED from, and the run the box gets.

SETTLING measures off the artifact itself, never beside it. STAGING uploads every
authored file beside the mesh and writes the manifest LAST, so a manifest exists
only for a run whose every file is already where the launcher will look."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from trid3nt_contracts import new_ulid

from trid3nt_server.workflows.runtime import journal_note
from trid3nt_server.workflows.mesh.shared.nodes import (
    accepted_mesh_nodes,
    read_accepted_mesh_nodes,
    read_centerline_utm,
    sample_layer_at_nodes,
)
from trid3nt_server.workflows.mesh.topology import RATING_CURVE_ROLE, read_topology

from trid3nt_server.workflows.inputs.layer_fields import layer_field
from trid3nt_server.workflows.inputs.point import (
    Point,
    as_utm,
    contain,
    publish_point,
    snap_to_wet,
)

from ..errors import TelemacError
from ..helpers.time_step import MESH_H_FLOOR_M, suggest_time_step_s
from ..helpers.uniform_flow import normal_depth_stage

logger = logging.getLogger("trid3nt_server.workflows.telemac.authoring.assembler")

__all__ = ["BASIN_BOUNDARY", "BASIN_GEOMETRY", "HARBOUR_GEOMETRY",
           "case_section", "mesh_nodes", "new_rundir", "settle_basin",
           "settle_catchment", "settle_harbour", "settle_reach",
           "settle_release", "stage_run", "stage_telemac_manifest", "to_utm"]

#: The names the run directory holds an open-water domain's staged geometry
#: under - the decks' own GEOMETRY / BOUNDARY CONDITIONS statements. A harbour
#: stages the geometry alone: its boundary file is RESTAMPED as the incident wave
#: and is written beside the deck rather than staged from the mesh.
HARBOUR_GEOMETRY = "harbour.slf"
BASIN_GEOMETRY = "basin.slf"
BASIN_BOUNDARY = "basin.cli"

#: What a continued run's PREVIOUS COMPUTATION FILE is called in the run
#: directory. The engine reads a file, not a URI, so the previous run's restart
#: record is staged under one name and the steering file names that.
_PREVIOUS_DEST = "previous.slf"
#: The engine's perfect-restart record, under the name the reach body writes it.
_RESTART = "restart_river.slf"
#: How a restart record names its depth, and the depth a node has to hold at
#: that instant to count as water a source can be released into.
_DEPTH_VARIABLE = ("WATER DEPTH", "HAUTEUR D'EAU", "HAUTEUR D EAU")
_WET_DEPTH_M = 0.01

#: The mesh boundary ROLE a catchment's outlet carries. Its quad prescribes a
#: LEVEL and the level comes from the run's own derived stage-discharge curve, so
#: the outlet stands where the flow leaving it says it stands rather than at a
#: constant nobody measured. The hydrograph is the flux across the nodes that
#: took the role.
_OUTLET_ROLE = RATING_CURVE_ROLE

#: The bottom-friction law a catchment is solved under, which is also the law its
#: outlet's rating curve is derived at. It is the deck's own LAW OF BOTTOM
#: FRICTION: a curve derived under a roughness the run is not solved at is a
#: level the run never sits at.
_ROG_FRICTION_LAW = 4

#: The friction the reach is solved at when the sheet states none, as the law and
#: the Strickler coefficient the deck writes. Named once because the outflow
#: stage is a normal depth AT this roughness: a stage derived at one number and a
#: file written at another is a level the run never sits at.
_REACH_FRICTION_LAW = 3
_REACH_STRICKLER = 33.0

#: How many solver steps between written frames when the sheet states no cadence.
_DEFAULT_GRAPHIC_PERIOD = 200


def case_section(*, module: str, steering: str, results: list[str],
                 server_facts: Mapping[str, Any], user_fortran: str | None = None,
                 coupling: str | None = None,
                 continue_from: str | None = None) -> dict[str, Any]:
    """The CASE a worker runs: which engine, which file, what it must produce.

    ``server_facts`` is copied into the worker's metrics verbatim, never re-derived."""
    return {"module": module, "steering": steering,
            **({"user_fortran": user_fortran} if user_fortran else {}),
            **({"coupling": coupling} if coupling else {}),
            **({"continue_from": continue_from} if continue_from else {}),
            "results": list(results), "server_facts": dict(server_facts)}


# ``inputs`` rows are ``{gs_uri, dest}``: what the launcher stages into the run
# directory before the container starts, which is why the worker needs no network.
# An authored run's section is ``case``.
def stage_telemac_manifest(*, section: str, config: Mapping[str, Any],
                           run_tag: str, outputs: list[str],
                           inputs: list[dict[str, str]] | None = None,
                           prefix: str | None = None,
                           extra: Mapping[str, Any] | None = None) -> str:
    """Write the worker manifest to the cache bucket -> its ``s3://`` URI.

    ``section`` is the dispatch key, ``prefix`` the staging word; they differ."""
    cache_bucket = (os.environ.get("TRID3NT_CACHE_BUCKET") or "").strip()
    if not cache_bucket:
        raise TelemacError(
            "TRID3NT_CACHE_BUCKET must be set to stage the TELEMAC manifest.",
            error_code="TELEMAC_STAGING_FAILED")
    from trid3nt_server.workflows.solver.solver import _get_s3_client

    manifest = {section: dict(config), "run_id": run_tag,
                "inputs": list(inputs or []), "telemac_args": [],
                "outputs": list(outputs), **dict(extra or {})}
    key = f"{prefix or section}/{run_tag}/manifest.json"
    _get_s3_client().put_object(
        Bucket=cache_bucket, Key=key,
        Body=json.dumps(manifest, indent=2).encode("utf-8"),
        ContentType="application/json")
    return f"s3://{cache_bucket}/{key}"


def _run_directory(run_tag: str) -> Path:
    rundir = Path(os.environ.get("TRID3NT_RUNS_DIR", "/tmp")) / f"telemac-{run_tag}"
    rundir.mkdir(parents=True, exist_ok=True)
    return rundir


def _cache_bucket() -> str:
    bucket = (os.environ.get("TRID3NT_CACHE_BUCKET") or "").strip()
    if not bucket:
        raise TelemacError("TRID3NT_CACHE_BUCKET must be set to stage an authored run.",
                           error_code="TELEMAC_STAGING_FAILED")
    return bucket


def _upload_authored(rundir: Path, run_tag: str, names: Sequence[str],
                     prefix: str) -> list[dict[str, str]]:
    """Upload every file this authoring wrote -> the manifest rows staging them."""
    from trid3nt_server.workflows.solver.solver import _get_s3_client

    bucket = _cache_bucket()
    s3 = _get_s3_client()
    rows: list[dict[str, str]] = []
    for name in names:
        key = f"{prefix}/{run_tag}/{name}"
        s3.put_object(Bucket=bucket, Key=key, Body=(rundir / name).read_bytes())
        rows.append({"gs_uri": f"s3://{bucket}/{key}", "dest": name})
    return rows


def _write_manifest(case: Mapping[str, Any], run_tag: str, *, outputs: list[str],
                    inputs: list[dict[str, str]], prefix: str) -> str:
    """Write the worker manifest for an authored case -> its ``s3://`` URI.

    Written by the one manifest writer, under the ``case`` dispatch key."""
    return stage_telemac_manifest(
        section="case", config=case, run_tag=run_tag, outputs=outputs,
        inputs=inputs, prefix=prefix)


def new_rundir() -> tuple[str, Path]:
    """A fresh run tag and the directory the run is authored into."""
    run_tag = new_ulid()
    return run_tag, _run_directory(run_tag)


async def stage_run(rundir: Path, run_tag: str, *, module: str, steering: str,
                    results: list[str], outputs: list[str],
                    mesh_inputs: list[dict[str, str]], prefix: str,
                    sheet: Mapping[str, Any], server_facts: Mapping[str, Any],
                    result_basename: str, user_fortran: str | None = None,
                    coupling: str | None = None,
                    continue_from: str | None = None) -> dict[str, Any]:
    """An authored run directory -> the staged run the box receives.

    The manifest is written LAST, so it exists only for a fully staged run."""
    # Every file the authoring wrote, under its path INSIDE the run directory:
    # the oil module's user fortran is a directory the engine compiles, so the
    # walk is recursive and the manifest dest carries the same relative path.
    authored = sorted(str(p.relative_to(rundir))
                      for p in rundir.rglob("*") if p.is_file())
    inputs = [*mesh_inputs,
              *await asyncio.to_thread(_upload_authored, rundir, run_tag,
                                       authored, prefix)]
    case = case_section(
        module=module, steering=steering, results=results,
        user_fortran=user_fortran, coupling=coupling,
        continue_from=continue_from, server_facts=server_facts)
    manifest_uri = await asyncio.to_thread(
        _write_manifest, case, run_tag, outputs=outputs, inputs=inputs,
        prefix=prefix)
    return {"run_tag": run_tag, "rundir": str(rundir), "sheet": dict(sheet),
            "case": case, "manifest_uri": manifest_uri, "authored": authored,
            "outputs": outputs, "inputs": inputs,
            "result_basename": result_basename}


def _mesh_field(mesh: Mapping[str, Any], name: str, *,
                missing: Callable[[str], Exception]) -> str:
    """One field of the ACCEPTED mesh's record, or the refusal that names it.

    Falling through would solve on a mesh nobody accepted, under its name."""
    uri = (mesh or {}).get(name)
    if not uri:
        raise missing(
            f"the mesh for this run carries no {name}, so the accepted mesh "
            f"cannot be staged (mesh record: {sorted((mesh or {}))}).")
    return str(uri)


def _reach_section_unmeasured(message: str) -> Exception:
    return TelemacError(message, error_code="TELEMAC_MESH_SECTION_UNMEASURED")


def _outlet_section_unmeasured(message: str) -> Exception:
    return TelemacError(message, error_code="TELEMAC_OUTLET_SECTION_UNMEASURED")


def _mesh_missing(message: str) -> Exception:
    return TelemacError(message, error_code="TELEMAC_MESH_NOT_ACCEPTED")


def _face_section(nodes: Sequence[int], node_xy: Any, bed: Any, *,
                  missing: Callable[[str], Exception]) -> list[list[float]]:
    """The channel a role's face cuts, as ``(offset, bed)`` pairs.

    A role is a contiguous run of the walk, so the offset is running chord."""
    import numpy as np

    xy = None if node_xy is None else np.asarray(node_xy, dtype=float)
    if xy is None or len(nodes) < 2 or max(nodes) >= xy.shape[0]:
        raise missing(
            f"a uniform-flow depth is derived over the channel the face cuts, "
            f"and that face names {len(nodes)} node(s) against "
            f"{0 if xy is None else xy.shape[0]} mesh coordinates; a mesh recipe "
            "names its faces with set_boundary_roles.")
    points = xy[list(nodes)]
    steps = np.hypot(*(points[1:] - points[:-1]).T)
    offsets = np.concatenate([[0.0], np.cumsum(steps)])
    # A node the bed left unpainted drops out of the section and takes no offset
    # with it: the survey has a hole in it, and closing the hole by shifting the
    # nodes past it would narrow a channel nobody re-measured.
    section = [[round(float(o), 3), round(float(z), 3)]
               for o, z in zip(offsets, bed[list(nodes)]) if np.isfinite(z)]
    if len(section) < 2:
        raise missing(f"the face carries {len(section)} painted node(s), which is "
                      "no section to derive a normal depth over.")
    return section


def _measured_reach(roles: Mapping[str, Any], node_xy: Any, node_bed: Any,
                    centerline_utm: Any) -> dict[str, Any]:
    """What the accepted mesh says about the reach the outflow stage rests on.

    Bed, outflow section and reach length, every one off the artifact itself."""
    import numpy as np

    # The two bed numbers are medians over the nodes each role names - the reach's
    # top, and the fall from there to its outflow. That fall over the centerline's
    # length is the friction slope the normal depth is computed at.
    bed = None if node_bed is None else np.asarray(node_bed, dtype=float)
    medians: dict[str, float] = {}
    role_nodes: dict[str, list[int]] = {}
    for role in ("inflow", "outflow"):
        nodes = [int(n) for n in ((roles or {}).get(role) or ())]
        if bed is None or not nodes or max(nodes) >= bed.shape[0]:
            raise TelemacError(
                f"the outflow stage is derived over the painted bed at the "
                f"{role!r} role's own nodes, and the accepted mesh carries "
                f"{0 if bed is None else bed.shape[0]} bed values under roles "
                f"{sorted(roles or {})}; a reach mesh recipe paints its bed with "
                "set_bed and names its faces with set_boundary_roles.",
                error_code="TELEMAC_MESH_BED_UNMEASURED")
        median = float(np.nanmedian(bed[nodes]))
        if not np.isfinite(median):
            raise TelemacError(
                f"every node the {role!r} role names carries an unpainted bed, so "
                "the outflow stage has no ground to be measured from.",
                error_code="TELEMAC_MESH_BED_UNMEASURED")
        medians[role] = median
        role_nodes[role] = nodes
    line = np.asarray(centerline_utm, dtype=float)
    length = (float(np.hypot(*(line[1:] - line[:-1]).T).sum())
              if line.ndim == 2 and len(line) > 1 else 0.0)
    return {"bed_top_m": medians["inflow"],
            "bed_drop_m": medians["inflow"] - medians["outflow"],
            "reach_length_m": round(length, 3),
            "outflow_section": _face_section(role_nodes["outflow"], node_xy, bed,
                                             missing=_reach_section_unmeasured)}


def _continuation_state(uri: str) -> dict[str, Any]:
    """The restart record's own last instant and depth field - read off the file.

    A continued run is the same declared scenario over an extended horizon."""
    import tempfile

    import numpy as np

    from trid3nt_server.workflows.solver.solver import _download_object
    from trid3nt_server.workflows.telemac.modules.outputs import read_selafin

    with tempfile.TemporaryDirectory(prefix="telemac-continue-") as tmp:
        path = Path(tmp) / _PREVIOUS_DEST
        _download_object(str(uri), path)
        record = read_selafin(path)
    if len(record["times"]) == 0:
        raise TelemacError(
            f"{uri} holds no time record, so there is no state to continue from "
            "and no instant to continue the scenario at. Point continue_from at "
            f"a completed run's {_RESTART}.",
            error_code="TELEMAC_CONTINUATION_UNREADABLE")
    depth = next((record["data"][name] for name in record["varnames"]
                  if name.strip().upper() in _DEPTH_VARIABLE), None)
    if depth is None:
        raise TelemacError(
            f"{uri} carries no water depth among {record['varnames']}, so the "
            "state this run would start from cannot say where it is wet.",
            error_code="TELEMAC_CONTINUATION_UNREADABLE")
    # Only the file can say where the continued leg stopped: the engine writes the
    # restart at its own last time step, which is not the graphic period, not the
    # asked duration, and not anything the server can compute from the ask. The
    # depth at that instant is the initial state, which decides where a release
    # can land.
    start_s = float(record["times"][-1])
    wet = np.asarray(depth[-1], dtype=float) > _WET_DEPTH_M
    return {
        "start_s": start_s, "wet": wet,
        "note": (f"the restart record this run continues from, at t={start_s:.0f} s: "
                 f"{int(wet.sum())} of {record['npoin']} nodes clear the "
                 f"{_WET_DEPTH_M} m wet floor"),
    }


def _initial_state(continue_from: str | None, node_count: int) -> dict[str, Any]:
    """WHAT THE RUN STARTS FROM: a fresh reach opens at the derived normal depth
    laid bed-parallel, a positive depth at every node; a CONTINUED one opens at
    the restart record's own wet/dry field and the instant it stands at."""
    if continue_from:
        return _continuation_state(str(continue_from))
    return {"start_s": None, "wet": [True] * node_count,
            "note": "the deck's own constant initial depth, the derived normal "
                    "depth laid bed-parallel over every node of the accepted mesh"}


def to_utm(source: Any, utm_epsg: int) -> Any:
    """A lon/lat geometry source -> its shapely geometry in the mesh's metres."""
    from pyproj import Transformer
    from shapely.geometry import shape as _shape
    from shapely.ops import transform as _transform, unary_union

    from trid3nt_server.workflows.inputs.geometry import (
        flatten_geometries, read_geometry_doc,
    )

    geometry = unary_union([_shape(g)
                            for g in flatten_geometries(read_geometry_doc(source))])
    tr = Transformer.from_crs(4326, int(utm_epsg), always_xy=True)
    return _transform(tr.transform, geometry)


def _to_lonlat_point(xy: tuple[float, float],
                     utm_epsg: int) -> tuple[float, float]:
    """One point in the mesh's own metres back in lon/lat."""
    from pyproj import Transformer

    lon, lat = Transformer.from_crs(int(utm_epsg), 4326, always_xy=True).transform(
        float(xy[0]), float(xy[1]))
    return (float(lon), float(lat))


def mesh_nodes(mesh: Mapping[str, Any]) -> tuple[Any, Any]:
    """The accepted mesh's node coordinates and bed, read off its display face.

    The ``.2dm`` is the one readable record of the geometry file's numbering."""
    points, _cells, z, _lonlat = read_accepted_mesh_nodes(
        _mesh_field(mesh, "display_uri", missing=_mesh_missing))
    if points is None or z is None:
        raise TelemacError(
            "the accepted mesh's display face carries no nodes or no painted bed, "
            "so this run has no ground to measure an outflow stage over and "
            "nowhere to settle a source; a reach mesh recipe paints its bed with "
            "set_bed.", error_code="TELEMAC_MESH_BED_UNMEASURED")
    return points, z


def _domain_polygon(artifact: Any) -> Any:
    """The polygon the accepted mesh was cut from, or a typed refusal.

    A mesh cut from a bbox carries no polygon and is refused, never approximated."""
    # The mesh records the RECIPE it was built from, so the domain a containment
    # test runs against is the mesh's own statement of it rather than a second
    # resolution of the same question. A point tested against four numbers is a
    # point nobody tested.
    recipe = ((getattr(artifact, "provenance", None) or {}).get("recipe") or {})
    extent = recipe.get("extent")
    if isinstance(extent, (tuple, list)) or extent is None:
        raise TelemacError(
            f"the accepted mesh for this run was cut from {extent!r} rather than "
            "from a domain polygon, so there is no mapped shape a source point "
            "could be inside of. A reach is meshed from the sectioned water "
            "polygon; solve on a mesh built that way.",
            error_code="TELEMAC_DOMAIN_NOT_A_POLYGON")
    return extent


def _station_on_mesh(*, centerline_utm: Any, mesh: Any,
                     fraction: float) -> tuple[tuple[float, float], str | None]:
    """A DERIVED source: ``fraction`` along the centerline, inside the mesh.

    Returns ``((lon, lat), note)`` in EPSG:4326, the note ``None`` if unmoved."""
    # The centerline is the whole navigated stretch; the accepted mesh is only the
    # part of it the mapped banks and the cleanup left, so a station on the line is
    # not a station in the domain. A source the solver cannot find an element for
    # stops the run at startup with nothing but "SOURCE POINT OUTSIDE DOMAIN", so
    # the station is walked DOWNSTREAM to the first one the triangulation holds and
    # the distance it travelled is said out loud.
    import numpy as np
    import shapely
    from pyproj import Transformer
    from shapely.geometry import LineString

    from trid3nt_server.workflows.mesh.shared.nodes import read_accepted_mesh_nodes

    utm_epsg = int(getattr(mesh.get("artifact"), "utm_epsg", 0) or 0)
    display_uri = str(mesh.get("display_uri") or "")
    line = LineString(centerline_utm)
    frac = min(max(float(fraction), 0.0), 1.0)
    start = frac * line.length
    back = Transformer.from_crs(int(utm_epsg or 4326), 4326, always_xy=True)

    points_utm, cells, _bed, _lonlat = read_accepted_mesh_nodes(
        display_uri, utm_epsg=utm_epsg)
    rings = np.asarray(points_utm, dtype=float)[np.asarray(cells, dtype=np.int64)]
    tree = shapely.STRtree(
        shapely.polygons(np.concatenate([rings, rings[:, :1]], axis=1)))
    # A cell-length stride: finer than that resolves nothing the mesh can hold,
    # coarser than that could step over a short meshed stretch entirely.
    step = max(float(line.length) / 2000.0, 1.0)
    walked = 0.0
    while start + walked <= line.length:
        here = line.interpolate(start + walked)
        if tree.query(here, predicate="intersects").size:
            lon, lat = back.transform(here.x, here.y)
            note = (None if walked <= 0.0 else
                    f"derived station moved {walked:.0f} m downstream to the "
                    "first point the accepted mesh holds")
            if note:
                logger.info("derived source walked %.0f m downstream into the "
                            "meshed reach", walked)
            return (float(lon), float(lat)), note
        walked += step
    raise TelemacError(
        f"no point on the centerline at or below {frac:.0%} of its length lies "
        "inside the accepted mesh, so there is nowhere in the solved domain to "
        "put the source. Mesh more of the reach (a finer mesh_resolution_m or a "
        "supplied mesh) or place the point explicitly.",
        error_code="TELEMAC_SOURCE_OFF_MESH")


async def _settle_release(
    point: Point | None, *, mesh: dict[str, Any],
    centerline: Any, centerline_utm: Any, utm_epsg: int, fraction: float,
    node_xy: Any, initial_state: Mapping[str, Any],
) -> tuple[Point, str]:
    """WHERE the source enters the water -> the settled Point and how it was decided.

    A supplied point the domain polygon does not hold raises rather than moves."""
    # With no point placed the source sits at ``fraction`` along the declared
    # centerline, walked downstream to the first station the ACCEPTED MESH holds:
    # the centerline is the whole navigated stretch and the mesh is only the part
    # of it the mapped banks left, so "on the line" and "in the domain" are two
    # different claims and only the second one solves.
    if point is None:
        (lon, lat), note = await asyncio.to_thread(
            _station_on_mesh, centerline_utm=centerline_utm, mesh=mesh,
            fraction=fraction)
        placed = Point(lon, lat)
    else:
        placed, moved_m = await asyncio.to_thread(
            contain, point, domain=_domain_polygon(mesh.get("artifact")),
            flowline=centerline, label="source")
        note = ("supplied point, inside the modeled domain and on the flowline"
                if moved_m <= 0.0 else
                f"supplied point, inside the modeled domain; moved {moved_m:.0f} m "
                "onto the flowline")

    # The settled point lands LAST on the nearest node holding water at t0. Both
    # steps above ask about geometry only; a bankfull domain at low flow has mapped
    # river that is dry ground when the run opens, and a source released onto it
    # discharges into the bed.
    wet_utm, moved_m, node = await asyncio.to_thread(
        snap_to_wet, as_utm(placed, utm_epsg),
        node_xy=node_xy, wet=initial_state["wet"], state=initial_state["note"],
        label="source")
    settled = (f"solved at mesh node {node}, which holds water at t0 "
               f"({initial_state['note']}), so nothing was moved")
    if moved_m > 0.0:
        settled = (f"moved {moved_m:.1f} m onto mesh node {node}, the nearest one "
                   f"holding water at t0 - the node it landed on was dry "
                   f"({initial_state['note']})")
    logger.info("source settled: %s", settled)
    journal_note(f"source point: {settled}."
                 + (f" Before that: {note}." if note else ""))
    lon, lat = _to_lonlat_point(wet_utm, utm_epsg)
    return Point(lon, lat, placed.name), "; ".join(
        part for part in (note, settled) if part)


async def settle_release(
    *,
    point: Point | None,
    mesh: dict[str, Any],
    centerline: Any,
    seed: dict[str, Any],
    reach: dict[str, Any],
    fraction: float,
    label: str,
    continue_from: str | None = None,
) -> dict[str, Any]:
    """Where a source enters the water, settled against the ACCEPTED mesh.

    A supplied point is held inside the domain; an unplaced one sits ``fraction``
    down the reach; both land on the nearest node holding water at t0."""
    from trid3nt_server.emission.pipeline_emitter import current_emitter

    utm_epsg = int(getattr(mesh.get("artifact"), "utm_epsg", 0) or 0)
    # The centerline is read head-to-tail from the seed the navigate was walked
    # downstream FROM, so ``fraction`` counts from upstream.
    centerline_utm = await asyncio.to_thread(
        read_centerline_utm, centerline, utm_epsg,
        start_lonlat=(float(seed["lon"]), float(seed["lat"])))
    node_xy, _bed = await asyncio.to_thread(mesh_nodes, mesh)
    initial_state = await asyncio.to_thread(_initial_state, continue_from, len(node_xy))
    placed, note = await _settle_release(
        point, mesh=mesh, centerline=centerline, centerline_utm=centerline_utm,
        utm_epsg=utm_epsg, fraction=fraction, node_xy=node_xy,
        initial_state=initial_state)
    # The marker rides BEFORE the solve, so the user sees the input against the
    # mesh rather than only in the results, and it carries the SETTLED point.
    await publish_point(current_emitter(), placed, label=label,
                        basis="user" if point is not None else "derived",
                        context=reach["slug"])
    at = as_utm(placed, utm_epsg)
    return {"at": [round(at[0], 3), round(at[1], 3)],
            "lon": round(placed.lon, 6), "lat": round(placed.lat, 6),
            "name": placed.name, "user_supplied": point is not None,
            "note": note, "fraction": float(min(max(fraction, 0.0), 1.0))}


def _outlet_boundary(mesh: Mapping[str, Any]) -> tuple[dict[str, Any], int, str, int]:
    """The declared OUTLET: ``(topology, number, what its quad prescribes, count)``.

    The number is the solver's own liquid-boundary walk order, 1-based."""
    topology = read_topology(_mesh_field(mesh, "topology_uri",
                                         missing=_mesh_missing))
    order = list(topology["liquid_boundary_order"])
    if _OUTLET_ROLE not in topology["roles"] or _OUTLET_ROLE not in order:
        raise TelemacError(
            f"no boundary node of the catchment mesh took the {_OUTLET_ROLE!r} "
            "role, so the basin has no outlet to drain through and no hydrograph "
            "to measure. Move the pour point onto the basin's own outlet, or mesh "
            "it finer so a boundary node reaches it.",
            error_code="TELEMAC_OUTLET_UNSET")
    # The solver numbers its liquid boundaries by walking the geometry and prints
    # one flux per number in its own volume balance; the accepted topology recorded
    # that numbering when the ``.cli`` was written. So the number is what turns
    # "the outlet" into the series the hydrograph reads, and the count is what lets
    # the deck state one stage-discharge entry per boundary in the same numbering.
    number = order.index(_OUTLET_ROLE) + 1
    return (topology, number,
            str(topology["liquid_boundary_prescribes"][number - 1]), len(order))


def _bed_slope(nodes: Sequence[int], node_xy: Any, node_bed: Any,
               cells: Any) -> float:
    """The bed gradient at a face, over the ELEMENTS that face's nodes belong to.

    The plane is fitted through the painted nodes of the touching elements."""
    import numpy as np

    xy = np.asarray(node_xy, dtype=float)
    bed = np.asarray(node_bed, dtype=float)
    tri = np.asarray(cells, dtype=np.int64)
    patch = np.unique(tri[np.isin(tri, np.asarray(list(nodes),
                                                  dtype=np.int64)).any(axis=1)])
    patch = patch[np.isfinite(bed[patch])]
    if patch.size < 3:
        raise TelemacError(
            f"the outlet face touches {patch.size} painted mesh node(s), which is "
            "no ground to measure a bed slope over; a catchment mesh paints its "
            "bed with set_bed.",
            error_code="TELEMAC_OUTLET_SLOPE_UNMEASURED")
    plane = np.linalg.lstsq(
        np.column_stack([xy[patch, 0], xy[patch, 1], np.ones(patch.size)]),
        bed[patch], rcond=None)[0]
    slope = float(np.hypot(plane[0], plane[1]))
    if not (slope > 0.0):
        raise TelemacError(
            f"the bed over the {patch.size} nodes around the outlet is flat "
            f"(gradient {slope:.6g}), so there is no uniform-flow depth for the "
            "outlet to hold and its level would have to come from a gauge.",
            error_code="TELEMAC_OUTLET_SLOPE_UNMEASURED")
    return slope


def _outlet_nodes(topology: Mapping[str, Any]) -> list[int]:
    return [int(n) for n in (topology["roles"].get(_OUTLET_ROLE) or ())]


def _outlet_manning(landcover: Any, lonlat: Any, roughness: Mapping[Any, Any],
                    unmapped: Any) -> list[float]:
    """The Manning n at the outlet nodes, read off the land cover the way the
    infiltration surface reads it, so the curve is derived under the roughness
    the deck writes at those nodes."""
    table = {int(code): tuple(row) for code, row in dict(roughness).items()}
    return [float(table.get(int(round(float(code))), tuple(unmapped))[1])
            for code in sample_layer_at_nodes(landcover, lonlat)]


def _liquid_boundaries(topology: Mapping[str, Any], node_xy: Any
                       ) -> list[dict[str, Any]]:
    """Each numbered liquid boundary, where it sits: the centroid of its role's
    nodes in the mesh's own metres. A role landing as several sections shares
    one centroid, because the topology records nodes per role."""
    import numpy as np

    xy = np.asarray(node_xy, dtype=float)
    out = []
    for number, role in enumerate(topology["liquid_boundary_order"], start=1):
        nodes = [int(n) for n in (topology["roles"].get(role) or ())]
        out.append({"number": number, "role": str(role),
                    "x": round(float(xy[nodes, 0].mean()), 3),
                    "y": round(float(xy[nodes, 1].mean()), 3)})
    return out


def _measured_outlet(topology: Mapping[str, Any], node_xy: Any, node_bed: Any,
                     cells: Any, manning: Any, *,
                     q_ceiling_m3s: float, q_ceiling_basis: str) -> dict[str, Any]:
    """What the accepted mesh says about the face the basin drains through.

    Section, slope and roughness, every one measured off the artifact itself;
    ``manning`` is the roughness at the outlet's own nodes."""
    import numpy as np

    nodes = _outlet_nodes(topology)
    coefficient = float(np.nanmedian(np.asarray(manning, dtype=float)))
    if not np.isfinite(coefficient) or coefficient <= 0.0:
        raise TelemacError(
            f"the friction field carries {coefficient!r} at the outlet nodes, so "
            "the outlet's rating curve has no roughness to be derived under.",
            error_code="TELEMAC_OUTLET_FRICTION_UNMEASURED")
    return {
        "section": _face_section(nodes, node_xy, node_bed,
                                 missing=_outlet_section_unmeasured),
        "slope": _bed_slope(nodes, node_xy, node_bed, cells),
        "law": _ROG_FRICTION_LAW, "coefficient": coefficient,
        "q_ceiling_m3s": float(q_ceiling_m3s),
        "q_ceiling_basis": q_ceiling_basis,
    }


def _rain_ceiling(rain: Mapping[str, Any], cells: Any,
                  node_xy: Any) -> tuple[float, str]:
    """The most the outlet can ever discharge, and the basis of that number.

    Gross rain on the meshed area caps it: infiltration only removes water."""
    import numpy as np

    xy = np.asarray(node_xy, dtype=float)
    tri = np.asarray(cells, dtype=np.int64)
    a, b, c = xy[tri[:, 0]], xy[tri[:, 1]], xy[tri[:, 2]]
    area_m2 = float(0.5 * np.abs((b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1])
                                 - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])).sum())
    peak_mm_hr = (max(float(v) for v in rain["series"])
                  if rain.get("kind") == "hyetograph"
                  else float(rain["intensity_mm_per_hr"]))
    if not (peak_mm_hr > 0.0 and area_m2 > 0.0):
        raise TelemacError(
            f"a storm of {peak_mm_hr:g} mm/h over {area_m2:g} m2 puts no water on "
            "the catchment, so there is no flow range for the outlet's rating "
            "curve to span.",
            error_code="TELEMAC_STORM_EMPTY")
    ceiling = peak_mm_hr / 1000.0 / 3600.0 * area_m2
    return ceiling, (
        f"the gross rain rate on the meshed catchment - peak {peak_mm_hr:g} mm/h "
        f"over {area_m2 / 1.0e6:.3f} km2 is {ceiling:.3f} m3/s, which no outlet "
        "flux can exceed because infiltration only removes water and storage "
        "only delays it")


async def settle_reach(
    *,
    reach: dict[str, Any],
    seed: dict[str, Any],
    mesh: dict[str, Any],
    centerline: Any,
    carrier_discharge: dict[str, Any],
    sim_duration_s: float,
    mesh_resolution_m: float | None = None,
    output_interval_min: float | None = None,
    friction_law: Any = None,
    friction_coefficient: float | None = None,
    continue_from: str | None = None,
) -> dict[str, Any]:
    """Everything the reach MEASURES, before a single keyword is set.

    The mesh is the ACCEPTED one, never an equivalent rebuild."""
    seed_lon, seed_lat = float(seed["lon"]), float(seed["lat"])

    # The granularity the run records is the one the ACCEPTED mesh was built at,
    # measured on its own cells; the asked edge stands only until a mesh exists
    # to measure. Nothing here re-derives an edge from a channel nobody surveyed.
    measured = mesh.get("min_edge_m")
    mesh_size_m = round(max(float(measured if measured is not None
                                  else mesh_resolution_m or 0.0),
                            MESH_H_FLOOR_M), 3)
    mesh_resolution_label = (
        f"{mesh_size_m:.3g} m measured minimum edge over "
        f"{mesh.get('element_count') or 0} elements" if measured is not None
        else f"{mesh_size_m:.3g} m asked edge (mesh unmeasured)")
    time_step_s = suggest_time_step_s(mesh_size_m, mesh=mesh.get("artifact"))

    artifact = mesh.get("artifact")
    utm_epsg = int(getattr(artifact, "utm_epsg", 0) or 0)
    # The centerline is read head-to-tail from the seed the navigate was walked
    # downstream FROM, so the bed the mesh carries slopes the same way.
    centerline_utm = await asyncio.to_thread(
        read_centerline_utm, centerline, utm_epsg,
        start_lonlat=(seed_lon, seed_lat))
    node_xy, node_bed = await asyncio.to_thread(mesh_nodes, mesh)
    # ``continue_from`` names a previous run's restart record, and the instant it
    # stands at is read here because every forcing series the sheet writes is the
    # same declared scenario evaluated over the stretch of one absolute clock.
    initial_state = await asyncio.to_thread(_initial_state, continue_from, len(node_xy))

    topology = await asyncio.to_thread(
        read_topology, _mesh_field(mesh, "topology_uri", missing=_mesh_missing))
    bed = _measured_reach(topology["roles"], node_xy, node_bed, centerline_utm)
    law = _REACH_FRICTION_LAW if friction_law is None else int(friction_law)
    coefficient = (_REACH_STRICKLER if friction_coefficient is None
                   else float(friction_coefficient))
    inflow_q = float(carrier_discharge["m3s"])
    normal = normal_depth_stage(bed, law=law, coefficient=coefficient,
                                discharge_q=inflow_q)
    journal_note(
        f"reach initial condition: constant depth {normal['depth_m']:.3f} m - the "
        f"SAME normal depth the outflow stage {normal['stage_m']:.3f} m is derived "
        f"as ({normal['q_m3s']:g} m3/s over the measured outflow section at "
        f"{normal['law']} {normal['coefficient']:g}). Bed-parallel at the friction "
        f"slope {normal['slope']:.6f}, which IS the uniform-flow surface, so the "
        "run opens at the equilibrium its own downstream boundary holds it to "
        "rather than draining a blanket depth into it.")

    start_time_s = float(initial_state["start_s"] or 0.0)
    duration_s = float(sim_duration_s)
    # WHICH dataset painted the mesh's nodes. The worker opens a file and cannot
    # know, so the label travels with the file - otherwise the run's own metrics
    # could not tell a GLO-30 bed from the 3DEP one a ladder fell to.
    bed_source = str((mesh.get("provenance") or {}).get("bed_source") or "staged")
    return {
        "name": reach["slug"],
        "title": f"{reach['slug']} REACH",
        "reach_name": reach["slug"],
        "location_name": reach["name"],
        "seed_lon": round(seed_lon, 6), "seed_lat": round(seed_lat, 6),
        "seed_source": seed.get("source"),
        "utm_epsg": utm_epsg,
        "mesh_id": mesh.get("mesh_id"),
        "mesh_size_m": mesh_size_m,
        "mesh_resolution_label": mesh_resolution_label,
        "mesh_resolution_asked_m": mesh_resolution_m,
        "time_step_s": time_step_s,
        "graphic_period": _graphic_period(output_interval_min, time_step_s),
        "duration_s": duration_s,
        "start_time_s": start_time_s,
        # The last simulated instant. A series composite writes its own tail
        # past this, so the tail is stated once - where the series is.
        "until_s": start_time_s + duration_s,
        "initial_state": initial_state["note"],
        "friction_law": law,
        "friction_coefficient": float(normal["coefficient"]),
        "depth_m": round(float(normal["depth_m"]), 3),
        "outflow_stage_m": round(float(normal["stage_m"]), 3),
        "inflow_q_m3s": inflow_q,
        "normal": {k: (round(v, 6) if isinstance(v, float) else v)
                   for k, v in normal.items()},
        "liquid_boundary_order": list(topology["liquid_boundary_order"]),
        "liquid_boundary_prescribes": list(topology["liquid_boundary_prescribes"]),
        "discharge_note": carrier_discharge.get("note"),
        "bed_source": bed_source,
        "continue_from": _PREVIOUS_DEST if continue_from else None,
        "restart": _RESTART,
        "mesh_inputs": [
            {"gs_uri": _mesh_field(mesh, "slf_uri", missing=_mesh_missing),
             "dest": "river.slf"},
            {"gs_uri": _mesh_field(mesh, "cli_uri", missing=_mesh_missing),
             "dest": "river.cli"},
            *([{"gs_uri": str(continue_from), "dest": _PREVIOUS_DEST}]
              if continue_from else [])],
        "server_facts": {
            "utm_epsg": utm_epsg,
            "bbox": [round(float(v), 6)
                     for v in (getattr(artifact, "bbox", None) or ())],
            "npoin": int(mesh.get("node_count") or 0),
            "nelem": int(mesh.get("element_count") or 0),
            "mesh_size_m": mesh_size_m,
            "name": reach["slug"],
            "duration_s": duration_s,
            "time_step_s": time_step_s,
            # WHICH file carries the time series. The deck states the RESULTS
            # FILE, so the name is the server's; the worker copies it and
            # measures the file it names.
            "result_slf": "r2d_river.slf",
            "bed_source": bed_source},
    }


def _graphic_period(output_interval_min: float | None, time_step_s: float) -> int:
    """The GRAPHIC PRINTOUT PERIOD in solver steps, off the run's own timestep.

    The cadence is asked in minutes; only this run's own step converts it."""
    if output_interval_min is None:
        return _DEFAULT_GRAPHIC_PERIOD
    return max(1, round(float(output_interval_min) * 60.0 / float(time_step_s)))


async def settle_catchment(
    *,
    catchment: dict[str, Any],
    rain: dict[str, Any],
    landcover: Any,
    roughness: Mapping[Any, Any],
    unmapped: Any,
    time_step_s: float,
    mesh_resolution_m: float | None = None,
    output_interval_min: float | None = None,
) -> dict[str, Any]:
    """Everything the catchment MEASURES at the face the basin drains through.

    ``catchment`` is the ACCEPTED mesh, never an equivalent rebuild; the outlet's
    roughness is read off ``landcover`` through ``roughness``, the class table
    the deck's own friction zones are written from."""
    artifact = catchment.get("artifact")
    utm_epsg = int(getattr(artifact, "utm_epsg", 0) or 0)
    probes = dict(getattr(artifact, "probes", None) or {})
    provenance = dict(catchment.get("provenance") or {})
    topology, outlet_boundary, outlet_prescribes, n_liquid = _outlet_boundary(
        catchment)
    if outlet_prescribes != "elevation":
        raise TelemacError(
            f"liquid boundary {outlet_boundary} carries a .cli code quad that "
            f"prescribes {outlet_prescribes!r}, and a stage-discharge curve is "
            "read only where the depth is prescribed; the boundary file and the "
            "steering file would describe different outlets.",
            error_code="TELEMAC_BOUNDARY_PRESCRIBES_NOTHING")
    mesh_size_m = float(catchment.get("min_edge_m") or mesh_resolution_m or 0.0)
    name = str(getattr(artifact, "name", None) or "watershed")
    bed_source = str(provenance.get("bed_source") or "staged")
    duration_s = float(rain["duration_s"])

    points_utm, cells, node_bed, lonlat = await asyncio.to_thread(
        accepted_mesh_nodes, catchment)
    q_ceiling, q_ceiling_basis = _rain_ceiling(rain, cells, points_utm)
    manning = await asyncio.to_thread(
        _outlet_manning, landcover, lonlat[_outlet_nodes(topology)], roughness,
        unmapped)
    outlet = _measured_outlet(
        topology, points_utm, node_bed, cells, manning,
        q_ceiling_m3s=q_ceiling, q_ceiling_basis=q_ceiling_basis)
    from ..helpers.uniform_flow import derive_rating_curve

    rating = derive_rating_curve(
        outlet["section"], law=int(outlet["law"]),
        coefficient=float(outlet["coefficient"]), slope=float(outlet["slope"]),
        q_ceiling_m3s=float(outlet["q_ceiling_m3s"]))
    journal_note(
        f"catchment outlet: liquid boundary {outlet_boundary} holds a DERIVED "
        f"stage-discharge curve - {len(rating['rows'])} points from the dry "
        f"section at {rating['thalweg_m']:.3f} m to {rating['stage_max_m']:.3f} m "
        f"at {rating['q_ceiling_m3s']:.3f} m3/s, each a normal depth over the "
        f"measured outlet section at {rating['law']} {rating['coefficient']:g} on "
        f"the measured bed slope {rating['slope']:.6f}. The range is "
        f"{outlet['q_ceiling_basis']}.")
    time_varying = bool(rain.get("time_varying"))
    return {
        "name": name,
        "title": f"{name} RAIN-ON-GRID",
        "domain_name": name,
        "utm_epsg": utm_epsg,
        "duration_s": duration_s,
        "time_step_s": float(time_step_s),
        "graphic_period": _graphic_period(output_interval_min, time_step_s),
        "rain_mm_per_day": float(rain["intensity_mm_per_hr"]) * 24.0,
        # The rain window is stated only when it CLOSES inside the run: a storm
        # that outlasts the horizon never stops, and a keyword saying so would
        # be an end nothing reaches. The recession limb is what the window is
        # for, so a storm that produces none states nothing.
        "rain_hours": (float(rain["rain_duration_s"]) / 3600.0
                       if rain.get("rain_duration_s") is not None
                       and 0.0 < float(rain["rain_duration_s"]) < duration_s
                       else None),
        # The land cover the deck's own surface is sampled from at the fill,
        # named by its raster, so the roughness the curve above was derived
        # under and the zones the deck writes come off one layer.
        "landcover": {"uri": str(layer_field(landcover, "uri") or "")},
        "hyetograph_blocks": ([[float(t), float(mm)] for t, mm in rain["blocks"]]
                              if time_varying else None),
        "outlet_boundary": outlet_boundary,
        "n_liquid_boundaries": n_liquid,
        "liquid_boundaries": _liquid_boundaries(topology, points_utm),
        "rating": {
            "at_boundary": outlet_boundary, "of_boundaries": n_liquid,
            "rows": [[q, z] for q, z in rating["rows"]],
            "note": (f"derived Z(Q) at liquid boundary {outlet_boundary}: normal "
                     f"depth over the measured outlet section at {rating['law']} "
                     f"{rating['coefficient']:g}, bed slope "
                     f"{rating['slope']:.6f}, {outlet['q_ceiling_basis']}")},
        "rain": dict(rain),
        "hyetograph_total_mm": (round(sum(float(mm) for _t, mm in rain["blocks"]), 4)
                                if time_varying else None),
        "mesh_node_count": int(catchment.get("node_count") or 0),
        "mesh_element_count": int(catchment.get("element_count") or 0),
        "mesh_size_m": mesh_size_m,
        "mesh_max_edge_m": float((probes.get("edge_length_m") or {}).get("max") or 0.0),
        "area_km2": float(probes.get("area_km2") or 0.0),
        "lonlat_bounds": [float(v) for v in (getattr(artifact, "bbox", None) or ())],
        "mesh_resolution_asked_m": mesh_resolution_m,
        "bed_source": bed_source,
        "bed_note": str(provenance.get("bed_fallback_note") or ""),
        "sizing_source": str(provenance.get("sizing_source") or ""),
        "domain_source": str(provenance.get("domain_source") or ""),
        "mesh_inputs": [
            {"gs_uri": _mesh_field(catchment, "slf_uri",
                                   missing=_mesh_missing),
             "dest": "rog.slf"},
            {"gs_uri": _mesh_field(catchment, "cli_uri",
                                   missing=_mesh_missing),
             "dest": "rog.cli"}],
        "server_facts": {
            "utm_epsg": utm_epsg,
            "bbox": [round(float(v), 6)
                     for v in (getattr(artifact, "bbox", None) or ())],
            "npoin": int(catchment.get("node_count") or 0),
            "nelem": int(catchment.get("element_count") or 0),
            "mesh_size_m": mesh_size_m,
            "name": name,
            "duration_s": duration_s,
            "time_step_s": float(time_step_s),
            "result_slf": "r2d_rog.slf",
            "bed_source": bed_source},
    }


def _mesh_facts(mesh: Mapping[str, Any], *,
                missing: Callable[[str], Exception]) -> dict[str, Any]:
    """The accepted mesh's own record, as every open-water sheet reads it.

    One reader: a harbour and a basin differ in what they DO with the mesh."""
    artifact = mesh.get("artifact")
    utm_epsg = int(getattr(artifact, "utm_epsg", 0) or 0)
    if not utm_epsg:
        raise missing(
            "the accepted mesh names no projected zone, so nothing solved on it "
            "can be georeferenced.")
    probes = dict(getattr(artifact, "probes", None) or {})
    edges = dict(probes.get("edge_length_m") or {})
    provenance = dict(mesh.get("provenance") or {})
    name = str(getattr(artifact, "name", None) or "domain")
    return {
        "mesh_name": name,
        "utm_epsg": utm_epsg,
        "mesh_node_count": int(mesh.get("node_count") or 0),
        "mesh_element_count": int(mesh.get("element_count") or 0),
        "mesh_size_m": float(edges.get("median") or mesh.get("min_edge_m") or 0.0),
        "mesh_edge_min_m": float(edges.get("min") or 0.0),
        "mesh_edge_max_m": float(edges.get("max") or 0.0),
        "bed_source": str(provenance.get("bed_source") or "staged"),
        "lonlat_bounds": [float(v)
                          for v in (getattr(artifact, "bbox", None) or ())],
    }


def _boundary_file(mesh: Mapping[str, Any], *,
                   missing: Callable[[str], Exception]) -> tuple[str, list[int]]:
    """The pair's own ``.cli`` text and the boundary nodes it numbers, in rank order.

    The file written from this geometry's IPOBO is the ONE record of the walk."""
    from trid3nt_server.tools.cache import read_object_bytes_s3

    uri = _mesh_field(mesh, "cli_uri", missing=missing)
    text = (read_object_bytes_s3(uri).decode("utf-8") if uri.startswith("s3://")
            else Path(uri).read_text(encoding="utf-8"))
    rows: list[tuple[int, int]] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            rows.append((int(parts[-1]), int(parts[-2])))
    if not rows:
        raise missing(f"the accepted mesh's boundary file {uri} holds no rows.")
    rows.sort()
    return text, [node - 1 for _rank, node in rows]


def _nodes_near(segments: Any, points_utm: Any, candidates: Sequence[int],
                tolerance_m: float) -> list[int]:
    """The candidate nodes lying within ``tolerance_m`` of any declared segment."""
    import numpy as np

    segs = np.asarray(segments, dtype=float).reshape(-1, 4)
    picked = np.asarray(candidates, dtype=np.int64)
    if segs.shape[0] == 0 or picked.size == 0:
        return []
    px = points_utm[picked, 0][:, None]
    py = points_utm[picked, 1][:, None]
    x0, y0, x1, y1 = segs[:, 0], segs[:, 1], segs[:, 2], segs[:, 3]
    dx, dy = x1 - x0, y1 - y0
    length2 = np.maximum(dx * dx + dy * dy, 1e-12)
    t = np.clip(((px - x0) * dx + (py - y0) * dy) / length2, 0.0, 1.0)
    distance = np.hypot(px - (x0 + t * dx), py - (y0 + t * dy)).min(axis=1)
    return [int(n) for n in picked[distance <= float(tolerance_m)]]


def _settled_walk(walk: Sequence[int], structure: set[int], liquid: set[int]
                  ) -> tuple[list[int], list[int]]:
    """The two roles as RUNS of the boundary walk, not as scatters of nodes.

    The structure wins where the two overlap, the way the stamp reads it."""
    order = [int(n) for n in walk]
    kind = ["structure" if n in structure else
            ("liquid" if n in liquid else "shore") for n in order]
    # A node standing alone between two of another kind is not a face. front2.f
    # says so itself - it refuses "a solid point between two liquid points" and the
    # reverse by name - so a lone node whose two walk neighbours agree with each
    # other and not with it takes their role. Settled until nothing moves, because
    # closing one hole can expose the next.
    for _pass in range(len(order)):
        moved = False
        for i in range(1, len(kind) - 1):
            if kind[i - 1] == kind[i + 1] != kind[i]:
                kind[i] = kind[i - 1]
                moved = True
        if not moved:
            break
    return ([n for n, k in zip(order, kind) if k == "structure"],
            [n for n, k in zip(order, kind) if k == "liquid"])


def _harbour_mesh_missing(message: str) -> Exception:
    return TelemacError(message, error_code="ARTEMIS_MESH_NOT_ACCEPTED")


def _basin_mesh_missing(message: str) -> Exception:
    return TelemacError(message, error_code="TELEMAC3D_MESH_NOT_ACCEPTED")


async def settle_harbour(
    *,
    mesh: dict[str, Any],
    structure: Any = None,
    structure_width_m: float,
    wave_period_s: float,
    wave_height_m: float,
    wave_direction_deg: float,
    reflection_coef: float,
    result_basename: str,
) -> dict[str, Any]:
    """What the accepted harbour mesh measures -> what the harbour sheet reads.

    A mesh naming no liquid boundary refuses: a wave has no edge to enter by."""
    from trid3nt_server.workflows.inputs.shape import polylines as _lines, shape

    facts = _mesh_facts(mesh, missing=_harbour_mesh_missing)
    utm_epsg = int(facts["utm_epsg"])
    topology = read_topology(_mesh_field(mesh, "topology_uri",
                                         missing=_harbour_mesh_missing))
    open_nodes = [int(n) for n in (topology["roles"].get("open") or ())]
    if not open_nodes:
        raise TelemacError(
            "the accepted mesh designates no liquid boundary, so a prescribed "
            f"incident wave has no edge to enter the domain through: {topology['states']}. "
            "Name an open stretch on the mesh (identify_ocean_boundary_sections) "
            "before solving a wave on it.",
            error_code="ARTEMIS_MESH_CLOSED")
    cli_text, boundary_nodes = _boundary_file(mesh, missing=_harbour_mesh_missing)
    points_utm, _cells, node_bed, _lonlat = await asyncio.to_thread(
        read_accepted_mesh_nodes,
        _mesh_field(mesh, "display_uri", missing=_harbour_mesh_missing))

    drawn = await asyncio.to_thread(
        shape, structure, label="structure", code="ARTEMIS_STRUCTURE_INVALID")
    polylines = (_lines(drawn, label="structure", code="ARTEMIS_STRUCTURE_INVALID")
                 if drawn is not None else [])
    segments = await asyncio.to_thread(
        _segments_utm, polylines, utm_epsg) if polylines else []
    # ONE NUMBER decides where the structure is, and it is the one the mesher
    # punched with: the footprint is the centreline buffered by HALF the declared
    # width, so the punched outline stands at that half-width and the water
    # inside it was removed. What a boundary node is measured against is that
    # outline plus the mesh's OWN edge, because a relaxation places a node on a
    # locked outline to within the edge it was built at - measured on this
    # harbour, the outline holds one population of nodes at 9-11 m off a 20 m
    # structure with nothing at all between 11 and 12 m, so an equality at
    # 10.000 m cuts that one population in half.
    # A structure face WINS a contested node: a barrier that imposed the incident
    # wave would radiate the sheltering away from inside the lee.
    band_m = float(structure_width_m) / 2.0 + float(facts["mesh_size_m"])
    on_structure = set(_nodes_near(segments, points_utm, boundary_nodes, band_m))
    structure_nodes, open_nodes = _settled_walk(
        boundary_nodes, on_structure, set(open_nodes) - on_structure)

    import numpy as np

    journal_note(
        f"harbour boundary: {len(boundary_nodes)} boundary nodes, "
        f"{len(open_nodes)} of them the designated liquid edge the "
        f"{wave_height_m:g} m incident wave enters through, "
        f"{len(structure_nodes)} standing on the {structure_width_m:g} m "
        f"structure footprint and reflecting "
        f"{reflection_coef:g} of it; every other face is the absorbing shore. "
        f"{topology['states']}.")
    return {
        **facts,
        "name": _slug(facts["mesh_name"]),
        "title": f"ARTEMIS {facts['mesh_name']}",
        "cli_text": cli_text,
        "open_nodes": open_nodes,
        "structure_nodes": structure_nodes,
        "structure_segments": [[float(v) for v in seg] for seg in segments],
        "boundary_nodes": len(boundary_nodes),
        "open_boundary_nodes": len(open_nodes),
        "structure_boundary_nodes": len(structure_nodes),
        "boundary_states": topology["states"],
        "wave_period_s": float(wave_period_s),
        "wave_height_m": float(wave_height_m),
        "wave_direction_deg": float(wave_direction_deg),
        "reflection_coef": float(reflection_coef),
        "max_depth_m": round(float(-np.nanmin(node_bed)), 2),
        "mesh_inputs": [
            {"gs_uri": _mesh_field(mesh, "slf_uri", missing=_harbour_mesh_missing),
             "dest": HARBOUR_GEOMETRY}],
        "server_facts": {
            "utm_epsg": utm_epsg,
            "bbox": [round(float(v), 6) for v in facts["lonlat_bounds"]],
            "npoin": facts["mesh_node_count"],
            "nelem": facts["mesh_element_count"],
            "mesh_size_m": facts["mesh_size_m"],
            "name": facts["mesh_name"],
            "result_slf": result_basename,
            "bed_source": facts["bed_source"]},
    }


async def settle_basin(
    *,
    mesh: dict[str, Any],
    warm_temp_c: float,
    cold_temp_c: float,
    thermocline_depth_m: float,
    wind_speed_mps: float,
    wind_direction_deg: float,
    levels: int,
    sim_duration_hours: float,
    time_step_s: float | None,
    output_interval_min: float | None,
    surface_m: float,
    domain_note: str = "",
    level_note: str = "",
    result_basename: str,
) -> dict[str, Any]:
    """What the accepted basin mesh measures -> what the 3D sheet is filled from.

    A basin naming no liquid boundary is what a lake IS: recorded, not refused."""
    import numpy as np

    facts = _mesh_facts(mesh, missing=_basin_mesh_missing)
    topology = read_topology(_mesh_field(mesh, "topology_uri",
                                         missing=_basin_mesh_missing))
    _points, _cells, node_bed, _lonlat = await asyncio.to_thread(
        read_accepted_mesh_nodes,
        _mesh_field(mesh, "display_uri", missing=_basin_mesh_missing))
    # The one measurement a vertical grid cannot be planned without is the DEEPEST
    # column the mesh carries: the near-surface layer a sigma grid achieves is set
    # over that column, so a plan made against a shallower one is a grid that
    # cannot hold the declared thermocline where the thermocline actually is. A
    # column is the free surface minus the bed, both on the one datum the level
    # producer already refused to mix.
    max_depth = float(surface_m) - float(np.nanmin(np.asarray(node_bed,
                                                              dtype=float)))
    duration_s = float(sim_duration_hours) * 3600.0
    # The step the basin is solved at is the accepted mesh's, through the one CFL
    # producer the reach's step comes from; a stated step is the caller's lever
    # and stands as written.
    derived_step = suggest_time_step_s(facts["mesh_size_m"],
                                       mesh=mesh.get("artifact"))
    step_s = float(time_step_s) if time_step_s is not None else derived_step
    steps = max(1, int(round(duration_s / step_s)))
    journal_note(
        f"basin column: {facts['mesh_node_count']} nodes over a {max_depth:.1f} m "
        f"deepest column below a {float(surface_m):.3f} m free surface, "
        f"{levels} sigma planes over {sim_duration_hours:g} h at "
        f"{step_s:g} s "
        f"({'stated' if time_step_s is not None else 'CFL-derived'} step; the "
        f"mesh measures {facts['mesh_size_m']:g} m). "
        f"{topology['states']} - the water in this domain is conserved."
        + (f" {domain_note}" if domain_note else "")
        + (f" {level_note}" if level_note else ""))
    return {
        **facts,
        "name": _slug(facts["mesh_name"]),
        "title": f"TELEMAC3D {facts['mesh_name']}",
        "boundary_states": topology["states"],
        "max_depth_m": round(max_depth, 2),
        "surface_m": round(float(surface_m), 3),
        "level_note": level_note,
        "duration_s": duration_s,
        "time_step_s": step_s,
        "n_steps": steps,
        "graphic_period": _graphic_period(output_interval_min, step_s),
        "listing_period": max(1, steps // 10),
        "warm_temp_c": float(warm_temp_c),
        "cold_temp_c": float(cold_temp_c),
        "thermocline_depth_m": float(thermocline_depth_m),
        "wind_speed_mps": float(wind_speed_mps),
        "wind_direction_deg": float(wind_direction_deg),
        "mesh_inputs": [
            {"gs_uri": _mesh_field(mesh, "slf_uri", missing=_basin_mesh_missing),
             "dest": BASIN_GEOMETRY},
            {"gs_uri": _mesh_field(mesh, "cli_uri", missing=_basin_mesh_missing),
             "dest": BASIN_BOUNDARY}],
        "server_facts": {
            "utm_epsg": int(facts["utm_epsg"]),
            "bbox": [round(float(v), 6) for v in facts["lonlat_bounds"]],
            "npoin": facts["mesh_node_count"],
            "nelem": facts["mesh_element_count"],
            "mesh_size_m": facts["mesh_size_m"],
            "name": facts["mesh_name"],
            "duration_s": duration_s,
            "time_step_s": step_s,
            "result_slf": result_basename,
            "bed_source": facts["bed_source"]},
    }


def _segments_utm(polylines: Sequence[Any], utm_epsg: int) -> list[list[float]]:
    """Declared lon/lat polylines -> their segments in the mesh's own zone."""
    from pyproj import Transformer

    forward = Transformer.from_crs(4326, int(utm_epsg), always_xy=True)
    out: list[list[float]] = []
    for line in polylines:
        points = [forward.transform(float(lon), float(lat)) for lon, lat in line]
        out += [[points[i][0], points[i][1], points[i + 1][0], points[i + 1][1]]
                for i in range(len(points) - 1)]
    return out


def _slug(name: str) -> str:
    """A name as the run prefix and the layer titles spell it."""
    return "".join(c if c.isalnum() else "_" for c in str(name).lower()).strip("_")
