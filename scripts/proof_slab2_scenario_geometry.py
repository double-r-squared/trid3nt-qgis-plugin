"""Slab2 SCENARIO geometry proof (the curved-interface money shot), EPSG:3857
over Esri World Imagery.

Renders the Cascadia M9.0 scenario SUBFAULT tiling directly from resolve_slab2_scenario
(no solver needed): every subfault centroid drawn at its real Slab2 position and
colored by its Tukey-tapered slip. The panel demonstrates, without a run-up solve, the
two things the scenario rung exists for:
  * the rupture FOLLOWS THE CURVED TRENCH (centroid lon migrates with latitude) -- NOT
    a straight fixed-lon bar;
  * the slip is TAPERED (peak in the interior, zero at the edges) and sums to M9.0.

Run (repo root, MinIO env not required -- basemap tiles only):
  venvs/agent/bin/python scripts/proof_slab2_scenario_geometry.py
"""
from __future__ import annotations

import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
from _slab2_fixture import write_cascadia_fixture, cascadia_trench_lon  # noqa: E402
import proof_geoclaw_chignik_runup as P  # noqa: E402  (basemap helpers)
from trid3nt_server.workflows.geoclaw.scenario_slab2 import (  # noqa: E402
    parse_slab2_grids,
    resolve_slab2_scenario,
    RIGIDITY_PA,
    moment_to_mw,
)

OUT_DIR = "/home/nate/Documents/trid3nt-local/docs/proof/templates"


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    import tempfile
    d = tempfile.mkdtemp()
    paths = write_cascadia_fixture(d)
    grids = parse_slab2_grids(paths["dep"], paths["str"], paths["dip"], zone_code="cas")
    m = resolve_slab2_scenario("Cascadia", 9.0, epicenter_lonlat=(-125.5, 45.0),
                               target_resolution_m=20000.0, grids=grids)
    lons = np.array([p.lon for p in m.patches])
    lats = np.array([p.lat for p in m.patches])
    slips = np.array([p.slip_m for p in m.patches])
    m0 = float(sum(RIGIDITY_PA * p.length_m * p.width_m * p.slip_m for p in m.patches))
    corr = float(np.corrcoef(lons, lats)[0, 1])
    print(f"subfaults={m.n_subfaults} slip {slips.min():.1f}-{slips.max():.1f} m "
          f"realized Mw={moment_to_mw(m0):.3f} corr(lon,lat)={corr:.3f}")

    w, s, e, n = lons.min() - 0.6, lats.min() - 0.4, lons.max() + 0.6, lats.max() + 0.4
    base, bext = P._basemap_for((w, s, e, n))
    fig, ax = plt.subplots(figsize=(8, 9))
    ax.imshow(base, extent=[bext[0], bext[1], bext[2], bext[3]], origin="upper")
    xs, ys = P.TO_3857.transform(lons, lats)
    sc = ax.scatter(xs, ys, c=slips, s=26, cmap="inferno", vmin=0.0,
                    vmax=float(slips.max()), edgecolors="none", alpha=0.9)
    # the real Slab2 trench trace (the curve the rupture follows)
    tl = np.linspace(lats.min(), lats.max(), 60)
    tx, ty = P.TO_3857.transform(cascadia_trench_lon(tl), tl)
    ax.plot(tx, ty, color="#33ffff", lw=1.6, ls="--", label="Slab2 trench trace")
    ax.set_xlim(bext[0], bext[1]); ax.set_ylim(bext[2], bext[3])
    ax.set_xticks([]); ax.set_yticks([])
    cb = fig.colorbar(sc, ax=ax, shrink=0.72); cb.set_label("subfault slip (m)")
    ax.legend(loc="upper right", fontsize=9)
    ax.set_title("Cascadia M9.0 SCENARIO -- Slab2 subfault tiling (curved interface)",
                 fontsize=11, weight="bold")
    fig.text(0.5, 0.02,
             f"{m.n_subfaults} subfaults on the REAL USGS Slab2 interface; slip "
             f"{slips.min():.1f}-{slips.max():.1f} m (Tukey-tapered, sums to Mw "
             f"{moment_to_mw(m0):.2f}). Centroid lon migrates with latitude "
             f"(corr={corr:.2f}) -- the rupture FOLLOWS THE TRENCH, not a straight bar. "
             f"HYPOTHETICAL scenario, not a real event.",
             ha="center", fontsize=8, wrap=True)
    fig.tight_layout(rect=[0, 0.05, 1, 1])
    out = os.path.join(OUT_DIR, "geoclaw_scenario_cascadia_geometry.png")
    fig.savefig(out, dpi=130)
    plt.close(fig)
    print("wrote", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
