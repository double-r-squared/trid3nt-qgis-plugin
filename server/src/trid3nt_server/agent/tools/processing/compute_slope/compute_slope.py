"""Atomic tool ``compute_slope`` - terrain slope raster from DEM (FR-CE-8, FR-DC).

This module registers one atomic tool that computes a slope raster from a DEM
by wrapping GDAL's ``gdaldem slope`` command:

    ``compute_slope(dem_uri, output_unit, algorithm) → LayerURI``

The result is a single-band GeoTIFF (units: degrees or percent rise/run) in the
same CRS and grid as the input DEM, stored under the FR-DC-3 cache shim at:

    ``s3://trid3nt-cache/cache/static-30d/slope/<key>.tif``

**Cache key** is derived from ``(dem_uri, output_unit, algorithm)`` — all three
parameters materially affect the output pixels, so all three participate in
cache-key derivation (FR-DC-3).

**Implementation flow (cache miss):**

1. Read the DEM bytes from S3 (or a local path for dev/test).
2. Write to a temp file (``gdaldem`` requires a file path, not stdin).
3. ``subprocess.run(["gdaldem", "slope", <input>, <output>, *flags])`` where:
   - ``-p`` is added when ``output_unit="percent"`` (percent rise/run).
   - ``-alg ZevenbergenThorne`` is added when ``algorithm="ZevenbergenThorne"``.
   - Horn is the GDAL default (no flag needed).
4. Read the output temp file, clean up.
5. ``read_through`` writes the bytes to the cache bucket.

**Cross-cutting invariants:**

- **Invariant 2 (Deterministic workflows): preserves.** Zero LLM calls.
- **FR-DC-6 (cacheable): honors.** ``cacheable=True``, ``ttl_class="static-30d"``,
  ``source_class="slope"`` — DEM-derived output is stable for the lifetime of
  the cached DEM.
- **NFR-R-1 (resilience): preserves.** gdaldem failures surface as
  ``SlopeComputeError`` (typed, never unhandled exception); DEM read
  errors are let through for the agent FR-AS-11 surface to handle.
"""

from __future__ import annotations

import logging
import os
import tempfile
from typing import Literal, Any

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.agent.tools import register_tool
from trid3nt_server.agent.tools.cache import CACHE_BUCKET, read_through
from trid3nt_server.agent.tools.processing._gdal_runner import (
    read_raster_bytes,
    resolve_gdaldem,
    run_gdal,
    translate_to_cog as _translate_to_cog,
)

__all__ = [
    "compute_slope",
    "SlopeComputeError",
]

logger = logging.getLogger("trid3nt_server.agent.tools.processing.compute_slope.compute_slope")

# ---------------------------------------------------------------------------
# Error class
# ---------------------------------------------------------------------------


class SlopeComputeError(RuntimeError):
    """Raised when ``gdaldem slope`` fails or the DEM cannot be fetched.

    ``error_code`` carries a SCREAMING_SNAKE_CASE code surfaced in the
    pipeline strip (NFR-R-1 typed-error requirement).

    Codes:
    - ``GDALDEM_UNAVAILABLE`` — ``gdaldem`` binary not found on PATH.
    - ``GDALDEM_FAILED`` — ``gdaldem slope`` returned non-zero.
    - ``DEM_DOWNLOAD_FAILED`` — S3/local read for the DEM URI failed.
    """

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


# ---------------------------------------------------------------------------
# Tool metadata
# ---------------------------------------------------------------------------

_COMPUTE_SLOPE_METADATA = AtomicToolMetadata(
    name="compute_slope",
    ttl_class="static-30d",
    source_class="slope",
    cacheable=True,
)

# ---------------------------------------------------------------------------
# gdaldem binary resolution + DEM read (shared runner)
# ---------------------------------------------------------------------------


def _get_gdaldem_bin() -> str:
    """Resolve the ``gdaldem`` binary path (env override -> PATH).

    Raises ``SlopeComputeError(GDALDEM_UNAVAILABLE)`` if not found.
    """
    binary = resolve_gdaldem()
    if binary is None:
        raise SlopeComputeError(
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
        on_error=lambda msg: SlopeComputeError("DEM_DOWNLOAD_FAILED", msg),
    )


# ---------------------------------------------------------------------------
# gdaldem slope subprocess wrapper
# ---------------------------------------------------------------------------


def _run_gdaldem_slope(
    input_path: str,
    output_path: str,
    output_unit: Literal["degrees", "percent"],
    algorithm: Literal["Horn", "ZevenbergenThorne"],
) -> None:
    """Run ``gdaldem slope`` as a subprocess.

    Args:
        input_path: local file path to the input DEM GeoTIFF.
        output_path: local file path for the output slope GeoTIFF.
        output_unit: ``"degrees"`` (default GDAL) or ``"percent"`` (adds ``-p``).
        algorithm: ``"Horn"`` (default) or ``"ZevenbergenThorne"`` (adds ``-alg ZevenbergenThorne``).

    Raises:
        SlopeComputeError: if the binary is missing or returns non-zero.
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


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


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
    # absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Compute terrain slope (steepness) from a DEM. Wraps ``gdaldem slope``.

    Use this when: landslide susceptibility, evacuation/accessibility
    routing, engineering road-grade assessment, or terrain steepness input
    to ``compute_zonal_statistics``. Do NOT use for: hillshade
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

            # 3. Run gdaldem slope.
            _run_gdaldem_slope(in_tmp, out_tmp, output_unit, algorithm)

            # 4. return real COG bytes (tiled + overviews).
            return _translate_to_cog(out_tmp)
        finally:
            for path in (in_tmp, out_tmp):
                if path is not None:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass

    # Cache key on (dem_uri, output_unit, algorithm).
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

    # Build a stable layer_id from the DEM URI + parameters.
    # Use only the last component of the path (the hash) to keep IDs concise.
    dem_key = dem_uri.rstrip("/").rsplit("/", 1)[-1].replace(".tif", "")
    layer_id = f"slope-{dem_key}-{output_unit}-{algorithm}"

    unit_label = "°" if output_unit == "degrees" else "%"
    return LayerURI(
        layer_id=layer_id,
        name=f"Slope ({output_unit}, {algorithm}) [{unit_label}]",
        layer_type="raster",
        uri=result.uri,
        style_preset="slope_angle_deg",  # tools-backlog: slope-angle ylorrd ramp (deg). Backend colormap here; the Orchestrator wires the frontend legend.
        role="context",
        units=output_unit,
    )
