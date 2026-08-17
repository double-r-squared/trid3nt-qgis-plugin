"""UZT (unsaturated-zone transport) local-first physics prototype.

Question class: a tracer applied at the LAND SURFACE transits the VADOSE zone
via UZF unsaturated flow before reaching the water table -- how long until it
arrives at the water table, and at what concentration? Anchor: modflow6-examples
ex-gwt-uzt-2d (UZF+UZT purely-advective unsat transport; MF6 lacks unsat
dispersion).

Physics assertion (behavior-proving smoke): arrival time at the water table
scales MONOTONICALLY with vadose thickness (depth to water table). We run 3
depths and assert t_arrival(shallow) < t_arrival(mid) < t_arrival(deep).

1D vertical column: 1 row, 1 col, NLAY layers, dz = 1.0 m each. The water table
sits at the top of layer WT_LAY; layers above are the vadose zone with a stack of
vertically-connected UZF cells (ivertcon chain). A constant infiltration flux
carries a unit tracer concentration in at the surface. A CHD at the bottom layer
fixes the saturated head; the UZT concentration observed at the lowest UZF cell
(just above the water table) is the arrival signal.
"""
import os
import numpy as np
import flopy

MF6 = "/home/nate/Documents/trid3nt-local/bin/mf6"
WS_ROOT = os.path.dirname(os.path.abspath(__file__))

DZ = 1.0                 # m per layer
DELR = DELC = 10.0       # m plan cell
NROW = NCOL = 1
FINF = 0.01              # m/day infiltration flux at surface
VKS = 0.1                # m/day saturated vertical K of the unsat medium
THTR, THTS, THTI = 0.05, 0.35, 0.08   # residual / saturated / initial water content
EPS = 4.0                # Brooks-Corey exponent
NPER = 1
PERLEN = 4000.0          # days (long enough for the deepest arrival)
NSTP = 400
CONC_IN = 1.0            # tracer concentration in infiltration (unit)


def build_run(ws, n_vadose):
    """n_vadose = number of unsaturated (UZF) layers above the water table."""
    os.makedirs(ws, exist_ok=True)
    nlay = n_vadose + 3          # +3 saturated layers below the water table
    top = float(nlay) * DZ
    botm = [top - (i + 1) * DZ for i in range(nlay)]
    name = "uzt"
    sim = flopy.mf6.MFSimulation(sim_name=name, sim_ws=ws, exe_name=MF6)
    flopy.mf6.ModflowTdis(sim, nper=NPER, perioddata=[(PERLEN, NSTP, 1.0)],
                          time_units="days")
    ims_gwf = flopy.mf6.ModflowIms(sim, complexity="MODERATE", outer_maximum=200,
                                   inner_maximum=100, linear_acceleration="BICGSTAB",
                                   filename="gwf.ims")
    ims_gwt = flopy.mf6.ModflowIms(sim, complexity="MODERATE", outer_maximum=200,
                                   inner_maximum=100, linear_acceleration="BICGSTAB",
                                   filename="gwt.ims")

    # ---- GWF flow ----
    gwf = flopy.mf6.ModflowGwf(sim, modelname="gwf", save_flows=True,
                               newtonoptions="NEWTON UNDER_RELAXATION")
    flopy.mf6.ModflowGwfdis(gwf, nlay=nlay, nrow=NROW, ncol=NCOL,
                            delr=DELR, delc=DELC, top=top, botm=botm)
    # water table at the top of layer n_vadose (0-based): head = botm[n_vadose-1]
    wt = botm[n_vadose - 1]
    flopy.mf6.ModflowGwfic(gwf, strt=wt)
    flopy.mf6.ModflowGwfnpf(gwf, save_flows=True, icelltype=1, k=1.0, k33=VKS)
    flopy.mf6.ModflowGwfsto(gwf, iconvert=1, ss=1e-5, sy=THTS, transient={0: True})
    # fix saturated head at the bottom layer (drain to keep a stable water table)
    chd = [[(nlay - 1, 0, 0), wt]]
    flopy.mf6.ModflowGwfchd(gwf, stress_period_data=chd)

    # ---- UZF: vertical chain of n_vadose cells ----
    # packagedata: [ifno, cellid, landflag, ivertcon, surfdep, vks, thtr, thts, thti, eps, bnd]
    uzf_pkdat = []
    surfdep = 0.1
    for i in range(n_vadose):
        ivertcon = i + 1 if i < n_vadose - 1 else -1   # chain down; -1 = to GW
        landflag = 1 if i == 0 else 0
        uzf_pkdat.append([i, (i, 0, 0), landflag, ivertcon, surfdep,
                          VKS, THTR, THTS, THTI, EPS, f"uz{i}"])
    # perioddata: [ifno, finf, pet, extdp, extwc, ha, hroot, rootact]
    uzf_perdat = {0: [[i, (FINF if i == 0 else 0.0), 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
                      for i in range(n_vadose)]}
    flopy.mf6.ModflowGwfuzf(
        gwf, print_flows=False, save_flows=True,
        simulate_et=False, linear_gwet=False,
        nuzfcells=n_vadose, ntrailwaves=7, nwavesets=40,
        packagedata=uzf_pkdat, perioddata=uzf_perdat,
        budget_filerecord=f"{name}.uzf.bud",
        boundnames=True,
        pname="uzf",
    )

    # ---- GWT transport ----
    gwt = flopy.mf6.ModflowGwt(sim, modelname="gwt", save_flows=True)
    flopy.mf6.ModflowGwtdis(gwt, nlay=nlay, nrow=NROW, ncol=NCOL,
                            delr=DELR, delc=DELC, top=top, botm=botm)
    flopy.mf6.ModflowGwtic(gwt, strt=0.0)
    flopy.mf6.ModflowGwtmst(gwt, porosity=THTS)
    flopy.mf6.ModflowGwtadv(gwt, scheme="TVD")
    flopy.mf6.ModflowGwtssm(gwt, sources=[[]])
    flopy.mf6.ModflowGwtoc(
        gwt, concentration_filerecord=f"{name}.ucn",
        saverecord=[("CONCENTRATION", "ALL")],
    )
    # UZT: transport in the unsaturated cells; infiltration carries CONC_IN
    uzt_pkdat = [[i, 0.0, f"uz{i}"] for i in range(n_vadose)]
    uzt_perdat = {0: [[0, "INFILTRATION", CONC_IN]]}
    flopy.mf6.ModflowGwtuzt(
        gwt, flow_package_name="uzf",
        print_flows=False, save_flows=True,
        packagedata=uzt_pkdat, uztperioddata=uzt_perdat,
        boundnames=True,
        concentration_filerecord=f"{name}.uzt.ucn",
        observations={f"{name}.uzt.obs.csv": [
            ("uzbot", "concentration", f"uz{n_vadose - 1}")]},
    )

    # GWF-GWT exchange provides the flows (no manual FMI needed)
    flopy.mf6.ModflowGwfoc(
        gwf, head_filerecord=f"{name}.hds", budget_filerecord=f"{name}.cbc",
        saverecord=[("HEAD", "ALL"), ("BUDGET", "ALL")],
    )
    flopy.mf6.ModflowGwfgwt(sim, exgtype="GWF6-GWT6", exgmnamea="gwf",
                            exgmnameb="gwt")
    sim.register_ims_package(ims_gwf, ["gwf"])
    sim.register_ims_package(ims_gwt, ["gwt"])

    sim.write_simulation(silent=True)
    ok, buff = sim.run_simulation(silent=True)
    if not ok:
        print("\n".join(buff[-30:]))
        raise RuntimeError(f"mf6 failed for n_vadose={n_vadose}")

    # arrival time = first time the bottom UZF cell conc crosses 0.5*CONC_IN
    obs = np.genfromtxt(os.path.join(ws, f"{name}.uzt.obs.csv"),
                        delimiter=",", names=True)
    t = np.atleast_1d(obs["time"])
    c = np.atleast_1d(obs["UZBOT"])
    thresh = 0.5 * CONC_IN
    idx = np.where(c >= thresh)[0]
    t_arr = float(t[idx[0]]) if len(idx) else float("nan")
    c_final = float(c[-1])
    depth = n_vadose * DZ
    return depth, t_arr, c_final


if __name__ == "__main__":
    print(f"{'n_vad':>5} {'depth_m':>8} {'t_arrive_d':>11} {'c_final':>8}")
    rows = []
    for nv in (2, 4, 8):
        ws = os.path.join(WS_ROOT, f"uzt_run_nv{nv}")
        d, ta, cf = build_run(ws, nv)
        rows.append((nv, d, ta, cf))
        print(f"{nv:>5} {d:>8.1f} {ta:>11.1f} {cf:>8.3f}")
    ts = [r[2] for r in rows]
    mono = all(ts[i] < ts[i + 1] for i in range(len(ts) - 1))
    print(f"\nMONOTONE arrival-time-vs-depth: {mono}  (times={ts})")
    assert mono, "PHYSICS ASSERTION FAILED: arrival not monotone in vadose depth"
    print("PASS: arrival time increases monotonically with vadose thickness")
