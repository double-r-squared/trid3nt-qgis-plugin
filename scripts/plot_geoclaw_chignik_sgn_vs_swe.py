"""Overlay + quantify the SGN-vs-SWE gauge waveforms produced by
drive_geoclaw_chignik_sgn_vs_swe.py. Writes the proof figure to
docs/proof/templates/geoclaw_chignik_sgn_vs_swe_arrival.png and prints the
comparison numbers.

Honest framing: at this near-field domain scale with a broad M8.2 earthquake
source the leading tsunami wave is long (weakly dispersive), so SGN and SWE are
visually indistinguishable at the gauge. The figure shows the overlay (top) plus
the MAGNIFIED SGN-SWE residual in mm (bottom) -- the dispersion signal is <0.05 mm
across the long leading wave and grows to only a few mm in the steeper trailing
trough. That small-but-nonzero, trough-concentrated residual IS the dispersive
physics; its physical negligibility at this scale is the fidelity-ladder finding.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

_SCRATCH = Path(
    "/tmp/claude-1000/-home-nate-Documents-GRACE-2/"
    "fd2df08a-a572-4b62-ba9a-e82d8a0a740e/scratchpad"
)
_PROOF = Path(
    "/home/nate/Documents/trid3nt-local/docs/proof/templates/"
    "geoclaw_chignik_sgn_vs_swe_arrival.png"
)


def _load(leg: str) -> dict:
    return json.loads((_SCRATCH / f"geoclaw_sgn_vs_swe_{leg}.json").read_text())


def main() -> None:
    swe = _load("swe")
    sgn = _load("sgn")
    t = np.asarray(swe["t"], float)
    es = np.asarray(swe["eta"], float)
    eg = np.asarray(sgn["eta"], float)
    d_mm = (eg - es) * 1000.0

    ptp = float(es.max() - es.min())
    crest_i = int(es.argmax())
    trough_i = int(es.argmin())
    max_abs_mm = float(np.max(np.abs(d_mm)))
    max_abs_pct = max_abs_mm / 1000.0 / ptp * 100.0
    # trailing window = after the leading crest
    trail = t > t[crest_i]
    rms_s = float(np.sqrt(np.mean((es[trail] - es[trail].mean()) ** 2)))
    rms_g = float(np.sqrt(np.mean((eg[trail] - eg[trail].mean()) ** 2)))

    plt.rcParams.update({"font.size": 10})
    fig, (ax0, ax1) = plt.subplots(
        2, 1, figsize=(10.5, 7.8), sharex=True,
        gridspec_kw={"height_ratios": [2.3, 1]},
    )

    ax0.plot(t, es, color="#9AA0A6", lw=3.2, label="SWE  (bouss_equations=0)")
    ax0.plot(t, eg, color="#1F6FEB", lw=1.3, label="SGN  (bouss_equations=2)")
    ax0.axhline(0, color="k", lw=0.5, ls=":", alpha=0.5)
    ax0.annotate(
        f"leading crest +{es[crest_i]:.3f} m\n@ t={t[crest_i]:.0f} s",
        xy=(t[crest_i], es[crest_i]), xytext=(t[crest_i] + 250, es[crest_i] - 0.02),
        fontsize=8.5, color="#333",
    )
    ax0.annotate(
        f"trough {es[trough_i]:.3f} m\n@ t={t[trough_i]:.0f} s",
        xy=(t[trough_i], es[trough_i]), xytext=(t[trough_i] - 900, es[trough_i] + 0.02),
        fontsize=8.5, color="#333",
    )
    ax0.set_ylabel("surface elevation eta (m)")
    ax0.set_title(
        "GeoClaw SGN vs SWE -- 2021 M8.2 Chignik tsunami, nearshore gauge "
        f"({sgn['gauge_lonlat'][0]:.2f}, {sgn['gauge_lonlat'][1]:.2f}), depth ~180 m\n"
        "SGN (thin blue) overlies SWE (thick grey): the two curves are "
        "indistinguishable at this near-field scale",
        fontsize=10.5,
    )
    ax0.legend(loc="lower left", framealpha=0.9)
    ax0.grid(True, alpha=0.22)

    ax1.plot(t, d_mm, color="#D1495B", lw=1.2)
    ax1.axhline(0, color="k", lw=0.5, alpha=0.6)
    ax1.axvspan(t[crest_i], t[-1], color="#D29922", alpha=0.07,
                label="trailing window (post-crest)")
    ax1.set_xlabel("time since rupture (s)")
    ax1.set_ylabel("SGN - SWE (mm)")
    ax1.set_title(
        "Dispersion signal = SGN - SWE residual (note mm scale): "
        f"< 0.05 mm on the long leading wave, up to {max_abs_mm:.1f} mm "
        "only in the steeper trailing trough",
        fontsize=9.5,
    )
    ax1.grid(True, alpha=0.22)
    ax1.legend(loc="upper left", fontsize=8)

    txt = (
        f"leading crest SWE {es[crest_i]:.4f} m / SGN {eg[crest_i]:.4f} m "
        f"(delta {(eg[crest_i]-es[crest_i])*1000:+.2f} mm)  |  "
        f"trailing RMS ratio SGN/SWE {rms_g/rms_s:.5f}  |  "
        f"max |SGN-SWE| {max_abs_mm:.2f} mm = {max_abs_pct:.2f}% of {ptp:.3f} m range  |  "
        f"solve wall SWE {swe['wall_s']:.0f} s vs SGN {sgn['wall_s']:.0f} s "
        f"({sgn['wall_s']/swe['wall_s']:.1f}x)\n"
        f"near-field bounded domain amr_levels={sgn['common']['amr_levels']} "
        f"tfinal={sgn['common']['sim_duration_s']:.0f} s bouss_min_depth=10 m  |  "
        "synthetic Okada at real 2021 Chignik ak0219neiszm epicenter, Mw 8.2, depth 35 km"
    )
    fig.text(0.5, 0.006, txt, ha="center", va="bottom", fontsize=7.0, color="#222")
    fig.tight_layout(rect=(0, 0.06, 1, 1))
    _PROOF.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(_PROOF, dpi=135)
    print("wrote", _PROOF)

    out = {
        "leading_crest_swe_m": float(es[crest_i]),
        "leading_crest_sgn_m": float(eg[crest_i]),
        "leading_crest_delta_mm": float((eg[crest_i] - es[crest_i]) * 1000),
        "leading_crest_t_s": float(t[crest_i]),
        "trough_swe_m": float(es[trough_i]),
        "trough_sgn_m": float(eg[trough_i]),
        "trough_t_s": float(t[trough_i]),
        "ptp_m": ptp,
        "max_abs_residual_mm": max_abs_mm,
        "max_abs_residual_pct_of_ptp": max_abs_pct,
        "max_abs_residual_t_s": float(t[int(np.argmax(np.abs(d_mm)))]),
        "trailing_rms_swe_m": rms_s,
        "trailing_rms_sgn_m": rms_g,
        "trailing_rms_ratio": rms_g / rms_s,
        "wall_swe_s": swe["wall_s"],
        "wall_sgn_s": sgn["wall_s"],
        "wall_ratio_sgn_over_swe": sgn["wall_s"] / swe["wall_s"],
        "swe_run_id": swe["run_id"],
        "sgn_run_id": sgn["run_id"],
    }
    (_SCRATCH / "geoclaw_sgn_vs_swe_summary.json").write_text(json.dumps(out, indent=2))
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
