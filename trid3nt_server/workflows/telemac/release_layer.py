"""The reach's RELEASE point as a context layer.

The discharge/outfall location is physics - it is where the source term enters
the water and where the downstream distance is measured from - but it lived only
as two numbers on a manifest. This puts it on the map beside the reach, labeled
with whether the user placed it or the pipeline derived it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from trid3nt_contracts import new_ulid
from trid3nt_contracts.execution import LayerURI

logger = logging.getLogger("trid3nt_server.workflows.telemac.release_layer")

__all__ = ["publish_release_point"]

#: Half-width of the point's declared bbox, in degrees. A zero-extent bbox reads
#: as "no extent" to the camera, so the point gets a small honest box instead.
_BBOX_PAD_DEG = 0.002

#: This layer's style token. Vector presets are free descriptive strings (the QML
#: style registry governs rasters), so it names what the point IS.
RELEASE_POINT_STYLE_PRESET = "release_point"


async def publish_release_point(emitter: Any, *, lon: float, lat: float,
                                user_supplied: bool, reach_name: str,
                                label: str = "Outfall") -> bool:
    """Put the release point on the canvas. Best-effort: never fails a run.

    ``user_supplied`` is what the name says out loud - a drawn or passed point is
    the user's claim about the world, a derived one is the pipeline's, and the two
    must not read the same on the map.
    """
    if emitter is None:
        return False
    basis = "user" if user_supplied else "derived"
    try:
        uri = await asyncio.to_thread(_upload_point, lon, lat, basis, reach_name)
        if uri is None:
            return False
        from trid3nt_server.emission.layer_uri_emit import publish_input_layer

        layer = LayerURI(
            layer_id=f"telemac-release-{new_ulid()}",
            name=f"{label} ({basis}) - {reach_name}",
            layer_type="vector",
            uri=uri,
            style_preset=RELEASE_POINT_STYLE_PRESET,
            role="context",
            bbox=(lon - _BBOX_PAD_DEG, lat - _BBOX_PAD_DEG,
                  lon + _BBOX_PAD_DEG, lat + _BBOX_PAD_DEG),
        )
        emitted = await publish_input_layer(emitter, layer, role="context")
        logger.info("release point layer emitted=%s basis=%s at (%.5f, %.5f)",
                    emitted, basis, lon, lat)
        return bool(emitted)
    except Exception as exc:  # noqa: BLE001 - a context layer never voids a solve
        logger.warning("release point layer skipped: %s", exc)
        return False


def _upload_point(lon: float, lat: float, basis: str, reach_name: str) -> str | None:
    import boto3

    from trid3nt_server.data.simulation.solver.solver import _get_runs_bucket

    body = json.dumps({
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {"role": "point", "basis": basis, "reach": reach_name},
            "geometry": {"type": "Point",
                         "coordinates": [round(lon, 6), round(lat, 6)]},
        }],
    }).encode("utf-8")
    bucket = _get_runs_bucket()
    key = f"inputs/{new_ulid()}/release_point.geojson"
    boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-west-2")) \
        .put_object(Bucket=bucket, Key=key, Body=body,
                    ContentType="application/geo+json")
    return f"s3://{bucket}/{key}"
