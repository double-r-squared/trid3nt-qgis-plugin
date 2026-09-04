#!/usr/bin/env python
"""LIVE proof: a processing raster reaches the map with NO publish tool involved.

The claim under test is the one NATE's ruling makes - emission is AUTOMATIC.
A tool that computes a raster returns a ``LayerURI`` and that is the whole of
its obligation; the layer is published (COG overviews, resolved style params,
data-driven legend) by the ONE emission seam on the way out, with no
``publish_layer`` call in the transcript because there is no such tool to call.

What this drives, and why it is the honest shape:

  * ``compute_hillshade`` is called through ``TOOL_REGISTRY[...].fn`` - the
    tool's real body over real 3DEP terrain, no stub - and wrapped in
    ``PipelineEmitter.emit_tool_call``, which is the seam the WS server uses.
    Driving the seam rather than the server is what isolates the claim: no LLM
    chooses anything here, so a published layer can only have come from the
    seam.
  * The publish MECHANISM is counted, not mocked. It must run exactly once,
    unasked. Zero would mean the layer reached the map unstyled; more than one
    would mean a second call site survived the collapse.
  * The registry is asserted to have no ``publish_layer`` entry at all, which
    is what "zero publish_layer invocation" means once the tool is deleted:
    not that the model declined to call it, but that it cannot.

Env (MinIO): set -a; source .env.local; set +a
Usage: proof_auto_emit_seam.py [--bbox min_lon,min_lat,max_lon,max_lat]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trid3nt_contracts import new_ulid  # noqa: E402
from trid3nt_contracts.execution import LayerURI  # noqa: E402

from trid3nt_server.emission.pipeline_emitter import PipelineEmitter  # noqa: E402
from trid3nt_server.tools import TOOL_REGISTRY  # noqa: E402

#: A small AOI over real 3DEP terrain - the Eel River reach the TELEMAC cohort
#: canaries already use, so the DEM tiles are the ones this box knows are real.
DEFAULT_BBOX = (-124.11, 40.485, -124.09, 40.500)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bbox", default=",".join(str(v) for v in DEFAULT_BBOX))
    ns = ap.parse_args()
    bbox = [float(v) for v in ns.bbox.split(",")]

    assert "publish_layer" not in TOOL_REGISTRY, (
        "publish_layer is STILL a registered tool - the deletion did not land"
    )

    # Count the mechanism. Nothing is stubbed: the real publish runs, we only
    # observe that the seam is what called it.
    calls: list[dict] = []
    from trid3nt_server.emission import publish as publish_mod

    original = publish_mod.publish_layer

    def counted(**kwargs):
        calls.append({k: v for k, v in kwargs.items() if k != "name"})
        return original(**kwargs)

    publish_mod.publish_layer = counted  # type: ignore[assignment]

    frames: list[dict] = []

    async def sink(text: str) -> None:
        frames.append(json.loads(text))

    emitter = PipelineEmitter(session_id=new_ulid(), sink=sink)

    try:
        # The DEM first - an INTERMEDIATE, and the one that used to carry
        # ``auto_publish: false`` so it would NOT reach the map. Under the
        # ruling it does: the user hides what they do not want.
        dem = await emitter.emit_tool_call(
            name="fetch_dem",
            tool_name="fetch_dem",
            invoke=lambda: TOOL_REGISTRY["fetch_dem"].fn(bbox=bbox, resolution_m=30),
        )
        dem_uri = dem.get("uri") if isinstance(dem, dict) else getattr(dem, "uri", None)
        assert dem_uri, f"fetch_dem returned no uri: {dem!r}"
        n_after_dem = len(calls)
        result = await emitter.emit_tool_call(
            name="compute_hillshade",
            tool_name="compute_hillshade",
            invoke=lambda: TOOL_REGISTRY["compute_hillshade"].fn(dem_uri=dem_uri),
        )
    finally:
        publish_mod.publish_layer = original  # type: ignore[assignment]

    assert isinstance(result, LayerURI), f"expected a LayerURI, got {type(result)}"

    session_states = [f for f in frames if f.get("type") == "session-state"]
    loaded = []
    for f in session_states:
        loaded = (f.get("payload") or {}).get("loaded_layers") or loaded

    report = {
        "tools": ["fetch_dem", "compute_hillshade"],
        "dem_uri": dem_uri,
        "publish_calls_after_fetch_dem": n_after_dem,
        "bbox": bbox,
        "publish_layer_in_registry": "publish_layer" in TOOL_REGISTRY,
        "registry_size": len(TOOL_REGISTRY),
        "publish_mechanism_calls": len(calls),
        "publish_mechanism_args": calls,
        "tool_returned_uri": result.uri,
        "session_state_frames": len(session_states),
        "loaded_layers": [
            {
                "layer_id": layer.get("layer_id"),
                "name": layer.get("name"),
                "uri": layer.get("uri"),
                "has_legend": bool(layer.get("legend")),
            }
            for layer in loaded
        ],
    }
    print(json.dumps(report, indent=2, default=str))

    assert len(calls) == 2, (
        f"the publish mechanism ran {len(calls)} times; the seam must call it "
        "exactly once per raster, unasked - once for the DEM intermediate and "
        "once for the hillshade"
    )
    assert n_after_dem == 1, (
        "the DEM intermediate did NOT publish; the auto_publish opt-out is "
        "still in force somewhere"
    )
    assert len(loaded) == 2, (
        f"expected the DEM and the hillshade on the map, got {loaded!r}"
    )
    published = loaded[-1]
    assert str(published.get("uri", "")).startswith("s3://"), published
    assert published.get("legend"), (
        "the layer reached the map with no legend - the publish enrichment did "
        "not land"
    )
    print("\nOK: the raster is on the map, published by the seam, "
          "with no publish_layer tool in the registry to have called.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
