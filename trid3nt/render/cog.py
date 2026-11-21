"""An array as COG bytes: the one array-to-COG writer, float32 DEFLATE with a nodata, tiled, CRS-tagged."""

from __future__ import annotations

import os
import tempfile
from typing import Any

__all__ = ["array_to_cog_bytes"]


def array_to_cog_bytes(
    array: Any,
    transform: Any,
    crs: Any,
    *,
    nodata: float | None = float("nan"),
    dtype: str = "float32",
    colormap: dict | None = None,
    colorinterp: str | None = None,
) -> bytes:
    """Serialize a 2D or multi-band 3D array to COG bytes. North-up is the caller's
    (the transform already carries a negative y-step), and the CRS is re-asserted
    after the astype so the geographic-correctness gate never sees a dropped CRS."""

    # ``colormap`` bakes a palette into band 1 so publish colorizes with no rescale;
    # ``colorinterp="rgba"`` with ``nodata=None`` carries transparency in alpha, not a sentinel.
    import numpy as np
    import rasterio

    arr = np.asarray(array, dtype=dtype)
    if arr.ndim == 2:
        arr = arr[np.newaxis, :, :]
    if arr.ndim != 3:
        raise ValueError(f"array must be 2D or multi-band 3D; got shape {arr.shape}")
    count, height, width = arr.shape

    def _write_palette(dst: Any) -> None:
        if colormap is not None:
            dst.write_colormap(1, colormap)
        try:
            from rasterio.enums import ColorInterp

            interp = list(dst.colorinterp)
            if colormap is not None:
                interp[0] = ColorInterp.palette
            if colorinterp == "rgba" and count == 4:
                interp[:4] = [ColorInterp.red, ColorInterp.green,
                              ColorInterp.blue, ColorInterp.alpha]
            dst.colorinterp = tuple(interp)
        except Exception:  # noqa: BLE001 -- colorinterp set is best-effort
            pass

    out_fd, out_path = tempfile.mkstemp(suffix=".tif", prefix="trid3nt_router_cog_")
    os.close(out_fd)
    try:
        base_profile = {
            "dtype": dtype,
            "count": count,
            "height": height,
            "width": width,
            "crs": crs,
            "transform": transform,
            "compress": "DEFLATE",
        }
        if nodata is not None:
            base_profile["nodata"] = nodata
        try:
            with rasterio.open(
                out_path, "w", driver="COG", blocksize=256, **base_profile
            ) as dst:
                dst.write(arr)
                _write_palette(dst)
        except Exception:  # noqa: BLE001 -- COG driver may be unavailable; GTiff tiled
            with rasterio.open(
                out_path,
                "w",
                driver="GTiff",
                tiled=True,
                blockxsize=256,
                blockysize=256,
                **base_profile,
            ) as dst:
                dst.write(arr)
                _write_palette(dst)
        with open(out_path, "rb") as f:
            return f.read()
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass
