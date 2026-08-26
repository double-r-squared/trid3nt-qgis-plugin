"""The render chokepoint: ``_resolve_qgis_style_params``.

Two separable things are pinned here, and only these two.

FIRST, the three RASTER GUARDS that live in the chokepoint because they are
facts about the FILE, not about the style. Each one is a way a single-band
rescale would CORRUPT an image that is already coloured:

  1. an embedded band-1 GDAL colour table (NLCD land cover) - the palette wins;
  2. RGB(A) / >= 3 bands (coloured relief, a landcover + hillshade composite) -
     the baked colours render directly;
  3. a terrain-family preset or URI (dem / hillshade / relief / elevation) -
     grayscale terrain auto-scales and RGBA terrain renders as it is.

Each guard resolves to ``""`` and beats any preset that would otherwise rescale.

SECOND, that everything AFTER the guards is DELEGATED - the chokepoint makes no
style decision of its own. The contract (``trid3nt_contracts/styles.yaml``)
declares the preset and ``emission/styles.py`` resolves it, so a ``policy: data``
preset is scaled to THIS raster's own range, a ``policy: fixed`` preset ignores
the raster entirely, the colormap is whatever the contract declared, and an
unreadable raster degrades to the preset's declared fallback range and says so.
The contract's own self-consistency lives in ``test_style_contract.py``.

Real GeoTIFF bytes built by rasterio - no network I/O.
"""

from __future__ import annotations

import numpy as np
import pytest
import rasterio
from rasterio.io import MemoryFile

from trid3nt_server.emission import publish as pl
from trid3nt_server.emission import styles
from trid3nt_server.emission.publish import (
    _is_rgba_or_multiband,
    _is_terrain_token_preset,
    _parse_style_params,
    _resolve_qgis_style_params,
    publish_layer,
)

# Patch target = the imported module OBJECT (monkeypatch.setattr on an object,
# not a dotted string), so every helper resolves through the patched name.
MOD = pl

#: A data-policy preset: its contract range [0, 3] is the FALLBACK, used only
#: when the run's own band statistics cannot be read.
DATA_PRESET = "continuous_flood_depth"
DATA_FALLBACK = "&rescale=0,3&colormap_name=ylgnbu"

#: A domain-standard BOUNDED preset: PGA is 0-1 g by definition, so it is
#: ``policy: fixed`` and never looks at the raster.
FIXED_PRESET = "continuous_seismic_pga"
FIXED_PARAMS = "&rescale=0,1&colormap_name=magma"


def _rescale_of(style_params: str) -> tuple[float, float]:
    lo, hi, _cmap = _parse_style_params(style_params)
    assert lo is not None and hi is not None, style_params
    return lo, hi


# --------------------------------------------------------------------------- #
# GeoTIFF byte builders
# --------------------------------------------------------------------------- #


def _continuous_geotiff_bytes(lo: float = 0.0, hi: float = 50.0, size: int = 64) -> bytes:
    """A georeferenced single-band float32 GeoTIFF, values spread over [lo, hi]."""
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


def _all_nan_geotiff_bytes(size: int = 32) -> bytes:
    """A float32 GeoTIFF whose band-1 is entirely NaN (no finite values)."""
    data = np.full((size, size), np.nan, dtype="float32")
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
            dst.write(data, 1)
        return mem.read()


def _single_value_geotiff_bytes(value: float = 7.0, size: int = 32) -> bytes:
    """A float32 GeoTIFF whose finite band-1 values are all identical."""
    data = np.full((size, size), value, dtype="float32")
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
        ) as dst:
            dst.write(data, 1)
        return mem.read()


_NLCD_COLORMAP = {
    0: (0, 0, 0, 0),
    11: (72, 109, 162, 255),
    21: (222, 197, 197, 255),
    41: (56, 129, 78, 255),
    81: (220, 217, 57, 255),
    90: (186, 217, 235, 255),
    255: (0, 0, 0, 0),
}


def _paletted_geotiff_bytes(size: int = 64) -> bytes:
    """A flat single-band uint8 GeoTIFF WITH an embedded color table (NLCD-like)."""
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
    """A georeferenced multiband uint8 GeoTIFF with RGB(A) color interpretation.

    Models the baked landcover + hillshade composite and the coloured-relief
    product - already colorized, must render DIRECTLY.
    """
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
# GUARD 1 - an embedded band-1 colour table wins over ANY preset
# --------------------------------------------------------------------------- #


def test_paletted_cog_emits_no_rescale(monkeypatch: pytest.MonkeyPatch) -> None:
    """A COG with an embedded band-1 colour table gets EMPTY style_params so the
    renderer colorizes from the palette (NLCD must not be washed out)."""
    monkeypatch.setattr(MOD, "_read_raster_bytes", lambda uri: _paletted_geotiff_bytes())
    assert _resolve_qgis_style_params("categorical_landcover", "s3://b/nlcd.tif") == ""


def test_paletted_cog_overrides_a_preset_that_would_rescale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The palette guard runs FIRST - it beats a contract preset with a range."""
    monkeypatch.setattr(MOD, "_read_raster_bytes", lambda uri: _paletted_geotiff_bytes())
    assert _resolve_qgis_style_params("precipitation_mm", "s3://b/weird.tif") == ""
    assert _resolve_qgis_style_params(DATA_PRESET, "s3://b/weird.tif") == ""


# --------------------------------------------------------------------------- #
# GUARD 2 - RGB(A) / multiband renders its own baked colours
# --------------------------------------------------------------------------- #


def test_is_rgba_or_multiband_detects_rgba() -> None:
    assert _is_rgba_or_multiband(_rgba_geotiff_bytes(bands=4)) is True
    assert _is_rgba_or_multiband(_rgba_geotiff_bytes(bands=3)) is True


def test_is_rgba_or_multiband_false_for_single_band() -> None:
    assert _is_rgba_or_multiband(_continuous_geotiff_bytes()) is False
    assert _is_rgba_or_multiband(None) is False
    assert _is_rgba_or_multiband(b"NOT A RASTER") is False


def test_rgba_cog_emits_no_rescale_even_with_a_rescaling_preset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A baked RGBA composite must render DIRECTLY - '' even when the preset
    would otherwise resolve to a rescale that corrupts the colours."""
    monkeypatch.setattr(MOD, "_read_raster_bytes", lambda uri: _rgba_geotiff_bytes(4))
    assert _resolve_qgis_style_params(
        "some_unknown_composite_preset", "s3://b/composite.tif") == ""
    assert _resolve_qgis_style_params(DATA_PRESET, "s3://b/composite.tif") == ""


def test_three_band_cog_emits_no_rescale(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(MOD, "_read_raster_bytes", lambda uri: _rgba_geotiff_bytes(3))
    assert _resolve_qgis_style_params("unknown_xyz", "s3://b/rgb.tif") == ""


def test_coloured_relief_composite_is_doubly_guarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A coloured-relief RGBA COG with a terrain preset trips BOTH the RGBA guard
    and the terrain-token guard -> ''."""
    monkeypatch.setattr(MOD, "_read_raster_bytes", lambda uri: _rgba_geotiff_bytes(4))
    assert _resolve_qgis_style_params("colored_relief", "s3://b/relief.tif") == ""


# --------------------------------------------------------------------------- #
# GUARD 3 - the terrain family renders without a colormap
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "preset",
    ["continuous_dem", "continuous_hillshade", "colored_relief", "terrain_rgba",
     "elevation"],
)
def test_terrain_token_presets_match(preset: str) -> None:
    assert _is_terrain_token_preset(preset, "s3://b/x.tif") is True


def test_slope_aspect_are_not_terrain_passthrough() -> None:
    """Slope and aspect carry real colormaps; dem/hillshade/relief/elevation stay
    grayscale passthrough."""
    assert _is_terrain_token_preset(
        "continuous_aspect", "s3://b/cache/static-30d/aspect/x.tif") is False
    assert _is_terrain_token_preset(
        "slope_angle_deg", "s3://b/cache/static-30d/slope/x.tif") is False
    assert _is_terrain_token_preset(
        "continuous_hillshade", "s3://b/cache/static-30d/hillshade/x.tif") is True


def test_non_terrain_preset_does_not_match() -> None:
    """A weather scalar preset must NOT trip the terrain passthrough."""
    assert _is_terrain_token_preset("hrrr_2m_temperature", "s3://b/t.tif") is False
    assert _is_terrain_token_preset("precipitation_mm", "s3://b/p.tif") is False
    # 'demo' must not match 'dem' (whole-token boundary).
    assert _is_terrain_token_preset("flood_demo", "s3://b/demo-flood.tif") is False


def test_terrain_token_matches_on_the_uri_when_the_preset_is_blank() -> None:
    assert _is_terrain_token_preset("", "s3://b/runs/hillshade-toutle.tif") is True


def test_dem_preset_emits_no_rescale(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        MOD, "_read_raster_bytes", lambda uri: _continuous_geotiff_bytes(100.0, 2500.0))
    assert _resolve_qgis_style_params("continuous_dem", "s3://b/dem.tif") == ""


def test_hillshade_preset_emits_no_rescale(monkeypatch: pytest.MonkeyPatch) -> None:
    """A viridis ramp painted on a grayscale hillshade was the regression."""
    monkeypatch.setattr(
        MOD, "_read_raster_bytes", lambda uri: _continuous_geotiff_bytes(0.0, 255.0))
    assert _resolve_qgis_style_params("continuous_hillshade", "s3://b/shade.tif") == ""


def test_slope_and_aspect_reach_their_contract_colormaps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past the terrain guard, slope/aspect resolve to their declared fixed scales."""
    monkeypatch.setattr(
        MOD, "_read_raster_bytes", lambda uri: _continuous_geotiff_bytes(0.0, 60.0))
    assert _resolve_qgis_style_params(
        "slope_angle_deg", "s3://b/cache/static-30d/slope/x.tif"
    ) == "&rescale=0,60&colormap_name=ylorrd"
    assert _resolve_qgis_style_params(
        "aspect_compass_deg", "s3://b/cache/static-30d/aspect/x.tif"
    ) == "&rescale=0,360&colormap_name=hsv"


# --------------------------------------------------------------------------- #
# PAST THE GUARDS - the chokepoint delegates, it does not decide
# --------------------------------------------------------------------------- #


def test_data_policy_preset_scales_to_this_rasters_own_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(a) A ``policy: data`` preset is scaled to the RUN's raster, not to the
    range the contract declares as its fallback."""
    monkeypatch.setattr(
        MOD, "_read_raster_bytes", lambda uri: _continuous_geotiff_bytes(20.0, 40.0))
    out = _resolve_qgis_style_params(DATA_PRESET, "s3://b/runs/depth.tif")
    assert out != DATA_FALLBACK, "the declared range is a fallback, not the answer"
    lo, hi = _rescale_of(out)
    assert 20.0 <= lo < hi <= 40.0
    assert out.endswith("&colormap_name=ylgnbu")


def test_data_policy_preset_falls_back_to_its_declared_range_when_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(b) No bytes -> the contract's declared range, and the legend ADMITS that
    the run's own values were unreadable rather than implying a real read."""
    monkeypatch.setattr(MOD, "_read_raster_bytes", lambda uri: None)
    assert _resolve_qgis_style_params(DATA_PRESET, "s3://b/runs/gone.tif") == DATA_FALLBACK

    resolved = styles.resolve_style(DATA_PRESET, read_range=styles.band_range_reader(None))
    assert resolved.source == styles.FALLBACK
    assert "unreadable" in resolved.legend_note()


def test_fixed_preset_does_not_vary_with_the_raster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(c) A domain-standard bounded preset is the SAME answer against two rasters
    with different ranges - the scale is a property of the quantity, not the run."""
    monkeypatch.setattr(
        MOD, "_read_raster_bytes", lambda uri: _continuous_geotiff_bytes(0.02, 0.09))
    quiet = _resolve_qgis_style_params(FIXED_PRESET, "s3://b/runs/pga-quiet.tif")
    monkeypatch.setattr(
        MOD, "_read_raster_bytes", lambda uri: _continuous_geotiff_bytes(0.4, 0.9))
    strong = _resolve_qgis_style_params(FIXED_PRESET, "s3://b/runs/pga-strong.tif")
    assert quiet == strong == FIXED_PARAMS


def test_all_nan_band_still_yields_a_usable_rescale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(d) An all-NaN band has no finite values to read -> the declared fallback,
    which is non-empty and non-degenerate."""
    monkeypatch.setattr(MOD, "_read_raster_bytes", lambda uri: _all_nan_geotiff_bytes())
    out = _resolve_qgis_style_params(DATA_PRESET, "s3://b/runs/nan.tif")
    assert out == DATA_FALLBACK
    lo, hi = _rescale_of(out)
    assert hi > lo


def test_single_value_band_is_widened_not_zero_width(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(d) Every finite value identical -> a zero-width range, which a renderer
    rejects, so the resolver widens it around the value."""
    monkeypatch.setattr(
        MOD, "_read_raster_bytes", lambda uri: _single_value_geotiff_bytes(7.0))
    out = _resolve_qgis_style_params(DATA_PRESET, "s3://b/runs/flat.tif")
    lo, hi = _rescale_of(out)
    assert hi > lo, "a single-value band must widen to a non-zero range"
    assert lo < 7.0 < hi


@pytest.mark.parametrize("payload", [b"NOT A RASTER", b"", None])
def test_unreadable_bytes_fall_back_rather_than_raise(
    payload: bytes | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(e) Junk / empty / absent bytes degrade to the declared range - a publish is
    never blocked on a band read."""
    monkeypatch.setattr(MOD, "_read_raster_bytes", lambda uri: payload)
    assert _resolve_qgis_style_params(DATA_PRESET, "s3://b/runs/junk.tif") == DATA_FALLBACK


def test_the_colormap_comes_from_the_contract_not_from_the_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(f) Reading the range off the raster must NOT also replace the declared
    colormap - a data-policy preset keeps its own hue."""
    monkeypatch.setattr(
        MOD, "_read_raster_bytes", lambda uri: _continuous_geotiff_bytes(0.0, 30.0))
    depth = _resolve_qgis_style_params(DATA_PRESET, "s3://b/runs/depth.tif")
    assert depth.endswith("&colormap_name=ylgnbu") and "viridis" not in depth
    plume = _resolve_qgis_style_params(
        "continuous_plume_concentration", "s3://b/runs/plume.tif")
    assert plume.endswith("&colormap_name=reds") and "viridis" not in plume


def test_a_classed_preset_paints_intervals_with_no_rescale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(g) A preset with a declared class table paints those intervals; there is no
    continuous range to rescale, and the raster is not read for one."""
    monkeypatch.setattr(
        MOD, "_read_raster_bytes", lambda uri: _continuous_geotiff_bytes(0.0, 900.0))
    out = _resolve_qgis_style_params("sediment_yield_t_ha_yr", "s3://b/runs/sed.tif")
    assert out.startswith("&colormap=")
    assert "rescale" not in out and "colormap_name" not in out


def test_an_unknown_preset_still_gets_a_non_empty_scale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A preset the contract does not declare falls to the neutral default: the
    raster's OWN range under a single-hue colormap. Never empty - empty hands the
    renderer a per-tile autoscale, which is a different meaning per tile."""
    monkeypatch.setattr(
        MOD, "_read_raster_bytes", lambda uri: _continuous_geotiff_bytes(0.0, 30.0))
    out = _resolve_qgis_style_params("gridmet_vs_unknown", "s3://b/runs/x.tif")
    assert out.endswith("&colormap_name=viridis")
    lo, hi = _rescale_of(out)
    assert 0.0 <= lo < hi <= 30.0


# --------------------------------------------------------------------------- #
# publish_layer end-to-end - the resolved style rides the LEGEND stash keyed by
# the returned raw s3:// uri.
# --------------------------------------------------------------------------- #


@pytest.fixture()
def _s3_publish(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRID3NT_STORAGE_BACKEND", "s3")


def test_publish_layer_unknown_preset_stashes_a_non_empty_style(
    _s3_publish: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: an unknown continuous preset resolves a non-empty
    rescale + colormap (never grayscale) and stashes it as the legend keyed by
    the returned raw s3 uri."""
    good = _continuous_geotiff_bytes(0.0, 25.0)
    # Already has overviews? Force overview-pass so the URI is unchanged, and
    # serve the same bytes for the style probe.
    monkeypatch.setattr(MOD, "_read_raster_bytes", lambda uri: good)
    monkeypatch.setattr(MOD, "_raster_has_overviews", lambda b: True)

    out = publish_layer(
        layer_uri="s3://bucket/runs/windspeed.tif",
        layer_id="wind",
        style_preset="gridmet_vs_unknown",
    )
    assert out == "s3://bucket/runs/windspeed.tif"
    legend = pl.pop_legend_for_uri(out)
    assert legend is not None and legend.kind == "continuous"
    assert legend.colormap == "viridis"
    assert legend.vmin is not None and legend.vmax is not None
    assert 0.0 <= legend.vmin < legend.vmax <= 25.0  # the raster's own range


def test_publish_layer_fixed_preset_stashes_the_domain_range(
    _s3_publish: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: a fixed-scale preset lands its declared domain range in the
    stashed legend, whatever the raster happens to span."""
    monkeypatch.setattr(
        MOD, "_read_raster_bytes", lambda uri: _continuous_geotiff_bytes(260.0, 310.0))
    monkeypatch.setattr(MOD, "_raster_has_overviews", lambda b: True)

    out = publish_layer(
        layer_uri="s3://bucket/runs/t2m.tif",
        layer_id="temp",
        style_preset="hrrr_2m_temperature",
    )
    assert out == "s3://bucket/runs/t2m.tif"
    legend = pl.pop_legend_for_uri(out)
    assert legend is not None and legend.kind == "continuous"
    assert (legend.colormap, legend.vmin, legend.vmax) == ("rdylbu_r", 250.0, 320.0)


def test_publish_layer_paletted_gets_a_categorical_legend(
    _s3_publish: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: a paletted NLCD COG resolves NO rescale (palette wins) and the
    stashed legend is the categorical key from the embedded GDAL table."""
    nlcd = _paletted_geotiff_bytes()
    monkeypatch.setattr(MOD, "_read_raster_bytes", lambda uri: nlcd)
    monkeypatch.setattr(MOD, "_raster_has_overviews", lambda b: True)

    out = publish_layer(
        layer_uri="s3://bucket/runs/nlcd.tif",
        layer_id="landcover",
        style_preset="categorical_landcover",
    )
    assert out == "s3://bucket/runs/nlcd.tif"
    legend = pl.pop_legend_for_uri(out)
    assert legend is not None and legend.kind == "categorical"
    assert {c.value for c in legend.classes} == {11, 21, 41, 81, 90}


def test_publish_layer_rgba_composite_has_no_legend(
    _s3_publish: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: a baked landcover + hillshade RGBA composite resolves NO
    rescale/colormap, so no legend is stashed and the plugin renders the baked
    colours directly."""
    monkeypatch.setattr(MOD, "_read_raster_bytes", lambda uri: _rgba_geotiff_bytes(4))
    monkeypatch.setattr(MOD, "_raster_has_overviews", lambda b: True)

    out = publish_layer(
        layer_uri="s3://bucket/runs/toutle-composite.tif",
        layer_id="composite",
        style_preset="continuous_dem",
    )
    assert out == "s3://bucket/runs/toutle-composite.tif"
    assert pl.pop_legend_for_uri(out) is None


def test_publish_layer_hillshade_has_no_legend(
    _s3_publish: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: a single-band grayscale hillshade resolves NO rescale/colormap
    (no viridis ramp on grayscale terrain) -> no stashed legend."""
    shade = _continuous_geotiff_bytes(0.0, 255.0)
    monkeypatch.setattr(MOD, "_read_raster_bytes", lambda uri: shade)
    monkeypatch.setattr(MOD, "_raster_has_overviews", lambda b: True)

    out = publish_layer(
        layer_uri="s3://bucket/runs/hillshade.tif",
        layer_id="shade",
        style_preset="continuous_hillshade",
    )
    assert out == "s3://bucket/runs/hillshade.tif"
    assert pl.pop_legend_for_uri(out) is None
