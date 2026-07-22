"""SFR feasibility smoke: 40x40 GWF grid (mirrors gwt_adapter demo scaffold)
+ ModflowGwfsfr 8-reach stream + WEL pumping well, run on the trid3nt-local mf6
6.5.0 binary. Proves: packagedata/connectiondata shapes on flopy 3.10.0, OBS
csv per-reach stage/flow/gwf-exchange output, convergence, and that WEL
depletion shows up in the SFR gwf exchange term."""
import os, sys, csv
import flopy

ws = os.path.join(os.path.dirname(__file__), "sfr_run")
os.makedirs(ws, exist_ok=True)
name = "sfrsmoke"
sim = flopy.mf6.MFSimulation(sim_name=name, sim_ws=ws, exe_name="/home/nate/Documents/trid3nt-local/bin/mf6")
# 2 periods: steady baseline (no pumping), then pumped steady state.
flopy.mf6.ModflowTdis(sim, nper=2, perioddata=[(1.0, 1, 1.0), (365.0, 1, 1.0)])
flopy.mf6.ModflowIms(sim, outer_maximum=200, inner_maximum=100, outer_dvclose=1e-6, inner_dvclose=1e-7, linear_acceleration="BICGSTAB")
gwf = flopy.mf6.ModflowGwf(sim, modelname=name, newtonoptions="NEWTON")
nrow = ncol = 40; delr = delc = 50.0
flopy.mf6.ModflowGwfdis(gwf, nlay=1, nrow=nrow, ncol=ncol, delr=delr, delc=delc, top=30.0, botm=0.0)
flopy.mf6.ModflowGwfnpf(gwf, icelltype=1, k=8.6)  # ~1e-4 m/s in m/day
flopy.mf6.ModflowGwfic(gwf, strt=28.0)
flopy.mf6.ModflowGwfsto(gwf, iconvert=1, sy=0.15, ss=1e-5, steady_state={0: True, 1: True})
# regional gradient CHD west->east (mirrors the adapter scaffold)
chd = [[(0, r, 0), 29.0] for r in range(nrow)] + [[(0, r, ncol - 1), 27.0] for r in range(nrow)]
flopy.mf6.ModflowGwfchd(gwf, stress_period_data={0: chd, 1: chd})
# WEL: off in period 0, pumping in period 1 (depletion pattern)
wel_cell = (0, 20, 24)
flopy.mf6.ModflowGwfwel(gwf, stress_period_data={0: [], 1: [[wel_cell, -500.0]]})
# SFR: 8 reaches west->east across row 20 (crossing near the well)
nreach = 8
pak = []
con = []
for i in range(nreach):
    cellid = (0, 20, 8 + i * 2)
    rlen = 100.0; rwid = 5.0; rgrd = 0.001
    rtp = 28.5 - 0.05 * i  # streambed top declining downstream
    rbth = 1.0; rhk = 0.5; man = 0.035
    ncon = (1 if i in (0, nreach - 1) else 2); ustrf = 1.0; ndv = 0
    pak.append((i, cellid, rlen, rwid, rgrd, rtp, rbth, rhk, man, ncon, ustrf, ndv))
    c = [i]
    if i > 0: c.append(i - 1)          # upstream connection (positive)
    if i < nreach - 1: c.append(-(i + 1))  # downstream connection (negative)
    con.append(c)
sfr = flopy.mf6.ModflowGwfsfr(
    gwf, save_flows=True, print_stage=False,
    stage_filerecord=f"{name}.sfr.stg", budget_filerecord=f"{name}.sfr.bud",
    budgetcsv_filerecord=f"{name}.sfr.bud.csv",
    nreaches=nreach, packagedata=pak, connectiondata=con,
    perioddata={0: [(0, "INFLOW", 5000.0)], 1: [(0, "INFLOW", 5000.0)]},
    unit_conversion=86400.0,
)
obs = {f"{name}.sfr.obs.csv": (
    [(f"stage_r{i+1}", "stage", i + 1) for i in range(nreach)]
    + [(f"flow_r{i+1}", "downstream-flow", i + 1) for i in range(nreach)]
    + [(f"gwf_r{i+1}", "sfr", i + 1) for i in range(nreach)]
)}
sfr.obs.initialize(filename=f"{name}.sfr.obs", continuous=obs)
flopy.mf6.ModflowGwfoc(gwf, head_filerecord=f"{name}.hds", budget_filerecord=f"{name}.cbc",
                       saverecord=[("HEAD", "ALL"), ("BUDGET", "ALL")])
sim.write_simulation()
ok, buff = sim.run_simulation(silent=True)
print("converged run:", ok)
with open(os.path.join(ws, f"{name}.sfr.obs.csv")) as fh:
    rows = list(csv.DictReader(fh))
base, pump = rows[0], rows[-1]
tot_base = sum(float(base[f"GWF_R{i+1}"]) for i in range(nreach))
tot_pump = sum(float(pump[f"GWF_R{i+1}"]) for i in range(nreach))
print("per-reach stage (pumped):", [round(float(pump[f'STAGE_R{i+1}']), 3) for i in range(nreach)])
print("per-reach downstream flow (pumped):", [round(float(pump[f'FLOW_R{i+1}']), 1) for i in range(nreach)])
print(f"net SFR->GWF exchange baseline={tot_base:.2f} pumped={tot_pump:.2f} m3/d")
print(f"streamflow depletion (delta exchange) = {tot_pump - tot_base:.2f} m3/d vs WEL 500")
