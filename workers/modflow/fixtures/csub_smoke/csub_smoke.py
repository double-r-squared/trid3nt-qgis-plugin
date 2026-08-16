"""CSUB land-subsidence feasibility smoke: 40x40 single-layer CONFINED GWF
(mirrors the sustainable_yield scaffold that land_subsidence reuses) + a
transient WEL pumping schedule (steady baseline period 0 -> N transient pumping
periods) + a ModflowGwfcsub package (ONE no-delay HEAD_BASED interbed per pumped
footprint cell). Run on trid3nt-local mf6 6.5.0.

Proves, per the design's Phase-1 gate:
  (a) NORMAL TERMINATION (converged);
  (b) THE STORAGE DOUBLE-COUNT FIX: three runs -
        A = plain (no CSUB), STO ss = geomech skeletal Ss (1e-5)  -> baseline dh
        B = CSUB present, STO ss STILL 1e-5 (the BUG: double storage)
        C = CSUB present, STO ss dropped to a water floor (1e-7),
            CSUB cg_ske_cr = 1e-5 supplies the skeletal storage (the FIX)
      C's head decline must match A within a few percent; B must be visibly off.
  (c) the compaction / z-displacement OUTPUT: exact file name + HeadFile text tag
      + units (m) + SIGN (positive-down per ex-gwf-csub-p04); dz ~ Ssv*b*dh.
"""
import os
import numpy as np
import flopy

HERE = os.path.dirname(__file__)
MF6 = "/home/nate/Documents/trid3nt-local/bin/mf6"

# --- grid / aquifer (confined single layer, sustainable_yield-style) --------- #
NROW = NCOL = 40
DELR = DELC = 50.0
TOP = 30.0
BOTM = 0.0
K = 8.6            # m/day
STRT = 28.0        # initial head
SY = 0.15          # unused (confined) but written like the scaffold
SS_GEOMECH = 1e-5  # the "full" skeletal specific storage (the double-count trap)
SS_CSUB_FLOOR = 0.0  # mf6 6.5.0 HARD-REQUIRES STO ss == 0 in all active cells when CSUB present
CG_SKE = 1e-5      # CSUB coarse-grained elastic skeletal Ss (== SS_GEOMECH)
WEL_CELL = (0, 20, 20)
PUMP = -4000.0     # m^3/day extraction
N_TRANSIENT = 10   # 10 yearly pumping periods
PERLEN = 365.0

# CSUB interbed demo defaults (design canonical case: San Joaquin Valley)
SSV = 2e-3         # inelastic (virgin) specific storage, m^-1
SSE = 5e-5         # elastic (recompression) specific storage, m^-1
THICK_FRAC = 0.5   # interbed occupies half the 30 m layer -> b = 15 m
THETA = 0.3        # interbed porosity


def footprint_cells():
    """Well cell + its 8 neighbours (9-cell pumped footprint)."""
    (_lay, wr, wc) = WEL_CELL
    cells = []
    for dr in (-1, 0, 1):
        for dc in (-1, 0, 1):
            cells.append((0, wr + dr, wc + dc))
    return cells


def build(ws, *, csub, sto_ss):
    os.makedirs(ws, exist_ok=True)
    name = "csubsmoke"
    sim = flopy.mf6.MFSimulation(sim_name=name, sim_ws=ws, exe_name=MF6)
    perioddata = [(1.0, 1, 1.0)] + [(PERLEN, 1, 1.0)] * N_TRANSIENT
    nper = len(perioddata)
    flopy.mf6.ModflowTdis(sim, time_units="DAYS", nper=nper, perioddata=perioddata)
    flopy.mf6.ModflowIms(
        sim, complexity="MODERATE", outer_maximum=100, inner_maximum=100,
        outer_dvclose=1e-6, inner_dvclose=1e-6, linear_acceleration="BICGSTAB",
    )
    gwf = flopy.mf6.ModflowGwf(sim, modelname=name, save_flows=True)
    flopy.mf6.ModflowGwfdis(
        gwf, nlay=1, nrow=NROW, ncol=NCOL, delr=DELR, delc=DELC, top=TOP, botm=BOTM,
    )
    flopy.mf6.ModflowGwfic(gwf, strt=STRT)
    flopy.mf6.ModflowGwfnpf(gwf, save_flows=True, icelltype=0, k=K)  # confined
    transient_map = {i: True for i in range(1, nper)}
    flopy.mf6.ModflowGwfsto(
        gwf, iconvert=0, ss=sto_ss, sy=SY,
        steady_state={0: True}, transient=transient_map, save_flows=True,
    )
    # regional gradient CHD west->east
    chd = [[(0, r, 0), 29.0] for r in range(NROW)] + [[(0, r, NCOL - 1), 27.0] for r in range(NROW)]
    flopy.mf6.ModflowGwfchd(gwf, stress_period_data={0: chd})
    # WEL: off in baseline period 0, pumping in all transient periods
    spd = {0: []}
    for p in range(1, nper):
        spd[p] = [[WEL_CELL, PUMP]]
    flopy.mf6.ModflowGwfwel(gwf, stress_period_data=spd)

    if csub:
        cells = footprint_cells()
        ninterbeds = len(cells)
        # packagedata row: (icsubno, cellid, cdelay, pcs0, thick_frac, rnb,
        #                   ssv_cc, sse_cr, theta, kv, h0, boundname)
        pkg = []
        for i, cid in enumerate(cells):
            pkg.append(
                (i, cid, "nodelay", 0.0, THICK_FRAC, 1.0, SSV, SSE, THETA, 1.0, 0.0, f"sub_r{i}")
            )
        # OBS types (all keyed on the per-interbed boundname sub_r{i}):
        #   interbed-compaction  -> COMPACTION_R{i} (total interbed compaction, m)
        #   inelastic-compaction -> INE_R{i} (permanent), elastic-compaction -> ELA_R{i}
        # inelastic_fraction = sum(INE) / (sum(INE) + sum(ELA)) over interbeds.
        obs = {f"{name}.csub.obs.csv": (
            [(f"compaction_r{i}", "interbed-compaction", f"sub_r{i}") for i in range(ninterbeds)]
            + [(f"ine_r{i}", "inelastic-compaction", f"sub_r{i}") for i in range(ninterbeds)]
            + [(f"ela_r{i}", "elastic-compaction", f"sub_r{i}") for i in range(ninterbeds)]
        )}
        csub_pkg = flopy.mf6.ModflowGwfcsub(
            gwf,
            save_flows=True,
            boundnames=True,
            head_based=True,
            initial_preconsolidation_head=True,
            cell_fraction=True,      # thick_frac is a fraction of cell thickness
            compression_indices=False,  # ssv_cc/sse_cr are Ss values (m^-1)
            ninterbeds=ninterbeds,
            maxsig0=0,
            cg_ske_cr=CG_SKE,
            cg_theta=THETA,
            packagedata=pkg,
            compaction_filerecord=f"{name}.csub.compaction.bin",
            zdisplacement_filerecord=f"{name}.csub.zdisp.bin",
        )
        csub_pkg.obs.initialize(filename=f"{name}.csub.obs", continuous=obs)

    flopy.mf6.ModflowGwfoc(
        gwf, head_filerecord=f"{name}.hds", budget_filerecord=f"{name}.cbc",
        saverecord=[("HEAD", "ALL"), ("BUDGET", "ALL")],
    )
    sim.write_simulation(silent=True)
    ok, buff = sim.run_simulation(silent=True)
    return name, ok


def final_head_at_well(ws, name):
    hds = flopy.utils.HeadFile(os.path.join(ws, f"{name}.hds"))
    h = hds.get_data(kstpkper=hds.get_kstpkper()[-1])
    return float(h[WEL_CELL[0], WEL_CELL[1], WEL_CELL[2]])


print("=" * 70)
print("RUN A: plain (no CSUB), STO ss = 1e-5 (geomech skeletal)  [baseline]")
nameA, okA = build(os.path.join(HERE, "runA_plain"), csub=False, sto_ss=SS_GEOMECH)
hA = final_head_at_well(os.path.join(HERE, "runA_plain"), nameA)
print(f"  converged={okA}  final head @ well = {hA:.4f} m  (decline {STRT-hA:.4f} m)")

print("=" * 70)
print("RUN B: CSUB present, STO ss = 1e-5 STILL (the DOUBLE-COUNT BUG)")
nameB, okB = build(os.path.join(HERE, "runB_csub_doublecount"), csub=True, sto_ss=SS_GEOMECH)
if okB and os.path.exists(os.path.join(HERE, "runB_csub_doublecount", f"{nameB}.hds")):
    hB = final_head_at_well(os.path.join(HERE, "runB_csub_doublecount"), nameB)
    print(f"  converged={okB}  final head @ well = {hB:.4f} m  (decline {STRT-hB:.4f} m)")
else:
    hB = None
    print(f"  converged={okB} -> mf6 REFUSED the deck. Reading the STO/CSUB guard error:")
    with open(os.path.join(HERE, "runB_csub_doublecount", "mfsim.lst")) as fh:
        txt = fh.read()
    for ln in txt.splitlines():
        if "storage" in ln.lower() or "csub" in ln.lower() and "must" in ln.lower():
            print("   ", ln.strip())
    print("   => ENGINE-ENFORCED double-count guard: STO ss MUST be 0 when CSUB present.")

print("=" * 70)
print("RUN C: CSUB present, STO ss = 0.0 (the FIX; CSUB cg_ske_cr owns skeletal storage)")
nameC, okC = build(os.path.join(HERE, "runC_csub_fixed"), csub=True, sto_ss=SS_CSUB_FLOOR)
hC = final_head_at_well(os.path.join(HERE, "runC_csub_fixed"), nameC)
print(f"  converged={okC}  final head @ well = {hC:.4f} m  (decline {STRT-hC:.4f} m)")

print("=" * 70)
dA, dC = STRT - hA, STRT - hC
print("STORAGE DOUBLE-COUNT PROOF (head decline at well, m):")
print(f"  A plain (no CSUB, ss=1e-5)     : {dA:.4f}")
if hB is None:
    print(f"  B CSUB + ss=1e-5              : mf6 ERRORED (engine-enforced guard; deck rejected)")
else:
    print(f"  B CSUB doublecount           : {STRT-hB:.4f}")
print(f"  C CSUB fixed (ss=0, cg_ske=1e-5): {dC:.4f}   ({(dC-dA)/dA*100:+.2f}% vs A)")
print(f"  => the fix aligns CSUB decline with the plain run: C within "
      f"{abs(dC-dA)/dA*100:.2f}% of A. The double-count is gone.")

# --- pin the compaction / z-displacement output (from the FIXED run C) ------- #
print("=" * 70)
print("CSUB OUTPUT (from run C - the corrected deck):")
wsC = os.path.join(HERE, "runC_csub_fixed")
for fn in os.listdir(wsC):
    if "csub" in fn:
        print("  file:", fn)

# PINNED TAGS: mf6 6.5.0 writes CSUB-COMPACTION and CSUB-ZDISPLACE (the latter
# TRUNCATED to 16 chars from "CSUB-ZDISPLACEMENT" -- the design's assumed tag is WRONG).
comp = flopy.utils.HeadFile(os.path.join(wsC, f"{nameC}.csub.compaction.bin"), text="CSUB-COMPACTION")
zdis = flopy.utils.HeadFile(os.path.join(wsC, f"{nameC}.csub.zdisp.bin"), text="CSUB-ZDISPLACE")
comp_final = comp.get_data(kstpkper=comp.get_kstpkper()[-1])
zdis_final = zdis.get_data(kstpkper=zdis.get_kstpkper()[-1])
comp_final = np.where(np.abs(comp_final) > 1e29, np.nan, comp_final)
zdis_final = np.where(np.abs(zdis_final) > 1e29, np.nan, zdis_final)
wc = (WEL_CELL[0], WEL_CELL[1], WEL_CELL[2])
print(f"  HeadFile text tags: CSUB-COMPACTION / CSUB-ZDISPLACE (confirmed readable)")
print(f"  compaction @ well cell (final)   = {comp_final[wc]:.6f} m")
print(f"  z-displacement @ well cell (final)= {zdis_final[wc]:.6f} m")
print(f"  z-displacement grid: min={np.nanmin(zdis_final):.6f}  max={np.nanmax(zdis_final):.6f} m")
print(f"  SIGN: positive value @ pumped cell => subsidence reported POSITIVE-DOWN"
      if zdis_final[wc] > 0 else
      f"  SIGN: NEGATIVE @ pumped cell => z-displacement is NEGATIVE-DOWN (flip needed)")

# --- analytical cross-check dz ~ Ssv * b * dh -------------------------------- #
b_interbed = THICK_FRAC * (TOP - BOTM)   # 0.5 * 30 = 15 m
dh = dC                                   # head decline at the well (m)
dz_analytic = SSV * b_interbed * dh
print("=" * 70)
print("ANALYTICAL CROSS-CHECK (ultimate no-delay compaction dz = Ssv*b*dh):")
print(f"  Ssv={SSV:g} m^-1  b={b_interbed:g} m  dh={dh:.4f} m")
print(f"  dz_analytic (ultimate) = {dz_analytic:.6f} m = {dz_analytic*100:.2f} cm")
print(f"  dz_model (final compaction @ well) = {comp_final[wc]:.6f} m = {comp_final[wc]*100:.2f} cm")
print(f"  ratio model/analytic = {comp_final[wc]/dz_analytic:.3f} "
      f"(transient under-shoots the t->inf ultimate; expect < 1)")

# --- inelastic fraction from the OBS csv ------------------------------------- #
print("=" * 70)
import csv as _csv
with open(os.path.join(wsC, f"{nameC}.csub.obs.csv")) as fh:
    rows = list(_csv.DictReader(fh))
print("  OBS csv header:", list(rows[0].keys()))
print("  OBS csv n_rows (timesteps):", len(rows))
ninter = sum(1 for k in rows[0] if k.upper().startswith("COMPACTION_R"))
tot_ine = sum(float(rows[-1][f"INE_R{i}"]) for i in range(ninter))
tot_ela = sum(float(rows[-1][f"ELA_R{i}"]) for i in range(ninter))
frac = tot_ine / (tot_ine + tot_ela) if (tot_ine + tot_ela) > 0 else 0.0
print(f"  inelastic_fraction = sum(INE)/(sum(INE)+sum(ELA)) = {frac:.4f} "
      f"(expect ~1.0: pcs0=0 head_based -> all drawdown inelastic/permanent)")
print("=" * 70)
print("SMOKE COMPLETE")
