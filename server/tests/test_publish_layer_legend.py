"""publish_layer -- DATA-DRIVEN LEGEND KEY (the colormap KEY comes FROM THE DATA).

NATE's principle: when we fetch a map the gradient/key must MEAN something, not be
a retroactive hardcoded guess. ``publish_layer`` now EMITS a ``LegendKey`` derived
DIRECTLY from the resolved TiTiler ``style_params`` -- the SAME
``&rescale=lo,hi&colormap_name=name`` it resolves for the raster -- so the legend
range and the painted-raster range AGREE by construction. TiTiler exit: the key
is stashed keyed by the returned raw ``s3://`` COG uri (the envelope uri) so the
pipeline emitter can lift it onto the ``ProjectLayerSummary`` (the atomic tool
returns a bare URI string, not a typed ``LayerURI``); the register-only manifest
seam (``register_published_manifest``) stashes by the same raw ``cog_uri`` (its
coverage lives in ``test_publish_manifest_register_only_phase4.py``).

Coverage:
  (a) a CONTINUOUS raster publish carries a legend with the REAL vmin/vmax +
      colormap (the percentile-fallback range for an unpinned preset; the pinned
      semantic range for a registry preset -- whichever the raster RENDERS with);
  (b) the legend range EQUALS the URL rescale range (no second, drifting read);
  (c) a CATEGORICAL (paletted/NLCD) raster carries kind="categorical" classes
      from the embedded GDAL color table (transparent slots dropped);
  (d) RGBA / terrain passthrough layers carry NO legend (None) -> legacy render;
  (e) the legend round-trips through the URI stash by display uri.

No Cloud Run / GCS / TiTiler network I/O -- real GeoTIFF bytes built by rasterio.
"""

from __future__ import annotations

import numpy as np
import pytest
import rasterio
from rasterio.io import MemoryFile

from grace2_agent.tools import publish_layer as pl
from grace2_agent.tools.publish_layer import (
    _categorical_legend_from_colormap,
    _parse_style_params,
    legend_for_published_layer,
    pop_legend_for_uri,
    publish_layer,
)

MOD = pl


# --------------------------------------------------------------------------- #
# GeoTIFF byte builders (mirror the F51 resolver test fixtures)
# --------------------------------------------------------------------------- #


def _continuous_geotiff_bytes(lo: float = 0.0, hi: float = 50.0, size: int = 64) -> bytes:
    rng = np.linspace(lo, hi, size * size, dtype="float32").reshape(size, size)
    transform = rasterio.transform.from_origin(0, size, 1, 1)
    with MemoryFile() as mem:
        with mem.open(
            driver="GTiff",
            height=size,
            width=size,
            count=1,
            dtype="float32",
            crs="EPSG:4326",
            transform=transform,
            nodata=float("nan"),
        ) as dst:
            dst.write(rng, 1)
        return mem.read()


_NLCD_COLORMAP = {
    0: (0, 0, 0, 0),  # transparent / nodata slot -> dropped from the legend
    11: (72, 109, 162, 255),
    21: (222, 197, 197, 255),
    41: (56, 129, 78, 255),
    81: (220, 217, 57, 255),
    90: (186, 217, 235, 255),
    255: (0, 0, 0, 0),  # transparent / nodata slot -> dropped from the legend
}


def _paletted_geotiff_bytes(size: int = 64) -> bytes:
    classes = np.array([11, 21, 41, 81, 90], dtype="uint8")
    data = classes[np.random.randint(0, len(classes), size=(size, size))]
    transform = rasterio.transform.from_origin(0, size, 1, 1)
    with MemoryFile() as mem:
        with mem.open(
            driver="GTiff",
            height=size,
            width=size,
            count=1,
            dtype="uint8",
            crs="EPSG:4326",
            transform=transform,
            nodata=255,
        ) as dst:
            dst.write(data, 1)
            dst.write_colormap(1, _NLCD_COLORMAP)
        return mem.read()


def _rgba_geotiff_bytes(bands: int = 4, size: int = 64) -> bytes:
    from rasterio.enums import ColorInterp

    data = np.random.randint(0, 256, size=(bands, size, size), dtype="uint8")
    transform = rasterio.transform.from_origin(0, size, 1, 1)
    interps = [ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.alpha]
    with MemoryFile() as mem:
        with mem.open(
            driver="GTiff",
            height=size,
            width=size,
            count=bands,
            dtype="uint8",
            crs="EPSG:4326",
            transform=transform,
            photometric="RGB",
        ) as dst:
            for b in range(bands):
                dst.write(data[b], b + 1)
            dst.colorinterp = tuple(interps[:bands])
        return mem.read()


# --------------------------------------------------------------------------- #
# _parse_style_params -- the inverse of the resolver's URL strings
# --------------------------------------------------------------------------- #


def test_parse_style_params_continuous() -> None:
    assert _parse_style_params("&rescale=0,3&colormap_name=ylgnbu") == (0.0, 3.0, "ylgnbu")


def test_parse_style_params_signed_range() -> None:
    assert _parse_style_params("&rescale=-25,25&colormap_name=rdbu") == (-25.0, 25.0, "rdbu")


def test_parse_style_params_scientific_notation() -> None:
    vmin, vmax, cmap = _parse_style_params("&rescale=1.2e-09,4.5e-06&colormap_name=viridis")
    assert vmin == pytest.approx(1.2e-09) and vmax == pytest.approx(4.5e-06)
    assert cmap == "viridis"


def test_parse_style_params_empty() -> None:
    assert _parse_style_params("") == (None, None, None)


# --------------------------------------------------------------------------- #
# legend_for_published_layer -- continuous
# --------------------------------------------------------------------------- #


def test_continuous_legend_from_registry_preset() -> None:
    """A pinned registry preset's legend uses the SAME range the URL renders with
    (the semantic fixed range), so legend and raster agree byte-for-byte."""
    legend = legend_for_published_layer(
        "continuous_flood_depth", "s3://b/flood.tif", "&rescale=0,3&colormap_name=ylgnbu"
    )
    assert legend is not None
    assert legend.kind == "continuous"
    assert legend.colormap == "ylgnbu"
    assert legend.vmin == 0.0
    assert legend.vmax == 3.0
    assert legend.label == "Flood depth"


def test_continuous_legend_uses_real_percentile_range() -> None:
    """An UNPINNED preset renders with the p2/p98 percentile rescale; the legend
    carries the IDENTICAL real range (no retroactive hardcoded guess)."""
    style_params = MOD._band1_percentile_rescale(_continuous_geotiff_bytes(0.0, 30.0))
    assert style_params is not None  # real range read off the COG
    legend = legend_for_published_layer("gridmet_vs_unknown", "s3://b/x.tif", style_params)
    assert legend is not None and legend.kind == "continuous"
    parsed_lo, parsed_hi, parsed_cmap = _parse_style_params(style_params)
    # The legend range is the SAME numbers as the rescale URL -> they AGREE.
    assert legend.vmin == parsed_lo
    assert legend.vmax == parsed_hi
    assert legend.colormap == parsed_cmap
    # And it is a REAL data range (not the 0,1 safe default), spanning the data.
    assert legend.vmin >= 0.0 and legend.vmax <= 30.0 and legend.vmax > legend.vmin


# --------------------------------------------------------------------------- #
# legend_for_published_layer -- categorical (embedded GDAL color table)
# --------------------------------------------------------------------------- #


def test_categorical_legend_from_color_table() -> None:
    """A paletted COG (empty style_params) yields a categorical legend, one swatch
    per OPAQUE class -- transparent nodata slots dropped."""
    legend = legend_for_published_layer(
        "categorical_landcover",
        "s3://b/nlcd.tif",
        "",  # paletted rasters publish with NO rescale (palette wins)
        raster_bytes=_paletted_geotiff_bytes(),
    )
    assert legend is not None
    assert legend.kind == "categorical"
    assert legend.classes is not None
    values = {c.value for c in legend.classes}
    assert values == {11, 21, 41, 81, 90}  # the 5 land-cover classes
    assert 0 not in values and 255 not in values  # transparent slots dropped
    for c in legend.classes:
        assert c.color.startswith("#") and len(c.color) == 7
        assert c.label == str(c.value)


def test_categorical_legend_helper_drops_transparent_and_orders() -> None:
    cmap = {41: (56, 129, 78, 255), 11: (72, 109, 162, 255), 0: (0, 0, 0, 0)}
    legend = _categorical_legend_from_colormap(cmap, label="Land cover")
    assert legend is not None and legend.kind == "categorical"
    # ordered by class index, transparent 0 dropped.
    assert [c.value for c in legend.classes] == [11, 41]
    assert legend.label == "Land cover"


def test_categorical_legend_none_when_all_transparent() -> None:
    assert _categorical_legend_from_colormap({0: (0, 0, 0, 0), 255: (1, 1, 1, 0)}) is None


# --------------------------------------------------------------------------- #
# legend_for_published_layer -- passthrough (NO legend = legacy render)
# --------------------------------------------------------------------------- #


def test_rgba_passthrough_has_no_legend() -> None:
    """An RGBA composite publishes with empty style_params + no color table; there
    is no meaningful key -> None -> the web legacy path renders it as before."""
    legend = legend_for_published_layer(
        "colored_relief", "s3://b/relief.tif", "", raster_bytes=_rgba_geotiff_bytes()
    )
    assert legend is None


def test_legend_fail_open_returns_none_on_unreadable_bytes() -> None:
    legend = legend_for_published_layer(
        "categorical_landcover", "s3://b/junk.tif", "", raster_bytes=b"not-a-geotiff"
    )
    assert legend is None


# --------------------------------------------------------------------------- #
# URI stash round-trip + end-to-end s3 publish carries the legend
# --------------------------------------------------------------------------- #


# NOTE (TiTiler exit): ``build_titiler_tile_url`` - the legacy register-only
# tile-template mint + its template-keyed legend stash - was DELETED once
# ``register_published_manifest`` swapped to stashing by the raw ``cog_uri``.
# That seam's legend coverage lives in
# ``test_publish_manifest_register_only_phase4.py``.


def _s3_titiler(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the AWS s3 publish branch (storage_scheme == 's3').

    TiTiler exit: no GRACE2_TILE_SERVER_BASE - publish_layer emits the raw
    s3:// COG uri and the legend stash is keyed by that uri.
    """
    from grace2_agent.tools import cache as cache_mod

    monkeypatch.setattr(cache_mod, "storage_scheme", lambda: "s3")


def test_publish_continuous_raster_stashes_legend_by_s3_uri(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a continuous raster publish returns the raw s3:// COG uri AND
    stashes a continuous legend keyed by that uri, with the REAL percentile
    range that EQUALS the resolved rescale (legend == render)."""
    _s3_titiler(monkeypatch)
    cog_bytes = _continuous_geotiff_bytes(0.0, 40.0)
    monkeypatch.setattr(MOD, "_read_raster_bytes", lambda uri: cog_bytes)
    # Don't rewrite/copy COGs in the test (overview check fails open to the uri).
    monkeypatch.setattr(MOD, "_ensure_raster_has_overviews", lambda uri: uri)

    out = publish_layer(
        layer_uri="s3://bucket/runs/somerun/x.tif",
        layer_id="layer-cont-1",
        style_preset="gridmet_vs_unknown",
    )
    # The envelope uri slot is the raw s3 COG (no tile template).
    assert out == "s3://bucket/runs/somerun/x.tif"
    # The stashed legend uses the IDENTICAL range + colormap the resolver
    # computed (the rescale/colormap now ride ONLY the legend, not a URL).
    legend = pop_legend_for_uri(out)
    assert legend is not None and legend.kind == "continuous"
    style_params = MOD._band1_percentile_rescale(cog_bytes)
    assert style_params is not None
    url_lo, url_hi, url_cmap = _parse_style_params(style_params)
    assert legend.vmin == url_lo and legend.vmax == url_hi and legend.colormap == url_cmap
    assert legend.vmax > legend.vmin  # real, non-degenerate range


def test_publish_paletted_raster_stashes_categorical_legend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A paletted (NLCD) raster publishes with empty style_params (palette wins)
    and stashes a categorical legend built from the embedded GDAL table, keyed
    by the returned s3 uri."""
    _s3_titiler(monkeypatch)
    monkeypatch.setattr(MOD, "_read_raster_bytes", lambda uri: _paletted_geotiff_bytes())
    monkeypatch.setattr(MOD, "_ensure_raster_has_overviews", lambda uri: uri)

    out = publish_layer(
        layer_uri="s3://bucket/runs/somerun/nlcd.tif",
        layer_id="layer-nlcd-1",
        style_preset="categorical_landcover",
    )
    assert out == "s3://bucket/runs/somerun/nlcd.tif"
    legend = pop_legend_for_uri(out)
    assert legend is not None
    assert legend.kind == "categorical"
    assert {c.value for c in legend.classes} == {11, 21, 41, 81, 90}
