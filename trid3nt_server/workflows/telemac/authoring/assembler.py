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
    read_accepted_mesh_nodes,
    read_centerline_utm,
)
from trid3nt_server.workflows.mesh.topology import RATING_CURVE_ROLE, read_topology

from ..helpers.catchment import mesh_nodes
from ..helpers.errors import (
    OpenWaterError,
    RainOnGridError,
    TelemacDyeScenarioError,
    TelemacDyeScenarioInputError,
)
from ..helpers.reach import MESH_H_FLOOR_M, coerce_lonlat_point, suggest_time_step_s
from ..helpers.uniform_flow import normal_depth_stage

logger = logging.getLogger("trid3nt_server.workflows.telemac.authoring.assembler")

__all__ = ["BASIN_BOUNDARY", "BASIN_GEOMETRY", "HARBOUR_GEOMETRY",
           "case_section", "new_rundir", "settle_basin", "settle_catchment",
           "settle_harbour", "settle_reach", "stage_run",
           "stage_telemac_manifest"]

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


# --------------------------------------------------------------------------- #
# The staging flow.
# --------------------------------------------------------------------------- #
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
        raise OpenWaterError(
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

    Written by the one manifest writer, under the ``case`` dispatch key."""
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
    return TelemacDyeScenarioError("TELEMAC_MESH_SECTION_UNMEASURED", message)


def _outlet_section_unmeasured(message: str) -> Exception:
    return RainOnGridError(message,
                           error_code="TELEMAC_ROG_OUTLET_SECTION_UNMEASURED")


def _reach_mesh_missing(message: str) -> Exception:
    return TelemacDyeScenarioError("TELEMAC_MESH_NOT_ACCEPTED", message)


def _catchment_mesh_missing(message: str) -> Exception:
    return RainOnGridError(message, error_code="TELEMAC_ROG_MESH_NOT_ACCEPTED")



# --------------------------------------------------------------------------- #
# The reach: what the accepted mesh measures before a keyword is set.
# --------------------------------------------------------------------------- #
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

    A continued run is the same declared scenario over an extended horizon."""
    import tempfile

    import numpy as np

    from trid3nt_server.workflows.solver.solver import _download_object
    from trid3nt_server.workflows.telemac.products.result_reader import read_selafin
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
    # Only the file can say where the continued leg stopped: the engine writes the
    # restart at its own last time step, which is not the graphic period, not the
    # asked duration, and not anything the server can compute from the ask. The
    # depth at that instant is the initial state, which decides where a release
    # can land.
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

    from trid3nt_server.workflows.shared.geometry import (
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

    The ``.2dm`` is the one readable record of the geometry file's numbering."""
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

    A supplied point the domain polygon does not hold raises rather than moves."""
    from trid3nt_server.workflows.telemac.helpers.release_point import (
        contain_release_point, derive_release_on_mesh, domain_polygon_of,
        snap_release_to_wetted,
    )

    # With no point placed the source sits at ``spill_fraction`` along the declared
    # centerline, walked downstream to the first station the ACCEPTED MESH holds:
    # the centerline is the whole navigated stretch and the mesh is only the part
    # of it the mapped banks left, so "on the line" and "in the domain" are two
    # different claims and only the second one solves.
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

    # The settled point lands LAST on the nearest node holding water at t0. Both
    # steps above ask about geometry only; a bankfull domain at low flow has mapped
    # river that is dry ground when the run opens, and a source released onto it
    # discharges into the bed.
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

    The number is the solver's own liquid-boundary walk order, 1-based."""
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

    Section, slope and roughness, every one measured off the artifact itself."""
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

    The mesh is the ACCEPTED one, never an equivalent rebuild."""
    from trid3nt_server.workflows.telemac.helpers.release_layer import publish_release_point
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
    # ``continue_from`` names a previous run's restart record, and the instant it
    # stands at is read here because every forcing series the sheet writes is the
    # same declared scenario evaluated over the stretch of one absolute clock.
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
        # The last simulated instant. A series composite writes its own tail
        # past this, so the tail is stated once - where the series is.
        "until_s": start_time_s + duration_s,
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

        preset = oil_preset(oil["preset"])
        settled["oil"] = {
            "preset": preset,
            **oil_inputs(preset=preset, release_step=int(oil["release_step"]),
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

    The cadence is asked in minutes; only this run's own step converts it."""
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

    ``catchment`` is the ACCEPTED mesh, never an equivalent rebuild."""
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
        # The rain window is stated only when it CLOSES inside the run: a storm
        # that outlasts the horizon never stops, and a keyword saying so would
        # be an end nothing reaches. The recession limb is what the window is
        # for, so a storm that produces none states nothing.
        "rain_hours": (float(rain["rain_duration_s"]) / 3600.0
                       if rain.get("rain_duration_s") is not None
                       and 0.0 < float(rain["rain_duration_s"]) < duration_s
                       else None),
        "antecedent_moisture": int(infiltration["amc_condition"]),
        "initial_abstraction": 1,
        "friction_law": _ROG_FRICTION_LAW,
        # The mesh's own coordinates, which nothing else on the run carries.
        # The per-node curve numbers and roughnesses are the INFILTRATION step's
        # and are read where they were produced - carrying them again here would
        # be the same field on the run twice.
        "node_xy": [[round(float(x), 3), round(float(y), 3)]
                    for x, y in points_utm[:, :2]],
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
        # The infiltration surface's own record, WITHOUT the per-node fields:
        # those are that step's and are read where they were produced, so the
        # run does not carry the same field twice.
        "infiltration": {key: value for key, value in infiltration.items()
                         if not key.startswith("node_")},
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


# --------------------------------------------------------------------------- #
# Open water: what an accepted AOI mesh measures before a keyword is set.
# --------------------------------------------------------------------------- #
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
    return OpenWaterError(message, error_code="ARTEMIS_MESH_NOT_ACCEPTED")


def _basin_mesh_missing(message: str) -> Exception:
    return OpenWaterError(message, error_code="TELEMAC3D_MESH_NOT_ACCEPTED")


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
    """What the accepted harbour mesh measures -> what the agitation sheet reads.

    A mesh naming no liquid boundary refuses: a wave has no edge to enter by."""
    from trid3nt_server.workflows.shared.supplied_geometry import supplied_polylines

    facts = _mesh_facts(mesh, missing=_harbour_mesh_missing)
    utm_epsg = int(facts["utm_epsg"])
    topology = read_topology(_mesh_field(mesh, "topology_uri",
                                         missing=_harbour_mesh_missing))
    open_nodes = [int(n) for n in (topology["roles"].get("open") or ())]
    if not open_nodes:
        raise OpenWaterError(
            "the accepted mesh designates no liquid boundary, so a prescribed "
            f"incident wave has no edge to enter the domain through: {topology['states']}. "
            "Name an open stretch on the mesh (identify_ocean_boundary_sections) "
            "before solving a wave on it.",
            error_code="ARTEMIS_MESH_CLOSED")
    cli_text, boundary_nodes = _boundary_file(mesh, missing=_harbour_mesh_missing)
    points_utm, _cells, node_bed, _lonlat = await asyncio.to_thread(
        read_accepted_mesh_nodes,
        _mesh_field(mesh, "display_uri", missing=_harbour_mesh_missing))

    polylines = await asyncio.to_thread(
        supplied_polylines, structure, label="structure",
        code="ARTEMIS_STRUCTURE_INVALID") or []
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
        "title": f"ARTEMIS AGITATION {facts['mesh_name']}",
        "domain_name": facts["mesh_name"],
        "domain_slug": _slug(facts["mesh_name"]),
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
        "title": f"TELEMAC3D STRATIFIED {facts['mesh_name']}",
        "domain_name": facts["mesh_name"],
        "domain_slug": _slug(facts["mesh_name"]),
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
