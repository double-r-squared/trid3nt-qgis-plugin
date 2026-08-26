"""The STYLE CONTRACT and its one resolver.

Pins the properties the contract exists to guarantee: every declared quantity
maps to a declared preset (the mirror cannot open again, because there is one
file); an unknown quantity degrades to the honest neutral ramp and counts; a
fixed-scale preset stays fixed; a data-policy preset scales to the RUN and says
so on its legend; a comparison set shares one range; and the register-only fast
path resolves the same string the download path does.
"""

from __future__ import annotations

import pytest

from trid3nt_contracts import styles as contract
from trid3nt_server.emission import styles


def test_every_declared_quantity_maps_to_a_declared_preset():
    declared = contract.preset_names()
    for quantity, name in contract.quantity_defaults().items():
        assert name in declared, (
            f"quantity {quantity!r} -> preset {name!r}, which the same file does "
            "not declare"
        )


def test_registered_quantity_resolves_to_its_physical_preset():
    assert styles.resolve_style_preset("flood_depth") == ("continuous_flood_depth", False)
    assert styles.resolve_style_preset("  Flood_Depth ") == (
        "continuous_flood_depth", False)


def test_a_per_instance_quantity_styles_as_its_family():
    assert styles.resolve_style_preset("plume_concentration__tce") == (
        "continuous_plume_concentration", False)


def test_unknown_quantity_falls_back_to_the_neutral_ramp_and_counts():
    styles.reset_unknown_quantity_fallback_count()
    name, is_fallback = styles.resolve_style_preset("no_such_quantity")
    assert name == styles.NEUTRAL_FALLBACK_PRESET and is_fallback is True
    assert styles.unknown_quantity_fallback_count() == 1
    styles.resolve_style_preset("also_unknown")
    assert styles.unknown_quantity_fallback_count() == 2
    styles.resolve_style_preset("flood_depth")
    assert styles.unknown_quantity_fallback_count() == 2


def test_the_mesh_quantity_resolves_to_a_mesh_preset_with_no_rescale():
    name, is_fallback = styles.resolve_style_preset("model_results")
    assert name == "mesh_grid" and is_fallback is False
    assert styles.resolve_style(name).style_params() == ""


def test_a_domain_standard_preset_stays_fixed_and_never_reads_the_raster():
    reads: list[object] = []

    def _read(scale):
        reads.append(scale)
        return (0.0, 42.0)

    resolved = styles.resolve_style("continuous_seismic_pga", read_range=_read)
    assert resolved.range == (0.0, 1.0) and resolved.source == styles.FIXED
    assert reads == [], "a fixed-scale preset must not pay for a band read"
    assert "fixed domain scale" in resolved.legend_note()


def test_a_data_policy_preset_scales_to_the_run_and_the_legend_says_so():
    resolved = styles.resolve_style("continuous_plume_concentration",
                                    read_range=lambda _s: (0.02, 28.7))
    assert resolved.range == (0.02, 28.7) and resolved.source == styles.FROM_DATA
    assert resolved.style_params() == "&rescale=0.02,28.7&colormap_name=reds"
    note = resolved.legend_note()
    assert "scaled to this run" in note and "28.7" in note and "mg/L" in note


def test_an_unreadable_run_falls_back_to_the_declared_range_and_admits_it():
    resolved = styles.resolve_style("continuous_plume_concentration",
                                    read_range=lambda _s: None)
    assert resolved.source == styles.FALLBACK
    assert "unreadable" in resolved.legend_note()


def test_a_comparison_set_shares_one_range():
    shared = styles.shared_range([(0.0, 10.0), (0.5, 28.7), None])
    assert shared == (0.0, 28.7)
    resolved = styles.resolve_style("continuous_plume_concentration", shared=shared)
    assert resolved.range == shared and resolved.source == styles.SHARED
    assert "shared across the compared set" in resolved.legend_note()


def test_an_override_beats_the_contract_and_shared_beats_the_override():
    override = contract.ScaleSpec(policy="fixed", range=(0.0, 5.0))
    resolved = styles.resolve_style("continuous_plume_concentration", override=override)
    assert resolved.range == (0.0, 5.0)
    resolved = styles.resolve_style("continuous_plume_concentration", override=override,
                                    shared=(0.0, 9.0))
    assert resolved.range == (0.0, 9.0) and resolved.source == styles.SHARED


def test_a_zero_width_run_range_is_widened_rather_than_emitted():
    resolved = styles.resolve_style("continuous_plume_concentration",
                                    read_range=lambda _s: (4.0, 4.0))
    lo, hi = resolved.range
    assert hi > lo, "a zero-width rescale is rejected by the renderer"


def test_a_categorical_preset_paints_from_its_declared_classes():
    resolved = styles.resolve_style("sediment_yield_t_ha_yr")
    assert resolved.classes, "the log-class table is the style"
    params = resolved.style_params()
    assert params.startswith("&colormap=") and "rescale" not in params
    assert styles.legend_classes("sediment_yield_t_ha_yr") == resolved.classes


def test_the_band_stats_fast_path_resolves_what_the_download_path_would():
    from trid3nt_server.emission.publish import style_params_from_band_stats

    assert style_params_from_band_stats("continuous_flood_depth", p2=0.1, p98=4.2) == \
        styles.resolve_style("continuous_flood_depth",
                             read_range=lambda _s: (0.1, 4.2)).style_params()
    for guard in ({"is_categorical": True}, {"is_rgba": True}):
        assert style_params_from_band_stats("continuous_flood_depth", **guard) == ""


def test_a_chart_and_its_layer_read_one_vocabulary():
    label, units = styles.quantity_axis("flood_depth")
    assert (label, units) == ("Flood depth", "m")
    assert styles.preset_units("continuous_flood_depth") == units


@pytest.mark.parametrize("name", sorted(contract.preset_names()))
def test_every_preset_resolves_to_a_usable_scale(name: str):
    resolved = styles.resolve_style(name)
    if resolved.kind == "mesh":
        assert resolved.style_params() == ""
        return
    assert resolved.style_params(), "a continuous preset never resolves to empty"
    assert resolved.legend_note()
