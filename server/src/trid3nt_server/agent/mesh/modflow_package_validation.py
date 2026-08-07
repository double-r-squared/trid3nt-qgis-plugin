"""Package-validation engine core for the MODFLOW V&V templates (ADR 0153).

Where the archetype composers build a place-based demo aquifer and render a map
layer, this core authors SMALL SYNTHETIC BENCHMARK decks that isolate a single
MF6 package and reproduce a PUBLISHED or ANALYTICAL reference, then solve each
through the local ``mf6`` binary and extract the computed-vs-reference quantity.
The product is a computed-vs-reference CHART plus typed scalars - NEVER a
georeferenced map (the decks are schematic, local model units).

Five cases, each exercising a package no archetype composer exposes:

- ``newton_dry_rewet`` (GWF-NPF / STO): the Zaidel (2013) staircase channel
  (modflow6-examples:ex-gwf-zaidel). A 200-cell unconfined channel over a
  descending staircase impervious base with constant heads 23 -> 10. The NEWTON
  formulation keeps the drying/rewetting cells active and yields a monotone
  staircase water table; the STANDARD formulation collapses cells to dry
  (nonphysical). The case is a solver-robustness CONTRAST (no published
  analytical array), so it reports the Newton-vs-standard dry-cell counts.

- ``maw_crossaquifer`` (GWF-MAW): a non-pumping multi-aquifer well casing
  connecting two confined aquifers (modflow6-examples:ex-gwf-maw-p01). The well
  short-circuits the aquifers and equilibrates to the Sokol (1963)
  transmissivity-weighted analytical level (T1*h1 + T2*h2)/(T1+T2). A free V&V
  point: computed MAW head vs the analytical level.

- ``hfb_barrier`` (GWF-HFB): a defined-thickness horizontal-flow barrier between
  two cell columns (modflow6-docs:gwf-hfb). The steady cross-barrier flux equals
  the HYDCHR barrier-conductance flux (HYDCHR * area * dh) and is INDEPENDENT of
  grid refinement (HYDCHR is a per-unit-area characteristic). The case solves the
  same domain at several column counts and reports the flux at each.

- ``prt_capture_zone`` (native mf6 PRT): a confined well in regional through-flow
  (CHD inflow / RIV discharge) tracked FORWARD from the inflow boundary
  (pathlines + travel times; the captured band width) and BACKWARD from the well
  through the reversed flow field (the capture zone; the down-gradient stagnation
  excursion). Both are checked against the Grubb (1993) uniform-flow capture-zone
  analytical (stagnation x_s = Q/(2*pi*U), width W = Q/U). The ``direction`` /
  ``n_particles`` knobs select the tracking direction shown and the release-ring
  size. Native PRT ships in mf6 6.7.0 - no MODPATH 7 install is needed (the
  ADR 0153 STOP was moot); an EXACT PRT-vs-MODPATH7 cross-tool match remains a
  future recipe (install mp7, run both off the same GWF output).

- ``henry_saltwater`` (GWF-BUY + GWT): the classic Henry (1964) coastal
  cross-section (modflow6-examples:ex-gwt-henry-a). BUY couples the GWT salt
  concentration to fluid density; a freshwater WEL inflow inland opposes a 35 ppt
  GHB seawater boundary seaward. The case reports the 0.5-isochlor toe and the
  wedge pattern (monotone stratification, fresh-top-inland / salt-bottom-seaward)
  against the published wedge.

- ``sfr_stream_depletion`` (GWF-SFR + WEL): a well-connected SFR stream and a
  pumping well in a confined aquifer. Stream depletion (the fraction of the
  pumping rate captured from the stream) is extracted by SUPERPOSITION - the
  SFR->GWF leakage difference between a pumping and a no-pumping run - and checked
  against the Glover & Balmer (1954) transient analytical q(t)/Q = erfc(sqrt(a^2
  S/(4 T t))) across a sequence of times. A well-connected streambed reproduces the
  fully-penetrating Glover curve; realistic streambed resistance sits below it.

- ``mvr_routing`` (GWF-MVR): a synthetic watershed cell block where a UZF column
  rejects the infiltration exceeding its vertical Ks and a DRN discharges
  groundwater, with MVR routing BOTH into the head reach of an SFR network. The
  case checks mover mass CONSERVATION - the volume SFR receives (FROM-MVR) equals
  the sum drawn from the providers (UZF rejected-infiltration + DRN discharge
  TO-MVR) to machine precision, within one coupled timestep.

Honesty (loud): every number is a real parsed mf6 output (invariant 1); the
decks are AUTHORED synthetic benchmarks labeled ``SyntheticInput(basis=
"default_demo")`` by the composer.
"""

from __future__ import annotations

import csv
import logging
import math
import os
import shutil
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger("trid3nt_server.agent.mesh.modflow_package_validation")

__all__ = [
    "ValidationCaseMeta",
    "SolvedValidation",
    "VALIDATION_CASES",
    "resolve_mf6_binary",
    "run_validation_case",
    "ModflowValidationError",
    "build_prt_gwf",
    "build_henry_saltwater",
    "build_glover_sfr",
    "build_mvr_routing",
    "HFB_GRID_NCOLS",
    "PRT_STAGNATION_M",
    "HENRY_TOE_PENETRATION_REF_M",
    "GLOVER_A_M",
    "GLOVER_TIMES_D",
    "glover_depletion_fraction",
]

#: mf6 dry-cell / no-flow sentinel written by the standard (non-Newton)
#: formulation when a convertible cell goes dry (a large-magnitude value).
_HDRY_SENTINEL = -1.0e30


class ModflowValidationError(RuntimeError):
    """A validation case could not be authored or solved (typed for the tool)."""

    error_code: str = "MODFLOW_VALIDATION_ERROR"


@dataclass(frozen=True)
class ValidationCaseMeta:
    """Static metadata for one validation case (question + reference + provenance)."""

    case: str
    question: str
    package: str
    reference_label: str
    reference_source: str
    basis_note: str
    tolerance: float


VALIDATION_CASES: dict[str, ValidationCaseMeta] = {
    "newton_dry_rewet": ValidationCaseMeta(
        case="newton_dry_rewet",
        question=(
            "can the solver handle a staircase impervious base drying and "
            "rewetting an unconfined channel without oscillation or failure?"
        ),
        package="GWF-NPF (Newton) / STO",
        reference_label=(
            "Newton-vs-standard robustness contrast (Newton keeps the channel "
            "wet in a monotone staircase; the standard formulation collapses "
            "cells to dry)"
        ),
        reference_source="modflow6-examples:ex-gwf-zaidel (Zaidel, 2013)",
        basis_note=(
            "Zaidel 200x1x1 staircase channel: top=25 m, botm steps 20/15/10/5/0 "
            "at cols 40/80/120/160, k=1e-4 m/d, CHD 23 m -> 10 m. Synthetic "
            "benchmark, not a site."
        ),
        tolerance=0.0,
    ),
    "maw_crossaquifer": ValidationCaseMeta(
        case="maw_crossaquifer",
        question=(
            "does a non-pumping multi-aquifer well equilibrate to the Sokol "
            "(1963) transmissivity-weighted analytical level between two "
            "confined aquifers?"
        ),
        package="GWF-MAW",
        reference_label="Sokol (1963) transmissivity-weighted analytical well level",
        reference_source="modflow6-examples:ex-gwf-maw-p01 (Sokol, 1963)",
        basis_note=(
            "Two confined aquifers (T_upper=92.9, T_lower=371.6 m2/d) driven to "
            "unequal heads; a non-pumping MAW casing connects both layers at the "
            "domain centre. Synthetic benchmark, not a site."
        ),
        tolerance=1.0e-6,  # relative error on the well head
    ),
    "hfb_barrier": ValidationCaseMeta(
        case="hfb_barrier",
        question=(
            "can a defined-thickness barrier reduce cross-wall flux to a target "
            "hydraulic characteristic, independent of grid refinement?"
        ),
        package="GWF-HFB",
        reference_label="HYDCHR barrier-conductance flux (HYDCHR * area * dh)",
        reference_source="modflow6-docs:gwf-hfb",
        basis_note=(
            "A 1000 m single-layer domain split by an HFB (HYDCHR=1e-6 1/d) with "
            "CHD 10 m -> 1 m, solved at 10/20/40/80 columns. Synthetic benchmark, "
            "not a site."
        ),
        tolerance=1.0e-2,  # relative error at the finest grid
    ),
    "prt_capture_zone": ValidationCaseMeta(
        case="prt_capture_zone",
        question=(
            "does native MF6 PRT particle tracking delineate a pumping well's "
            "capture zone (backward from the well) and pathlines/travel times "
            "(forward from the regional inflow), matching the Grubb (1963/1993) "
            "uniform-flow capture-zone analytical?"
        ),
        package="PRT (native mf6 particle tracking) on GWF-WEL/CHD/RIV",
        reference_label=(
            "Grubb (1993) uniform-flow capture-zone analytical: down-gradient "
            "stagnation distance x_s = Q/(2*pi*K*b*i) and up-gradient capture "
            "width W = Q/(K*b*i)"
        ),
        reference_source="Grubb (1993) Ground Water 31(1):27-32; mf6-examples:ex-prt-mp7-p01",
        basis_note=(
            "Confined single-layer 1210x1010 m domain (K=10 m/d, b=20 m), a "
            "regional gradient set by a CHD inflow (west) and a RIV discharge "
            "boundary (east), and a Q=60 m3/d well at centre. Native mf6 PRT "
            "(ModflowPrt + ModflowEms), backward tracking via the reversed GWF "
            "flow field. Synthetic benchmark, not a site."
        ),
        tolerance=1.0e-1,  # relative error on the stagnation distance
    ),
    "henry_saltwater": ValidationCaseMeta(
        case="henry_saltwater",
        question=(
            "does the BUY variable-density package on a GWF-GWT pair reproduce "
            "the classic Henry saltwater-intrusion wedge (the 0.5 isochlor "
            "shape)?"
        ),
        package="GWF-BUY (variable density) + GWT",
        reference_label=(
            "Henry (1964) saltwater-intrusion wedge: a stable interface with the "
            "0.5-relative-salinity isochlor toe at ~0.6 fractional inland "
            "penetration (bottom layer)"
        ),
        reference_source="modflow6-examples:ex-gwt-henry-a (Henry, 1964)",
        basis_note=(
            "Henry 40-layer x 80-column vertical cross-section (2.0 m x 1.0 m), "
            "K=864 m/d, porosity=0.35, diffc=0.57024, freshwater WEL inflow "
            "5.7024 m3/d (inland) vs a 35 ppt GHB seawater boundary (seaward), "
            "BUY drhodc=0.7. Synthetic benchmark, not a site."
        ),
        tolerance=0.20,  # relative band on the 0.5-isochlor toe penetration
    ),
    "sfr_stream_depletion": ValidationCaseMeta(
        case="sfr_stream_depletion",
        question=(
            "does a MODFLOW 6 SFR-coupled pumping well reproduce the Glover (1954) "
            "analytical stream-depletion curve - the fraction of the pumping rate "
            "captured from a nearby stream as a function of time?"
        ),
        package="GWF-SFR (streamflow routing) + WEL",
        reference_label=(
            "Glover-Balmer (1954) transient stream-depletion fraction q(t)/Q = "
            "erfc(sqrt(a^2 S / (4 T t))) for a fully-penetrating stream"
        ),
        reference_source="Glover & Balmer (1954) Eos Trans. AGU 35(3):468-470",
        basis_note=(
            "Confined single-layer 6000x4500 m domain (T=200 m2/d, S=0.1), a "
            "well-connected SFR stream along the west edge, and a Q=400 m3/d well "
            "300 m from the stream pumped from t=0. Stream depletion is the SFR->GWF "
            "leakage difference between a pumping and a no-pumping run (superposition). "
            "Synthetic benchmark, not a site."
        ),
        tolerance=0.15,  # max relative error over the resolved (q/Q>=0.05) window
    ),
    "mvr_routing": ValidationCaseMeta(
        case="mvr_routing",
        question=(
            "can the MVR (Mover) package transfer rejected UZF infiltration and "
            "groundwater discharge into SFR reaches within one coupled timestep, "
            "conserving mass exactly?"
        ),
        package="GWF-MVR (Mover) coupling UZF + DRN -> SFR",
        reference_label=(
            "mover mass conservation: total volume received by SFR (FROM-MVR) equals "
            "the sum drawn from the providers (UZF rejected-infiltration + DRN "
            "discharge TO-MVR), to machine precision"
        ),
        reference_source="modflow6-docs:gwf-mvr; ex-gwf-sfr/uzf watershed tradition",
        basis_note=(
            "Synthetic 10x12 x 100 m watershed cell block: a UZF column with "
            "infiltration (2 m/d) exceeding vertical Ks (0.5 m/d) rejects the excess, "
            "a DRN discharges groundwater, and MVR routes BOTH into the head reach of "
            "an 8-reach SFR network. Synthetic benchmark, not a site."
        ),
        tolerance=1.0e-6,  # relative conservation error
    ),
}


@dataclass
class SolvedValidation:
    """The solved result of one validation case (engine-core, pre-contract)."""

    case: str
    computed_value: float | None
    reference_value: float | None
    delta: float | None
    relative_error: float | None
    validated: bool
    tolerance: float
    metrics: dict[str, Any] = field(default_factory=dict)
    chart_spec: dict[str, Any] | None = None
    chart_title: str = ""
    chart_caption: str = ""


# --------------------------------------------------------------------------- #
# mf6 binary resolution (mirrors run_modflow._local_mf6_bin: env -> PATH ->
# repo bin/mf6 fallback so the offline V&V smoke runs without $TRID3NT_MF6_BIN).
# --------------------------------------------------------------------------- #


def resolve_mf6_binary() -> str | None:
    """Return a runnable ``mf6`` path, or None when no binary is available.

    Resolution order: ``$TRID3NT_MF6_BIN`` -> ``mf6`` on PATH -> the repo-root
    ``bin/mf6`` (the checked-in local binary). Returns None so the caller can
    raise a typed error / the offline test can skip the solve.
    """
    env = os.environ.get("TRID3NT_MF6_BIN")
    if env:
        p = shutil.which(env) or (env if os.path.isfile(env) and os.access(env, os.X_OK) else None)
        if p:
            return p
    onpath = shutil.which("mf6")
    if onpath:
        return onpath
    # repo-root bin/mf6 (this file: <repo>/server/src/trid3nt_server/agent/mesh/..)
    repo_bin = Path(__file__).resolve().parents[5] / "bin" / "mf6"
    if repo_bin.is_file() and os.access(repo_bin, os.X_OK):
        return str(repo_bin)
    return None


def _new_ws(prefix: str) -> str:
    return tempfile.mkdtemp(prefix=f"mf_vv_{prefix}_")


# --------------------------------------------------------------------------- #
# Deck authoring (flopy). Each ``build_*`` writes a runnable mf6 deck to a temp
# workspace and returns (sims_by_name, ws) WITHOUT running - so the offline test
# asserts the input contract with no mf6 dependency.
# --------------------------------------------------------------------------- #


def _zaidel_botm(ncol: int) -> np.ndarray:
    botm = np.zeros((1, 1, ncol), dtype=float)
    base = 20.0
    for j in range(ncol):
        botm[0, :, j] = base
        if j + 1 in (40, 80, 120, 160):
            base -= 5.0
    return botm


def build_newton_dry_rewet(ws: str | None = None):
    """Author the Zaidel channel twice: NEWTON and STANDARD formulations."""
    import flopy

    mf6 = resolve_mf6_binary()
    ws = ws or _new_ws("newton")
    ncol, delr = 200, 5.0
    botm = _zaidel_botm(ncol)
    sims: dict[str, Any] = {}
    for name, newton in (("newton", True), ("standard", False)):
        sub = os.path.join(ws, name)
        sim = flopy.mf6.MFSimulation(sim_name=name, sim_ws=sub, exe_name=mf6 or "mf6")
        flopy.mf6.ModflowTdis(sim, nper=1, perioddata=[(1.0, 1, 1.0)])
        gwf = flopy.mf6.ModflowGwf(
            sim, modelname=name,
            newtonoptions="newton" if newton else None, save_flows=True,
        )
        flopy.mf6.ModflowIms(
            sim,
            complexity="complex" if newton else "simple",
            linear_acceleration="bicgstab" if newton else "cg",
            outer_maximum=100, inner_maximum=100,
            outer_dvclose=1e-6, inner_dvclose=1e-7,
        )
        flopy.mf6.ModflowGwfdis(gwf, nlay=1, nrow=1, ncol=ncol, delr=delr,
                                delc=1.0, top=25.0, botm=botm)
        flopy.mf6.ModflowGwfic(gwf, strt=23.0)
        flopy.mf6.ModflowGwfnpf(gwf, icelltype=1, k=1e-4)
        flopy.mf6.ModflowGwfchd(gwf, stress_period_data=[
            [(0, 0, 0), 23.0], [(0, 0, ncol - 1), 10.0]])
        flopy.mf6.ModflowGwfoc(gwf, head_filerecord=f"{name}.hds",
                               saverecord=[("HEAD", "ALL")])
        sim.write_simulation(silent=True)
        sims[name] = sim
    return sims, ws, botm.ravel()


def build_maw_crossaquifer(ws: str | None = None):
    """Author the two-confined-aquifer non-pumping MAW well benchmark."""
    import flopy

    mf6 = resolve_mf6_binary()
    ws = ws or _new_ws("maw")
    nlay, nrow, ncol = 2, 21, 21
    delr = delc = 100.0
    top = 10.0
    botm = [-40.0, -80.0]
    b1, b2 = top - botm[0], botm[0] - botm[1]
    # transmissivities pinned to ex-gwf-maw-p01 (T_upper=92.9, T_lower=371.6)
    k1, k2 = 92.9 / b1, 371.6 / b2
    h1, h2 = 5.0, 8.66  # unequal driving heads on the two aquifers
    sim = flopy.mf6.MFSimulation(sim_name="maw", sim_ws=ws, exe_name=mf6 or "mf6")
    flopy.mf6.ModflowTdis(sim, nper=1, perioddata=[(1.0, 1, 1.0)])
    gwf = flopy.mf6.ModflowGwf(sim, modelname="maw", save_flows=True)
    flopy.mf6.ModflowIms(sim, complexity="simple", outer_maximum=200,
                         inner_maximum=200, outer_dvclose=1e-9, inner_dvclose=1e-10)
    flopy.mf6.ModflowGwfdis(gwf, nlay=nlay, nrow=nrow, ncol=ncol, delr=delr,
                            delc=delc, top=top, botm=botm)
    flopy.mf6.ModflowGwfic(gwf, strt=6.0)
    # near-zero vertical K -> the ONLY cross-aquifer path is the MAW casing.
    flopy.mf6.ModflowGwfnpf(gwf, icelltype=0, k=[k1, k2], k33=[1e-16, 1e-16])
    d: dict[tuple, float] = {}
    for r in range(nrow):
        for c in (0, ncol - 1):
            d[(0, r, c)] = h1
            d[(1, r, c)] = h2
    for c in range(ncol):
        for r in (0, nrow - 1):
            d[(0, r, c)] = h1
            d[(1, r, c)] = h2
    flopy.mf6.ModflowGwfchd(gwf, stress_period_data=[[k, v] for k, v in d.items()])
    wc = (nrow // 2, ncol // 2)
    connd = [
        [0, 0, (0, wc[0], wc[1]), top, botm[0], 1.0, 0.15],
        [0, 1, (1, wc[0], wc[1]), botm[0], botm[1], 1.0, 0.15],
    ]
    flopy.mf6.ModflowGwfmaw(
        gwf, nmawwells=1, print_head=True, save_flows=True,
        packagedata=[[0, 0.15, botm[1], 6.0, "THIEM", 2]],
        connectiondata=connd,
        perioddata={0: [[0, "RATE", 0.0]]},  # non-pumping
        head_filerecord="maw.maw.hds",
    )
    flopy.mf6.ModflowGwfoc(gwf, head_filerecord="maw.hds", saverecord=[("HEAD", "ALL")])
    sim.write_simulation(silent=True)
    analytical = (k1 * b1 * h1 + k2 * b2 * h2) / (k1 * b1 + k2 * b2)
    params = {"T1": k1 * b1, "T2": k2 * b2, "h1": h1, "h2": h2}
    return sim, ws, analytical, params


#: Column counts the HFB grid-independence sweep solves the same domain at.
HFB_GRID_NCOLS: tuple[int, ...] = (10, 20, 40, 80)
_HFB_HYDCHR = 1.0e-6
_HFB_LENGTH_M = 1000.0
_HFB_K = 10.0
_HFB_DELC = 10.0
_HFB_THICK = 10.0
_HFB_DH = 9.0  # CHD 10 -> 1


def build_hfb_barrier(ws: str | None = None):
    """Author the barrier domain at several column counts (grid-independence)."""
    import flopy

    mf6 = resolve_mf6_binary()
    ws = ws or _new_ws("hfb")
    sims: dict[int, Any] = {}
    for ncol in HFB_GRID_NCOLS:
        nsub = ncol // 2
        sub = os.path.join(ws, f"n{ncol}")
        delr = _HFB_LENGTH_M / ncol
        sim = flopy.mf6.MFSimulation(sim_name=f"hfb{ncol}", sim_ws=sub, exe_name=mf6 or "mf6")
        flopy.mf6.ModflowTdis(sim, nper=1, perioddata=[(1.0, 1, 1.0)])
        gwf = flopy.mf6.ModflowGwf(sim, modelname=f"hfb{ncol}", save_flows=True)
        flopy.mf6.ModflowIms(sim, complexity="simple", outer_dvclose=1e-9,
                             inner_dvclose=1e-10)
        flopy.mf6.ModflowGwfdis(gwf, nlay=1, nrow=1, ncol=ncol, delr=delr,
                                delc=_HFB_DELC, top=_HFB_THICK, botm=0.0)
        flopy.mf6.ModflowGwfic(gwf, strt=10.0)
        flopy.mf6.ModflowGwfnpf(gwf, icelltype=0, k=_HFB_K, save_flows=True)
        flopy.mf6.ModflowGwfchd(gwf, stress_period_data=[
            [(0, 0, 0), 10.0], [(0, 0, ncol - 1), 1.0]])
        flopy.mf6.ModflowGwfhfb(gwf, stress_period_data=[
            [(0, 0, nsub - 1), (0, 0, nsub), _HFB_HYDCHR]])
        flopy.mf6.ModflowGwfoc(gwf, budget_filerecord=f"hfb{ncol}.cbc",
                               head_filerecord=f"hfb{ncol}.hds",
                               saverecord=[("HEAD", "ALL"), ("BUDGET", "ALL")])
        sim.write_simulation(silent=True)
        sims[ncol] = sim
    # analytical barrier-limited flux (barrier resistance >> aquifer): the near-
    # entire head drop is across the barrier, so Q ~= HYDCHR * area * dh.
    area = _HFB_THICK * _HFB_DELC
    analytical_q = _HFB_HYDCHR * area * _HFB_DH
    return sims, ws, analytical_q


# --------------------------------------------------------------------------- #
# Solve + extract + chart (each returns a SolvedValidation).
# --------------------------------------------------------------------------- #


def _run(sim) -> bool:
    ok, _buff = sim.run_simulation(silent=True)
    return bool(ok)


def _solve_newton_dry_rewet() -> SolvedValidation:
    sims, ws, botm = build_newton_dry_rewet()
    heads: dict[str, np.ndarray] = {}
    converged: dict[str, bool] = {}
    for name, sim in sims.items():
        converged[name] = _run(sim)
        try:
            heads[name] = sim.get_model(name).output.head().get_data().ravel()
        except Exception:  # noqa: BLE001 - a failed solve has no head file
            heads[name] = np.array([])

    nwt = heads.get("newton", np.array([]))
    std = heads.get("standard", np.array([]))
    ncol = botm.size

    def _dry_count(h: np.ndarray) -> int:
        if h.size == 0:
            return ncol
        return int(((h <= _HDRY_SENTINEL / 2) | (h < botm - 1e-6)).sum())

    nwt_dry = _dry_count(nwt)
    std_dry = _dry_count(std)
    nwt_monotone = bool(nwt.size and np.all(np.diff(nwt) <= 1e-6))
    nwt_min = float(nwt.min()) if nwt.size else None
    nwt_max = float(nwt.max()) if nwt.size else None
    # acceptance: Newton solved to a physical monotone staircase (0 dry) AND the
    # standard formulation collapsed >0 cells to dry (the robustness contrast).
    validated = bool(
        converged.get("newton")
        and nwt_dry == 0
        and nwt_monotone
        and nwt_min is not None and nwt_min >= 10.0 - 1e-6
        and nwt_max is not None and nwt_max <= 23.0 + 1e-6
        and std_dry > 0
    )

    x = (np.arange(ncol) + 0.5) * 5.0
    std_plot = np.where(std < botm, botm, std) if std.size == ncol else None
    values: list[dict[str, Any]] = []
    for xi, bi in zip(x, botm):
        values.append({"x": round(float(xi), 2), "y": round(float(bi), 4),
                       "series": "impervious base"})
    for xi, hi in zip(x, nwt):
        values.append({"x": round(float(xi), 2), "y": round(float(hi), 4),
                       "series": "Newton water table"})
    if std_plot is not None:
        for xi, hi in zip(x, std_plot):
            values.append({"x": round(float(xi), 2), "y": round(float(hi), 4),
                           "series": "standard (dry-collapsed)"})
    spec = {
        "data": {"values": values},
        "mark": {"type": "line", "point": False},
        "encoding": {
            "x": {"field": "x", "type": "quantitative", "title": "distance along channel (m)"},
            "y": {"field": "y", "type": "quantitative", "title": "elevation (m)"},
            "color": {"field": "series", "type": "nominal", "title": "profile"},
        },
        "title": "Zaidel staircase channel: Newton vs standard formulation",
    }
    caption = (
        f"Newton keeps all {ncol} cells wet in a monotone staircase "
        f"(dry cells: Newton={nwt_dry}); the standard formulation collapses "
        f"{std_dry} cells to dry (nonphysical, plotted at the base). "
        "modflow6-examples:ex-gwf-zaidel."
    )
    return SolvedValidation(
        case="newton_dry_rewet",
        computed_value=None, reference_value=None, delta=None, relative_error=None,
        validated=validated, tolerance=0.0,
        metrics={
            "newton_converged": converged.get("newton", False),
            "standard_converged": converged.get("standard", False),
            "newton_dry_cells": nwt_dry,
            "standard_dry_cells": std_dry,
            "newton_monotone_descending": nwt_monotone,
            "newton_head_min_m": nwt_min,
            "newton_head_max_m": nwt_max,
            "ncol": ncol,
        },
        chart_spec=spec,
        chart_title="Zaidel staircase channel: Newton vs standard formulation",
        chart_caption=caption,
    )


def _solve_maw_crossaquifer() -> SolvedValidation:
    sim, ws, analytical, params = build_maw_crossaquifer()
    ok = _run(sim)
    computed: float | None = None
    if ok:
        try:
            mh = sim.get_model("maw").maw.output.head().get_data()
            computed = float(np.array(mh).ravel()[-1])
        except Exception:  # noqa: BLE001
            computed = None
    delta = abs(computed - analytical) if computed is not None else None
    rel = (delta / abs(analytical)) if (delta is not None and analytical) else None
    validated = bool(computed is not None and rel is not None and rel < 1e-6)

    values = [
        {"x": f"upper aquifer (T={params['T1']:.0f})", "y": round(params["h1"], 4)},
        {"x": f"lower aquifer (T={params['T2']:.0f})", "y": round(params["h2"], 4)},
    ]
    rule = [{"y": round(analytical, 5),
             "label": "MAW well = Sokol T-weighted analytical",
             "strokeDash": [5, 4]}]
    spec = {
        "title": "Non-pumping MAW well equilibrium between two confined aquifers",
        "layer": [
            {
                "data": {"values": values},
                "mark": {"type": "bar"},
                "encoding": {
                    "x": {"field": "x", "type": "nominal", "title": "aquifer (driving head)"},
                    "y": {"field": "y", "type": "quantitative", "title": "head (m)"},
                },
            },
            {
                "data": {"values": rule},
                "mark": {"type": "rule"},
                "encoding": {"y": {"field": "y", "type": "quantitative"}},
            },
        ],
    }
    caption = (
        f"Computed MAW head {computed:.5f} m vs Sokol analytical {analytical:.5f} m "
        f"(delta {delta:.2e} m). "
        if computed is not None else
        f"Sokol analytical {analytical:.5f} m (solve unavailable). "
    ) + "modflow6-examples:ex-gwf-maw-p01."
    return SolvedValidation(
        case="maw_crossaquifer",
        computed_value=computed, reference_value=analytical, delta=delta,
        relative_error=rel, validated=validated, tolerance=1e-6,
        metrics={
            "transmissivity_upper_m2_d": params["T1"],
            "transmissivity_lower_m2_d": params["T2"],
            "aquifer_head_upper_m": params["h1"],
            "aquifer_head_lower_m": params["h2"],
            "converged": ok,
        },
        chart_spec=spec,
        chart_title="Non-pumping MAW well equilibrium between two confined aquifers",
        chart_caption=caption,
    )


def _solve_hfb_barrier() -> SolvedValidation:
    sims, ws, analytical_q = build_hfb_barrier()
    import flopy  # noqa: F401 - ensure flopy present for output readers

    flux_by_ncol: dict[int, float] = {}
    for ncol, sim in sims.items():
        ok = _run(sim)
        if not ok:
            continue
        try:
            bud = sim.get_model(f"hfb{ncol}").output.budget()
            chdrec = bud.get_data(text="CHD")[-1]
            q_in = float(sum(r["q"] for r in chdrec if r["q"] > 0))
            flux_by_ncol[ncol] = q_in
        except Exception:  # noqa: BLE001
            continue

    computed = None
    finest = max(flux_by_ncol) if flux_by_ncol else None
    if finest is not None:
        computed = flux_by_ncol[finest]
    delta = abs(computed - analytical_q) if computed is not None else None
    rel = (delta / abs(analytical_q)) if (delta is not None and analytical_q) else None
    # grid-independence: max relative spread of flux across the grids.
    grid_var = None
    if len(flux_by_ncol) >= 2:
        vals = list(flux_by_ncol.values())
        grid_var = float((max(vals) - min(vals)) / abs(np.mean(vals)))
    validated = bool(
        computed is not None and rel is not None and rel < 1e-2
        and grid_var is not None and grid_var < 1e-3
    )

    values = [{"x": ncol, "y": float(q), "series": "computed barrier flux"}
              for ncol, q in sorted(flux_by_ncol.items())]
    rule = [{"y": float(analytical_q), "label": "HYDCHR analytical flux",
             "strokeDash": [5, 4]}]
    spec = {
        "title": "HFB barrier flux is grid-refinement independent",
        "layer": [
            {
                "data": {"values": values},
                "mark": {"type": "line", "point": True},
                "encoding": {
                    "x": {"field": "x", "type": "quantitative", "title": "grid columns (finer ->)"},
                    "y": {"field": "y", "type": "quantitative", "title": "cross-barrier flux (m3/d)"},
                    "color": {"field": "series", "type": "nominal", "title": ""},
                },
            },
            {
                "data": {"values": rule},
                "mark": {"type": "rule"},
                "encoding": {"y": {"field": "y", "type": "quantitative"}},
            },
        ],
    }
    caption = (
        f"Barrier flux {computed:.4e} m3/d vs HYDCHR analytical {analytical_q:.4e} m3/d "
        f"(delta {delta:.2e}); flux varies < {grid_var:.1e} across grids "
        f"({min(flux_by_ncol)}..{max(flux_by_ncol)} cols) = grid-independent. "
        if (computed is not None and grid_var is not None) else
        f"HYDCHR analytical {analytical_q:.4e} m3/d (solve unavailable). "
    ) + "modflow6-docs:gwf-hfb."
    return SolvedValidation(
        case="hfb_barrier",
        computed_value=computed, reference_value=analytical_q, delta=delta,
        relative_error=rel, validated=validated, tolerance=1e-2,
        metrics={
            "flux_by_ncol_m3_d": {str(k): v for k, v in sorted(flux_by_ncol.items())},
            "max_relative_grid_variation": grid_var,
            "hydchr_per_day": _HFB_HYDCHR,
        },
        chart_spec=spec,
        chart_title="HFB barrier flux is grid-refinement independent",
        chart_caption=caption,
    )


# --------------------------------------------------------------------------- #
# PRT capture zone (native mf6 particle tracking) vs Grubb (1993) analytical.
#
# A confined single-layer aquifer with a regional gradient (CHD inflow west,
# RIV discharge east) and a pumping well at the centre has a closed-form
# capture-zone solution (Grubb, 1993): the down-gradient stagnation point sits at
# x_s = Q/(2*pi*U) and the up-gradient capture width asymptotes to W = Q/U, with
# U = K*b*i the regional volumetric flux per unit width. Native mf6 PRT
# (ModflowPrt + ModflowEms) tracks particles FORWARD from the inflow boundary
# (pathlines + travel times; the captured band width -> W) and BACKWARD from the
# well through the reversed flow field (the capture zone; the max down-gradient
# excursion -> x_s). Both are checked against Grubb, so - unlike the ADR 0153
# STOP, which had no numeric reference for the 3-layer ex-prt-mp7-p01 system -
# this case is validated NUMERICALLY, not just qualitatively.
# --------------------------------------------------------------------------- #

PRT_NLAY, PRT_NROW, PRT_NCOL = 1, 101, 121
PRT_DELR = PRT_DELC = 10.0            # m
PRT_TOP, PRT_BOTM = 20.0, 0.0        # confined thickness b = 20 m
PRT_K = 10.0                         # m/d
PRT_POROSITY = 0.20
PRT_HW, PRT_HE = 21.0, 20.0          # west inflow head / east discharge stage
PRT_Q = 60.0                         # well pumping (m3/d)
PRT_WELL_ROW = PRT_NROW // 2
PRT_WELL_COL = PRT_NCOL // 2
#: aquifer thickness, regional gradient, and Grubb flux-per-unit-width.
_PRT_B = PRT_TOP - PRT_BOTM
_PRT_LX = PRT_NCOL * PRT_DELR
_PRT_I = (PRT_HW - PRT_HE) / _PRT_LX
_PRT_U = PRT_K * _PRT_B * _PRT_I
#: Grubb analytical: stagnation distance and asymptotic capture width.
PRT_STAGNATION_M = PRT_Q / (2.0 * math.pi * _PRT_U)
PRT_CAPTURE_WIDTH_ASYMPTOTE_M = PRT_Q / _PRT_U


def _grubb_capture_halfwidth(distance_upgradient_m: float) -> float:
    """Grubb (1993) capture-zone half-width at a finite up-gradient distance.

    The dividing streamline satisfies ``x = y / tan(2*pi*U*y/Q)``; the
    up-gradient branch (y in ``(Q/4U, Q/2U)``) gives the half-width at distance
    ``L = -x``. Solved by bisection so the finite-domain width is compared
    (the asymptote W = Q/U is only reached as L -> infinity).
    """
    k = 2.0 * math.pi * _PRT_U / PRT_Q
    lo, hi = PRT_Q / (4.0 * _PRT_U) + 1e-9, PRT_Q / (2.0 * _PRT_U) - 1e-9

    def f(y: float) -> float:
        return y / math.tan(k * y) + distance_upgradient_m

    # f(lo) > 0, f(hi) < 0, monotone decreasing.
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if f(mid) > 0.0:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def _prt_xy_center(row: int, col: int) -> tuple[float, float]:
    return (col + 0.5) * PRT_DELR, (PRT_NROW - row - 0.5) * PRT_DELC


def build_prt_gwf(ws: str | None = None):
    """Author + write the confined well+river GWF flow deck (no run)."""
    import flopy

    mf6 = resolve_mf6_binary()
    ws = ws or _new_ws("prt")
    sub = os.path.join(ws, "gwf")
    sim = flopy.mf6.MFSimulation(sim_name="gwf", sim_ws=sub, exe_name=mf6 or "mf6")
    flopy.mf6.ModflowTdis(sim, time_units="days", nper=1, perioddata=[(1.0, 1, 1.0)])
    gwf = flopy.mf6.ModflowGwf(sim, modelname="gwf", save_flows=True)
    flopy.mf6.ModflowIms(sim, complexity="simple", outer_dvclose=1e-9, inner_dvclose=1e-10)
    flopy.mf6.ModflowGwfdis(gwf, nlay=PRT_NLAY, nrow=PRT_NROW, ncol=PRT_NCOL,
                            delr=PRT_DELR, delc=PRT_DELC, top=PRT_TOP, botm=PRT_BOTM,
                            length_units="meters")
    flopy.mf6.ModflowGwfic(gwf, strt=PRT_HW)
    flopy.mf6.ModflowGwfnpf(gwf, icelltype=0, k=PRT_K,
                            save_specific_discharge=True, save_saturation=True)
    flopy.mf6.ModflowGwfchd(gwf, stress_period_data=[
        [(0, r, 0), PRT_HW] for r in range(PRT_NROW)])
    # East = a RIV discharge boundary (high conductance -> fixed stage): the
    # "river" the regional flow drains to.
    flopy.mf6.ModflowGwfriv(gwf, stress_period_data=[
        [(0, r, PRT_NCOL - 1), PRT_HE, 1.0e6, PRT_BOTM + 0.1] for r in range(PRT_NROW)])
    flopy.mf6.ModflowGwfwel(gwf, stress_period_data=[[(0, PRT_WELL_ROW, PRT_WELL_COL), -PRT_Q]])
    flopy.mf6.ModflowGwfoc(gwf, head_filerecord="gwf.hds", budget_filerecord="gwf.cbc",
                           saverecord=[("HEAD", "ALL"), ("BUDGET", "ALL")])
    sim.write_simulation(silent=True)
    return sim, ws, sub


def _prt_release_forward(sub_per_cell: int = 3) -> list[tuple]:
    """Forward release: a dense line just inside the west inflow boundary."""
    pts: list[tuple] = []
    n = 0
    z = (PRT_TOP + PRT_BOTM) / 2.0
    for r in range(PRT_NROW):
        for s in range(sub_per_cell):
            x = (1 + 0.5) * PRT_DELR
            y = (PRT_NROW - r - (s + 0.5) / sub_per_cell) * PRT_DELC
            pts.append((n, (0, r, 1), x, y, z, f"f{n}"))
            n += 1
    return pts


def _prt_release_backward(n_particles: int) -> list[tuple]:
    """Backward release: a ring of particles around the well cell."""
    cx, cy = _prt_xy_center(PRT_WELL_ROW, PRT_WELL_COL)
    z = (PRT_TOP + PRT_BOTM) / 2.0
    pts: list[tuple] = []
    for n in range(n_particles):
        a = 2.0 * math.pi * n / n_particles
        pts.append((n, (0, PRT_WELL_ROW, PRT_WELL_COL),
                    cx + 3.0 * math.cos(a), cy + 3.0 * math.sin(a), z, f"b{n}"))
    return pts


def _run_prt(ws: str, gwf_dir: str, direction: str, releasepts: list[tuple]) -> dict[str, list]:
    """Build + run a PRT sim (reversing the GWF field for backward) -> tracks."""
    import flopy
    from flopy.utils import CellBudgetFile, HeadFile

    mf6 = resolve_mf6_binary()
    prt_ws = os.path.join(ws, direction)
    os.makedirs(prt_ws, exist_ok=True)
    hds = os.path.join(gwf_dir, "gwf.hds")
    cbc = os.path.join(gwf_dir, "gwf.cbc")
    if direction == "backward":
        gsim = flopy.mf6.MFSimulation.load(sim_ws=gwf_dir, exe_name=mf6 or "mf6")
        hrev, crev = hds + ".rev", cbc + ".rev"
        CellBudgetFile(cbc, tdis=gsim.tdis).reverse(crev)
        HeadFile(hds, tdis=gsim.tdis).reverse(hrev)
        hds, cbc = hrev, crev
    psim = flopy.mf6.MFSimulation(sim_name="prt", sim_ws=prt_ws, exe_name=mf6 or "mf6")
    flopy.mf6.ModflowTdis(psim, time_units="days", nper=1, perioddata=[(1.0, 1, 1.0)])
    prt = flopy.mf6.ModflowPrt(psim, modelname="prt")
    flopy.mf6.ModflowPrtdis(prt, nlay=PRT_NLAY, nrow=PRT_NROW, ncol=PRT_NCOL,
                            delr=PRT_DELR, delc=PRT_DELC, top=PRT_TOP, botm=PRT_BOTM,
                            length_units="meters")
    flopy.mf6.ModflowPrtmip(prt, porosity=PRT_POROSITY)
    flopy.mf6.ModflowPrtprp(prt, pname="prp", nreleasepts=len(releasepts),
                            packagedata=releasepts, boundnames=True,
                            perioddata={0: ["FIRST"]}, extend_tracking=True,
                            exit_solve_tolerance=1e-5)
    flopy.mf6.ModflowPrtoc(prt, trackcsv_filerecord="prt.trk.csv")
    flopy.mf6.ModflowPrtfmi(prt, packagedata=[
        ("GWFHEAD", os.path.abspath(hds)), ("GWFBUDGET", os.path.abspath(cbc))])
    ems = flopy.mf6.ModflowEms(psim)
    psim.register_solution_package(ems, [prt.name])
    psim.write_simulation(silent=True)
    ok, _buff = psim.run_simulation(silent=True)
    if not ok:
        raise ModflowValidationError(f"PRT {direction} solve did not converge")
    tracks: dict[str, list] = defaultdict(list)
    with open(os.path.join(prt_ws, "prt.trk.csv")) as fh:
        for row in csv.DictReader(fh):
            tracks[row["name"]].append(row)
    return dict(tracks)


def _solve_prt_capture_zone(direction: str = "backward", n_particles: int = 40) -> SolvedValidation:
    import flopy  # noqa: F401 - ensure flopy present

    if direction not in ("forward", "backward"):
        raise ModflowValidationError(
            f"prt direction must be 'forward' or 'backward'; got {direction!r}"
        )
    sim, ws, gwf_dir = build_prt_gwf()
    ok, _buff = sim.run_simulation(silent=True)
    if not ok:
        raise ModflowValidationError("PRT GWF flow solve did not converge")

    # FORWARD: capture band width at the west inflow line vs Grubb finite-distance.
    fwd = _run_prt(ws, gwf_dir, "forward", _prt_release_forward())
    n_fwd = len(fwd)
    captured_y: list[float] = []
    fwd_all_terminated = True
    travel_times: list[float] = []
    for name, tr in fwd.items():
        last = tr[-1]
        if str(last["istatus"]) not in ("5", "3", "7", "8", "9"):
            fwd_all_terminated = False
        travel_times.append(float(last["t"]))
        icell = int(float(last["icell"])) - 1
        rr, cc = icell // PRT_NCOL, icell % PRT_NCOL
        if (rr, cc) == (PRT_WELL_ROW, PRT_WELL_COL):
            captured_y.append(float(last["y"]))
    band_width = (len(captured_y) / n_fwd) * (PRT_NROW * PRT_DELC) if n_fwd else 0.0
    release_dist = (PRT_WELL_COL - 1) * PRT_DELR
    grubb_width = 2.0 * _grubb_capture_halfwidth(release_dist)
    width_rel = abs(band_width - grubb_width) / grubb_width if grubb_width else None

    # BACKWARD: capture zone; max down-gradient excursion -> stagnation distance.
    bwd = _run_prt(ws, gwf_dir, "backward", _prt_release_backward(n_particles))
    cx, _cy = _prt_xy_center(PRT_WELL_ROW, PRT_WELL_COL)
    stagnation = 0.0
    term_upgradient = 0
    for name, tr in bwd.items():
        stagnation = max(stagnation, max(0.0, max(float(p["x"]) for p in tr) - cx))
        icell = int(float(tr[-1]["icell"])) - 1
        if icell % PRT_NCOL <= 1:  # terminates at / adjacent to the west inflow
            term_upgradient += 1
    stag_rel = abs(stagnation - PRT_STAGNATION_M) / PRT_STAGNATION_M

    validated = bool(
        fwd_all_terminated
        and len(captured_y) > 0
        and width_rel is not None and width_rel < 0.20
        and stag_rel < 0.10
        and term_upgradient == len(bwd)
    )

    # Chart: computed/analytical ratio for the two Grubb metrics (target 1.0).
    values = [
        {"x": "stagnation distance", "y": round(stagnation / PRT_STAGNATION_M, 4)},
        {"x": "capture width", "y": round((band_width / grubb_width) if grubb_width else 0.0, 4)},
    ]
    rule = [{"y": 1.0, "label": "Grubb (1993) analytical", "strokeDash": [5, 4]}]
    spec = {
        "title": f"Native MF6 PRT capture zone vs Grubb analytical ({direction} shown)",
        "layer": [
            {
                "data": {"values": values},
                "mark": {"type": "bar"},
                "encoding": {
                    "x": {"field": "x", "type": "nominal", "title": "capture-zone metric"},
                    "y": {"field": "y", "type": "quantitative",
                          "title": "PRT computed / Grubb analytical"},
                },
            },
            {
                "data": {"values": rule},
                "mark": {"type": "rule"},
                "encoding": {"y": {"field": "y", "type": "quantitative"}},
            },
        ],
    }
    caption = (
        f"Backward stagnation {stagnation:.1f} m vs Grubb {PRT_STAGNATION_M:.1f} m "
        f"(rel {stag_rel:.1%}); forward capture band {band_width:.0f} m vs Grubb "
        f"finite-distance {grubb_width:.0f} m (rel {width_rel:.1%}); "
        f"{len(captured_y)} of {n_fwd} inflow particles captured, "
        f"{term_upgradient}/{len(bwd)} backward particles up-gradient. "
        "Grubb (1993); mf6-examples:ex-prt-mp7-p01."
    )
    return SolvedValidation(
        case="prt_capture_zone",
        computed_value=stagnation, reference_value=PRT_STAGNATION_M,
        delta=abs(stagnation - PRT_STAGNATION_M), relative_error=stag_rel,
        validated=validated, tolerance=0.10,
        metrics={
            "direction_shown": direction,
            "stagnation_distance_m": stagnation,
            "stagnation_grubb_m": PRT_STAGNATION_M,
            "stagnation_relative_error": stag_rel,
            "capture_width_m": band_width,
            "capture_width_grubb_finite_m": grubb_width,
            "capture_width_asymptote_m": PRT_CAPTURE_WIDTH_ASYMPTOTE_M,
            "capture_width_relative_error": width_rel,
            "forward_particles": n_fwd,
            "forward_captured": len(captured_y),
            "forward_all_terminated": fwd_all_terminated,
            "forward_travel_time_min_d": min(travel_times) if travel_times else None,
            "forward_travel_time_max_d": max(travel_times) if travel_times else None,
            "backward_particles": len(bwd),
            "backward_terminate_upgradient": term_upgradient,
            "regional_flux_U_m2_d": _PRT_U,
            "pumping_rate_m3_d": PRT_Q,
        },
        chart_spec=spec,
        chart_title=f"Native MF6 PRT capture zone vs Grubb analytical ({direction})",
        chart_caption=caption,
    )


# --------------------------------------------------------------------------- #
# Henry saltwater intrusion (BUY variable-density + GWT) vs the published wedge.
# --------------------------------------------------------------------------- #

HENRY_NLAY, HENRY_NROW, HENRY_NCOL = 40, 1, 80
HENRY_DELR, HENRY_DELC = 0.025, 1.0     # m ; Lx = 2.0 m, Lz = 1.0 m
HENRY_TOP = 1.0
HENRY_K = 864.0                         # m/d
HENRY_POROSITY = 0.35
HENRY_DIFFC = 0.57024                   # m2/d
HENRY_INFLOW_TOTAL = 5.7024             # m3/d freshwater inflow (inland)
HENRY_GHB_COND = 1728.0
HENRY_CSALT = 35.0                      # ppt seawater
HENRY_DENSEREF = 1000.0
HENRY_DRHODC = 0.7
#: representative published 0.5-isochlor toe penetration (m, from the sea
#: boundary at the bottom layer) for ex-gwt-henry-a - the pattern anchor.
HENRY_TOE_PENETRATION_REF_M = 0.79


def build_henry_saltwater(ws: str | None = None):
    """Author + write the Henry BUY+GWT variable-density deck (no run)."""
    import flopy

    mf6 = resolve_mf6_binary()
    ws = ws or _new_ws("henry")
    botm = [HENRY_TOP - (i + 1) * (HENRY_TOP / HENRY_NLAY) for i in range(HENRY_NLAY)]
    sim = flopy.mf6.MFSimulation(sim_name="henry", sim_ws=ws, exe_name=mf6 or "mf6")
    flopy.mf6.ModflowTdis(sim, time_units="days", nper=1, perioddata=[(0.5, 500, 1.0)])
    gwf = flopy.mf6.ModflowGwf(sim, modelname="flow", save_flows=True)
    flopy.mf6.ModflowGwfdis(gwf, nlay=HENRY_NLAY, nrow=HENRY_NROW, ncol=HENRY_NCOL,
                            delr=HENRY_DELR, delc=HENRY_DELC, top=HENRY_TOP, botm=botm)
    flopy.mf6.ModflowGwfic(gwf, strt=HENRY_TOP)
    flopy.mf6.ModflowGwfnpf(gwf, icelltype=0, k=HENRY_K, save_specific_discharge=True)
    flopy.mf6.ModflowGwfbuy(gwf, denseref=HENRY_DENSEREF, nrhospecies=1,
                            packagedata=[(0, HENRY_DRHODC, 0.0, "trans", "concentration")])
    flopy.mf6.ModflowGwfwel(
        gwf, auxiliary="CONCENTRATION", pname="WEL-1",
        stress_period_data=[[(k, 0, 0), HENRY_INFLOW_TOTAL / HENRY_NLAY, 0.0]
                            for k in range(HENRY_NLAY)])
    flopy.mf6.ModflowGwfghb(
        gwf, auxiliary="CONCENTRATION", pname="GHB-1",
        stress_period_data=[[(k, 0, HENRY_NCOL - 1), HENRY_TOP, HENRY_GHB_COND, HENRY_CSALT]
                            for k in range(HENRY_NLAY)])
    flopy.mf6.ModflowGwfoc(gwf, head_filerecord="flow.hds", budget_filerecord="flow.cbc",
                           saverecord=[("HEAD", "LAST"), ("BUDGET", "LAST")])
    gwt = flopy.mf6.ModflowGwt(sim, modelname="trans")
    flopy.mf6.ModflowGwtdis(gwt, nlay=HENRY_NLAY, nrow=HENRY_NROW, ncol=HENRY_NCOL,
                            delr=HENRY_DELR, delc=HENRY_DELC, top=HENRY_TOP, botm=botm)
    flopy.mf6.ModflowGwtic(gwt, strt=HENRY_CSALT)
    flopy.mf6.ModflowGwtadv(gwt, scheme="upstream")
    flopy.mf6.ModflowGwtdsp(gwt, xt3d_off=True, diffc=HENRY_DIFFC)
    flopy.mf6.ModflowGwtmst(gwt, porosity=HENRY_POROSITY)
    flopy.mf6.ModflowGwtssm(gwt, sources=[["GHB-1", "AUX", "CONCENTRATION"],
                                          ["WEL-1", "AUX", "CONCENTRATION"]])
    flopy.mf6.ModflowGwtoc(gwt, concentration_filerecord="trans.ucn",
                           saverecord=[("CONCENTRATION", "LAST")])
    flopy.mf6.ModflowGwfgwt(sim, exgtype="GWF6-GWT6", exgmnamea="flow", exgmnameb="trans")
    # Two IMS: GWF first (must precede GWT in mfsim.nam), GWT second.
    ims_flow = flopy.mf6.ModflowIms(sim, complexity="moderate", linear_acceleration="bicgstab",
                                    outer_dvclose=1e-6, inner_dvclose=1e-7, filename="flow.ims")
    sim.register_ims_package(ims_flow, ["flow"])
    ims_trans = flopy.mf6.ModflowIms(sim, complexity="moderate", linear_acceleration="bicgstab",
                                     outer_dvclose=1e-6, inner_dvclose=1e-7, filename="trans.ims")
    sim.register_ims_package(ims_trans, ["trans"])
    sim.write_simulation(silent=True)
    return sim, ws


def _solve_henry_saltwater() -> SolvedValidation:
    sim, ws = build_henry_saltwater()
    ok, _buff = sim.run_simulation(silent=True)
    if not ok:
        raise ModflowValidationError("Henry BUY+GWT solve did not converge")
    conc = sim.get_model("trans").output.concentration().get_data()  # (nlay, nrow, ncol)
    rel = np.asarray(conc)[:, 0, :] / HENRY_CSALT                     # (nlay, ncol)
    xc = (np.arange(HENRY_NCOL) + 0.5) * HENRY_DELR                   # inland x=0 -> sea x=Lx
    bottom = rel[-1, :]
    # 0.5 isochlor toe on the bottom layer: inland-most (smallest x) crossing.
    inland = np.where(bottom >= 0.5)[0]
    toe_x = float(xc[int(inland.min())]) if inland.size else float("nan")
    lx = HENRY_NCOL * HENRY_DELR
    toe_penetration = lx - toe_x if inland.size else float("nan")     # from the sea
    delta = abs(toe_penetration - HENRY_TOE_PENETRATION_REF_M)
    rel_err = delta / HENRY_TOE_PENETRATION_REF_M
    # pattern checks: a stable wedge (fresh inland-top, salt seaward-bottom), the
    # bottom salinity monotone increasing toward the sea, and an intermediate toe.
    monotone = bool(np.all(np.diff(bottom) >= -0.02))
    fresh_top_inland = float(rel[0, 0]) < 0.1
    salt_bottom_sea = float(rel[-1, -1]) > 0.9
    intermediate_toe = bool(inland.size and 0.15 * lx < toe_x < 0.85 * lx)
    validated = bool(monotone and fresh_top_inland and salt_bottom_sea
                     and intermediate_toe and rel_err < 0.30)

    # Chart: the 0.5-isochlor interface depth vs x (the canonical Henry plot).
    values: list[dict[str, Any]] = []
    depth_of = HENRY_TOP  # top elevation
    for j in range(HENRY_NCOL):
        col = rel[:, j]
        idx = np.where(col >= 0.5)[0]
        if idx.size:
            # shallowest layer reaching 0.5 -> interface elevation at this x.
            lay = int(idx.min())
            z_iface = HENRY_TOP - (lay + 0.5) * (HENRY_TOP / HENRY_NLAY)
            values.append({"x": round(float(xc[j]), 4), "y": round(float(z_iface), 4)})
    spec = {
        "data": {"values": values},
        "mark": {"type": "line", "point": True, "color": "#1f5fbf"},
        "encoding": {
            "x": {"field": "x", "type": "quantitative",
                  "title": "distance from inland boundary (m)  [sea at x=2.0]"},
            "y": {"field": "y", "type": "quantitative", "title": "0.5-isochlor elevation (m)"},
        },
        "title": "Henry saltwater wedge: 0.5-relative-salinity isochlor",
    }
    caption = (
        f"BUY+GWT Henry wedge: 0.5-isochlor toe penetrates {toe_penetration:.2f} m "
        f"inland from the sea (bottom layer), vs the published ~{HENRY_TOE_PENETRATION_REF_M:.2f} m "
        f"(rel {rel_err:.0%}); bottom salinity monotone toward the sea={monotone}. "
        "modflow6-examples:ex-gwt-henry-a."
    )
    return SolvedValidation(
        case="henry_saltwater",
        computed_value=toe_penetration, reference_value=HENRY_TOE_PENETRATION_REF_M,
        delta=delta, relative_error=rel_err, validated=validated, tolerance=0.30,
        metrics={
            "toe_penetration_from_sea_m": toe_penetration,
            "toe_x_from_inland_m": toe_x,
            "domain_length_m": lx,
            "bottom_salinity_monotone_to_sea": monotone,
            "fresh_top_inland": fresh_top_inland,
            "salt_bottom_seaward": salt_bottom_sea,
            "intermediate_toe": intermediate_toe,
            "seawater_salinity_ppt": HENRY_CSALT,
        },
        chart_spec=spec,
        chart_title="Henry saltwater wedge: 0.5-relative-salinity isochlor",
        chart_caption=caption,
    )


# --------------------------------------------------------------------------- #
# SFR stream depletion (streamflow routing + pumping well) vs Glover (1954).
#
# A confined single-layer aquifer with a well-connected SFR stream along the
# west edge and a well at distance ``a`` has a closed-form transient stream-
# depletion solution (Glover & Balmer, 1954): the fraction of the pumping rate
# captured from the stream is q(t)/Q = erfc(sqrt(a^2 S / (4 T t))). Stream
# depletion is defined by SUPERPOSITION - the difference in SFR->GWF leakage
# between a pumping and a no-pumping run - so the baseline stream stage / mounding
# cancels and only the well's capture remains (exactly what Glover predicts). A
# well-connected streambed (conductance >> aquifer transmissivity scale)
# reproduces the fully-penetrating Glover bound; the georeferenced stream_depletion
# composer uses realistic (lower) streambed K and correctly sits below this curve.
# --------------------------------------------------------------------------- #

GLOV_NLAY, GLOV_NROW, GLOV_NCOL = 1, 90, 120
GLOV_DELR = GLOV_DELC = 50.0          # m ; 6000 x 4500 m domain
GLOV_TOP, GLOV_BOTM = 20.0, 0.0       # confined thickness b = 20 m
GLOV_K = 10.0                         # m/d
GLOV_T = GLOV_K * (GLOV_TOP - GLOV_BOTM)   # transmissivity 200 m2/d
GLOV_S = 0.1                          # storage coefficient
GLOV_STRT = 19.0                      # uniform initial head = stream stage datum
GLOV_Q = 400.0                        # well pumping (m3/d)
GLOV_WELL_COL = 6                     # well 6 cells (300 m) east of the col-0 stream
GLOV_WELL_ROW = GLOV_NROW // 2
GLOV_INFLOW = 3000.0                  # SFR headwater inflow (>> Q so reaches stay wet)
#: well-to-stream distance (m), cell-centre to col-0 stream line.
GLOVER_A_M: float = GLOV_WELL_COL * GLOV_DELR
#: elapsed times after pump start (days) the depletion curve is sampled at.
GLOVER_TIMES_D: tuple[float, ...] = (2.0, 5.0, 10.0, 20.0, 40.0, 80.0, 160.0)


def glover_depletion_fraction(t_days: float, a_m: float = GLOVER_A_M) -> float:
    """Glover & Balmer (1954) transient stream-depletion fraction q(t)/Q.

    ``erfc(sqrt(a^2 S / (4 T t)))`` for a fully-penetrating stream a distance
    ``a_m`` from a well pumping since ``t=0`` in a confined aquifer (T, S). Pure.
    """
    if t_days <= 0.0:
        return 0.0
    return math.erfc(math.sqrt(a_m * a_m * GLOV_S / (4.0 * GLOV_T * t_days)))


def build_glover_sfr(pump: bool, ws: str | None = None):
    """Author + write one Glover SFR deck (pumping or no-pumping). No run.

    A confined transient GWF with an SFR stream along col 0 (well-connected
    streambed) and a WEL at ``(GLOV_WELL_ROW, GLOV_WELL_COL)`` running at
    ``GLOV_Q`` when ``pump`` else 0. Multi-period so the depletion is sampled at
    ``GLOVER_TIMES_D``; the SFR gwf-exchange is written to an OBS csv per reach.
    """
    import flopy

    mf6 = resolve_mf6_binary()
    ws = ws or _new_ws("glover")
    sub = os.path.join(ws, "pump" if pump else "nopump")
    name = "glov"
    perlen = [GLOVER_TIMES_D[0]] + [
        GLOVER_TIMES_D[i] - GLOVER_TIMES_D[i - 1] for i in range(1, len(GLOVER_TIMES_D))
    ]
    nper = len(perlen)
    sim = flopy.mf6.MFSimulation(sim_name=name, sim_ws=sub, exe_name=mf6 or "mf6")
    flopy.mf6.ModflowTdis(sim, time_units="days", nper=nper,
                          perioddata=[(pl, 12, 1.3) for pl in perlen])
    flopy.mf6.ModflowIms(sim, complexity="moderate", linear_acceleration="bicgstab",
                         outer_maximum=500, inner_maximum=500, under_relaxation="dbd",
                         outer_dvclose=1e-7, inner_dvclose=1e-8)
    gwf = flopy.mf6.ModflowGwf(sim, modelname=name, save_flows=True)
    flopy.mf6.ModflowGwfdis(gwf, nlay=GLOV_NLAY, nrow=GLOV_NROW, ncol=GLOV_NCOL,
                            delr=GLOV_DELR, delc=GLOV_DELC, top=GLOV_TOP, botm=GLOV_BOTM,
                            length_units="meters")
    flopy.mf6.ModflowGwfic(gwf, strt=GLOV_STRT)
    flopy.mf6.ModflowGwfnpf(gwf, icelltype=0, k=GLOV_K, save_flows=True)
    flopy.mf6.ModflowGwfsto(gwf, iconvert=0, ss=GLOV_S / (GLOV_TOP - GLOV_BOTM),
                            sy=GLOV_S, transient={i: True for i in range(nper)})
    nreach = GLOV_NROW
    pak, con = [], []
    for i in range(nreach):
        # (ifno, cellid, rlen, rwid, rgrd, rtp, rbth, rhk, man, ncon, ustrf, ndv)
        pak.append((i, (0, i, 0), GLOV_DELC, 45.0, 0.0005, GLOV_STRT, 1.0, 2.0, 0.03,
                    (1 if i in (0, nreach - 1) else 2), 1.0, 0))
        c = [i]
        if i > 0:
            c.append(i - 1)
        if i < nreach - 1:
            c.append(-(i + 1))
        con.append(c)
    sfr = flopy.mf6.ModflowGwfsfr(
        gwf, save_flows=True, budgetcsv_filerecord=f"{name}.sfr.bud.csv",
        nreaches=nreach, packagedata=pak, connectiondata=con,
        perioddata={0: [(0, "INFLOW", GLOV_INFLOW)]}, unit_conversion=86400.0)
    obs = {f"{name}.sfr.obs.csv": [(f"gwf_r{i + 1}", "sfr", i + 1) for i in range(nreach)]}
    sfr.obs.initialize(filename=f"{name}.sfr.obs", continuous=obs)
    q = GLOV_Q if pump else 0.0
    flopy.mf6.ModflowGwfwel(gwf, stress_period_data={
        p: [[(0, GLOV_WELL_ROW, GLOV_WELL_COL), -q]] for p in range(nper)})
    flopy.mf6.ModflowGwfoc(gwf, saverecord=[])
    sim.write_simulation(silent=True)
    return sim, sub, name


def _glover_leak_series(sub: str, name: str):
    """Read the per-reach SFR->GWF obs csv -> (times, total-leakage) arrays."""
    rows = list(csv.DictReader(open(os.path.join(sub, f"{name}.sfr.obs.csv"))))
    nreach = GLOV_NROW
    t = np.array([float(r["time"]) for r in rows])
    leak = np.array([sum(float(r[f"GWF_R{i + 1}"]) for i in range(nreach)) for r in rows])
    return t, leak


def _solve_sfr_stream_depletion() -> SolvedValidation:
    sim_p, sub_p, name = build_glover_sfr(pump=True)
    okp, _ = sim_p.run_simulation(silent=True)
    if not okp:
        raise ModflowValidationError("SFR stream-depletion pumping solve did not converge")
    sim_n, sub_n, _ = build_glover_sfr(pump=False)
    okn, _ = sim_n.run_simulation(silent=True)
    if not okn:
        raise ModflowValidationError("SFR stream-depletion baseline solve did not converge")

    tp, lp = _glover_leak_series(sub_p, name)
    tn, ln = _glover_leak_series(sub_n, name)

    fracs: list[float] = []
    glovers: list[float] = []
    rels: list[float] = []
    for t in GLOVER_TIMES_D:
        jp = int(np.argmin(np.abs(tp - t)))
        jn = int(np.argmin(np.abs(tn - t)))
        depl = float(lp[jp] - ln[jn])  # extra stream->aquifer transfer due to pumping
        frac = depl / GLOV_Q
        glov = glover_depletion_fraction(t)
        fracs.append(frac)
        glovers.append(glov)
        rels.append(abs(frac - glov) / glov if glov else float("nan"))

    # resolved window: times where the depletion fraction is non-trivial (>= 5%);
    # below that the discretization noise on a tiny signal dominates the relative
    # error and Glover's semi-infinite assumption is not yet informative.
    resolved = [i for i, g in enumerate(glovers) if g >= 0.05]
    max_rel = max((rels[i] for i in resolved), default=float("nan"))
    rms_rel = (
        float(math.sqrt(sum(rels[i] ** 2 for i in resolved) / len(resolved)))
        if resolved else float("nan")
    )
    monotone = all(fracs[i] <= fracs[i + 1] + 1e-3 for i in range(len(fracs) - 1))
    in_range = all(-0.02 <= f <= 1.02 for f in fracs)
    validated = bool(
        okp and okn and resolved and monotone and in_range and max_rel < 0.15
    )
    # headline on the latest sampled time (the best-resolved, most-informative point)
    computed = fracs[-1]
    reference = glovers[-1]

    values: list[dict[str, Any]] = []
    for t, f, g in zip(GLOVER_TIMES_D, fracs, glovers):
        values.append({"x": float(t), "y": round(float(f), 4), "series": "MF6 SFR (superposition)"})
        values.append({"x": float(t), "y": round(float(g), 4), "series": "Glover (1954) erfc"})
    spec = {
        "data": {"values": values},
        "mark": {"type": "line", "point": True},
        "encoding": {
            "x": {"field": "x", "type": "quantitative", "title": "time since pumping start (days)"},
            "y": {"field": "y", "type": "quantitative", "title": "stream-depletion fraction q(t)/Q"},
            "color": {"field": "series", "type": "nominal", "title": ""},
        },
        "title": "SFR stream depletion vs Glover (1954) analytical",
    }
    caption = (
        f"MF6 SFR-coupled well (a={GLOVER_A_M:.0f} m, Q={GLOV_Q:.0f} m3/d, T={GLOV_T:.0f} "
        f"m2/d, S={GLOV_S:g}) depletion fraction {computed:.3f} at {GLOVER_TIMES_D[-1]:.0f} d "
        f"vs Glover {reference:.3f} (delta {abs(computed - reference):.3f}); max relative "
        f"error {max_rel:.1%} over the resolved window. Glover & Balmer (1954)."
    )
    return SolvedValidation(
        case="sfr_stream_depletion",
        computed_value=computed, reference_value=reference,
        delta=abs(computed - reference), relative_error=rels[-1],
        validated=validated, tolerance=0.15,
        metrics={
            "well_to_stream_distance_m": GLOVER_A_M,
            "pumping_rate_m3_d": GLOV_Q,
            "transmissivity_m2_d": GLOV_T,
            "storage_coefficient": GLOV_S,
            "times_days": list(GLOVER_TIMES_D),
            "depletion_fraction_mf6": [round(f, 4) for f in fracs],
            "depletion_fraction_glover": [round(g, 4) for g in glovers],
            "relative_error_by_time": [round(r, 4) for r in rels],
            "max_relative_error_resolved": max_rel,
            "rms_relative_error_resolved": rms_rel,
            "monotone_increasing": monotone,
            "pump_converged": okp,
            "baseline_converged": okn,
        },
        chart_spec=spec,
        chart_title="SFR stream depletion vs Glover (1954) analytical",
        chart_caption=caption,
    )


# --------------------------------------------------------------------------- #
# MVR (Mover) routing: UZF rejected infiltration + DRN discharge -> SFR.
#
# The MVR package moves water between advanced packages. This case authors a
# small watershed cell block where a UZF column rejects the infiltration its
# vertical Ks cannot accept and a DRN discharges groundwater, and MVR routes BOTH
# into the head reach of an SFR network. The V&V is mover mass CONSERVATION: the
# volume SFR receives (FROM-MVR) equals the sum the providers give up (UZF
# rejected-infiltration + DRN discharge TO-MVR), to machine precision, in the same
# timestep. This answers the advanced-package-mover row: does MVR transfer rejected
# UZF + package discharge into SFR reaches within one coupled timestep?
# --------------------------------------------------------------------------- #

MVR_NROW, MVR_NCOL = 10, 12
MVR_DELR = MVR_DELC = 100.0
MVR_TOP, MVR_BOTM = 50.0, 0.0
MVR_K = 5.0
MVR_SFR_ROW = 5
MVR_NREACH = 8


def build_mvr_routing(ws: str | None = None):
    """Author + write the UZF+DRN -> SFR mover watershed deck. No run."""
    import flopy

    mf6 = resolve_mf6_binary()
    ws = ws or _new_ws("mvr")
    name = "mvr"
    sim = flopy.mf6.MFSimulation(sim_name=name, sim_ws=ws, exe_name=mf6 or "mf6")
    flopy.mf6.ModflowTdis(sim, time_units="days", nper=1, perioddata=[(10.0, 10, 1.2)])
    flopy.mf6.ModflowIms(sim, complexity="moderate", linear_acceleration="bicgstab",
                         outer_maximum=500, inner_maximum=500, under_relaxation="dbd",
                         outer_dvclose=1e-7, inner_dvclose=1e-8)
    gwf = flopy.mf6.ModflowGwf(sim, modelname=name, newtonoptions="NEWTON", save_flows=True)
    flopy.mf6.ModflowGwfdis(gwf, nlay=1, nrow=MVR_NROW, ncol=MVR_NCOL,
                            delr=MVR_DELR, delc=MVR_DELC, top=MVR_TOP, botm=MVR_BOTM,
                            length_units="meters")
    flopy.mf6.ModflowGwfic(gwf, strt=45.0)
    flopy.mf6.ModflowGwfnpf(gwf, icelltype=1, k=MVR_K, save_flows=True)
    flopy.mf6.ModflowGwfsto(gwf, iconvert=1, ss=1e-5, sy=0.2, transient={0: True})
    flopy.mf6.ModflowGwfchd(gwf, stress_period_data=[
        [(0, r, MVR_NCOL - 1), 40.0] for r in range(MVR_NROW)])
    # SFR receiver: 8 reaches along a row, mover-enabled.
    pak, con = [], []
    for i in range(MVR_NREACH):
        pak.append((i, (0, MVR_SFR_ROW, 1 + i), MVR_DELR, 5.0, 0.001, 44.0 - 0.1 * i,
                    1.0, 0.2, 0.035, (1 if i in (0, MVR_NREACH - 1) else 2), 1.0, 0))
        c = [i]
        if i > 0:
            c.append(i - 1)
        if i < MVR_NREACH - 1:
            c.append(-(i + 1))
        con.append(c)
    flopy.mf6.ModflowGwfsfr(
        gwf, save_flows=True, mover=True, pname="SFR-1",
        budgetcsv_filerecord=f"{name}.sfr.bud.csv",
        nreaches=MVR_NREACH, packagedata=pak, connectiondata=con,
        perioddata={0: [(0, "INFLOW", 1000.0)]}, unit_conversion=86400.0)
    # DRN provider: groundwater discharge on upland cells (elev below the head).
    flopy.mf6.ModflowGwfdrn(gwf, save_flows=True, mover=True, pname="DRN-1",
                            stress_period_data=[[(0, r, 2), 43.0, 500.0] for r in range(2, 5)])
    # UZF provider: infiltration (2 m/d) exceeding vertical Ks (0.5 m/d) -> rejected.
    uzf_cells = [(0, r, 3) for r in range(2, 5)]
    uzf_pack = [(n, cid, 1, -1, 0.1, 0.5, 0.05, 0.30, 0.08, 4.0)
                for n, cid in enumerate(uzf_cells)]
    uzf_spd = {0: [(n, 2.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0) for n in range(len(uzf_cells))]}
    flopy.mf6.ModflowGwfuzf(
        gwf, save_flows=True, mover=True, pname="UZF-1",
        budget_filerecord=f"{name}.uzf.bud", simulate_et=False, linear_gwet=False,
        nuzfcells=len(uzf_cells), ntrailwaves=7, nwavesets=40,
        packagedata=uzf_pack, perioddata=uzf_spd)
    # MVR: route DRN + UZF (rejected) discharge into SFR reach 0.
    packages = [("DRN-1",), ("UZF-1",), ("SFR-1",)]
    perioddata = [(prov, pid, "SFR-1", 0, "FACTOR", 1.0)
                  for prov in ("DRN-1", "UZF-1") for pid in range(3)]
    flopy.mf6.ModflowGwfmvr(gwf, print_flows=True, maxmvr=len(perioddata),
                            maxpackages=len(packages), packages=packages,
                            perioddata={0: perioddata})
    flopy.mf6.ModflowGwfoc(gwf, budget_filerecord=f"{name}.cbc",
                           saverecord=[("BUDGET", "ALL")])
    sim.write_simulation(silent=True)
    return sim, ws, name


def _solve_mvr_routing() -> SolvedValidation:
    import flopy

    sim, ws, name = build_mvr_routing()
    ok, _ = sim.run_simulation(silent=True)
    if not ok:
        raise ModflowValidationError("MVR routing solve did not converge")

    gwf_bud = flopy.utils.CellBudgetFile(os.path.join(ws, f"{name}.cbc"), precision="double")
    kk = gwf_bud.get_kstpkper()[-1]
    drn_mvr = abs(float(np.sum(gwf_bud.get_data(text="DRN-TO-MVR", kstpkper=kk)[0]["q"])))
    uzf_bud = flopy.utils.CellBudgetFile(os.path.join(ws, f"{name}.uzf.bud"), precision="double")
    uzf_mvr = 0.0
    for nm in uzf_bud.get_unique_record_names():
        txt = nm.decode().strip() if isinstance(nm, bytes) else nm.strip()
        if "MVR" in txt and "REJ" in txt:
            uzf_mvr = abs(float(np.sum(uzf_bud.get_data(text=txt, kstpkper=kk)[0]["q"])))
    rows = list(csv.DictReader(open(os.path.join(ws, f"{name}.sfr.bud.csv"))))
    sfr_from_mvr = float(rows[-1]["FROM-MVR_IN"])

    providers = drn_mvr + uzf_mvr
    delta = abs(providers - sfr_from_mvr)
    rel = delta / sfr_from_mvr if sfr_from_mvr else None
    validated = bool(
        rel is not None and rel < 1e-6 and uzf_mvr > 0.0 and drn_mvr > 0.0
    )

    values = [
        {"x": "UZF rejected", "y": round(uzf_mvr, 3), "series": "provider TO-MVR"},
        {"x": "DRN discharge", "y": round(drn_mvr, 3), "series": "provider TO-MVR"},
        {"x": "SFR received", "y": round(sfr_from_mvr, 3), "series": "receiver FROM-MVR"},
    ]
    rule = [{"y": round(providers, 3), "label": "providers total (UZF + DRN)",
             "strokeDash": [5, 4]}]
    spec = {
        "title": "MVR mass conservation: UZF rejected + DRN discharge routed to SFR",
        "layer": [
            {
                "data": {"values": values},
                "mark": {"type": "bar"},
                "encoding": {
                    "x": {"field": "x", "type": "nominal", "title": "mover flow"},
                    "y": {"field": "y", "type": "quantitative", "title": "routed rate (m3/d)"},
                    "color": {"field": "series", "type": "nominal", "title": ""},
                },
            },
            {
                "data": {"values": rule},
                "mark": {"type": "rule"},
                "encoding": {"y": {"field": "y", "type": "quantitative"}},
            },
        ],
    }
    caption = (
        f"MVR routes UZF rejected infiltration {uzf_mvr:.1f} + DRN discharge "
        f"{drn_mvr:.1f} = {providers:.1f} m3/d into SFR, which receives "
        f"{sfr_from_mvr:.1f} m3/d (conservation delta {delta:.2e} m3/d, "
        f"rel {rel:.1e}). modflow6-docs:gwf-mvr."
    )
    return SolvedValidation(
        case="mvr_routing",
        computed_value=sfr_from_mvr, reference_value=providers,
        delta=delta, relative_error=rel, validated=validated, tolerance=1e-6,
        metrics={
            "uzf_rejected_to_mvr_m3_d": uzf_mvr,
            "drn_discharge_to_mvr_m3_d": drn_mvr,
            "providers_total_m3_d": providers,
            "sfr_received_from_mvr_m3_d": sfr_from_mvr,
            "conservation_delta_m3_d": delta,
            "conservation_relative_error": rel,
            "converged": ok,
        },
        chart_spec=spec,
        chart_title="MVR mass conservation: UZF rejected + DRN discharge routed to SFR",
        chart_caption=caption,
    )


_SOLVERS = {
    "newton_dry_rewet": _solve_newton_dry_rewet,
    "maw_crossaquifer": _solve_maw_crossaquifer,
    "hfb_barrier": _solve_hfb_barrier,
    "henry_saltwater": _solve_henry_saltwater,
    "sfr_stream_depletion": _solve_sfr_stream_depletion,
    "mvr_routing": _solve_mvr_routing,
}


def run_validation_case(
    case: str, *, direction: str = "backward", n_particles: int = 40
) -> SolvedValidation:
    """Author + solve one validation case, returning the extracted V&V result.

    ``direction`` / ``n_particles`` apply ONLY to ``prt_capture_zone`` (the PRT
    tracking direction shown and the backward release-ring size); the other
    cases ignore them. Raises ``ModflowValidationError`` when ``case`` is unknown
    or no mf6 binary is available (a solve is required - the product IS the mf6
    comparison).
    """
    known = set(_SOLVERS) | {"prt_capture_zone"}
    if case not in known:
        raise ModflowValidationError(
            f"unknown validation case {case!r}; expected one of {sorted(known)}"
        )
    if resolve_mf6_binary() is None:
        err = ModflowValidationError(
            "no mf6 binary available (set $TRID3NT_MF6_BIN or install mf6 on PATH)"
        )
        err.error_code = "MODFLOW_MF6_BIN_MISSING"
        raise err
    logger.info("modflow validation case=%s solving", case)
    if case == "prt_capture_zone":
        result = _solve_prt_capture_zone(direction=direction, n_particles=n_particles)
    else:
        result = _SOLVERS[case]()
    logger.info(
        "modflow validation case=%s validated=%s computed=%s reference=%s delta=%s",
        case, result.validated, result.computed_value, result.reference_value,
        result.delta,
    )
    return result
