"""Group-1 qgis_provider rows, mode open: each row registers, its uri builds
against the row's own template, and it refuses by name with no session bound.
Live-network shape (a real service answering) is not asserted here - the
qgis_provider machinery itself is covered in test_router_qgis_provider.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from trid3nt_server.server.processing import SessionUnavailableError
from trid3nt_server.tools import TOOL_REGISTRY, fetchers
from trid3nt_server.tools.fetchers._router.executors import qgis_provider
from trid3nt_server.tools.fetchers._router.spec import compose_specs_from_tree

_BBOX = (-95.8, 29.5, -95.0, 30.1)

_SPECS = compose_specs_from_tree(Path(fetchers.__file__).resolve().parent)


def _spec(name: str):
    return _SPECS[name]


def _assert_open_row(name: str) -> None:
    spec = _spec(name)
    assert spec.ingest["access"] == qgis_provider.ACCESS
    assert spec.ingest["qgis_provider"]["mode"] == "open"
    assert spec.hooks is None or not spec.hooks.model_dump(exclude_none=True)
    assert spec.output.layer_type == "record"
    assert TOOL_REGISTRY[name].metadata.cacheable is False


def _assert_refuses_with_no_session(name: str, **params) -> None:
    import trid3nt_server.render.pipeline_emitter as pe_mod

    orig = pe_mod.current_emitter
    pe_mod.current_emitter = lambda: None
    try:
        with pytest.raises(SessionUnavailableError):
            TOOL_REGISTRY[name].fn(bbox=_BBOX, **params)
    finally:
        pe_mod.current_emitter = orig


def test_usace_dams_is_a_provider_row_in_mode_open() -> None:
    _assert_open_row("fetch_usace_dams")
    spec = _spec("fetch_usace_dams")
    plain = qgis_provider.build_uri(spec, {"bbox": _BBOX})
    assert plain.endswith("NID_v1/FeatureServer/0'")
    filtered = qgis_provider.build_uri(
        spec, {"bbox": _BBOX, "hazard_potential": "High", "min_height_ft": 50.0}
    )
    assert "sql=HAZARD_POTENTIAL='High' AND DAM_HEIGHT >= 50" in filtered


def test_usace_dams_refuses_by_name_with_no_session() -> None:
    _assert_refuses_with_no_session("fetch_usace_dams")


def test_fema_nfhl_zones_is_a_provider_row_in_mode_open() -> None:
    _assert_open_row("fetch_fema_nfhl_zones")
    spec = _spec("fetch_fema_nfhl_zones")
    plain = qgis_provider.build_uri(spec, {"bbox": _BBOX})
    assert plain.endswith("NFHL/MapServer/28'")
    filtered = qgis_provider.build_uri(spec, {"bbox": _BBOX, "zone": "VE"})
    assert filtered.endswith("sql=FLD_ZONE='VE'")


def test_fema_nfhl_zones_refuses_by_name_with_no_session() -> None:
    _assert_refuses_with_no_session("fetch_fema_nfhl_zones")


def test_epa_frs_facilities_is_a_provider_row_per_program() -> None:
    _assert_open_row("fetch_epa_frs_facilities")
    spec = _spec("fetch_epa_frs_facilities")
    tri = qgis_provider.build_uri(spec, {"bbox": _BBOX, "facility_program": "tri"})
    assert tri.endswith("MapServer/15'")
    superfund = qgis_provider.build_uri(
        spec, {"bbox": _BBOX, "facility_program": "superfund"}
    )
    assert superfund.endswith("MapServer/14'")


def test_epa_frs_facilities_refuses_by_name_with_no_session() -> None:
    _assert_refuses_with_no_session("fetch_epa_frs_facilities", facility_program="tri")


def test_hifld_critical_infrastructure_is_a_provider_row_per_type() -> None:
    _assert_open_row("fetch_hifld_critical_infrastructure")
    spec = _spec("fetch_hifld_critical_infrastructure")
    hospitals = qgis_provider.build_uri(
        spec, {"bbox": _BBOX, "facility_type": "hospitals"}
    )
    assert hospitals.endswith("Hospitals/FeatureServer/0'")
    power = qgis_provider.build_uri(
        spec, {"bbox": _BBOX, "facility_type": "power_plants"}
    )
    assert power.endswith("Power_Plants/FeatureServer/0'")


def test_hifld_transmission_lines_is_a_provider_row_in_mode_open() -> None:
    _assert_open_row("fetch_hifld_transmission_lines")
    spec = _spec("fetch_hifld_transmission_lines")
    filtered = qgis_provider.build_uri(
        spec, {"bbox": _BBOX, "min_voltage_kv": 230.0}
    )
    assert filtered.endswith("sql=VOLTAGE >= 230")


def test_mtbs_burn_severity_is_a_provider_row_in_mode_open() -> None:
    _assert_open_row("fetch_mtbs_burn_severity")
    spec = _spec("fetch_mtbs_burn_severity")
    filtered = qgis_provider.build_uri(
        spec, {"bbox": _BBOX, "year_range": [2010, 2020]}
    )
    assert filtered.endswith("sql=YEAR >= 2010 AND YEAR <= 2020")


def test_nifc_fire_perimeters_is_a_provider_row_in_mode_open() -> None:
    _assert_open_row("fetch_nifc_fire_perimeters")
    spec = _spec("fetch_nifc_fire_perimeters")
    plain = qgis_provider.build_uri(spec, {"bbox": _BBOX})
    assert plain.endswith("WFIGS_Interagency_Perimeters_Current/FeatureServer/0'")


def test_usace_levees_is_a_provider_row_per_layer() -> None:
    _assert_open_row("fetch_usace_levees")
    spec = _spec("fetch_usace_levees")
    default = qgis_provider.build_uri(spec, {"bbox": _BBOX, "layer": "leveed_areas"})
    assert default.endswith("FeatureServer/16'")
    routes = qgis_provider.build_uri(spec, {"bbox": _BBOX, "layer": "system_routes"})
    assert routes.endswith("FeatureServer/14'")


def test_nhd_area_water_is_a_provider_row_in_mode_open() -> None:
    _assert_open_row("fetch_nhd_area_water")
    spec = _spec("fetch_nhd_area_water")
    plain = qgis_provider.build_uri(spec, {"bbox": _BBOX})
    assert plain.endswith("NHDPlus_HR/MapServer/8'")


def test_nhd_waterbodies_is_a_provider_row_in_mode_open() -> None:
    _assert_open_row("fetch_nhd_waterbodies")
    spec = _spec("fetch_nhd_waterbodies")
    plain = qgis_provider.build_uri(spec, {"bbox": _BBOX})
    assert plain.endswith("NHDPlus_HR/MapServer/9'")


def test_nhdplus_hr_flowlines_is_a_provider_row_in_mode_open() -> None:
    _assert_open_row("fetch_nhdplus_hr_flowlines")
    spec = _spec("fetch_nhdplus_hr_flowlines")
    plain = qgis_provider.build_uri(spec, {"bbox": _BBOX})
    assert plain.endswith("NHDPlus_HR/MapServer/3'")
    filtered = qgis_provider.build_uri(
        spec, {"bbox": _BBOX, "gnis_name": "Eel River"}
    )
    assert filtered.endswith("sql=UPPER(gnis_name)=UPPER('Eel River')")


def test_noaa_slr_scenarios_is_a_provider_row_per_level() -> None:
    _assert_open_row("fetch_noaa_slr_scenarios")
    spec = _spec("fetch_noaa_slr_scenarios")
    one_ft = qgis_provider.build_uri(spec, {"bbox": _BBOX, "scenario_ft": 1.0})
    assert one_ft.endswith("slr_1ft/MapServer/0'")
    three_ft = qgis_provider.build_uri(spec, {"bbox": _BBOX, "scenario_ft": 3.0})
    assert three_ft.endswith("slr_3ft/MapServer/0'")


def test_noaa_slr_confidence_is_a_provider_row_per_level() -> None:
    _assert_open_row("fetch_noaa_slr_confidence")
    spec = _spec("fetch_noaa_slr_confidence")
    uri = qgis_provider.build_uri(spec, {"bbox": _BBOX, "slr_ft": 3.0})
    assert uri.endswith("conf_3ft/MapServer'")
    assert "format='PNG32'" in uri


def test_noaa_slr_marsh_is_a_provider_row_per_level() -> None:
    _assert_open_row("fetch_noaa_slr_marsh")
    spec = _spec("fetch_noaa_slr_marsh")
    uri = qgis_provider.build_uri(spec, {"bbox": _BBOX, "slr_ft": 0.5})
    assert uri.endswith("marsh_050/MapServer'")


def test_noaa_sst_is_a_provider_row_wms() -> None:
    _assert_open_row("fetch_noaa_sst")
    spec = _spec("fetch_noaa_sst")
    sst = qgis_provider.build_uri(spec, {"bbox": _BBOX, "variable": "CRW_SST"})
    assert "layers='dhw_5km:CRW_SST'" in sst
    anomaly = qgis_provider.build_uri(
        spec, {"bbox": _BBOX, "variable": "CRW_SSTANOMALY"}
    )
    assert "layers='dhw_5km:CRW_SSTANOMALY'" in anomaly


@pytest.mark.parametrize("name, extra", [
    ("fetch_hifld_critical_infrastructure", {"facility_type": "hospitals"}),
    ("fetch_hifld_transmission_lines", {}),
    ("fetch_mtbs_burn_severity", {}),
    ("fetch_nifc_fire_perimeters", {}),
    ("fetch_usace_levees", {}),
    ("fetch_nhd_area_water", {}),
    ("fetch_nhd_waterbodies", {}),
    ("fetch_nhdplus_hr_flowlines", {}),
    ("fetch_noaa_slr_scenarios", {}),
    ("fetch_noaa_slr_confidence", {}),
    ("fetch_noaa_slr_marsh", {}),
    ("fetch_noaa_sst", {}),
])
def test_group1_row_refuses_by_name_with_no_session(name: str, extra: dict) -> None:
    _assert_refuses_with_no_session(name, **extra)
