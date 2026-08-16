"""Sandbox proof: OQ AmplificationFunction convolution at one site.

Runs a classical-PSHA point deck over a synthetic G-R area source TWICE: once on
rock (ampcode A, factor ~1) and once on a soft NEHRP class E (published ASCE 7-22
Fpga site coefficient), and reports the amplification factor at the target PoE.
Proves the discrete-amplification mechanism on our own demo source (not case_55).
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path("/home/nate/Documents/trid3nt-local")
sys.path.insert(0, str(REPO / "server" / "src"))
from trid3nt_server.workflows.openquake._local_oq import (  # noqa: E402
    DEFAULT_IMLS_G,
    aoi_centroid,
    imls_list_str,
    render_area_source_model_xml,
    render_trivial_gmpe_logic_tree_xml,
    render_trivial_source_logic_tree_xml,
)
from trid3nt_server.workflows.openquake.postprocess_openquake import (  # noqa: E402
    parse_hazard_curve_csv,
)

OQ_BIN = str(REPO / "venvs" / "agent" / "bin" / "oq")

# Salt Lake basin soft-soil vs Wasatch rock: a real soft-vs-rock US contrast.
BBOX = (-112.02, 40.66, -111.80, 40.85)  # Salt Lake City valley
LON, LAT = aoi_centroid(BBOX)

# ASCE 7-22 Fpga (PGA site coefficient) at low intensity, published NEHRP classes.
# Reference rock = 760 m/s (site class B/C boundary).
AMP = {"A": 0.8, "C": 1.3, "D": 1.6, "E": 2.4}


def site_model_csv(ampcode: str, vs30: float) -> str:
    return (
        "lon,lat,vs30,vs30measured,z1pt0,z2pt5,ampcode\n"
        f"{LON:.5f},{LAT:.5f},{vs30:g},1,50.0,1.0,{ampcode}\n"
    )


def amplification_csv(ampcode: str, factor: float) -> str:
    # vs30_ref header line the convolution parser reads; factor per IMT + sigma.
    return f'#,,,vs30_ref=760\nampcode,PGA,sigma_PGA\n{ampcode},{factor:g},.0\n'


def job_ini() -> str:
    iml = imls_list_str(DEFAULT_IMLS_G)
    return (
        "[general]\ndescription = amp probe\ncalculation_mode = classical\n"
        "random_seed = 23\n\n[logic_tree]\nnumber_of_logic_tree_samples = 0\n\n"
        "[erf]\nrupture_mesh_spacing = 5\nwidth_of_mfd_bin = 0.2\n"
        "area_source_discretization = 10.0\n\n[site_params]\n"
        "site_model_file = site_model.csv\n\n[calculation]\n"
        "source_model_logic_tree_file = source_model_logic_tree.xml\n"
        "gsim_logic_tree_file = gmpe_logic_tree.xml\ninvestigation_time = 50.0\n"
        f'intensity_measure_types_and_levels = {{"PGA": [{iml}]}}\n'
        "soil_intensities = 0.01 0.05 0.1 0.2 0.4 0.8 1.2\n"
        "truncation_level = 3\nmaximum_distance = 200.0\n"
        "amplification_csv = amplification.csv\namplification_method = convolution\n"
        "vs30_tolerance = 2000\n\n[output]\nexport_dir = out\nmean = true\n"
    )


def run(ampcode: str, vs30: float, factor: float) -> dict:
    rundir = Path(tempfile.mkdtemp(prefix=f"ampprobe_{ampcode}_"))
    files = {
        "source_model.xml": render_area_source_model_xml(
            BBOX, a_value=4.0, b_value=1.0, min_magnitude=5.0, max_magnitude=7.0),
        "source_model_logic_tree.xml": render_trivial_source_logic_tree_xml(),
        "gmpe_logic_tree.xml": render_trivial_gmpe_logic_tree_xml("BooreAtkinson2008"),
        "site_model.csv": site_model_csv(ampcode, vs30),
        "amplification.csv": amplification_csv(ampcode, factor),
        "job.ini": job_ini(),
    }
    for name, text in files.items():
        (rundir / name).write_text(text)
    env = dict(os.environ)
    env["OQ_DATADIR"] = str(rundir / "oqdata")
    proc = subprocess.run(
        [OQ_BIN, "engine", "--run", "job.ini", "--exports", "csv"],
        cwd=str(rundir), env=env, capture_output=True, text=True, timeout=900)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-12:]
        print(f"[{ampcode}] FAILED rc={proc.returncode}\n" + "\n".join(tail))
        return {}
    curves = sorted((rundir / "out").glob("hazard_curve-mean*.csv"))
    parsed = parse_hazard_curve_csv(curves[0].read_text())
    return {
        "imls": list(parsed.get("hazard_curve_imls_g") or []),
        "poe": list(parsed.get("hazard_curve_mean_poe") or []),
    }


def iml_at(curve: dict, target_poe: float) -> float | None:
    best = None
    for x, p in zip(curve["imls"], curve["poe"]):
        if p > 0 and x > 0:
            if best is None or abs(p - target_poe) < best[0]:
                best = (abs(p - target_poe), x)
    return best[1] if best else None


def poe_at(curve: dict, target_iml: float) -> float | None:
    best = None
    for x, p in zip(curve["imls"], curve["poe"]):
        if best is None or abs(x - target_iml) < best[0]:
            best = (abs(x - target_iml), p)
    return best[1] if best else None


if __name__ == "__main__":
    fixed_imls = [0.145, 0.284, 0.556]  # PGA g probe points on the ladder
    rock = run("A", 760.0, AMP["A"])
    rock_poes = {g: poe_at(rock, g) for g in fixed_imls}
    print(f"ROCK (class A, Fpga {AMP['A']}): "
          + "  ".join(f"PoE@{g}g={rock_poes[g]:.4g}" for g in fixed_imls))
    for cls in ("C", "D", "E"):
        vs30 = {"C": 540.0, "D": 260.0, "E": 150.0}[cls]
        soil = run(cls, vs30, AMP[cls])
        parts = []
        for g in fixed_imls:
            sp = poe_at(soil, g)
            rp = rock_poes[g]
            ratio = (sp / rp) if (sp and rp) else float("nan")
            parts.append(f"PoE@{g}g={sp:.4g}(x{ratio:.2f})")
        print(f"NEHRP {cls} (vs30 {vs30:g}, Fpga {AMP[cls]}): " + "  ".join(parts))
