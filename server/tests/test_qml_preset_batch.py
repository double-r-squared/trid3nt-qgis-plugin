"""tools-backlog #3 -- the per-tool QML/colormap presets that replace the generic
continuous_dem placeholder.

Landed backend colormaps (the Orchestrator wires the frontend legends + substrate):
  - impervious surface  -> reds 0-100%
  - population density   -> magma (people/pixel)
  - slope ANGLE (deg)    -> ylorrd 0-60   (slope removed from the terrain passthrough)
  - aspect COMPASS (deg) -> cyclic hsv 0-360 (aspect removed from the terrain passthrough)
hillshade / dem / relief / terrain / elevation STAY grayscale (shaded relief + bare
DEM render correctly unstyled).

ASCII only.
"""

from __future__ import annotations

from grace2_agent.tools.publish_layer import (
    _TITILER_STYLE_REGISTRY,
    _infer_style_preset,
    _is_terrain_token_preset,
    _registry_style_params,
)


def test_new_presets_registered():
    assert _TITILER_STYLE_REGISTRY["impervious_surface_pct"] == ("0,100", "reds")
    assert _TITILER_STYLE_REGISTRY["population_density"] == ("0,250", "magma")
    assert _TITILER_STYLE_REGISTRY["slope_angle_deg"] == ("0,60", "ylorrd")
    assert _TITILER_STYLE_REGISTRY["aspect_compass_deg"] == ("0,360", "hsv")


def test_new_presets_resolve_to_expected_colormap():
    assert _registry_style_params("impervious_surface_pct") == "&rescale=0,100&colormap_name=reds"
    assert _registry_style_params("population_density") == "&rescale=0,250&colormap_name=magma"
    assert _registry_style_params("slope_angle_deg") == "&rescale=0,60&colormap_name=ylorrd"
    assert _registry_style_params("aspect_compass_deg") == "&rescale=0,360&colormap_name=hsv"


def test_hillshade_still_passthrough_slope_aspect_not():
    # hillshade stays grayscale via the F51 terrain passthrough (correct for relief).
    assert _is_terrain_token_preset("continuous_dem", "s3://b/cache/static-30d/hillshade/x.tif") is True
    # slope/aspect were removed -> they reach the colormap registry instead.
    assert _is_terrain_token_preset("slope_angle_deg", "s3://b/cache/static-30d/slope/x.tif") is False
    assert _is_terrain_token_preset("aspect_compass_deg", "s3://b/cache/static-30d/aspect/x.tif") is False


def test_slope_aspect_infer_to_colormap_presets():
    # an auto-inferred slope/aspect layer routes to its colormap preset, not "" / flood.
    assert _infer_style_preset("s3://b/cache/static-30d/slope/x.tif", "slope-1") == "slope_angle_deg"
    assert _infer_style_preset("s3://b/cache/static-30d/aspect/x.tif", "aspect-1") == "aspect_compass_deg"
    assert _infer_style_preset("s3://b/cache/static-30d/hillshade/x.tif", "hs-1") == ""
