"""The CORRIDOR mesher: a river reach, meshed along the water it actually occupies.

The domain is the reach's own water polygon where one is mapped and an offset
ribbon where none is, cut at the two end transects that become the inflow and
outflow. The triangulation itself runs where the triangulator lives - inside the
TELEMAC image, on the staged centerline and bank polygons the server fetched - so
this file wraps that build rather than restating it, and the mesh a run gets is
byte-for-byte the mesh the image has always produced from the same inputs.

What the LIFT buys: the reach mesher is reachable as a mesh ask in its own right,
so a corridor can be built, probed, edited and accepted before any deck exists,
instead of coming into being as a side effect of authoring one.

A corridor mesh carries NO bed. Elevation is fitted to the reach at deck-authoring
time from the staged terrain, and a mesh that claimed a bed it never sampled would
read to a solver as ground at sea level.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Mapping

from trid3nt_contracts import new_ulid

from trid3nt_server.workflows.mesh.meshers import (
    EditAction,
    Mesh,
    MeshField,
    MeshToolError,
    apply_layer_edits_action,
    register_mesher,
)

logger = logging.getLogger("trid3nt_server.workflows.mesh.meshers.corridor_tin")

__all__ = ["CORRIDOR_TIN", "build"]

#: Bank sources the corridor accepts: the real per-station polygons, or the
#: assumed constant-width ribbon. There is no third, and no silent fallback
#: between them - a reach with no mapped polygon refuses and names the retry.
_BANK_SOURCES = ("nhd_area", "constant_ribbon")

#: A healthy mesh-only build is tens of seconds; this bounds a triangulator that
#: will not converge so a mesh ask cannot park a turn indefinitely.
_BUILD_TIMEOUT_S = 240

_FIELDS = (
    MeshField("kind", types=(str,), choices=("unstructured_tri",),
              default="unstructured_tri",
              doc="unstructured_tri - a corridor interior is triangulated"),
    MeshField("domain", types=(dict,), required=True,
              doc="the acquired reach: its name, slug and the flowline seed the "
                  "corridor is navigated from"),
    MeshField("extent_km", types=(int, float), default=6.0,
              doc="how far the corridor runs along the flow axis, in km"),
    MeshField("width_m", types=(int, float), default=60.0,
              doc="the assumed cross-stream channel width, in metres"),
    MeshField("banks", types=(str,), choices=_BANK_SOURCES, default="nhd_area",
              doc="where the corridor BOUNDARY comes from: real NHDArea water "
                  "polygons, or an assumed constant-width ribbon"),
    MeshField("refine", types=(dict,),
              doc="{'edge_length': target triangle edge in metres, 'mode': the "
                  "named resolution preset} - either may be absent"),
)


def build(spec: Mapping[str, Any]) -> Mesh:
    """Navigate the reach, stage its geometry, and triangulate the corridor."""
    return _await(_build(spec))


async def _build(spec: Mapping[str, Any]) -> Mesh:
    from trid3nt_server.workflows.telemac.steps.deck import normalize_bank_source
    from trid3nt_server.workflows.telemac.steps.reach import (
        resolve_reach_river,
        suggest_mesh_size_m,
        suggest_time_step_s,
    )

    domain = dict(spec["domain"])
    reach, seed = _reach_and_seed(domain)
    extent_km = float(spec.get("extent_km", 6.0))
    width_m = float(spec.get("width_m", 60.0))
    banks = normalize_bank_source(spec.get("banks"))
    refine = dict(spec.get("refine") or {})
    edge_length = refine.get("edge_length")

    sizing = suggest_mesh_size_m(
        reach_length_km=extent_km, channel_width_m=width_m,
        resolution=str(refine.get("mode") or "auto"),
        override_m=float(edge_length) if edge_length else None)
    run_tag = new_ulid()
    # with_bed False on purpose: this build samples no elevations, so fetching a
    # raster it would never read is work nobody asked for.
    river = await resolve_reach_river(
        reach=reach, seed=seed, run_tag=run_tag, reach_length_km=extent_km,
        bank_source=banks, with_bed=False)
    ask = {
        "name": reach["slug"],
        "seed_lon": round(float(river["provenance"]["seed_lon"]), 6),
        "seed_lat": round(float(river["provenance"]["seed_lat"]), 6),
        "nav_direction": "DM",
        "distance_km": extent_km,
        "channel_width_m": width_m,
        "bank_source": banks,
        "mesh_size_m": sizing.mesh_size_m,
        "time_step_s": suggest_time_step_s(sizing.mesh_size_m),
    }
    run_id, metrics = await _triangulate(ask, run_tag, river["inputs"])
    files = await asyncio.to_thread(_stage_outputs, run_id)
    points, cells = await asyncio.to_thread(_read_geometry, files["slf_uri"])

    utm_epsg = int(metrics.get("utm_epsg") or 0)
    if utm_epsg <= 0:
        raise MeshToolError(
            "MESH_CORRIDOR_NO_CRS",
            f"the corridor build (run {run_id}) reported no UTM zone, so the "
            "mesh cannot state what its coordinates mean.")
    return Mesh(
        points=points, cells=cells, crs_authid=f"EPSG:{utm_epsg}", bed=None,
        meta={
            "utm_epsg": utm_epsg,
            "lonlat_bbox": tuple(float(v) for v in metrics["bbox4326"]),
            "domain": domain,
            # The accepted topology travels WITH the mesh, so the solve that
            # consumes it runs on this triangulation rather than an equivalent
            # rebuild of it.
            "files": files,
            "probes": {
                "domain_mode": metrics.get("domain_mode"),
                "island_count": metrics.get("n_islands"),
                "water_coverage_frac": metrics.get("water_coverage_frac"),
                "inflow_nodes": metrics.get("n_inflow_nodes"),
                "outflow_nodes": metrics.get("n_outflow_nodes"),
            },
            "artifact": {
                # A corridor is bed-less and still TELEMAC's to solve: the reach
                # deck stages this topology and fits the bed onto it from the
                # terrain that run acquires, so the geometry file alone understates
                # what the mesh is good for.
                "engine_compat": ["telemac"],
                "provenance": {
                    "extent_km": extent_km,
                    "width_m": width_m,
                    "bank_source": str(metrics.get("bank_source") or banks),
                    "mesh_size_m": float(sizing.mesh_size_m),
                    "mesh_size_note": sizing.cap_note,
                    "resolution_label": sizing.label,
                    "sizing_source": _sizing_source(metrics, banks),
                    # The corridor is bed-less by construction: elevation is fitted
                    # to the reach when the deck is authored, from the terrain that
                    # run stages.
                    "dem_source": "bed: NOT SAMPLED - a corridor's elevation is "
                                  "fitted at deck-authoring time",
                    "centerline_sha256": river["provenance"]["centerline_sha256"],
                    "centerline_comids": river["provenance"]["centerline_comids"],
                    "seed_rung": river["provenance"]["seed_rung"],
                    "build_run_id": run_id,
                },
            },
            "synthetic_inputs": [
                {"param": "extent_km", "value": extent_km, "units": "km",
                 "basis": "user"},
                {"param": "channel_width_m", "value": width_m, "units": "m",
                 "basis": "user"},
                {"param": "mesh_resolution_m", "value": float(sizing.mesh_size_m),
                 "units": "m", "basis": "derived", "note": sizing.label},
                {"param": "mesh_domain",
                 "value": f"{points.shape[0]} nodes / {cells.shape[0]} elements",
                 "basis": "derived",
                 "real_source_if_any": _sizing_source(metrics, banks)},
            ],
        })


def _reach_and_seed(domain: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split the declared domain into the reach and the mid-reach seed it carries.

    The seed is the point the corridor is navigated from, so it is part of the
    domain rather than a second ask; a domain that carries none refuses here
    instead of navigating from a place centroid nobody chose.
    """
    reach = dict(domain.get("reach") or domain)
    seed = domain.get("seed")
    if not isinstance(seed, Mapping) or not seed:
        raise MeshToolError(
            "MESH_CORRIDOR_NO_SEED",
            "the corridor domain carries no mid-reach seed, so there is no point "
            "on the flowline to navigate the corridor from. Acquire the reach "
            "domain (which resolves the seed) before asking for its mesh.")
    if not reach.get("slug"):
        raise MeshToolError(
            "MESH_CORRIDOR_NO_REACH",
            f"the corridor domain names no reach ({sorted(reach)}); the corridor "
            "is built along an acquired reach, not around a bare point.")
    return reach, dict(seed)


async def _triangulate(ask: Mapping[str, Any], run_tag: str,
                       inputs: list[dict[str, str]]) -> tuple[str, dict[str, Any]]:
    """Run the corridor triangulation in its box -> ``(run_id, metrics)``.

    The box builds the mesh and stops: no bed, no solve. Its typed refusals -
    banks unavailable, a reach too short for its own width - are surfaced as they
    are, because both name a corrective the caller can act on.
    """
    from trid3nt_server.workflows.solver.solver import run_solver, wait_for_completion
    from trid3nt_server.workflows.telemac.run_telemac import TELEMAC_SOLVER_NAME
    from trid3nt_server.workflows.telemac.steps.deck import stage_manifest
    from trid3nt_server.workflows.telemac.steps.solve import (
        raise_if_banks_unavailable,
        raise_if_reach_degenerate,
        read_run_metrics,
    )

    manifest_uri = await asyncio.to_thread(
        stage_manifest, dict(ask), run_tag, mesh_only=True, inputs=list(inputs))
    handle = run_solver(solver=TELEMAC_SOLVER_NAME, model_setup_uri=manifest_uri,
                        compute_class="small")
    result = await wait_for_completion(handle, poll_interval_s=3,
                                       timeout_s=_BUILD_TIMEOUT_S)
    run_id = getattr(result, "run_id", None) or handle.run_id
    if result is None or result.status != "complete":
        metrics = await asyncio.to_thread(read_run_metrics, run_id)
        raise_if_banks_unavailable(metrics)
        raise_if_reach_degenerate(metrics)
        raise MeshToolError(
            "MESH_CORRIDOR_BUILD_FAILED",
            f"the corridor build did not complete "
            f"(status={getattr(result, 'status', None)}, run {run_id}).")
    metrics = await asyncio.to_thread(read_run_metrics, run_id)
    if int(metrics.get("npoin") or 0) <= 0:
        raise MeshToolError(
            "MESH_CORRIDOR_BUILD_FAILED",
            f"the corridor build (run {run_id}) reported no node count.")
    return run_id, metrics


#: What the mesh-only build leaves behind, and the artifact field each one lands
#: under. The SELAFIN is the geometry; the ``.cli`` is only valid against that
#: geometry's own boundary numbering; the topology bundle carries what neither
#: file can - which stretch of the boundary is the inflow and which the outflow.
_BUILD_OUTPUTS: Mapping[str, str] = {
    "slf_uri": "river.slf",
    "cli_uri": "river.cli",
    "topology_uri": "river_mesh.npz",
}


def _stage_outputs(run_id: str) -> dict[str, str]:
    """Bring the build's geometry files local -> ``{artifact field: local path}``."""
    import os
    from pathlib import Path

    from trid3nt_contracts import new_ulid
    from trid3nt_server.workflows.solver.solver import _get_runs_bucket, _get_s3_client

    rundir = (Path(os.environ.get("TRID3NT_RUNS_DIR", "/tmp"))
              / f"mesh-{new_ulid()}")
    rundir.mkdir(parents=True, exist_ok=True)
    client, bucket = _get_s3_client(), _get_runs_bucket()
    staged: dict[str, str] = {}
    for field_name, basename in _BUILD_OUTPUTS.items():
        local = rundir / basename
        local.write_bytes(client.get_object(
            Bucket=bucket, Key=f"{run_id}/{basename}")["Body"].read())
        staged[field_name] = str(local)
    return staged


def _read_geometry(slf_path: str) -> tuple[Any, Any]:
    """Read the built geometry back -> ``(points (N,2) metres, cells (M,3))``."""
    from pathlib import Path

    import numpy as np

    from trid3nt_server.workflows.telemac.postprocess_telemac import read_selafin

    geometry = read_selafin(Path(slf_path))
    points = np.column_stack([np.asarray(geometry["x"], dtype=float),
                              np.asarray(geometry["y"], dtype=float)])
    return points, np.asarray(geometry["ikle"], dtype=np.int64)


def _sizing_source(metrics: Mapping[str, Any], banks: str) -> str:
    """What the corridor boundary ACTUALLY came from, copied from the build.

    The build reports whether it meshed the real water polygon or fell to the
    ribbon, and which bank source it ended on. Claiming the real river for a mesh
    that ribboned is the same false promise as an undeclared substitution.
    """
    mode = str(metrics.get("domain_mode") or "").strip()
    source = str(metrics.get("bank_source") or banks).strip()
    if mode:
        return f"{mode} corridor domain; banks from {source}"
    return (f"corridor domain UNREPORTED by the build; banks were asked of "
            f"{source}")


def _await(coro: Any) -> Any:
    """Run ``coro`` to completion from a synchronous mesher build.

    A build is demanded from a worker thread, which has no running loop; being
    called from one instead would mean the caller blocked the loop on a container
    run, so it refuses and names the offload rather than deadlocking.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise MeshToolError(
        "MESH_CORRIDOR_ON_LOOP",
        "the corridor build runs a container and reads an object store, so it "
        "cannot be demanded on the event loop; offload the build to a thread.")


def _set_resolution(mesh: Mesh, *, edge_length_m: float) -> Mesh:
    """Re-triangulate the corridor at a different target edge length."""
    built = dict(mesh.meta["artifact"]["provenance"])
    return build({
        "domain": dict(mesh.meta["domain"]),
        "extent_km": built["extent_km"],
        "width_m": built["width_m"],
        "banks": built["bank_source"],
        "refine": {"edge_length": float(edge_length_m)}})


def _set_extent(mesh: Mesh, *, extent_km: float) -> Mesh:
    """Re-navigate and re-triangulate a corridor of a different length."""
    built = dict(mesh.meta["artifact"]["provenance"])
    return build({
        "domain": dict(mesh.meta["domain"]),
        "extent_km": float(extent_km),
        "width_m": built["width_m"],
        "banks": built["bank_source"],
        "refine": {"edge_length": built["mesh_size_m"]}})


# --------------------------------------------------------------------------- #
# Re-adopting a hand-edited corridor.
# --------------------------------------------------------------------------- #
#: How far a new boundary node may sit from the nearest boundary node whose role
#: the build classified before that role stops describing it. One channel width is
#: the corridor's own cross-stream scale, so a node further than this from every
#: classified node is on a stretch the hand-edit reshaped rather than a moved copy
#: of one the build already ruled on.
_ROLE_CARRY_LIMIT_WIDTHS = 1.0


def _readopt(previous: Mesh, adopted: Mesh) -> Mesh:
    """Rewrite the corridor's solver files from the adopted nodes and cells.

    The solve does not read a corridor's SELAFIN - it rebuilds one from the
    topology bundle with the bed fitted to the reach at authoring time - so the
    bundle is what a hand-edit has to reproduce: the boundary walk, the IPOBO
    ranking derived from it, the per-node boundary-condition codes, and which
    stretch of the boundary the flow enters and leaves by.

    Geometry states the first three. The last is the BUILD's classification, and
    an edited layer carries none of it, so every new boundary node takes the role
    of the nearest node the build classified - exact for the nodes the edit left
    alone - and the distance that carry spanned is reported rather than assumed.
    """
    import numpy as np

    from workers.telemac._staged_mesh import (
        STAGED_MESH_FILENAME,
        staged_mesh_bundle,
        write_mesh_bundle,
    )

    from trid3nt_server.workflows.mesh.telemac_build import write_bottom_selafin

    built = _previous_bundle(previous, staged_mesh_bundle)
    points = np.asarray(adopted.points, dtype=float)
    cells = _oriented(points, np.asarray(adopted.cells, dtype=np.int64))
    rings = _boundary_rings(cells)
    ring = np.concatenate(rings)

    width_m = float(dict(previous.meta["artifact"]["provenance"])["width_m"])
    roles, carried_m = _carry_roles(points[ring], built,
                                    limit_m=width_m * _ROLE_CARRY_LIMIT_WIDTHS)
    mesh = _bundle_dict(points, cells, rings, ring, roles, built)

    rundir = _readopt_rundir()
    files = {
        "slf_uri": str(write_bottom_selafin(
            str(rundir / "river.slf"), points, cells,
            np.zeros(points.shape[0], dtype=float), title="TRID3NT REACH CORRIDOR")),
        "cli_uri": _write_cli(mesh, rundir / "river.cli"),
        "topology_uri": write_mesh_bundle(mesh, str(rundir / STAGED_MESH_FILENAME)),
    }
    probes = {**{k: v for k, v in dict(previous.meta.get("probes") or {}).items()
                 if k in ("domain_mode", "water_coverage_frac")},
              "island_count": len(rings) - 1,
              "inflow_nodes": int(mesh["n_in"]),
              "outflow_nodes": int(mesh["n_out"]),
              "boundary_role_carry_m": carried_m}
    artifact = {**dict(adopted.meta.get("artifact") or {}),
                "engine_compat": ["telemac"]}
    return Mesh(points=points, cells=cells, crs_authid=adopted.crs_authid,
                bed=None, meta={**dict(adopted.meta), "files": files,
                                "probes": probes, "artifact": artifact})


def _previous_bundle(previous: Mesh, reader: Any) -> dict[str, Any]:
    """The topology bundle the edited mesh was hand-edited FROM."""
    from pathlib import Path

    uri = dict(previous.meta.get("files") or {}).get("topology_uri")
    if not uri or not Path(str(uri)).is_file():
        raise MeshToolError(
            "MESH_CORRIDOR_NO_TOPOLOGY",
            f"the corridor this layer was edited from carries no readable topology "
            f"bundle ({uri!r}), so the boundary roles the build classified cannot "
            "be carried onto the edited nodes and the edit would leave a mesh no "
            "solve could be staged on.")
    built = reader(str(Path(str(uri)).parent))
    if built is None:
        raise MeshToolError(
            "MESH_CORRIDOR_NO_TOPOLOGY",
            f"the topology bundle beside {uri!r} is not the corridor bundle a "
            "solve is staged from.")
    return built


def _readopt_rundir() -> Any:
    import os
    from pathlib import Path

    rundir = (Path(os.environ.get("TRID3NT_RUNS_DIR", "/tmp"))
              / f"mesh-{new_ulid()}")
    rundir.mkdir(parents=True, exist_ok=True)
    return rundir


def _oriented(points: Any, cells: Any) -> Any:
    """Triangles wound counter-clockwise, which is what puts the domain on the left."""
    import numpy as np

    a, b, c = cells[:, 0], cells[:, 1], cells[:, 2]
    x, y = points[:, 0], points[:, 1]
    twice_area = ((x[b] - x[a]) * (y[c] - y[a]) - (x[c] - x[a]) * (y[b] - y[a]))
    fixed = np.array(cells, dtype=np.int64, copy=True)
    fixed[twice_area < 0] = fixed[twice_area < 0][:, ::-1]
    return fixed


def _boundary_rings(cells: Any) -> list[Any]:
    """The closed boundary walks, outer ring first, in domain-on-the-left order."""
    import numpy as np

    counts: dict[tuple[int, int], int] = {}
    directed: dict[tuple[int, int], tuple[int, int]] = {}
    for tri in cells:
        for k in range(3):
            u, v = int(tri[k]), int(tri[(k + 1) % 3])
            key = (min(u, v), max(u, v))
            counts[key] = counts.get(key, 0) + 1
            directed[key] = (u, v)
    boundary = [directed[k] for k, n in counts.items() if n == 1]
    nxt = {u: v for u, v in boundary}
    if len(nxt) != len(boundary):
        raise MeshToolError(
            "MESH_CORRIDOR_NON_MANIFOLD",
            f"the edited layer's boundary is not a set of simple closed walks "
            f"({len(boundary)} boundary edges leave {len(nxt)} distinct nodes), so "
            "it has no IPOBO ranking a solve could be staged with.")
    rings: list[Any] = []
    unvisited = set(nxt)
    while unvisited:
        start = next(iter(unvisited))
        walk = [start]
        cur = nxt[start]
        while cur != start:
            walk.append(cur)
            cur = nxt[cur]
        unvisited -= set(walk)
        rings.append(np.asarray(walk, dtype=np.int64))
    rings.sort(key=len, reverse=True)
    return rings


def _carry_roles(ring_xy: Any, built: Mapping[str, Any],
                 *, limit_m: float) -> tuple[Any, dict[str, float]]:
    """Each new boundary node's role, taken from the nearest classified node."""
    import numpy as np
    from scipy.spatial import cKDTree

    old_ring = np.asarray(built["ring"], dtype=np.int64)
    old_xy = np.column_stack([np.asarray(built["X"], dtype=float)[old_ring],
                              np.asarray(built["Y"], dtype=float)[old_ring]])
    old_cls = np.asarray(built["cls"], dtype=object)
    distance, index = cKDTree(old_xy).query(np.asarray(ring_xy, dtype=float), k=1)
    roles = np.asarray([str(old_cls[int(i)]) for i in index], dtype=object)
    beyond = float(distance.max()) if distance.size else 0.0
    if beyond > limit_m:
        raise MeshToolError(
            "MESH_CORRIDOR_ROLES_UNCARRIABLE",
            f"an edited boundary node sits {beyond:.1f} m from the nearest node "
            f"the build classified (limit {limit_m:.1f} m, one channel width), so "
            "whether the flow enters or leaves there is not something this edit "
            "can answer. Re-run the corridor build instead of reshaping its ends.")
    return roles, {"max": beyond, "median": float(np.median(distance))
                   if distance.size else 0.0,
                   "measured": "distance from each edited boundary node to the "
                               "nearest node the build classified"}


#: The boundary-condition codes each role writes, as TELEMAC's
#: (LIHBOR, LIUBOR, LIVBOR, LITBOR) quadruple.
_ROLE_CODES: Mapping[str, tuple[int, int, int, int]] = {
    "wall": (2, 2, 2, 2),
    "inflow": (4, 5, 5, 5),
    "outflow": (5, 4, 4, 4),
}


def _bundle_dict(points: Any, cells: Any, rings: list[Any], ring: Any,
                 roles: Any, built: Mapping[str, Any]) -> dict[str, Any]:
    """The built-mesh dict the worker's bundle writer takes."""
    import numpy as np

    npoin = int(points.shape[0])
    nptfr = int(ring.size)
    ipob = np.zeros(npoin, dtype=np.int32)
    for rank, node in enumerate(ring, start=1):
        ipob[int(node)] = rank
    codes = np.asarray([_ROLE_CODES[str(r)] for r in roles], dtype=np.int64)
    in_nodes = {int(n) for n, r in zip(ring, roles) if str(r) == "inflow"}
    out_nodes = {int(n) for n, r in zip(ring, roles) if str(r) == "outflow"}
    if not in_nodes or not out_nodes:
        raise MeshToolError(
            "MESH_CORRIDOR_NO_FLOW_BOUNDARY",
            f"the edited corridor has {len(in_nodes)} inflow and "
            f"{len(out_nodes)} outflow boundary nodes; a reach solve forces "
            "discharge in at one end cap and out at the other, so a mesh missing "
            "either cannot be solved on.")
    return {
        "X": points[:, 0], "Y": points[:, 1], "ikle": cells, "npoin": npoin,
        "ring": ring, "ipob": ipob, "nptfr": nptfr,
        "lihbor": codes[:, 0], "liubor": codes[:, 1],
        "livbor": codes[:, 2], "litbor": codes[:, 3],
        "cls": roles, "in_nodes": in_nodes, "out_nodes": out_nodes,
        "n_in": len(in_nodes), "n_out": len(out_nodes),
        "n_islands": len(rings) - 1, "boundary_rings": rings,
        "centerline": np.asarray(built["centerline"], dtype=float),
        "domain_mode": built.get("domain_mode"),
        "water_coverage_frac": built.get("water_coverage_frac"),
        "banks_ok": bool(built.get("banks_ok")),
        "smooth_tries": int(built.get("smooth_tries") or 0),
    }


def _write_cli(mesh: Mapping[str, Any], path: Any) -> str:
    """The boundary-conditions file, in the fixed column order TELEMAC reads."""
    ring = mesh["ring"]
    lines = [
        f"{int(mesh['lihbor'][k])} {int(mesh['liubor'][k])} "
        f"{int(mesh['livbor'][k])}  0.000 0.000 0.000 0.000  "
        f"{int(mesh['litbor'][k])}  0.000 0.000 0.000 "
        f"{int(ring[k]) + 1:>11d} {k + 1:>11d}   # {mesh['cls'][k]}"
        for k in range(int(mesh["nptfr"]))]
    path.write_text("\n".join(lines) + "\n")
    return str(path)


CORRIDOR_TIN = register_mesher(
    "corridor_tin",
    build,
    actions=(
        EditAction(
            name="set_resolution", apply=_set_resolution,
            inputs={"edge_length_m": MeshField(
                "edge_length_m", types=(int, float), required=True,
                doc="the new target triangle edge, in metres")},
            doc="Re-triangulate the corridor at a different edge length."),
        EditAction(
            name="set_extent", apply=_set_extent,
            inputs={"extent_km": MeshField(
                "extent_km", types=(int, float), required=True,
                doc="the new corridor length along the flow axis, in km")},
            doc="Re-navigate the reach and re-triangulate a longer or shorter "
                "corridor."),
        apply_layer_edits_action(regenerate=_readopt),
    ),
    fields=_FIELDS,
)
