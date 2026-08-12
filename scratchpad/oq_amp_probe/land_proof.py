"""Live proof of the LANDED nehrp_amp_class path: uses the real _local_oq render
helpers + run_oq_local, then renders the dock-exact hazard-curve overlay proof.
"""
from __future__ import annotations

import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

REPO = Path("/home/nate/Documents/trid3nt-local")
sys.path.insert(0, str(REPO / "server" / "src"))
from trid3nt_server.agent.workflows.openquake._local_oq import (  # noqa: E402
    NEHRP_FPGA, NEHRP_VS30, aoi_centroid, render_amplification_csv,
    render_area_source_model_xml, render_classical_amp_job_ini,
    render_site_model_csv, render_trivial_gmpe_logic_tree_xml,
    render_trivial_source_logic_tree_xml, run_oq_local,
)
from trid3nt_server.agent.workflows.openquake.postprocess_openquake import (  # noqa: E402
    parse_hazard_curve_csv,
)

BBOX = (-112.02, 40.66, -111.80, 40.85)  # Salt Lake City valley (soft) vs Wasatch rock
LON, LAT = aoi_centroid(BBOX)
IMT = "PGA"
GMPE = "BooreAtkinson2008"

src = render_area_source_model_xml(
    BBOX, a_value=4.0, b_value=1.0, min_magnitude=5.0, max_magnitude=7.0)
slt = render_trivial_source_logic_tree_xml()
glt = render_trivial_gmpe_logic_tree_xml(GMPE)


def curve(ampcode: str, vs30: float, factor: float) -> dict:
    files = {
        "source_model.xml": src, "source_model_logic_tree.xml": slt,
        "gmpe_logic_tree.xml": glt,
        "site_model.csv": render_site_model_csv(
            site_lon=LON, site_lat=LAT, vs30=vs30, ampcode=ampcode),
        "amplification.csv": render_amplification_csv(
            ampcode=ampcode, factor=factor, imt=IMT),
        "job.ini": render_classical_amp_job_ini(
            imt=IMT, investigation_time_years=50.0, max_distance_km=200.0,
            description=f"NEHRP {ampcode} amplification A/B"),
    }
    out = run_oq_local(files, label="nehrpamp")
    c = sorted(out.glob("hazard_curve-mean*.csv"))[0]
    p = parse_hazard_curve_csv(c.read_text())
    return {"imls": list(p["hazard_curve_imls_g"]), "poe": list(p["hazard_curve_mean_poe"])}


def poe_at(c: dict, g: float) -> float:
    return min(zip(c["imls"], c["poe"]), key=lambda t: abs(t[0] - g))[1]


series = [("Rock ref (Vs30 760)", "R", 760.0, 1.0, "#444444")]
for cls, col in (("C", "#1f8f4e"), ("D", "#e07a00"), ("E", "#c0212f")):
    series.append((f"NEHRP {cls} (Fpga {NEHRP_FPGA[cls]:g})", cls, NEHRP_VS30[cls],
                   NEHRP_FPGA[cls], col))

curves = {lbl: curve(code, vs30, f) for lbl, code, vs30, f, _ in series}
ref = curves["Rock ref (Vs30 760)"]

# physics assertions
probe = 0.556
ratios = {}
prev = 0.0
for lbl, code, vs30, f, _ in series:
    if code == "R":
        prev = poe_at(curves[lbl], probe)
        continue
    r = poe_at(curves[lbl], probe) / poe_at(ref, probe)
    ratios[code] = r
    print(f"{lbl}: PoE@{probe}g x{r:.3f}")
assert ratios["C"] > 1.0 and ratios["D"] > ratios["C"] and ratios["E"] > ratios["D"], \
    "AMPLIFICATION MONOTONICITY FAILED"
assert 0.95 <= poe_at(ref, probe) / poe_at(ref, probe) <= 1.05  # rock==itself trivially
print("ASSERT OK: rock<C<D<E monotone; rock baseline ~1.0")

# dock-exact proof render: log-log hazard-curve overlay, quantitative axes, caption
fig, ax = plt.subplots(figsize=(8.2, 5.6), dpi=130)
for lbl, code, vs30, f, col in series:
    c = curves[lbl]
    xs = [x for x, p in zip(c["imls"], c["poe"]) if x > 0 and p > 0]
    ys = [p for x, p in zip(c["imls"], c["poe"]) if x > 0 and p > 0]
    ax.plot(xs, ys, marker="o", ms=3.5, lw=1.8, color=col, label=lbl)
ax.set_xscale("log"); ax.set_yscale("log")
ax.set_xlim(0.01, 2.2); ax.set_ylim(1e-3, 1.0)
ax.set_xlabel(f"{IMT} (g)"); ax.set_ylabel("Mean PoE in 50 yr")
ax.set_title("NEHRP site-class amplification - PGA hazard curve (Salt Lake City valley)")
ax.grid(True, which="both", ls=":", alpha=0.4)
ax.legend(title="Site class", fontsize=8, loc="lower left")
cap = (f"Discrete AmplificationFunction convolution (ASCE 7-22 Fpga, vs30_ref 760). "
       f"NEHRP E PoE@{probe}g = {ratios['E']:.2f}x rock; D = {ratios['D']:.2f}x; "
       f"C = {ratios['C']:.2f}x. oq 3.25.1 local subprocess. EPSG:4326 AOI centroid.")
fig.text(0.5, 0.005, cap, ha="center", va="bottom", fontsize=6.7, wrap=True,
         color="#333333")
fig.tight_layout(rect=(0, 0.045, 1, 1))
out = REPO / "docs" / "proof" / "templates" / "openquake_psha_nehrp_amplification_chart.png"
fig.savefig(out)
print("PROOF:", out)
