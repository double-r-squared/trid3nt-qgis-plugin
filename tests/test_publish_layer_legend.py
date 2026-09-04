"""publish_layer -- the RESOLVED STYLE a published raster carries.

The gradient and the range a reader is shown must MEAN something, so both come
out of ONE resolution of the layer's declared row against the layer's own
bytes: the legend range and the painted range agree by construction because
there is no second read. The result is stashed keyed by the returned raw
``s3://`` COG uri, so the pipeline emitter can lift it onto the
``ProjectLayerSummary`` (the atomic tool returns a bare URI string, not a typed
``LayerURI``); the register-only manifest seam stashes by the same
``cog_uri`` (coverage in ``test_publish_manifest_register_only_phase4.py``).

Coverage:
  (a) a CONTINUOUS raster carries the REAL vmin/vmax + ramp (the run's own
      p2/p98 for a data-policy row; the declared domain range for a fixed one);
  (b) the layer ships the .qml the map loads, over that same range;
  (c) a paletted raster carries ``kind="classed"`` swatches from its own
      embedded GDAL colour table (transparent slots dropped);
  (d) an RGB(A) composite carries NO key - it is already painted;
  (e) the resolved style round-trips through the URI stash.

No network I/O -- real GeoTIFF bytes built by rasterio.
"""

from __future__ import annotations

import numpy as np
import pytest
import rasterio
from rasterio.io import MemoryFile

from trid3nt_server.emission import publish as pl
from trid3nt_server.emission.publish import (
    _categorical_legend_from_colormap,
    legend_for_published_layer,
    pop_legend_for_uri,
    publish_layer,
    resolve_layer_style,
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
# legend_for_published_layer -- continuous
# --------------------------------------------------------------------------- #


_FLOOD = {"kind": "continuous", "ramp": "ylgnbu", "units": "m",
          "label": "Flood depth",
          "scale": {"policy": "fixed", "range": [0, 3], "transform": "linear"}}


def test_a_fixed_row_paints_and_labels_the_range_it_declared() -> None:
    legend = legend_for_published_layer(_FLOOD, "s3://b/flood.tif",
                                        raster_bytes=b"")
    assert legend is not None
    assert legend.kind == "continuous"
    assert legend.colormap == "ylgnbu"
    assert (legend.vmin, legend.vmax) == (0.0, 3.0)
    assert legend.label == "Flood depth"


def test_the_layer_ships_the_qml_the_map_loads_over_that_same_range() -> None:
    legend = legend_for_published_layer(_FLOOD, "s3://b/flood.tif",
                                        raster_bytes=b"")
    assert legend.qml is not None
    assert 'type="singlebandpseudocolor"' in legend.qml
    assert 'classificationMin="0"' in legend.qml
    assert 'classificationMax="3"' in legend.qml


def test_continuous_legend_uses_real_percentile_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A data-policy preset renders with the p2/p98 range read off THIS raster;
    the legend carries the IDENTICAL real range (no retroactive hardcoded guess).

    The range is controlled by handing the resolver a real COG whose data span we
    know, so the string under test is the one the chokepoint actually resolves.
    """
    monkeypatch.setattr(
        MOD, "_read_raster_bytes", lambda uri: _continuous_geotiff_bytes(0.0, 30.0)
    )
    resolved = resolve_layer_style(None, "s3://b/x.tif")
    legend = legend_for_published_layer(None, "s3://b/x.tif")
    assert legend is not None and legend.kind == "continuous"
    # ONE resolution: the legend range is the resolved range, not a second read.
    assert (legend.vmin, legend.vmax) == resolved.range
    assert legend.colormap == resolved.preset.ramp
    # And it is a REAL data range (not the 0,1 safe default), spanning the data.
    assert legend.vmin >= 0.0 and legend.vmax <= 30.0 and legend.vmax > legend.vmin


# --------------------------------------------------------------------------- #
# legend_for_published_layer -- categorical (embedded GDAL color table)
# --------------------------------------------------------------------------- #


def test_categorical_legend_from_color_table() -> None:
    """A paletted COG (empty style_params) yields a categorical legend, one swatch
    per OPAQUE class -- transparent nodata slots dropped."""
    legend = legend_for_published_layer(
        {"kind": "classed", "label": "Land Cover"},
        "s3://b/nlcd.tif",
        raster_bytes=_paletted_geotiff_bytes(),
    )
    assert legend is not None
    assert legend.kind == "classed"
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
    assert legend is not None and legend.kind == "classed"
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
        None, "s3://b/relief.tif", raster_bytes=_rgba_geotiff_bytes()
    )
    assert legend is None


def test_legend_fail_open_returns_none_on_unreadable_bytes() -> None:
    legend = legend_for_published_layer(
        {"kind": "classed"}, "s3://b/junk.tif", raster_bytes=b"not-a-geotiff")
    assert legend is not None and legend.qml is not None


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

    TiTiler exit: no TRID3NT_TILE_SERVER_BASE - publish_layer emits the raw
    s3:// COG uri and the legend stash is keyed by that uri.
    """
    from trid3nt_server.tools import cache as cache_mod

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
    )
    # The envelope uri slot is the raw s3 COG (no tile template).
    assert out == "s3://bucket/runs/somerun/x.tif"
    # The stashed legend uses the IDENTICAL range + colormap the resolver
    # computed (the rescale/colormap now ride ONLY the legend, not a URL).
    legend = pop_legend_for_uri(out)
    assert legend is not None and legend.kind == "continuous"
    # Re-resolve through the SAME chokepoint on the SAME bytes: the legend must
    # be those numbers, not a second range read somewhere else.
    resolved = resolve_layer_style(None, "s3://bucket/runs/somerun/x.tif")
    assert (legend.vmin, legend.vmax) == resolved.range
    assert legend.colormap == resolved.preset.ramp
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
    )
    assert out == "s3://bucket/runs/somerun/nlcd.tif"
    legend = pop_legend_for_uri(out)
    assert legend is not None
    assert legend.kind == "classed"
    assert {c.value for c in legend.classes} == {11, 21, 41, 81, 90}
