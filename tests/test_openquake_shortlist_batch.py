"""ADR 0182 OpenQuake shortlist batch: disaggregation + event-based-PSHA + the
psha Vs30 A/B fold. Pure/offline coverage (deck renderers, CSV parsers, the
consistency reducer, registration + typed-error paths) - no ``oq`` subprocess.

ASCII only.
"""

from __future__ import annotations

import asyncio

import trid3nt_server.main as _main
from trid3nt_server.agent.workflows.openquake._local_oq import (
    render_area_source_model_xml,
    render_classical_point_job_ini,
    render_trivial_gmpe_logic_tree_xml,
    render_trivial_source_logic_tree_xml,
)
from trid3nt_server.agent.workflows.openquake.disaggregation.disaggregation import (
    openquake_disaggregation,
    parse_mag_dist_eps_csv,
    render_disaggregation_job_ini,
)
from trid3nt_server.agent.workflows.openquake.event_based.event_based import (
    _consistency,
    openquake_event_based,
    render_event_based_job_ini,
)

BBOX = (-122.55, 37.55, -122.05, 37.95)


# --------------------------------------------------------------------------- #
# Deck renderers (pure).
# --------------------------------------------------------------------------- #
def test_area_source_xml_is_nrml04_lon_lat():
    xml = render_area_source_model_xml(
        BBOX, a_value=4.0, b_value=1.0, min_magnitude=5.0, max_magnitude=7.5
    )
    assert 'xmlns="http://openquake.org/xmlns/nrml/0.4"' in xml
    assert "<areaSource" in xml and "truncGutenbergRichterMFD" in xml
    # lon-first posList (OpenQuake sourceconverter reads lon lat).
    assert f"{BBOX[0]} {BBOX[1]}" in xml


def test_disaggregation_job_ini():
    ini = render_disaggregation_job_ini(
        site_lon=-122.3, site_lat=37.75, imt="PGA", poe=0.10,
        investigation_time_years=50.0, gmpe_lt_file="gmpe_logic_tree.xml",
        source_lt_file="source_model_logic_tree.xml", max_distance_km=300.0,
        reference_vs30=760.0, mag_bin_width=0.5, distance_bin_width=20.0,
        num_epsilon_bins=3,
    )
    assert "calculation_mode = disaggregation" in ini
    assert "poes_disagg = 0.1" in ini
    assert "num_epsilon_bins = 3" in ini
    assert "disagg_outputs = Mag_Dist_Eps Mag_Dist" in ini
    assert "sites = -122.300000 37.750000" in ini


def test_event_based_job_ini_has_minimum_intensity():
    ini = render_event_based_job_ini(
        bbox=BBOX, imt="PGA", poe=0.10, investigation_time_years=50.0,
        ses_per_logic_tree_path=200, grid_spacing_km=8.0, max_distance_km=300.0,
        reference_vs30=760.0, gmpe_lt_file="gmpe_logic_tree.xml",
        source_lt_file="source_model_logic_tree.xml",
    )
    assert "calculation_mode = event_based" in ini
    assert "ses_per_logic_tree_path = 200" in ini
    # over a grid, the engine requires minimum_intensity (proven by a live run).
    assert "minimum_intensity = 0.05" in ini
    assert "hazard_curves_from_gmfs = true" in ini
    assert "region = " in ini


def test_classical_point_job_ini():
    ini = render_classical_point_job_ini(
        site_lon=-122.3, site_lat=37.75, imt="PGA",
        investigation_time_years=50.0, max_distance_km=300.0, reference_vs30=260.0,
    )
    assert "calculation_mode = classical" in ini
    assert "reference_vs30_value = 260" in ini
    assert "sites = -122.300000 37.750000" in ini


def test_trivial_logic_trees():
    assert "sourceModel" in render_trivial_source_logic_tree_xml()
    assert "gmpeModel" in render_trivial_gmpe_logic_tree_xml("BooreAtkinson2008")
    assert "BooreAtkinson2008" in render_trivial_gmpe_logic_tree_xml("BooreAtkinson2008")


# --------------------------------------------------------------------------- #
# CSV parsers / reducers.
# --------------------------------------------------------------------------- #
_MDE_CSV = (
    "#,,,,,,provenance\n"
    "imt,iml,poe,mag,dist,eps,rlz0\n"
    # M5.25 concentrates in ONE high-epsilon bin (0.030); M6.25 spreads across
    # epsilon summing to 0.045 -> M6.25@10 is the modal CELL, M5.25 the modal BIN.
    "PGA,0.24,0.1,5.25,10.0,2.0,0.030\n"
    "PGA,0.24,0.1,5.25,10.0,0.0,0.000\n"
    "PGA,0.24,0.1,6.25,10.0,-2.0,0.010\n"
    "PGA,0.24,0.1,6.25,10.0,0.0,0.020\n"
    "PGA,0.24,0.1,6.25,10.0,2.0,0.015\n"
    "PGA,0.24,0.1,6.25,30.0,0.0,0.005\n"
)


def test_parse_mag_dist_eps_dominant_is_modal_cell():
    p = parse_mag_dist_eps_csv(_MDE_CSV)
    assert p["iml"] == 0.24
    # dominant CELL summed over epsilon = M6.25 @ 10km (0.045 > 0.030).
    assert p["dominant_magnitude"] == 6.25
    assert p["dominant_distance_km"] == 10.0
    # dominant epsilon within that cell = the 0.020 bin (eps 0.0).
    assert p["dominant_epsilon"] == 0.0
    assert p["n_bins"] == 5  # zero-contribution bin dropped
    # contribution-weighted mean magnitude between 5.25 and 6.25.
    assert 5.25 < p["mean_magnitude"] < 6.25


def test_parse_mag_dist_eps_empty():
    p = parse_mag_dist_eps_csv("#prov\nimt,iml,poe,mag,dist,eps,rlz0\n")
    assert p["n_bins"] == 0


def test_consistency_median_verdict():
    # two nearly-equal curves -> "consistent with"; a tail blow-up must not flip it.
    imls = [0.1, 0.2, 0.4, 0.8, 1.6]
    eb = {"imls": imls, "poe": [0.5, 0.3, 0.1, 0.02, 0.0005]}
    cl = {"imls": imls, "poe": [0.5, 0.31, 0.1, 0.02, 0.0001]}  # 400% at the tail
    note, median_rel = _consistency(eb, cl)
    assert "consistent with" in note
    assert median_rel < 0.25


# --------------------------------------------------------------------------- #
# Registration + typed-error paths.
# --------------------------------------------------------------------------- #
def test_new_tools_registered():
    _main._import_tools_registry()
    from trid3nt_server.agent.tools import TOOL_REGISTRY

    for n in ("openquake_disaggregation", "openquake_event_based"):
        e = TOOL_REGISTRY[n]
        assert e.metadata.tier == "template"
        assert e.metadata.engine == "openquake"
        assert callable(e.fn)


def test_disaggregation_bad_bbox_returns_typed_error():
    out = asyncio.run(openquake_disaggregation(bbox=None))
    assert out["status"] == "error"
    assert out["error_code"] == "DISAGG_PARAMS_INVALID"


def test_event_based_bad_poe_returns_typed_error():
    out = asyncio.run(openquake_event_based(bbox=BBOX, poe=1.5))
    assert out["status"] == "error"
    assert out["error_code"] == "EB_PARAMS_INVALID"
