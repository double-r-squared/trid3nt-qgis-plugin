"""jrc_global_surface_water: the per-band colormap hook.

A per-band GDAL color table that is a PURE function of the ``band`` param: it never
reads the fetched array and does no I/O. The mosaic serializer bakes it into the emitted
COG's band-1 palette, so the layer colorizes with no style-registry row."""

from __future__ import annotations

from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..._router.errors import router_input_error
from ..._router.hooks import register_hook


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
    """The per-band GDAL color table for the resolved ``band``. The router's enum gate
    already validated it pre-network, so the out-of-set raise here is defence in
    depth."""
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
