"""Local GWE (MODFLOW 6 Groundwater Energy) heat-transport physics sandbox.

Direct-call proof of the GWE deck physics BEFORE productionizing into the
adapter (local-first doctrine). Builds standalone FloPy GWF+GWE decks and runs
them through the LOCAL mf6 6.7.0 binary, then asserts MONOTONE heat-transport
physics. No product code imported -- this validates that the package wiring
(GWE + CND/EST + ESL/CTP/SSM + GWF6-GWE6 exchange) reproduces the expected
thermal behaviour that the ex-gwe modflow6-examples cases demonstrate.

Modes proven (heat-twin-of-plume archetype family):
  A. injection_plume -- warm-water injection well drives a downgradient thermal
     plume (the heat twin of contaminant_plume). Assertions:
       (A1) plume warm-cell extent grows monotonically with injection dT
            (heat twin of "plume extent grows with source strength");
       (A2) an advective field shifts the thermal centroid DOWNGRADIENT vs a
            conduction-only (no-flow) field whose plume stays radially centered
            (advection-dominated vs conduction-dominated regimes DIFFER).
  B. ates_cycling -- seasonal inject-warm / recover cycle. Assertions:
       (B1) single-cycle recovery efficiency is strictly between 0 and 1
            (recovery efficiency < 100%);
       (B2) recovery efficiency RISES with cycle count (the aquifer thermal
            buffer pre-warms, so later cycles recover a larger fraction).

Units: SI throughout (LENGTH=METERS, TIME=SECONDS) so thermal conductivity
[W/m/degC = J/s/m/degC], heat capacity [J/kg/degC] and density [kg/m3] are
unambiguous. The productionized adapter converts to its DAYS time base.

Run:
  cd /home/nate/Documents/trid3nt-local
  TRID3NT_MF6_BIN=$PWD/bin/mf6 venvs/agent/bin/python \
    scripts/sandbox/modflow/gwe_thermal_physics_sandbox.py
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import flopy
import numpy as np

MF6 = os.environ.get("TRID3NT_MF6_BIN", str(Path.cwd() / "bin" / "mf6"))
WORK = Path("/tmp/claude-1000/-home-nate-Documents-GRACE-2/"
            "fd2df08a-a572-4b62-ba9a-e82d8a0a740e/scratchpad/gwe_sandbox")

# --- domain / physics constants (SI) ------------------------------------- #
NROW = NCOL = 41
DELR = DELC = 10.0                 # m
TOP, BOTM = 0.0, -20.0             # 20 m saturated thickness
K_MS = 1.0e-4                      # hydraulic conductivity, m/s
POROSITY = 0.20
AMBIENT_T = 10.0                   # degC, initial + boundary temperature
# thermal properties (typical saturated sand/water) -- SI
CPW, RHOW = 4184.0, 1000.0         # water heat capacity J/kg/degC, density kg/m3
CPS, RHOS = 800.0, 2650.0         # solid grain heat capacity, density
KTW, KTS = 0.56, 2.5              # thermal conductivity of water / solid, W/m/degC
ALH = 1.0                         # longitudinal thermal dispersivity, m
CENTER = NROW // 2                # well cell index (center)
DAY = 86400.0


def _sim(ws: Path, nper_periods):
    ws.mkdir(parents=True, exist_ok=True)
    sim = flopy.mf6.MFSimulation(sim_name="gwe", sim_ws=str(ws),
                                 exe_name=MF6, version="mf6")
    flopy.mf6.ModflowTdis(sim, time_units="SECONDS", nper=len(nper_periods),
                          perioddata=nper_periods)
    return sim


def _gwf(sim, *, regional_gradient=0.0, wel_spd=None, wel_aux=False):
    name = "flow"
    flopy.mf6.ModflowIms(sim, filename=f"{name}.ims", complexity="SIMPLE",
                         outer_dvclose=1e-8, inner_dvclose=1e-8,
                         linear_acceleration="CG")
    gwf = flopy.mf6.ModflowGwf(sim, modelname=name, save_flows=True)
    sim.register_ims_package(sim.get_package(f"{name}.ims"), [name])
    flopy.mf6.ModflowGwfdis(gwf, nlay=1, nrow=NROW, ncol=NCOL, delr=DELR,
                            delc=DELC, top=TOP, botm=BOTM)
    head_w = TOP + regional_gradient * NCOL * DELR
    flopy.mf6.ModflowGwfic(gwf, strt=head_w)
    flopy.mf6.ModflowGwfnpf(gwf, save_flows=True, save_specific_discharge=True,
                            icelltype=0, k=K_MS)
    if regional_gradient != 0.0:
        chd = []
        for r in range(NROW):
            chd.append([(0, r, 0), head_w])
            chd.append([(0, r, NCOL - 1), TOP])
        flopy.mf6.ModflowGwfchd(gwf, stress_period_data={0: chd})
    else:
        # pin one corner so the no-flow system is well-posed
        flopy.mf6.ModflowGwfchd(gwf, stress_period_data={0: [[(0, 0, 0), TOP]]})
    if wel_spd is not None:
        kw = {"auxiliary": ["TEMPERATURE"]} if wel_aux else {}
        flopy.mf6.ModflowGwfwel(gwf, stress_period_data=wel_spd,
                                pname="wel", **kw)
    flopy.mf6.ModflowGwfoc(gwf, head_filerecord=f"{name}.hds",
                           budget_filerecord=f"{name}.cbc",
                           saverecord=[("HEAD", "LAST"), ("BUDGET", "LAST")])
    return gwf


def _gwe(sim, *, ctp_spd=None, ssm_from_wel=False):
    name = "energy"
    flopy.mf6.ModflowIms(sim, filename=f"{name}.ims", complexity="MODERATE",
                         outer_dvclose=1e-8, inner_dvclose=1e-8,
                         linear_acceleration="BICGSTAB")
    gwe = flopy.mf6.ModflowGwe(sim, modelname=name, save_flows=True)
    sim.register_ims_package(sim.get_package(f"{name}.ims"), [name])
    flopy.mf6.ModflowGwedis(gwe, nlay=1, nrow=NROW, ncol=NCOL, delr=DELR,
                            delc=DELC, top=TOP, botm=BOTM)
    flopy.mf6.ModflowGweic(gwe, strt=AMBIENT_T)
    flopy.mf6.ModflowGweadv(gwe, scheme="TVD")
    flopy.mf6.ModflowGwecnd(gwe, alh=ALH, ath1=ALH * 0.1, ktw=KTW, kts=KTS)
    flopy.mf6.ModflowGweest(gwe, porosity=POROSITY, heat_capacity_water=CPW,
                            density_water=RHOW, heat_capacity_solid=CPS,
                            density_solid=RHOS)
    if ctp_spd is not None:
        flopy.mf6.ModflowGwectp(gwe, stress_period_data=ctp_spd)
    if ssm_from_wel:
        flopy.mf6.ModflowGwessm(gwe, sources=[["wel", "AUX", "TEMPERATURE"]])
    else:
        flopy.mf6.ModflowGwessm(gwe, sources=[[]])
    flopy.mf6.ModflowGweoc(gwe, temperature_filerecord=f"{name}.ucn",
                           budget_filerecord=f"{name}.cbc",
                           saverecord=[("TEMPERATURE", "ALL"), ("BUDGET", "LAST")])
    flopy.mf6.ModflowGwfgwe(sim, exgtype="GWF6-GWE6", exgmnamea="flow",
                            exgmnameb="energy")
    return gwe


def _read_temp(ws: Path):
    fp = flopy.utils.HeadFile(str(ws / "energy.ucn"), text="TEMPERATURE")
    return fp.get_alldata()  # (ntimes, nlay, nrow, ncol)


def _run(sim, ws: Path) -> None:
    sim.write_simulation()
    ok, buff = sim.run_simulation(silent=True)
    if not ok:
        raise RuntimeError(f"mf6 failed in {ws}:\n" + "\n".join(buff[-20:]))


# ======================================================================== #
# MODE A -- injection thermal plume
# ======================================================================== #
def mode_a_injection_plume():
    print("\n=== MODE A: injection thermal plume ===")
    Q = 5.0e-3          # m3/s warm-water injection
    sim_secs = 120 * DAY
    perioddata = [(1.0, 1, 1.0), (sim_secs, 60, 1.2)]  # steady spin-up + transient

    def build_run(tag, inj_dT, regional_gradient):
        ws = WORK / tag
        if ws.exists():
            shutil.rmtree(ws)
        inj_T = AMBIENT_T + inj_dT
        wel_spd = {0: [], 1: [[(0, CENTER, CENTER), Q, inj_T]]}
        sim = _sim(ws, perioddata)
        _gwf(sim, regional_gradient=regional_gradient, wel_spd=wel_spd, wel_aux=True)
        _gwe(sim, ssm_from_wel=True)
        _run(sim, ws)
        temp = _read_temp(ws)[-1, 0]      # final temperature field
        return temp

    # A1: warm-cell extent grows with injection dT (advective field)
    extents = {}
    for dT in (5.0, 15.0, 30.0):
        temp = build_run(f"a1_dT{int(dT)}", dT, REGIONAL := 0.002)
        warm = int((temp > AMBIENT_T + 0.5).sum())   # cells warmer than +0.5 degC
        extents[dT] = warm
        print(f"  A1 dT=+{dT:>4.0f} degC -> warm-cell count = {warm}")
    a1 = extents[5.0] < extents[15.0] < extents[30.0]
    print(f"  [A1] monotone extent vs dT: {a1}")

    # A2: advection shifts centroid downgradient; conduction-only stays centered.
    # Use a fixed-temperature (CTP) hot cell -- NO water injection -- so the only
    # transport is by the ambient flow field: a regional gradient (advective) vs
    # no flow (pure conduction). This isolates advection from conduction cleanly.
    def build_ctp(tag, regional_gradient):
        ws = WORK / tag
        if ws.exists():
            shutil.rmtree(ws)
        ctp = {0: [], 1: [[(0, CENTER, CENTER), AMBIENT_T + 40.0]]}
        sim = _sim(ws, perioddata)
        _gwf(sim, regional_gradient=regional_gradient, wel_spd=None, wel_aux=False)
        _gwe(sim, ctp_spd=ctp, ssm_from_wel=False)
        _run(sim, ws)
        return _read_temp(ws)[-1, 0]

    temp_adv = build_ctp("a2_adv", 0.010)             # strong regional flow W->E
    temp_cond = build_ctp("a2_cond", 0.0)             # no regional flow
    def centroid_col(temp):
        w = np.clip(temp - AMBIENT_T, 0, None)
        cols = np.arange(NCOL)[None, :]
        return float((w * cols).sum() / w.sum())
    shift_adv = centroid_col(temp_adv) - CENTER
    shift_cond = centroid_col(temp_cond) - CENTER
    print(f"  A2 advective centroid col-shift  = {shift_adv:+.2f} cells")
    print(f"  A2 conduction centroid col-shift = {shift_cond:+.2f} cells")
    a2 = shift_adv > 0.5 and abs(shift_cond) < 0.25
    print(f"  [A2] advection downgradient vs conduction centered: {a2}")
    return a1, a2


# ======================================================================== #
# MODE B -- ATES seasonal charge / recover
# ======================================================================== #
def mode_b_ates(n_cycles: int):
    """One cycle = inject warm (season 1) then extract (season 2).

    Recovery efficiency = mean(T_produced - T_ambient) over the extraction
    season / (T_inject - T_ambient). Reads the well-cell temperature during the
    extraction period. Returns the LAST-cycle recovery efficiency.
    """
    ws = WORK / f"ates_c{n_cycles}"
    if ws.exists():
        shutil.rmtree(ws)
    season = 90 * DAY
    inj_T = AMBIENT_T + 40.0            # inject 50 degC water
    Qin, Qout = 3.0e-3, -3.0e-3
    # periods: [steady spin-up] + per cycle [inject, extract]
    perioddata = [(1.0, 1, 1.0)]
    wel_spd = {0: []}
    p = 1
    extract_periods = []
    for _ in range(n_cycles):
        perioddata.append((season, 30, 1.1))          # inject
        wel_spd[p] = [[(0, CENTER, CENTER), Qin, inj_T]]
        p += 1
        perioddata.append((season, 30, 1.1))          # extract
        wel_spd[p] = [[(0, CENTER, CENTER), Qout, AMBIENT_T]]
        extract_periods.append(p)
        p += 1
    sim = _sim(ws, perioddata)
    _gwf(sim, regional_gradient=0.0, wel_spd=wel_spd, wel_aux=True)
    _gwe(sim, ssm_from_wel=True)
    _run(sim, ws)

    # temperature time series at the well cell
    fp = flopy.utils.HeadFile(str(ws / "energy.ucn"), text="TEMPERATURE")
    times = fp.get_times()
    kstpkper = fp.get_kstpkper()
    # map each output record to its (period) index
    last_extract = extract_periods[-1]
    prod_T = [fp.get_data(kstpkper=kk)[0, CENTER, CENTER]
              for kk in kstpkper if kk[1] == last_extract]
    eff = (np.mean(prod_T) - AMBIENT_T) / (inj_T - AMBIENT_T)
    print(f"  ATES cycles={n_cycles}: mean produced T={np.mean(prod_T):.2f} degC, "
          f"recovery eff={eff:.3f}")
    return float(eff)


def main():
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)
    print(f"mf6 = {MF6}")

    a1, a2 = mode_a_injection_plume()

    print("\n=== MODE B: ATES seasonal charge/recover ===")
    eff1 = mode_b_ates(1)
    eff2 = mode_b_ates(2)
    eff3 = mode_b_ates(3)
    b1 = 0.0 < eff1 < 1.0
    b2 = eff1 < eff2 < eff3
    print(f"  [B1] 0 < eff < 1 (single cycle): {b1}")
    print(f"  [B2] recovery efficiency rises with cycle count: {b2}")

    print("\n=== SUMMARY ===")
    results = {"A1_extent_vs_dT": a1, "A2_advection_vs_conduction": a2,
               "B1_eff_bounded": b1, "B2_eff_rises_with_cycles": b2}
    for k, v in results.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    allgreen = all(results.values())
    print(f"\n{'ALL GREEN' if allgreen else 'SOME RED'}")
    return 0 if allgreen else 1


if __name__ == "__main__":
    raise SystemExit(main())
