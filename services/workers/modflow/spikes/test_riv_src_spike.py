"""SPIKE (sprint-17 Wave B, lane J5) — MODFLOW river-seepage FEASIBILITY.

THROWAWAY PROOF, NOT THE PRODUCTION ENGINE. Phase 0 only: confirm that a
self-contained FloPy GWF + RIV + GWT + SRC deck runs to *Normal termination*
on the local ``mf6`` binary and produces a NON-ZERO RIV leakage budget term
(gaining/losing river), with the GWT transport model carrying an along-river
SRC solute source.

This proves the building blocks the production river-coupled seepage engine
(Wave C) will assemble inside ``services/workers/modflow/gwt_adapter.py``:

  * RIV package — a river polyline draped onto a small structured grid, with
    per-cell conductance + stage + rbot. The river head being ABOVE the
    aquifer head (a losing reach) drives a non-zero RIV leakage term in the
    GWF volumetric budget. This is the missing piece the project memory note
    ``project_modflow_river_seepage_demo`` flags: "river-coupled seepage NOT
    yet (needs RIV/SRC + along-river source)".
  * SRC package — a solute mass-loading source placed ALONG the river cells in
    the transport model, so the contaminant enters where the river leaks into
    the aquifer (the seepage plume), mirroring the spill-loading SRC the
    production ``gwt_adapter.build_modflow_deck`` already writes.

The deck mirrors the structure of FloPy's ``ex-gwf-sfr-p01`` reduced to a RIV
boundary (RIV is the simplest head-dependent river flux package; SFR adds a
full streamflow-routing network the v0.1 seepage demo does not need).

Determinism: pure deterministic FloPy + a subprocess mf6 run. No LLM, no AWS.

Run:
    venvs/agent/bin/python -m pytest \
        services/workers/modflow/spikes/test_riv_src_spike.py -q

Binary resolution mirrors the production seam (``run_modflow._mf6_binary``):
    1. ``$TRID3NT_MF6_BIN`` if set,
    2. ``mf6`` on PATH,
    3. a known local install (``~/AGRI-SENTINEL/.modflow/bin/mf6``),
    4. ``flopy.utils.get_modflow`` fetch into a temp dir.
If NONE resolve, the test degrades to DECK-CONSTRUCTION-ONLY: it still builds
+ writes the deck and asserts the .riv/.src input files are correct, and skips
only the live-run assertions (clearly marked) — the live mf6 run must then be
done on the agent box.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import flopy
import numpy as np
import pytest

# --------------------------------------------------------------------------- #
# Local-grid demo constants (a small, fast 1-layer structured grid).
# --------------------------------------------------------------------------- #
NLAY = 1
NROW = 10
NCOL = 20
DELR = 50.0  # column width (m)
DELC = 50.0  # row height (m)
TOP = 10.0
BOTM = -20.0

# Regional west->east constant-head gradient (drives a background flow field).
HEAD_WEST = 9.0
HEAD_EAST = 7.0

KH_M_PER_DAY = 5.0  # hydraulic conductivity (m/day)
POROSITY = 0.25

# The river runs west->east along the middle row. River stage is set ABOVE the
# local aquifer head over the reach so the reach LOSES water to the aquifer ->
# a non-zero RIV leakage term in the GWF budget (the seepage we want to prove).
RIVER_ROW = NROW // 2
RIVER_STAGE = 9.5  # above HEAD_EAST so the eastern reach leaks in
RIVER_RBOT = 8.0
RIVER_COND = 100.0  # per-cell conductance (m^2/day)

# Solute mass-loading along the river (g/day), placed at the river cells in the
# transport model so the contaminant enters with the seepage.
SRC_MASS_G_PER_DAY = 1.0e5

SIM_NAME = "rivspike"
GWF_NAME = "gwf_riv"
GWT_NAME = "gwt_riv"


# --------------------------------------------------------------------------- #
# mf6 binary resolution (mirrors run_modflow._mf6_binary, plus a fetch fallback)
# --------------------------------------------------------------------------- #
def _known_local_mf6() -> str | None:
    candidates = [
        Path.home() / "AGRI-SENTINEL" / ".modflow" / "bin" / "mf6",
        Path.home() / ".local" / "bin" / "mf6",
    ]
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    return None


def _resolve_mf6(fetch_dir: Path) -> str | None:
    """Return a runnable mf6 path, or None if none is available."""
    env = (os.environ.get("TRID3NT_MF6_BIN") or "").strip()
    if env and Path(env).is_file() and os.access(env, os.X_OK):
        return env
    on_path = shutil.which("mf6")
    if on_path:
        return on_path
    local = _known_local_mf6()
    if local:
        return local
    # Last resort: fetch via flopy into a temp dir (network-permitting).
    try:
        from flopy.utils import get_modflow  # type: ignore

        fetch_dir.mkdir(parents=True, exist_ok=True)
        get_modflow(str(fetch_dir))
        cand = fetch_dir / "mf6"
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    except Exception:  # noqa: BLE001 — offline / fetch unavailable -> deck-only
        return None
    return None


# --------------------------------------------------------------------------- #
# Deck builder — self-contained GWF + RIV + GWT + SRC
# --------------------------------------------------------------------------- #
def _build_riv_src_deck(sim_ws: Path, exe_name: str = "mf6") -> flopy.mf6.MFSimulation:
    """Assemble a GWF(+RIV) / GWT(+SRC) MF6 simulation in ``sim_ws``.

    Single steady-state flow stress period + a transient transport period.
    """
    sim = flopy.mf6.MFSimulation(
        sim_name=SIM_NAME,
        sim_ws=str(sim_ws),
        exe_name=exe_name,
        version="mf6",
    )

    # Period 0: steady-state flow spin-up (1 day, 1 step). Period 1: transient
    # transport (100 days, 10 steps) so the SRC source advects with seepage.
    flopy.mf6.ModflowTdis(
        sim,
        time_units="DAYS",
        nper=2,
        perioddata=[(1.0, 1, 1.0), (100.0, 10, 1.0)],
    )

    ims_gwf = flopy.mf6.ModflowIms(
        sim,
        filename=f"{GWF_NAME}.ims",
        complexity="SIMPLE",
        outer_dvclose=1e-7,
        inner_dvclose=1e-7,
        linear_acceleration="CG",
    )
    ims_gwt = flopy.mf6.ModflowIms(
        sim,
        filename=f"{GWT_NAME}.ims",
        complexity="MODERATE",
        outer_dvclose=1e-7,
        inner_dvclose=1e-7,
        linear_acceleration="BICGSTAB",
    )

    # --- GWF flow model ---------------------------------------------------- #
    gwf = flopy.mf6.ModflowGwf(
        sim, modelname=GWF_NAME, model_nam_file=f"{GWF_NAME}.nam", save_flows=True
    )
    sim.register_ims_package(ims_gwf, [GWF_NAME])

    flopy.mf6.ModflowGwfdis(
        gwf,
        length_units="METERS",
        nlay=NLAY,
        nrow=NROW,
        ncol=NCOL,
        delr=DELR,
        delc=DELC,
        top=TOP,
        botm=BOTM,
        filename=f"{GWF_NAME}.dis",
    )
    flopy.mf6.ModflowGwfic(gwf, strt=HEAD_WEST, filename=f"{GWF_NAME}.ic")
    flopy.mf6.ModflowGwfnpf(
        gwf,
        save_flows=True,
        save_specific_discharge=True,
        icelltype=0,  # confined
        k=KH_M_PER_DAY,
        filename=f"{GWF_NAME}.npf",
    )

    # West/east constant-head gradient (background regional flow).
    chd_records: list = []
    for r in range(NROW):
        chd_records.append([(0, r, 0), HEAD_WEST])
        chd_records.append([(0, r, NCOL - 1), HEAD_EAST])
    flopy.mf6.ModflowGwfchd(
        gwf,
        stress_period_data={0: chd_records, 1: chd_records},
        filename=f"{GWF_NAME}.chd",
    )

    # --- RIV package: a west->east river polyline draped on the middle row. -- #
    # Per-cell (cellid, stage, cond, rbot). Stage above the local aquifer head
    # over the eastern reach -> a LOSING reach -> non-zero RIV leakage budget.
    # Skip the boundary columns (they are CHD cells).
    river_cells = [(RIVER_ROW, c) for c in range(1, NCOL - 1)]
    riv_records = [
        [(0, r, c), RIVER_STAGE, RIVER_COND, RIVER_RBOT] for (r, c) in river_cells
    ]
    flopy.mf6.ModflowGwfriv(
        gwf,
        stress_period_data={0: riv_records, 1: riv_records},
        save_flows=True,
        filename=f"{GWF_NAME}.riv",
        pname="riv-0",
    )

    flopy.mf6.ModflowGwfoc(
        gwf,
        head_filerecord=f"{GWF_NAME}.hds",
        budget_filerecord=f"{GWF_NAME}.cbc",
        saverecord=[("HEAD", "ALL"), ("BUDGET", "ALL")],
        filename=f"{GWF_NAME}.oc",
    )

    # --- GWT transport model ----------------------------------------------- #
    gwt = flopy.mf6.ModflowGwt(
        sim, modelname=GWT_NAME, model_nam_file=f"{GWT_NAME}.nam", save_flows=True
    )
    sim.register_ims_package(ims_gwt, [GWT_NAME])

    flopy.mf6.ModflowGwtdis(
        gwt,
        length_units="METERS",
        nlay=NLAY,
        nrow=NROW,
        ncol=NCOL,
        delr=DELR,
        delc=DELC,
        top=TOP,
        botm=BOTM,
        filename=f"{GWT_NAME}.dis",
    )
    flopy.mf6.ModflowGwtic(gwt, strt=0.0, filename=f"{GWT_NAME}.ic")
    flopy.mf6.ModflowGwtadv(gwt, scheme="TVD", filename=f"{GWT_NAME}.adv")
    flopy.mf6.ModflowGwtdsp(
        gwt,
        alh=10.0,
        ath1=1.0,
        atv=0.1,
        filename=f"{GWT_NAME}.dsp",
    )
    flopy.mf6.ModflowGwtmst(gwt, porosity=POROSITY, filename=f"{GWT_NAME}.mst")

    # SRC along the river cells (active only in the transient transport period).
    src_records = [[(0, r, c), SRC_MASS_G_PER_DAY] for (r, c) in river_cells]
    flopy.mf6.ModflowGwtsrc(
        gwt,
        stress_period_data={0: [], 1: src_records},
        filename=f"{GWT_NAME}.src",
        pname="src-0",
    )

    # SSM is required because the GWF model has boundary packages (CHD + RIV).
    # An EMPTY SSM (sources=None) is the MF6 idiom (matches the production
    # gwt_adapter): with no AUXMIXED concentration declared, water entering
    # across CHD and seeping in across RIV carries ZERO concentration (clean
    # regional/river water). The contaminant comes ONLY from the SRC mass
    # source along the river -> the seepage plume is the SRC mass, not the
    # river water itself. (Wave C can later let the river itself carry a
    # concentration via an AUXILIARY on RIV + an SSM AUX source.)
    flopy.mf6.ModflowGwtssm(gwt, sources=None, filename=f"{GWT_NAME}.ssm")

    flopy.mf6.ModflowGwtoc(
        gwt,
        concentration_filerecord=f"{GWT_NAME}.ucn",
        budget_filerecord=f"{GWT_NAME}.cbc",
        saverecord=[("CONCENTRATION", "ALL"), ("BUDGET", "ALL")],
        filename=f"{GWT_NAME}.oc",
    )

    # --- GWF-GWT exchange -------------------------------------------------- #
    flopy.mf6.ModflowGwfgwt(
        sim,
        exgtype="GWF6-GWT6",
        exgmnamea=GWF_NAME,
        exgmnameb=GWT_NAME,
        filename="gwfgwt.exg",
    )
    return sim


# --------------------------------------------------------------------------- #
# Deck-construction-only assertions (always run, no binary needed)
# --------------------------------------------------------------------------- #
def test_riv_src_deck_writes_correct_input_files(tmp_path):
    """Build + write the deck; assert the .riv and .src input files are well
    formed. This is the floor: it runs with or without an mf6 binary."""
    sim = _build_riv_src_deck(tmp_path)
    sim.write_simulation()

    riv = tmp_path / f"{GWF_NAME}.riv"
    src = tmp_path / f"{GWT_NAME}.src"
    assert riv.is_file(), "RIV input file not written"
    assert src.is_file(), "SRC input file not written"

    riv_text = riv.read_text().lower()
    # RIV records carry (cellid stage cond rbot); FloPy writes the floats in
    # scientific notation (e.g. 9.50000000e+00 1.00000000e+02 8.00000000e+00).
    assert "begin period" in riv_text
    assert "9.50000000e+00" in riv_text  # river stage
    assert "1.00000000e+02" in riv_text  # conductance
    assert "8.00000000e+00" in riv_text  # rbot
    # Number of RIV records == number of interior river cells.
    n_riv = sum(
        1
        for line in riv_text.splitlines()
        if line.strip()
        and line.strip().split()[0].isdigit()
        and "maxbound" not in line
    )
    assert n_riv >= (NCOL - 2), f"expected >= {NCOL - 2} RIV records, got {n_riv}"

    src_text = src.read_text().lower()
    assert "begin period" in src_text
    # SRC mass rate written in scientific notation, e.g. 1.00000000E+05.
    assert "e+05" in src_text or "100000" in src_text


# --------------------------------------------------------------------------- #
# LIVE run assertions (skip cleanly if no mf6 binary resolvable)
# --------------------------------------------------------------------------- #
def test_riv_src_runs_to_normal_termination_with_nonzero_leakage(tmp_path):
    """The GO assertion: mf6 terminates normally AND the GWF budget carries a
    NON-ZERO RIV leakage term AND the GWT transport ran with the SRC source."""
    mf6 = _resolve_mf6(tmp_path / "_mf6_fetch")
    if mf6 is None:
        pytest.skip(
            "no mf6 binary resolvable (env/PATH/known-local/fetch all failed) "
            "-- deck-construction-only here; live run must be done on the box"
        )

    sim_ws = tmp_path / "run"
    sim = _build_riv_src_deck(sim_ws, exe_name=mf6)
    sim.write_simulation()

    success, buff = sim.run_simulation(silent=True)
    assert success, f"mf6 did NOT report success. tail:\n{''.join(buff[-20:])}"

    # 1) Normal termination. mf6 writes the terminal banner to the
    #    SIMULATION list file (mfsim.lst), NOT the per-model .lst files.
    sim_lst = (sim_ws / "mfsim.lst").read_text()
    assert "Normal termination of simulation" in sim_lst, (
        "mfsim.lst does not report Normal termination"
    )

    # 2) NON-ZERO RIV leakage in the GWF cell-by-cell budget. The RIV budget
    #    record is a recarray with a 'q' field (per-reach-cell exchange flow);
    #    a losing river (stage above aquifer head) yields a non-zero sum.
    cbc = flopy.utils.CellBudgetFile(str(sim_ws / f"{GWF_NAME}.cbc"))
    record_names = {
        (r.strip() if isinstance(r, str) else r.strip().decode())
        for r in cbc.get_unique_record_names(decode=True)
    }
    assert any("RIV" in n.upper() for n in record_names), (
        f"no RIV record in GWF budget; have {sorted(record_names)}"
    )
    riv_data = cbc.get_data(text="RIV")
    assert riv_data, "RIV budget record present but empty"
    last = riv_data[-1]
    q = np.asarray(last["q"], dtype=float)
    total_abs_leakage = float(np.sum(np.abs(q)))
    assert total_abs_leakage > 1e-6, (
        f"RIV leakage is effectively zero ({total_abs_leakage}); "
        "river is not exchanging water with the aquifer"
    )

    # 3) GWT transport ran with the SRC source -> a concentration field exists
    #    and is non-zero somewhere (the seepage plume).
    ucn = flopy.utils.HeadFile(
        str(sim_ws / f"{GWT_NAME}.ucn"), text="concentration"
    )
    conc = ucn.get_data(totim=ucn.get_times()[-1])
    assert float(np.nanmax(conc)) > 0.0, (
        "transport concentration field is all zero -> SRC source did not load"
    )

    # Stash the proven leakage magnitude for the spike finding (visible in -s).
    print(
        f"\n[SPIKE] RIV leakage total |q| = {total_abs_leakage:.3f} m^3/day "
        f"over {q.size} reach cells; max conc = {float(np.nanmax(conc)):.3g}"
    )


if __name__ == "__main__":  # pragma: no cover - manual spike driver
    import sys

    sys.exit(pytest.main([__file__, "-q"]))
