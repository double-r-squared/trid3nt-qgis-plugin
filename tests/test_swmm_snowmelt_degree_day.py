"""SWMM Snow Pack degree-day melt, declared (ADR 0307).

Offline coverage for the declared forcing, the deck writer, the plan shape and
the answer the three variants produce. Every deck here is a few days of hourly
steps on one subcatchment, so the native engine runs inside the offline suite
rather than behind a flag.
"""
from __future__ import annotations

import pytest

from trid3nt_server.workflows.swmm.snowmelt_degree_day.snowmelt_degree_day import (
    PARAMS,
    plan,
    swmm_snowmelt_degree_day,
)
from trid3nt_server.workflows.swmm.snowmelt_degree_day.steps import (
    RAIN_ONLY_DIVIDING_TEMP_F,
    build_snowmelt_inp,
    rain_on_snow_forcing,
)

_DECK = dict(
    cmin=0.001, cmax=0.01, base_temp_f=32.0, dividing_temp_f=32.0,
    percent_impervious=80.0, plow_fraction=0.90, removal=False,
    plow_threshold_in=0.3, plow_out_fraction=1.0, free_water_fraction=0.10,
    initial_snow_depth_in=0.0, initial_free_water_in=0.0,
    depth_at_full_cover_in=2.0, ati_weight=0.5, negative_melt_ratio=0.6,
    site_elevation_ft=500.0, site_latitude_deg=43.0,
    longitude_correction_min=0.0, evaporation_in_day=0.0,
    horton_max_rate_in_hr=3.0, horton_min_rate_in_hr=0.5,
    horton_decay_per_hr=4.0, horton_dry_time_days=7.0,
)

_FORCING = dict(
    dt_min=60, sim_days=5.0, cold_temp_f=20.0, warm_temp_f=45.0,
    warmup_start_hr=48.0, warmup_end_hr=60.0, snowfall_start_hr=12.0,
    snowfall_end_hr=36.0, snowfall_intensity_in_hr=0.05,
    rain_start_hr=60.0, rain_end_hr=72.0, rain_intensity_in_hr=0.15,
)


# --- the declared forcing --------------------------------------------------- #
@pytest.mark.asyncio
async def test_forcing_is_cold_then_warm_with_snow_then_rain():
    out = await rain_on_snow_forcing(**_FORCING)
    temp = dict(out["temperature"])
    rain = dict(out["rainfall"])
    assert temp["0:00"] == 20.0            # cold spell
    assert temp["24:00"] == 20.0
    assert temp["60:00"] == 45.0           # ramp complete
    assert rain["0:00"] == 0.0             # dry before the snowfall window
    assert rain["12:00"] == 0.05           # snowfall (sub-freezing -> snow)
    assert rain["36:00"] == 0.0            # dry between
    assert rain["60:00"] == 0.15           # the warm rain burst
    assert len(out["temperature"]) == 120  # 5 days at 60 min


@pytest.mark.asyncio
async def test_explicit_series_supersedes_only_what_is_supplied():
    """One series may be real observations while the other stays declared."""
    out = await rain_on_snow_forcing(
        **_FORCING, temperature_series_f=[["0:00", 10.0], ["1:00", 11.0]])
    assert out["temperature"] == [("0:00", 10.0), ("1:00", 11.0)]
    assert len(out["rainfall"]) == 120


@pytest.mark.asyncio
async def test_malformed_series_refuses_rather_than_reverting():
    from trid3nt_server.workflows.swmm.steps import SwmmDeckError

    with pytest.raises(SwmmDeckError):
        await rain_on_snow_forcing(
            **_FORCING, rainfall_series_in_hr=[["0:00", "not-a-rate"]])


# --- the deck --------------------------------------------------------------- #
def test_deck_carries_the_snowpack_and_the_degree_day_block():
    inp = build_snowmelt_inp([("0:00", 20.0), ("1:00", 21.0)],
                             [("0:00", 0.05), ("1:00", 0.05)], 60, 50.0, **_DECK)
    assert "[SNOWPACKS]" in inp
    assert "SP1 PLOWABLE   0.001 0.01 32.0 0.10 0 0 0.9" in inp
    assert "SNOWMELT 32.0 0.5 0.6 500 43.0 0" in inp
    assert "REMOVAL" not in inp


def test_removal_variant_adds_the_plow_line():
    inp = build_snowmelt_inp([("0:00", 20.0), ("1:00", 21.0)],
                             [("0:00", 0.05), ("1:00", 0.05)], 60, 50.0,
                             **{**_DECK, "removal": True})
    assert "SP1 REMOVAL 0.3 1.0 0 0 0 0" in inp


def test_rain_only_control_drops_the_dividing_temperature_below_everything():
    inp = build_snowmelt_inp(
        [("0:00", -40.0)], [("0:00", 0.05)], 60, 50.0,
        **{**_DECK, "dividing_temp_f": RAIN_ONLY_DIVIDING_TEMP_F})
    assert f"SNOWMELT {RAIN_ONLY_DIVIDING_TEMP_F}" in inp


# --- the declared plan ------------------------------------------------------ #
@pytest.mark.asyncio
async def test_plan_is_one_forcing_and_three_variants():
    from trid3nt_server.declarative import resolve_params, validate_plan

    p = await resolve_params(PARAMS, {})
    built = plan(p, None)
    validate_plan(built, PARAMS, (), sheet=p)
    labels = [s.label for s in built.flat()]
    assert labels == ["form", "forcing", "deck_snow", "solve_snow",
                      "deck_rain", "solve_rain", "deck_plow", "solve_plow",
                      "snowmelt"]
    charts = [c.name for s in built.flat() for c in s.charts]
    assert charts == ["swe_series", "runoff_snowmelt_vs_rain_only"]


@pytest.mark.asyncio
async def test_no_physics_param_rests_on_a_labeled_default():
    """Law 9, structurally: nothing here claims to be a site measurement."""
    from trid3nt_server.declarative.params import doors

    offenders = [q.name for q in PARAMS
                 if q.consequence == "physics"
                 and q.door in (doors.SCENARIO, doors.CONSTANT)]
    assert offenders == []


# --- the answer ------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_rain_on_snow_amplifies_the_peak_over_the_rain_only_control():
    res = await swmm_snowmelt_degree_day()
    assert res["status"] == "ok", res
    assert res["peak_swe_in"] > 0                      # a pack formed
    assert res["total_melt_in"] > 0                    # and melted
    assert res["rain_on_snow_peak_amplification"] > 1  # stacked over rain-only
    # the climate-naive model fabricates runoff during the cold spell
    assert res["cold_period_runoff_fraction_rain_only"] > 0
    # plowing removes snow, so the plowed pack peaks lower
    assert res["removal_peak_swe_in"] < res["peak_swe_in"]
    assert abs(res["continuity_error_pct"]) < 1.0
    assert res["chart_specs"] == ["runoff_snowmelt_vs_rain_only", "swe_series"]


@pytest.mark.asyncio
async def test_a_dividing_temperature_below_the_cold_spell_builds_no_pack():
    """The dividing temperature is the single switch, and it is honest.

    Dropped below the cold-spell temperature, every drop is rain - which is
    exactly what the built-in rain-only control does, so the physical run and
    its control coincide and the amplification collapses to 1.
    """
    res = await swmm_snowmelt_degree_day(dividing_temp_f=10.0, sim_days=4.0)
    assert res["status"] == "ok", res
    assert res["peak_swe_in"] == 0.0
    assert res["rain_on_snow_peak_amplification"] == pytest.approx(1.0, abs=0.01)


@pytest.mark.asyncio
async def test_a_cold_spell_above_freezing_is_clamped_by_the_declaration():
    """``cold_temp_f`` is the SUB-FREEZING spell; its bound says so and holds."""
    from trid3nt_server.declarative import resolve_params

    p = await resolve_params(PARAMS, {"cold_temp_f": 40.0})
    row = p.row("cold_temp_f")
    assert row.value == 32.0
    assert row.clamped_from == 40.0
    assert "CLAMPED" in row.note


def test_registered():
    from trid3nt_server.data import TOOL_REGISTRY
    assert "swmm_snowmelt_degree_day" in TOOL_REGISTRY
