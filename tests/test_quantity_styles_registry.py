"""The seam's quantity -> style-preset registry + neutral-ramp fallback.

Pins: a registered quantity resolves to its physical preset; an unknown quantity
degrades to the honest neutral ramp, bumps the process fallback counter, and
never returns a physically-meaningful preset. Every seeded preset is a real key
the publish_layer resolver knows (no silent physically-wrong colormap).
"""

from __future__ import annotations

from trid3nt_server.emission import quantity_styles as qs
from trid3nt_server.tools.publish_layer.publish_layer import _QGIS_STYLE_REGISTRY


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
    # Every seeded quantity maps to a preset the publish_layer RASTER registry
    # knows (so the seam never emits a physically-wrong colormap for a KNOWN
    # quantity) -- EXCEPT the declared mesh presets, which render plugin-side via
    # MDAL (QgsMeshLayer), not the raster titiler rescale registry (ADR 0283).
    for quantity, preset in qs.QUANTITY_STYLE_PRESETS.items():
        assert preset in _QGIS_STYLE_REGISTRY or preset in qs.MESH_PRESETS, (
            f"quantity {quantity!r} -> preset {preset!r} is not a raster registry "
            f"key nor a declared mesh preset"
        )


def test_mesh_quantity_resolves_to_mesh_preset():
    # The native-mesh quantity (ADR 0283) resolves to mesh_grid, is NOT a
    # neutral-ramp fallback, and mesh_grid is a declared (non-raster) mesh preset.
    preset, is_fallback = qs.resolve_style_preset("model_results")
    assert preset == "mesh_grid" and is_fallback is False
    assert preset in qs.MESH_PRESETS
