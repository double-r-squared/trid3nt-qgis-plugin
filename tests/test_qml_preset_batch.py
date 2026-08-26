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

import pytest

from trid3nt_contracts.styles import preset
from trid3nt_server.emission.publish import (
    _infer_style_preset,
    _is_terrain_token_preset,
)
from trid3nt_server.emission.styles import resolve_style

_DECLARED = {
    "impervious_surface_pct": ((0.0, 100.0), "reds"),
    "population_density": ((0.0, 250.0), "magma"),
    "slope_angle_deg": ((0.0, 60.0), "ylorrd"),
    "aspect_compass_deg": ((0.0, 360.0), "hsv"),
}


@pytest.mark.parametrize("name", sorted(_DECLARED))
def test_the_contract_declares_each_preset_with_its_domain_range(name):
    rng, cmap = _DECLARED[name]
    spec = preset(name)
    assert spec is not None, f"{name} is not declared in the style contract"
    # These four are DOMAIN-bounded (a percentage, a compass bearing, a slope
    # angle), so the range is fixed and must not move with the raster.
    assert spec.scale.policy == "fixed" and spec.scale.range == rng
    assert spec.colormap == cmap


@pytest.mark.parametrize("name", sorted(_DECLARED))
def test_each_preset_resolves_to_its_declared_colormap(name):
    rng, cmap = _DECLARED[name]
    lo, hi = rng
    assert resolve_style(name).style_params() == (
        f"&rescale={lo:g},{hi:g}&colormap_name={cmap}")


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
