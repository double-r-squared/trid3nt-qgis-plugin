"""Live proof: a hand-edited corridor is accepted, staged, and SOLVED on.

The repro the round-2 re-verify reproduced, end to end against the real stack:
build a corridor through its box, hand-edit the mesh layer the way QGIS would,
accept it, author the DO-sag deck on it and run the solve. What it proves is
that the geometry the worker ran on IS the edited one - the run's own metrics
report the node and element counts of the accepted mesh, and the mesh it used
was the staged bundle rather than one it built for itself.

Run:
  cd /home/nate/Documents/trid3nt-local
  set -a; source .env.local; set +a
  venvs/agent/bin/python scripts/proof_corridor_hand_edit_solve.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

import numpy as np

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("proof_corridor_hand_edit_solve")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

LOCATION = "Eel River near Scotia, California"

#: A DO-sag reach, coarse on purpose: the point is which geometry the solve ran
#: on, and a coarse corridor reaches that answer in minutes.
DO_SAG = {"bod_mgl": 20.0, "upstream_do_mgl": 8.0, "saturation_mgl": 9.0,
          "water_temp_c": 20.0, "k1_per_day": 0.3, "k2_per_day": 0.5,
          "k2_formula": 0, "standard_mgl": 5.0}


async def _reach_and_seed() -> tuple[dict, dict]:
    from trid3nt_server.workflows.telemac.steps.reach import (
        geocode_reach, reach_seed,
    )

    # No flowline is prefetched here: the seed falls to the geocoded centroid,
    # which the corridor build NLDI-snaps to the nearest flowline COMID anyway.
    reach = await geocode_reach(location=LOCATION, bbox=None)
    return reach, await reach_seed(reach=reach, rivers=None)


def _hand_edit(session, workdir: Path) -> Path:
    """The accepted layer as a human refined it in QGIS.

    One interior triangle is split at its centroid: the node count rises by one
    and the element count by two, which no rebuild of the same corridor could
    produce. That is what makes the geometry the solve reports DISCRIMINATE
    between the mesh that was accepted and an equivalent rebuild of it.
    """
    from trid3nt_server.emission.mesh_display import write_2dm_arrays

    mesh = session.mesh
    points = np.array(mesh.points, dtype=float, copy=True)
    cells = np.asarray(mesh.cells, dtype=np.int64)
    counts: dict[tuple[int, int], int] = {}
    for tri in cells:
        for k in range(3):
            a, b = int(tri[k]), int(tri[(k + 1) % 3])
            key = (min(a, b), max(a, b))
            counts[key] = counts.get(key, 0) + 1
    boundary = {n for (a, b), c in counts.items() if c == 1 for n in (a, b)}
    inner = [i for i, tri in enumerate(cells)
             if not any(int(n) in boundary for n in tri)]
    if not inner:
        raise SystemExit("the built corridor has no fully interior triangle")
    target = inner[len(inner) // 2]
    a, b, c = (int(n) for n in cells[target])
    new_node = points.shape[0]
    points = np.vstack([points, points[[a, b, c]].mean(axis=0)])
    cells = np.vstack([np.delete(cells, target, axis=0),
                       [[a, b, new_node], [b, c, new_node], [c, a, new_node]]])
    z = (np.asarray(mesh.bed, dtype=float) if mesh.has_bed
         else np.zeros(points.shape[0]))
    if z.shape[0] != points.shape[0]:
        z = np.append(z, z[[a, b, c]].mean())
    path = workdir / "hand_edited.2dm"
    path.write_text(write_2dm_arrays(points, cells, z))
    log.info("hand edit: triangle %d split at its centroid -> node %d",
             target, new_node)
    return path


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--edge-m", type=float, default=45.0)
    ap.add_argument("--extent-km", type=float, default=3.0)
    ap.add_argument("--duration-s", type=float, default=900.0)
    ap.add_argument("--out", default="/tmp/corridor_hand_edit_proof.json")
    args = ap.parse_args()

    from trid3nt_server.workflows.mesh.artifact import mesh_compatible_with_engine
    from trid3nt_server.workflows.mesh.session import MeshSession
    from trid3nt_server.workflows.mesh.tool import tool
    from trid3nt_server.workflows.telemac.steps.deck import write_reach_deck
    from trid3nt_server.workflows.telemac.steps.solve import read_run_metrics, solve_reach

    reach, seed = await _reach_and_seed()
    log.info("reach=%s seed=(%.5f,%.5f)", reach.get("slug"), seed["lon"], seed["lat"])

    declaration = tool.build_mesh(
        mesher="corridor_tin", kind="unstructured_tri",
        domain={"reach": dict(reach), "seed": dict(seed)},
        extent_km=args.extent_km, width_m=60.0, banks="nhd_area",
        refine={"edge_length": args.edge_m})
    session = await asyncio.to_thread(MeshSession, declaration,
                                      name="hand-edit proof corridor")
    built = await asyncio.to_thread(lambda: session.probes())
    log.info("built: %d nodes / %d elements", built["node_count"],
             built["element_count"])
    built_files = dict(session.mesh.meta["files"])

    layer = _hand_edit(session, session.workdir)
    edited = await asyncio.to_thread(session.edit, "apply_layer_edits", str(layer))
    art = await asyncio.to_thread(session.accept)
    ok, reason = mesh_compatible_with_engine(art, "telemac")
    log.info("accepted %s: %d nodes / %d elements, telemac_compatible=%s (%s)",
             art.mesh_id, art.node_count, art.element_count, ok, reason)
    if not art.topology_uri:
        raise SystemExit("the accepted mesh carries no topology to stage")

    deck = await write_reach_deck(
        reach=reach, seed=seed,
        mesh={"artifact": art, "mesh_id": art.mesh_id, "slf_uri": art.slf_uri,
              "cli_uri": art.cli_uri, "topology_uri": art.topology_uri},
        carrier_discharge={"m3s": 60.0, "basis": "user",
                           "note": "pinned for a reference run"},
        substance="sewage", do_sag_config=DO_SAG,
        reach_length_km=args.extent_km, channel_width_m=60.0,
        sim_duration_s=args.duration_s, mesh_resolution_m=args.edge_m)
    log.info("deck staged mesh rows: %s",
             [row for row in deck["inputs"] if row["dest"] == "river_mesh.npz"])

    solved = await solve_reach(deck=deck, compute_class="small")
    metrics = await asyncio.to_thread(read_run_metrics, solved["run_id"])

    record = {
        "run_id": solved["run_id"],
        "status": solved.get("status"),
        "mesh_id": art.mesh_id,
        "built_nodes": built["node_count"],
        "built_elements": built["element_count"],
        "accepted_nodes": art.node_count,
        "accepted_elements": art.element_count,
        "boundary_role_carry_m": edited.get("boundary_role_carry_m"),
        "telemac_compatible": [ok, reason],
        "topology_uri": art.topology_uri,
        "built_topology_uri": built_files.get("topology_uri"),
        "metrics_npoin": metrics.get("npoin"),
        "metrics_nelem": metrics.get("nelem"),
        "metrics_mesh_origin": metrics.get("mesh_origin"),
        "metrics_wall_s": metrics.get("wall_s"),
        "metrics_correct_end": metrics.get("correct_end"),
        # The edit added a node and two elements, so a rebuild of the same
        # corridor could not report these counts: the solve ran on the accepted
        # geometry rather than on an equivalent of it.
        "SOLVED_ON_ACCEPTED_MESH": (
            int(metrics.get("npoin") or 0) == int(art.node_count)
            == int(built["node_count"]) + 1
            and int(metrics.get("nelem") or 0) == int(art.element_count)
            == int(built["element_count"]) + 2
            and art.topology_uri != built_files.get("topology_uri")),
    }
    print("PROOF " + json.dumps(record, default=str))
    Path(args.out).write_text(json.dumps(record, indent=2, default=str))
    return 0 if record["SOLVED_ON_ACCEPTED_MESH"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
