"""The accepted mesh -> what the sheet is FILLED from, and the run the box gets.

Two acts, and the flip between them is the sheet. SETTLING is everything the run
has to MEASURE before a keyword can be set: the bed at the mesh's declared roles,
the section its outflow face cuts, the uniform-flow depth that section conveys
the prescribed discharge at, where the release actually lands, and - for a
catchment - the outlet face and the flow range its rating curve has to span.
Every one of them comes off the artifact itself, because a number derived beside
the mesh could disagree with the ground the geometry file holds.

STAGING is the other act: everything the fill wrote is uploaded beside the mesh
the solve runs on, and the manifest that names the case is written LAST - so a
manifest exists only for a run whose every file is already where the launcher
will look for it.

What is NOT here any more is the authoring. A steering file is the SERIALIZER's,
written by telapy against the engine's own dictionary; the sheet the serializer
resolves is the template's.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from trid3nt_contracts import new_ulid

from trid3nt_server.workflows.runtime import journal_note
from trid3nt_server.workflows.mesh.shared.nodes import (
    read_accepted_mesh_nodes,
    read_centerline_utm,
)
from trid3nt_server.workflows.mesh.topology import RATING_CURVE_ROLE, read_topology

from .open_water import OpenWaterError, case_section, stage_telemac_manifest
from ..helpers.catchment import mesh_nodes
from ..helpers.errors import (
    RainOnGridError,
    TelemacDyeScenarioError,
    TelemacDyeScenarioInputError,
)
from ..helpers.reach import MESH_H_FLOOR_M, coerce_lonlat_point, suggest_time_step_s
from ..helpers.uniform_flow import normal_depth_stage

logger = logging.getLogger("trid3nt_server.workflows.telemac.authoring.assembler")

__all__ = ["new_rundir", "settle_catchment", "settle_reach", "stage_run"]

#: What a continued run's PREVIOUS COMPUTATION FILE is called in the run
#: directory. The engine reads a file, not a URI, so the previous run's restart
#: record is staged under one name and the steering file names that.
_PREVIOUS_DEST = "previous.slf"
#: The engine's perfect-restart record, under the name the reach body writes it.
_RESTART = "restart_river.slf"

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

#: How far down its own reach a DO-sag outfall sits. The reach was navigated
#: downstream FROM the outfall, so the discharge belongs at the top; the fraction
#: is what holds the source node off the inflow face rather than on it.
DO_SAG_OUTFALL_FRAC = 0.02

#: How far past the last simulated instant a forcing series runs, so the engine's
#: time interpolation never reads off the end of it.
_SERIES_TAIL_S = 100.0


# --------------------------------------------------------------------------- #
# The staging flow.
# --------------------------------------------------------------------------- #
def _run_directory(run_tag: str) -> Path:
    rundir = Path(os.environ.get("TRID3NT_RUNS_DIR", "/tmp")) / f"telemac-{run_tag}"
    rundir.mkdir(parents=True, exist_ok=True)
    return rundir


def _cache_bucket() -> str:
    bucket = (os.environ.get("TRID3NT_CACHE_BUCKET") or "").strip()
    if not bucket:
        raise TelemacDyeScenarioError(
            "TELEMAC_STAGING_FAILED",
            "TRID3NT_CACHE_BUCKET must be set to stage an authored run.")
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

    The document itself is written by the ONE manifest writer, under the ``case``
    key the worker dispatches on.
    """
    try:
        return stage_telemac_manifest(
            section="case", config=case, run_tag=run_tag, outputs=outputs,
            inputs=inputs, prefix=prefix)
    except OpenWaterError as exc:
        raise TelemacDyeScenarioError("TELEMAC_STAGING_FAILED", str(exc)) from exc


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

    Everything the authoring wrote is uploaded beside the mesh the solve runs on,
    and the manifest that names the case is written LAST - so a manifest exists
    only for a run whose every file is already where the launcher will look.
    """
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





# --------------------------------------------------------------------------- #
# The reach: what the accepted mesh measures before a keyword is set.
# --------------------------------------------------------------------------- #
def _face_section(nodes: Sequence[int], node_xy: Any, bed: Any, *,
                  missing: Callable[[str], Exception]) -> list[list[float]]:
    """The channel a role's face cuts, as ``(offset, bed)`` pairs.

    A role is a contiguous RUN of the boundary walk, so the nodes arrive in the
    order they lie along the face and the offset is the running chord distance
    between them. That makes the section a real transect of the painted bed
    rather than a scatter that has to be re-ordered by a rule of its own.

    A node the bed left unpainted drops out of the section and takes no offset
    with it: the survey has a hole in it, and closing the hole by shifting the
    nodes past it would narrow a channel nobody re-measured.
    """
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
    section = [[round(float(o), 3), round(float(z), 3)]
               for o, z in zip(offsets, bed[list(nodes)]) if np.isfinite(z)]
    if len(section) < 2:
        raise missing(f"the face carries {len(section)} painted node(s), which is "
                      "no section to derive a normal depth over.")
    return section


def _measured_reach(roles: Mapping[str, Any], node_xy: Any, node_bed: Any,
                    centerline_utm: Any) -> dict[str, Any]:
    """What the accepted mesh says about the reach the outflow stage rests on.

    Three measurements, every one off the artifact itself: the bed at the two
    declared roles, the SECTION the outflow face cuts through that bed, and the
    length of the line the mesh was built over. A profile derived beside the mesh
    could disagree with the bed the geometry file holds, and a stage derived from
    it would be prescribed against ground the solver never sees.

    The two bed numbers are medians over the nodes each role names - the reach's
    top, and the fall from there to its outflow. That fall over that length is
    the friction slope the normal depth is computed at.
    """
    import numpy as np

    bed = None if node_bed is None else np.asarray(node_bed, dtype=float)
    medians: dict[str, float] = {}
    role_nodes: dict[str, list[int]] = {}
    for role in ("inflow", "outflow"):
        nodes = [int(n) for n in ((roles or {}).get(role) or ())]
        if bed is None or not nodes or max(nodes) >= bed.shape[0]:
            raise TelemacDyeScenarioError(
                "TELEMAC_MESH_BED_UNMEASURED",
                f"the outflow stage is derived over the painted bed at the "
                f"{role!r} role's own nodes, and the accepted mesh carries "
                f"{0 if bed is None else bed.shape[0]} bed values under roles "
                f"{sorted(roles or {})}; a reach mesh recipe paints its bed with "
                "set_bed and names its faces with set_boundary_roles.")
        median = float(np.nanmedian(bed[nodes]))
        if not np.isfinite(median):
            raise TelemacDyeScenarioError(
                "TELEMAC_MESH_BED_UNMEASURED",
                f"every node the {role!r} role names carries an unpainted bed, so "
                "the outflow stage has no ground to be measured from.")
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

    A continued run is the SAME declared scenario over an extended horizon, and
    the horizon begins where the leg being continued stopped. Only that file can
    say when: the engine writes the restart at its own last time step, which is
    not the graphic period, not the asked duration, and not something the server
    can compute from the ask. The depth at that instant is the run's INITIAL
    STATE, which is what decides where a release can land.
    """
    import tempfile

    import numpy as np

    from trid3nt_server.workflows.solver.solver import _download_object
    from trid3nt_server.workflows.telemac.result_reader import read_selafin
    from trid3nt_server.workflows.telemac.products.postprocess_telemac import (
        _DEPTH_VAR_KEYS, TELEMAC_WSE_WET_DEPTH_M,
    )

    with tempfile.TemporaryDirectory(prefix="telemac-continue-") as tmp:
        path = Path(tmp) / _PREVIOUS_DEST
        _download_object(str(uri), path)
        record = read_selafin(path)
    if len(record["times"]) == 0:
        raise TelemacDyeScenarioError(
            "TELEMAC_CONTINUATION_UNREADABLE",
            f"{uri} holds no time record, so there is no state to continue from "
            "and no instant to continue the scenario at. Point continue_from at "
            f"a completed run's {_RESTART}.")
    depth = next((record["data"][name] for name in record["varnames"]
                  if name.strip().upper() in _DEPTH_VAR_KEYS), None)
    if depth is None:
        raise TelemacDyeScenarioError(
            "TELEMAC_CONTINUATION_UNREADABLE",
            f"{uri} carries no water depth among {record['varnames']}, so the "
            "state this run would start from cannot say where it is wet.")
    start_s = float(record["times"][-1])
    wet = np.asarray(depth[-1], dtype=float) > TELEMAC_WSE_WET_DEPTH_M
    return {
        "start_s": start_s, "wet": wet,
        "note": (f"the restart record this run continues from, at t={start_s:.0f} s: "
                 f"{int(wet.sum())} of {record['npoin']} nodes clear the "
                 f"{TELEMAC_WSE_WET_DEPTH_M} m wet floor"),
    }


def _to_utm(source: Any, utm_epsg: int) -> Any:
    """A lon/lat geometry source -> its shapely geometry in the mesh's metres."""
    from pyproj import Transformer
    from shapely.geometry import shape as _shape
    from shapely.ops import transform as _transform, unary_union

    from trid3nt_server.tools.processing._geometry_common import (
        flatten_geometries, read_geometry_doc,
    )

    geometry = unary_union([_shape(g)
                            for g in flatten_geometries(read_geometry_doc(source))])
    tr = Transformer.from_crs(4326, int(utm_epsg), always_xy=True)
    return _transform(tr.transform, geometry)


def _to_utm_point(lonlat: tuple[float, float],
                  utm_epsg: int) -> tuple[float, float]:
    """One lon/lat point in the mesh's own metres."""
    from pyproj import Transformer

    x, y = Transformer.from_crs(4326, int(utm_epsg), always_xy=True).transform(
        float(lonlat[0]), float(lonlat[1]))
    return (float(x), float(y))


def _to_lonlat_point(xy: tuple[float, float],
                     utm_epsg: int) -> tuple[float, float]:
    """One point in the mesh's own metres back in lon/lat."""
    from pyproj import Transformer

    lon, lat = Transformer.from_crs(int(utm_epsg), 4326, always_xy=True).transform(
        float(xy[0]), float(xy[1]))
    return (float(lon), float(lat))


def _mesh_nodes(mesh: Mapping[str, Any]) -> tuple[Any, Any]:
    """The accepted mesh's node coordinates and bed, read off its display face.

    The ``.2dm`` is the one readable record of the node numbering the geometry
    file carries, so the bed read here is the bed the solve starts from - which
    is what the outflow stage is measured over, and what a NESTOR design grade
    digs back to.
    """
    points, _cells, z, _lonlat = read_accepted_mesh_nodes(
        _mesh_field(mesh, "display_uri", missing=_reach_mesh_missing))
    if points is None or z is None:
        raise TelemacDyeScenarioError(
            "TELEMAC_MESH_BED_UNMEASURED",
            "the accepted mesh's display face carries no nodes or no painted bed, "
            "so this run has no ground to measure an outflow stage over and "
            "nowhere to settle a release; a reach mesh recipe paints its bed with "
            "set_bed.")
    return points, z

async def _settle_release(
    release_pair: tuple[float, float] | None, *, mesh: dict[str, Any],
    centerline: Any, centerline_utm: Any, utm_epsg: int, spill_fraction: float,
    node_xy: Any, initial_state: Mapping[str, Any],
) -> tuple[tuple[float, float], str]:
    """WHERE the source enters the water -> ``(lon, lat)`` and how it was decided.

    ONE seam, because there is one centerline. A SUPPLIED point is settled against
    real geometry: the domain polygon the accepted mesh was cut from decides
    whether it can be a source at all, and the declared centerline decides where on
    the river it sits. A point the domain does not hold raises through, because
    the only alternatives are releasing somewhere the user did not choose or
    solving a source outside the water.

    With none placed the source sits at ``spill_fraction`` along that SAME
    centerline - the line the section was cut between and the mesh was built over -
    walked downstream to the first station the ACCEPTED MESH holds. The centerline
    is the whole navigated stretch and the mesh is only the part of it the mapped
    banks left, so "on the line" and "in the domain" are two different claims and
    only the second one solves.

    Either way the settled point lands LAST on the nearest node ``initial_state``
    holds water at. Both earlier steps answer questions about geometry - is it in
    the domain, is it on the river - and neither asks whether there is water there
    at t0; a bankfull domain at low flow has mapped river that is dry ground when
    the run opens, and a source released onto it discharges into the bed.
    """
    from trid3nt_server.workflows.telemac.release_point import (
        contain_release_point, derive_release_on_mesh, domain_polygon_of,
        snap_release_to_wetted,
    )

    if release_pair is None:
        lonlat, note = await asyncio.to_thread(
            derive_release_on_mesh, centerline_utm=centerline_utm, mesh=mesh,
            fraction=spill_fraction)
    else:
        contained = await asyncio.to_thread(
            contain_release_point, point=release_pair,
            domain=domain_polygon_of(mesh.get("artifact")),
            flowline=centerline)
        lonlat, note = (contained.lon, contained.lat), contained.note

    wet_utm, moved_m, node = await asyncio.to_thread(
        snap_release_to_wetted, _to_utm_point(lonlat, utm_epsg),
        node_xy=node_xy, wet=initial_state["wet"], state=initial_state["note"])
    settled = (f"solved at mesh node {node}, which holds water at t0 "
               f"({initial_state['note']}), so nothing was moved")
    if moved_m > 0.0:
        settled = (f"moved {moved_m:.1f} m onto mesh node {node}, the nearest one "
                   f"holding water at t0 - the node it landed on was dry "
                   f"({initial_state['note']})")
    logger.info("release settled: %s", settled)
    journal_note(f"release point: {settled}."
                 + (f" Before that: {note}." if note else ""))
    return _to_lonlat_point(wet_utm, utm_epsg), "; ".join(
        part for part in (note, settled) if part)


# --------------------------------------------------------------------------- #
# The catchment: what the accepted mesh measures at the face it drains through.
# --------------------------------------------------------------------------- #
def _outlet_boundary(mesh: Mapping[str, Any]) -> tuple[dict[str, Any], int, str, int]:
    """The declared OUTLET: ``(topology, number, what its quad prescribes, count)``.

    The solver numbers its liquid boundaries by walking the geometry, the accepted
    topology recorded that numbering when the ``.cli`` was written, and the solver
    prints one flux per number in its own volume balance. So the number is what
    turns "the outlet" into the series the hydrograph reads, and the count is what
    lets the steering file state one stage-discharge entry per boundary in that
    same numbering.
    """
    topology = read_topology(_mesh_field(mesh, "topology_uri",
                                         missing=_catchment_mesh_missing))
    order = list(topology["liquid_boundary_order"])
    if _OUTLET_ROLE not in topology["roles"] or _OUTLET_ROLE not in order:
        raise RainOnGridError(
            f"no boundary node of the catchment mesh took the {_OUTLET_ROLE!r} "
            "role, so the basin has no outlet to drain through and no hydrograph "
            "to measure. Move the pour point onto the basin's own outlet, or mesh "
            "it finer so a boundary node reaches it.",
            error_code="TELEMAC_ROG_NO_OUTLET_NODES")
    number = order.index(_OUTLET_ROLE) + 1
    return (topology, number,
            str(topology["liquid_boundary_prescribes"][number - 1]), len(order))


def _bed_slope(nodes: Sequence[int], node_xy: Any, node_bed: Any,
               cells: Any) -> float:
    """The bed gradient at a face, over the ELEMENTS that face's nodes belong to.

    A friction slope is a fall over a run, and the only run a catchment outlet
    has is the ground it drains across: the elements touching the face are the
    mesh's own neighbourhood of it, so the plane fitted through their painted
    nodes is a measurement rather than a window somebody chose.
    """
    import numpy as np

    xy = np.asarray(node_xy, dtype=float)
    bed = np.asarray(node_bed, dtype=float)
    tri = np.asarray(cells, dtype=np.int64)
    patch = np.unique(tri[np.isin(tri, np.asarray(list(nodes),
                                                  dtype=np.int64)).any(axis=1)])
    patch = patch[np.isfinite(bed[patch])]
    if patch.size < 3:
        raise RainOnGridError(
            f"the outlet face touches {patch.size} painted mesh node(s), which is "
            "no ground to measure a bed slope over; a catchment mesh paints its "
            "bed with set_bed.",
            error_code="TELEMAC_ROG_OUTLET_SLOPE_UNMEASURED")
    plane = np.linalg.lstsq(
        np.column_stack([xy[patch, 0], xy[patch, 1], np.ones(patch.size)]),
        bed[patch], rcond=None)[0]
    slope = float(np.hypot(plane[0], plane[1]))
    if not (slope > 0.0):
        raise RainOnGridError(
            f"the bed over the {patch.size} nodes around the outlet is flat "
            f"(gradient {slope:.6g}), so there is no uniform-flow depth for the "
            "outlet to hold and its level would have to come from a gauge.",
            error_code="TELEMAC_ROG_OUTLET_SLOPE_UNMEASURED")
    return slope


def _measured_outlet(topology: Mapping[str, Any], node_xy: Any, node_bed: Any,
                     cells: Any, node_manning: Any, *,
                     q_ceiling_m3s: float, q_ceiling_basis: str) -> dict[str, Any]:
    """What the accepted mesh says about the face the basin drains through.

    Everything the outlet's rating curve is derived from, measured off the
    artifact itself: the section the outlet face cuts through the painted bed,
    the bed slope over the elements it touches, and the roughness the run's own
    friction field carries there. A curve derived from anything else would hold
    the outlet at a level computed over ground the solver never reads.
    """
    import numpy as np

    nodes = [int(n) for n in (topology["roles"].get(_OUTLET_ROLE) or ())]
    manning = np.asarray(node_manning, dtype=float)[nodes]
    coefficient = float(np.nanmedian(manning))
    if not np.isfinite(coefficient) or coefficient <= 0.0:
        raise RainOnGridError(
            f"the friction field carries {coefficient!r} at the outlet nodes, so "
            "the outlet's rating curve has no roughness to be derived under.",
            error_code="TELEMAC_ROG_OUTLET_FRICTION_UNMEASURED")
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

    Every drop this run holds falls on the mesh, so the gross rain rate over the
    meshed area is a CEILING on the outlet flux: infiltration only removes water
    and storage only delays it. It is the top of the flow range the rating curve
    is swept over, which is what "spanning the expected hydrograph" means without
    a hydrograph in hand.
    """
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
        raise RainOnGridError(
            f"a storm of {peak_mm_hr:g} mm/h over {area_m2:g} m2 puts no water on "
            "the catchment, so there is no flow range for the outlet's rating "
            "curve to span.",
            error_code="TELEMAC_ROG_NO_STORM")
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
    reach_polygon: Any = None,
    release_coords: Any = None,
    spill_fraction: float = 0.25,
    rain: Mapping[str, Any] | None = None,
    mesh_resolution_m: float | None = None,
    output_interval_min: float | None = None,
    friction_law: Any = None,
    friction_coefficient: float | None = None,
    continue_from: str | None = None,
    marker_label: str = "Release point",
    oil: Mapping[str, Any] | None = None,
    dredge: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Everything the reach MEASURES, before a single keyword is set.

    The MESH is the accepted one, so the numbers here are the triangulation that
    was presented rather than an equivalent rebuild: the timestep follows the edge
    that mesh was BUILT at, the bed comes off its own display face, and the
    boundary walk is the numbering its ``.cli`` recorded.

    The RELEASE POINT is settled against the one centerline before anything is
    staged: a supplied point outside the domain polygon refuses while the user
    can still move it, one inside it is put on the flowline, and an unplaced one
    walks ``spill_fraction`` along the same line. The marker goes on the canvas
    at the point the deck will carry, saying out loud who placed it.

    ``continue_from`` is a previous run's RESTART record. The instant that file
    stands at is read here, because every forcing series the sheet writes is the
    SAME declared scenario evaluated over the stretch of one absolute clock this
    run covers.
    """
    from trid3nt_server.workflows.telemac.release_layer import publish_release_point
    from trid3nt_server.emission.pipeline_emitter import current_emitter

    release_pair = coerce_lonlat_point(release_coords)
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
    # downstream FROM, so ``spill_fraction`` counts from upstream and the bed the
    # mesh carries slopes the same way.
    centerline_utm = await asyncio.to_thread(
        read_centerline_utm, centerline, utm_epsg,
        start_lonlat=(seed_lon, seed_lat))
    node_xy, node_bed = await asyncio.to_thread(_mesh_nodes, mesh)
    # WHAT THE RUN STARTS FROM. A fresh reach opens at the derived normal depth
    # laid bed-parallel, which is a positive depth at every node the deck writes
    # it over; a CONTINUED one opens at the restart record's own wet/dry field.
    initial_state = (
        await asyncio.to_thread(_continuation_state, str(continue_from))
        if continue_from else
        {"start_s": None, "wet": [True] * len(node_xy),
         "note": "the deck's own constant initial depth, the derived normal "
                 "depth laid bed-parallel over every node of the accepted mesh"})
    release_lonlat, release_note = await _settle_release(
        release_pair, mesh=mesh, centerline=centerline,
        centerline_utm=centerline_utm, utm_epsg=utm_epsg,
        spill_fraction=spill_fraction, node_xy=node_xy,
        initial_state=initial_state)
    # The marker rides BEFORE the solve, so the user sees the input against the
    # mesh rather than only in the results, and it carries the SETTLED point.
    await publish_release_point(
        current_emitter(), lon=release_lonlat[0], lat=release_lonlat[1],
        user_supplied=release_pair is not None, reach_name=reach["slug"],
        label=marker_label)

    topology = await asyncio.to_thread(
        read_topology, _mesh_field(mesh, "topology_uri",
                                   missing=_reach_mesh_missing))
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
    source_utm = _to_utm_point(release_lonlat, utm_epsg)
    # WHICH dataset painted the mesh's nodes. The worker opens a file and cannot
    # know, so the label travels with the file - otherwise the run's own metrics
    # could not tell a GLO-30 bed from the 3DEP one a ladder fell to.
    bed_source = str((mesh.get("provenance") or {}).get("bed_source") or "staged")
    settled: dict[str, Any] = {
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
        # The horizon every forcing series is written over, past the last
        # simulated instant so the time interpolation never reads off the end.
        "until_s": start_time_s + duration_s + _SERIES_TAIL_S,
        "friction_law": law,
        "friction_coefficient": float(normal["coefficient"]),
        "depth_m": round(float(normal["depth_m"]), 3),
        "outflow_stage_m": round(float(normal["stage_m"]), 3),
        "inflow_q_m3s": inflow_q,
        "normal": {k: (round(v, 6) if isinstance(v, float) else v)
                   for k, v in normal.items()},
        "liquid_boundary_order": list(topology["liquid_boundary_order"]),
        "liquid_boundary_prescribes": list(topology["liquid_boundary_prescribes"]),
        "release_at": [round(source_utm[0], 3), round(source_utm[1], 3)],
        "release_lon": round(release_lonlat[0], 6),
        "release_lat": round(release_lonlat[1], 6),
        "release_user_supplied": release_pair is not None,
        "release_note": release_note,
        "spill_fraction": float(min(max(spill_fraction, 0.0), 1.0)),
        "discharge_note": carrier_discharge.get("note"),
        # The on-mesh forcing the rain composite reads, and the provenance the
        # published layer carries for it. Absent is an answer: a run with no rain
        # states none, and the layer grows no rain row.
        "rain_mm_per_day": (rain or {}).get("mm_per_day"),
        "rain_note": (rain or {}).get("note"),
        "rain_rung": (rain or {}).get("rung"),
        "bed_source": bed_source,
        "continue_from": _PREVIOUS_DEST if continue_from else None,
        "restart": _RESTART,
        # Present as NOTHING unless this question asks for one: a composite that
        # reads a field the run holds as nothing expands to no keyword at all.
        "oil": None,
        "dredging": None,
        "mesh_inputs": [
            {"gs_uri": _mesh_field(mesh, "slf_uri", missing=_reach_mesh_missing),
             "dest": "river.slf"},
            {"gs_uri": _mesh_field(mesh, "cli_uri", missing=_reach_mesh_missing),
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
    if oil is not None:
        from ..helpers.oil import oil_inputs
        from ..helpers.substance import oil_preset

        settled["oil"] = {
            **oil_inputs(preset=oil_preset(oil["preset"]),
                         release_step=int(oil["release_step"]),
                         x=source_utm[0], y=source_utm[1]),
            # The write cadence is asked for in SECONDS; the only thing that
            # turns seconds into steps is the step this run is solved at.
            "period_steps": max(int(float(oil["drogues_period_s"])
                                    / max(time_step_s, 1e-6)), 1)}
    if dredge is not None and dredge.get("on"):
        from ..helpers.dredging import NESTOR_TIME_ORIGIN, dredge_field

        settled["dredging"] = {
            **await asyncio.to_thread(
                dredge_field, field=dredge["field"], rule=dredge["rule"],
                centerline_utm=centerline_utm,
                reach_polygon_utm=await asyncio.to_thread(_to_utm, reach_polygon,
                                                          utm_epsg),
                node_xy=node_xy, node_bed=node_bed, duration_s=duration_s,
                design_grade_m=dredge.get("design_grade_m")),
            # NESTOR reads absolute DATES, which map to sim seconds through the
            # origin the carrier's deck stamps.
            "time_origin": list(NESTOR_TIME_ORIGIN)}
    return settled


def _graphic_period(output_interval_min: float | None, time_step_s: float) -> int:
    """The GRAPHIC PRINTOUT PERIOD in solver steps, off the run's own timestep.

    The cadence is asked for in MINUTES, and the only thing that turns minutes
    into steps is the step this same run is solved at.
    """
    if output_interval_min is None:
        return _DEFAULT_GRAPHIC_PERIOD
    return max(1, round(float(output_interval_min) * 60.0 / float(time_step_s)))


async def settle_catchment(
    *,
    catchment: dict[str, Any],
    infiltration: dict[str, Any],
    rain: dict[str, Any],
    time_step_s: float,
    mesh_resolution_m: float | None = None,
    output_interval_min: float | None = None,
) -> dict[str, Any]:
    """Everything the catchment MEASURES at the face the basin drains through.

    ``catchment`` is the ACCEPTED mesh: its geometry, its boundary conditions and
    the outlet role its pour point matched are what the solve runs on, so the
    outlet's rating curve is derived over the triangulation that was presented
    rather than an equivalent rebuild.
    """
    from trid3nt_server.workflows.telemac.templates.rain_on_grid.cn_infiltration import (
        select_runoff_path,
    )

    decision = (select_runoff_path(hyetograph_mm=rain["series"])
                if rain["kind"] == "hyetograph"
                else select_runoff_path(
                    constant_intensity_mm_per_hr=rain["intensity_mm_per_hr"]))

    artifact = catchment.get("artifact")
    utm_epsg = int(getattr(artifact, "utm_epsg", 0) or 0)
    probes = dict(getattr(artifact, "probes", None) or {})
    provenance = dict(catchment.get("provenance") or {})
    topology, outlet_boundary, outlet_prescribes, n_liquid = _outlet_boundary(
        catchment)
    if outlet_prescribes != "elevation":
        raise RainOnGridError(
            f"liquid boundary {outlet_boundary} carries a .cli code quad that "
            f"prescribes {outlet_prescribes!r}, and a stage-discharge curve is "
            "read only where the depth is prescribed; the boundary file and the "
            "steering file would describe different outlets.",
            error_code="TELEMAC_BOUNDARY_PRESCRIBES_NOTHING")
    mesh_size_m = float(catchment.get("min_edge_m") or mesh_resolution_m or 0.0)
    name = str(getattr(artifact, "name", None) or "watershed")
    bed_source = str(provenance.get("bed_source") or "staged")
    duration_s = float(rain["duration_s"])

    points_utm, cells, node_bed, _lonlat = await asyncio.to_thread(
        mesh_nodes, catchment)
    q_ceiling, q_ceiling_basis = _rain_ceiling(rain, cells, points_utm)
    outlet = _measured_outlet(
        topology, points_utm, node_bed, cells, infiltration["node_manning"],
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
    return {
        "name": name,
        "title": f"{name} RAIN-ON-GRID",
        "domain_name": name,
        "utm_epsg": utm_epsg,
        "duration_s": duration_s,
        "time_step_s": float(time_step_s),
        "graphic_period": _graphic_period(output_interval_min, time_step_s),
        "rain_mm_per_day": float(rain["intensity_mm_per_hr"]) * 24.0,
        "rain_hours": (None if rain.get("rain_duration_s") is None
                       else float(rain["rain_duration_s"]) / 3600.0),
        "antecedent_moisture": int(infiltration["amc_condition"]),
        "initial_abstraction": 1,
        "friction_law": _ROG_FRICTION_LAW,
        "node_xy": [[round(float(x), 3), round(float(y), 3)]
                    for x, y in points_utm[:, :2]],
        "node_cn2": [round(float(v), 3) for v in infiltration["node_cn2"]],
        "node_manning": [round(float(v), 4)
                         for v in infiltration["node_manning"]],
        "hyetograph_blocks": ([[float(t), float(mm)] for t, mm in rain["blocks"]]
                              if decision.time_varying else None),
        "outlet_boundary": outlet_boundary,
        "n_liquid_boundaries": n_liquid,
        "rating": {
            "at_boundary": outlet_boundary, "of_boundaries": n_liquid,
            "rows": [[q, z] for q, z in rating["rows"]],
            "note": (f"derived Z(Q) at liquid boundary {outlet_boundary}: normal "
                     f"depth over the measured outlet section at {rating['law']} "
                     f"{rating['coefficient']:g}, bed slope "
                     f"{rating['slope']:.6f}, {outlet['q_ceiling_basis']}")},
        "runoff_path": decision.path,
        "runoff_reason": decision.reason,
        "rain": dict(rain),
        "infiltration": {"amc_condition": int(infiltration["amc_condition"])},
        "hyetograph_total_mm": (round(sum(float(mm) for _t, mm in rain["blocks"]), 4)
                                if decision.time_varying else None),
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
                                   missing=_catchment_mesh_missing),
             "dest": "rog.slf"},
            {"gs_uri": _mesh_field(catchment, "cli_uri",
                                   missing=_catchment_mesh_missing),
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
