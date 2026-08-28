#!/usr/bin/env python
"""THE FLAGSHIP: an authored OceanMesh2D domain, fed into ARTEMIS, proven.

One end-to-end run that exercises the whole mesh front against a real harbour:

  1. AUTHOR - ``tool.build_mesh`` on the ``om2d`` mesher over the Point Judith
     Harbor of Refuge, adaptively sized (fine at the shore and the structure,
     coarse offshore over the shelf), then two DECLARED edits: the surveyed
     breakwater punched out as a conformal obstacle, and the seaward side
     designated open. The recipe - spec plus that ordered chain - is the record.
  2. FEED - the accepted mesh goes into ``artemis_harbor_agitation`` EXPLICITLY,
     through the template's own ``mesh`` slot. Nothing is discovered.
  3. SOLVE + PROVE - the live run writes its evidence to the canonical proof
     path and ``scripts/assemble_proof_packet.py`` assembles the delivery packet
     from it.
  4. COMPARE - the same question, same forcing, on the worker's own uniform grid,
     so the adaptive answer is read against the one it replaces rather than
     against nothing. The comparison is a numbers file beside the packet.

WHY POINT JUDITH AND NOT MARQUETTE. The rematch was ruled on the Marquette
harbour, and the ``om2d`` mesher cannot cut a domain there: it takes its water
from the GSHHG L1 land polygons, which describe the boundary between land and
OCEAN. Lake Superior is not in them, so the whole Marquette AOI reads as land and
the sizing function has no shoreline to measure from. Point Judith is the same
question - a real surveyed breakwater sheltering a real harbour over real
surveyed bathymetry - on water GSHHG actually describes.

Env (MinIO + the agent daemon): set -a; source .env.local; set +a; make agent
Usage:
  proof_artemis_om2d_rematch.py                 # author a mesh, run, prove
  proof_artemis_om2d_rematch.py --mesh s3://... # reuse an authored mesh
  proof_artemis_om2d_rematch.py --mesh-only     # author and stop
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from trid3nt_server.testing.live_run import GateAnswers, LiveRun, run_live  # noqa: E402
from trid3nt_server.testing.proof_paths import evidence_path, proof_dir  # noqa: E402

__all__ = ["author_mesh", "barrier_footprint", "main", "render_mesh_figures",
           "run_leg"]

#: The canary this driver writes: ``artemis_om2d_rematch`` at the ``refined``
#: variant, which is what an adaptive mesh against a uniform grid IS.
PROOF_NAME = "artemis_om2d_rematch"
PROOF_VARIANT = "refined"

#: Point Judith Harbor of Refuge, RI - three breakwaters mapped in OpenStreetMap
#: sheltering a real harbour, CUDEM 1/9" bathymetry underneath, and open Atlantic
#: shelf to the south for the swell to arrive over.
AOI = (-71.525, 41.338, -71.492, 41.368)

#: The mesh ask. The finest edge sits at the shore and around the structure, the
#: coarsest offshore; the gradation limits how fast one becomes the other.
REFINE = {"edge_length": 25.0, "min_spacing": 8.0, "gradation": 0.2}

#: What the bed is sampled from. CUDEM's 1/9 arc-second nearshore collection
#: covers this harbour, which is the resolution a 130 m wave needs.
BED = "fetch_topobathy"

#: The BARRIER WIDTH the mapped centerline is given, in metres. OpenStreetMap
#: maps a rubble-mound breakwater as a line and a line has no area to remove from
#: the water, so the structure is meshed at a declared width - a labeled modelling
#: choice, in the range a Harbor-of-Refuge mound occupies at the waterline.
BARRIER_WIDTH_M = 20.0

#: Which boundary opens, and how deep a node must be for the mesher's library to
#: read it as ocean. The side is NAMED rather than left to the seaward pick: this
#: AOI's boundary reaches -18 m on the south and the west shelf both, so a pick by
#: depth alone can land on the wrong one.
OPEN_SIDE = "south"
OPEN_DEPTH_THRESHOLD_M = -12.0

#: The incident sea state, PRESCRIBED. 90 deg is the trig convention's +Y, so the
#: swell propagates north - in through the designated south boundary and at the
#: breakwaters.
FORCING: dict[str, Any] = {
    "wave_mode": "diffraction",
    "wave_period_s": 12.0,
    "wave_height_m": 2.0,
    "wave_direction_deg": 90.0,
    "reflection_coef": 0.5,
    "bathy_source": "noaa_greatlakes",
    "compute_class": "medium",
}

#: The grid the COMPARISON leg is laid at. The template's own labeled default for
#: a real harbour: the worker's ring walk cannot discretize this AOI's islands any
#: finer, so a comparison at the authored mesh's median edge has no run to make.
COMPARISON_RESOLUTION_M = 40.0


def barrier_footprint(bbox: tuple[float, ...], out_dir: Path) -> tuple[str, str]:
    """The surveyed breakwater as a WATER-REMOVING footprint -> (geojson, layer uri).

    The fetched structure is a set of centerlines. A centerline bounds no area, so
    subtracting it from the water domain removes nothing and the triangulation
    closes straight over it - conformal nodes on a barrier that is not there. The
    footprint is that centerline given :data:`BARRIER_WIDTH_M`, which is what the
    mesher can actually punch out.
    """
    import geopandas as gpd

    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.tools.cache import read_object_bytes_s3

    layer = TOOL_REGISTRY["fetch_osm_breakwaters"].fn(bbox=list(bbox))
    local = out_dir / "breakwaters.fgb"
    local.write_bytes(read_object_bytes_s3(layer.uri)
                      if str(layer.uri).startswith("s3://")
                      else Path(layer.uri).read_bytes())
    lines = gpd.read_file(local).to_crs(4326)
    metric = lines.estimate_utm_crs()
    footprint = lines.to_crs(metric).buffer(BARRIER_WIDTH_M / 2.0).union_all()
    out = out_dir / "breakwater_footprint.geojson"
    gpd.GeoSeries([footprint], crs=metric).to_crs(4326).to_file(
        out, driver="GeoJSON")
    return str(out), str(layer.uri)


def author_mesh(work_dir: Path) -> tuple[Any, str, str]:
    """Author the om2d domain through the declared chain.

    Returns the accepted artifact, the structure layer the run is handed, and the
    footprint the cut was constrained to - the last so a proof render can draw the
    mesh against the geometry it was supposed to follow.
    """
    from trid3nt_server.workflows.mesh.session import MeshSession
    from trid3nt_server.workflows.mesh.tool import tool

    footprint, structure_uri = barrier_footprint(AOI, work_dir)
    declaration = (
        tool.build_mesh(mesher="om2d", kind="unstructured_tri", aoi=AOI,
                        refine=REFINE, bed=BED)
        .edit("add_obstacle", footprint)
        .edit("set_boundary", side=OPEN_SIDE, type="open",
              depth_threshold=OPEN_DEPTH_THRESHOLD_M))
    session = MeshSession(declaration, case_id=None,
                          name="Point Judith Harbor of Refuge")
    artifact = session.accept()
    print(json.dumps({
        "mesh": artifact.name, "mesher": artifact.mode,
        "display_uri": artifact.display_uri, "slf_uri": artifact.slf_uri,
        "cli_uri": artifact.cli_uri, "recipe_uri": artifact.recipe_uri,
        "nodes": artifact.node_count, "elements": artifact.element_count,
        "crs": artifact.crs_authid, "probes": artifact.probes,
        "open_boundary_info": artifact.open_boundary_info,
        "engine_compat": artifact.engine_compat,
        "barrier_width_m": BARRIER_WIDTH_M,
    }, indent=2, default=str))
    return artifact, structure_uri, footprint


def render_mesh_figures(artifact: Any, footprint: str,
                        directory: Path) -> list[str]:
    """The MESH, as the two pictures a reader has to be able to check it against.

    The engine render carries the wireframe over the solved field, which shows
    that the solve ran on this mesh. Neither shows whether the cut LANDED on the
    surveyed structure, because at a whole-domain zoom a 20 m barrier is a line
    two pixels wide. So: the sized domain, and then a crop tight enough that the
    element edges and the mapped centreline are separable.
    """
    import geopandas as gpd
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.tri as mtri
    import numpy as np

    from trid3nt_server.tools.cache import read_object_bytes_s3
    from trid3nt_server.workflows.mesh.watershed import read_2dm_mesh

    local = directory / "authored_mesh.2dm"
    local.write_bytes(read_object_bytes_s3(artifact.display_uri))
    points, cells, bed = read_2dm_mesh(str(local))
    lines = gpd.read_file(footprint).to_crs(artifact.crs_authid)
    tri = mtri.Triangulation(points[:, 0], points[:, 1], cells)
    edge = np.linalg.norm(points[cells[:, 0]] - points[cells[:, 1]], axis=1)

    out: list[str] = []
    for name, window in (("mesh_wireframe", None),
                         ("mesh_cut_zoom", _cut_window(lines, points))):
        figure, axes = plt.subplots(figsize=(11, 11))
        face = axes.tripcolor(tri, facecolors=edge, cmap="magma_r",
                              edgecolors="none", alpha=0.85)
        axes.triplot(tri, color="#102030",
                     lw=0.12 if window is None else 0.6, alpha=0.85)
        lines.boundary.plot(ax=axes, color="#00d0ff", lw=1.6)
        figure.colorbar(face, ax=axes, fraction=0.03,
                        label="element edge length (m)")
        if window is not None:
            axes.set_xlim(window[0], window[2])
            axes.set_ylim(window[1], window[3])
        axes.set_aspect("equal")
        axes.set_title(
            f"{artifact.name} - {artifact.node_count} nodes / "
            f"{artifact.element_count} elements, {artifact.crs_authid}\n"
            f"cyan = the surveyed breakwater footprint the cut was constrained to; "
            f"measured max node offset "
            f"{artifact.probes['breakline_offset_m']['max']:.1f} m, median "
            f"{artifact.probes['breakline_offset_m']['median']:.1f} m",
            fontsize=9)
        path = directory / f"{PROOF_NAME}_{PROOF_VARIANT}_{name}.png"
        figure.savefig(path, dpi=130, bbox_inches="tight")
        plt.close(figure)
        out.append(str(path))
    return out


def _cut_window(lines: Any, points: Any) -> tuple[float, float, float, float]:
    """A crop ON the structure, wide enough to hold the cut and no wider.

    Centred on the footprint itself rather than on its centroid: a breakwater bent
    into a V has its centroid in the middle of the harbour it shelters, and a crop
    there is a picture of open water.
    """
    import numpy as np
    from shapely.ops import nearest_points

    union = lines.union_all()
    on_structure = nearest_points(union, union.centroid)[0]
    span = 0.06 * float(np.ptp(points[:, 0]))
    return (on_structure.x - span, on_structure.y - span,
            on_structure.x + span, on_structure.y + span)


def run_leg(*, mesh_uri: str | None, structure_uri: str, title: str,
            timeout_s: float) -> Any:
    """One live agitation run - on the supplied mesh, or on the worker's grid."""
    args: dict[str, Any] = {"bbox": list(AOI), "structure": structure_uri,
                            "input_mode": "user_gated", **FORCING}
    if mesh_uri:
        args["mesh"] = mesh_uri
    else:
        args["target_resolution_m"] = COMPARISON_RESOLUTION_M
    return run_live(LiveRun(
        tool="artemis_harbor_agitation", args={**args, "restart_clean": True},
        case_title=title, answers=GateAnswers(confirm="proceed"),
        timeout_s=timeout_s, cleanup_case=True))


def _worker_metrics(run_id: str | None) -> dict[str, Any]:
    """What the SOLVE itself recorded - the mesh it read, the field it measured.

    The run's ``metrics.json`` is the published layer's scalars; the numbers that
    say which mesh was consumed live in the worker's own document beside it, and a
    comparison that cited only the first could not tell the two legs apart.
    """
    import boto3

    if not run_id:
        return {}
    client = boto3.client("s3", endpoint_url=os.environ["AWS_ENDPOINT_URL"],
                          region_name=os.environ.get("AWS_REGION", "us-east-1"))
    bucket = os.environ.get("TRID3NT_RUNS_BUCKET", "trid3nt-runs")
    try:
        return json.loads(client.get_object(
            Bucket=bucket, Key=f"{run_id}/telemac_metrics.json")["Body"].read())
    except Exception as exc:  # noqa: BLE001 - an unread document says so
        return {"telemac_metrics_unread": f"{type(exc).__name__}: {exc}"}


def _answer(evidence: Any) -> dict[str, Any]:
    metrics = {**(evidence.metrics or {}), **_worker_metrics(evidence.run_id)}
    answer = {k: metrics.get(k) for k in (
        "kd_max", "hs_max_m", "kd_sheltered", "kd_exposed", "sheltering_ratio",
        "npoin", "nelem", "dx_m", "mesh_source", "mesh_edge_min_m",
        "mesh_edge_median_m", "mesh_edge_max_m", "mesh_open_boundary_nodes",
        "mesh_boundary_nodes", "mesh_structure_face_nodes", "bed_clamped_nodes",
        "wavelength_m", "wall_s")}
    answer["status"] = metrics.get("status") or evidence.step_state
    answer["error"] = metrics.get("error") or evidence.step_error
    return answer


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mesh", default=None,
                    help="reuse an authored mesh (its display .2dm uri) instead "
                         "of authoring a fresh one")
    ap.add_argument("--mesh-only", action="store_true", default=False)
    ap.add_argument("--no-comparison", dest="comparison", action="store_false",
                    default=True, help="skip the uniform-grid leg")
    ap.add_argument("--timeout", type=float, default=2400.0)
    ns = ap.parse_args(argv)

    directory = Path(proof_dir(PROOF_NAME, PROOF_VARIANT))
    work = Path(os.environ.get("TRID3NT_RUNS_DIR", "/tmp")) / "artemis-om2d-rematch"
    work.mkdir(parents=True, exist_ok=True)

    if ns.mesh:
        from trid3nt_server.workflows.mesh.artifact import read_mesh_artifact_sidecar
        from trid3nt_server.workflows.solver.solver import _get_s3_client

        artifact = read_mesh_artifact_sidecar(ns.mesh, _get_s3_client())
        if artifact is None:
            print(f"NO MESH: {ns.mesh} carries no mesh_artifact.json sidecar",
                  file=sys.stderr)
            return 2
        footprint, structure_uri = barrier_footprint(AOI, work)
    else:
        artifact, structure_uri, footprint = author_mesh(work)
    if ns.mesh_only:
        return 0

    evidence = run_leg(mesh_uri=artifact.display_uri, structure_uri=structure_uri,
                       title="flagship: artemis on an authored om2d mesh "
                             "(Point Judith Harbor of Refuge)",
                       timeout_s=ns.timeout)
    out = Path(evidence_path(f"{PROOF_NAME}_{PROOF_VARIANT}"))
    out.write_text(json.dumps(evidence.as_dict(), indent=2, default=str),
                   encoding="utf-8")
    print(json.dumps({"evidence": str(out), "run_id": evidence.run_id,
                      "answer": _answer(evidence)}, indent=2, default=str))
    try:
        evidence.require_ok().require_run_products()
    except Exception as exc:  # noqa: BLE001 - the reason IS the report
        print(f"FLAGSHIP FAILED: {exc}", file=sys.stderr)
        return 1

    if ns.comparison:
        uniform = run_leg(mesh_uri=None, structure_uri=structure_uri,
                          title="comparison: artemis on the worker's uniform grid "
                                "(Point Judith Harbor of Refuge)",
                          timeout_s=ns.timeout)
        (directory / "uniform_grid_comparison.json").write_text(json.dumps({
            "what": "the SAME question and the same prescribed forcing, solved on "
                    "the worker's own uniform grid, so the authored mesh's answer "
                    "is read against the one it replaces",
            "aoi": list(AOI), "forcing": FORCING,
            "authored_mesh": {"run_id": evidence.run_id, "mesh": artifact.name,
                              **_answer(evidence)},
            "uniform_grid": {"run_id": uniform.run_id,
                             "target_resolution_m": COMPARISON_RESOLUTION_M,
                             **_answer(uniform)},
        }, indent=2, default=str) + "\n", encoding="utf-8")
        print(json.dumps({"comparison": _answer(uniform)}, indent=2, default=str))

    from trid3nt_server.testing.canaries import assemble_packet

    packet = assemble_packet(f"{PROOF_NAME}_{PROOF_VARIANT}")
    figures = render_mesh_figures(artifact, footprint, directory)
    print(json.dumps({"packet": packet["verdict"], "missing": packet["missing"],
                      "directory": packet["directory"],
                      "mesh_figures": figures}, indent=2, default=str))
    return 0 if packet["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
