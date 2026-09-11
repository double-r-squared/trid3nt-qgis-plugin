"""Atomic tool ``compute_hillshade`` - hillshade raster from a DEM.

Wraps ``gdaldem hillshade`` into a single-band uint8 raster on the input DEM's
grid; the cache key is all six parameters, so overrides never collide.
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
    "compute_hillshade",
    "HillshadeComputeError",
]

logger = logging.getLogger("trid3nt_server.tools.derive.compute_hillshade.compute_hillshade")

# ---------------------------------------------------------------------------
# Error class
# ---------------------------------------------------------------------------


class HillshadeComputeError(RuntimeError):
    """``gdaldem hillshade`` failed or the DEM could not be read. ``error_code`` is
    one of GDALDEM_UNAVAILABLE, GDALDEM_FAILED, DEM_DOWNLOAD_FAILED, BLEND_FAILED.
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
    """Resolve ``gdaldem``; absent raises
    ``HillshadeComputeError(GDALDEM_UNAVAILABLE)``.
    """
    binary = resolve_gdaldem()
    if binary is None:
        raise HillshadeComputeError(
            "GDALDEM_UNAVAILABLE",
            "gdaldem binary not found on PATH; set TRID3NT_GDALDEM_BIN "
            "or install gdal-bin.",
        )
    return binary


def _ensure_output_crs_matches_dem(dem_path: str, output_path: str) -> None:
    """Stamp the DEM's CRS onto the gdaldem output when it degraded; never raises,
    so a failed stamp leaves the file as gdaldem wrote it.
    """
    # The hillshade is on the SAME grid as the input DEM by construction, so a
    # mismatched output CRS is a proj.db-less LOCAL_CS fallback and rewriting
    # the tag in place is always correct.
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
    """Read the DEM bytes from an ``s3://`` URI or a local path; ``storage_client``
    is ignored and a read failure raises ``HillshadeComputeError``.
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
    """Run ``gdaldem hillshade`` on local paths; azimuth and altitude are degrees,
    and a missing binary or non-zero exit raises ``HillshadeComputeError``.
    """
    gdaldem = _get_gdaldem_bin()

    cmd: list[str] = [
        gdaldem, "hillshade",
        input_path, output_path,
    ]
    # gdaldem rejects -az together with -multidirectional.
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
    """Multiply-blend two single-band hillshades sharing a grid into one uint8
    GeoTIFF; any numpy or rasterio failure raises ``HillshadeComputeError``.
    """
    # The Imhof multiply, result = (A/255) * (B/255) * 255, darkens a valley
    # where both illuminations are dark while keeping sun-facing ridges bright.
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

        # gdaldem writes 0 for flat and nodata cells, so the blend keeps 0.
        mask_a = (data_a == nodata_a) if nodata_a is not None else np.zeros_like(data_a, dtype=bool)
        mask_b = (data_b == nodata_b) if nodata_b is not None else np.zeros_like(data_b, dtype=bool)
        nodata_mask = mask_a | mask_b

        blended = (data_a / 255.0) * (data_b / 255.0) * 255.0
        blended = np.clip(blended, 0, 255)
        blended[nodata_mask] = 0.0

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
    """Hillshade GeoTIFF bytes for ``style`` on a cache miss."""
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
        # Some gdaldem builds error when the output file already exists.
        os.unlink(out_tmp)

        if style == "swiss_double":
            with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as out_b_f:
                out_tmp_b = out_b_f.name
            os.unlink(out_tmp_b)

            # 315 deg is the classic NW sun position.
            _run_gdaldem_hillshade(
                in_tmp, out_tmp,
                azimuth=315.0, altitude=altitude, z_factor=z_factor,
                algorithm=algorithm,
            )
            # 135 deg is its complement, filling the shadows the first pass cast.
            _run_gdaldem_hillshade(
                in_tmp, out_tmp_b,
                azimuth=135.0, altitude=altitude, z_factor=z_factor,
                algorithm=algorithm,
            )
            with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as blend_f:
                blend_tmp = blend_f.name
            os.unlink(blend_tmp)
            _multiply_blend_hillshades(out_tmp, out_tmp_b, blend_tmp)
            _ensure_output_crs_matches_dem(in_tmp, blend_tmp)
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
            # The preset pins ZevenbergenThorne over any algorithm override.
            _run_gdaldem_hillshade(
                in_tmp, out_tmp,
                azimuth=azimuth, altitude=altitude, z_factor=z_factor,
                algorithm="ZevenbergenThorne",
            )
        else:
            _run_gdaldem_hillshade(
                in_tmp, out_tmp,
                azimuth=azimuth, altitude=altitude, z_factor=z_factor,
                algorithm=algorithm,
            )

        _ensure_output_crs_matches_dem(in_tmp, out_tmp)
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
    # A preset overrides these, so they are only consulted for "standard".
    algorithm: Literal["Horn", "ZevenbergenThorne", "Igor"] = "Horn",
    azimuth: float = 315.0,
    altitude: float = 45.0,
    z_factor: float = 1.0,
    *,
    _storage_client: object | None = None,
    _bucket: str | None = None,
    # absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> LayerURI:
    """Compute a hillshade raster from a DEM (wraps ``gdaldem hillshade``).

    Use this, not compute_slope or compute_colored_relief, for the shaded-relief
    HILLSHADE render: context beneath an overlay, terrain influence on a result,
    a Swiss-style stack under colored relief. Returns single-band uint8 on the
    DEM's grid. Not for slope or aspect, quantitative elevation, or bathymetry.

    Params:
        dem_uri: single-band elevation DEM in metres (typically ``fetch_dem``).
        style: "standard" (Horn, az 315, alt 45), "swiss_double" (two Horn
            passes multiply-blended, darker valleys - the cartographic pick),
            "multidirectional" (no dead-lit sides), "combined" (brightness
            carries slope, for steep terrain), "smooth" (ZevenbergenThorne,
            for noisy DEMs).
        algorithm, azimuth, altitude, z_factor: overrides consulted for
            "standard" only. Higher altitude flattens the shading; z_factor
            above 1.0 amplifies low relief.
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
        style={"kind": "continuous", "ramp": "gray", "units": "m", "label": "Elevation"},
        role="context",
        units="intensity",  # 0 - 255 uint8 luminance
    )
