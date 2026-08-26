"""The publish boundary's RENDERING-FAMILY default, and what it must not conclude.

``_infer_style_preset`` runs only when a producer named no preset. It may route
on how a raster has to be PAINTED - RGBA and grayscale terrain render correctly
unstyled, slope and aspect carry their own colormaps - and it may never conclude
a physical QUANTITY from a filename. An undeclared quantity gets the neutral
ramp over its own range, because a named physical ramp would paint an unknown
field in the colours and legend of a known one.
"""

from trid3nt_server.emission.publish import _infer_style_preset
from trid3nt_server.emission.styles import NEUTRAL_FALLBACK_PRESET


def test_terrain_families_get_no_preset():
    assert _infer_style_preset(
        "gs://b/cache/static-30d/colored_relief/abc.tif", "colored-relief-terrain-1"
    ) == ""
    assert _infer_style_preset(
        "gs://b/cache/static-30d/hillshade/abc.tif", "hillshade-asheville"
    ) == ""
    assert _infer_style_preset("gs://b/cache/static-30d/dem/x.tif", "my-dem") == ""


def test_slope_and_aspect_carry_their_own_colormaps():
    assert _infer_style_preset(
        "gs://b/cache/static-30d/slope/x.tif", "slope-1") == "slope_angle_deg"
    assert _infer_style_preset(
        "gs://b/cache/static-30d/aspect/x.tif", "aspect-1") == "aspect_compass_deg"


def test_an_undeclared_quantity_gets_the_neutral_ramp():
    """A filename is not a measurement, so no physical ramp may be read off one."""
    assert _infer_style_preset(
        "gs://runs/01X/flood_depth_peak.tif", "flood-depth-peak-01X"
    ) == NEUTRAL_FALLBACK_PRESET


def test_a_plume_is_not_painted_as_a_depth():
    """The two fields share no units, no range and no legend; one ramp for both
    reports a concentration in the colours and label of a water depth."""
    assert _infer_style_preset(
        "gs://runs/01X/plume_concentration.tif", "plume-concentration-01X"
    ) == NEUTRAL_FALLBACK_PRESET


def test_token_boundaries_not_substrings():
    """``demo`` must not trip the ``dem`` terrain token and lose its styling."""
    assert _infer_style_preset(
        "gs://runs/01X/some_field.tif", "demo-field-1"
    ) == NEUTRAL_FALLBACK_PRESET
    assert _infer_style_preset(
        "gs://runs/01X/dem/x.tif", "demo-1") == ""
