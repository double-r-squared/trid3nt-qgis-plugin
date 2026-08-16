"""Atomic tool ``compute_aspect`` - terrain aspect raster from DEM.

This module registers one atomic tool that computes an aspect raster from a DEM
by wrapping GDAL's ``gdaldem aspect`` command:

    ``compute_aspect(dem_uri, algorithm, zero_for_flat) → LayerURI``

The result is a single-band GeoTIFF (compass direction 0–360°; 0=N, 90=E,
180=S, 270=W) in the same CRS and grid as the input DEM, stored under the
cache shim at:

    ``s3://trid3nt-cache/cache/static-30d/aspect/<key>.tif``

**Cache key** is derived from ``(dem_uri, algorithm, zero_for_flat)`` — all
three parameters materially affect the output pixels, so all three participate
in cache-key derivation.

**Implementation flow (cache miss):**

1. Read the DEM bytes from S3 (or a local path for dev/test).
2. Write to a temp file (``gdaldem`` requires a file path, not stdin).
3. ``subprocess.run(["gdaldem", "aspect", <input>, <output>, *flags])`` where:
   - ``-zero_for_flat`` is added when ``zero_for_flat=True`` (flat areas → 0
     instead of the gdaldem default of -9999).
   - ``-alg ZevenbergenThorne`` is added when ``algorithm="ZevenbergenThorne"``.
   - Horn is the GDAL default (no flag needed).
4. Read the output temp file, clean up.
5. ``read_through`` writes the bytes to the cache bucket.

**Cross-cutting invariants:**

- **Invariant 2 (Deterministic workflows): preserves.** Zero LLM calls.
- **(cacheable): honors.** ``cacheable=True``, ``ttl_class="static-30d"``,
  ``source_class="aspect"`` — DEM-derived output is stable for the lifetime of
  the cached DEM.
- **(resilience): preserves.** gdaldem failures surface as
  ``AspectComputeError`` (typed, never unhandled exception); DEM read
  errors are let through for the agent surface to handle.
"""

from __future__ import annotations

import logging
import os
import tempfile
from typing import Literal, Any

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.data import register_tool
from trid3nt_server.data.cache import CACHE_BUCKET, read_through
from trid3nt_server.data.processing._gdal_runner import (
    read_raster_bytes,
    resolve_gdaldem,
    run_gdal,
    translate_to_cog as _translate_to_cog,
)

__all__ = [
    "compute_aspect",
    "AspectComputeError",
]

logger = logging.getLogger("trid3nt_server.data.processing.compute_aspect.compute_aspect")

# ---------------------------------------------------------------------------
# Error class
# ---------------------------------------------------------------------------


class AspectComputeError(RuntimeError):
    """Raised when ``gdaldem aspect`` fails or the DEM cannot be fetched.

    ``error_code`` carries a SCREAMING_SNAKE_CASE code surfaced in the
    pipeline strip (typed-error requirement).

    Codes:
    - ``GDALDEM_UNAVAILABLE`` — ``gdaldem`` binary not found on PATH.
    - ``GDALDEM_FAILED`` — ``gdaldem aspect`` returned non-zero.
    - ``DEM_DOWNLOAD_FAILED`` — S3/local read for the DEM URI failed.
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
    """Resolve the ``gdaldem`` binary path (env override -> PATH).

    Raises ``AspectComputeError(GDALDEM_UNAVAILABLE)`` if not found.
    """
    binary = resolve_gdaldem()
    if binary is None:
        raise AspectComputeError(
            "GDALDEM_UNAVAILABLE",
            "gdaldem binary not found on PATH; set TRID3NT_GDALDEM_BIN "
            "or install gdal-bin.",
        )
    return binary


def _download_dem_bytes(dem_uri: str, storage_client: object | None = None) -> bytes:
    """Read the DEM bytes from an ``s3://`` URI or a local path (typed error on failure).

    ``storage_client`` is ignored (retained for backward-compatible signatures).
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
    """Run ``gdaldem aspect`` as a subprocess.

    Args:
        input_path: local file path to the input DEM GeoTIFF.
        output_path: local file path for the output aspect GeoTIFF.
        algorithm: ``"Horn"`` (default) or ``"ZevenbergenThorne"``
            (adds ``-alg ZevenbergenThorne``).
        zero_for_flat: if True, adds ``-zero_for_flat`` flag so flat areas
            output 0 instead of the gdaldem default of -9999.

    Raises:
        AspectComputeError: if the binary is missing or returns non-zero.
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
    # absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Compute terrain aspect (compass face direction) from a DEM. Wraps ``gdaldem aspect``.

    Use this when: solar exposure, wildfire fire/wind-direction correlation,
    landslide/habitat aspect preference, or "which way do slopes face?".
    Do NOT use for: steepness (``compute_slope``); shadow visualization
    (``compute_hillshade``); colored elevation basemap
    (``compute_colored_relief``).

    Params:
        dem_uri: single-band elevation DEM GeoTIFF (typically from
            ``fetch_dem``).
        algorithm: ``"Horn"`` (default, 3x3 gradient) or
            ``"ZevenbergenThorne"`` (smoother, for rough/noisy DEMs).
        zero_for_flat: ``True`` (default) labels flat areas aspect=0
            (North); ``False`` labels them ``-9999`` so downstream code can
            distinguish flat from north-facing.

    Returns:
        ``LayerURI`` for a single-band Float32 aspect GeoTIFF (0-360 deg,
        North=0, East=90), same CRS/grid as input, cache bucket, TTL 30d.

    Raises:
        AspectComputeError: gdaldem unavailable/non-zero exit, or DEM
            download failure.
    """
    effective_bucket = _bucket or CACHE_BUCKET

    def _fetch() -> bytes:
        # 1. Download the DEM.
        dem_bytes = _download_dem_bytes(dem_uri, _storage_client)

        # 2. Write to a temp input file.
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

            # 3. Run gdaldem aspect.
            _run_gdaldem_aspect(in_tmp, out_tmp, algorithm, zero_for_flat)

            # 4. return real COG bytes (tiled + overviews).
            return _translate_to_cog(out_tmp)
        finally:
            for path in (in_tmp, out_tmp):
                if path is not None:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass

    # Cache key on (dem_uri, algorithm, zero_for_flat).
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

    # Build a stable layer_id from the DEM URI + parameters.
    # Use only the last component of the path (the hash) to keep IDs concise.
    dem_key = dem_uri.rstrip("/").rsplit("/", 1)[-1].replace(".tif", "")
    zff_label = "zff" if zero_for_flat else "nozff"
    layer_id = f"aspect-{dem_key}-{algorithm}-{zff_label}"

    return LayerURI(
        layer_id=layer_id,
        name=f"Aspect ({algorithm}, {'zero-flat' if zero_for_flat else 'nodata-flat'})",
        layer_type="raster",
        uri=result.uri,
        style_preset="aspect_compass_deg",  # tools-backlog: cyclic compass-aspect hsv ramp (deg). Backend colormap here; the Orchestrator wires the frontend compass legend.
        role="context",
        units="degrees",
    )
