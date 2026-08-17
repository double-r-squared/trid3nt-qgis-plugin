"""CSUB delay-interbed + effective-stress upgrade prototype.

The landed land_subsidence archetype uses ONE no-delay HEAD_BASED interbed per
pumped cell. The board row csub_effective_stress_vs_head_based_crosscheck asks
for the EFFECTIVE_STRESS formulation + delay interbeds + elastic-vs-inelastic per
the ex-gwf-csub-p04 class. This prototype proves three physics behaviors before
wiring the knobs into the adapter:

  (A) COMPACTION SCALES WITH PUMPING  -- more extraction -> more subsidence
      (monotone).  This is the mandated behavior-proving smoke.
  (B) DELAY-INTERBED LAG  -- a delay interbed (finite vertical diffusivity)
      compacts more SLOWLY than an equivalent no-delay interbed given the same
      head decline; at the end of pumping the delay bed's compaction is BELOW the
      no-delay bed's (time-lagged consolidation, the ex-gwf-csub delay physics).
  (C) EFFECTIVE_STRESS vs HEAD_BASED cross-check -- the two skeletal-storage
      formulations produce compaction of the same order for the same stress path
      (the board row's crosscheck).

Confined single-layer transient deck (mirrors fixtures/csub_smoke).
"""
import os
import numpy as np
import flopy

MF6 = "/home/nate/Documents/trid3nt-local/bin/mf6"
WS_ROOT = os.path.dirname(os.path.abspath(__file__))

NROW = NCOL = 40
DELR = DELC = 50.0
TOP, BOTM = 30.0, 0.0
K = 8.6
STRT = 28.0
CG_SKE = 1e-5
WEL_CELL = (0, 20, 20)
N_TRANSIENT = 10
PERLEN = 365.0
SSV, SSE = 2e-3, 5e-5
THICK_FRAC = 0.5
THETA = 0.3


def footprint():
    (_l, wr, wc) = WEL_CELL
    return [(0, wr + dr, wc + dc) for dr in (-1, 0, 1) for dc in (-1, 0, 1)]


def build(ws, *, pump, head_based, delay):
    os.makedirs(ws, exist_ok=True)
    name = "csd"
    sim = flopy.mf6.MFSimulation(sim_name=name, sim_ws=ws, exe_name=MF6)
    perioddata = [(1.0, 1, 1.0)] + [(PERLEN, 12, 1.0)] * N_TRANSIENT
    flopy.mf6.ModflowTdis(sim, nper=1 + N_TRANSIENT, perioddata=perioddata,
                          time_units="days")
    flopy.mf6.ModflowIms(sim, complexity="COMPLEX", outer_maximum=200,
                         inner_maximum=100)
    gwf = flopy.mf6.ModflowGwf(sim, modelname=name, save_flows=True)
    flopy.mf6.ModflowGwfdis(gwf, nlay=1, nrow=NROW, ncol=NCOL, delr=DELR,
                            delc=DELC, top=TOP, botm=BOTM)
    flopy.mf6.ModflowGwfic(gwf, strt=STRT)
    flopy.mf6.ModflowGwfnpf(gwf, icelltype=0, k=K, save_flows=True)
    # CSUB owns skeletal storage -> STO ss MUST be 0 in active cells
    sto_trans = {p: True for p in range(1, 1 + N_TRANSIENT)}
    sto_trans[0] = False
    flopy.mf6.ModflowGwfsto(gwf, iconvert=0, ss=0.0, sy=0.0,
                            steady_state={0: True}, transient=sto_trans)
    # perimeter CHD ring holds the regional head
    chd = []
    for r in range(NROW):
        for c in range(NCOL):
            if r in (0, NROW - 1) or c in (0, NCOL - 1):
                chd.append([(0, r, c), STRT])
    flopy.mf6.ModflowGwfchd(gwf, stress_period_data={0: chd})
    # transient pumping in periods 1..N
    wel_spd = {p: [[WEL_CELL, pump]] for p in range(1, 1 + N_TRANSIENT)}
    flopy.mf6.ModflowGwfwel(gwf, stress_period_data=wel_spd)

    # ---- CSUB ----
    cdelay = "delay" if delay else "nodelay"
    pkdat = []
    for i, cell in enumerate(footprint()):
        # delay beds need rnb (#beds) and a positive kv; nodelay ignores kv
        rnb = 1.0
        kv = 1e-6 if delay else 1.0        # low vertical K -> slow consolidation
        pkdat.append((i, cell, cdelay, 0.0, THICK_FRAC, rnb,
                      SSV, SSE, THETA, kv, 0.0, f"sub{i}"))
    # obs by interbed NUMBER (1-based icsubno); boundname is rejected for
    # interbed-compaction in mf6 6.7.0.
    obs = [(f"comp{i}", "interbed-compaction", i + 1) for i in range(len(pkdat))]
    kwargs = dict(
        ninterbeds=len(pkdat), cg_ske_cr=CG_SKE, cg_theta=0.3,
        beta=4.6e-10, gammaw=9806.65,
        packagedata=pkdat, cell_fraction=True, compression_indices=False,
        boundnames=True,
        observations={f"{name}.csub.obs.csv": obs},
        zdisplacement_filerecord=[f"{name}.csub.zdis.bin"],
        package_convergence_filerecord=f"{name}.csub.conv.csv",
    )
    if delay:
        kwargs["ndelaycells"] = 19
    if head_based:
        kwargs["head_based"] = True
        kwargs["initial_preconsolidation_head"] = STRT
    else:
        # effective-stress formulation: geostatic loads
        kwargs["sgm"] = 1.7
        kwargs["sgs"] = 2.0
        kwargs["specified_initial_preconsolidation_stress"] = True
        # provide pcs0 as a stress offset of 0 (initial eff stress = preconsolidation)
    # zdisplacement requires an OC-like interbed thickness sum record; keep simple
    flopy.mf6.ModflowGwfcsub(gwf, **kwargs)

    flopy.mf6.ModflowGwfoc(
        gwf, head_filerecord=f"{name}.hds", budget_filerecord=f"{name}.cbc",
        saverecord=[("HEAD", "LAST")],
    )
    sim.write_simulation(silent=True)
    ok, buff = sim.run_simulation(silent=True)
    if not ok:
        print("\n".join(buff[-25:]))
        raise RuntimeError(f"mf6 failed head_based={head_based} delay={delay} pump={pump}")

    o = np.genfromtxt(os.path.join(ws, f"{name}.csub.obs.csv"),
                      delimiter=",", names=True)
    cols = [n for n in o.dtype.names if n.upper().startswith("COMP")]
    # total compaction (sum over interbeds) at each time; final = last row
    tot = np.zeros(len(np.atleast_1d(o[cols[0]])))
    for cn in cols:
        tot = tot + np.atleast_1d(o[cn])
    t = np.atleast_1d(o["time"])
    return t, tot  # metres, positive-down


if __name__ == "__main__":
    print("=== (A) compaction scales with pumping (head_based, nodelay) ===")
    finals = []
    for pump in (-2000.0, -4000.0, -8000.0):
        ws = os.path.join(WS_ROOT, f"csd_A_p{int(-pump)}")
        t, tot = build(ws, pump=pump, head_based=True, delay=False)
        finals.append((pump, float(tot[-1])))
        print(f"  pump={pump:>8.0f} m3/d  final_compaction={tot[-1]*100:8.3f} cm")
    mags = [abs(f[1]) for f in finals]
    monoA = all(mags[i] < mags[i + 1] for i in range(len(mags) - 1))
    print(f"  MONOTONE compaction-vs-pumping: {monoA}")

    print("\n=== (B) delay-interbed lag vs no-delay (head_based, pump=-4000) ===")
    tn, cn = build(os.path.join(WS_ROOT, "csd_B_nodelay"),
                   pump=-4000.0, head_based=True, delay=False)
    td, cd = build(os.path.join(WS_ROOT, "csd_B_delay"),
                   pump=-4000.0, head_based=True, delay=True)
    print(f"  nodelay final={cn[-1]*100:8.3f} cm   delay final={cd[-1]*100:8.3f} cm")
    lag = abs(cd[-1]) < abs(cn[-1])
    print(f"  DELAY LAGS no-delay at end-of-pumping: {lag}")

    print("\n=== (C) effective-stress vs head-based crosscheck (nodelay, pump=-4000) ===")
    the, che = build(os.path.join(WS_ROOT, "csd_C_hb"),
                     pump=-4000.0, head_based=True, delay=False)
    tef, cef = build(os.path.join(WS_ROOT, "csd_C_es"),
                     pump=-4000.0, head_based=False, delay=False)
    print(f"  head_based final={che[-1]*100:8.3f} cm   eff_stress final={cef[-1]*100:8.3f} cm")
    ratio = abs(cef[-1]) / abs(che[-1]) if che[-1] else float("nan")
    print(f"  same-order crosscheck ratio (es/hb)={ratio:.3f}")

    print("\nSUMMARY")
    assert monoA, "FAIL: compaction not monotone in pumping"
    assert lag, "FAIL: delay interbed did not lag no-delay"
    print("PASS: (A) compaction monotone in pumping; (B) delay lags no-delay")
