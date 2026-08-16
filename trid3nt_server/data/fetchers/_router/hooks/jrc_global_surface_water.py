"""``fetch_jrc_global_surface_water`` colormap hook (raster-modes wave).

The ONE irreducible per-source step for the jrc-gsw fold: a per-band GDAL color
table that is a PURE function of the ``band`` param (never reads the fetched array,
does no I/O). The ``stac_continuous_mosaic`` serializer bakes the returned
``{value:(r,g,b,a)}`` table into the emitted uint8 COG's band-1 palette so
``publish_layer`` colorizes directly from the embedded ramp, independent of the
single-band TiTiler style registry. The ramp math is carried VERBATIM from the twin
``_band_colormap`` (white->deep-blue occurrence/recurrence, a 12-step seasonality
ramp, a red->white->blue diverging change ramp).
"""

from __future__ import annotations

from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..errors import router_input_error
from . import register_hook


def _blue_ramp_colormap(nodata: int, vmax: int) -> dict[int, tuple[int, int, int, int]]:
    """White(low)->deep-blue(high) ramp over [1..vmax]; ``nodata`` transparent."""
    cmap: dict[int, tuple[int, int, int, int]] = {}
    for v in range(256):
        if v == nodata or v > vmax:
            cmap[v] = (0, 0, 0, 0)
            continue
        t = max(0.0, min(1.0, v / float(vmax)))
        r = int(round(247 - t * (247 - 8)))
        g = int(round(251 - t * (251 - 48)))
        b = int(round(255 - t * (255 - 107)))
        cmap[v] = (r, g, b, 255)
    return cmap


def _seasonality_colormap() -> dict[int, tuple[int, int, int, int]]:
    """12-step blue ramp over months 1..12; 0 (no water) transparent."""
    cmap: dict[int, tuple[int, int, int, int]] = {}
    for v in range(256):
        if v == 0 or v > 12:
            cmap[v] = (0, 0, 0, 0)
            continue
        t = (v - 1) / 11.0
        r = int(round(229 - t * (229 - 8)))
        g = int(round(245 - t * (245 - 48)))
        b = int(round(249 - t * (249 - 107)))
        cmap[v] = (r, g, b, 255)
    return cmap


def _change_colormap() -> dict[int, tuple[int, int, int, int]]:
    """Diverging red(loss)->white(no change=100)->blue(gain) over [0..200]."""
    cmap: dict[int, tuple[int, int, int, int]] = {}
    for v in range(256):
        if v > 200:
            cmap[v] = (0, 0, 0, 0)
            continue
        if v <= 100:
            t = v / 100.0
            r = int(round(178 + t * (247 - 178)))
            g = int(round(24 + t * (247 - 24)))
            b = int(round(43 + t * (247 - 43)))
        else:
            t = (v - 100) / 100.0
            r = int(round(247 - t * (247 - 33)))
            g = int(round(247 - t * (247 - 102)))
            b = int(round(247 - t * (247 - 172)))
        cmap[v] = (r, g, b, 255)
    return cmap


@register_hook("jrc_global_surface_water.colormap")
def colormap(spec: SourceSpec, params: dict[str, Any]) -> dict[int, tuple[int, int, int, int]]:
    """Return the per-band GDAL color table for the resolved ``band`` param.

    The band was validated by the router's enum param gate pre-network, so an
    unknown value raises the source's typed BAND_INVALID input error (defensive:
    the router never reaches here with an out-of-set band).
    """
    band = params.get("band")
    if band in ("occurrence", "recurrence"):
        return _blue_ramp_colormap(nodata=0, vmax=100)
    if band == "seasonality":
        return _seasonality_colormap()
    if band == "change":
        return _change_colormap()
    raise router_input_error(
        spec.error_code_prefix, f"no colormap for band {band!r}",
        spec.input_error_suffix,
    )
