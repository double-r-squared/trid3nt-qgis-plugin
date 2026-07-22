"""Precompute per-COG band statistics so the agent skips the COG re-download.

The agent's ``publish_layer._resolve_titiler_style_params`` re-downloads each COG
to (a) probe RGBA/multiband (``_is_rgba_or_multiband``), (b) probe categorical
palettes, and (c) compute the 2nd/98th-percentile generic-fallback rescale
(``_band1_percentile_rescale``). For the known continuous presets (flood/wave)
the agent registry already gives a deterministic rescale, so ``band_stats`` is
belt-and-suspenders for those — but it makes the GENERIC-fallback + the
categorical / RGBA passthrough guards COG-read-free for any worker raster.

Pure rasterio/numpy off a LOCAL COG path (the worker already wrote it locally) —
GPL-free, agent-import-free.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

LOG = logging.getLogger("trid3nt.worker.raster_postprocess.band_stats")


def compute_band_stats(cog_path: Path) -> dict[str, Any]:
    """Read a local COG and return the band_stats dict the manifest carries.

    Shape (matches the spike manifest contract):
        {"is_categorical": bool, "is_rgba": bool,
         "p2": float|None, "p98": float|None,
         "min": float|None, "max": float|None}

    * ``is_rgba`` mirrors the agent's ``_is_rgba_or_multiband`` (band count >= 3
      OR any R/G/B/A colorinterp) -> the agent returns "" (TiTiler passthrough).
    * ``is_categorical`` is True when band 1 carries a color table (palette) ->
      the agent passes it through untouched.
    * ``p2``/``p98`` mirror the agent's ``_band1_percentile_rescale`` (2nd/98th
      percentile of finite band-1 values, NaN/nodata masked, zero-width widened),
      so the agent's generic fallback never re-reads the COG.

    Best-effort: a read failure returns all-None / False so the agent simply
    falls back to its own COG read (no regression).
    """
    out: dict[str, Any] = {
        "is_categorical": False,
        "is_rgba": False,
        "p2": None,
        "p98": None,
        "min": None,
        "max": None,
    }
    try:
        import numpy as np  # type: ignore
        import rasterio  # type: ignore
        from rasterio.enums import ColorInterp  # type: ignore
    except Exception as exc:  # noqa: BLE001
        LOG.debug("band_stats deps unavailable (%s: %s)", type(exc).__name__, exc)
        return out

    try:
        with rasterio.open(str(cog_path)) as src:
            # RGBA / multiband probe (agent _is_rgba_or_multiband parity).
            rgba = {
                ColorInterp.red,
                ColorInterp.green,
                ColorInterp.blue,
                ColorInterp.alpha,
            }
            out["is_rgba"] = bool(
                src.count >= 3 or any(ci in rgba for ci in src.colorinterp)
            )

            # Categorical palette probe (band-1 color table present).
            try:
                out["is_categorical"] = src.colormap(1) is not None
            except Exception:  # noqa: BLE001 — no palette -> not categorical
                out["is_categorical"] = False

            if out["is_rgba"]:
                # An RGBA raster has no single-band scalar rescale to compute.
                return out

            band = src.read(1, masked=True)
            arr = np.ma.filled(band.astype("float64"), np.nan)
            finite = arr[np.isfinite(arr)]
            if finite.size == 0:
                return out
            lo = float(np.percentile(finite, 2))
            hi = float(np.percentile(finite, 98))
            if hi <= lo:
                pad = max(abs(lo) * 0.01, 1e-6)
                lo, hi = lo - pad, hi + pad
            out["p2"] = lo
            out["p98"] = hi
            out["min"] = float(np.min(finite))
            out["max"] = float(np.max(finite))
    except Exception as exc:  # noqa: BLE001
        LOG.debug("band_stats read failed (%s: %s)", type(exc).__name__, exc)
    return out
