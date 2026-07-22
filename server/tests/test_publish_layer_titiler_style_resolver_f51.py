"""publish_layer F51 — TiTiler style resolver (AWS s3 branch).

On the AWS deployment rasters publish through TiTiler, which renders a single
band float32 COG as PER-TILE-AUTOSCALED GRAYSCALE (invisible / washed out)
unless the tile request carries an explicit ``&rescale=<lo>,<hi>`` and a
``&colormap_name=<name>``. Before F51 only ``continuous_flood_depth`` and
``continuous_plume_concentration`` got params (a hardcoded 2-entry if/elif);
EVERY other continuous preset fell through to ``style_params=""`` and the layer
painted invisible.

F51 routes the s3 branch through ``_resolve_titiler_style_params``:

  (a) flood + plume presets produce the SAME params as before (byte-for-byte);
  (b) an UNKNOWN continuous preset gets a non-empty band-stats rescale+colormap
      (the percentile fallback, exercised with a real raster + a mocked bytes
      read);
  (c) temperature / precip / wind-component presets hit the typed REGISTRY band;
  (d) a PALETTED COG (embedded GDAL color table — NLCD) yields NO rescale so the
      embedded palette wins (job-0324 — never washed out);
  (e) the stats-read-fails path still yields a non-empty SAFE default.

These exercise the pure resolver helper plus the s3 branch end-to-end with real
GeoTIFF bytes built by rasterio — no Cloud Run / GCS / TiTiler network I/O.
"""

from __future__ import annotations

import numpy as np
import pytest
import rasterio
from rasterio.io import MemoryFile

from grace2_agent.tools import publish_layer as pl
from grace2_agent.tools.publish_layer import (
    _band1_percentile_rescale,
    _is_rgba_or_multiband,
    _is_terrain_token_preset,
    _registry_style_params,
    _resolve_titiler_style_params,
    publish_layer,
)

# Patch target = the imported module OBJECT (monkeypatch.setattr on an object,
# not a dotted string), so every helper resolves through the patched name.
MOD = pl


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

    Models the baked landcover + hillshade composite (NATE's Toutle demo) and
    the colored-relief product — already colorized, must render DIRECTLY.
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
# (a) flood / plume — byte-for-byte UNCHANGED
# --------------------------------------------------------------------------- #


def test_flood_preset_params_unchanged() -> None:
    assert (
        _registry_style_params("continuous_flood_depth")
        == "&rescale=0,3&colormap_name=ylgnbu"
    )


def test_plume_preset_params_unchanged() -> None:
    assert (
        _registry_style_params("continuous_plume_concentration")
        == "&rescale=0,10&colormap_name=reds"
    )


# --------------------------------------------------------------------------- #
# sprint-17 wave animation — continuous_wave_height (ADDITIVE; depth unchanged)
# --------------------------------------------------------------------------- #


def test_wave_height_preset_resolves_to_cyan_ramp() -> None:
    """continuous_wave_height resolves to the new 0,6 gnbu (cyan/blue) ramp."""
    assert (
        _registry_style_params("continuous_wave_height")
        == "&rescale=0,6&colormap_name=gnbu"
    )


def test_wave_height_preset_is_in_registry() -> None:
    assert "continuous_wave_height" in pl._TITILER_STYLE_REGISTRY
    assert pl._TITILER_STYLE_REGISTRY["continuous_wave_height"] == ("0,6", "gnbu")


def test_wave_height_distinct_from_flood_depth() -> None:
    """The wave ramp must be VISIBLY distinct from depth (different colormap)."""
    wave = _registry_style_params("continuous_wave_height")
    depth = _registry_style_params("continuous_flood_depth")
    assert wave != depth
    assert "gnbu" in wave
    assert "ylgnbu" in depth


def test_flood_depth_byte_identical_after_wave_addition() -> None:
    """Adding the wave preset MUST NOT change continuous_flood_depth (byte-id)."""
    assert (
        _registry_style_params("continuous_flood_depth")
        == "&rescale=0,3&colormap_name=ylgnbu"
    )
    assert pl._TITILER_STYLE_REGISTRY["continuous_flood_depth"] == ("0,3", "ylgnbu")


def test_resolve_wave_height_preset_wins_over_band_stats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wave registry entry resolves even with a continuous COG in hand
    (registry exact-match wins before the band-stats fallback)."""
    monkeypatch.setattr(
        MOD, "_read_raster_bytes", lambda uri: _continuous_geotiff_bytes(0.0, 6.0)
    )
    out = _resolve_titiler_style_params("continuous_wave_height", "s3://b/wave.tif")
    assert out == "&rescale=0,6&colormap_name=gnbu"


def test_resolve_flood_preset_does_not_read_or_rescale_differently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flood preset resolves to the exact legacy params even with a continuous COG
    in hand (registry wins before the band-stats fallback)."""
    monkeypatch.setattr(
        MOD, "_read_raster_bytes", lambda uri: _continuous_geotiff_bytes()
    )
    out = _resolve_titiler_style_params("continuous_flood_depth", "s3://b/f.tif")
    assert out == "&rescale=0,3&colormap_name=ylgnbu"


def test_resolve_plume_preset_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(MOD, "_read_raster_bytes", lambda uri: None)
    out = _resolve_titiler_style_params(
        "continuous_plume_concentration", "s3://b/p.tif"
    )
    assert out == "&rescale=0,10&colormap_name=reds"


# --------------------------------------------------------------------------- #
# (c) registry families — temperature / precip / wind-components
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "preset,expected",
    [
        ("precipitation_mm", "&rescale=0,100&colormap_name=blues"),
        ("gridmet_pr", "&rescale=0,100&colormap_name=blues"),
        ("era5_total_precipitation", "&rescale=0,100&colormap_name=blues"),
        ("hrrr_2m_temperature", "&rescale=250,320&colormap_name=rdylbu_r"),
        ("gridmet_tmmx", "&rescale=250,320&colormap_name=rdylbu_r"),
        ("era5_2m_temperature", "&rescale=250,320&colormap_name=rdylbu_r"),
        ("wind_speed", "&rescale=0,25&colormap_name=viridis"),
        ("hrrr_10m_u_wind", "&rescale=-25,25&colormap_name=rdbu"),
        ("hrrr_10m_v_wind", "&rescale=-25,25&colormap_name=rdbu"),
        ("era5_10m_u_wind", "&rescale=-25,25&colormap_name=rdbu"),
        ("gridmet_pdsi", "&rescale=-6,6&colormap_name=rdbu"),
        ("gridmet_fm100", "&rescale=0,40&colormap_name=ylgn"),
        ("gridmet_fm1000", "&rescale=0,40&colormap_name=ylgn"),
        ("goes_visible", "&rescale=0,1&colormap_name=gray"),
        ("goes_ir", "&rescale=180,330&colormap_name=gray_r"),
        ("goes_wv", "&rescale=180,330&colormap_name=gray_r"),
    ],
)
def test_registry_exact_matches(preset: str, expected: str) -> None:
    assert _registry_style_params(preset) == expected


@pytest.mark.parametrize(
    "preset,expected",
    [
        # Future variants caught by substring/prefix, not exact key.
        ("era5_2m_temperature_max", "&rescale=250,320&colormap_name=rdylbu_r"),
        ("hrrr_surface_temperature", "&rescale=250,320&colormap_name=rdylbu_r"),
        ("nldas_total_precip", "&rescale=0,100&colormap_name=blues"),
        ("gridmet_10m_u_wind", "&rescale=-25,25&colormap_name=rdbu"),
        ("hrrr_surface_wind_speed", "&rescale=0,25&colormap_name=viridis"),
    ],
)
def test_registry_substring_family_matches(preset: str, expected: str) -> None:
    assert _registry_style_params(preset) == expected


def test_registry_unknown_returns_none() -> None:
    """An unknown preset is NOT in the registry (falls through to band-stats)."""
    assert _registry_style_params("gridmet_vs_totally_unknown_xyz") is None
    assert _registry_style_params("") is None


def test_registry_smoke_falls_through() -> None:
    """hrrr_smoke_* is intentionally excluded so it hits the band-stats path."""
    assert _registry_style_params("hrrr_smoke_near_surface") is None


# --------------------------------------------------------------------------- #
# (b) unknown continuous preset -> non-empty band-stats rescale + colormap
# --------------------------------------------------------------------------- #


def test_band_percentile_rescale_emits_viridis() -> None:
    """A real continuous raster yields a finite p2,p98 rescale with viridis."""
    out = _band1_percentile_rescale(_continuous_geotiff_bytes(lo=0.0, hi=50.0))
    assert out is not None
    assert out.startswith("&rescale=")
    assert out.endswith("&colormap_name=viridis")
    # The percentiles must be inside (well, near) the [0,50] data range.
    body = out[len("&rescale="):].split("&")[0]
    lo_s, hi_s = body.split(",")
    lo, hi = float(lo_s), float(hi_s)
    assert hi > lo
    assert 0.0 <= lo < hi <= 50.0


def test_resolve_unknown_preset_uses_band_stats(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unknown preset resolves to the band-stats rescale (mocked bytes read)."""
    monkeypatch.setattr(
        MOD, "_read_raster_bytes", lambda uri: _continuous_geotiff_bytes(0.0, 30.0)
    )
    out = _resolve_titiler_style_params("gridmet_vs_unknown", "s3://b/x.tif")
    assert out != ""
    assert out.startswith("&rescale=")
    assert out.endswith("&colormap_name=viridis")


def test_resolve_smoke_preset_uses_band_stats(monkeypatch: pytest.MonkeyPatch) -> None:
    """hrrr_smoke_* (tiny range) falls through registry to band-stats."""
    monkeypatch.setattr(
        MOD,
        "_read_raster_bytes",
        lambda uri: _continuous_geotiff_bytes(1e-9, 1e-6),
    )
    out = _resolve_titiler_style_params("hrrr_smoke_near_surface", "s3://b/smoke.tif")
    assert out.startswith("&rescale=")
    assert out.endswith("&colormap_name=viridis")
    assert out != pl._TITILER_SAFE_DEFAULT  # real stats, not the fallback floor


# --------------------------------------------------------------------------- #
# (d) paletted COG -> NO rescale (embedded palette wins)
# --------------------------------------------------------------------------- #


def test_resolve_paletted_cog_emits_no_rescale(monkeypatch: pytest.MonkeyPatch) -> None:
    """A COG with an embedded band-1 color table gets EMPTY style_params so
    TiTiler colorizes from the palette (NLCD must not be washed out)."""
    monkeypatch.setattr(
        MOD, "_read_raster_bytes", lambda uri: _paletted_geotiff_bytes()
    )
    # Even with a preset that WOULD normally rescale, the palette guard wins.
    out = _resolve_titiler_style_params("categorical_landcover", "s3://b/nlcd.tif")
    assert out == ""


def test_resolve_paletted_cog_overrides_registry_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Palette guard is FIRST — it beats even a registry-matching preset."""
    monkeypatch.setattr(
        MOD, "_read_raster_bytes", lambda uri: _paletted_geotiff_bytes()
    )
    out = _resolve_titiler_style_params("precipitation_mm", "s3://b/weird.tif")
    assert out == ""


# --------------------------------------------------------------------------- #
# (e) stats-read-fails -> non-empty SAFE default
# --------------------------------------------------------------------------- #


def test_resolve_safe_default_when_bytes_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unknown preset + unreadable bytes -> the SAFE non-empty default."""
    monkeypatch.setattr(MOD, "_read_raster_bytes", lambda uri: None)
    out = _resolve_titiler_style_params("totally_unknown_preset", "s3://b/gone.tif")
    assert out == pl._TITILER_SAFE_DEFAULT
    assert out == "&rescale=0,1&colormap_name=viridis"
    assert out != ""


def test_band_percentile_none_for_all_nan() -> None:
    """An all-NaN band has no finite values -> None (caller uses safe default)."""
    assert _band1_percentile_rescale(_all_nan_geotiff_bytes()) is None


def test_resolve_all_nan_uses_safe_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        MOD, "_read_raster_bytes", lambda uri: _all_nan_geotiff_bytes()
    )
    out = _resolve_titiler_style_params("unknown_xyz", "s3://b/nan.tif")
    assert out == pl._TITILER_SAFE_DEFAULT


def test_band_percentile_widens_single_value_range() -> None:
    """A single-value band must NOT yield a zero-width rescale (TiTiler rejects)."""
    out = _band1_percentile_rescale(_single_value_geotiff_bytes(7.0))
    assert out is not None
    body = out[len("&rescale="):].split("&")[0]
    lo_s, hi_s = body.split(",")
    assert float(hi_s) > float(lo_s), "single-value band must widen to a non-zero range"


def test_band_percentile_none_for_junk_bytes() -> None:
    assert _band1_percentile_rescale(b"NOT A RASTER") is None
    assert _band1_percentile_rescale(None) is None
    assert _band1_percentile_rescale(b"") is None


# --------------------------------------------------------------------------- #
# REGRESSION FIX: terrain + RGBA rasters must NOT get a colormap/rescale
#
# An adversarial verify panel found a HIGH-severity regression: the four terrain
# composers (compute_colored_relief / compute_hillshade / compute_slope /
# compute_aspect) and the blended composite publish with an explicit
# style_preset (e.g. 'continuous_dem') the registry does NOT know -> the old
# resolver fell to the band-stats viridis fallback, painting a viridis ramp on a
# grayscale hillshade and corrupting an RGBA colored-relief / blended composite.
# Pre-fix these rendered correctly with EMPTY style_params. The resolver now
# returns "" for them BEFORE the registry / band-stats.
# --------------------------------------------------------------------------- #


def test_continuous_dem_preset_is_terrain_passthrough() -> None:
    """'continuous_dem' tokenizes to include 'dem' -> terrain passthrough."""
    assert _is_terrain_token_preset("continuous_dem", "s3://b/dem.tif") is True


@pytest.mark.parametrize(
    "preset",
    [
        "continuous_dem",
        "continuous_hillshade",
        # slope/aspect REMOVED from the passthrough (tools-backlog #3) -> they now
        # carry colormaps; see test_slope_aspect_no_longer_passthrough below.
        "colored_relief",
        "terrain_rgba",
        "elevation",
    ],
)
def test_terrain_token_presets_match(preset: str) -> None:
    assert _is_terrain_token_preset(preset, "s3://b/x.tif") is True


def test_slope_aspect_no_longer_passthrough() -> None:
    """tools-backlog #3: slope/aspect were removed from the terrain passthrough so
    they reach the colormap registry. (hillshade/dem/relief/elevation still grayscale.)"""
    assert _is_terrain_token_preset("continuous_aspect", "s3://b/cache/static-30d/aspect/x.tif") is False
    assert _is_terrain_token_preset("slope_angle_deg", "s3://b/cache/static-30d/slope/x.tif") is False
    assert _is_terrain_token_preset("continuous_hillshade", "s3://b/cache/static-30d/hillshade/x.tif") is True


def test_non_terrain_preset_does_not_match() -> None:
    """A weather scalar preset must NOT trip the terrain passthrough."""
    assert _is_terrain_token_preset("hrrr_2m_temperature", "s3://b/t.tif") is False
    assert _is_terrain_token_preset("precipitation_mm", "s3://b/p.tif") is False
    # 'demo' must not match 'dem' (whole-token boundary).
    assert _is_terrain_token_preset("flood_demo", "s3://b/demo-flood.tif") is False


def test_terrain_token_matches_on_uri_when_preset_blank() -> None:
    assert _is_terrain_token_preset("", "s3://b/runs/hillshade-toutle.tif") is True


# (a) style_preset='continuous_dem' -> ''
def test_resolve_continuous_dem_emits_no_rescale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The DEM/terrain preset gets EMPTY style_params (no viridis ramp)."""
    monkeypatch.setattr(
        MOD, "_read_raster_bytes", lambda uri: _continuous_geotiff_bytes(100.0, 2500.0)
    )
    out = _resolve_titiler_style_params("continuous_dem", "s3://b/dem.tif")
    assert out == ""


# (c) hillshade preset -> '' (slope/aspect now get colormaps; tools-backlog #3)
@pytest.mark.parametrize(
    "preset",
    ["continuous_hillshade"],
)
def test_resolve_terrain_composer_presets_emit_no_rescale(
    preset: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Single-band grayscale terrain composers render correctly with NO rescale
    (a viridis ramp on a hillshade was the regression). Hillshade stays grayscale;
    slope/aspect now carry colormaps (tools-backlog #3)."""
    monkeypatch.setattr(
        MOD, "_read_raster_bytes", lambda uri: _continuous_geotiff_bytes(0.0, 255.0)
    )
    out = _resolve_titiler_style_params(preset, "s3://b/shade.tif")
    assert out == ""


def test_resolve_slope_aspect_presets_emit_colormap(monkeypatch: pytest.MonkeyPatch) -> None:
    """tools-backlog #3: slope/aspect presets resolve to their colormaps."""
    monkeypatch.setattr(
        MOD, "_read_raster_bytes", lambda uri: _continuous_geotiff_bytes(0.0, 60.0)
    )
    assert _resolve_titiler_style_params(
        "slope_angle_deg", "s3://b/cache/static-30d/slope/x.tif"
    ) == "&rescale=0,60&colormap_name=ylorrd"
    assert _resolve_titiler_style_params(
        "aspect_compass_deg", "s3://b/cache/static-30d/aspect/x.tif"
    ) == "&rescale=0,360&colormap_name=hsv"


# (b) a mocked 3-band / RGBA COG -> '' even with a NON-terrain preset
def test_is_rgba_or_multiband_detects_rgba() -> None:
    assert _is_rgba_or_multiband(_rgba_geotiff_bytes(bands=4)) is True
    assert _is_rgba_or_multiband(_rgba_geotiff_bytes(bands=3)) is True


def test_is_rgba_or_multiband_false_for_single_band() -> None:
    assert _is_rgba_or_multiband(_continuous_geotiff_bytes()) is False
    assert _is_rgba_or_multiband(None) is False
    assert _is_rgba_or_multiband(b"NOT A RASTER") is False


def test_resolve_rgba_cog_emits_no_rescale_even_with_nonterrain_preset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A baked RGBA composite (NATE's Toutle landcover+hillshade) must render
    DIRECTLY — '' even when the preset is a NON-terrain string that would
    otherwise hit the band-stats fallback and corrupt the colors."""
    monkeypatch.setattr(MOD, "_read_raster_bytes", lambda uri: _rgba_geotiff_bytes(4))
    out = _resolve_titiler_style_params(
        "some_unknown_composite_preset", "s3://b/composite.tif"
    )
    assert out == ""


def test_resolve_3band_cog_emits_no_rescale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(MOD, "_read_raster_bytes", lambda uri: _rgba_geotiff_bytes(3))
    out = _resolve_titiler_style_params("unknown_xyz", "s3://b/rgb.tif")
    assert out == ""


def test_resolve_colored_relief_composite_no_rescale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end resolver: a colored-relief RGBA COG with a terrain preset is
    doubly protected (RGBA guard AND terrain-token guard) -> ''."""
    monkeypatch.setattr(MOD, "_read_raster_bytes", lambda uri: _rgba_geotiff_bytes(4))
    out = _resolve_titiler_style_params("colored_relief", "s3://b/relief.tif")
    assert out == ""


# (d) the 6 weather scalar presets STILL get non-empty registry params
@pytest.mark.parametrize(
    "preset,expected",
    [
        ("hrrr_10m_u_wind", "&rescale=-25,25&colormap_name=rdbu"),
        ("hrrr_2m_temperature", "&rescale=250,320&colormap_name=rdylbu_r"),
        ("precipitation_mm", "&rescale=0,100&colormap_name=blues"),
        ("wind_speed", "&rescale=0,25&colormap_name=viridis"),
        ("gridmet_pr", "&rescale=0,100&colormap_name=blues"),
        ("era5_2m_temperature", "&rescale=250,320&colormap_name=rdylbu_r"),
    ],
)
def test_weather_scalars_still_get_registry_params_after_fix(
    preset: str, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cure must survive: single-band weather scalars (count==1, NOT terrain,
    NOT paletted) STILL resolve to their registry rescale+colormap."""
    # A single-band continuous COG: NOT RGBA, NOT terrain-token, NOT paletted.
    monkeypatch.setattr(
        MOD, "_read_raster_bytes", lambda uri: _continuous_geotiff_bytes(0.0, 50.0)
    )
    out = _resolve_titiler_style_params(preset, "s3://b/weather.tif")
    assert out == expected
    assert out != ""


# (e) 'precipitable_water' must NOT get the precip ramp (loose-substring fix)
def test_precipitable_water_does_not_get_precip_ramp() -> None:
    """'precipitable_water' is NOT precipitation — it must not hit the 0,100
    blues precip ramp via a loose 'precip' substring."""
    assert _registry_style_params("precipitable_water") is None
    assert _registry_style_params("era5_total_column_water_vapour") is None


def test_precipitable_water_resolves_to_band_stats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A precipitable-water single-band scalar falls to the generic band-stats
    rescale (viridis), NOT the precip ramp."""
    monkeypatch.setattr(
        MOD, "_read_raster_bytes", lambda uri: _continuous_geotiff_bytes(5.0, 60.0)
    )
    out = _resolve_titiler_style_params("precipitable_water", "s3://b/pwat.tif")
    assert out != ""
    assert "blues" not in out
    assert out.endswith("&colormap_name=viridis")


def test_precip_family_still_matches_guarded_variants() -> None:
    """Genuine precip variable names still resolve to the blues ramp."""
    assert (
        _registry_style_params("nldas_total_precip")
        == "&rescale=0,100&colormap_name=blues"
    )
    assert (
        _registry_style_params("hrrr_surface_precipitation")
        == "&rescale=0,100&colormap_name=blues"
    )


# (f) NLCD paletted still '' (re-assert with explicit ordering after the new
#     RGBA/terrain guards — paletted is still resolved FIRST).
def test_nlcd_paletted_still_no_rescale_after_fix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(MOD, "_read_raster_bytes", lambda uri: _paletted_geotiff_bytes())
    out = _resolve_titiler_style_params("categorical_landcover", "s3://b/nlcd.tif")
    assert out == ""


# --------------------------------------------------------------------------- #
# s3 branch end-to-end — the resolved style now rides the LEGEND stash keyed by
# the returned raw s3:// uri (TiTiler exit: no tile template to bake it into).
# --------------------------------------------------------------------------- #


@pytest.fixture()
def _s3_titiler(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRACE2_STORAGE_BACKEND", "s3")


def test_publish_layer_s3_unknown_preset_stashes_nonempty_style(
    _s3_titiler: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: an unknown continuous preset on the s3 branch resolves a
    non-empty rescale+colormap (never grayscale) and stashes it as the legend
    keyed by the returned raw s3 uri."""
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
    assert legend.vmax > legend.vmin
    assert 0.0 <= legend.vmin < legend.vmax <= 25.0  # real band-stats range


def test_publish_layer_s3_temperature_preset_hits_registry(
    _s3_titiler: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: a temperature preset lands the registry rdylbu_r band in the
    stashed legend (vmin/vmax = the pinned 250-320 K range)."""
    good = _continuous_geotiff_bytes(260.0, 310.0)
    monkeypatch.setattr(MOD, "_read_raster_bytes", lambda uri: good)
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


def test_publish_layer_s3_paletted_gets_categorical_legend(
    _s3_titiler: None, monkeypatch: pytest.MonkeyPatch
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


def test_publish_layer_s3_rgba_composite_has_no_legend(
    _s3_titiler: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: NATE's baked landcover+hillshade RGBA composite resolves NO
    rescale/colormap (regression — band-stats viridis corrupted the colors),
    so no legend is stashed and the plugin renders the baked colors directly."""
    rgba = _rgba_geotiff_bytes(4)
    monkeypatch.setattr(MOD, "_read_raster_bytes", lambda uri: rgba)
    monkeypatch.setattr(MOD, "_raster_has_overviews", lambda b: True)

    out = publish_layer(
        layer_uri="s3://bucket/runs/toutle-composite.tif",
        layer_id="composite",
        style_preset="continuous_dem",
    )
    assert out == "s3://bucket/runs/toutle-composite.tif"
    assert pl.pop_legend_for_uri(out) is None


def test_publish_layer_s3_hillshade_has_no_legend(
    _s3_titiler: None, monkeypatch: pytest.MonkeyPatch
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
