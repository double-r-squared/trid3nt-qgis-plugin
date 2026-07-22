"""job-0269b: family-aware default style preset for publish_layer."""

from trid3nt_server.tools.publish_layer import _infer_style_preset


def test_terrain_families_get_no_preset():
    assert _infer_style_preset(
        "gs://b/cache/static-30d/colored_relief/abc.tif", "colored-relief-terrain-1"
    ) == ""
    assert _infer_style_preset(
        "gs://b/cache/static-30d/hillshade/abc.tif", "hillshade-asheville"
    ) == ""
    # tools-backlog #3: slope/aspect now infer their colormap presets (not "").
    assert _infer_style_preset("gs://b/cache/static-30d/slope/x.tif", "slope-1") == "slope_angle_deg"
    assert _infer_style_preset("gs://b/cache/static-30d/aspect/x.tif", "aspect-1") == "aspect_compass_deg"
    assert _infer_style_preset("gs://b/cache/static-30d/dem/x.tif", "my-dem") == ""


def test_flood_and_plume_keep_flood_ramp_default():
    assert _infer_style_preset(
        "gs://runs/01X/flood_depth_peak.tif", "flood-depth-peak-01X"
    ) == "continuous_flood_depth"
    assert _infer_style_preset(
        "gs://runs/01X/plume_concentration.tif", "plume-concentration-01X"
    ) == "continuous_flood_depth"


def test_token_boundaries_not_substrings():
    # "demo" must NOT match the "dem" terrain token.
    assert _infer_style_preset(
        "gs://runs/01X/flood_depth_peak.tif", "demo-flood-1"
    ) == "continuous_flood_depth"
