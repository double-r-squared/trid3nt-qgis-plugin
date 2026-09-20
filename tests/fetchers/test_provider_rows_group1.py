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


@pytest.mark.parametrize("name, extra", [
    ("fetch_hifld_critical_infrastructure", {"facility_type": "hospitals"}),
    ("fetch_hifld_transmission_lines", {}),
    ("fetch_mtbs_burn_severity", {}),
    ("fetch_nifc_fire_perimeters", {}),
    ("fetch_usace_levees", {}),
])
def test_group1_row_refuses_by_name_with_no_session(name: str, extra: dict) -> None:
    _assert_refuses_with_no_session(name, **extra)
