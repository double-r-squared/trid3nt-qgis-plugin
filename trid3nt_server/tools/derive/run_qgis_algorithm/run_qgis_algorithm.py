"""``run_qgis_algorithm`` - one QGIS Processing algorithm run in the user's session.

The body is a request on the plugin wire: the session runs ``processing.run``
over layers named on the canvas, adds the output to the project and answers
with the layer summary. Nothing runs on the daemon and nothing is fetched.
"""

from __future__ import annotations

from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.server.processing import SessionProcessingFailedError, run_in_session
from trid3nt_server.tools import register_tool

__all__ = ["run_qgis_algorithm"]

_METADATA = AtomicToolMetadata(
    name="run_qgis_algorithm",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)


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
) -> dict[str, Any]:
    """Run a QGIS Processing algorithm in the user's QGIS session over canvas layers.

    ROUTING: any standard geoprocessing on a layer ALREADY on the map - slope,
    aspect, hillshade, contours (native:slope, native:aspect, native:hillshade,
    gdal:contour), clip or mask (gdal:cliprasterbymasklayer, native:clip), zonal
    statistics (native:zonalstatisticsfb), raster calculator (native:rastercalc),
    buffer, dissolve, reproject, resample, polygonize, NDVI or any band math.
    NOT for fetching data: call the fetch_* tool first, then run over the fetched
    layer. NOT for a simulation.

    `algorithm` is the Processing id `provider:name` as the QGIS Processing
    algorithm reference lists it; `params` are that algorithm's own parameters,
    a layer given by its canvas layer NAME. Leave OUTPUT unset: the output is
    written to a temporary file, added to the project and summarized back.

    Returns the output layer summary (layer_name, kind, crs, extent, band or
    feature count) or the algorithm's error verbatim.
    """
    if not isinstance(algorithm, str) or not algorithm.strip():
        raise SessionProcessingFailedError(
            f"algorithm must be a Processing id like 'native:slope'; got {algorithm!r}"
        )
    if params is not None and not isinstance(params, dict):
        raise SessionProcessingFailedError(
            f"params must be a mapping of the algorithm's parameters; got {type(params).__name__}"
        )
    response = await run_in_session(
        kind="algorithm", algorithm=algorithm.strip(), params=params or {}
    )
    return {"status": "ok", "algorithm": algorithm.strip(), **(response.result or {})}
