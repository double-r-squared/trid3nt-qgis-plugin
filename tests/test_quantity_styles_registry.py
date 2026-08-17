"""The seam's quantity -> style-preset registry + neutral-ramp fallback.

Pins: a registered quantity resolves to its physical preset; an unknown quantity
degrades to the honest neutral ramp, bumps the process fallback counter, and
never returns a physically-meaningful preset. Every seeded preset is a real key
the publish_layer resolver knows (no silent physically-wrong colormap).
"""

from __future__ import annotations

from trid3nt_server.emission import quantity_styles as qs
from trid3nt_server.data.publish_layer.publish_layer import _QGIS_STYLE_REGISTRY


def test_registered_quantity_resolves_to_physical_preset():
    preset, is_fallback = qs.resolve_style_preset("flood_depth")
    assert preset == "continuous_flood_depth" and is_fallback is False
    # Case + whitespace tolerant.
    preset2, fb2 = qs.resolve_style_preset("  Flood_Depth ")
    assert preset2 == "continuous_flood_depth" and fb2 is False


def test_unknown_quantity_falls_back_and_counts():
    qs.reset_unknown_quantity_fallback_count()
    preset, is_fallback = qs.resolve_style_preset("no_such_quantity")
    assert preset == qs.NEUTRAL_FALLBACK_PRESET and is_fallback is True
    assert qs.unknown_quantity_fallback_count() == 1
    qs.resolve_style_preset("also_unknown")
    assert qs.unknown_quantity_fallback_count() == 2
    # A registered quantity does not bump the counter.
    qs.resolve_style_preset("flood_depth")
    assert qs.unknown_quantity_fallback_count() == 2


def test_seeded_presets_are_real_registry_keys():
    # Every seeded quantity maps to a preset the publish_layer registry knows
    # (so the seam never emits a physically-wrong colormap for a KNOWN quantity).
    for quantity, preset in qs.QUANTITY_STYLE_PRESETS.items():
        assert preset in _QGIS_STYLE_REGISTRY, (
            f"quantity {quantity!r} -> preset {preset!r} is not a registry key"
        )
