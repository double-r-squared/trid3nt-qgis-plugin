"""Atomic tool ``compute_aspect`` - terrain aspect raster from a DEM.

Wraps ``gdaldem aspect``. The output keeps the input DEM's CRS and grid, and the
cache key is (dem_uri, algorithm, zero_for_flat) - all three move pixels.
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
from trid3nt_server.tools.processing._gdal_runner import (
    read_raster_bytes,
    resolve_gdaldem,
    run_gdal,
)

__all__ = [
    "compute_aspect",
    "AspectComputeError",
]

logger = logging.getLogger("trid3nt_server.tools.processing.compute_aspect.compute_aspect")

# ---------------------------------------------------------------------------
# Error class
# ---------------------------------------------------------------------------


class AspectComputeError(RuntimeError):
    """``gdaldem aspect`` failed or the DEM could not be read. ``error_code`` is one
    of GDALDEM_UNAVAILABLE, GDALDEM_FAILED, DEM_DOWNLOAD_FAILED.
    """

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


# ---------------------------------------------------------------------------
# Tool metadata
# ---------------------------------------------------------------------------

_COMPUTE_ASPECT_METADATA = AtomicToolMetadata(
    name="compute_aspect",
    ttl_class="static-30d",
    source_class="aspect",
    cacheable=True,
)

# ---------------------------------------------------------------------------
# gdaldem binary resolution + DEM read (shared runner)
# ---------------------------------------------------------------------------


def _get_gdaldem_bin() -> str:
    """Resolve ``gdaldem``; absent raises ``AspectComputeError(GDALDEM_UNAVAILABLE)``."""
    binary = resolve_gdaldem()
    if binary is None:
        raise AspectComputeError(
            "GDALDEM_UNAVAILABLE",
            "gdaldem binary not found on PATH; set TRID3NT_GDALDEM_BIN "
            "or install gdal-bin.",
        )
    return binary


def _download_dem_bytes(dem_uri: str, storage_client: object | None = None) -> bytes:
    """Read the DEM bytes from an ``s3://`` URI or a local path; ``storage_client``
    is ignored and a read failure raises ``AspectComputeError``.
    """
    del storage_client
    return read_raster_bytes(
        dem_uri,
        on_error=lambda msg: AspectComputeError("DEM_DOWNLOAD_FAILED", msg),
    )


# ---------------------------------------------------------------------------
# gdaldem aspect subprocess wrapper
# ---------------------------------------------------------------------------


def _run_gdaldem_aspect(
    input_path: str,
    output_path: str,
    algorithm: Literal["Horn", "ZevenbergenThorne"],
    zero_for_flat: bool,
) -> None:
    """Run ``gdaldem aspect`` on local paths; ``zero_for_flat`` sends flat cells to
    0 rather than -9999. A missing binary or non-zero exit raises ``AspectComputeError``.
    """
    gdaldem = _get_gdaldem_bin()

    cmd: list[str] = [gdaldem, "aspect", input_path, output_path]
    if zero_for_flat:
        cmd.append("-zero_for_flat")
    if algorithm == "ZevenbergenThorne":
        cmd.extend(["-alg", "ZevenbergenThorne"])

    logger.info(
        "compute_aspect: running gdaldem aspect input=%s algorithm=%s zero_for_flat=%s cmd=%s",
        input_path,
        algorithm,
        zero_for_flat,
        " ".join(cmd),
    )

    run_gdal(
        cmd, gdaldem,
        on_unavailable=lambda msg: AspectComputeError("GDALDEM_UNAVAILABLE", msg),
        on_failed=lambda msg: AspectComputeError("GDALDEM_FAILED", msg),
    )

    logger.info(
        "compute_aspect: gdaldem aspect completed output=%s", output_path
    )


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


@register_tool(
    _COMPUTE_ASPECT_METADATA,
    # Annotations: readOnlyHint=True (reads input raster/vector; writes cache
    # artifact only via the read-through shim), openWorldHint=False (all
    # computation is local GDAL/numpy; no external API calls),
    # destructiveHint=False, idempotentHint=True (deterministic transform;
    # same inputs always produce the same output pixels).
)
def compute_aspect(
    dem_uri: str,
    algorithm: Literal["Horn", "ZevenbergenThorne"] = "Horn",
    zero_for_flat: bool = True,
    *,
    _storage_client: object | None = None,
    _bucket: str | None = None,
    # absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> LayerURI:
    """Compute terrain aspect (compass face direction) from a DEM. Wraps ``gdaldem aspect``.

    Use this when: solar exposure, wildfire fire/wind-direction correlation,
    landslide/habitat aspect preference, or "which way do slopes face?".
    Do NOT use for: steepness (``compute_slope``); shadow visualization
    (``compute_hillshade``); colored elevation basemap
    (``compute_colored_relief``).

    Params:
        dem_uri: single-band elevation DEM (typically from ``fetch_dem``).
        algorithm: ``"Horn"`` (default) or ``"ZevenbergenThorne"`` (smoother,
            for noisy DEMs).
        zero_for_flat: ``True`` (default) labels flat cells 0 (North);
            ``False`` labels them -9999 so flat stays distinguishable.

    Returns:
        ``LayerURI`` for a Float32 aspect GeoTIFF (0-360 deg, North=0,
        East=90), same CRS/grid as the input.

    Raises:
        AspectComputeError: gdaldem unavailable/non-zero, or DEM download failure.
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

            _run_gdaldem_aspect(in_tmp, out_tmp, algorithm, zero_for_flat)

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
        "algorithm": algorithm,
        "zero_for_flat": zero_for_flat,
    }

    result = read_through(
        metadata=_COMPUTE_ASPECT_METADATA,
        params=params,
        ext="tif",
        fetch_fn=_fetch,
        bucket=effective_bucket,
        storage_client=_storage_client,
    )
    assert result.uri is not None, "compute_aspect is cacheable; uri must be set"

    # Only the last path component (the hash) goes into the id, to keep it short.
    dem_key = dem_uri.rstrip("/").rsplit("/", 1)[-1].replace(".tif", "")
    zff_label = "zff" if zero_for_flat else "nozff"
    layer_id = f"aspect-{dem_key}-{algorithm}-{zff_label}"

    return LayerURI(
        layer_id=layer_id,
        name=f"Aspect ({algorithm}, {'zero-flat' if zero_for_flat else 'nodata-flat'})",
        layer_type="raster",
        uri=result.uri,
        style={"kind": "continuous", "ramp": "hsv", "units": "deg",
         "label": "Aspect", "scale": {"policy": "fixed",
         "range": [0, 360], "transform": "linear"}},
        role="context",
        units="degrees",
    )
