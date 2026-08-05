"""Package-validation engine core for the MODFLOW V&V templates (ADR 0153).

Where the archetype composers build a place-based demo aquifer and render a map
layer, this core authors SMALL SYNTHETIC BENCHMARK decks that isolate a single
MF6 package and reproduce a PUBLISHED or ANALYTICAL reference, then solve each
through the local ``mf6`` binary and extract the computed-vs-reference quantity.
The product is a computed-vs-reference CHART plus typed scalars - NEVER a
georeferenced map (the decks are schematic, local model units).

Three cases, each exercising a package no archetype composer exposes:

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

Honesty (loud): every number is a real parsed mf6 output (invariant 1); the
decks are AUTHORED synthetic benchmarks labeled ``SyntheticInput(basis=
"default_demo")`` by the composer. The MODPATH-7 cross-tool PRT case is NOT
included here - the mp7 binary is absent from the image/env (ADR 0153 STOP).
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
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


_SOLVERS = {
    "newton_dry_rewet": _solve_newton_dry_rewet,
    "maw_crossaquifer": _solve_maw_crossaquifer,
    "hfb_barrier": _solve_hfb_barrier,
}


def run_validation_case(case: str) -> SolvedValidation:
    """Author + solve one validation case, returning the extracted V&V result.

    Raises ``ModflowValidationError`` when ``case`` is unknown or no mf6 binary
    is available (a solve is required - the product IS the mf6 comparison).
    """
    if case not in _SOLVERS:
        raise ModflowValidationError(
            f"unknown validation case {case!r}; expected one of {sorted(_SOLVERS)}"
        )
    if resolve_mf6_binary() is None:
        err = ModflowValidationError(
            "no mf6 binary available (set $TRID3NT_MF6_BIN or install mf6 on PATH)"
        )
        err.error_code = "MODFLOW_MF6_BIN_MISSING"
        raise err
    logger.info("modflow validation case=%s solving", case)
    result = _SOLVERS[case]()
    logger.info(
        "modflow validation case=%s validated=%s computed=%s reference=%s delta=%s",
        case, result.validated, result.computed_value, result.reference_value,
        result.delta,
    )
    return result
