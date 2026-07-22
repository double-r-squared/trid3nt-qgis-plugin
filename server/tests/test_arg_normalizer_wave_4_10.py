"""Wave 4.10 alias pre-population tests (job B13).

For each of the 14 new Wave 4.10 endpoints: every documented alias in
``_TOOL_SPECIFIC_ALIASES`` normalizes to the canonical parameter name.

Design notes:
- No real tool imports — each test provides a tiny ``fn`` whose signature
  is the canonical contract, exactly like the existing
  ``test_tool_arg_normalizer.py`` style.
- Each test covers one tool and exercises every alias added in B13.
- All tests are named ``test_<tool>_<param>_alias_<variant>`` so failures
  pinpoint the exact alias that broke.
"""

from __future__ import annotations

from typing import Any

import pytest

from trid3nt_server.tool_arg_normalizer import normalize_args


# --------------------------------------------------------------------------- #
# Helpers: tiny fake callables whose signatures are the canonical contracts
# --------------------------------------------------------------------------- #


def _fn_fema_nfhl_zones(
    bbox: tuple | None = None,
    sfha_only: bool = False,
    zone_filter: list | None = None,
) -> Any:
    return None


def _fn_hrrr_forecast(
    bbox: tuple | None = None,
    variable: str = "2m_temperature",
    forecast_hour: int = 1,
    cycle: str | None = None,
) -> Any:
    return None


def _fn_noaa_nwm_streamflow(
    bbox: tuple | None = None,
    product: str = "analysis_assim",
    valid_time: str | None = None,
    forecast_hour: int = 0,
) -> Any:
    return None


def _fn_usace_levees(
    bbox: tuple | None = None,
    layer: str = "leveed_areas",
) -> Any:
    return None


def _fn_usace_dams(
    bbox: tuple | None = None,
) -> Any:
    return None


def _fn_usace_nsi(
    bbox: tuple | None = None,
) -> Any:
    return None


def _fn_asos_metar(
    bbox: tuple | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
) -> Any:
    return None


def _fn_gridmet(
    bbox: tuple | None = None,
    variable: str = "pr",
    start_date: str | None = None,
    end_date: str | None = None,
) -> Any:
    return None


def _fn_noaa_coops_tides(
    bbox: tuple | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    product: str = "water_level",
) -> Any:
    return None


def _fn_noaa_slr_scenarios(
    bbox: tuple | None = None,
    scenario_ft: list | None = None,
) -> Any:
    return None


def _fn_gtsm_tide_surge(
    bbox: tuple | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    output: str = "surge",
    api_key: str | None = None,
    secret_ref: str | None = None,
) -> Any:
    return None


def _fn_raws_weather(
    bbox: tuple | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
) -> Any:
    return None


def _fn_nhdplus_nldi_navigate(
    seed_point: tuple | None = None,
    comid: int | None = None,
    direction: str = "DM",
    distance_km: float = 100.0,
) -> Any:
    return None


def _fn_statsgo_soils(
    bbox: tuple | None = None,
    field: str = "kffact",
    timeout_s: float = 30.0,
) -> Any:
    return None


def _fn_hrrr_smoke(
    bbox: tuple | None = None,
    variable: str = "MASSDEN",
    forecast_hour: int = 1,
    cycle: str | None = None,
) -> Any:
    return None


def _fn_3dep_extra(
    bbox: tuple | None = None,
    resolution: int = 10,
    max_tiles: int = 16,
    timeout_s: float = 120.0,
) -> Any:
    return None


def _fn_usfs_canopy_fuels(
    bbox: tuple | None = None,
    layer: str = "CBH",
) -> Any:
    return None


# =========================================================================== #
# fetch_fema_nfhl_zones
# =========================================================================== #

_NFHL = "fetch_fema_nfhl_zones"


@pytest.mark.parametrize("alias", ["bounding_box", "extent", "bounds"])
def test_fema_nfhl_bbox_alias(alias: str) -> None:
    val = (-82.0, 26.0, -81.5, 26.5)
    out = normalize_args(_NFHL, {alias: val}, _fn_fema_nfhl_zones)
    assert out.get("bbox") == val
    assert alias not in out


@pytest.mark.parametrize("alias", ["sfha", "special_flood_hazard", "sfha_filter", "flood_hazard_only"])
def test_fema_nfhl_sfha_only_alias(alias: str) -> None:
    out = normalize_args(_NFHL, {alias: True}, _fn_fema_nfhl_zones)
    assert out.get("sfha_only") is True
    assert alias not in out


@pytest.mark.parametrize("alias", ["zones", "flood_zones", "zone_codes", "flood_zone_filter", "zone_types"])
def test_fema_nfhl_zone_filter_alias(alias: str) -> None:
    val = ["AE", "VE"]
    out = normalize_args(_NFHL, {alias: val}, _fn_fema_nfhl_zones)
    assert out.get("zone_filter") == val
    assert alias not in out


# =========================================================================== #
# fetch_hrrr_forecast
# =========================================================================== #

_HRRR = "fetch_hrrr_forecast"


@pytest.mark.parametrize("alias", ["bounding_box", "extent", "bounds"])
def test_hrrr_forecast_bbox_alias(alias: str) -> None:
    val = (-82.0, 26.0, -81.5, 26.5)
    out = normalize_args(_HRRR, {alias: val}, _fn_hrrr_forecast)
    assert out.get("bbox") == val


@pytest.mark.parametrize("alias", ["vars", "fields", "variables", "field"])
def test_hrrr_forecast_variable_alias(alias: str) -> None:
    out = normalize_args(_HRRR, {alias: "surface_precip_1hr"}, _fn_hrrr_forecast)
    assert out.get("variable") == "surface_precip_1hr"
    assert alias not in out


@pytest.mark.parametrize("alias", ["fcst_hr", "fhr", "hour", "lead_hour", "lead_time", "forecast_lead"])
def test_hrrr_forecast_forecast_hour_alias(alias: str) -> None:
    out = normalize_args(_HRRR, {alias: 6}, _fn_hrrr_forecast)
    assert out.get("forecast_hour") == 6
    assert alias not in out


@pytest.mark.parametrize("alias", ["cycle_iso", "run_time", "init_time", "cycle_time", "model_run"])
def test_hrrr_forecast_cycle_alias(alias: str) -> None:
    val = "2026-06-09T00:00:00Z"
    out = normalize_args(_HRRR, {alias: val}, _fn_hrrr_forecast)
    assert out.get("cycle") == val
    assert alias not in out


# =========================================================================== #
# fetch_noaa_nwm_streamflow
# =========================================================================== #

_NWM = "fetch_noaa_nwm_streamflow"


@pytest.mark.parametrize("alias", ["bounding_box", "extent", "bounds"])
def test_nwm_streamflow_bbox_alias(alias: str) -> None:
    val = (-81.9, 26.5, -81.6, 26.8)
    out = normalize_args(_NWM, {alias: val}, _fn_noaa_nwm_streamflow)
    assert out.get("bbox") == val


@pytest.mark.parametrize("alias", ["configuration", "model_run", "cfg", "run_type", "model_config"])
def test_nwm_streamflow_product_alias(alias: str) -> None:
    out = normalize_args(_NWM, {alias: "short_range"}, _fn_noaa_nwm_streamflow)
    assert out.get("product") == "short_range"
    assert alias not in out


@pytest.mark.parametrize("alias", ["datetime", "date", "time", "timestamp", "valid_datetime"])
def test_nwm_streamflow_valid_time_alias(alias: str) -> None:
    val = "2026-06-09T12:00:00Z"
    out = normalize_args(_NWM, {alias: val}, _fn_noaa_nwm_streamflow)
    assert out.get("valid_time") == val
    assert alias not in out


@pytest.mark.parametrize("alias", ["fcst_hr", "fhr", "hour", "lead_hour"])
def test_nwm_streamflow_forecast_hour_alias(alias: str) -> None:
    out = normalize_args(_NWM, {alias: 3}, _fn_noaa_nwm_streamflow)
    assert out.get("forecast_hour") == 3
    assert alias not in out


# =========================================================================== #
# fetch_usace_levees
# =========================================================================== #

_LEVEES = "fetch_usace_levees"


@pytest.mark.parametrize("alias", ["bounding_box", "extent", "bounds"])
def test_usace_levees_bbox_alias(alias: str) -> None:
    val = (-90.3, 29.8, -89.9, 30.2)
    out = normalize_args(_LEVEES, {alias: val}, _fn_usace_levees)
    assert out.get("bbox") == val


@pytest.mark.parametrize("alias", ["layer_type", "geometry_type", "levee_type", "feature_type", "dataset"])
def test_usace_levees_layer_alias(alias: str) -> None:
    out = normalize_args(_LEVEES, {alias: "system_routes"}, _fn_usace_levees)
    assert out.get("layer") == "system_routes"
    assert alias not in out


# =========================================================================== #
# fetch_usace_dams
# =========================================================================== #

_DAMS = "fetch_usace_dams"


@pytest.mark.parametrize("alias", ["bounding_box", "extent", "bounds", "region", "area"])
def test_usace_dams_bbox_alias(alias: str) -> None:
    val = (-82.0, 26.0, -81.5, 26.5)
    out = normalize_args(_DAMS, {alias: val}, _fn_usace_dams)
    assert out.get("bbox") == val


# =========================================================================== #
# fetch_usace_nsi
# =========================================================================== #

_NSI = "fetch_usace_nsi"


@pytest.mark.parametrize("alias", ["bounding_box", "extent", "bounds", "region", "area"])
def test_usace_nsi_bbox_alias(alias: str) -> None:
    val = (-82.0, 26.0, -81.5, 26.5)
    out = normalize_args(_NSI, {alias: val}, _fn_usace_nsi)
    assert out.get("bbox") == val


# =========================================================================== #
# fetch_asos_metar
# =========================================================================== #

_ASOS = "fetch_asos_metar"


@pytest.mark.parametrize("alias", ["bounding_box", "extent", "bounds"])
def test_asos_metar_bbox_alias(alias: str) -> None:
    val = (-82.0, 26.0, -81.5, 26.5)
    out = normalize_args(_ASOS, {alias: val}, _fn_asos_metar)
    assert out.get("bbox") == val


@pytest.mark.parametrize("alias", ["start_date", "begin", "start", "from_time", "datetime_start", "time_start"])
def test_asos_metar_start_time_alias(alias: str) -> None:
    val = "2026-06-09T06:00:00Z"
    out = normalize_args(_ASOS, {alias: val}, _fn_asos_metar)
    assert out.get("start_time") == val
    assert alias not in out


@pytest.mark.parametrize("alias", ["end_date", "end", "stop", "to_time", "datetime_end", "time_end"])
def test_asos_metar_end_time_alias(alias: str) -> None:
    val = "2026-06-09T09:00:00Z"
    out = normalize_args(_ASOS, {alias: val}, _fn_asos_metar)
    assert out.get("end_time") == val
    assert alias not in out


# =========================================================================== #
# fetch_gridmet
# =========================================================================== #

_GRIDMET = "fetch_gridmet"


@pytest.mark.parametrize("alias", ["bounding_box", "extent", "bounds"])
def test_gridmet_bbox_alias(alias: str) -> None:
    val = (-117.5, 33.5, -116.5, 34.5)
    out = normalize_args(_GRIDMET, {alias: val}, _fn_gridmet)
    assert out.get("bbox") == val


@pytest.mark.parametrize("alias", ["vars", "field", "variables", "param", "metric"])
def test_gridmet_variable_alias(alias: str) -> None:
    out = normalize_args(_GRIDMET, {alias: "fm100"}, _fn_gridmet)
    assert out.get("variable") == "fm100"
    assert alias not in out


@pytest.mark.parametrize("alias", ["start", "begin", "from_date", "datetime_start", "start_time", "date_start"])
def test_gridmet_start_date_alias(alias: str) -> None:
    val = "2026-06-01"
    out = normalize_args(_GRIDMET, {alias: val}, _fn_gridmet)
    assert out.get("start_date") == val
    assert alias not in out


@pytest.mark.parametrize("alias", ["end", "stop", "to_date", "datetime_end", "end_time", "date_end"])
def test_gridmet_end_date_alias(alias: str) -> None:
    val = "2026-06-09"
    out = normalize_args(_GRIDMET, {alias: val}, _fn_gridmet)
    assert out.get("end_date") == val
    assert alias not in out


# =========================================================================== #
# fetch_noaa_coops_tides
# =========================================================================== #

_COOPS = "fetch_noaa_coops_tides"


@pytest.mark.parametrize("alias", ["bounding_box", "extent", "bounds"])
def test_noaa_coops_bbox_alias(alias: str) -> None:
    val = (-82.0, 26.0, -81.5, 26.5)
    out = normalize_args(_COOPS, {alias: val}, _fn_noaa_coops_tides)
    assert out.get("bbox") == val


@pytest.mark.parametrize("alias", ["start", "begin", "from_date", "start_time", "datetime_start", "date_start"])
def test_noaa_coops_start_date_alias(alias: str) -> None:
    val = "2022-09-28"
    out = normalize_args(_COOPS, {alias: val}, _fn_noaa_coops_tides)
    assert out.get("start_date") == val
    assert alias not in out


@pytest.mark.parametrize("alias", ["end", "stop", "to_date", "end_time", "datetime_end", "date_end"])
def test_noaa_coops_end_date_alias(alias: str) -> None:
    val = "2022-09-29"
    out = normalize_args(_COOPS, {alias: val}, _fn_noaa_coops_tides)
    assert out.get("end_date") == val
    assert alias not in out


@pytest.mark.parametrize("alias", ["data_type", "observation_type", "tide_product", "measurement"])
def test_noaa_coops_product_alias(alias: str) -> None:
    out = normalize_args(_COOPS, {alias: "predictions"}, _fn_noaa_coops_tides)
    assert out.get("product") == "predictions"
    assert alias not in out


# =========================================================================== #
# fetch_noaa_slr_scenarios
# =========================================================================== #

_SLR = "fetch_noaa_slr_scenarios"


@pytest.mark.parametrize("alias", ["bounding_box", "extent", "bounds"])
def test_noaa_slr_bbox_alias(alias: str) -> None:
    val = (-82.0, 26.0, -81.5, 26.5)
    out = normalize_args(_SLR, {alias: val}, _fn_noaa_slr_scenarios)
    assert out.get("bbox") == val


@pytest.mark.parametrize("alias", ["scenario", "scenarios", "sea_level_rise", "slr", "slr_ft", "rise_ft", "feet"])
def test_noaa_slr_scenario_ft_alias(alias: str) -> None:
    val = [1, 2, 3]
    out = normalize_args(_SLR, {alias: val}, _fn_noaa_slr_scenarios)
    assert out.get("scenario_ft") == val
    assert alias not in out


# =========================================================================== #
# fetch_gtsm_tide_surge
# =========================================================================== #

_GTSM = "fetch_gtsm_tide_surge"


@pytest.mark.parametrize("alias", ["bounding_box", "extent", "bounds"])
def test_gtsm_bbox_alias(alias: str) -> None:
    val = (-82.0, 26.0, -81.5, 26.5)
    out = normalize_args(_GTSM, {alias: val}, _fn_gtsm_tide_surge)
    assert out.get("bbox") == val


@pytest.mark.parametrize("alias", ["start", "begin", "from_date", "start_time", "datetime_start"])
def test_gtsm_start_date_alias(alias: str) -> None:
    val = "2022-09-28"
    out = normalize_args(_GTSM, {alias: val}, _fn_gtsm_tide_surge)
    assert out.get("start_date") == val
    assert alias not in out


@pytest.mark.parametrize("alias", ["end", "stop", "to_date", "end_time", "datetime_end"])
def test_gtsm_end_date_alias(alias: str) -> None:
    val = "2022-09-29"
    out = normalize_args(_GTSM, {alias: val}, _fn_gtsm_tide_surge)
    assert out.get("end_date") == val
    assert alias not in out


@pytest.mark.parametrize("alias", ["variable", "product", "data_type", "output_type", "field"])
def test_gtsm_output_alias(alias: str) -> None:
    out = normalize_args(_GTSM, {alias: "tide"}, _fn_gtsm_tide_surge)
    assert out.get("output") == "tide"
    assert alias not in out


# =========================================================================== #
# fetch_raws_weather
# =========================================================================== #

_RAWS = "fetch_raws_weather"


@pytest.mark.parametrize("alias", ["bounding_box", "extent", "bounds"])
def test_raws_weather_bbox_alias(alias: str) -> None:
    val = (-111.5, 40.5, -110.5, 41.5)
    out = normalize_args(_RAWS, {alias: val}, _fn_raws_weather)
    assert out.get("bbox") == val


@pytest.mark.parametrize("alias", ["start_date", "begin", "start", "from_time", "datetime_start", "time_start"])
def test_raws_weather_start_time_alias(alias: str) -> None:
    val = "2026-06-01T00:00:00Z"
    out = normalize_args(_RAWS, {alias: val}, _fn_raws_weather)
    assert out.get("start_time") == val
    assert alias not in out


@pytest.mark.parametrize("alias", ["end_date", "end", "stop", "to_time", "datetime_end", "time_end"])
def test_raws_weather_end_time_alias(alias: str) -> None:
    val = "2026-06-07T00:00:00Z"
    out = normalize_args(_RAWS, {alias: val}, _fn_raws_weather)
    assert out.get("end_time") == val
    assert alias not in out


# =========================================================================== #
# fetch_nhdplus_nldi_navigate
# =========================================================================== #

_NLDI = "fetch_nhdplus_nldi_navigate"


@pytest.mark.parametrize("alias", ["point", "location", "coordinate", "coordinates", "lat_lon", "latlon"])
def test_nhdplus_nldi_seed_point_alias(alias: str) -> None:
    val = (-81.87, 26.65)
    out = normalize_args(_NLDI, {alias: val}, _fn_nhdplus_nldi_navigate)
    assert out.get("seed_point") == val
    assert alias not in out


@pytest.mark.parametrize("alias", ["reach_id", "nhd_id", "feature_id", "nhdplus_id", "nhd_comid"])
def test_nhdplus_nldi_comid_alias(alias: str) -> None:
    out = normalize_args(_NLDI, {alias: 16754658}, _fn_nhdplus_nldi_navigate)
    assert out.get("comid") == 16754658
    assert alias not in out


@pytest.mark.parametrize("alias", ["nav_direction", "navigation", "navigate", "upstream_downstream"])
def test_nhdplus_nldi_direction_alias(alias: str) -> None:
    out = normalize_args(_NLDI, {alias: "UM"}, _fn_nhdplus_nldi_navigate)
    assert out.get("direction") == "UM"
    assert alias not in out


@pytest.mark.parametrize("alias", ["distance", "km", "length_km", "search_distance", "max_distance_km"])
def test_nhdplus_nldi_distance_km_alias(alias: str) -> None:
    out = normalize_args(_NLDI, {alias: 50.0}, _fn_nhdplus_nldi_navigate)
    assert out.get("distance_km") == 50.0
    assert alias not in out


# =========================================================================== #
# fetch_statsgo_soils
# =========================================================================== #

_STATSGO = "fetch_statsgo_soils"


@pytest.mark.parametrize("alias", ["bounding_box", "extent", "bounds"])
def test_statsgo_soils_bbox_alias(alias: str) -> None:
    val = (-82.0, 26.0, -81.5, 26.5)
    out = normalize_args(_STATSGO, {alias: val}, _fn_statsgo_soils)
    assert out.get("bbox") == val


@pytest.mark.parametrize("alias", ["attribute", "variable", "soil_property", "property", "soil_attribute", "soil_field"])
def test_statsgo_soils_field_alias(alias: str) -> None:
    out = normalize_args(_STATSGO, {alias: "kffact"}, _fn_statsgo_soils)
    assert out.get("field") == "kffact"
    assert alias not in out


@pytest.mark.parametrize("alias", ["timeout", "timeout_seconds", "http_timeout", "request_timeout"])
def test_statsgo_soils_timeout_alias(alias: str) -> None:
    out = normalize_args(_STATSGO, {alias: 60.0}, _fn_statsgo_soils)
    assert out.get("timeout_s") == 60.0
    assert alias not in out


# =========================================================================== #
# fetch_hrrr_smoke
# =========================================================================== #

_HRRR_SMOKE = "fetch_hrrr_smoke"


@pytest.mark.parametrize("alias", ["bounding_box", "extent", "bounds"])
def test_hrrr_smoke_bbox_alias(alias: str) -> None:
    val = (-122.5, 39.5, -121.5, 40.5)
    out = normalize_args(_HRRR_SMOKE, {alias: val}, _fn_hrrr_smoke)
    assert out.get("bbox") == val


@pytest.mark.parametrize("alias", ["vars", "field", "variables", "smoke_variable"])
def test_hrrr_smoke_variable_alias(alias: str) -> None:
    out = normalize_args(_HRRR_SMOKE, {alias: "COLMD"}, _fn_hrrr_smoke)
    assert out.get("variable") == "COLMD"
    assert alias not in out


@pytest.mark.parametrize("alias", ["fcst_hr", "fhr", "hour", "lead_hour", "lead_time"])
def test_hrrr_smoke_forecast_hour_alias(alias: str) -> None:
    out = normalize_args(_HRRR_SMOKE, {alias: 3}, _fn_hrrr_smoke)
    assert out.get("forecast_hour") == 3
    assert alias not in out


@pytest.mark.parametrize("alias", ["cycle_iso", "run_time", "init_time", "cycle_time", "model_run"])
def test_hrrr_smoke_cycle_alias(alias: str) -> None:
    val = "2026-06-09T06:00:00Z"
    out = normalize_args(_HRRR_SMOKE, {alias: val}, _fn_hrrr_smoke)
    assert out.get("cycle") == val
    assert alias not in out


# =========================================================================== #
# fetch_3dep_extra
# =========================================================================== #

_3DEP = "fetch_3dep_extra"


@pytest.mark.parametrize("alias", ["bounding_box", "extent", "bounds"])
def test_3dep_extra_bbox_alias(alias: str) -> None:
    val = (-82.0, 26.0, -81.5, 26.5)
    out = normalize_args(_3DEP, {alias: val}, _fn_3dep_extra)
    assert out.get("bbox") == val


@pytest.mark.parametrize("alias", ["res", "cell_size", "pixel_size", "spatial_resolution", "grid_resolution"])
def test_3dep_extra_resolution_alias(alias: str) -> None:
    out = normalize_args(_3DEP, {alias: 1}, _fn_3dep_extra)
    assert out.get("resolution") == 1
    assert alias not in out


@pytest.mark.parametrize("alias", ["tile_limit", "max_tile_count", "tiles", "num_tiles"])
def test_3dep_extra_max_tiles_alias(alias: str) -> None:
    out = normalize_args(_3DEP, {alias: 32}, _fn_3dep_extra)
    assert out.get("max_tiles") == 32
    assert alias not in out


@pytest.mark.parametrize("alias", ["timeout", "timeout_seconds", "http_timeout", "request_timeout"])
def test_3dep_extra_timeout_alias(alias: str) -> None:
    out = normalize_args(_3DEP, {alias: 60.0}, _fn_3dep_extra)
    assert out.get("timeout_s") == 60.0
    assert alias not in out


# =========================================================================== #
# fetch_usfs_canopy_fuels
# =========================================================================== #

_CANOPY = "fetch_usfs_canopy_fuels"


@pytest.mark.parametrize("alias", ["bounding_box", "extent", "bounds"])
def test_usfs_canopy_bbox_alias(alias: str) -> None:
    val = (-117.5, 33.5, -116.5, 34.5)
    out = normalize_args(_CANOPY, {alias: val}, _fn_usfs_canopy_fuels)
    assert out.get("bbox") == val


@pytest.mark.parametrize("alias", ["variable", "fuel_layer", "product", "dataset", "layer_name", "fuel_type"])
def test_usfs_canopy_layer_alias(alias: str) -> None:
    out = normalize_args(_CANOPY, {alias: "CBD"}, _fn_usfs_canopy_fuels)
    assert out.get("layer") == "CBD"
    assert alias not in out
