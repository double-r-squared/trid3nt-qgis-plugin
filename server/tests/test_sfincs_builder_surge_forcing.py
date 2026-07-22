"""Unit tests for COASTAL SFINCS surge / discharge / wind / pressure forcing,
subgrid tables, building-obstacle masks, and the pandas>=2.0 ``set_forcing_1d``
guard (AGENT A — forcing + obstacles).

TARGET (NATE 2026-06-17): the COASTAL SFINCS North Star — Deltares Hurricane
Michael / Mexico Beach — needs the SFINCS deck to carry SURGE / TIDE / RIVER
DISCHARGE / WIND / PRESSURE boundary forcing (today's ``sfincs_builder`` is
PLUVIAL-only) plus BUILDING OBSTACLES (subgrid + footprint mask) for a rough
urban-flood-around-buildings estimate.

These tests PROVE, at the YAML-emission seam (``_generate_hydromt_yaml_config``,
the same seam ``test_sfincs_builder_mask_active`` asserts against):

1.  A ``ForcingSpec`` carrying surge members emits the matching
    ``setup_waterlevel_forcing`` / ``setup_river_inflow`` +
    ``setup_discharge_forcing`` / ``setup_wind_forcing[_from_grid]`` /
    ``setup_pressure_forcing_from_grid`` blocks, with the correct kwargs.
2.  ``setup_river_inflow`` is ALWAYS emitted BEFORE ``setup_discharge_forcing``
    (the hydromt-sfincs ordering contract — inflow makes the src points, the
    discharge step attaches the series).
3.  ``enable_subgrid`` emits a ``setup_subgrid`` block reusing the dep +
    roughness datasets; ``nr_subgrid_pixels`` is honoured.
4.  A building-obstacle URI in ``"exclude"`` mode burns an ``exclude_mask`` into
    ``setup_mask_active`` (no-flow holes); in ``"raise"`` mode it burns a
    ``datasets_riv`` raised-bank entry into ``setup_subgrid`` instead.
5.  A pure-pluvial deck (no surge members, no subgrid, no obstacles) emits NONE
    of the new blocks — it stays byte-identical to the v0.1 pluvial deck
    (regression guard).
6.  The pandas>=2.0 guard re-attaches ``pandas.Index.is_integer`` /
    ``is_numeric`` so hydromt-sfincs 1.2.2 ``set_forcing_1d`` (the shared
    surge/discharge sink) stays callable on pandas 2.x and 3.x.
"""

from __future__ import annotations

import yaml

from trid3nt_server.workflows.sfincs_builder import (
    _PANDAS_GUARD_OK,
    BuildOptions,
    DischargeForcing,
    ForcingSpec,
    InfiltrationForcing,
    PressureForcing,
    WaterlevelForcing,
    WindForcing,
    _generate_hydromt_yaml_config,
    _install_pandas_set_forcing_1d_guard,
)

# A coastal AOI near Mexico Beach, FL (the North Star geography).
_MEXICO_BEACH_BBOX = (-85.45, 29.92, -85.38, 29.98)

# Local paths so ``_stage_gcs_local`` is a no-op (no GCS/S3 in unit tests).
_DEM = "/tmp/does-not-exist-dep.tif"  # unreadable -> wide-fallback mask (fine)
_LC = "/tmp/lc.tif"
_MAP = "/tmp/manning.csv"


def _emit(forcing: ForcingSpec, options: BuildOptions) -> dict:
    """Emit the deck YAML for ``forcing`` + ``options`` and parse it to a dict."""
    text = _generate_hydromt_yaml_config(
        bbox=_MEXICO_BEACH_BBOX,
        options=options,
        dem_local_path=_DEM,
        landcover_local_path=_LC,
        river_local_path=None,
        forcing=forcing,
        mapping_csv_path=_MAP,
    )
    parsed = yaml.safe_load(text)
    assert isinstance(parsed, dict), f"YAML did not parse to a dict:\n{text}"
    return parsed


# --------------------------------------------------------------------------- #
# Test 1 — surge / tide / discharge / wind / pressure blocks are emitted
# --------------------------------------------------------------------------- #


def test_surge_forcing_emits_all_blocks() -> None:
    """A fully-populated surge ForcingSpec emits every forcing block + kwargs."""
    forcing = ForcingSpec(
        forcing_type="storm_surge",
        waterlevel=WaterlevelForcing(
            timeseries_uri="/tmp/wl.csv",
            locations_uri="/tmp/bnd.fgb",
            offset=0.15,
            buffer_m=5000.0,
        ),
        discharge=DischargeForcing(
            timeseries_uri="/tmp/dis.csv",
            rivers_uri="/tmp/riv.fgb",
            river_upa_km2=10.0,
        ),
        wind=WindForcing(magnitude=45.0, direction=170.0),
        pressure=PressureForcing(grid_uri="/tmp/press.nc", fill_value=101325.0),
    )
    deck = _emit(forcing, BuildOptions(grid_resolution_m=100.0, autoscale_grid=False))

    # Water-level (bzs) — surge + tide boundary.
    assert "setup_waterlevel_forcing" in deck
    wl = deck["setup_waterlevel_forcing"]
    assert wl["timeseries"] == "/tmp/wl.csv"
    assert wl["locations"] == "/tmp/bnd.fgb"
    assert wl["offset"] == 0.15
    assert wl["buffer"] == 5000.0

    # River inflow + discharge (dis).
    assert "setup_river_inflow" in deck
    assert deck["setup_river_inflow"]["rivers"] == "/tmp/riv.fgb"
    assert deck["setup_river_inflow"]["river_upa"] == 10.0
    assert "setup_discharge_forcing" in deck
    assert deck["setup_discharge_forcing"]["timeseries"] == "/tmp/dis.csv"

    # Uniform wind.
    assert "setup_wind_forcing" in deck
    assert deck["setup_wind_forcing"]["magnitude"] == 45.0
    assert deck["setup_wind_forcing"]["direction"] == 170.0

    # Gridded pressure.
    assert "setup_pressure_forcing_from_grid" in deck
    assert deck["setup_pressure_forcing_from_grid"]["press"] == "/tmp/press.nc"
    assert deck["setup_pressure_forcing_from_grid"]["fill_value"] == 101325.0


# --------------------------------------------------------------------------- #
# Test 2 — river inflow ALWAYS precedes discharge forcing (order contract)
# --------------------------------------------------------------------------- #


def test_river_inflow_precedes_discharge_forcing() -> None:
    """``setup_river_inflow`` must be emitted BEFORE ``setup_discharge_forcing``.

    hydromt-sfincs contract: inflow establishes the ``src`` discharge points,
    discharge attaches the series — reversing them silently drops the forcing.
    """
    forcing = ForcingSpec(
        forcing_type="storm_surge",
        discharge=DischargeForcing(
            timeseries_uri="/tmp/dis.csv",
            hydrography_uri="/tmp/hydro.nc",
        ),
    )
    deck = _emit(forcing, BuildOptions(autoscale_grid=False))
    keys = list(deck.keys())
    assert "setup_river_inflow" in keys
    assert "setup_discharge_forcing" in keys
    assert keys.index("setup_river_inflow") < keys.index("setup_discharge_forcing")


# --------------------------------------------------------------------------- #
# Test 3 — waterlevel geodataset path + gridded wind path
# --------------------------------------------------------------------------- #


def test_waterlevel_geodataset_and_gridded_wind() -> None:
    """The single-geodataset water-level path + gridded-wind path emit correctly."""
    forcing = ForcingSpec(
        forcing_type="storm_surge",
        waterlevel=WaterlevelForcing(geodataset_uri="/tmp/wl_points.nc"),
        wind=WindForcing(grid_uri="/tmp/wind.nc"),
    )
    deck = _emit(forcing, BuildOptions(autoscale_grid=False))
    assert deck["setup_waterlevel_forcing"]["geodataset"] == "/tmp/wl_points.nc"
    # geodataset path must NOT also emit timeseries/locations.
    assert "timeseries" not in deck["setup_waterlevel_forcing"]
    # Gridded wind goes to setup_wind_forcing_from_grid, not the uniform tool.
    assert "setup_wind_forcing_from_grid" in deck
    assert deck["setup_wind_forcing_from_grid"]["wind"] == "/tmp/wind.nc"
    assert "setup_wind_forcing" not in deck


# --------------------------------------------------------------------------- #
# Test 4 — subgrid block + building obstacles (exclude + raise modes)
# --------------------------------------------------------------------------- #


def test_subgrid_block_emitted() -> None:
    """``enable_subgrid`` emits ``setup_subgrid`` reusing dep + roughness datasets."""
    forcing = ForcingSpec(
        forcing_type="pluvial_synthetic", precip_inches=8.0, duration_hours=24.0
    )
    deck = _emit(
        forcing,
        BuildOptions(
            enable_subgrid=True,
            subgrid_nr_subgrid_pixels=15,
            autoscale_grid=False,
        ),
    )
    assert "setup_subgrid" in deck
    sg = deck["setup_subgrid"]
    assert sg["datasets_dep"][0]["elevtn"]  # dep reused
    assert sg["datasets_rgh"][0]["lulc"] == _LC  # roughness reused
    assert sg["nr_subgrid_pixels"] == 15


def test_building_obstacle_exclude_mode() -> None:
    """``"exclude"`` mode burns the footprint geofile as a no-flow exclude_mask."""
    forcing = ForcingSpec(
        forcing_type="pluvial_synthetic", precip_inches=8.0, duration_hours=24.0
    )
    deck = _emit(
        forcing,
        BuildOptions(
            building_obstacle_uri="/tmp/buildings.fgb",
            building_obstacle_mode="exclude",
            autoscale_grid=False,
        ),
    )
    # exclude_mask lands inside setup_mask_active (cells -> inactive / no-flow).
    assert deck["setup_mask_active"]["exclude_mask"] == "/tmp/buildings.fgb"
    assert deck["setup_mask_active"]["all_touched"] is True
    # exclude mode does NOT need (nor emit) a datasets_riv obstacle.
    if "setup_subgrid" in deck:
        assert "datasets_riv" not in deck["setup_subgrid"]


def test_building_obstacle_raise_mode_burns_datasets_riv() -> None:
    """``"raise"`` mode (subgrid on) burns footprints as a datasets_riv obstacle."""
    forcing = ForcingSpec(
        forcing_type="pluvial_synthetic", precip_inches=8.0, duration_hours=24.0
    )
    deck = _emit(
        forcing,
        BuildOptions(
            enable_subgrid=True,
            building_obstacle_uri="/tmp/buildings.fgb",
            building_obstacle_mode="raise",
            autoscale_grid=False,
        ),
    )
    # raise mode keeps cells active: NO exclude_mask, footprints raised via subgrid.
    assert "exclude_mask" not in deck["setup_mask_active"]
    assert "datasets_riv" in deck["setup_subgrid"]
    assert (
        deck["setup_subgrid"]["datasets_riv"][0]["centerlines"] == "/tmp/buildings.fgb"
    )


# --------------------------------------------------------------------------- #
# Test 5 — pure-pluvial deck emits NONE of the new blocks (regression guard)
# --------------------------------------------------------------------------- #


def test_pure_pluvial_deck_is_v01_compatible() -> None:
    """No surge members + no subgrid + no obstacles → none of the new keys leak.

    This guards the v0.1 pluvial deck shape: the surge/subgrid/obstacle work must
    be strictly additive — a plain Atlas-14 flood (the M5 demo) must emit exactly
    the blocks it emitted before this change.
    """
    forcing = ForcingSpec(
        forcing_type="pluvial_synthetic", precip_inches=8.0, duration_hours=24.0
    )
    deck = _emit(forcing, BuildOptions(autoscale_grid=False))
    new_keys = {
        "setup_subgrid",
        "setup_waterlevel_forcing",
        "setup_river_inflow",
        "setup_discharge_forcing",
        "setup_wind_forcing",
        "setup_wind_forcing_from_grid",
        "setup_pressure_forcing_from_grid",
    }
    leaked = new_keys & set(deck.keys())
    assert not leaked, f"pure-pluvial deck leaked new blocks: {leaked}"
    # The v0.1 blocks are still all present.
    assert "setup_precip_forcing" in deck
    assert "setup_mask_active" in deck
    assert "setup_manning_roughness" in deck
    # And the pluvial mask carries no building exclude_mask.
    assert "exclude_mask" not in deck["setup_mask_active"]


# --------------------------------------------------------------------------- #
# Test 6 — compound deck (pluvial precip + surge waterlevel) carries both
# --------------------------------------------------------------------------- #


def test_compound_deck_carries_precip_and_surge() -> None:
    """A pluvial + surge compound deck emits BOTH precip and water-level forcing."""
    forcing = ForcingSpec(
        forcing_type="pluvial_synthetic",
        precip_inches=6.0,
        duration_hours=24.0,
        return_period_years=100,
        waterlevel=WaterlevelForcing(geodataset_uri="/tmp/wl.nc"),
    )
    deck = _emit(forcing, BuildOptions(autoscale_grid=False))
    assert "setup_precip_forcing" in deck
    assert "setup_waterlevel_forcing" in deck
    assert deck["setup_waterlevel_forcing"]["geodataset"] == "/tmp/wl.nc"


# --------------------------------------------------------------------------- #
# Test 7 — pandas>=2.0 set_forcing_1d guard
# --------------------------------------------------------------------------- #


def test_pandas_set_forcing_1d_guard_installed() -> None:
    """The module-level pandas guard ran and keeps Index predicates callable.

    hydromt-sfincs 1.2.2 ``set_forcing_1d`` (the shared surge/discharge sink)
    calls ``Index.is_integer`` / ``Index.is_numeric`` — pandas removed both in
    3.0. The guard re-attaches them so the surge/river forcing path the COASTAL
    North Star needs does not raise on pandas>=3.0.
    """
    import warnings

    import pandas as pd

    assert _PANDAS_GUARD_OK is True
    # Idempotent re-run is safe.
    assert _install_pandas_set_forcing_1d_guard() is True

    idx = pd.RangeIndex(3)  # the exact index type set_forcing_1d hits
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # 2.x emits a deprecation; 3.x raised pre-guard
        assert hasattr(idx, "is_integer")
        assert hasattr(idx, "is_numeric")
        assert idx.is_integer() is True
        assert idx.is_numeric() is True

    # A non-integer index reports is_integer() False (correctness, not just presence).
    sidx = pd.Index(["a", "b"])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert sidx.is_integer() is False


# --------------------------------------------------------------------------- #
# Test 8 — advanced-physics switches land in the setup_config block
#          (NATE 2026-06-26 — physics_registry FIX + _emit_physics_config)
# --------------------------------------------------------------------------- #


def test_advanced_physics_emits_into_setup_config() -> None:
    """``BuildOptions.advanced_physics`` writes advection/theta/alpha/huthresh/
    latitude/cdval into the setup_config block (HydroMT passthrough -> sfincs.inp).

    Asserts the two physics_registry bug fixes: ``coriolis_latitude`` maps to
    ``latitude`` (NOT a fictional ``coriolis`` key) and ``wind_drag`` maps to a
    flat ``cdval`` curve with ``cdnrb: 3`` (NOT the ``cdwnd`` speed-breakpoint axis).
    """
    forcing = ForcingSpec(
        forcing_type="pluvial_synthetic", precip_inches=8.0, duration_hours=24.0
    )
    deck = _emit(
        forcing,
        BuildOptions(
            autoscale_grid=False,
            advanced_physics={
                "advection": 1,
                "theta": 0.95,
                "alpha": 0.7,
                "huthresh": 0.02,
                "coriolis_latitude": 29.9,
                "wind_drag": 0.0026,
            },
        ),
    )
    cfg = deck["setup_config"]
    assert cfg["advection"] == 1
    assert cfg["theta"] == 0.95
    assert cfg["alpha"] == 0.7
    assert cfg["huthresh"] == 0.02
    # coriolis_latitude -> sfincs.inp:latitude (the registry FIX; no `coriolis` key).
    assert cfg["latitude"] == 29.9
    assert "coriolis" not in cfg
    # wind_drag -> a flat cdval [cd,cd,cd] curve with cdnrb=3 (the registry FIX).
    assert cfg["cdval"] == [0.0026, 0.0026, 0.0026]
    assert cfg["cdnrb"] == 3


def test_wind_drag_zero_keeps_default_formula() -> None:
    """``wind_drag == 0`` (the registry default) emits NO cdval override."""
    forcing = ForcingSpec(
        forcing_type="pluvial_synthetic", precip_inches=8.0, duration_hours=24.0
    )
    deck = _emit(
        forcing,
        BuildOptions(autoscale_grid=False, advanced_physics={"wind_drag": 0.0}),
    )
    cfg = deck["setup_config"]
    assert "cdval" not in cfg
    assert "cdnrb" not in cfg


def test_pluvial_deck_setup_config_unchanged_without_physics() -> None:
    """REGRESSION: a pluvial deck with advanced_physics=None carries NONE of the
    physics keys in setup_config (byte-identical baseline)."""
    forcing = ForcingSpec(
        forcing_type="pluvial_synthetic", precip_inches=8.0, duration_hours=24.0
    )
    deck = _emit(forcing, BuildOptions(autoscale_grid=False))
    cfg = deck["setup_config"]
    for k in ("advection", "theta", "alpha", "huthresh", "latitude", "cdval", "cdnrb", "qinf"):
        assert k not in cfg, f"pluvial setup_config leaked physics key {k!r}"


# --------------------------------------------------------------------------- #
# Test 9 — infiltration (CN scsfile / constant qinffile / bare qinf)
# --------------------------------------------------------------------------- #


def test_infiltration_cn_emits_setup_cn_infiltration() -> None:
    """A ``cn_uri`` infiltration member emits ``setup_cn_infiltration`` with a
    null antecedent_moisture (single-band GCN250 contract)."""
    forcing = ForcingSpec(
        forcing_type="pluvial_synthetic",
        precip_inches=8.0,
        duration_hours=24.0,
        infiltration=InfiltrationForcing(cn_uri="/tmp/gcn250.tif", antecedent_moisture=None),
    )
    deck = _emit(forcing, BuildOptions(autoscale_grid=False))
    assert "setup_cn_infiltration" in deck
    assert deck["setup_cn_infiltration"]["cn"] == "/tmp/gcn250.tif"
    # antecedent_moisture: null (single-band raster -> bare-DataArray branch).
    assert deck["setup_cn_infiltration"]["antecedent_moisture"] is None
    # CN path does NOT also emit a bare qinf or a constant-infiltration step.
    assert "setup_constant_infiltration" not in deck
    assert "qinf" not in deck["setup_config"]


def test_infiltration_constant_lulc_emits_setup_constant_infiltration() -> None:
    """A ``lulc_uri`` + ``reclass_table_uri`` member emits ``setup_constant_infiltration``."""
    forcing = ForcingSpec(
        forcing_type="pluvial_synthetic",
        precip_inches=8.0,
        duration_hours=24.0,
        infiltration=InfiltrationForcing(
            lulc_uri="/tmp/lc.tif", reclass_table_uri="/tmp/inf.csv"
        ),
    )
    deck = _emit(forcing, BuildOptions(autoscale_grid=False))
    assert "setup_constant_infiltration" in deck
    assert deck["setup_constant_infiltration"]["lulc"] == "/tmp/lc.tif"
    assert deck["setup_constant_infiltration"]["reclass_table"] == "/tmp/inf.csv"
    assert "setup_cn_infiltration" not in deck


def test_infiltration_bare_constant_emits_qinf_in_setup_config() -> None:
    """A bare ``constant_mm_per_hr`` (no raster) routes to the scalar
    sfincs.inp:qinf inside setup_config (setup_constant_infiltration needs a raster)."""
    forcing = ForcingSpec(
        forcing_type="pluvial_synthetic",
        precip_inches=8.0,
        duration_hours=24.0,
        infiltration=InfiltrationForcing(constant_mm_per_hr=3.5),
    )
    deck = _emit(forcing, BuildOptions(autoscale_grid=False))
    assert deck["setup_config"]["qinf"] == 3.5
    assert "setup_cn_infiltration" not in deck
    assert "setup_constant_infiltration" not in deck


# --------------------------------------------------------------------------- #
# Test 10 — levee-breach interior point source (NATE 2026-06-26)
# --------------------------------------------------------------------------- #


def test_breach_emits_interior_discharge_with_merge() -> None:
    """A ``breach`` member emits a ``setup_discharge_forcing`` with explicit
    interior ``locations`` + ``merge: true`` and NO ``setup_river_inflow``."""
    forcing = ForcingSpec(
        forcing_type="storm_surge",
        breach=DischargeForcing(
            timeseries_uri="/tmp/breach_dis.csv",
            locations_uri="/tmp/breach_src.fgb",
        ),
    )
    deck = _emit(forcing, BuildOptions(autoscale_grid=False))
    assert "setup_discharge_forcing" in deck
    dq = deck["setup_discharge_forcing"]
    assert dq["locations"] == "/tmp/breach_src.fgb"
    assert dq["timeseries"] == "/tmp/breach_dis.csv"
    assert dq["merge"] is True
    # A pure breach (no rivers/hydrography) does NOT trigger setup_river_inflow.
    assert "setup_river_inflow" not in deck


def test_compound_river_inflow_then_breach_discharge_order() -> None:
    """A compound run with BOTH an edge river discharge AND an interior breach
    emits setup_river_inflow + the river discharge FIRST, then the breach block."""
    text = _generate_hydromt_yaml_config(
        bbox=_MEXICO_BEACH_BBOX,
        options=BuildOptions(autoscale_grid=False),
        dem_local_path=_DEM,
        landcover_local_path=_LC,
        river_local_path=None,
        forcing=ForcingSpec(
            forcing_type="storm_surge",
            discharge=DischargeForcing(
                timeseries_uri="/tmp/river_dis.csv",
                rivers_uri="/tmp/riv.fgb",
            ),
            breach=DischargeForcing(
                timeseries_uri="/tmp/breach_dis.csv",
                locations_uri="/tmp/breach_src.fgb",
            ),
        ),
        mapping_csv_path=_MAP,
    )
    # The breach merges with the river dis -> two setup_discharge_forcing blocks.
    assert text.count("setup_discharge_forcing:") == 2
    # Ordering: river inflow first, then the merge: true breach block.
    i_river = text.index("setup_river_inflow:")
    i_merge = text.index("merge: true")
    assert i_river < i_merge
    # The breach locations point appears (interior src cell).
    assert "/tmp/breach_src.fgb" in text


def test_pure_pluvial_deck_no_breach_or_infiltration() -> None:
    """REGRESSION: a pure-pluvial deck (no breach / infiltration / physics) emits
    none of the new blocks and its setup_config carries no qinf/physics keys."""
    forcing = ForcingSpec(
        forcing_type="pluvial_synthetic", precip_inches=8.0, duration_hours=24.0
    )
    deck = _emit(forcing, BuildOptions(autoscale_grid=False))
    new_keys = {
        "setup_cn_infiltration",
        "setup_constant_infiltration",
    }
    assert not (new_keys & set(deck.keys()))
    # Only one discharge block at most (none here, pure pluvial).
    assert "setup_discharge_forcing" not in deck
    assert "qinf" not in deck["setup_config"]
    assert "advection" not in deck["setup_config"]


# --------------------------------------------------------------------------- #
# Test 11 — tsunami waveform synthesizer (sfincs_forcing_adapter)
# --------------------------------------------------------------------------- #


def test_tsunami_ldn_synth_leads_with_depression(tmp_path) -> None:
    """``synthesize_tsunami_bzs(wave_type="ldn")`` writes a real bzs CSV whose
    series LEADS with a depression (a trough precedes the crest)."""
    import csv as _csv

    from trid3nt_server.workflows.sfincs_forcing_adapter import synthesize_tsunami_bzs

    out = synthesize_tsunami_bzs(
        _MEXICO_BEACH_BBOX,
        eta_max_m=3.0,
        period_s=900.0,
        wave_type="ldn",
        lead_depression=True,
        window_hours=6.0,
        stage_dir=str(tmp_path),
    )
    assert "timeseries_uri" in out and "locations_uri" in out
    # Read the bzs CSV back; the first column is the datetime index, the rest are
    # the per-boundary-point series (all identical for the synthetic edge points).
    with open(out["timeseries_uri"], newline="") as fh:
        rows = list(_csv.reader(fh))
    header, data = rows[0], rows[1:]
    series = [float(r[1]) for r in data]  # first boundary point's series
    # Leading-depression: the global minimum (trough) occurs BEFORE the global
    # maximum (crest).
    i_trough = series.index(min(series))
    i_crest = series.index(max(series))
    assert i_trough < i_crest, "LDN tsunami must lead with a depression (trough first)"
    # The amplitude reaches ~ the requested eta (the derivative-of-Gaussian is
    # normalized to its extremum).
    assert max(series) > 2.5
    assert min(series) < -2.5


def test_tsunami_solitary_synth_is_single_crest(tmp_path) -> None:
    """``wave_type="solitary"`` produces a single positive sech^2 crest with no
    leading trough (the series minimum is ~0, not a deep withdrawal)."""
    import csv as _csv

    from trid3nt_server.workflows.sfincs_forcing_adapter import synthesize_tsunami_bzs

    out = synthesize_tsunami_bzs(
        _MEXICO_BEACH_BBOX,
        eta_max_m=2.0,
        period_s=600.0,
        wave_type="solitary",
        window_hours=4.0,
        stage_dir=str(tmp_path),
    )
    with open(out["timeseries_uri"], newline="") as fh:
        rows = list(_csv.reader(fh))
    series = [float(r[1]) for r in rows[1:]]
    assert max(series) > 1.5  # the crest reaches ~ eta
    assert min(series) >= -0.05  # no leading depression for the solitary wave


def test_tsunami_synth_feeds_waterlevel_deck(tmp_path) -> None:
    """The tsunami synth dict flows through a WaterlevelForcing -> the deck emits
    setup_waterlevel_forcing + setup_mask_bounds(btype waterlevel) so the bzs is
    not inert."""
    from trid3nt_server.workflows.sfincs_forcing_adapter import synthesize_tsunami_bzs

    out = synthesize_tsunami_bzs(
        _MEXICO_BEACH_BBOX,
        eta_max_m=3.0,
        period_s=900.0,
        wave_type="ldn",
        window_hours=6.0,
        stage_dir=str(tmp_path),
    )
    forcing = ForcingSpec(
        forcing_type="storm_surge",
        waterlevel=WaterlevelForcing(
            timeseries_uri=out["timeseries_uri"],
            locations_uri=out["locations_uri"],
        ),
    )
    deck = _emit(forcing, BuildOptions(autoscale_grid=False))
    assert "setup_waterlevel_forcing" in deck
    assert deck["setup_waterlevel_forcing"]["timeseries"] == out["timeseries_uri"]
    # The seaward water-level boundary cells (msk==2) must be created or the bzs
    # is inert (the surge-inundation root cause).
    assert "setup_mask_bounds" in deck
    assert deck["setup_mask_bounds"]["btype"] == "waterlevel"
