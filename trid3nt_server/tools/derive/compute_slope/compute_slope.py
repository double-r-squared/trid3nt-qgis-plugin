"""Atomic tool ``compute_slope`` - terrain slope raster from a DEM.

Wraps ``gdaldem slope``. The output keeps the input DEM's CRS and grid, and the
cache key is (dem_uri, output_unit, algorithm) - all three move pixels.
"""
from __future__ import annotations

import logging
import os
import tempfile
from typing import Literal, Any

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.cache import CACHE_BUCKET, read_through
from trid3nt_server.emission.cog import translate_to_cog as _translate_to_cog
from trid3nt_server.tools.derive._gdal_runner import (
    read_raster_bytes,
    resolve_gdaldem,
    run_gdal,
)

__all__ = [
    "compute_slope",
    "SlopeComputeError",
]

logger = logging.getLogger("trid3nt_server.tools.derive.compute_slope.compute_slope")



class SlopeComputeError(RuntimeError):
    """``gdaldem slope`` failed or the DEM could not be read. ``error_code`` is one
    of GDALDEM_UNAVAILABLE, GDALDEM_FAILED, DEM_DOWNLOAD_FAILED.
    """

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code



_COMPUTE_SLOPE_METADATA = AtomicToolMetadata(
    name="compute_slope",
    ttl_class="static-30d",
    source_class="slope",
    cacheable=True,
)



def _get_gdaldem_bin() -> str:
    """Resolve ``gdaldem``; absent raises ``SlopeComputeError(GDALDEM_UNAVAILABLE)``."""
    binary = resolve_gdaldem()
    if binary is None:
        raise SlopeComputeError(
            "GDALDEM_UNAVAILABLE",
            "gdaldem binary not found on PATH; set TRID3NT_GDALDEM_BIN "
            "or install gdal-bin.",
        )
    return binary


def _download_dem_bytes(dem_uri: str, storage_client: object | None = None) -> bytes:
    """Read the DEM bytes from an ``s3://`` URI or a local path; ``storage_client``
    is ignored and a read failure raises ``SlopeComputeError``.
    """
    del storage_client
    return read_raster_bytes(
        dem_uri,
        on_error=lambda msg: SlopeComputeError("DEM_DOWNLOAD_FAILED", msg),
    )




def _run_gdaldem_slope(
    input_path: str,
    output_path: str,
    output_unit: Literal["degrees", "percent"],
    algorithm: Literal["Horn", "ZevenbergenThorne"],
) -> None:
    """Run ``gdaldem slope`` on local paths (``-p`` for percent, ``-alg`` for
    ZevenbergenThorne); a missing binary or non-zero exit raises ``SlopeComputeError``.
    """
    gdaldem = _get_gdaldem_bin()

    cmd: list[str] = [gdaldem, "slope", input_path, output_path]
    if output_unit == "percent":
        cmd.append("-p")
    if algorithm == "ZevenbergenThorne":
        cmd.extend(["-alg", "ZevenbergenThorne"])

    logger.info(
        "compute_slope: running gdaldem slope input=%s output_unit=%s algorithm=%s cmd=%s",
        input_path,
        output_unit,
        algorithm,
        " ".join(cmd),
    )

    run_gdal(
        cmd, gdaldem,
        on_unavailable=lambda msg: SlopeComputeError("GDALDEM_UNAVAILABLE", msg),
        on_failed=lambda msg: SlopeComputeError("GDALDEM_FAILED", msg),
    )

    logger.info(
        "compute_slope: gdaldem slope completed output=%s", output_path
    )




@register_tool(
    _COMPUTE_SLOPE_METADATA,
    # Annotations: readOnlyHint=True (reads input raster/vector; writes cache
    # artifact only via the read-through shim), openWorldHint=False (all
    # computation is local GDAL/numpy; no external API calls),
    # destructiveHint=False, idempotentHint=True (deterministic transform;
    # same inputs always produce the same output pixels).
)
def compute_slope(
    dem_uri: str,
    output_unit: Literal["degrees", "percent"] = "degrees",
    algorithm: Literal["Horn", "ZevenbergenThorne"] = "Horn",
    *,
    _storage_client: object | None = None,
    _bucket: str | None = None,
    # absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> LayerURI:
    """Compute terrain slope (steepness) from a DEM. Wraps ``gdaldem slope``.

    Use this when: landslide susceptibility, evacuation/accessibility
    routing, engineering road-grade assessment, or terrain steepness input
    to a code_exec playground zonal-stats recipe. Do NOT use for: hillshade
    (``compute_hillshade``); colored elevation (``compute_colored_relief``);
    aspect (``compute_aspect``).

    Params:
        dem_uri: single-band elevation DEM (typically from ``fetch_dem``).
        output_unit: ``"degrees"`` (default, 0-90) or ``"percent"``
            (rise/run x 100; use for road-grade/engineering contexts).
        algorithm: ``"Horn"`` (default) or ``"ZevenbergenThorne"``
            (smoother, for rough/noisy DEMs).

    Returns:
        ``LayerURI`` for a single-band Float32 slope GeoTIFF (same CRS/grid
        as input; cache bucket, TTL 30d).

    Raises:
        SlopeComputeError: gdaldem unavailable/non-zero, or DEM download
            failure.
    """
    effective_bucket = _bucket or CACHE_BUCKET

    def _fetch() -> bytes:
        dem_bytes = _download_dem_bytes(dem_uri, _storage_client)

        in_tmp: str | None = None
        out_tmp: str | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as in_f:
                in_tmp = in_f.name
                in_f.write(dem_bytes)

            with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as out_f:
                out_tmp = out_f.name
            # Remove the output placeholder so gdaldem creates it fresh
            # (gdaldem errors if the output already exists on some GDAL builds).
            os.unlink(out_tmp)

            _run_gdaldem_slope(in_tmp, out_tmp, output_unit, algorithm)

            return _translate_to_cog(out_tmp)
        finally:
            for path in (in_tmp, out_tmp):
                if path is not None:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass

    params = {
        "dem_uri": dem_uri,
        "output_unit": output_unit,
        "algorithm": algorithm,
    }

    result = read_through(
        metadata=_COMPUTE_SLOPE_METADATA,
        params=params,
        ext="tif",
        fetch_fn=_fetch,
        bucket=effective_bucket,
        storage_client=_storage_client,
    )
    assert result.uri is not None, "compute_slope is cacheable; uri must be set"

    # Only the last path component (the hash) goes into the id, to keep it short.
    dem_key = dem_uri.rstrip("/").rsplit("/", 1)[-1].replace(".tif", "")
    layer_id = f"slope-{dem_key}-{output_unit}-{algorithm}"

    unit_label = "°" if output_unit == "degrees" else "%"
    return LayerURI(
        layer_id=layer_id,
        name=f"Slope ({output_unit}, {algorithm}) [{unit_label}]",
        layer_type="raster",
        uri=result.uri,
        style={"kind": "continuous", "ramp": "ylorrd", "units": "deg",
         "label": "Slope angle", "scale": {"policy": "fixed",
         "range": [0, 60], "transform": "linear"}},
        role="context",
        units=output_unit,
    )
