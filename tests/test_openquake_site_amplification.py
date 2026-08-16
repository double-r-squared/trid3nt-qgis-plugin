"""ADR 0220 OpenQuake site-model batch: the discrete NEHRP site-class
amplification fold (``openquake_psha`` knob ``nehrp_amp_class``).

Pure/offline coverage (published amplification tables, the amplification-deck
renderers, the knob wiring). One end-to-end oq run asserting monotone
rock<C<D<E amplification is included but SKIPPED when the ``oq`` binary is absent.

ASCII only.
"""

from __future__ import annotations

import inspect
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from trid3nt_server.workflows.openquake._local_oq import (
    NEHRP_FPGA,
    NEHRP_VS30,
    aoi_centroid,
    render_amplification_csv,
    render_area_source_model_xml,
    render_classical_amp_job_ini,
    render_site_model_csv,
    render_trivial_gmpe_logic_tree_xml,
    render_trivial_source_logic_tree_xml,
)
from trid3nt_server.workflows.openquake.postprocess_openquake import (
    parse_hazard_curve_csv,
)
from trid3nt_server.workflows.openquake.psha import psha as _psha

BBOX = (-112.02, 40.66, -111.80, 40.85)  # Salt Lake City valley


# --------------------------------------------------------------------------- #
# Published amplification tables.
# --------------------------------------------------------------------------- #
def test_nehrp_fpga_published_values_and_monotone():
    # ASCE 7-22 / FEMA P-2078 Fpga low-intensity site coefficients.
    assert NEHRP_FPGA == {"A": 0.8, "B": 0.9, "C": 1.3, "D": 1.6, "E": 2.4}
    # softer class => larger amplification (monotone through the soft classes).
    assert NEHRP_FPGA["A"] < NEHRP_FPGA["C"] < NEHRP_FPGA["D"] < NEHRP_FPGA["E"]


def test_nehrp_vs30_softer_class_lower_vs30():
    assert NEHRP_VS30["A"] > NEHRP_VS30["C"] > NEHRP_VS30["D"] > NEHRP_VS30["E"]


# --------------------------------------------------------------------------- #
# Deck renderers (pure).
# --------------------------------------------------------------------------- #
def test_site_model_csv_carries_ampcode():
    lon, lat = aoi_centroid(BBOX)
    csv = render_site_model_csv(site_lon=lon, site_lat=lat, vs30=260.0, ampcode="E")
    header, row = csv.strip().splitlines()
    assert header.split(",") == [
        "lon", "lat", "vs30", "vs30measured", "z1pt0", "z2pt5", "ampcode"]
    assert row.endswith(",E")
    assert "260" in row


def test_amplification_csv_convolution_format():
    csv = render_amplification_csv(ampcode="E", factor=2.4, imt="PGA")
    lines = csv.strip().splitlines()
    assert lines[0].startswith("#") and "vs30_ref=760" in lines[0]
    assert lines[1] == "ampcode,PGA,sigma_PGA"
    assert lines[2] == "E,2.4,.0"  # deterministic median coefficient (sigma 0)


def test_amplification_job_ini_wires_convolution():
    ini = render_classical_amp_job_ini(
        imt="PGA", investigation_time_years=50.0, max_distance_km=200.0)
    assert "amplification_method = convolution" in ini
    assert "amplification_csv = amplification.csv" in ini
    assert "site_model_file = site_model.csv" in ini
    assert "soil_intensities =" in ini
    assert "vs30_tolerance =" in ini  # wide, so soft Vs30 is accepted


# --------------------------------------------------------------------------- #
# Knob wiring.
# --------------------------------------------------------------------------- #
def test_nehrp_amp_class_param_on_tool_and_composer():
    assert "nehrp_amp_class" in inspect.signature(_psha.openquake_psha).parameters
    assert "nehrp_amp_class" in inspect.signature(
        _psha.model_openquake_psha).parameters
    assert hasattr(_psha, "_emit_nehrp_amp_chart")


# --------------------------------------------------------------------------- #
# End-to-end amplification physics (SKIPPED when oq is absent).
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(shutil.which("oq") is None, reason="oq binary not on PATH")
def test_amplification_monotone_rock_lt_c_lt_d_lt_e():
    lon, lat = aoi_centroid(BBOX)
    src = render_area_source_model_xml(
        BBOX, a_value=4.0, b_value=1.0, min_magnitude=5.0, max_magnitude=7.0)
    slt = render_trivial_source_logic_tree_xml()
    glt = render_trivial_gmpe_logic_tree_xml("BooreAtkinson2008")

    def poe_at_iml(ampcode: str, vs30: float, factor: float, probe: float) -> float:
        rundir = Path(tempfile.mkdtemp(prefix=f"amptest_{ampcode}_"))
        files = {
            "source_model.xml": src,
            "source_model_logic_tree.xml": slt,
            "gmpe_logic_tree.xml": glt,
            "site_model.csv": render_site_model_csv(
                site_lon=lon, site_lat=lat, vs30=vs30, ampcode=ampcode),
            "amplification.csv": render_amplification_csv(
                ampcode=ampcode, factor=factor, imt="PGA"),
            "job.ini": render_classical_amp_job_ini(
                imt="PGA", investigation_time_years=50.0, max_distance_km=200.0),
        }
        for name, text in files.items():
            (rundir / name).write_text(text)
        env = {"OQ_DATADIR": str(rundir / "oqdata")}
        import os
        env = {**os.environ, **env}
        proc = subprocess.run(
            ["oq", "engine", "--run", "job.ini", "--exports", "csv"],
            cwd=str(rundir), env=env, capture_output=True, text=True, timeout=900)
        assert proc.returncode == 0, (proc.stderr or proc.stdout)[-800:]
        curve = sorted((rundir / "out").glob("hazard_curve-mean*.csv"))[0]
        p = parse_hazard_curve_csv(curve.read_text())
        pairs = list(zip(p["hazard_curve_imls_g"], p["hazard_curve_mean_poe"]))
        return min(pairs, key=lambda t: abs(t[0] - probe))[1]

    probe = 0.556
    rock = poe_at_iml("R", 760.0, 1.0, probe)
    c = poe_at_iml("C", NEHRP_VS30["C"], NEHRP_FPGA["C"], probe)
    d = poe_at_iml("D", NEHRP_VS30["D"], NEHRP_FPGA["D"], probe)
    e = poe_at_iml("E", NEHRP_VS30["E"], NEHRP_FPGA["E"], probe)
    # soft soil amplifies; monotone rock < C < D < E; rock baseline ~1.0.
    assert rock > 0.0
    assert c > rock and d > c and e > d, (rock, c, d, e)
    # class E physically plausible: a meaningful amplification, not runaway.
    assert 1.5 < (e / rock) < 5.0, e / rock
