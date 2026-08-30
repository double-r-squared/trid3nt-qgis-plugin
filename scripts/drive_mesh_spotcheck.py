#!/usr/bin/env python
"""Live driver: the standing mesh spot-check lane.

Invoke this after ANY mesh-functionality landing. It calls the registered
``build_mesh`` tool DIRECTLY (``TOOL_REGISTRY["build_mesh"].fn``, no daemon/
socket), builds ONE coarse mesh, and prints the numeric facts a change is
judged on: the probes measured on the accepted topology, the emitted MDAL
display layer, the MeshArtifact's engine-compat facts, and the journaled
recipe. NATE loads the printed display layer uri in QGIS as the visual pass.

The edge-length lever routes to whichever field the CHOSEN mesher actually
declares (``resolution_m`` | ``min_edge_length_m`` | ``refine``), read off the
mesher's own field registry at call time rather than assumed by mesher name.
A mesher with no edge-sizing field (``telapy_mesh``, which adopts an existing
geometry) gets no override.

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
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from _env_guard import require_local_endpoint  # noqa: E402

#: A small named place, not a reach descriptor: the AOI meshers (reg_grid,
#: om2d, watershed, coastal_edge) resolve ``location`` through the
#: raw ``geocode_location`` bbox, and a "<feature> near <place>" query answers
#: with the feature's FULL extent (a river name geocodes to the whole state) --
#: a real town geocodes tight, which is what a coarse spot check needs.
DEFAULT_LOCATION = "Scotia, California"
#: Coarse cell/triangle edge (m): fast to build, not a resolution study.
DEFAULT_EDGE_LENGTH_M = 250.0


def _edge_lever(mesher: str, edge_length_m: float) -> dict:
    """The edge-length override, routed to whichever field THIS mesher declares.

    Read off the mesher's OWN field registry rather than guessed by name.
    """
    from trid3nt_server.workflows.mesh.meshers import get_mesher

    declared = get_mesher(mesher).fields
    if "resolution_m" in declared:
        return {"resolution_m": edge_length_m}
    if "min_edge_length_m" in declared:
        return {"min_edge_length_m": edge_length_m}
    if "refine" in declared:
        return {"refine": {"min_spacing": edge_length_m,
                           "edge_length": edge_length_m * 4.0}}
    return {}


def _parse_field(raw: str) -> tuple[str, object]:
    """``NAME=VALUE`` -> ``(NAME, value)``, VALUE JSON-parsed when possible."""
    name, sep, value = raw.partition("=")
    if not sep:
        raise argparse.ArgumentTypeError(f"--field wants NAME=VALUE; got {raw!r}")
    try:
        return name, json.loads(value)
    except json.JSONDecodeError:
        return name, value


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
        call.update(_edge_lever(ns.mesher, ns.edge_length_m))
        for name, value in ns.field or []:
            call[name] = value

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
                    help="coarse cell/triangle edge (m); routed to whichever "
                         "field the chosen mesher declares (see --field to "
                         "override the levers this does not reach, e.g. a "
                         "mesher's own max edge or gradation)")
    ap.add_argument("--field", action="append", type=_parse_field, default=[],
                    metavar="NAME=VALUE",
                    help="extra mesher-declared field override (repeatable); "
                         "VALUE is JSON-parsed when possible, else kept as a "
                         "string. Applied AFTER --edge-length-m, so it wins.")
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
