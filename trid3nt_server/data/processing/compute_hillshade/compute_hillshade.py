"""Atomic tool ``compute_hillshade`` - hillshade raster from DEM.

This module registers one atomic tool that computes a hillshade raster from a DEM
by wrapping GDAL's ``gdaldem hillshade`` command:

    ``compute_hillshade(dem_uri, style, algorithm, azimuth, altitude, z_factor) → LayerURI``

The result is a single-band GeoTIFF in the same CRS and grid as the input DEM,
stored under the cache shim at:

    ``s3://trid3nt-cache/cache/static-30d/hillshade/<key>.tif``

**Style presets:**

- ``"standard"`` -- single hillshade, Horn algorithm, azimuth 315°, altitude 45°
  (the GDAL default). Fast, suitable for general use.
- ``"swiss_double"`` -- two hillshades (Horn @ 315° + Horn @ 135°) multiply-blended
  into a single GeoTIFF via numpy (Imhof-style richer cartographic depth). The
  blend is pre-composited server-side: the LLM-visible result is one layer.
- ``"multidirectional"`` -- single hillshade with ``-multidirectional`` flag; combines
  NE/SE/NW/SW illuminations, no dead-lit sides.
- ``"combined"`` -- ``-combined`` flag; brightness incorporates slope steepness; best
  for steep mountainous terrain.
- ``"smooth"`` -- Horn algorithm + ZevenbergenThorne smoothing flag; smoother results
  on rough terrain.

**Cache key** is derived from ``(dem_uri, style, algorithm, azimuth, altitude, z_factor)`` -- all six parameters materially affect the output pixels.

**Implementation flow (cache miss):**

1. Read the DEM bytes from S3 (or a local path for dev/test).
2. Write to a temp file (``gdaldem`` requires a file path).
3. ``subprocess.run(["gdaldem", "hillshade", <input>, <output>, *flags])`` where:
   - ``-az <azimuth>`` sets the azimuth (315° default).
   - ``-alt <altitude>`` sets the altitude (45° default).
   - ``-z <z_factor>`` sets the vertical exaggeration (1.0 default).
   - ``-alg ZevenbergenThorne`` is added when ``style="smooth"``.
   - ``-multidirectional`` is added when ``style="multidirectional"``.
   - ``-combined`` is added when ``style="combined"``.
   - ``swiss_double`` runs gdaldem TWICE (315° + 135°) then numpy multiply-blends.
4. Read the output bytes, clean up temp files.
5. ``read_through`` writes the bytes to the cache bucket.

**Cross-cutting invariants:**

- **Invariant 2 (Deterministic workflows): preserves.** Zero LLM calls.
- **(cacheable): honors.** ``cacheable=True``, ``ttl_class="static-30d"``,
  ``source_class="hillshade"`` -- DEM-derived output is stable for the lifetime of
  the cached DEM.
- **(resilience): preserves.** gdaldem failures surface as
  ``HillshadeComputeError`` (typed, never unhandled exception); DEM read errors
  are let through for the agent surface to handle.
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
    translate_to_cog,
)

__all__ = [
    "compute_hillshade",
    "HillshadeComputeError",
]

logger = logging.getLogger("trid3nt_server.data.processing.compute_hillshade.compute_hillshade")

# ---------------------------------------------------------------------------
# Error class
# ---------------------------------------------------------------------------


class HillshadeComputeError(RuntimeError):
    """Raised when ``gdaldem hillshade`` fails or the DEM cannot be fetched.

    ``error_code`` carries a SCREAMING_SNAKE_CASE code surfaced in the
    pipeline strip (typed-error requirement).

    Codes:
    - ``GDALDEM_UNAVAILABLE`` -- ``gdaldem`` binary not found on PATH.
    - ``GDALDEM_FAILED`` -- ``gdaldem hillshade`` returned non-zero.
    - ``DEM_DOWNLOAD_FAILED`` -- S3/local read for the DEM URI failed.
    - ``BLEND_FAILED`` -- numpy multiply-blend step failed (swiss_double only).
    """

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


# ---------------------------------------------------------------------------
# Tool metadata
# ---------------------------------------------------------------------------

_COMPUTE_HILLSHADE_METADATA = AtomicToolMetadata(
    name="compute_hillshade",
    ttl_class="static-30d",
    source_class="hillshade",
    cacheable=True,
)

# ---------------------------------------------------------------------------
# gdaldem binary resolution + COG encode (shared runner)
# ---------------------------------------------------------------------------


def _get_gdaldem_bin() -> str:
    """Resolve the ``gdaldem`` binary path (env override -> PATH).

    Raises ``HillshadeComputeError(GDALDEM_UNAVAILABLE)`` if not found.
    """
    binary = resolve_gdaldem()
    if binary is None:
        raise HillshadeComputeError(
            "GDALDEM_UNAVAILABLE",
            "gdaldem binary not found on PATH; set TRID3NT_GDALDEM_BIN "
            "or install gdal-bin.",
        )
    return binary


def _translate_to_cog(input_path: str, gdaldem_bin: object | None = None) -> bytes:
    """In-process COG encode (rasterio). ``gdaldem_bin`` accepted for back-compat, ignored.

    Retained as the module-level entry point that publish_layer / fetch_landcover
    import; delegates to the shared runner's rasterio COG encoder.
    """
    return translate_to_cog(input_path)


def _ensure_output_crs_matches_dem(dem_path: str, output_path: str) -> None:
    """Stamp the DEM's CRS onto the gdaldem output if it degraded.

    Belt-and-suspenders for environments where the PROJ wiring above is not
    enough (or a future gdal build regresses differently): the hillshade is
    on the SAME grid as the input DEM by construction, so when the output's
    CRS does not match the DEM's (typically a proj.db-less ``LOCAL_CS``
    fallback), rewriting the CRS tag in place is always correct.

    Never raises -- a failed stamp logs a warning and leaves the file as
    gdaldem wrote it (legacy behavior).
    """
    try:
        import rasterio

        with rasterio.open(dem_path) as src:
            dem_crs = src.crs
        if dem_crs is None:
            return
        with rasterio.open(output_path) as dst:
            out_crs = dst.crs
        if out_crs == dem_crs:
            return
        with rasterio.open(output_path, "r+") as dst:
            dst.crs = dem_crs
        logger.warning(
            "compute_hillshade: output CRS degraded to %r (gdaldem ran without "
            "proj.db?); re-stamped from DEM as %r",
            str(out_crs),
            str(dem_crs),
        )
    except Exception as exc:  # noqa: BLE001 -- stamp is best-effort
        logger.warning(
            "compute_hillshade: CRS verification/stamp failed for %s (%s: %s) -- "
            "leaving gdaldem output unchanged",
            output_path,
            type(exc).__name__,
            exc,
        )


# ---------------------------------------------------------------------------
# DEM read helper
# ---------------------------------------------------------------------------


def _download_dem_bytes(dem_uri: str, storage_client: object | None = None) -> bytes:
    """Read the DEM bytes from an ``s3://`` URI or a local path (typed error on failure).

    ``storage_client`` is ignored (retained for backward-compatible signatures).
    """
    del storage_client
    return read_raster_bytes(
        dem_uri,
        on_error=lambda msg: HillshadeComputeError("DEM_DOWNLOAD_FAILED", msg),
    )


# ---------------------------------------------------------------------------
# gdaldem hillshade subprocess wrapper
# ---------------------------------------------------------------------------


def _run_gdaldem_hillshade(
    input_path: str,
    output_path: str,
    azimuth: float,
    altitude: float,
    z_factor: float,
    algorithm: Literal["Horn", "ZevenbergenThorne", "Igor"],
    *,
    multidirectional: bool = False,
    combined: bool = False,
) -> None:
    """Run ``gdaldem hillshade`` as a subprocess.

    Args:
        input_path: local file path to the input DEM GeoTIFF.
        output_path: local file path for the output hillshade GeoTIFF.
        azimuth: sun azimuth in degrees (0 - 360, clockwise from north).
        altitude: sun altitude in degrees above the horizon (0 - 90).
        z_factor: vertical exaggeration factor (1.0 = no exaggeration).
        algorithm: gradient algorithm. ``"Horn"`` is the GDAL default.
            ``"ZevenbergenThorne"`` adds ``-alg ZevenbergenThorne``.
            ``"Igor"`` adds ``-igor``.
        multidirectional: if True, adds ``-multidirectional`` flag.
        combined: if True, adds ``-combined`` flag.

    Raises:
        HillshadeComputeError: if the binary is missing or returns non-zero.
    """
    gdaldem = _get_gdaldem_bin()

    cmd: list[str] = [
        gdaldem, "hillshade",
        input_path, output_path,
    ]
    # -az and -multidirectional are mutually exclusive in GDAL; omit -az when
    # multidirectional mode is active (gdaldem rejects the combination).
    if not multidirectional:
        cmd.extend(["-az", str(azimuth)])
    cmd.extend([
        "-alt", str(altitude),
        "-z", str(z_factor),
        "-of", "GTiff",
    ])
    if algorithm == "ZevenbergenThorne":
        cmd.extend(["-alg", "ZevenbergenThorne"])
    elif algorithm == "Igor":
        cmd.extend(["-igor"])
    if multidirectional:
        cmd.append("-multidirectional")
    if combined:
        cmd.append("-combined")

    logger.info(
        "compute_hillshade: running gdaldem hillshade input=%s az=%s alt=%s z=%s "
        "algorithm=%s multidirectional=%s combined=%s cmd=%s",
        input_path, azimuth, altitude, z_factor, algorithm,
        multidirectional, combined, " ".join(cmd),
    )

    run_gdal(
        cmd, gdaldem,
        on_unavailable=lambda msg: HillshadeComputeError("GDALDEM_UNAVAILABLE", msg),
        on_failed=lambda msg: HillshadeComputeError("GDALDEM_FAILED", msg),
    )

    logger.info(
        "compute_hillshade: gdaldem hillshade completed output=%s", output_path
    )


def _multiply_blend_hillshades(
    path_a: str,
    path_b: str,
    output_path: str,
) -> None:
    """Multiply-blend two single-band hillshade GeoTIFFs into one.

    Implements the Imhof "swiss double" blending technique:

        result = (A / 255.0) * (B / 255.0) * 255.0

    The multiply blend darkens valleys (both illuminations are dark) while
    preserving brightness on sun-facing ridges, giving richer cartographic
    depth than a single illumination direction.

    Both inputs must share the same CRS, extent, and grid (they are derived
    from the same DEM so this is guaranteed). The output is written as a
    single-band Float32-normalized-to-uint8 GeoTIFF.

    Args:
        path_a: local path to first hillshade GeoTIFF (e.g. azimuth 315°).
        path_b: local path to second hillshade GeoTIFF (e.g. azimuth 135°).
        output_path: local path for the blended output GeoTIFF.

    Raises:
        HillshadeComputeError: on any numpy/rasterio failure.
    """
    try:
        import numpy as np
        import rasterio

        with rasterio.open(path_a) as src_a:
            data_a = src_a.read(1).astype(np.float32)
            profile = src_a.profile.copy()
            nodata_a = src_a.nodata

        with rasterio.open(path_b) as src_b:
            data_b = src_b.read(1).astype(np.float32)
            nodata_b = src_b.nodata

        # Build masks for nodata regions (gdaldem uses 0 for flat/nodata in
        # hillshade output; preserve those as 0 in the blend).
        mask_a = (data_a == nodata_a) if nodata_a is not None else np.zeros_like(data_a, dtype=bool)
        mask_b = (data_b == nodata_b) if nodata_b is not None else np.zeros_like(data_b, dtype=bool)
        nodata_mask = mask_a | mask_b

        # Multiply blend: (A/255) * (B/255) * 255 -- keeps values in [0, 255].
        blended = (data_a / 255.0) * (data_b / 255.0) * 255.0
        blended = np.clip(blended, 0, 255)
        blended[nodata_mask] = 0.0

        # Write as uint8 GeoTIFF (standard hillshade output dtype).
        profile.update(dtype="uint8", count=1, nodata=0)
        with rasterio.open(output_path, "w", **profile) as dst:
            dst.write(blended.astype(np.uint8), 1)

        logger.info(
            "compute_hillshade: swiss_double blend complete output=%s", output_path
        )

    except HillshadeComputeError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HillshadeComputeError(
            "BLEND_FAILED",
            f"numpy multiply-blend failed for swiss_double: {exc}",
        ) from exc


# ---------------------------------------------------------------------------
# Per-style fetch function builders
# ---------------------------------------------------------------------------


def _make_fetch_fn(
    dem_uri: str,
    style: str,
    algorithm: Literal["Horn", "ZevenbergenThorne", "Igor"],
    azimuth: float,
    altitude: float,
    z_factor: float,
    storage_client: object | None,
) -> bytes:
    """Produce hillshade bytes for the given style on cache-miss.

    Returns the raw bytes of the output GeoTIFF.
    """
    dem_bytes = _download_dem_bytes(dem_uri, storage_client)

    in_tmp: str | None = None
    out_tmp: str | None = None
    out_tmp_b: str | None = None  # only for swiss_double
    blend_tmp: str | None = None  # only for swiss_double

    try:
        with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as in_f:
            in_tmp = in_f.name
            in_f.write(dem_bytes)

        with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as out_f:
            out_tmp = out_f.name
        os.unlink(out_tmp)  # gdaldem errors if output file already exists on some builds

        if style == "swiss_double":
            # Run gdaldem twice: azimuth 315° + azimuth 135°; then multiply-blend.
            with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as out_b_f:
                out_tmp_b = out_b_f.name
            os.unlink(out_tmp_b)

            # First pass: primary azimuth (315° -- the classic NW sun position).
            _run_gdaldem_hillshade(
                in_tmp, out_tmp,
                azimuth=315.0, altitude=altitude, z_factor=z_factor,
                algorithm=algorithm,
            )
            # Second pass: complementary azimuth (135° -- SE, fills shadows from 315°).
            _run_gdaldem_hillshade(
                in_tmp, out_tmp_b,
                azimuth=135.0, altitude=altitude, z_factor=z_factor,
                algorithm=algorithm,
            )
            # Multiply-blend the two hillshades into a single GeoTIFF.
            with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as blend_f:
                blend_tmp = blend_f.name
            os.unlink(blend_tmp)
            _multiply_blend_hillshades(out_tmp, out_tmp_b, blend_tmp)
            _ensure_output_crs_matches_dem(in_tmp, blend_tmp)
            # serve a real COG - see _translate_to_cog.
            return _translate_to_cog(blend_tmp)

        elif style == "multidirectional":
            _run_gdaldem_hillshade(
                in_tmp, out_tmp,
                azimuth=azimuth, altitude=altitude, z_factor=z_factor,
                algorithm=algorithm,
                multidirectional=True,
            )
        elif style == "combined":
            _run_gdaldem_hillshade(
                in_tmp, out_tmp,
                azimuth=azimuth, altitude=altitude, z_factor=z_factor,
                algorithm=algorithm,
                combined=True,
            )
        elif style == "smooth":
            # ZevenbergenThorne smoothing -- use the algorithm kwarg override if
            # the caller explicitly chose a different algorithm, but the preset
            # itself is intended for smoothed results.
            _run_gdaldem_hillshade(
                in_tmp, out_tmp,
                azimuth=azimuth, altitude=altitude, z_factor=z_factor,
                algorithm="ZevenbergenThorne",
            )
        else:
            # "standard" (and custom: use whatever algorithm/az/alt/z are set).
            _run_gdaldem_hillshade(
                in_tmp, out_tmp,
                azimuth=azimuth, altitude=altitude, z_factor=z_factor,
                algorithm=algorithm,
            )

        _ensure_output_crs_matches_dem(in_tmp, out_tmp)
        # serve a real COG (tiled + overviews) - see _translate_to_cog.
        return _translate_to_cog(out_tmp)

    finally:
        for path in (in_tmp, out_tmp, out_tmp_b, blend_tmp):
            if path is not None:
                try:
                    os.unlink(path)
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


@register_tool(
    _COMPUTE_HILLSHADE_METADATA,
    # Annotations: readOnlyHint=True (reads input raster/vector; writes cache
    # artifact only via the read-through shim), openWorldHint=False (all
    # computation is local GDAL/numpy; no external API calls),
    # destructiveHint=False, idempotentHint=True (deterministic transform;
    # same inputs always produce the same output pixels).
)
def compute_hillshade(
    dem_uri: str,
    style: Literal["standard", "swiss_double", "multidirectional", "combined", "smooth"] = "standard",
    # Power-user overrides (primarily consulted for "standard"; presets override
    # specific fields -- e.g. "smooth" always uses ZevenbergenThorne).
    algorithm: Literal["Horn", "ZevenbergenThorne", "Igor"] = "Horn",
    azimuth: float = 315.0,
    altitude: float = 45.0,
    z_factor: float = 1.0,
    *,
    _storage_client: object | None = None,
    _bucket: str | None = None,
    # absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Compute a hillshade raster from a DEM (wraps ``gdaldem hillshade``).

    Use this (not compute_slope or compute_colored_relief) for the
    shaded-relief HILLSHADE render of terrain: cartographic context beneath
    a flood/habitat/hazard overlay, showing terrain influence (ridge/valley
    drainage) on results, a Swiss-style multiply-blend stack (colored relief
    + this), or an explicit "hillshade"/"terrain background"/"shaded relief"
    request. Applies GDAL's hillshade algorithm to a single-band elevation
    GeoTIFF, returns a single-band uint8 intensity raster (0-255), same
    CRS/grid. Five style presets control illumination + blending.

    Do NOT use for: slope/aspect analysis (compute_slope/compute_aspect);
    quantitative elevation display/stats (compute_colored_relief or
    the code_exec playground); bathymetry; animated/time-varying terrain
    (static single-time raster only).

    Style presets:
        "standard" (default): single hillshade, Horn algorithm, azimuth
            315deg, altitude 45deg -- fast, general use.
        "swiss_double": two Horn hillshades (315deg + 135deg) multiply-blended
            into one GeoTIFF (Imhof-style) -- darker valleys, bright ridges;
            best for cartography/"professional"/"nice-looking" requests.
        "multidirectional": GDAL ``-multidirectional`` (NE/SE/NW/SW combined,
            no dead-lit sides) -- pick for "no dead spots"/"no shadows" on
            complex ridge terrain.
        "combined": GDAL ``-combined`` (brightness incorporates slope
            steepness) -- pick for mountains/steep terrain.
        "smooth": Horn + ZevenbergenThorne gradient estimator, less
            high-frequency noise -- pick for rough/noisy DEMs.

    Params:
        dem_uri: URI of a DEM GeoTIFF (typically from ``fetch_dem``),
            single-band elevation in meters.
        style: one of the five preset names above; power-user ``algorithm``/
            ``azimuth``/``altitude``/``z_factor`` overrides apply only to
            "standard" (other presets override them, e.g. "smooth" always
            uses ZevenbergenThorne).
        algorithm: ``"Horn"`` (default, standard 3x3 gradient) |
            ``"ZevenbergenThorne"`` (smoother) | ``"Igor"`` (experimental,
            steep terrain). Only consulted for "standard"/"swiss_double".
        azimuth: sun azimuth degrees (0-360, clockwise from north). Default
            315 (NW).
        altitude: sun altitude degrees (0-90). Default 45; higher flattens
            shading, lower emphasizes relief.
        z_factor: vertical exaggeration. Default 1.0; >1.0 amplifies relief
            (useful for low-relief coastal DEMs).

    Returns:
        ``LayerURI`` pointing at a hillshade GeoTIFF:
        ``s3://trid3nt-cache/cache/static-30d/hillshade/<key>.tif`` (for
        "swiss_double", the pre-blended composite). Single-band uint8
        (0-255), same CRS/grid as the input DEM.

    routed through ``read_through`` -- identical
    ``(dem_uri, style, algorithm, azimuth, altitude, z_factor)`` reuses the
    cached hillshade (30-day TTL).

    Raises:
        HillshadeComputeError: gdaldem unavailable/non-zero exit, DEM
            download fails, or the swiss_double blend fails.
    """
    effective_bucket = _bucket or CACHE_BUCKET

    def _fetch() -> bytes:
        return _make_fetch_fn(
            dem_uri=dem_uri,
            style=style,
            algorithm=algorithm,
            azimuth=azimuth,
            altitude=altitude,
            z_factor=z_factor,
            storage_client=_storage_client,
        )

    # Cache key on all six parameters; style drives the actual algorithm choices
    # but we include algorithm/azimuth/altitude/z_factor so that "standard" with
    # custom overrides can coexist with "standard" at defaults in the same cache.
    params = {
        "dem_uri": dem_uri,
        "style": style,
        "algorithm": algorithm,
        "azimuth": azimuth,
        "altitude": altitude,
        "z_factor": z_factor,
    }

    result = read_through(
        metadata=_COMPUTE_HILLSHADE_METADATA,
        params=params,
        ext="tif",
        fetch_fn=_fetch,
        bucket=effective_bucket,
        storage_client=_storage_client,
    )
    assert result.uri is not None, "compute_hillshade is cacheable; uri must be set"

    # Build a concise layer_id and human-readable name.
    dem_key = dem_uri.rstrip("/").rsplit("/", 1)[-1].replace(".tif", "")
    layer_id = f"hillshade-{dem_key}-{style}"

    style_labels = {
        "standard": "Hillshade (Standard)",
        "swiss_double": "Hillshade (Swiss Double)",
        "multidirectional": "Hillshade (Multidirectional)",
        "combined": "Hillshade (Combined)",
        "smooth": "Hillshade (Smooth)",
    }
    name = style_labels.get(style, f"Hillshade ({style})")

    return LayerURI(
        layer_id=layer_id,
        name=name,
        layer_type="raster",
        uri=result.uri,
        style_preset="continuous_dem",  # grayscale via the F51 terrain passthrough -- CORRECT for shaded relief (tools-backlog #3: no colormap wanted)
        role="context",
        units="intensity",  # 0 - 255 uint8 luminance
    )
