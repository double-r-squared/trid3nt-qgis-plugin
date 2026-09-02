#!/usr/bin/env python
"""Live driver: the standing mesh spot-check lane.

Invoke this after ANY mesh-functionality landing. It calls the registered
``build_mesh`` tool DIRECTLY (``TOOL_REGISTRY["build_mesh"].fn``, no daemon/
socket), builds ONE coarse mesh, and prints the numeric facts a change is
judged on: the probes measured on the accepted topology, the emitted MDAL
display layer, the MeshArtifact's engine-compat facts, and the journaled
recipe. NATE loads the printed display layer uri in QGIS as the visual pass.

The edge-length lever is ``resolution_m``, the ONE agnostic size word every
mesher reads. Everything else about a build is an OP: pass ``--op`` to declare
the program, or omit it for the mesher's own hard-baked default list.

Env (MinIO): set -a; source .env.local; set +a
Usage:
    # coarse reg_grid (default: fastest mesher, no container) by place name
    venvs/agent/bin/python scripts/drive_mesh_spotcheck.py \\
        --location "Scotia, California" --edge-length-m 250

    # coarse om2d coastal mesh by bbox (Marquette Lower Harbor, MI -- a real
    # Great Lakes harbour with surveyed shoreline, no place-name geocode needed)
    venvs/agent/bin/python scripts/drive_mesh_spotcheck.py \\
        --mesher om2d --bbox -87.39234 46.52812 -87.36788 46.55021 \\
        --edge-length-m 300

    # the same coast, sized by distance to shore and held to a gradation: the
    # RECIPE, declared op by op under the library's own names
    venvs/agent/bin/python scripts/drive_mesh_spotcheck.py \\
        --mesher om2d --bbox -87.39234 46.52812 -87.36788 46.55021 \\
        --edge-length-m 300 \\
        --op feature_sizing_function \\
        --op 'enforce_mesh_gradation:{"gradation": 0.2}' \\
        --op delete_boundary_faces --op 'fix_mesh:{"delete_unused": true}' \\
        --op 'set_bed:{"source": "fetch_topobathy"}'
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from _env_guard import require_local_endpoint  # noqa: E402

#: A small named place, not a reach descriptor: both meshers resolve
#: ``location`` through the
#: raw ``geocode_location`` bbox, and a "<feature> near <place>" query answers
#: with the feature's FULL extent (a river name geocodes to the whole state) --
#: a real town geocodes tight, which is what a coarse spot check needs.
DEFAULT_LOCATION = "Scotia, California"
#: Coarse cell/triangle edge (m): fast to build, not a resolution study. It is
#: the ONE agnostic size word, so it reaches every mesher under the same name.
DEFAULT_EDGE_LENGTH_M = 250.0


def _parse_op(raw: str) -> dict:
    """``fn`` or ``fn:{"kwarg": value}`` -> one recipe entry off the command line."""
    name, sep, kwargs = raw.partition(":")
    if not sep:
        return {"fn": name}
    try:
        parsed = json.loads(kwargs)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(
            f"--op wants fn or fn:<json object>; {kwargs!r} is not JSON") from exc
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError(
            f"--op kwargs are a JSON object; got {parsed!r}")
    return {"fn": name, **parsed}


def _s3_client():
    import boto3

    return boto3.client("s3", endpoint_url=require_local_endpoint(),
                        region_name=os.environ.get("AWS_REGION", "us-east-1"))


async def _drive(ns: argparse.Namespace) -> dict:
    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.workflows.mesh.artifact import read_mesh_artifact_sidecar
    from trid3nt_server.workflows.mesh.meshers import MeshToolError

    try:
        call: dict = {"mesher": ns.mesher, "input_mode": "auto"}
        if ns.kind:
            call["kind"] = ns.kind
        if ns.bbox:
            call["bbox"] = tuple(ns.bbox)
        elif ns.location:
            call["location"] = ns.location
        call["resolution_m"] = ns.edge_length_m
        if ns.op:
            call["ops"] = list(ns.op)

        print(f"build_mesh({json.dumps(call, default=str)})")
        fn = TOOL_REGISTRY["build_mesh"].fn
        layer = await fn(**call)
    except MeshToolError as exc:
        return {"ok": False, "error_code": exc.error_code, "error_message": str(exc)}

    out: dict = {
        "ok": True,
        "display_layer_uri": layer.uri,
        "display_layer_type": layer.layer_type,
        "display_layer_crs": layer.crs_authid,
        "display_layer_bbox": layer.bbox,
    }
    art = read_mesh_artifact_sidecar(layer.uri, _s3_client())
    if art is None:
        out["mesh_artifact"] = None
        out["mesh_artifact_note"] = "no sidecar found beside the display layer"
        return out
    out["probes"] = art.probes
    out["mesh_artifact"] = {
        "mesh_id": art.mesh_id, "mode": art.mode,
        "crs_authid": art.crs_authid, "unsolvable_reason": art.unsolvable_reason(),
        "node_count": art.node_count, "element_count": art.element_count,
        "has_bathymetry": art.has_bathymetry,
        "display_uri": art.display_uri, "slf_uri": art.slf_uri,
        "cli_uri": art.cli_uri,
        "open_boundary_info": art.open_boundary_info,
    }
    out["recipe_uri"] = art.recipe_uri
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mesher", default="reg_grid",
                    help="reg_grid (default: fastest, no container) | om2d")
    ap.add_argument("--kind", default=None,
                    help="the mesh shape; unset takes the chosen mesher's own "
                         "declared default")
    ap.add_argument("--location", default=None,
                    help=f"place name (geocoded); defaults to "
                         f"{DEFAULT_LOCATION!r} when neither --location nor "
                         "--bbox is given")
    ap.add_argument("--bbox", type=float, nargs=4, default=None,
                    metavar=("MIN_LON", "MIN_LAT", "MAX_LON", "MAX_LAT"))
    ap.add_argument("--edge-length-m", type=float, default=DEFAULT_EDGE_LENGTH_M,
                    help="the ONE agnostic size word (m): the finest cell or "
                         "triangle edge every mesher reads under this name")
    ap.add_argument("--op", action="append", type=_parse_op, default=[],
                    metavar='FN[:{"kwarg": value}]',
                    help="one recipe op, by the function's own name "
                         "(repeatable, order meaningful). Declaring any of them "
                         "replaces the mesher's default list wholesale.")
    ns = ap.parse_args()
    if not ns.location and not ns.bbox:
        ns.location = DEFAULT_LOCATION

    out = asyncio.run(_drive(ns))
    print(json.dumps(out, indent=2, default=str))
    if not out.get("ok"):
        # Typed refusal, verbatim -- no massaging.
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
