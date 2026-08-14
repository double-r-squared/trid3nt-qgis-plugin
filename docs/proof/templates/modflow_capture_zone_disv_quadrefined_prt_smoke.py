"""Through-image DISV quad-refined backward-PRT capture-zone smoke (ADR 0258).

Runs INSIDE trid3nt-local/modflow:adr0258. Proves the gridgen binary + mf6 6.7.0
build and solve a quad-refined DISV grid, then that a backward PRT (reversed
GWF field, ex-prt-mp7-p02 pattern) delineates a pumping-well capture zone on it.

Discriminating proof: the SAME physics on a COARSE structured-equivalent grid vs
the gridgen quad-REFINED grid -- near-well cell area and pathline seeding
resolution differ by the refinement factor; the refined capture zone is the
higher-fidelity delineation.
"""
import os
import math
import json
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import flopy
from flopy.utils.gridgen import Gridgen
from flopy.discretization import VertexGrid

WS = "/work"
GRIDGEN = os.environ.get("TRID3NT_GRIDGEN_BIN", "gridgen")
MF6 = os.environ.get("TRID3NT_MF6_BIN", "mf6")

# --- Domain (local origin, single confined layer) ------------------------
NLAY, NROW, NCOL = 1, 40, 40
DELR = DELC = 100.0          # 4000 x 4000 m base grid
TOP, BOT = 50.0, 0.0
K = 10.0                      # m/d
POROSITY = 0.25
WELL_Q = -1200.0             # m3/d extraction
GRAD_HEAD_W, GRAD_HEAD_E = 52.0, 48.0   # regional W->E gradient via CHD
WELL_XY = (2050.0, 2050.0)   # near centre, offset to break symmetry
N_PARTICLES = 24
RING_R = 45.0                # release ring radius (m) around well


def _base_structured_dis(ws, name):
    sim = flopy.mf6.MFSimulation(sim_name=name, sim_ws=ws, exe_name=MF6)
    flopy.mf6.ModflowTdis(sim)
    gwf = flopy.mf6.ModflowGwf(sim, modelname=name)
    flopy.mf6.ModflowGwfdis(
        gwf, nlay=NLAY, nrow=NROW, ncol=NCOL,
        delr=DELR, delc=DELC, top=TOP, botm=BOT,
    )
    return gwf


def build_disv_gridprops(ws, refine_level):
    """gridgen: refine a polygon around the well -> DISV gridprops.

    refine_level=0 returns the un-refined base as DISV (the coarse control).
    """
    gwf = _base_structured_dis(ws, "base")
    g = Gridgen(gwf.modelgrid, model_ws=ws, exe_name=GRIDGEN)
    if refine_level > 0:
        wx, wy = WELL_XY
        half = 300.0
        poly = [[
            (wx - half, wy - half), (wx + half, wy - half),
            (wx + half, wy + half), (wx - half, wy + half),
            (wx - half, wy - half),
        ]]
        g.add_refinement_features([poly], "polygon", refine_level, range(NLAY))
    g.build(verbose=False)
    return g.get_gridprops_disv()


def _vgrid(gp):
    return VertexGrid(
        vertices=gp["vertices"], cell2d=gp["cell2d"],
        top=np.full(gp["ncpl"], TOP), botm=np.array(gp["botm"]),
        nlay=gp["nlay"], ncpl=gp["ncpl"],
    )


def build_and_run_gwf(ws, gp, name):
    vg = _vgrid(gp)
    xc, yc = vg.xcellcenters, vg.ycellcenters
    ncpl = gp["ncpl"]

    sim = flopy.mf6.MFSimulation(sim_name=name, sim_ws=ws, exe_name=MF6)
    flopy.mf6.ModflowTdis(sim, nper=1, perioddata=[(1.0, 1, 1.0)])
    flopy.mf6.ModflowIms(sim, complexity="MODERATE",
                         inner_dvclose=1e-8, outer_dvclose=1e-7)
    gwf = flopy.mf6.ModflowGwf(sim, modelname=name, save_flows=True)
    flopy.mf6.ModflowGwfdisv(gwf, **gp)
    flopy.mf6.ModflowGwfnpf(gwf, save_specific_discharge=True, save_saturation=True,
                            icelltype=0, k=K)
    flopy.mf6.ModflowGwfic(gwf, strt=50.0)

    # CHD on west / east boundary columns (regional gradient)
    xmin, xmax = xc.min(), xc.max()
    chd = []
    for i in range(ncpl):
        if xc[i] <= xmin + DELR:
            chd.append([(0, i), GRAD_HEAD_W])
        elif xc[i] >= xmax - DELR:
            chd.append([(0, i), GRAD_HEAD_E])
    flopy.mf6.ModflowGwfchd(gwf, stress_period_data=chd)

    # Well: locate the cell2d containing WELL_XY
    well_cell = int(vg.intersect(WELL_XY[0], WELL_XY[1]))
    flopy.mf6.ModflowGwfwel(gwf, stress_period_data=[[(0, well_cell), WELL_Q]])

    flopy.mf6.ModflowGwfoc(
        gwf, head_filerecord=f"{name}.hds", budget_filerecord=f"{name}.cbc",
        saverecord=[("HEAD", "ALL"), ("BUDGET", "ALL")],
    )
    sim.write_simulation()
    ok, buff = sim.run_simulation(silent=True)
    if not ok:
        raise RuntimeError(f"GWF {name} did not converge:\n" + "\n".join(buff[-20:]))
    head = gwf.output.head().get_data().ravel()
    return vg, well_cell, head, xc, yc


def run_backward_prt(ws, gp, vg, well_cell, name):
    """ex-prt-mp7-p02 backward: reverse GWF head+budget, forward-track a ring."""
    hds = os.path.join(ws, f"{name}.hds")
    cbc = os.path.join(ws, f"{name}.cbc")
    hds_rev = os.path.join(ws, f"{name}_rev.hds")
    cbc_rev = os.path.join(ws, f"{name}_rev.cbc")
    flopy.utils.HeadFile(hds).reverse(hds_rev)
    flopy.utils.CellBudgetFile(cbc).reverse(cbc_rev)

    wx, wy = vg.xcellcenters[well_cell], vg.ycellcenters[well_cell]
    zc = 0.5 * (TOP + BOT)
    releasepts = []
    for k in range(N_PARTICLES):
        ang = 2 * math.pi * k / N_PARTICLES
        px, py = wx + RING_R * math.cos(ang), wy + RING_R * math.sin(ang)
        cid = int(vg.intersect(px, py))
        releasepts.append((k, (0, cid), px, py, zc))

    prt_name = f"{name}_prt"
    sim = flopy.mf6.MFSimulation(sim_name=prt_name, sim_ws=ws, exe_name=MF6)
    flopy.mf6.ModflowTdis(sim, nper=1, perioddata=[(1.0, 1, 1.0)])
    prt = flopy.mf6.ModflowPrt(sim, modelname=prt_name)
    flopy.mf6.ModflowPrtdisv(prt, **gp)
    flopy.mf6.ModflowPrtmip(prt, porosity=POROSITY)
    flopy.mf6.ModflowPrtprp(
        prt, nreleasepts=len(releasepts), packagedata=releasepts,
        perioddata={0: ["FIRST"]}, extend_tracking=True, boundnames=True,
        exit_solve_tolerance=1e-5, stoptime=1.0e6,
    )
    trk_csv = f"{prt_name}.trk.csv"
    flopy.mf6.ModflowPrtoc(prt, track_filerecord=f"{prt_name}.trk",
                           trackcsv_filerecord=trk_csv)
    flopy.mf6.ModflowPrtfmi(prt, packagedata=[
        ("GWFHEAD", hds_rev), ("GWFBUDGET", cbc_rev)])
    ems = flopy.mf6.ModflowEms(sim, filename=f"{prt_name}.ems")
    sim.register_solution_package(ems, [prt.name])
    sim.write_simulation()
    ok, buff = sim.run_simulation(silent=True)
    if not ok:
        raise RuntimeError(f"PRT {prt_name} failed:\n" + "\n".join(buff[-25:]))

    import csv
    paths = {}
    with open(os.path.join(ws, trk_csv)) as fh:
        for row in csv.DictReader(fh):
            pid = int(float(row.get("irpt", row.get("iprp", 0))))
            paths.setdefault(pid, []).append((float(row["x"]), float(row["y"])))
    return paths


def capture_area(paths):
    """Convex-hull area (m2) of all backward pathline vertices -> capture zone."""
    pts = np.array([p for path in paths.values() for p in path])
    if len(pts) < 3:
        return 0.0, pts
    try:
        from scipy.spatial import ConvexHull
        return ConvexHull(pts).volume, pts
    except Exception:
        # shoelace on sorted-by-angle fallback
        c = pts.mean(axis=0)
        ang = np.arctan2(pts[:, 1] - c[1], pts[:, 0] - c[0])
        q = pts[np.argsort(ang)]
        x, y = q[:, 0], q[:, 1]
        return 0.5 * abs(np.dot(x, np.roll(y, 1)) - np.dot(y, np.roll(x, 1))), pts


def main():
    os.makedirs(WS, exist_ok=True)
    results = {}
    panels = {}
    for tag, level in [("coarse", 0), ("refined", 3)]:
        ws = os.path.join(WS, tag)
        os.makedirs(ws, exist_ok=True)
        gp = build_disv_gridprops(ws, level)
        areas = np.array([_poly_area(gp, i) for i in range(gp["ncpl"])])
        vg, well_cell, head, xc, yc = build_and_run_gwf(ws, gp, tag)
        paths = run_backward_prt(ws, gp, vg, well_cell, tag)
        czone, pts = capture_area(paths)
        results[tag] = {
            "ncpl": int(gp["ncpl"]),
            "min_cell_area_m2": float(areas.min()),
            "min_cell_edge_m": float(math.sqrt(areas.min())),
            "well_cell2d": int(well_cell),
            "n_particles_tracked": len(paths),
            "capture_zone_area_km2": czone / 1e6,
            "head_min": float(head.min()), "head_max": float(head.max()),
        }
        panels[tag] = (gp, vg, head, paths, well_cell)
    render(panels, results)
    print(json.dumps(results, indent=2))


def _poly_area(gp, i):
    verts = {v[0]: (v[1], v[2]) for v in gp["vertices"]}
    c = gp["cell2d"][i]
    ring = [verts[j] for j in c[4:]]
    x = np.array([p[0] for p in ring]); y = np.array([p[1] for p in ring])
    return 0.5 * abs(np.dot(x, np.roll(y, 1)) - np.dot(y, np.roll(x, 1)))


def render(panels, results):
    fig, axes = plt.subplots(1, 2, figsize=(15, 7.6), constrained_layout=True)
    for ax, tag in zip(axes, ("coarse", "refined")):
        gp, vg, head, paths, well_cell = panels[tag]
        pmv = flopy.plot.PlotMapView(modelgrid=vg, ax=ax)
        hd = pmv.plot_array(head, cmap="Blues", alpha=0.75)
        pmv.plot_grid(lw=0.25, color="0.45")            # DISV mesh wireframe
        for path in paths.values():
            a = np.array(path)
            ax.plot(a[:, 0], a[:, 1], "-", color="crimson", lw=0.9)   # polylines
        ax.plot(vg.xcellcenters[well_cell], vg.ycellcenters[well_cell],
                "k^", ms=11, label="pumping well")
        r = results[tag]
        ax.set_title(f"{tag.upper()}  ncpl={r['ncpl']}  "
                     f"min cell {r['min_cell_edge_m']:.0f} m\n"
                     f"backward capture zone {r['capture_zone_area_km2']:.3f} km2",
                     fontsize=11)
        ax.set_xlabel("x (m)"); ax.set_ylabel("y (m)")
        ax.set_aspect("equal"); ax.legend(loc="upper right", fontsize=8)
    fig.colorbar(hd, ax=axes, shrink=0.6, label="head (m)")
    fig.suptitle("MODFLOW6 gridgen DISV quad-refined vs coarse -- backward PRT "
                 "capture zone (through image trid3nt-local/modflow:adr0258)",
                 fontsize=12)
    out = os.path.join(WS, "disv_prt_capture_zone_proof.png")
    fig.savefig(out, dpi=135)
    print("PROOF_PNG", out)


if __name__ == "__main__":
    main()
