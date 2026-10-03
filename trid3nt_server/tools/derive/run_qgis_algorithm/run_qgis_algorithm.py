"""``run_qgis_algorithm`` - one QGIS Processing algorithm run in the user's session.

The body is a request on the plugin wire: the session runs ``processing.run``
over layers named by id or canvas name and writes each output to a file. Each
file becomes a case layer the way a fetch output does - in the store, returned
as a ``LayerURI`` - so the next call names it by its layer id.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from trid3nt_contracts.execution import LayerURI, layer_seed
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.server.processing import SessionProcessingFailedError, run_in_session
from trid3nt_server.tools import register_tool

__all__ = ["QgisLayerURI", "run_qgis_algorithm"]

_METADATA = AtomicToolMetadata(
    name="run_qgis_algorithm",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
)

#: What an output says about its values, carried from the layer it was computed
#: from: the zero it is counted from, the shift that zero publishes, and the
#: quantity. A key the input layers state differently is carried by none.
_INHERITED = ("vertical_datum", "datum_offset_m", "datum_offset_frame", "quantity")


class QgisLayerURI(LayerURI):
    """One algorithm output as a case layer, with the CRS and extent QGIS read."""

    algorithm: str
    crs: str | None = None
    extent: list[float] = []
    outputs: dict[str, Any] = {}


def _inherited(params: dict[str, Any]) -> dict[str, Any]:
    """The statements every case layer named in ``params`` agrees on."""
    from trid3nt_server.render.uri_registry import lookup_layer_for_handle

    inputs = [layer for value in params.values() if isinstance(value, str)
              and (layer := lookup_layer_for_handle(value)) is not None]
    out = {}
    for key in _INHERITED:
        stated = {layer[key] for layer in inputs if layer.get(key) is not None}
        if len(stated) == 1:
            out[key] = stated.pop()
    return out


def _case_layer(algorithm: str, output: dict[str, Any], **fields: Any) -> QgisLayerURI:
    """The file one output was written to, put in the runs bucket -> its layer."""
    from trid3nt_server import storage

    # A GeoPackage source names its table after a '|'; the file is before it.
    path = str(output.get("source") or "").split("|", 1)[0]
    if not os.path.isfile(path):
        raise SessionProcessingFailedError(
            f"{algorithm} wrote its output to {output.get('source')!r}, which is "
            "no file this server can read, so it cannot become a case layer")
    seed = layer_seed()
    bucket = storage.runs_bucket()
    key = f"qgis-{seed}/{os.path.basename(path)}"
    with open(path, "rb") as written:
        storage.client().put_object(Bucket=bucket, Key=key, Body=written.read())
    return QgisLayerURI.published(
        "qgis", seed=seed, name=output.get("layer_name") or algorithm,
        layer_type=output.get("kind") or "raster", uri=f"s3://{bucket}/{key}",
        algorithm=algorithm, crs=output.get("crs"),
        extent=output.get("extent") or [], **fields)


@register_tool(
    _METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def run_qgis_algorithm(
    algorithm: str,
    params: dict[str, Any] | None = None,
    # absorb model-invented kwargs.
    **_extra_ignored: Any,
) -> Any:
    """Run a QGIS Processing algorithm in the user's QGIS session over case layers.

    ROUTING: any standard geoprocessing on a layer ALREADY on the map - slope,
    aspect, hillshade, contours (native:slope, native:aspect, native:hillshade,
    gdal:contour), clip or mask (gdal:cliprasterbymasklayer, native:clip), zonal
    statistics (native:zonalstatisticsfb), raster calculator (native:rastercalc),
    buffer, dissolve, reproject, resample, polygonize, NDVI or any band math.
    NOT for fetching data: call the fetch_* tool first, then run over the fetched
    layer. NOT for a simulation.

    `algorithm` is the Processing id `provider:name` as the QGIS Processing
    algorithm reference lists it; `params` are that algorithm's own parameters,
    a layer given by its layer id or its canvas layer NAME. Leave OUTPUT unset.

    Returns each output as a NEW CASE LAYER (layer_id, name, uri, crs, extent,
    and the vertical datum, datum shift and quantity the input layer carried):
    name that layer_id in the next call. An algorithm that writes no layer
    returns its values; a failure returns the algorithm's error verbatim.
    """
    if not isinstance(algorithm, str) or not algorithm.strip():
        raise SessionProcessingFailedError(
            f"algorithm must be a Processing id like 'native:slope'; got {algorithm!r}"
        )
    if params is not None and not isinstance(params, dict):
        raise SessionProcessingFailedError(
            f"params must be a mapping of the algorithm's parameters; got {type(params).__name__}"
        )
    algorithm = algorithm.strip()
    response = await run_in_session(kind="algorithm", algorithm=algorithm, params=params or {})
    result = dict(response.result or {})
    outputs = [layer for layer in result.pop("layers", None) or [result]
               if layer.get("source")]
    if not outputs:
        return {"status": "ok", "algorithm": algorithm, **result}
    inherited = _inherited(params or {})
    layers = [await asyncio.to_thread(_case_layer, algorithm, output,
                                      outputs=result.get("outputs") or {}, **inherited)
              for output in outputs]
    return layers[0] if len(layers) == 1 else layers
