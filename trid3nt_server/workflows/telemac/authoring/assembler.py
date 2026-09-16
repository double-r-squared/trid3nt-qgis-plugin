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

from trid3nt_server.inputs.point import (
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
           "case_section", "mesh_nodes", "new_rundir", "open_channel",
           "open_water", "settle_dredge", "settle_harbour",
           "settle_outlet_rating", "settle_release",
           "stage_run", "stage_telemac_manifest", "to_utm"]

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
#: The engine's perfect-restart record, under the name a body that keeps one
#: writes it.
_RESTART = "restart_domain.slf"
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
    from trid3nt_server import storage

    manifest = {section: dict(config), "run_id": run_tag,
                "inputs": list(inputs or []), "telemac_args": [],
                "outputs": list(outputs), **dict(extra or {})}
    key = f"{prefix or section}/{run_tag}/manifest.json"
    storage.client().put_object(
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
    from trid3nt_server import storage

    bucket = _cache_bucket()
    s3 = storage.client()
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

    from trid3nt_server.inputs.geometry import (
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


def _inside_domain(point: Point, polygon: Any, label: str) -> Point:
    """Refuse a point the meshed domain does not hold; hold one it does.

    What a domain with no channel through it can say about a placed point: it is
    inside or it is not, and moving it would be this step choosing the place."""
    from shapely.geometry import Point as _P, shape as _shape
    from shapely.ops import transform as _transform
    from pyproj import Transformer

    from trid3nt_server.inputs.geometry import (
        flatten_geometries, read_geometry_doc, utm_epsg_for,
    )
    from trid3nt_server.inputs.point import PointOutsideDomainError

    shapes = [_shape(g) for g in flatten_geometries(read_geometry_doc(polygon))
              if str(g.get("type")) in ("Polygon", "MultiPolygon")]
    if not shapes:
        raise TelemacError(
            f"the domain carries no polygon, so there is no shape the {label} "
            "could be inside of.", error_code="TELEMAC_DOMAIN_NOT_A_POLYGON")
    from shapely.ops import unary_union

    domain_ll = unary_union(shapes).buffer(0)
    epsg = utm_epsg_for(float(domain_ll.centroid.x), float(domain_ll.centroid.y))
    forward = Transformer.from_crs(4326, epsg, always_xy=True)
    domain_m = _transform(forward.transform, domain_ll)
    here = _P(*forward.transform(point.lon, point.lat))
    if not domain_m.covers(here):
        raise PointOutsideDomainError(point, label=label,
                                      distance_m=float(here.distance(domain_m)))
    return point


def _inside_water(cells: Any, wet: Any) -> Any:
    """The nodes a SOURCE may enter the water at: wet at t0, and not on the edge."""
    import numpy as np

    from trid3nt_server.workflows.mesh.shared.nodes import interior_nodes

    held = np.asarray(wet, dtype=bool)
    return held & interior_nodes(cells, held.shape[0])


async def _settle_release(
    point: Point | None, *, mesh: dict[str, Any],
    centerline: Any, centerline_utm: Any, utm_epsg: int, fraction: float,
    node_xy: Any, initial_state: Mapping[str, Any], label: str,
) -> tuple[Point, str]:
    """WHERE the source enters the water -> the settled Point and how it was decided.

    A supplied point the domain polygon does not hold raises rather than moves."""
    # With no point placed the source sits at ``fraction`` along the domain's own
    # CENTERLINE companion, walked downstream to the first station the ACCEPTED
    # MESH holds: the centerline is the whole navigated stretch and the mesh is
    # only the part of it the mapped banks left, so "on the line" and "in the
    # domain" are two different claims and only the second one solves.
    if point is None:
        if centerline_utm is None:
            raise TelemacError(
                f"this domain offers no centerline for the {label} to be placed "
                "along, so where the substance enters the water is not something "
                "the run can derive. Place the point.",
                error_code="TELEMAC_RELEASE_UNPLACED")
        (lon, lat), note = await asyncio.to_thread(
            _station_on_mesh, centerline_utm=centerline_utm, mesh=mesh,
            fraction=fraction)
        placed = Point(lon, lat)
    elif centerline is None:
        placed = await asyncio.to_thread(
            _inside_domain, point, _domain_polygon(mesh.get("artifact")), label)
        note = "supplied point, inside the modeled domain"
    else:
        placed, moved_m = await asyncio.to_thread(
            contain, point, domain=_domain_polygon(mesh.get("artifact")),
            flowline=centerline, label=label)
        note = ("supplied point, inside the modeled domain and on the flowline"
                if moved_m <= 0.0 else
                f"supplied point, inside the modeled domain; moved {moved_m:.0f} m "
                "onto the flowline")

    # The settled point lands LAST on the nearest node a source may enter the
    # water at. Both steps above ask about geometry only; a bankfull domain at
    # low flow has mapped river that is dry ground when the run opens, and a
    # source released onto it discharges into the bed.
    wet_utm, moved_m, node = await asyncio.to_thread(
        snap_to_wet, as_utm(placed, utm_epsg),
        node_xy=node_xy, wet=initial_state["wet"], state=initial_state["note"],
        label=label)
    settled = (f"solved at mesh node {node}, {moved_m:.1f} m from where it was "
               f"placed - the nearest node, which holds water at t0 "
               f"({initial_state['note']})")
    logger.info("%s settled: %s", label, settled)
    journal_note(f"{label}: {settled}."
                 + (f" Before that: {note}." if note else ""))
    lon, lat = _to_lonlat_point(wet_utm, utm_epsg)
    return Point(lon, lat, placed.name), "; ".join(
        part for part in (note, settled) if part)


async def settle_release(
    *,
    point: Point | None,
    mesh: dict[str, Any],
    label: str,
    domain: Any = None,
    fraction: float = 0.5,
    continue_from: str | None = None,
) -> dict[str, Any]:
    """Where a source enters the water, settled against the ACCEPTED mesh.

    A supplied point is held inside the domain - and onto the channel where the
    domain has a CENTERLINE companion; an unplaced one sits ``fraction`` along
    that centerline. Both land on the nearest node holding water at t0, and a
    domain with no centerline and no placed point refuses rather than guessing."""
    from trid3nt_server.render.pipeline_emitter import current_emitter

    utm_epsg = int(getattr(mesh.get("artifact"), "utm_epsg", 0) or 0)
    # The producer's own centerline runs head-to-tail the way the water does, so
    # ``fraction`` counts from upstream without a seed to orient it.
    centerline = getattr(domain, "companions", {}).get("centerline")
    centerline_utm = None if centerline is None else await asyncio.to_thread(
        read_centerline_utm, centerline, utm_epsg)
    node_xy, cells, _bed, _lonlat = await asyncio.to_thread(
        accepted_mesh_nodes, mesh)
    initial_state = await asyncio.to_thread(_initial_state, continue_from, len(node_xy))
    # WHERE A SOURCE MAY ENTER: wet at t0, and off the edge - the mesh's own rim
    # is where boundary conditions are imposed, and a source placed on it is one
    # the solver reads as outside the domain it solves.
    initial_state = {**initial_state,
                     "wet": _inside_water(cells, initial_state["wet"])}
    placed, note = await _settle_release(
        point, mesh=mesh, centerline=centerline, centerline_utm=centerline_utm,
        utm_epsg=utm_epsg, fraction=fraction, node_xy=node_xy,
        initial_state=initial_state, label=label)
    # The marker rides BEFORE the solve, so the user sees the input against the
    # mesh rather than only in the results, and it carries the SETTLED point.
    await publish_point(current_emitter(), placed, label=label,
                        basis="user" if point is not None else "derived",
                        context=_slug(getattr(domain, "name", None)
                                      or str(mesh.get("mesh_id") or "domain")))
    at = as_utm(placed, utm_epsg)
    return {"at": [round(at[0], 3), round(at[1], 3)],
            "lon": round(placed.lon, 6), "lat": round(placed.lat, 6),
            "name": placed.name, "user_supplied": point is not None,
            "note": note, "fraction": float(min(max(fraction, 0.0), 1.0))}


#: How far past the outermost node a reference profile reaches, as a fraction of
#: what it spans. The profiles are a SURFACE the engine interpolates every node
#: of a field onto, and a node outside the quadrangle two neighbouring profiles
#: bound reads no level at all - so the band overhangs the mesh on all four
#: sides rather than ending on it.
_PROFILE_MARGIN = 1.25
#: How close two profiles may stand, as a multiple of their own half-width. Two
#: perpendiculars to a bending line converge on the inside of the bend, and a
#: pair that crosses inside the band bounds a quadrangle turned inside out; at
#: this spacing they can only cross where the reach turns more than fifty
#: degrees between them.
_PROFILE_SPACING = 2.0
#: The fewest profiles the reader takes, and the most a reach is described with:
#: the surface between them is linear, so past a handful the extra lines state
#: nothing the interpolation does not already carry.
_PROFILE_MIN, _PROFILE_MAX = 2, 12


async def settle_dredge(*, mesh: dict[str, Any], line: Any, domain: Any,
                        settled: Mapping[str, Any],
                        areas: Mapping[str, Any]) -> dict[str, Any]:
    """The areas a dredge works on and the surface its levels are read from,
    both measured against the ACCEPTED mesh.

    ``areas`` is ``{name: geometry source}``; each comes back as the polygon in
    the mesh's own metres; ``line`` is the LINE the profiles are stationed along,
    and the reference is the run's OWN opening water surface."""
    utm_epsg = int(settled["utm_epsg"])
    node_xy, node_bed = await asyncio.to_thread(mesh_nodes, mesh)
    centerline_utm = await asyncio.to_thread(
        read_centerline_utm, line, utm_epsg, start_lonlat=_dredge_head(domain))
    fields = {name: await asyncio.to_thread(
                  _dredge_field, source, name, utm_epsg=utm_epsg, node_xy=node_xy)
              for name, source in areas.items() if source is not None}
    flat = str(settled.get("opening") or "") == FLAT
    profiles = await asyncio.to_thread(
        _reference_profiles, centerline_utm, node_xy=node_xy, node_bed=node_bed,
        depth_m=None if flat else float(settled["depth_m"]),
        level_m=float(settled["level_m"]))
    journal_note(
        f"dredge reference surface: {len(profiles)} profiles across the reach at "
        + (f"the flat {float(settled['level_m']):.3f} m the water stands at"
           if flat else
           f"the bed the mesh carries plus the {float(settled['depth_m']):.3f} m "
           "normal depth the run opens at")
        + ", which is the water surface the run opens on; every level a dredging "
        "action states is read from it.")
    return {**fields, "profiles": profiles}


def _dredge_head(domain: Any) -> tuple[float, float] | None:
    """Which end of the centerline is upstream, off the domain's own inflow run.

    Without one the merged direction stands, and the stationing is whichever way
    the line was drawn."""
    for run in getattr(domain, "runs", ()) or ():
        if getattr(run, "type", "") == "inflow":
            return (float(run.start.lon), float(run.start.lat))
    return None


def _dredge_field(source: Any, name: str, *, utm_epsg: int,
                  node_xy: Any) -> dict[str, Any]:
    """One drawn area -> the polygon the engine works in, in the mesh's metres.

    An area holding no mesh node is refused: nothing in it could be moved."""
    import numpy as np
    from shapely import contains_xy
    from shapely.geometry import Polygon

    geometry = to_utm(source, utm_epsg)
    parts = sorted(getattr(geometry, "geoms", [geometry]),
                   key=lambda g: g.area, reverse=True)
    if not parts or parts[0].area <= 0.0:
        raise TelemacError(
            f"the {name} area carries no polygon, so it bounds nothing to work "
            "on. Draw the area on the canvas or supply a polygon layer.",
            error_code="TELEMAC_DREDGE_AREA_EMPTY")
    # The engine reads a field as ONE closed ring, so both the file and the guard
    # take that ring: a bent or rotated area whose bounding box holds nodes its
    # interior does not would otherwise pass here and find nothing at the solve.
    worked = Polygon(parts[0].exterior)
    ring = [[round(float(x), 3), round(float(y), 3)]
            for x, y in worked.exterior.coords]
    xy = np.asarray(node_xy, dtype=float)
    inside = np.asarray(contains_xy(worked, xy[:, 0], xy[:, 1]))
    if not bool(inside.any()):
        raise TelemacError(
            f"the {name} area holds no node of the mesh this run solves on, so "
            "the engine would find nothing in it to move. Draw it over the "
            "modelled reach.", error_code="TELEMAC_DREDGE_AREA_OFF_MESH")
    return {"label": name, "vertices": ring}


def _reference_profiles(centerline_utm: Any, *, node_xy: Any, node_bed: Any,
                        depth_m: float | None, level_m: float | None
                        ) -> list[list[float]]:
    """The reference surface as the engine reads it: seven reals per profile.

    A band of cross-sections down the reach at the water surface the run opens
    at - the stated level where that surface is flat, the local bed plus the
    depth where it follows the bed - overhanging the mesh at both ends and both
    sides so every node of a field lies inside one pair."""
    import numpy as np

    line = np.asarray(centerline_utm, dtype=float)
    xy = np.asarray(node_xy, dtype=float)
    bed = np.asarray(node_bed, dtype=float)
    segment = line[1:] - line[:-1]
    length = np.maximum(np.hypot(segment[:, 0], segment[:, 1]), 1e-9)
    cumulative = np.concatenate([[0.0], np.cumsum(length)])
    station, offset = _chainage(xy, line, segment, length, cumulative)
    half = float(np.abs(offset).max()) * _PROFILE_MARGIN
    reach = float(station.max() - station.min()) * _PROFILE_MARGIN
    count = int(np.clip(round(reach / max(half * _PROFILE_SPACING, 1e-9)),
                        _PROFILE_MIN, _PROFILE_MAX))
    margin = (reach - (station.max() - station.min())) / 2.0
    rows: list[list[float]] = []
    for where in np.linspace(float(station.min()) - margin,
                             float(station.max()) + margin, count):
        point, axis = _on_line(line, segment, length, cumulative, float(where))
        normal = np.array([-axis[1], axis[0]], dtype=float)
        node = int(np.argmin(np.hypot(xy[:, 0] - point[0], xy[:, 1] - point[1])))
        level = (float(level_m) if depth_m is None
                 else float(bed[node]) + float(depth_m))
        left, right = point + normal * half, point - normal * half
        rows.append([round(float(left[0]), 3), round(float(left[1]), 3),
                     round(level, 4),
                     round(float(right[0]), 3), round(float(right[1]), 3),
                     round(level, 4),
                     round(float(where - station.min()) / 1000.0, 4)])
    return rows


def _chainage(xy: Any, line: Any, segment: Any, length: Any,
              cumulative: Any) -> tuple[Any, Any]:
    """Each point's arc length ALONG the line and its distance OFF it."""
    import numpy as np

    t = np.clip(((xy[:, None, 0] - line[None, :-1, 0]) * segment[None, :, 0]
                 + (xy[:, None, 1] - line[None, :-1, 1]) * segment[None, :, 1])
                / (length * length)[None, :], 0.0, 1.0)
    foot_x = line[None, :-1, 0] + t * segment[None, :, 0]
    foot_y = line[None, :-1, 1] + t * segment[None, :, 1]
    distance = np.hypot(foot_x - xy[:, None, 0], foot_y - xy[:, None, 1])
    nearest = np.argmin(distance, axis=1)
    rows = np.arange(xy.shape[0])
    return (cumulative[nearest] + t[rows, nearest] * length[nearest],
            distance[rows, nearest])


def _on_line(line: Any, segment: Any, length: Any, cumulative: Any,
             where: float) -> tuple[Any, Any]:
    """The point at arc length ``where`` and the unit tangent there.

    Past either end the end segment is extended, which is how a profile comes to
    stand outside the mesh it has to overhang."""
    import numpy as np

    index = int(np.clip(np.searchsorted(cumulative, where) - 1, 0,
                        len(length) - 1))
    t = (where - cumulative[index]) / length[index]
    return line[index] + t * segment[index], segment[index] / length[index]


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


async def settle_outlet_rating(
    *,
    mesh: dict[str, Any],
    landcover: Any,
    roughness: Mapping[Any, Any],
    unmapped: Any,
    mm_per_hr: float | None = None,
    series: Any = None,
    record: Any = None,
) -> dict[str, Any]:
    """The stage-discharge curve the catchment's outlet holds, and which boundary
    reads it.

    The section, the bed slope and the roughness are measured off the accepted
    mesh itself; the flow range is the gross rain on the meshed area, which
    nothing leaving it can exceed, because infiltration only removes water and
    storage only delays it."""
    topology, outlet_boundary, outlet_prescribes, n_liquid = _outlet_boundary(mesh)
    if outlet_prescribes != "elevation":
        raise TelemacError(
            f"liquid boundary {outlet_boundary} carries a .cli code quad that "
            f"prescribes {outlet_prescribes!r}, and a stage-discharge curve is "
            "read only where the depth is prescribed; the boundary file and the "
            "steering file would describe different outlets.",
            error_code="TELEMAC_BOUNDARY_PRESCRIBES_NOTHING")
    points_utm, cells, node_bed, lonlat = await asyncio.to_thread(
        accepted_mesh_nodes, mesh)
    measured = series if series is not None else record
    storm = ({"kind": "hyetograph", "series": list(measured)} if measured
             else {"kind": "design_storm", "intensity_mm_per_hr": mm_per_hr})
    q_ceiling, q_ceiling_basis = _rain_ceiling(storm, cells, points_utm)
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
    note = (f"derived Z(Q) at liquid boundary {outlet_boundary}: normal depth "
            f"over the measured outlet section at {rating['law']} "
            f"{rating['coefficient']:g}, bed slope {rating['slope']:.6f}, "
            f"{outlet['q_ceiling_basis']}")
    journal_note(
        f"catchment outlet: liquid boundary {outlet_boundary} holds a DERIVED "
        f"stage-discharge curve - {len(rating['rows'])} points from the dry "
        f"section at {rating['thalweg_m']:.3f} m to {rating['stage_max_m']:.3f} m "
        f"at {rating['q_ceiling_m3s']:.3f} m3/s, each a normal depth over the "
        f"measured outlet section at {rating['law']} {rating['coefficient']:g} on "
        f"the measured bed slope {rating['slope']:.6f}. The range is "
        f"{outlet['q_ceiling_basis']}.")
    return {"at_boundary": outlet_boundary, "of_boundaries": n_liquid,
            "rows": [[q, z] for q, z in rating["rows"]], "note": note}


def _domain_unmeasured(message: str) -> Exception:
    return TelemacError(message, error_code="TELEMAC_DOMAIN_UNMEASURED")


async def open_water(
    *,
    mesh: dict[str, Any],
    sim_duration_s: float,
    level: Any = None,
    name: str = "",
    geometry: str = "geometry.slf",
    boundary: str = "boundary.cli",
    result: str = "results.slf",
    mesh_resolution_m: float | None = None,
    output_interval_min: float | None = None,
    continue_from: str | None = None,
) -> dict[str, Any]:
    """A BODY OF WATER OPENS at a level over its bed: the mesh it was handed, the
    clock the run turns on, the files the box is given, and THE OPENING - the
    level the surface stands at and the depth that leaves over every node.

    Any body of water: the level is stated or somebody measured it, the
    depth is that level minus the bed, the edge is wall all round and the surface
    opens flat. Nothing here knows a reach from a lake. What a question ADDS on
    top - an inflow's normal depth, an outlet rating curve - is its own step,
    and what that step measured arrives here as the level this one opens at."""
    facts = _mesh_facts(mesh, missing=_domain_unmeasured)
    measured = mesh.get("min_edge_m")
    mesh_size_m = round(max(float(measured if measured is not None
                                  else mesh_resolution_m or 0.0),
                            MESH_H_FLOOR_M), 3)
    mesh_resolution_label = (
        f"{mesh_size_m:.3g} m measured minimum edge over "
        f"{facts['mesh_element_count']} elements" if measured is not None
        else f"{mesh_size_m:.3g} m asked edge (mesh unmeasured)")
    time_step_s = suggest_time_step_s(mesh_size_m, mesh=mesh.get("artifact"))
    node_xy, _bed = await asyncio.to_thread(mesh_nodes, mesh)
    initial_state = await asyncio.to_thread(_initial_state, continue_from,
                                            len(node_xy))
    topology = await asyncio.to_thread(
        read_topology, _mesh_field(mesh, "topology_uri", missing=_mesh_missing))
    start_time_s = float(initial_state["start_s"] or 0.0)
    duration_s = float(sim_duration_s)
    slug = _slug(name or facts["mesh_name"])
    opening = await asyncio.to_thread(_opening, level, mesh)
    return {
        "name": slug,
        "title": f"{slug} DOMAIN",
        "location_name": name or facts["mesh_name"],
        "utm_epsg": facts["utm_epsg"],
        "mesh_id": mesh.get("mesh_id"),
        "mesh_size_m": mesh_size_m,
        "mesh_resolution_label": mesh_resolution_label,
        "mesh_resolution_asked_m": mesh_resolution_m,
        "time_step_s": time_step_s,
        "graphic_period": _graphic_period(output_interval_min, time_step_s),
        "duration_s": duration_s,
        "start_time_s": start_time_s,
        "until_s": start_time_s + duration_s,
        "initial_state": initial_state["note"],
        # THE OPENING, as every deck reads it: what the surface opens at, the
        # depth under it and the keyword that says which of the two the engine is
        # to lay. A run nobody stated a level for carries None for all three and
        # opens the way its own deck says.
        **opening,
        # WHICH dataset painted the mesh's nodes, and where each one stopped: a
        # two-source bed says how much of the domain the survey covered.
        "bed_source": facts["bed_source"],
        "liquid_boundary_order": list(topology["liquid_boundary_order"]),
        "liquid_boundary_prescribes": list(topology["liquid_boundary_prescribes"]),
        # WHERE each numbered liquid boundary sits, so a printed flux can be read
        # at the one a Point stands on.
        "liquid_boundaries": _liquid_boundaries(topology, node_xy),
        "continue_from": _PREVIOUS_DEST if continue_from else None,
        "mesh_inputs": [
            {"gs_uri": _mesh_field(mesh, "slf_uri", missing=_mesh_missing),
             "dest": geometry},
            {"gs_uri": _mesh_field(mesh, "cli_uri", missing=_mesh_missing),
             "dest": boundary},
            *([{"gs_uri": str(continue_from), "dest": _PREVIOUS_DEST}]
              if continue_from else [])],
        "server_facts": {
            "utm_epsg": facts["utm_epsg"],
            "bbox": [round(float(v), 6) for v in facts["lonlat_bounds"]],
            "npoin": facts["mesh_node_count"],
            "nelem": facts["mesh_element_count"],
            "mesh_size_m": mesh_size_m,
            "name": slug,
            "duration_s": duration_s,
            "time_step_s": time_step_s,
            "result_slf": result,
            "bed_source": facts["bed_source"]},
    }


#: The three keys every deck reads the water off, and the two an addition fills
#: where the edge carries values. A body nobody stated a level for carries None
#: in all of them: no water was measured, and nothing is claimed.
_NO_WATER: dict[str, Any] = {
    "level_m": None, "depth_m": None, "max_depth_m": None, "opening": None,
    "inflow_q_m3s": None, "outflow_stage_m": None,
}

#: What the engine is told to lay: a flat surface at the level, or a sheet of one
#: depth following the bed. Flat is what any body of water does; bed-parallel is
#: what a reach that FALLS holds, and only its own addition measures that.
FLAT = "CONSTANT ELEVATION"
BED_PARALLEL = "CONSTANT DEPTH"


def _opening(level: Any, mesh: Mapping[str, Any]) -> dict[str, Any]:
    """THE OPENING: the level this domain stands at and the depth that leaves.

    ``level`` is what somebody measured: a number, an observation, or the record
    an ADDITION returned after measuring the water over this same mesh - a reach
    holds its own opening, and the base takes it rather than deriving a second
    one. Nothing measured and a bed stated as a DEPTH is the bed's own zero;
    nothing measured over a bed on a datum is no water, which is an answer and
    not a refusal - a deck that needs none opens the way it says."""
    import numpy as np

    # A bed STATED as a depth is counted from the free surface itself, so the
    # mesh knows where that surface stands and nothing has to be read for it.
    zero = dict(mesh.get("provenance") or {}).get("free_surface_m")
    if isinstance(level, Mapping):
        measured = {**_NO_WATER, **{k: v for k, v in level.items() if k in _NO_WATER}}
    else:
        measured = dict(_NO_WATER)
        measured["level_m"] = (None if level is None
                               else float(getattr(level, "value", level)))
    if measured["level_m"] is None and zero is None:
        return dict(_NO_WATER)
    surface = float(measured["level_m"] if measured["level_m"] is not None
                    else zero)
    _points, _cells, node_bed, _lonlat = read_accepted_mesh_nodes(
        _mesh_field(mesh, "display_uri", missing=_mesh_missing))
    bed = np.asarray(node_bed, dtype=float)
    floor, ceiling = float(np.nanmin(bed)), float(np.nanmax(bed))
    measured["level_m"] = round(surface, 3)
    measured["opening"] = measured["opening"] or FLAT
    if measured["opening"] == BED_PARALLEL:
        # A SHEET OF ONE DEPTH following the bed: every node holds that depth,
        # and the column is it. The level is where the outflow section stands
        # under that sheet, which is the only place the two agree.
        depth = float(measured["depth_m"])
        measured["max_depth_m"] = round(depth, 2)
        journal_note(
            f"the opening: bed-parallel at {depth:.3f} m over a bed the "
            f"accepted mesh carries between {floor:.3f} m and {ceiling:.3f} m, "
            f"so every node holds that depth and the outflow section stands at "
            f"{measured['level_m']:.3f} m.")
        return measured
    deepest = surface - floor
    if deepest <= 0.0:
        raise TelemacError(
            f"the free surface opens at {surface:.3f} m and the deepest node of "
            "the accepted mesh sits at or above it, so this domain holds no "
            "water to solve in. State the depth the body holds, or name a bed "
            "measured on the datum the level is read on.",
            error_code="TELEMAC_DOMAIN_HOLDS_NO_WATER")
    wet = surface - bed
    measured["max_depth_m"] = round(deepest, 2)
    if measured["depth_m"] is None:
        measured["depth_m"] = round(float(np.nanmean(wet[wet > 0.0])), 3)
    journal_note(
        f"the opening: FLAT at {measured['level_m']:.3f} m over a "
        f"bed the accepted mesh carries between {floor:.3f} m and "
        f"{ceiling:.3f} m, so the deepest column is "
        f"{measured['max_depth_m']:.2f} m and "
        f"{float((wet > 0.0).mean()) * 100.0:.1f}% of the nodes hold water.")
    return measured


async def open_channel(
    *,
    mesh: dict[str, Any],
    friction_law: int,
    friction_coefficient: float,
    carrier: Any = None,
    stage: Any = None,
) -> dict[str, Any]:
    """A CHANNEL OPENS UNDER A DISCHARGE, on top of any body of water: what the
    inflow carries, what the outflow holds, and the depth the run opens at.

    The flow is what the inflow run carries - a reading, or the number stated on
    the call. Where a LEVEL was measured and it stands over the inflow run's own
    bed, that level is what the outflow holds and what the run opens flat at.
    Where none was, the stage is a NORMAL DEPTH over the section the outflow run
    cuts, derived at the roughness the deck is written at, and the run opens
    bed-parallel at that same depth - the equilibrium its own downstream boundary
    holds it to rather than a blanket depth draining into it.

    An edge that names NO runs is a closed body: there is no channel here to
    measure, so this adds nothing and hands back the level it was given, which
    is what the base opens the water flat at."""
    node_xy, node_bed = await asyncio.to_thread(mesh_nodes, mesh)
    topology = await asyncio.to_thread(
        read_topology, _mesh_field(mesh, "topology_uri", missing=_mesh_missing))
    roles = topology["roles"] or {}
    held = _held_level(stage)
    if not (roles.get("inflow") and roles.get("outflow")):
        return {"level_m": None if held is None else round(held[0], 3),
                "depth_m": None, "opening": None,
                "inflow_q_m3s": None, "outflow_stage_m": None,
                "discharge_note": "this domain's edge names no runs, so no flow "
                                  "is imposed and no stage is derived."}
    law, coefficient = int(friction_law), float(friction_coefficient)
    inflow_q, discharge_note = _carried_discharge(carrier)
    bed = _measured_channel(roles, node_xy, node_bed)
    # A LEVEL SOMEBODY MEASURED is where the water stands, and the run opens flat
    # at it: a uniform-flow depth is a model of the same surface, and a model
    # does not stand over a measurement of the thing it models. The derivation is
    # for the reach that has no measurement - or the one a measurement does not
    # reach, where a horizontal surface at the outflow's level leaves the inflow
    # face dry and the engine has no water to impose a discharge on.
    if held is not None and _stands_over_the_reach(bed, held[0]):
        normal = _level_opening(bed, held, discharge_q=inflow_q)
        journal_note(
            f"the open channel: the outflow HOLDS at the measured level "
            f"{normal['stage_m']:.3f} m and the run opens FLAT at it, which "
            f"stands {normal['stage_m'] - float(bed['bed_top_m']):.3f} m over "
            f"the inflow run's own bed; the measured ends fall "
            f"{normal['drop_m']:.3f} m over {normal['length_m']:.1f} m and no "
            f"uniform-flow depth is derived. {normal['held']} {discharge_note}")
    else:
        normal = normal_depth_stage(bed, law=law, coefficient=coefficient,
                                    discharge_q=inflow_q)
        journal_note(
            f"the open channel: constant depth {normal['depth_m']:.3f} m - the SAME "
            f"normal depth the outflow stage {normal['stage_m']:.3f} m is derived "
            f"as ({normal['q_m3s']:g} m3/s over the measured outflow section at "
            f"{normal['law']} {normal['coefficient']:g}). Bed-parallel at the "
            f"friction slope {normal['slope']:.6f}, which IS the uniform-flow "
            f"surface. {discharge_note}")
    return {
        # The level the base opens the water at, and HOW: a reach that falls
        # holds a sheet of one depth on its own friction slope, and one that
        # does not holds flat at the level somebody measured.
        "level_m": round(float(normal["stage_m"]), 3),
        "depth_m": round(float(normal["depth_m"]), 3),
        "opening": FLAT if float(normal["slope"]) == 0.0 else BED_PARALLEL,
        "outflow_stage_m": round(float(normal["stage_m"]), 3),
        "inflow_q_m3s": inflow_q,
        "friction_law": law,
        "friction_coefficient": coefficient,
        "discharge_note": discharge_note,
        "normal": {k: (round(v, 6) if isinstance(v, float) else v)
                   for k, v in normal.items()},
    }


def _carried_discharge(carrier: Any) -> tuple[float, str]:
    """What the inflow run carries, and where the number came from -> refuses.

    One value, whichever way it arrived: the slot reads a stated number and a
    record the same, and a stated one stands over any record. Nothing carried is
    no flow to impose, and the run says so."""
    reported = _reported_discharge(carrier)
    if reported is None:
        raise TelemacError(
            "the inflow run carries a discharge and nothing reported one over "
            "this domain. State the flow on the carrier slot, or name a source "
            "that reaches this water.",
            error_code="TELEMAC_INFLOW_DISCHARGE_UNMEASURED")
    value, note = reported
    return value, note


def _held_level(stage: Any) -> tuple[float, str] | None:
    """The water-surface ELEVATION somebody measured at the outflow, or ``None``.

    An elevation on the datum the bed is painted on - never a height above a
    gauge's own zero, which is a different number about a different surface."""
    from trid3nt_server.inputs.observation import Observation

    if stage is None:
        return None
    if isinstance(stage, (int, float)) and not isinstance(stage, bool):
        return float(stage), "the outflow level was handed over as a number."
    if not isinstance(stage, Observation):
        raise TelemacError(
            f"the outflow level for this run arrived as {type(stage).__name__}, "
            "which is a record rather than a reading: which site reports the "
            "level and how old the sample is are the observation slot's to "
            "decide. Declare the row as Data.observation(...) so one value "
            "reaches this step.",
            error_code="TELEMAC_STAGE_UNINGESTED")
    return float(stage.value), (
        f"the outflow holds at {stage.value:g} m, which "
        f"{stage.site_name or stage.site_id or 'the record'} reported"
        + (f" on {stage.sampled}" if stage.sampled else "") + ".")


def _stands_over_the_reach(bed: Mapping[str, Any], level_m: float) -> bool:
    """Is this measured level the water over THIS reach, rather than part of it?

    Over the inflow run's own bed, yes: the surface covers the reach end to end.
    Below the outflow section altogether, also yes - it is not a level on this
    bed at all and the opening refuses by name rather than quietly deriving one
    instead. In between is the reach a uniform-flow depth is for: a horizontal
    surface at the outflow's level would leave the inflow face dry."""
    section = [float(z) for _offset, z in (bed.get("outflow_section") or ())]
    return (level_m > float(bed["bed_top_m"])
            or (bool(section) and level_m <= min(section)))


def _level_opening(bed: Mapping[str, Any], held: tuple[float, str], *,
                   discharge_q: float) -> dict[str, Any]:
    """The opening a reach that does not FALL takes: the level that was measured.

    A uniform-flow depth is a fall over a length, so measured ends that sit level
    have none; the water still stands where the measurement says it does, and the
    run opens flat at the depth that leaves over the outflow section."""
    stage_m, note = held
    section = [(float(offset), float(z))
               for offset, z in (bed.get("outflow_section") or ())]
    if len(section) < 2:
        raise TelemacError(
            f"the outflow run's own section carries {len(section)} point(s), so "
            "there is no bed under the level the outflow is held at.",
            error_code="TELEMAC_OUTFLOW_SECTION_UNMEASURED")
    thalweg = min(z for _offset, z in section)
    depth = stage_m - thalweg
    if depth <= 0.0:
        raise TelemacError(
            f"the outflow is held at {stage_m:.3f} m and the deepest node of the "
            f"outflow run's own section sits at {thalweg:.3f} m, so the level "
            "handed over is at or below the bed. A level here is an ELEVATION on "
            "the datum the bed is painted on, not a height above a gauge's zero.",
            error_code="TELEMAC_OUTFLOW_STAGE_BELOW_BED")
    return {"stage_m": stage_m, "depth_m": depth, "slope": 0.0,
            "drop_m": float(bed["bed_drop_m"]),
            "length_m": float(bed["reach_length_m"]),
            "held": note, "q_m3s": float(discharge_q)}


def _reported_discharge(carrier: Any) -> tuple[float, str] | None:
    """The streamflow the carrier OBSERVATION reports, with what reported it.

    ``None`` where nothing came - a context row whose source held nothing. A
    record that never passed its slot's ingestion is refused by name rather than
    read here: choosing the nearest site is what the observation slot does."""
    from trid3nt_server.inputs.observation import Observation

    if carrier is None:
        return None
    if isinstance(carrier, (int, float)) and not isinstance(carrier, bool):
        return float(carrier), (
            f"the discharge {float(carrier):g} m3/s was stated on the call.")
    if not isinstance(carrier, Observation):
        raise TelemacError(
            f"the carrier for this run arrived as {type(carrier).__name__}, which "
            "is a record rather than a reading: which site reports the flow and "
            "how old the sample is are the observation slot's to decide. Declare "
            "the row as Data.observation(...) so one value reaches this step.",
            error_code="TELEMAC_CARRIER_UNINGESTED")
    where = carrier.site_name or carrier.site_id
    if not where and not carrier.sampled:
        # A reading with no site and no moment is the value the caller STATED,
        # which the slot carries in the same shape as a record so this step reads
        # one thing; saying a record reported it would name a source nobody read.
        return float(carrier.value), (
            f"the discharge {carrier.value:g} m3/s was stated on the call.")
    return float(carrier.value), (
        f"the discharge {carrier.value:g} m3/s is what "
        f"{where or 'the carrier record'} reported"
        + (f" on {carrier.sampled}" if carrier.sampled else "") + ".")


def _measured_channel(roles: Mapping[str, Any], node_xy: Any,
                      node_bed: Any) -> dict[str, Any]:
    """What the accepted mesh says about the channel the outflow stage rests on.

    The two bed medians are over the nodes the inflow and outflow runs name, and
    the fall between them over the distance between them IS the friction slope -
    measured on the mesh rather than on a line laid beside it."""
    import numpy as np

    bed = None if node_bed is None else np.asarray(node_bed, dtype=float)
    xy = None if node_xy is None else np.asarray(node_xy, dtype=float)
    medians: dict[str, float] = {}
    centres: dict[str, Any] = {}
    role_nodes: dict[str, list[int]] = {}
    for role in ("inflow", "outflow"):
        nodes = [int(n) for n in ((roles or {}).get(role) or ())]
        if bed is None or max(nodes) >= bed.shape[0]:
            raise TelemacError(
                f"the outflow stage is derived over the painted bed at the "
                f"{role!r} run's own nodes, and the accepted mesh carries "
                f"{0 if bed is None else bed.shape[0]} bed values under roles "
                f"{sorted(roles or {})}.",
                error_code="TELEMAC_MESH_BED_UNMEASURED")
        median = float(np.nanmedian(bed[nodes]))
        if not np.isfinite(median):
            raise TelemacError(
                f"every node the {role!r} run names carries an unpainted bed, so "
                "the outflow stage has no ground to be measured from.",
                error_code="TELEMAC_MESH_BED_UNMEASURED")
        medians[role] = median
        centres[role] = xy[nodes].mean(axis=0)
        role_nodes[role] = nodes
    length = float(np.hypot(*(centres["outflow"] - centres["inflow"])))
    return {"bed_top_m": medians["inflow"],
            "bed_drop_m": medians["inflow"] - medians["outflow"],
            "reach_length_m": round(length, 3),
            "outflow_section": _face_section(role_nodes["outflow"], node_xy, bed,
                                             missing=_reach_section_unmeasured)}


def _graphic_period(output_interval_min: float | None, time_step_s: float) -> int:
    """The GRAPHIC PRINTOUT PERIOD in solver steps, off the run's own timestep.

    The cadence is asked in minutes; only this run's own step converts it."""
    if output_interval_min is None:
        return _DEFAULT_GRAPHIC_PERIOD
    return max(1, round(float(output_interval_min) * 60.0 / float(time_step_s)))


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
    from trid3nt_server.inputs.shape import polylines as _lines, shape

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
