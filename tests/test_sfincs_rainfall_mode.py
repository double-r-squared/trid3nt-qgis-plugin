"""SFINCS rain lever (surge-only vs design-storm) -- deck-emission + gate pins.

The composer exposes ``rainfall="design_storm"|"none"`` on ``sfincs_flood`` /
``model_flood_scenario``. "design_storm" (default) emits the return-period
Atlas-14 precipitation; "none" builds a SURGE-ONLY deck with NO
``setup_precip_forcing`` block, so a storm-surge inundation question is
answerable without co-occurring rain wetting the whole domain / both sides of a
hydraulic structure.

These tests pin the two modes at the DECK level (the load-bearing "rain block
present/absent in the built deck" contract) plus the Invariant-7 honesty gate: a
surge-only ForcingSpec with no surge driver hard-errors rather than authoring a
deck with zero forcing.
"""

from __future__ import annotations

import pytest
import yaml

from trid3nt_server.agent.workflows.sfincs.sfincs_builder import (
    BuildOptions,
    ForcingSpec,
    SFINCSSetupError,
    WaterlevelForcing,
    _generate_hydromt_yaml_config,
    build_sfincs_model,
)

# A coastal AOI near Mexico Beach, FL (matches the surge-forcing builder tests).
_BBOX = (-85.45, 29.92, -85.38, 29.98)
_DEM = "/tmp/does-not-exist-dep.tif"  # unreadable -> wide-fallback mask (fine)
_LC = "/tmp/lc.tif"
_MAP = "/tmp/manning.csv"


def _emit(forcing: ForcingSpec, options: BuildOptions) -> tuple[str, dict]:
    """Render the deck YAML and return (raw_text, parsed_dict)."""
    text = _generate_hydromt_yaml_config(
        bbox=_BBOX,
        options=options,
        dem_local_path=_DEM,
        landcover_local_path=_LC,
        river_local_path=None,
        forcing=forcing,
        mapping_csv_path=_MAP,
    )
    parsed = yaml.safe_load(text)
    assert isinstance(parsed, dict), f"YAML did not parse to a dict:\n{text}"
    return text, parsed


def test_design_storm_deck_emits_precip_block() -> None:
    """rainfall='design_storm' (pluvial_synthetic) -> setup_precip_forcing present."""
    forcing = ForcingSpec(
        forcing_type="pluvial_synthetic",
        precip_inches=6.0,
        duration_hours=24.0,
        return_period_years=100,
    )
    text, deck = _emit(forcing, BuildOptions(grid_resolution_m=100.0, autoscale_grid=False))
    assert "setup_precip_forcing" in deck
    # magnitude = 6.0 in * 25.4 / 24 hr -> ~6.35 mm/hr (design-storm arithmetic).
    assert deck["setup_precip_forcing"]["magnitude"] == pytest.approx(6.0 * 25.4 / 24.0)


def test_surge_only_deck_omits_precip_block() -> None:
    """rainfall='none' (surge_only) with a surge driver -> NO precip block, surge stays."""
    forcing = ForcingSpec(
        forcing_type="surge_only",
        duration_hours=24.0,
        waterlevel=WaterlevelForcing(
            timeseries_uri="/tmp/wl.csv",
            locations_uri="/tmp/bnd.fgb",
        ),
    )
    text, deck = _emit(forcing, BuildOptions(grid_resolution_m=100.0, autoscale_grid=False))
    # The rain forcing is GONE (both the block and any netamt/precip key).
    assert "setup_precip_forcing" not in deck
    assert "setup_precip_forcing" not in text
    # ... but the surge water-level boundary IS still emitted (surge drives it).
    assert "setup_waterlevel_forcing" in deck
    assert deck["setup_waterlevel_forcing"]["timeseries"] == "/tmp/wl.csv"


def test_surge_only_without_driver_hard_errors() -> None:
    """A surge-only ForcingSpec with NO surge driver is FORCING_OUT_OF_RANGE.

    Invariant 7: a deck with no rainfall AND no surge/tide/discharge boundary
    would produce no flood -- reject it, never author a silently-empty deck. The
    gate fires at the top of build_sfincs_model (before any DEM/landcover read),
    so it needs no staged rasters.
    """
    forcing = ForcingSpec(forcing_type="surge_only", duration_hours=24.0)
    with pytest.raises(SFINCSSetupError) as ei:
        build_sfincs_model(
            dem_uri=_DEM,
            landcover_uri=_LC,
            river_geometry_uri=None,
            forcing=forcing,
            bbox=_BBOX,
            options=BuildOptions(grid_resolution_m=100.0, autoscale_grid=False),
            nlcd_vintage_year=2021,
        )
    assert ei.value.error_code == "FORCING_OUT_OF_RANGE"
