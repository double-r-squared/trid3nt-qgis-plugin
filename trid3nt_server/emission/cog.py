"""COG encoding - the publication step that makes a raster renderable.

Best-effort by contract: a failed encode returns the input bytes unchanged, so a
raster that renders slowly is still published.
"""

from __future__ import annotations

import logging
import os
import tempfile

logger = logging.getLogger("trid3nt_server.emission.cog")

__all__ = ["translate_to_cog"]


def translate_to_cog(input_path: str) -> bytes:
    """Encode a flat GeoTIFF into tiled COG-with-overviews bytes.
    Preserves dtype, CRS, transform, nodata, band color-interpretation and a
    band-1 palette. Returns the input bytes unchanged on any failure; never raises.
    """
    out_tmp: str | None = None
    try:
        import rasterio

        with rasterio.open(input_path) as src:
            profile = {
                "driver": "COG",
                "width": src.width,
                "height": src.height,
                "count": src.count,
                "dtype": src.dtypes[0],
                "crs": src.crs,
                "transform": src.transform,
                "compress": "DEFLATE",
            }
            if src.nodata is not None:
                profile["nodata"] = src.nodata
            data = src.read()
            colorinterp = src.colorinterp
            try:
                cmap = src.colormap(1)
            except (ValueError, KeyError):
                cmap = None
            except Exception:  # noqa: BLE001 -- any read failure -> no colormap
                cmap = None
        with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as of:
            out_tmp = of.name
        with rasterio.open(out_tmp, "w", OVERVIEW_RESAMPLING="NEAREST", **profile) as dst:
            dst.write(data)
            try:
                dst.colorinterp = colorinterp
            except Exception:  # noqa: BLE001 -- colorinterp set is best-effort
                pass
            if cmap:
                try:
                    dst.write_colormap(1, cmap)
                except Exception:  # noqa: BLE001 -- colormap copy is best-effort
                    pass
        with open(out_tmp, "rb") as f:
            return f.read()
    except Exception as exc:  # noqa: BLE001 -- COG encode is best-effort
        logger.warning(
            "translate_to_cog: rasterio COG encode failed (%s: %s); returning flat bytes",
            type(exc).__name__,
            exc,
        )
        with open(input_path, "rb") as f:
            return f.read()
    finally:
        if out_tmp is not None:
            try:
                os.unlink(out_tmp)
            except OSError:
                pass
