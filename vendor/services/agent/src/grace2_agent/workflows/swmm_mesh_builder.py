"""DEM -> quasi-2D node-link SWMM mesh builder (sprint-16 P2, PySWMM urban-flood
engine, Path A — confirmed by NATE's PCSWMM screenshot: animated depth around
BUILDING OBSTRUCTIONS + a SOUND BARRIER with RED walls / GREEN flap gates).

This module turns an AOI DEM (+ building footprints + tagged barrier lines) into
a runnable quasi-2D SWMM ``.inp`` deck and runs it headless via pyswmm. It is the
engine core: the SFINCS analogue is ``sfincs_builder.py`` (whose adaptive-mesh
budget math is lifted + RE-FIT here for SWMM), and the contract/tool template is
``run_modflow.py`` + ``modflow_contracts.py``. The forcing comes from the P1
nested-hyetograph builder (``swmm_hyetograph.build_nested_hyetograph``); the run
args / output layer shapes are the P1 ``swmm_contracts`` (``SWMMRunArgs`` /
``SWMMDepthLayerURI``).

Quasi-2D representation (PROVEN by the P0 GO/NO-GO spike,
``services/workers/swmm/spike_quasi2d.py`` — every swmm-api signature here is
reused from it):

- One STORAGE node per ACTIVE cell. Invert = the resampled DEM elevation; a
  FUNCTIONAL storage curve ``data=[A1=0, A2=0, A0=cell_area]`` makes the surface
  area the constant cell footprint (so volume = depth * cell_area).
- 4-connectivity, de-duplicated overland RECT_OPEN CONDUITS between neighbouring
  active cells. ``length = resolution``, ``roughness`` = NLCD Manning n from
  ``manning_mapping.csv`` (``load_manning_mapping``; the SFINCS substrate table).
- One per-cell SUBCATCHMENT drains the design-storm rainfall onto its own node,
  fed by a single RAINGAGE + the nested-hyetograph TIMESERIES.
- Exactly ONE dedicated boundary OUTFALL fed by a SINGLE conduit from the lowest
  active boundary cell. P0 carry-forward: a SWMM outfall takes EXACTLY ONE inlet
  link (ERROR 141/145 otherwise), so we never make a cell itself the outfall.

Building obstruction (``building_representation`` PARAM — never hardcoded):
- ``"drop"``      (DEFAULT, matches the screenshot): building cells get NO node
  and NO link — a hole in the mesh; water routes AROUND the obstruction.
- ``"raise"``     building cells stay but their invert is lifted ``+raise_m`` so
  they dam flow (a solid pad).
- ``"roughness"`` building cells stay but their incident overland conduits get a
  bumped Manning n (a soft obstruction).

Barriers (tagged-LineString FeatureCollection snapped to cell-pair edges):
- RED ``wall``      = OMIT the overland conduit between the two cells (a hard
  dam). P0-proven: omitting the conduit ponds water upstream.
- GREEN ``flap_gate`` = an ORIFICE with ``has_flap_gate=True`` (the ONLY element
  that takes a flap-gate kwarg in swmm-api 0.4.73 — a Conduit does NOT). The
  orifice is oriented from the PROTECTED side to the wet side so SWMM's flap
  blocks reverse flow into the protected area. P0-proven one-way: a flap orifice
  passed 0.000 CMS on the reverse gradient vs 18.191 for a plain conduit.

Infiltration (``infiltration_method`` PARAM on the PERVIOUS fraction):
- ``"none"``       fully impervious (the spike default; all rain runs off).
- ``"scs_cn"``     SCS Curve-Number loss (a curve number drives the SubArea
  pervious fraction + a HORTON-equivalent; for v0.1 we encode CN via the SWMM
  CURVE_NUMBER infiltration option on the pervious area).
- ``"green_ampt"`` Green-Ampt loss (suction / conductivity / initial deficit on
  the pervious area).

Adaptive-mesh budget (lifted from ``sfincs_builder`` + RE-FIT for SWMM). The
model was originally anchored to the P0 SYNTHETIC spike (400 cells / dt=2 s /
6 h => ~19 s single-thread), but the FIRST LIVE urban run exposed that anchor
as ~16x optimistic: 1190 active cells took 983 s (16.4 min) wall, where the
old fit predicted only ~63 s, so the autoscaler UNDER-coarsened. The model is
now RE-FIT to the LIVE anchor (1190 cells -> 983 s); the synthetic spike is a
different (easy-convergence) regime and is no longer trusted for sizing.
DYNWAVE overland is O(cells * steps), so a large AOI is coarsened up
``SWMM_RES_LADDER = (1, 2, 5, 10, 20) m`` until the estimated active-cell
count fits a wall-clock budget. Never produces a degenerate/empty grid.

Mass-balance honesty gate (cross-check improvement): after the run we read the
``.rpt`` **Flow Routing Continuity** error; if it exceeds the tolerance we raise
a typed ``SWMMMeshError("SWMM_MASS_BALANCE_EXCEEDED")`` instead of publishing a
silently-wrong depth layer (the data-source-fallback / honesty-floor norm).

Determinism (invariant 1/2): no LLM in the path; given the same inputs the deck
is reproducible. pyswmm + swmm-api are **lazy-imported** inside the functions
that need them so the agent service still imports this module when SWMM is
absent (only the urban-flood worker path triggers a real build/run).
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .swmm_hyetograph import HyetographResult, build_nested_hyetograph

try:  # numpy/rasterio are agent-venv deps (SFINCS chain) — but stay defensive.
    import numpy as np
except Exception:  # pragma: no cover - numpy is a hard dep in practice
    np = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

__all__ = [
    "SWMMMeshError",
    "BuildResult",
    "build_swmm_mesh",
    "run_swmm_deck",
    "read_flow_routing_continuity",
    "read_quality_routing_continuity",
    # adaptive budget (RE-FIT from the P0 anchor)
    "SWMM_RES_LADDER",
    "SWMM_SOLVE_BUDGET_S",
    "SWMM_PERF_A",
    "SWMM_PERF_P",
    "estimate_swmm_solve_seconds",
    "compute_swmm_cell_cap",
    "autoscale_swmm_resolution",
    "suggest_swmm_resolution",
    "SWMMAutoscaleResult",
    "clamp_swmm_resolution_to_real_cap",
    "SWMMRealCapClampResult",
    # Manning loader re-export (the SFINCS substrate table)
    "load_manning_mapping",
]

# Re-use the SFINCS substrate Manning loader (version-pinned NLCD -> n table).
from .sfincs_builder import load_manning_mapping  # noqa: E402

# Default overland Manning n when no NLCD raster is supplied (matches the spike
# / the contracts default). Used for the synthetic-AOI proof and as a fallback.
DEFAULT_OVERLAND_N: float = 0.03

# A tall RECT_OPEN overland conduit so it never surcharges shut (spike value).
_COND_HEIGHT_M: float = 3.0
# Storage max depth (m) — generous so a cell never caps and loses mass (spike).
_DEPTH_MAX_M: float = 5.0
# NLCD "Developed, High Intensity" — used to bump roughness for the
# ``building_representation="roughness"`` mode when no class raster is present.
_BUILDING_ROUGHNESS_N: float = 0.20


# --------------------------------------------------------------------------- #
# Typed error (mirrors SFINCSSetupError / MODFLOWWorkflowError shape)
# --------------------------------------------------------------------------- #
class SWMMMeshError(RuntimeError):
    """Raised by the SWMM mesh builder / runner on any typed failure.

    ``error_code`` is the A.6 open-set code surfaced to the WS error frame and
    threaded into the final envelope. Codes used by this module:

    - ``SWMM_MASS_BALANCE_EXCEEDED`` — the **headline** honesty gate: the run's
      Flow Routing Continuity error exceeded ``mass_balance_tolerance_pct``;
      ``details`` carries ``{continuity_error_pct, tolerance_pct, rpt_path}``.
    - ``SWMM_EMPTY_MESH`` — the DEM produced zero active cells (all nodata, or
      every cell dropped as a building) — nothing to solve.
    - ``SWMM_DEM_UNREADABLE`` — the DEM bytes could not be read.
    - ``SWMM_DEPENDENCY_MISSING`` — pyswmm / swmm-api not importable in the
      runtime (lazy import failed); surfaces as an honest typed error.
    - ``SWMM_RUN_FAILED`` — pyswmm raised during the headless solve.
    - ``SWMM_CONTINUITY_UNREADABLE`` — the .rpt produced no Flow Routing
      Continuity error line (the run did not complete as expected).
    """

    def __init__(
        self,
        error_code: str,
        *,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message or error_code)
        self.error_code = error_code
        self.details: dict[str, Any] = dict(details or {})


# --------------------------------------------------------------------------- #
# Adaptive-mesh budget — lifted from sfincs_builder + RE-FIT for SWMM.
#
# RE-FIT (BREAK D): the model is now anchored to the FIRST LIVE urban run, NOT
# the synthetic P0 spike. The spike (400 ACTIVE cells -> 19.022 s) turned out to
# be ~16x optimistic relative to a real urban DEM: the live run logged 1190
# ACTIVE cells taking 983 s (16.4 min) single-thread, where the old fit
# (A=2.604e-2, p=1.10) predicted only ~62.9 s. So the autoscaler UNDER-coarsened
# (it thought fine resolution fit the budget when it did not). We re-fit so the
# model REPRODUCES the live anchor and slightly OVER-estimates everywhere else.
#
# DYNWAVE overland cost scales ~ cells * steps; with a fixed dt+duration the
# step count is fixed, so wall time is ~LINEAR in the active-cell count at fixed
# routing-step. We KEEP the near-linear exponent p=1.10 (p slightly > 1 to stay
# conservative against super-linear trial/Jacobian growth in DYNWAVE as the
# network widens - a HIGHER p coarsens MORE, the safe direction) and re-pin A
# from the LIVE anchor (1190 cells -> 983 s). This is the env-overridable retune
# the original comments anticipated as real (cells, time) telemetry landed.
#
# Every coefficient is an env-overridable module constant so the cap re-tunes
# from logged solve-telemetry as MORE real (cells, time) records land. We NEVER
# produce a degenerate/empty grid — resolution is clamped to the coarsest rung
# and the cap floored.
# --------------------------------------------------------------------------- #
def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning("swmm autoscale: env %s=%r not a float; using %s", name, raw, default)
        return default


def _env_resolution_ladder(default: tuple[float, ...]) -> tuple[float, ...]:
    raw = os.environ.get("GRACE2_SWMM_RES_LADDER")
    if raw is None or not raw.strip():
        return default
    try:
        vals = sorted(
            {float(p.strip()) for p in raw.split(",") if p.strip() and float(p.strip()) > 0}
        )
    except (TypeError, ValueError):
        logger.warning(
            "swmm autoscale: env GRACE2_SWMM_RES_LADDER=%r unparseable; using %s", raw, default
        )
        return default
    return tuple(vals) if vals else default


#: Wall-clock budget (s) we size the active-cell cap against. SWMM single-thread
#: is the current path; keep a tight default so a big urban AOI coarsens rather
#: than wedging the always-on box. Env: ``GRACE2_SWMM_SOLVE_BUDGET_S``.
SWMM_SOLVE_BUDGET_S: float = _env_float("GRACE2_SWMM_SOLVE_BUDGET_S", 300.0)

#: Fraction of the budget reserved for non-solve overhead (DEM fetch, deck
#: write, postprocess). Env: ``GRACE2_SWMM_OVERHEAD_FRACTION``.
SWMM_OVERHEAD_FRACTION: float = _env_float("GRACE2_SWMM_OVERHEAD_FRACTION", 0.35)

#: Perf model T(N) = PERF_A * N^PERF_P (single thread). RE-FIT (BREAK D) from the
#: LIVE urban anchor (983 s @ 1190 cells), NOT the optimistic synthetic spike.
#: p kept at 1.10 (slightly > 1 to stay conservative against DYNWAVE's
#: super-linear trial growth as the network widens - a HIGHER p coarsens MORE,
#: the safe direction). A solved anchor-exact given p:
#:     983 = A * 1190^1.10  ->  1190^1.10 = exp(1.10*ln1190)=2416.03
#:     A = 983 / 2416.03 = 0.40687
#: This re-fit predicts T(1190)=983 s (exact) and T(400)~=296 s, vs the old
#: A=2.604e-2 which predicted only ~62.9 s at 1190 cells (~16x optimistic).
#: Env: ``GRACE2_SWMM_PERF_P`` / ``GRACE2_SWMM_PERF_A``.
SWMM_PERF_P: float = _env_float("GRACE2_SWMM_PERF_P", 1.10)
SWMM_PERF_A: float = _env_float("GRACE2_SWMM_PERF_A", 0.40687)

#: Resolution ladder (m) the autoscaler snaps UP through. 1 m is the finest
#: urban rung (building-scale); 20 m the coarsest we will solve at (still
#: meaningful for a wide AOI, the floor against degenerate over-coarsening).
#: Env: ``GRACE2_SWMM_RES_LADDER`` (comma-separated).
SWMM_RES_LADDER: tuple[float, ...] = _env_resolution_ladder((1.0, 2.0, 5.0, 10.0, 20.0))

#: Hard floor on the active-cell cap so a hostile budget/perf override can never
#: drive the cap to a degenerate near-zero value. After the BREAK-D re-fit, at
#: the default 300 s / 0.35 overhead / p=1.10 / A=0.40687 the natural cap is
#: ~273 cells (was ~4.6k under the optimistic spike fit), so this 200-cell floor
#: is now close to the operating point but still not binding at default budget.
SWMM_MIN_CELL_CAP: int = int(_env_float("GRACE2_SWMM_MIN_CELL_CAP", 200))


def estimate_swmm_solve_seconds(active_cells: int) -> float:
    """Estimate single-thread SWMM wall-clock seconds for ``active_cells``.

    ``T(N) = SWMM_PERF_A * N^SWMM_PERF_P``, RE-FIT (BREAK D) from the LIVE urban
    anchor (983 s @ 1190 cells). Returns 0.0 for a non-positive count.
    """
    if active_cells <= 0:
        return 0.0
    return SWMM_PERF_A * (float(active_cells) ** SWMM_PERF_P)


def compute_swmm_cell_cap() -> int:
    """Max active-cell count that solves inside the budget (single thread).

    Invert the perf model at the budget net of overhead::

        N_cap = ( (1 - overhead) * budget / PERF_A ) ** (1 / PERF_P)

    Floored at ``SWMM_MIN_CELL_CAP`` so a hostile env override can't degenerate
    the cap.
    """
    budget = max(0.0, SWMM_SOLVE_BUDGET_S)
    overhead = min(max(SWMM_OVERHEAD_FRACTION, 0.0), 0.95)
    solve_budget = budget * (1.0 - overhead)
    if SWMM_PERF_A <= 0 or solve_budget <= 0:
        cap = SWMM_MIN_CELL_CAP
    else:
        cap = int((solve_budget / SWMM_PERF_A) ** (1.0 / SWMM_PERF_P))
    cap = max(cap, SWMM_MIN_CELL_CAP)
    logger.info(
        "swmm autoscale: cell cap=%d (budget=%.0fs overhead=%.2f p=%.3f a=%.3e)",
        cap, budget, overhead, SWMM_PERF_P, SWMM_PERF_A,
    )
    return cap


@dataclass(frozen=True)
class SWMMAutoscaleResult:
    """Outcome of ``autoscale_swmm_resolution`` — the chosen resolution + why."""

    resolution_m: float
    estimated_active_cells: int
    cell_cap: int
    base_resolution_m: float
    estimated_active_cells_at_base: int
    estimated_solve_seconds: float
    coarsened: bool
    reason: str


def _estimate_active_cells_at_resolution(
    active_base: int, base_res_m: float, target_res_m: float
) -> int:
    """Scale an active-cell count from ``base_res_m`` to ``target_res_m``.

    Active AREA is invariant; the COUNT scales by ``(base/target)**2``.
    Coarsening (target > base) shrinks the count. Floored at 1 for a non-empty
    domain so the estimate never advertises a free solve.
    """
    if active_base <= 0 or base_res_m <= 0 or target_res_m <= 0:
        return 0
    scaled = active_base * (base_res_m / target_res_m) ** 2
    return max(1, int(round(scaled)))


def autoscale_swmm_resolution(
    active_cells_at_base: int,
    *,
    base_resolution_m: float,
) -> SWMMAutoscaleResult:
    """Choose the overland cell size so the estimated solve fits the budget.

    Walks ``SWMM_RES_LADDER`` from the finest rung >= ``base_resolution_m``
    upward, snapping UP until the estimated active-cell count is at or under
    ``compute_swmm_cell_cap()``. NEVER degenerate: the walk stops at the coarsest
    rung even if the cap is still exceeded (a huge AOI solves coarse but
    non-empty), and the estimate is floored at 1 for a real domain.

    ``active_cells_at_base`` is the active-cell count counted from the staged DEM
    at ``base_resolution_m`` (the requested resolution).
    """
    cap = compute_swmm_cell_cap()
    ladder = sorted({base_resolution_m, *SWMM_RES_LADDER})
    ladder = [r for r in ladder if r >= base_resolution_m] or [base_resolution_m]

    est_at_base = _estimate_active_cells_at_resolution(
        active_cells_at_base, base_resolution_m, base_resolution_m
    )

    chosen_res = ladder[0]
    chosen_est = est_at_base
    coarsened = False
    for res in ladder:
        est = _estimate_active_cells_at_resolution(active_cells_at_base, base_resolution_m, res)
        chosen_res = res
        chosen_est = est
        coarsened = res > base_resolution_m
        if est <= cap:
            break

    capped_out = chosen_est > cap
    est_solve_s = estimate_swmm_solve_seconds(chosen_est)
    if capped_out:
        reason = (
            f"AOI exceeds cap even at coarsest rung {chosen_res:.0f}m "
            f"(est {chosen_est} > cap {cap}); clamped to coarsest rung"
        )
    elif coarsened:
        reason = (
            f"coarsened {base_resolution_m:.0f}m->{chosen_res:.0f}m to fit cap "
            f"{cap} (est {est_at_base}@base -> {chosen_est}@chosen)"
        )
    else:
        reason = f"base {base_resolution_m:.0f}m fits cap {cap} (est {chosen_est})"

    logger.info(
        "swmm autoscale: resolution_m=%.0f est_active=%d cap=%d est_solve=%.0fs reason=%s",
        chosen_res, chosen_est, cap, est_solve_s, reason,
    )
    return SWMMAutoscaleResult(
        resolution_m=chosen_res,
        estimated_active_cells=chosen_est,
        cell_cap=cap,
        base_resolution_m=base_resolution_m,
        estimated_active_cells_at_base=est_at_base,
        estimated_solve_seconds=est_solve_s,
        coarsened=coarsened,
        reason=reason,
    )


def suggest_swmm_resolution(
    dem_path: str,
    requested_resolution_m: float,
) -> SWMMAutoscaleResult:
    """Pre-run granularity suggestion for the #154 gate — DEM read + autoscale ONLY.

    Reads the staged DEM at ``requested_resolution_m``, counts active (finite)
    cells with the EXACT same ``_read_and_resample_dem`` + ``np.isfinite(...).sum()``
    that :func:`build_swmm_mesh` uses for its inline autoscale prelude, then runs
    :func:`autoscale_swmm_resolution` on that count. This is the ONLY thing it
    does — no deck authoring, no building rasterization, no run. Reusing the same
    read+count guarantees the gate card and the real build cannot diverge: the
    suggested resolution / active-cell estimate the user SEES is what the build
    would compute given the same DEM + requested resolution.

    The active-cell count here is the count BEFORE building drop (the build
    additionally subtracts dropped-building cells later), matching the inline
    autoscale prelude in :func:`build_swmm_mesh` which also autoscales on the raw
    finite-cell count at the base resolution.

    Args:
        dem_path: an on-disk DEM (GeoTIFF) path the mesh builder reads (the
            composer localizes the cache URI to a local path before calling).
        requested_resolution_m: the user-requested overland cell size, m (> 0) —
            the base resolution the ladder snaps UP from.

    Returns:
        A :class:`SWMMAutoscaleResult` carrying the suggested resolution, the
        active-cell estimate at the suggested resolution, the honoured cell cap,
        the estimated solve seconds, and the coarsened flag + reason.

    Raises:
        SWMMMeshError: ``SWMM_DEPENDENCY_MISSING`` (numpy/rasterio unavailable),
        ``SWMM_DEM_UNREADABLE`` (read failure), or ``SWMM_EMPTY_MESH`` (the DEM
        produced zero finite cells at the requested resolution).
    """
    if np is None:  # pragma: no cover - numpy is a hard dep in practice
        raise SWMMMeshError("SWMM_DEPENDENCY_MISSING", message="numpy unavailable")

    base_res = float(requested_resolution_m)
    if base_res <= 0:
        # Defensive: a non-positive request would degenerate the ladder. The
        # contract / tool default is 10 m; fall back to the builder default.
        base_res = 10.0

    # IDENTICAL read + count to build_swmm_mesh's inline prelude (~839-858) so
    # the card and the build cannot diverge.
    grid = _read_and_resample_dem(dem_path, base_res)
    active_at_base = int(np.isfinite(grid.elev).sum())
    if active_at_base <= 0:
        raise SWMMMeshError(
            "SWMM_EMPTY_MESH",
            message="DEM produced zero finite cells at the requested resolution",
            details={"dem_path": dem_path, "resolution_m": base_res},
        )
    return autoscale_swmm_resolution(active_at_base, base_resolution_m=base_res)


@dataclass(frozen=True)
class SWMMRealCapClampResult:
    """Outcome of :func:`clamp_swmm_resolution_to_real_cap`.

    ``resolution_m`` is the resolution the build MUST use; ``real_active_cells``
    is the REAL ceil-grid finite-cell count :func:`build_swmm_mesh` will count at
    that resolution (already <= ``cell_cap`` whenever a finite resolution exists);
    ``clamped`` is True iff the user's ``chosen_resolution_m`` was coarsened to fit.
    """

    resolution_m: float
    real_active_cells: int
    cell_cap: int
    chosen_resolution_m: float
    clamped: bool


def clamp_swmm_resolution_to_real_cap(
    dem_path: str,
    chosen_resolution_m: float,
    *,
    cell_cap: int | None = None,
) -> SWMMRealCapClampResult:
    """Clamp a user-chosen SWMM resolution against the REAL build cell count.

    The #154 ``narrow_scope`` override gate previously inverted the AREA model
    ``cells = base_cells * (base/res)**2`` to find the finest resolution that
    "fits" the cap, then built with ``enable_autoscale=False`` (no downstream
    cap re-check). But :func:`build_swmm_mesh` re-reads the DEM at the clamped
    resolution and counts active cells via the REAL ``ceil(extent/res)`` grid
    (the same ``_read_and_resample_dem`` + ``np.isfinite().sum()`` the build
    uses), which OVERSHOOTS the area model (~6% for a square fully-active AOI,
    worse for sparse AOIs) -- so an over-fine override could still solve OVER cap.

    This helper closes that breach by clamping against the AUTHORITATIVE count:
    it probes the real grid at the SWMM resolution ladder (the same rungs the
    proceed-path autoscaler walks), ASCENDING from the chosen resolution, and
    returns the FINEST rung whose REAL active-cell count is at or under the cap.
    A coarser-than-cap choice is honoured unchanged (its real count already
    fits). The walk never degenerates: it stops at the coarsest rung even if the
    cap is still exceeded (a huge AOI solves coarse but non-empty), mirroring
    :func:`autoscale_swmm_resolution`.

    Probing reads the DEM at candidate resolutions via the SAME
    :func:`_read_and_resample_dem` the build uses, so the returned
    ``real_active_cells`` is exactly what :func:`build_swmm_mesh` will count.
    This is synchronous compute (rasterio + numpy) -- the gate offloads the whole
    call via ``asyncio.to_thread`` (no-sync-blocking-on-asyncio-loop norm).

    Args:
        dem_path: an on-disk DEM (GeoTIFF) the mesh builder reads (the gate has
            already localized the cache URI to a local path).
        chosen_resolution_m: the user's narrow_scope override resolution, m (>0);
            a non-positive value falls back to the finest ladder rung.
        cell_cap: the cap to honour; defaults to :func:`compute_swmm_cell_cap`.

    Returns:
        A :class:`SWMMRealCapClampResult`.

    Raises:
        SWMMMeshError: ``SWMM_DEPENDENCY_MISSING`` / ``SWMM_DEM_UNREADABLE`` from
        the DEM read, or ``SWMM_EMPTY_MESH`` if every candidate rung is empty.
    """
    if np is None:  # pragma: no cover - numpy is a hard dep in practice
        raise SWMMMeshError("SWMM_DEPENDENCY_MISSING", message="numpy unavailable")

    cap = int(cell_cap) if cell_cap is not None else compute_swmm_cell_cap()
    if cap <= 0:
        cap = SWMM_MIN_CELL_CAP

    chosen = float(chosen_resolution_m)
    if chosen <= 0:
        chosen = float(min(SWMM_RES_LADDER)) if SWMM_RES_LADDER else 10.0

    # Candidate rungs >= the chosen resolution (we only ever COARSEN to fit the
    # cap; a finer rung would only INCREASE the count). Always include the chosen
    # resolution itself as the finest candidate so an under-cap choice is kept
    # EXACTLY (not snapped onto a ladder rung).
    rungs = sorted({chosen, *(float(r) for r in SWMM_RES_LADDER if float(r) >= chosen)})
    if not rungs:
        rungs = [chosen]

    def _real_active_cells(res_m: float) -> int:
        grid = _read_and_resample_dem(dem_path, res_m)
        return int(np.isfinite(grid.elev).sum())

    last_res = rungs[0]
    last_count = 0
    for res in rungs:
        count = _real_active_cells(res)
        last_res, last_count = res, count
        if count <= cap:
            break

    if last_count <= 0:
        raise SWMMMeshError(
            "SWMM_EMPTY_MESH",
            message="DEM produced zero finite cells at every candidate resolution",
            details={"dem_path": dem_path, "chosen_resolution_m": chosen},
        )

    clamped = last_res > chosen
    if last_count > cap:
        logger.warning(
            "swmm real-cap clamp: coarsest rung %.2fm still over cap "
            "(real %d > cap %d) -- solving coarse but non-empty",
            last_res, last_count, cap,
        )
    logger.info(
        "swmm real-cap clamp: chosen=%.2fm -> built=%.2fm real_active=%d cap=%d clamped=%s",
        chosen, last_res, last_count, cap, clamped,
    )
    return SWMMRealCapClampResult(
        resolution_m=float(last_res),
        real_active_cells=int(last_count),
        cell_cap=cap,
        chosen_resolution_m=chosen,
        clamped=bool(clamped),
    )


# --------------------------------------------------------------------------- #
# DEM read + resample to the active grid.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _Grid:
    """A resampled DEM grid + its geo-referencing, in a projected metres CRS."""

    elev: Any  # np.ndarray (n_rows, n_cols), float; nan = nodata/inactive
    res_m: float
    transform: Any  # affine.Affine of the resampled grid (metres)
    crs: Any  # rasterio CRS (projected metres)
    nrows: int
    ncols: int


def _utm_crs_for_lonlat(lon: float, lat: float):
    """Pick the UTM zone CRS for a lon/lat so resampling/lengths are in metres."""
    from rasterio.crs import CRS

    zone = int((lon + 180.0) // 6.0) + 1
    epsg = (32600 if lat >= 0 else 32700) + zone
    return CRS.from_epsg(epsg)


def _read_and_resample_dem(dem_path: str, target_res_m: float) -> _Grid:
    """Read the DEM, reproject to a metres CRS, resample to ``target_res_m``.

    Uses ``Resampling.average`` (mean elevation per coarse cell — the right
    aggregation for an overland invert). Returns a ``_Grid`` whose ``elev`` has
    ``nan`` at nodata. Raises ``SWMMMeshError("SWMM_DEM_UNREADABLE")`` on read
    failure.
    """
    try:
        import rasterio
        from rasterio.enums import Resampling
        from rasterio.warp import calculate_default_transform, reproject
    except Exception as exc:  # pragma: no cover
        raise SWMMMeshError(
            "SWMM_DEPENDENCY_MISSING",
            message=f"rasterio unavailable for DEM resample: {exc}",
        ) from exc

    try:
        with rasterio.open(dem_path) as src:
            src_crs = src.crs
            src_bounds = src.bounds
            # Choose a projected metres CRS. If the DEM is already projected
            # (metres), keep it; if geographic, reproject to the local UTM zone.
            is_geographic = bool(getattr(src_crs, "is_geographic", True)) if src_crs else True
            if is_geographic or src_crs is None:
                centre_lon = 0.5 * (src_bounds.left + src_bounds.right)
                centre_lat = 0.5 * (src_bounds.bottom + src_bounds.top)
                dst_crs = _utm_crs_for_lonlat(centre_lon, centre_lat)
            else:
                dst_crs = src_crs

            # Target transform at the requested metres resolution.
            dst_transform, dst_w, dst_h = calculate_default_transform(
                src_crs, dst_crs, src.width, src.height, *src_bounds,
                resolution=target_res_m,
            )
            src_arr = src.read(1, masked=True).astype("float64").filled(np.nan)
            src_nodata = src.nodata

            dst_arr = np.full((dst_h, dst_w), np.nan, dtype="float64")
            reproject(
                source=src_arr,
                destination=dst_arr,
                src_transform=src.transform,
                src_crs=src_crs if src_crs else dst_crs,
                dst_transform=dst_transform,
                dst_crs=dst_crs,
                resampling=Resampling.average,
                src_nodata=src_nodata if src_nodata is not None else np.nan,
                dst_nodata=np.nan,
            )
    except SWMMMeshError:
        raise
    except Exception as exc:
        raise SWMMMeshError(
            "SWMM_DEM_UNREADABLE",
            message=f"could not read/resample DEM {dem_path}: {exc}",
            details={"dem_path": dem_path},
        ) from exc

    # Mask common sentinels that survived as finite numbers.
    for s in (-9999.0, -32768.0, 3.4028234663852886e38):
        dst_arr = np.where(np.isclose(dst_arr, s), np.nan, dst_arr)

    nrows, ncols = dst_arr.shape
    return _Grid(
        elev=dst_arr,
        res_m=float(target_res_m),
        transform=dst_transform,
        crs=dst_crs,
        nrows=int(nrows),
        ncols=int(ncols),
    )


def _rasterize_buildings(grid: _Grid, footprints: Any) -> Any:
    """Rasterize building footprints onto the grid → bool mask (True=building).

    ``footprints`` is a GeoJSON FeatureCollection dict (the
    ``fetch_buildings(source=osm)`` shape) OR a list of shapely geometries OR
    ``None``. Reprojects geometries into the grid CRS, then
    ``rasterio.features.rasterize``. Returns an all-False mask when there are no
    footprints.
    """
    mask = np.zeros((grid.nrows, grid.ncols), dtype=bool)
    if footprints is None:
        return mask
    try:
        from rasterio.features import rasterize
        from rasterio.warp import transform_geom
    except Exception:  # pragma: no cover
        return mask

    # Normalise to a list of (geom_mapping, src_crs).
    geoms: list[dict] = []
    src_crs = "EPSG:4326"  # GeoJSON / OSM footprints are WGS84 lon/lat
    if isinstance(footprints, dict) and footprints.get("type") == "FeatureCollection":
        for feat in footprints.get("features", []) or []:
            g = feat.get("geometry")
            if isinstance(g, dict) and g.get("type") in ("Polygon", "MultiPolygon"):
                geoms.append(g)
    elif isinstance(footprints, (list, tuple)):
        from shapely.geometry import mapping as shp_mapping

        for f in footprints:
            try:
                geoms.append(shp_mapping(f))
            except Exception:
                if isinstance(f, dict):
                    geoms.append(f)
    if not geoms:
        return mask

    # Reproject each geometry into the grid CRS.
    projected = []
    for g in geoms:
        try:
            projected.append(transform_geom(src_crs, grid.crs, g))
        except Exception:
            continue
    if not projected:
        return mask

    try:
        burned = rasterize(
            [(g, 1) for g in projected],
            out_shape=(grid.nrows, grid.ncols),
            transform=grid.transform,
            fill=0,
            all_touched=True,
            dtype="uint8",
        )
        mask = burned.astype(bool)
    except Exception as exc:  # noqa: BLE001
        logger.warning("swmm: building rasterize failed (%s); no obstruction applied", exc)
    return mask


# --------------------------------------------------------------------------- #
# Barrier snapping: tagged LineStrings -> cell-pair edges.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _BarrierEdge:
    """A snapped barrier on the edge between two 4-neighbour cells.

    ``protected`` is the cell the barrier protects (the "dry"/landward side) and
    ``wet`` is the cell on the flooded side. For a ``flap_gate`` the orifice runs
    ``from_node=protected -> to_node=wet`` so SWMM's flap blocks reverse (wet ->
    protected) inflow into the protected area. For a ``wall`` we simply OMIT the
    overland conduit between the two cells.
    """

    barrier_type: str  # "wall" | "flap_gate"
    cell_a: tuple[int, int]
    cell_b: tuple[int, int]
    protected: tuple[int, int]
    wet: tuple[int, int]


def _latlon_to_grid_rc(grid: _Grid, lon: float, lat: float) -> tuple[int, int] | None:
    """Project a WGS84 lon/lat to the grid's metres CRS, then to (row, col).

    Returns ``None`` if the point falls outside the grid.
    """
    from rasterio.transform import rowcol
    from rasterio.warp import transform as warp_transform

    try:
        xs, ys = warp_transform("EPSG:4326", grid.crs, [lon], [lat])
        x, y = xs[0], ys[0]
        r, c = rowcol(grid.transform, x, y)
    except Exception:
        return None
    if 0 <= r < grid.nrows and 0 <= c < grid.ncols:
        return int(r), int(c)
    return None


def _snap_barriers_to_edges(
    grid: _Grid, active: Any, barriers: dict | None
) -> list[_BarrierEdge]:
    """Snap each tagged LineString to the sequence of 4-neighbour cell-pair edges
    its vertices cross.

    For each consecutive vertex pair we walk the cells the segment passes
    through (Bresenham-style on the grid) and, for every 4-adjacent transition
    between two ACTIVE cells, emit a ``_BarrierEdge``. The PROTECTED side is read
    from the feature's ``properties.protected_side`` ("left"|"right" relative to
    the segment direction) — defaulting to the higher-elevation cell as
    "protected" (water comes from the lower/wet side) when unspecified.
    """
    out: list[_BarrierEdge] = []
    if not barriers:
        return out
    features = barriers.get("features") if isinstance(barriers, dict) else None
    if not isinstance(features, list):
        return out

    for feat in features:
        geom = (feat or {}).get("geometry") or {}
        if geom.get("type") != "LineString":
            continue
        props = (feat or {}).get("properties") or {}
        btype = props.get("barrier_type")
        if btype not in ("wall", "flap_gate"):
            continue
        protected_side = props.get("protected_side")  # "left"|"right"|None
        coords = geom.get("coordinates") or []
        # Project vertices to grid (row, col).
        rc_pts: list[tuple[int, int]] = []
        for lon, lat in [(c[0], c[1]) for c in coords if len(c) >= 2]:
            rc = _latlon_to_grid_rc(grid, lon, lat)
            if rc is not None:
                rc_pts.append(rc)
        if len(rc_pts) < 2:
            continue

        for (r0, c0), (r1, c1) in zip(rc_pts[:-1], rc_pts[1:]):
            cells = _bresenham_cells(r0, c0, r1, c1)
            # The "side" sign for protected_side: cross product of the segment
            # direction with the cell->neighbour direction.
            seg_dr, seg_dc = (r1 - r0), (c1 - c0)
            for (pr, pc), (nr, nc) in zip(cells[:-1], cells[1:]):
                # Only 4-adjacent transitions (skip diagonals from Bresenham).
                if abs(pr - nr) + abs(pc - nc) != 1:
                    continue
                a, b = (pr, pc), (nr, nc)
                if not (active[a[0], a[1]] and active[b[0], b[1]]):
                    continue
                protected, wet = _resolve_protected(
                    grid, a, b, seg_dr, seg_dc, protected_side
                )
                out.append(
                    _BarrierEdge(
                        barrier_type=btype, cell_a=a, cell_b=b, protected=protected, wet=wet
                    )
                )
    return out


def _resolve_protected(grid, a, b, seg_dr, seg_dc, protected_side):
    """Decide which of the cell pair (a,b) is PROTECTED vs WET.

    If ``protected_side`` is given ("left"/"right" of the segment direction) we
    use the sign of the 2D cross product of the segment vector with the a->b
    vector to assign sides. Otherwise the HIGHER-elevation cell is treated as
    protected (water rises from the lower side).
    """
    if protected_side in ("left", "right"):
        # cross = seg x (a->b). >0 => b is to the LEFT of the segment direction.
        ab_dr, ab_dc = (b[0] - a[0]), (b[1] - a[1])
        cross = seg_dr * ab_dc - seg_dc * ab_dr
        b_is_left = cross > 0
        if protected_side == "left":
            protected = b if b_is_left else a
        else:  # "right"
            protected = a if b_is_left else b
        wet = a if protected == b else b
        return protected, wet
    # Fallback: higher cell = protected.
    ea = grid.elev[a[0], a[1]]
    eb = grid.elev[b[0], b[1]]
    ea = ea if not (np is not None and np.isnan(ea)) else -1e30
    eb = eb if not (np is not None and np.isnan(eb)) else -1e30
    if ea >= eb:
        return a, b
    return b, a


def _bresenham_cells(r0: int, c0: int, r1: int, c1: int) -> list[tuple[int, int]]:
    """4-connected supercover line from (r0,c0) to (r1,c1).

    Unlike classic Bresenham (which steps diagonally), this yields a path where
    every consecutive pair is 4-adjacent, so a barrier line crossing a diagonal
    still snaps to two real cell-pair edges. Standard grid-supercover walk.
    """
    cells: list[tuple[int, int]] = [(r0, c0)]
    r, c = r0, c0
    dr = abs(r1 - r0)
    dc = abs(c1 - c0)
    sr = 1 if r1 > r0 else -1
    sc = 1 if c1 > c0 else -1
    err = dr - dc
    while (r, c) != (r1, c1):
        e2 = 2 * err
        moved = False
        if e2 > -dc:
            err -= dc
            r += sr
            cells.append((r, c))
            moved = True
        if e2 < dr:
            err += dr
            c += sc
            cells.append((r, c))
            moved = True
        if not moved:  # safety: should not happen
            break
    return cells


# --------------------------------------------------------------------------- #
# Infiltration parameters per method (on the pervious fraction).
# --------------------------------------------------------------------------- #
def _infiltration_option(method: str) -> str:
    """Map our infiltration_method to the SWMM OPTIONS[INFILTRATION] keyword."""
    return {
        "none": "HORTON",  # with zero rates == no loss (spike pattern)
        "scs_cn": "CURVE_NUMBER",
        "green_ampt": "GREEN_AMPT",
    }.get(method, "HORTON")


def _add_infiltration_obj(inp, sections, scname: str, method: str, *, curve_number: float,
                          ga_suction: float, ga_conductivity: float, ga_init_deficit: float):
    """Add the per-subcatchment INFILTRATION object matching ``method``."""
    from swmm_api.input_file.sections.subcatch import (
        InfiltrationHorton,
        InfiltrationCurveNumber,
        InfiltrationGreenAmpt,
    )

    if method == "scs_cn":
        inp.add_obj(
            InfiltrationCurveNumber(
                subcatchment=scname,
                curve_no=curve_number,
                # 2nd param historically hydraulic conductivity (unused in
                # modern SWMM5) / 3rd = drying time (days).
                hydraulic_conductivity=0.0,
                time_dry=7.0,
            )
        )
    elif method == "green_ampt":
        inp.add_obj(
            InfiltrationGreenAmpt(
                subcatchment=scname,
                suction_head=ga_suction,
                hydraulic_conductivity=ga_conductivity,
                moisture_deficit_init=ga_init_deficit,
            )
        )
    else:  # "none" -> zero-rate Horton (all rain runs off)
        inp.add_obj(
            InfiltrationHorton(
                subcatchment=scname,
                rate_max=0.0, rate_min=0.0, decay=0.0, time_dry=0.0, volume_max=0.0,
            )
        )


# --------------------------------------------------------------------------- #
# The build.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class BuildResult:
    """Result of ``build_swmm_mesh`` — the deck path + mesh provenance."""

    inp_path: str
    n_active_cells: int
    n_storage_nodes: int
    n_conduits: int
    n_buildings_dropped: int
    n_walls: int
    n_flap_gates: int
    resolution_m: float
    autoscale_reason: str
    crs: str
    transform: list[float]
    hyetograph: HyetographResult
    grid_shape: tuple[int, int]
    outfall_cell: tuple[int, int]
    barriers_geojson: dict | None = field(default=None)
    # Water-quality provenance (sprint-WQ): (name, unit) per authored pollutant,
    # in [POLLUTANTS] / out.pollutants ORDER, so the postprocess maps each
    # POLLUT_CONC index -> name/unit WITHOUT re-parsing the deck. Empty when the
    # run authored no WQ sections (hydraulics-only) — the byte-identical default.
    pollutants: list[tuple[str, str]] = field(default_factory=list)


def _cell_node(i: int, j: int) -> str:
    return f"S_{i}_{j}"


def build_swmm_mesh(
    *,
    dem_path: str,
    out_inp_path: str,
    bbox: tuple[float, float, float, float] | None = None,
    total_rain_depth_mm: float = 120.0,
    storm_duration_hr: float = 6.0,
    rain_interval_min: int = 5,
    target_resolution_m: float = 10.0,
    building_footprints: Any = None,
    building_representation: str = "drop",
    building_raise_m: float = 1.0,
    infiltration_method: str = "none",
    curve_number: float = 80.0,
    green_ampt_suction_mm: float = 110.0,
    green_ampt_conductivity_mm_hr: float = 3.3,
    green_ampt_init_deficit: float = 0.3,
    manning_overland: float = DEFAULT_OVERLAND_N,
    nlcd_manning: float | None = None,
    barriers: dict | None = None,
    nesting_exponent: float = 0.62,
    sim_routing_step_s: float = 2.0,
    enable_autoscale: bool = True,
    advanced_physics: dict | None = None,
    pollutants: list[Any] | None = None,
    dry_buildup_days: int = 0,
    washoff_model: str = "exp",
) -> BuildResult:
    """Build a quasi-2D SWMM ``.inp`` deck from a DEM (+ buildings + barriers).

    Returns a :class:`BuildResult`. Raises :class:`SWMMMeshError` with a typed
    code on any structural failure (unreadable DEM, empty mesh, missing dep).
    The deck is NOT run here — call :func:`run_swmm_deck` to solve it and apply
    the mass-balance honesty gate.

    The mesh follows the P0 spike exactly: one STORAGE node per active cell,
    4-connectivity RECT_OPEN overland conduits, per-cell rainfall subcatchments,
    and ONE dedicated boundary outfall fed by a single conduit.
    """
    if np is None:  # pragma: no cover
        raise SWMMMeshError("SWMM_DEPENDENCY_MISSING", message="numpy unavailable")

    # --- swmm-api is lazy-imported here (NOT at module top) ---
    try:
        from swmm_api import SwmmInput
        from swmm_api.input_file.section_labels import OPTIONS, REPORT
        from swmm_api.input_file.sections.node import Storage, Outfall
        from swmm_api.input_file.sections.link import Conduit, Orifice
        from swmm_api.input_file.sections.link_component import CrossSection
        from swmm_api.input_file.sections.subcatch import SubCatchment, SubArea
        from swmm_api.input_file.sections.others import RainGage, TimeseriesData
    except Exception as exc:
        raise SWMMMeshError(
            "SWMM_DEPENDENCY_MISSING",
            message=f"swmm-api unavailable: {exc}. Install pyswmm + swmm-api.",
        ) from exc

    # --- water-quality specs (sprint-WQ): normalize to plain dicts ONCE so the
    # authoring below is agnostic to PollutantSpec pydantic objects vs dicts.
    # EMPTY => NO WQ sections => a BYTE-IDENTICAL hydraulics-only deck (the depth
    # path is not touched when pollutants is None/[]). ------------------------
    wq_specs: list[dict[str, Any]] = _normalize_pollutant_specs(pollutants)

    # --- choose resolution via the adaptive budget (RE-FIT from P0 anchor) ---
    # We need an active-cell estimate at the base resolution; read the DEM once
    # at the requested resolution to count active cells, then (optionally) snap.
    grid = _read_and_resample_dem(dem_path, target_resolution_m)
    active_at_base = int(np.isfinite(grid.elev).sum())
    if active_at_base <= 0:
        raise SWMMMeshError(
            "SWMM_EMPTY_MESH",
            message="DEM produced zero finite cells at the requested resolution",
            details={"dem_path": dem_path, "resolution_m": target_resolution_m},
        )

    autoscale_reason = "autoscale disabled"
    res_m = target_resolution_m
    if enable_autoscale:
        auto = autoscale_swmm_resolution(active_at_base, base_resolution_m=target_resolution_m)
        autoscale_reason = auto.reason
        if auto.resolution_m != target_resolution_m:
            res_m = auto.resolution_m
            grid = _read_and_resample_dem(dem_path, res_m)

    elev = grid.elev
    nrows, ncols = grid.nrows, grid.ncols

    # --- building obstruction mask ---
    building_mask = _rasterize_buildings(grid, building_footprints)
    n_buildings_dropped = 0

    # --- the ACTIVE mask: finite DEM AND (not a dropped building) ---
    active = np.isfinite(elev)
    if building_representation == "drop":
        drop = active & building_mask
        n_buildings_dropped = int(drop.sum())
        active = active & ~building_mask
    elif building_representation == "raise":
        # cells stay active; raise their invert below.
        n_buildings_dropped = 0
    elif building_representation == "roughness":
        n_buildings_dropped = 0
    else:
        raise SWMMMeshError(
            "SWMM_EMPTY_MESH",
            message=f"unknown building_representation {building_representation!r}",
        )

    n_active = int(active.sum())
    if n_active <= 1:
        raise SWMMMeshError(
            "SWMM_EMPTY_MESH",
            message=f"only {n_active} active cell(s) after building drop — nothing to solve",
            details={"resolution_m": res_m},
        )

    # --- effective cell elevations (apply 'raise' obstruction) ---
    cell_elev = elev.copy()
    if building_representation == "raise":
        bump = active & building_mask
        cell_elev = np.where(bump, cell_elev + float(building_raise_m), cell_elev)

    cell_area = res_m * res_m
    cell_area_ha = cell_area / 10_000.0

    # --- overland Manning n ---
    overland_n = float(nlcd_manning) if nlcd_manning is not None else float(manning_overland)
    # building roughness bump applies to conduits incident to building cells.
    rough_cells = (active & building_mask) if building_representation == "roughness" else None

    # --- nested design-storm hyetograph (P1 builder) ---
    hyet = build_nested_hyetograph(
        total_depth_mm=total_rain_depth_mm,
        storm_duration_hr=storm_duration_hr,
        rain_interval_min=rain_interval_min,
        nesting_exponent=nesting_exponent,
    )

    inp = SwmmInput()

    # --- OPTIONS (DYNWAVE overland; P0-proven settings) ---
    end_hh = int(storm_duration_hr) + 1  # +1 h drain-down tail
    inp[OPTIONS] = {
        "FLOW_UNITS": "CMS",
        "INFILTRATION": _infiltration_option(infiltration_method),
        "FLOW_ROUTING": "DYNWAVE",
        "LINK_OFFSETS": "DEPTH",
        "MIN_SLOPE": 0,
        "ALLOW_PONDING": "YES",
        "SKIP_STEADY_STATE": "NO",
        "START_DATE": "01/01/2024",
        "START_TIME": "00:00:00",
        "REPORT_START_DATE": "01/01/2024",
        "REPORT_START_TIME": "00:00:00",
        "END_DATE": "01/01/2024",
        "END_TIME": f"{end_hh:02d}:00:00",
        "SWEEP_START": "01/01",
        "SWEEP_END": "12/31",
        "DRY_DAYS": 0,
        "REPORT_STEP": "00:05:00",
        "WET_STEP": "00:01:00",
        "DRY_STEP": "00:05:00",
        "ROUTING_STEP": sim_routing_step_s,
        "RULE_STEP": "00:00:00",
        "INERTIAL_DAMPING": "PARTIAL",
        "NORMAL_FLOW_LIMITED": "BOTH",
        "FORCE_MAIN_EQUATION": "H-W",
        "VARIABLE_STEP": 0.75,
        "LENGTHENING_STEP": 0,
        "MIN_SURFAREA": 1.0,
        "MAX_TRIALS": 8,
        "HEAD_TOLERANCE": 0.0015,
        "SYS_FLOW_TOL": 5,
        "LAT_FLOW_TOL": 5,
        "MINIMUM_STEP": 0.5,
        "THREADS": 1,
    }

    # levers STEP 3: advanced_physics OPTIONS overrides (ALREADY VALIDATED by
    # physics_registry.validate_and_resolve_physics("swmm", ...)). The registry
    # keys map onto SWMM OPTIONS keys: routing_method -> FLOW_ROUTING,
    # routing_step_s -> ROUTING_STEP, variable_step -> VARIABLE_STEP,
    # threads -> THREADS. None / {} => byte-identical DYNWAVE deck.
    _phys = dict(advanced_physics or {})
    _SWMM_OPTION_BY_KEY = {
        "routing_method": "FLOW_ROUTING",
        "routing_step_s": "ROUTING_STEP",
        "variable_step": "VARIABLE_STEP",
        "threads": "THREADS",
    }
    for _k, _opt in _SWMM_OPTION_BY_KEY.items():
        if _k in _phys:
            inp[OPTIONS][_opt] = _phys[_k]

    # CONSTITUTIVE-PHYSICS levers (advanced / demo-default): subcatchment
    # roughness + imperviousness. Each falls back to the EXACT historical
    # literal when the user did not set it, so an unset run is byte-identical.
    #   n_imperv (SubArea)          historical 0.012
    #   n_perv   (SubArea)          historical 0.1
    #   imperviousness_pct          historical toggle: 100 (no infil) else 60
    _n_imperv = float(_phys.get("n_imperv", 0.012))
    _n_perv = float(_phys.get("n_perv", 0.1))
    _imperv_override = _phys.get("imperviousness_pct")

    # WQ antecedent dry-buildup lever: DRY_DAYS lets buildup accumulate over N
    # antecedent dry days before the storm. Only overridden when WQ is active AND
    # a non-zero value is requested — an unset/0 WQ run keeps the historical 0
    # (so the OPTIONS block stays byte-identical on the hydraulics-only path).
    if wq_specs and int(dry_buildup_days) > 0:
        inp[OPTIONS]["DRY_DAYS"] = int(dry_buildup_days)

    inp[REPORT] = {
        "INPUT": "NO", "CONTROLS": "NO", "SUBCATCHMENTS": "NONE",
        "NODES": "ALL", "LINKS": "ALL",
    }

    # --- RAINGAGE + nested hyetograph TIMESERIES ---
    inp.add_obj(TimeseriesData(name="HYET", data=list(hyet.timeseries)))
    inp.add_obj(
        RainGage(
            name="RG",
            form="INTENSITY",
            interval=f"0:{rain_interval_min:02d}",
            SCF=1.0,
            source="TIMESERIES",
            timeseries="HYET",
        )
    )

    # --- pick the dedicated boundary OUTFALL cell: lowest ACTIVE boundary cell.
    outfall_cell = _lowest_boundary_cell(active, cell_elev, nrows, ncols)
    if outfall_cell is None:
        # fall back to the globally-lowest active cell.
        outfall_cell = _lowest_active_cell(active, cell_elev)

    # --- STORAGE nodes (one per active cell) ---
    for i in range(nrows):
        for j in range(ncols):
            if not active[i, j]:
                continue
            inp.add_obj(
                Storage(
                    name=_cell_node(i, j),
                    elevation=float(cell_elev[i, j]),
                    depth_max=_DEPTH_MAX_M,
                    depth_init=0.0,
                    kind=Storage.TYPES.FUNCTIONAL,
                    data=[0.0, 0.0, cell_area],  # A1=0, A2=0, A0=cell_area
                )
            )

    # --- ONE dedicated boundary OUTFALL fed by a SINGLE conduit (P0 rule) ---
    oi, oj = outfall_cell
    out_elev = float(cell_elev[oi, oj]) - 1.0  # 1 m drop to drive free discharge
    inp.add_obj(Outfall(name="OUT", elevation=out_elev, kind=Outfall.TYPES.FREE))
    inp.add_obj(
        Conduit(
            name="L_OUTLET",
            from_node=_cell_node(oi, oj),
            to_node="OUT",
            length=res_m,
            roughness=overland_n,
            offset_upstream=0,
            offset_downstream=0,
        )
    )
    inp.add_obj(
        CrossSection(link="L_OUTLET", shape="RECT_OPEN", height=_COND_HEIGHT_M, parameter_2=res_m)
    )

    # --- SUBCATCHMENTS: one per active cell, drain rain onto that cell's node ---
    for i in range(nrows):
        for j in range(ncols):
            if not active[i, j]:
                continue
            scname = f"C_{i}_{j}"
            inp.add_obj(
                SubCatchment(
                    name=scname,
                    rain_gage="RG",
                    outlet=_cell_node(i, j),
                    area=cell_area_ha,
                    imperviousness=(
                        float(_imperv_override) if _imperv_override is not None
                        else (100.0 if infiltration_method == "none" else 60.0)
                    ),
                    width=res_m,
                    slope=0.5,
                )
            )
            inp.add_obj(
                SubArea(
                    subcatchment=scname,
                    n_imperv=_n_imperv,
                    n_perv=_n_perv,
                    storage_imperv=0.0,
                    storage_perv=0.0,
                    pct_zero=100,
                    route_to="OUTLET",
                )
            )
            _add_infiltration_obj(
                inp, None, scname, infiltration_method,
                curve_number=curve_number,
                ga_suction=green_ampt_suction_mm,
                ga_conductivity=green_ampt_conductivity_mm_hr,
                ga_init_deficit=green_ampt_init_deficit,
            )
            # WQ (sprint-WQ): assign the single uniform "urban" land use to THIS
            # per-cell subcatchment at 100% (one Coverage row per active cell,
            # mirroring the SubCatchment/SubArea loop). Gated on wq_specs so a
            # non-WQ run adds no [COVERAGES] section.
            if wq_specs:
                _add_coverage_obj(inp, scname)

    # --- WQ (sprint-WQ): author [POLLUTANTS]/[LANDUSES]/[BUILDUP]/[WASHOFF] ONCE.
    # Gated on wq_specs => a hydraulics-only deck is byte-identical. The solver
    # auto-runs buildup/washoff/routing when these sections are present (no
    # OPTIONS change beyond the optional DRY_DAYS lever above). ----------------
    wq_pollutants: list[tuple[str, str]] = []
    if wq_specs:
        wq_pollutants = _author_wq_sections(inp, wq_specs, washoff_model)

    # --- snap barriers to cell-pair edges BEFORE laying conduits ---
    barrier_edges = _snap_barriers_to_edges(grid, active, barriers)
    wall_edges = {
        frozenset({be.cell_a, be.cell_b}) for be in barrier_edges if be.barrier_type == "wall"
    }
    flap_edges = [be for be in barrier_edges if be.barrier_type == "flap_gate"]
    flap_edge_set = {frozenset({be.cell_a, be.cell_b}) for be in flap_edges}

    # --- overland CONDUITS (4-connectivity, de-dup'd) ---
    n_conduits = 1  # the outlet conduit
    for i in range(nrows):
        for j in range(ncols):
            if not active[i, j]:
                continue
            for ni, nj in ((i, j + 1), (i + 1, j)):  # east, south (de-dup)
                if ni >= nrows or nj >= ncols:
                    continue
                if not active[ni, nj]:
                    continue
                edge = frozenset({(i, j), (ni, nj)})
                if edge in wall_edges:
                    continue  # RED wall: OMIT the conduit
                if edge in flap_edge_set:
                    continue  # GREEN flap: an orifice replaces the conduit (below)
                cname = _add_overland_conduit(
                    inp, Conduit, CrossSection, cell_elev,
                    (i, j), (ni, nj), res_m, overland_n, rough_cells,
                )
                if cname:
                    n_conduits += 1

    # --- GREEN flap gates: native SWMM flap via ORIFICE has_flap_gate=True ---
    n_flap = 0
    for k, be in enumerate(flap_edges):
        fname = f"FLAP_{k}"
        # Orient from PROTECTED -> WET so the flap blocks reverse (wet->protected).
        inp.add_obj(
            Orifice(
                name=fname,
                from_node=_cell_node(*be.protected),
                to_node=_cell_node(*be.wet),
                orientation="SIDE",
                offset=0.0,
                discharge_coefficient=0.65,
                has_flap_gate=True,  # P0 carry-forward: the ONLY flap-bearing element
                hours_to_open=0,
            )
        )
        inp.add_obj(
            CrossSection(link=fname, shape="RECT_CLOSED", height=_COND_HEIGHT_M, parameter_2=res_m)
        )
        n_flap += 1

    Path(out_inp_path).parent.mkdir(parents=True, exist_ok=True)
    inp.write_file(out_inp_path)

    return BuildResult(
        inp_path=out_inp_path,
        n_active_cells=n_active,
        n_storage_nodes=n_active,
        n_conduits=n_conduits,
        n_buildings_dropped=n_buildings_dropped,
        n_walls=len(wall_edges),
        n_flap_gates=n_flap,
        resolution_m=res_m,
        autoscale_reason=autoscale_reason,
        crs=str(grid.crs),
        transform=list(grid.transform)[:6],
        hyetograph=hyet,
        grid_shape=(nrows, ncols),
        outfall_cell=outfall_cell,
        barriers_geojson=barriers,
        pollutants=wq_pollutants,
    )


def _add_overland_conduit(inp, Conduit, CrossSection, cell_elev, a, b, res_m, base_n, rough_cells):
    """Add a single RECT_OPEN overland conduit from the higher to lower cell."""
    ai, aj = a
    bi, bj = b
    if cell_elev[ai, aj] >= cell_elev[bi, bj]:
        frm, to = a, b
    else:
        frm, to = b, a
    cname = f"L_{frm[0]}_{frm[1]}__{to[0]}_{to[1]}"
    n = base_n
    if rough_cells is not None and (rough_cells[ai, aj] or rough_cells[bi, bj]):
        n = _BUILDING_ROUGHNESS_N
    inp.add_obj(
        Conduit(
            name=cname,
            from_node=_cell_node(*frm),
            to_node=_cell_node(*to),
            length=res_m,
            roughness=n,
            offset_upstream=0,
            offset_downstream=0,
        )
    )
    inp.add_obj(
        CrossSection(link=cname, shape="RECT_OPEN", height=_COND_HEIGHT_M, parameter_2=res_m)
    )
    return cname


def _lowest_boundary_cell(active, cell_elev, nrows, ncols):
    """The lowest-elevation ACTIVE cell on the grid boundary (for the outfall)."""
    best = None
    best_e = math.inf
    for i in range(nrows):
        for j in range(ncols):
            if not active[i, j]:
                continue
            if i in (0, nrows - 1) or j in (0, ncols - 1):
                e = float(cell_elev[i, j])
                if e < best_e:
                    best_e = e
                    best = (i, j)
    return best


def _lowest_active_cell(active, cell_elev):
    """The globally lowest active cell (fallback outfall feed)."""
    best = None
    best_e = math.inf
    nrows, ncols = active.shape
    for i in range(nrows):
        for j in range(ncols):
            if active[i, j] and float(cell_elev[i, j]) < best_e:
                best_e = float(cell_elev[i, j])
                best = (i, j)
    return best


# --------------------------------------------------------------------------- #
# Run + mass-balance honesty gate.
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Water-quality (buildup/washoff) deck authoring (sprint-WQ). All gated by the
# caller on a non-empty pollutant list; a hydraulics-only deck never reaches
# here, so the depth path stays byte-identical.
# --------------------------------------------------------------------------- #
#: The single uniform land use for v1. We have NO per-cell land-use raster, so
#: one "urban" class at 100% coverage is the honest demo minimum (never fake
#: sub-block residential/commercial precision — the NLCD split is the deferred
#: upgrade). Kept as a constant so the Coverage loop + [LANDUSES] agree.
_WQ_LAND_USE: str = "urban"


def _normalize_pollutant_specs(pollutants: list[Any] | None) -> list[dict[str, Any]]:
    """Coerce a list of ``PollutantSpec`` pydantic objects / dicts to plain dicts.

    The builder must not depend on the contracts package, so it reads specs
    structurally (getattr for pydantic, ``[]`` for dicts). Each returned dict has
    the eight WQ keys (name/unit/buildup_max/buildup_rate/buildup_power/
    washoff_coef/washoff_exp/decay_per_day) + ``emc_concentration``. A spec
    missing a ``name`` or ``buildup_max`` is dropped (never author a nameless /
    zero-buildup pollutant). Returns ``[]`` for ``None`` / empty (byte-identical
    hydraulics-only deck).
    """
    if not pollutants:
        return []

    def _get(spec: Any, key: str, default: Any = None) -> Any:
        if isinstance(spec, dict):
            return spec.get(key, default)
        return getattr(spec, key, default)

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for spec in pollutants:
        name = _get(spec, "name")
        bmax = _get(spec, "buildup_max")
        if not name or bmax is None:
            continue
        nm = str(name)
        if nm in seen:
            continue  # dedup by name (POLLUT_CONC index must be 1:1 with name)
        seen.add(nm)
        out.append(
            {
                "name": nm,
                "unit": str(_get(spec, "unit", "MG/L") or "MG/L"),
                "buildup_max": float(bmax),
                "buildup_rate": float(_get(spec, "buildup_rate", 1.0) or 0.0),
                "buildup_power": float(_get(spec, "buildup_power", 1.0) or 1.0),
                "washoff_coef": float(_get(spec, "washoff_coef", 5.0) or 0.0),
                "washoff_exp": float(_get(spec, "washoff_exp", 1.8) or 0.0),
                "decay_per_day": float(_get(spec, "decay_per_day", 0.0) or 0.0),
                "emc_concentration": float(_get(spec, "emc_concentration", 100.0) or 0.0),
            }
        )
    return out


def _add_coverage_obj(inp: Any, scname: str) -> None:
    """Add one [COVERAGES] row assigning the single "urban" land use at 100%."""
    from swmm_api.input_file.sections.subcatch import Coverage

    inp.add_obj(Coverage(subcatchment=scname, land_use_dict={_WQ_LAND_USE: 100.0}))


def _author_wq_sections(
    inp: Any, wq_specs: list[dict[str, Any]], washoff_model: str
) -> list[tuple[str, str]]:
    """Author [POLLUTANTS]/[LANDUSES]/[BUILDUP]/[WASHOFF] onto the deck ONCE.

    Returns the ``(name, unit)`` list in authored ([POLLUTANTS]) ORDER — the SAME
    order SWMM's ``out.pollutants`` reports, so the postprocess maps each
    POLLUT_CONC index -> name/unit without re-parsing the deck. Semantics PINNED
    by the Phase-1 in-image smoke:

      - Pollutant: decay is a first-order routing sink (1/day); rain/gw/rdii
        concentrations are 0 (demo). Count units (``#/L``) propagate to the
        outfall load as a LOG10 count in the .rpt (handled downstream).
      - BuildUp POW: swmm-api ``BuildUp(C1,C2,C3)`` IS SWMM's column order
        (max, rate, TIME-EXPONENT); per-unit AREA => mass/ha (kg/ha for MG/L).
        A large exponent overflows ``t^power`` and SWMM rejects the deck, so
        ``buildup_power`` is kept small by the presets.
      - WashOff: EXP (``W = C1 * q^C2 * B``, runoff-driven first flush) is the
        headline; EMC (fixed event-mean concentration, bypasses buildup) is the
        flat-conc control run selected by ``washoff_model="emc"``.
    """
    from swmm_api.input_file.sections.others import (
        BuildUp,
        LandUse,
        Pollutant,
        WashOff,
    )

    # ONE uniform land use (see _WQ_LAND_USE rationale).
    inp.add_obj(LandUse(name=_WQ_LAND_USE))

    use_emc = str(washoff_model).strip().lower() == "emc"
    authored: list[tuple[str, str]] = []
    for spec in wq_specs:
        name = spec["name"]
        unit = spec["unit"]
        inp.add_obj(
            Pollutant(
                name=name,
                unit=unit,
                c_rain=0.0,
                c_gw=0.0,
                c_rdii=0.0,
                decay=spec["decay_per_day"],
            )
        )
        # BuildUp is authored even in EMC mode (harmless; EMC washoff ignores it).
        inp.add_obj(
            BuildUp(
                land_use=_WQ_LAND_USE,
                pollutant=name,
                func_type=BuildUp.FUNCTIONS.POW,
                C1=spec["buildup_max"],
                C2=spec["buildup_rate"],
                C3=spec["buildup_power"],
                per_unit=BuildUp.UNIT.AREA,
            )
        )
        if use_emc:
            # EMC: C1 = fixed event-mean concentration; C2 unused (0). No first
            # flush (a constant-concentration dilution control).
            inp.add_obj(
                WashOff(
                    land_use=_WQ_LAND_USE,
                    pollutant=name,
                    func_type=WashOff.FUNCTIONS.EMC,
                    C1=spec["emc_concentration"],
                    C2=0.0,
                    sweeping_removal=0.0,
                    BMP_removal=0.0,
                )
            )
        else:
            inp.add_obj(
                WashOff(
                    land_use=_WQ_LAND_USE,
                    pollutant=name,
                    func_type=WashOff.FUNCTIONS.EXP,
                    C1=spec["washoff_coef"],
                    C2=spec["washoff_exp"],
                    sweeping_removal=0.0,
                    BMP_removal=0.0,
                )
            )
        authored.append((name, unit))
    return authored


def read_flow_routing_continuity(rpt_path: str) -> float | None:
    """Parse the **Flow Routing Continuity** error (%) from a SWMM ``.rpt``.

    Returns the signed percentage, or ``None`` if no such line was found (the
    run did not complete the routing report).
    """
    import re

    try:
        txt = Path(rpt_path).read_text()
    except Exception:
        return None
    in_block = False
    for line in txt.splitlines():
        if "Flow Routing Continuity" in line:
            in_block = True
            continue
        if in_block and "Continuity Error" in line:
            m = re.search(r"(-?\d+\.\d+)\s*$", line.strip())
            if m:
                return float(m.group(1))
    return None


def read_quality_routing_continuity(
    rpt_path: str, pollutant_index: int = 0
) -> float | None:
    """Parse the **Quality Routing Continuity** error (%) for one pollutant.

    SWMM's ``.rpt`` Quality Routing Continuity block carries ONE column per
    pollutant, in ``[POLLUTANTS]`` order (the header row shows the per-column
    UNITS — ``kg`` / ``LogN`` — not the names, so the mapping is POSITIONAL:
    ``pollutant_index`` is the 0-based position in ``BuildResult.pollutants``).
    The block's ``Continuity Error (%) .....  <err_p0>  <err_p1> ...`` line has
    one signed percentage per pollutant; this returns the value at
    ``pollutant_index``.

    Returns the signed percentage, or ``None`` when the block / column is absent
    (a WQ-less run, or a run whose WQ report did not complete). Pinned against the
    Phase-1 in-image smoke ``.rpt`` block format.
    """
    import re

    try:
        txt = Path(rpt_path).read_text()
    except Exception:
        return None
    in_block = False
    for line in txt.splitlines():
        if "Quality Routing Continuity" in line:
            in_block = True
            continue
        if in_block and "Continuity Error" in line:
            # every signed decimal on the line, in column (pollutant) order.
            nums = re.findall(r"-?\d+\.\d+", line)
            if 0 <= pollutant_index < len(nums):
                return float(nums[pollutant_index])
            return None
    return None


@dataclass(frozen=True)
class RunResult:
    """Result of running a SWMM deck headless via pyswmm."""

    rpt_path: str
    out_path: str
    continuity_error_pct: float
    n_steps: int
    last_dt_s: float | None
    wall_seconds: float
    peak_depth_grid: Any  # np.ndarray (nrows, ncols), peak node depth (m); nan=inactive
    max_depth_m: float
    n_wet_cells: int


def run_swmm_deck(
    build: BuildResult,
    *,
    mass_balance_tolerance_pct: float = 5.0,
    wet_threshold_m: float = 0.05,
    sample_every_steps: int = 30,
) -> RunResult:
    """Run the built deck headless via pyswmm + apply the mass-balance gate.

    Tracks the PEAK-volume depth grid (the meaningful wet state) and returns a
    :class:`RunResult`. Raises :class:`SWMMMeshError("SWMM_MASS_BALANCE_EXCEEDED")`
    if the Flow Routing Continuity error exceeds the tolerance — the honesty gate
    that turns a silently-wrong layer into a typed failure.
    """
    import time

    try:
        from pyswmm import Simulation, Nodes
    except Exception as exc:
        raise SWMMMeshError(
            "SWMM_DEPENDENCY_MISSING",
            message=f"pyswmm unavailable for run: {exc}",
        ) from exc

    nrows, ncols = build.grid_shape
    inp_path = build.inp_path
    rpt_path = str(Path(inp_path).with_suffix(".rpt"))
    out_path = str(Path(inp_path).with_suffix(".out"))

    # Active-cell coordinate list (storage node names exist only for these).
    active_cells = _active_cells_from_deck(build)

    peak_grid = np.full((nrows, ncols), np.nan)
    peak_sum = -1.0
    n_steps = 0
    last_dt = None
    t0 = time.time()
    try:
        with Simulation(inp_path) as sim:
            node_objs = Nodes(sim)
            prev = None
            k = 0
            for _ in sim:
                n_steps += 1
                k += 1
                now = sim.current_time
                if prev is not None:
                    last_dt = (now - prev).total_seconds()
                prev = now
                if k % sample_every_steps == 0:
                    g = np.full((nrows, ncols), np.nan)
                    s = 0.0
                    for (i, j) in active_cells:
                        d = float(node_objs[_cell_node(i, j)].depth)
                        g[i, j] = d
                        s += d
                    if s > peak_sum:
                        peak_sum = s
                        peak_grid = g
            # final snapshot if we never sampled (very short run)
            if peak_sum < 0:
                g = np.full((nrows, ncols), np.nan)
                for (i, j) in active_cells:
                    g[i, j] = float(node_objs[_cell_node(i, j)].depth)
                peak_grid = g
    except SWMMMeshError:
        raise
    except Exception as exc:
        raise SWMMMeshError(
            "SWMM_RUN_FAILED",
            message=f"pyswmm raised during the headless solve: {exc}",
            details={"inp_path": inp_path},
        ) from exc
    wall = time.time() - t0

    cont = read_flow_routing_continuity(rpt_path)
    if cont is None:
        raise SWMMMeshError(
            "SWMM_CONTINUITY_UNREADABLE",
            message="no Flow Routing Continuity error line in the .rpt",
            details={"rpt_path": rpt_path},
        )
    if abs(cont) > float(mass_balance_tolerance_pct):
        raise SWMMMeshError(
            "SWMM_MASS_BALANCE_EXCEEDED",
            message=(
                f"Flow Routing Continuity error {cont:+.3f}% exceeds tolerance "
                f"{mass_balance_tolerance_pct:.1f}% — refusing to publish a "
                f"silently-wrong depth layer"
            ),
            details={
                "continuity_error_pct": cont,
                "tolerance_pct": mass_balance_tolerance_pct,
                "rpt_path": rpt_path,
            },
        )

    finite = np.isfinite(peak_grid)
    max_depth = float(np.nanmax(peak_grid)) if finite.any() else 0.0
    n_wet = int((np.nan_to_num(peak_grid, nan=0.0) >= wet_threshold_m).sum())

    return RunResult(
        rpt_path=rpt_path,
        out_path=out_path,
        continuity_error_pct=cont,
        n_steps=n_steps,
        last_dt_s=last_dt,
        wall_seconds=wall,
        peak_depth_grid=peak_grid,
        max_depth_m=max_depth,
        n_wet_cells=n_wet,
    )


def _active_cells_from_deck(build: BuildResult) -> list[tuple[int, int]]:
    """Recover the active-cell (row, col) list by re-reading the deck's STORAGE.

    The storage node names encode the cell index (``S_<i>_<j>``); recovering them
    from the written deck keeps the run loop independent of the build's in-memory
    state (and matches what a worker re-loading a staged deck would do).
    """
    from swmm_api import SwmmInput
    from swmm_api.input_file.section_labels import STORAGE

    cells: list[tuple[int, int]] = []
    inp = SwmmInput.read_file(build.inp_path)
    # Index (not .get) so swmm-api lazily parses the section into an InpSection
    # of objects; a missing section indexes to an empty InpSection.
    storages = inp[STORAGE]
    for name in storages:
        if not isinstance(name, str) or not name.startswith("S_"):
            continue
        parts = name.split("_")
        if len(parts) == 3:
            try:
                cells.append((int(parts[1]), int(parts[2])))
            except ValueError:
                continue
    return cells
