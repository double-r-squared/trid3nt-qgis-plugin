"""ADR 0164: OpenQuake scenario-GMF + secondary-perils template unit tests.

Fast, hermetic coverage of the pure helpers (rupture/deck rendering, avg-GMF
parsing, Wald-Allen Vs30, terrain covariates, the openquake.sep model wrappers)
plus registration/contracts. One end-to-end oq run is included but SKIPPED when
the ``oq`` binary is absent.

ASCII only.
"""

from __future__ import annotations

import shutil

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.openquake_contracts import (
    ScenarioGmfLayerURI,
    SecondaryPerilLayerURI,
)

from trid3nt_server.agent.workflows.openquake.scenario_gmf.scenario_gmf import (
    ScenarioRupture,
    _parse_avg_gmf_csv,
    render_scenario_job_ini,
    render_scenario_rupture_xml,
    resolve_scenario_rupture,
    run_scenario_gmf,
)
from trid3nt_server.agent.workflows.openquake.secondary_perils.secondary_perils import (
    _landslide_probability,
    _liquefaction_probability,
    compute_site_covariates,
    wald_allen_vs30_active,
)


# --------------------------------------------------------------------------- #
# Contracts
# --------------------------------------------------------------------------- #
def test_scenario_gmf_layer_is_layer_uri_round_trip():
    layer = ScenarioGmfLayerURI(
        layer_id="scenario-gmf-mean-01ABC", name="Scenario ground motion",
        layer_type="raster", uri="s3://runs/01ABC/scenario_gmf_mean_4326.tif",
        style_preset="continuous_seismic_pga", role="primary", units="g",
        bbox=(-122.3, 37.7, -122.1, 37.9), imt="PGA", magnitude=6.9,
        num_ground_motion_fields=100, max_mean_value=0.58,
        median_spread_factor=1.68, n_sites=55, rupture_kind="real-fault",
        rupture_note="Rupture placed on the Hayward fault trace.",
    )
    assert isinstance(layer, LayerURI)
    assert layer.magnitude == pytest.approx(6.9)
    assert ScenarioGmfLayerURI.model_validate_json(layer.model_dump_json()) == layer


def test_secondary_peril_layer_round_trip():
    layer = SecondaryPerilLayerURI(
        layer_id="eq-liquefaction-01ABC", name="Liquefaction probability",
        layer_type="raster", uri="s3://runs/01ABC/liq.tif",
        style_preset="continuous_liquefaction_probability", role="primary",
        units="probability", bbox=(-122.3, 37.7, -122.1, 37.9),
        peril="liquefaction", model_name="zhu_etal_2015_general",
        max_probability=0.79, mean_probability=0.08, exceedance_area_km2=78.0,
        n_sites=55, magnitude=6.9, site_data_note="PGA from OpenQuake ...",
    )
    assert isinstance(layer, LayerURI)
    assert layer.peril == "liquefaction"
    assert SecondaryPerilLayerURI.model_validate_json(layer.model_dump_json()) == layer


# --------------------------------------------------------------------------- #
# Rupture + deck rendering
# --------------------------------------------------------------------------- #
def _demo_rupture() -> ScenarioRupture:
    return ScenarioRupture(
        trace=[[-122.0, 37.5], [-121.8, 37.7]], magnitude=6.7, rake=0.0, dip=90.0,
        upper_depth_km=2.0, lower_depth_km=14.0, hypocenter_depth_km=8.0,
        kind="synthetic", note="demo",
    )


def test_render_scenario_rupture_xml_is_valid_nrml():
    xml = render_scenario_rupture_xml(_demo_rupture())
    assert "<simpleFaultRupture>" in xml
    assert "<magnitude>6.7</magnitude>" in xml
    assert "-122.000000 37.500000" in xml
    assert "<dip>90</dip>" in xml
    # Parses as XML.
    import xml.etree.ElementTree as ET

    ET.fromstring(xml)


def test_render_scenario_job_ini_has_scenario_recipe():
    ini = render_scenario_job_ini(
        bbox=(-122.15, 37.4, -121.65, 37.85), imt="PGA",
        gsim="BooreAtkinson2008", num_gmfs=100, reference_vs30=600.0,
        grid_spacing_km=4.0, max_distance_km=200.0,
    )
    assert "calculation_mode = scenario" in ini
    assert "rupture_model_file = rupture_model.xml" in ini
    assert "ground_motion_correlation_model = JB2009" in ini
    assert "number_of_ground_motion_fields = 100" in ini
    # PGA + PGV always exported (the sep models need both).
    assert "intensity_measure_types = PGA, PGV" in ini
    assert "region_grid_spacing = 4" in ini


def test_resolve_scenario_rupture_caller_trace_wins():
    rup = resolve_scenario_rupture(
        [-122.3, 37.7, -122.1, 37.9], 6.5,
        rupture_trace=[[-122.25, 37.72], [-122.15, 37.88]], rake=10.0, dip=80.0,
    )
    assert rup.trace == [[-122.25, 37.72], [-122.15, 37.88]]
    assert rup.magnitude == pytest.approx(6.5)
    assert rup.dip == pytest.approx(80.0)


def test_parse_avg_gmf_csv():
    text = (
        "#,,,,,,\"generated_by='OpenQuake 3.25.1'\"\n"
        "custom_site_id,lon,lat,gmv_PGA,gsd_PGA,gmv_PGV,gsd_PGV\n"
        "a,-122.1,37.5,0.19,1.72,10.7,1.74\n"
        "b,-122.1,37.6,0.20,1.75,12.4,1.67\n"
    )
    sites, imts = _parse_avg_gmf_csv(text)
    assert len(sites) == 2
    assert set(imts) == {"PGA", "PGV"}
    assert sites[0]["gmv_PGA"] == pytest.approx(0.19)
    assert sites[1]["gsd_PGV"] == pytest.approx(1.67)


# --------------------------------------------------------------------------- #
# Secondary-peril covariates + models
# --------------------------------------------------------------------------- #
def test_wald_allen_vs30_monotone_bins():
    grad = np.array([0.0, 1e-3, 5e-3, 0.02, 0.06, 0.2])
    vs30 = wald_allen_vs30_active(grad)
    # Steeper gradient -> stiffer (non-decreasing) Vs30; flat -> soft sediment.
    assert np.all(np.diff(vs30) >= 0)
    assert vs30[0] == pytest.approx(180.0)
    assert vs30[-1] == pytest.approx(900.0)


def test_liquefaction_and_landslide_models_in_unit_interval():
    pga = np.array([0.15, 0.25, 0.4])
    liq = _liquefaction_probability(pga, 6.7, cti=np.array([10.0, 12.0, 14.0]),
                                    vs30=np.array([200.0, 240.0, 300.0]))
    assert liq.shape == (3,)
    assert np.all((liq >= 0.0) & (liq <= 1.0))
    # Soft wet flats with 0.25-0.4g must show meaningful liquefaction.
    assert liq.max() > 0.1

    slope = np.radians(np.array([20.0, 30.0, 40.0]))
    lsl = _landslide_probability(np.array([0.3, 0.3, 0.3]), 6.7, slope, 0.2)
    assert lsl.shape == (3,)
    assert np.all((lsl >= 0.0) & (lsl <= 1.0))
    # Steep weak slopes under 0.3g must show non-zero landslide probability.
    assert lsl.max() > 0.0


def test_compute_site_covariates_on_synthetic_dem(tmp_path):
    n = 80
    yy, xx = np.mgrid[0:n, 0:n]
    dem = (np.abs(xx - 40) * 4.0 + yy * 0.3).astype("float32")  # V-shaped valley
    tf = from_bounds(-122.2, 37.7, -122.0, 37.9, n, n)
    p = tmp_path / "dem.tif"
    with rasterio.open(
        p, "w", driver="GTiff", height=n, width=n, count=1, dtype="float32",
        crs="EPSG:4326", transform=tf,
    ) as d:
        d.write(dem, 1)
    lons = np.array([-122.10, -122.05, -122.15])
    lats = np.array([37.80, 37.75, 37.85])
    cov = compute_site_covariates(str(p), lons, lats)
    assert cov.slope_rad.shape == (3,)
    assert np.all(np.isfinite(cov.slope_rad))
    assert np.all(cov.vs30 >= 180.0)
    assert np.all(np.isfinite(cov.cti))
    assert "richdem" in cov.cti_source or "default" in cov.cti_source


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #
def test_templates_registered_engine_openquake():
    import trid3nt_server.main as _main

    _main._import_tools_registry()
    from trid3nt_server.agent.tools import TOOL_REGISTRY

    for name in ("openquake_scenario_gmf", "openquake_secondary_perils"):
        e = TOOL_REGISTRY[name]
        assert e.metadata.tier == "template"
        assert e.metadata.engine == "openquake"
        assert e.metadata.cacheable is False
        assert callable(e.fn)


# --------------------------------------------------------------------------- #
# End-to-end scenario run (skipped when oq is absent)
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(shutil.which("oq") is None, reason="oq binary not on PATH")
def test_run_scenario_gmf_end_to_end_tiny():
    rup = ScenarioRupture(
        trace=[[-122.12, 37.72], [-122.02, 37.82]], magnitude=6.5, rake=0.0,
        dip=90.0, upper_depth_km=2.0, lower_depth_km=12.0,
        hypocenter_depth_km=7.0, kind="synthetic", note="test",
    )
    res = run_scenario_gmf(
        bbox=(-122.15, 37.70, -122.00, 37.85), magnitude=6.5, imt="PGA",
        num_gmfs=20, gsim="BooreAtkinson2008", reference_vs30=600.0,
        site_grid_spacing_km=6.0, max_distance_km=150.0, rupture=rup,
    )
    assert res.sites, "scenario run produced no sites"
    assert "PGA" in res.imts
    assert all("gmv_PGA" in s and "gsd_PGA" in s for s in res.sites)
    assert res.magnitude == pytest.approx(6.5)
