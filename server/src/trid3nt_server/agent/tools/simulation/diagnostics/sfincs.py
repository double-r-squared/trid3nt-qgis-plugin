"""Internal SFINCS diagnostics parser for ``read_run_diagnostics`` (NOT registered).

SFINCS has NO explicit continuity field, so the top-level ``mass_balance_pct``
is DERIVED (``mass_balance_source="derived"``) from the cumulative water budget
in ``sfincs_map.nc``:

    residual = cumprcp - cuminf - storage_delta - boundary_flux
    mass_balance_pct = residual / cumprcp * 100

Each term is summed over active (``msk``) cells. When the map output carries no
``cumprcp`` / ``cuminf`` (a run without those cumulative maps -- the common
case), ``mass_balance_pct`` is ``null`` (honesty floor -- never invented), NOT a
fabricated zero. ``boundary_flux`` is not written to the map file, so the derived
residual attributes net boundary flux + numerical error together (for a closed
rainfall-on-grid domain that residual IS the numerical mass-balance error) --
stated in ``notes``. Max water depth (instability sanity), average timestep,
and runtime come from ``sfincs.stdout`` / the map file.

netCDF variable names verified against a real ``sfincs_map.nc`` (build-contract
5.1). ASCII only.
"""

from __future__ import annotations

import re
from typing import Any

from ._common import (
    DiagnosticsArtifactMissing,
    DiagnosticsParseError,
    EngineDiagnostics,
    RunArtifacts,
)

__all__ = ["parse_sfincs"]

# --- Named thresholds (source comments). --- #

#: A derived mass-balance residual above this magnitude (percent of cumulative
#: precip) is surfaced as a warning. Wider than the SWMM/MODFLOW band because
#: the SFINCS value is a DERIVED residual (boundary flux is not isolated).
SFINCS_MB_WARN_PCT: float = 5.0

#: Overland water depth above this (metres) is physically implausible for most
#: rainfall/flood domains and is flagged (coastal surge can legitimately exceed
#: it -- a warning, not a gate).
SFINCS_IMPLAUSIBLE_DEPTH_M: float = 50.0

#: SFINCS fill / nodata sentinel guard: mask any |value| above this before a
#: max/sum so a fill value never poisons the statistic.
_FILL_GUARD: float = 1.0e10

#: Candidate netCDF variable names (SFINCS builds vary the exact spelling).
_CUMPRCP_NAMES = ("cumprcp", "cumulative_precipitation", "cumprecip")
_CUMINF_NAMES = ("cuminf", "cumulative_infiltration")
_DEPTH_NAMES = ("h", "water_depth")
_MAXDEPTH_NAMES = ("hmax", "zsmax")
_MASK_NAMES = ("msk", "mask")

_AVG_DT_RE = re.compile(r"Average time step \(s\)\s*:\s*(-?\d+(?:\.\d+)?)")
_TOTAL_TIME_RE = re.compile(r"Total time\s*:\s*(-?\d+(?:\.\d+)?)")
_FINISHED_RE = re.compile(r"Simulation finished")


def _first_var(ds: Any, names: tuple[str, ...]) -> Any | None:
    for n in names:
        if n in ds.variables:
            return ds.variables[n]
    return None


def _active_sum(arr: Any, mask: Any) -> float | None:
    """Sum a 2D map over active (mask==1) cells, guarding fill/nodata."""
    import numpy as np

    a = np.asarray(arr, dtype=np.float64)
    a = np.where(np.abs(a) > _FILL_GUARD, np.nan, a)
    if mask is not None:
        m = np.asarray(mask)
        a = np.where(m == 1, a, np.nan)
    if not np.isfinite(a).any():
        return None
    return float(np.nansum(a))


def _cell_area_m2(ds: Any) -> float | None:
    """Uniform cell area from the x/y face-coordinate spacing (metres)."""
    import numpy as np

    xv = _first_var(ds, ("x",))
    yv = _first_var(ds, ("y",))
    if xv is None or yv is None:
        return None
    try:
        x = np.asarray(xv[:], dtype=np.float64)
        y = np.asarray(yv[:], dtype=np.float64)
        if x.ndim != 2 or y.ndim != 2 or x.shape[1] < 2 or y.shape[0] < 2:
            return None
        dx = abs(float(x[0, 1] - x[0, 0]))
        dy = abs(float(y[1, 0] - y[0, 0]))
        if dx <= 0 or dy <= 0:
            return None
        return dx * dy
    except Exception:  # noqa: BLE001
        return None


def parse_sfincs(art: RunArtifacts, status: str) -> EngineDiagnostics:
    """Parse SFINCS map netCDF (derived budget + depth) + stdout timing."""
    try:
        import numpy as np  # noqa: F401
        import netCDF4  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise DiagnosticsParseError(
            "sfincs", art.run_id, "sfincs_map.nc",
            f"netCDF4/numpy unavailable: {exc}",
        ) from exc

    map_uri, map_bytes = art.read_output_optional(suffix="sfincs_map.nc")
    if map_bytes is None:
        map_uri, map_bytes = art.read_output_optional(suffix="_map.nc")
    if map_bytes is None:
        raise DiagnosticsArtifactMissing(
            "sfincs", art.run_id, "sfincs_map.nc",
            "the map netCDF carries depth + the cumulative water budget",
        )

    notes: list[str] = []
    warnings: list[str] = []

    try:
        ds = netCDF4.Dataset("inmemory.nc", mode="r", memory=map_bytes)
    except Exception as exc:  # noqa: BLE001
        raise DiagnosticsParseError(
            "sfincs", art.run_id, map_uri or "sfincs_map.nc",
            f"could not open map netCDF: {exc}",
        ) from exc

    try:
        mask_v = _first_var(ds, _MASK_NAMES)
        mask = mask_v[:] if mask_v is not None else None
        cell_area = _cell_area_m2(ds)

        # -- derived mass balance. ------------------------------------------ #
        cumprcp_v = _first_var(ds, _CUMPRCP_NAMES)
        cuminf_v = _first_var(ds, _CUMINF_NAMES)
        sum_cumprcp = _active_sum(cumprcp_v[:], mask) if cumprcp_v is not None else None
        sum_cuminf = _active_sum(cuminf_v[:], mask) if cuminf_v is not None else None

        # storage delta from depth (last - first time slice).
        depth_v = _first_var(ds, _DEPTH_NAMES)
        sum_storage_delta: float | None = None
        if depth_v is not None and depth_v.ndim == 3 and depth_v.shape[0] >= 2:
            import numpy as np

            first = np.asarray(depth_v[0], dtype=np.float64)
            last = np.asarray(depth_v[-1], dtype=np.float64)
            sum_storage_delta = _active_sum(last - first, mask)

        boundary_flux_m3 = None  # not written to the SFINCS map file

        mass_balance_pct: float | None = None
        mass_balance_source: str | None = None
        if sum_cumprcp is not None and sum_cumprcp != 0.0:
            inf = sum_cuminf or 0.0
            storage = sum_storage_delta or 0.0
            residual = sum_cumprcp - inf - storage
            mass_balance_pct = round(residual / sum_cumprcp * 100.0, 6)
            mass_balance_source = "derived"
            notes.append(
                "mass_balance_pct DERIVED: (cumprcp - cuminf - storage_delta) / "
                "cumprcp * 100 over active cells. Boundary flux is not written to "
                "the map file, so this residual attributes net boundary flux + "
                "numerical error together (for a closed rainfall-on-grid domain it "
                "IS the numerical mass-balance error)."
            )
        else:
            notes.append(
                "SFINCS map output carried no cumulative precipitation "
                "(cumprcp); mass_balance_pct left null (not derivable, honesty "
                "floor)."
            )

        cumprcp_m3 = (
            round(sum_cumprcp * cell_area, 4)
            if sum_cumprcp is not None and cell_area is not None
            else None
        )
        cuminf_m3 = (
            round(sum_cuminf * cell_area, 4)
            if sum_cuminf is not None and cell_area is not None
            else None
        )
        storage_delta_m3 = (
            round(sum_storage_delta * cell_area, 4)
            if sum_storage_delta is not None and cell_area is not None
            else None
        )
        if cell_area is None and sum_cumprcp is not None:
            notes.append(
                "cell area not derivable from x/y coords; *_m3 volumes left null "
                "(the derived percent is area-invariant and unaffected)."
            )

        # -- max water depth. ----------------------------------------------- #
        maxdepth_v = _first_var(ds, _MAXDEPTH_NAMES)
        max_water_depth_m: float | None = None
        depth_source = maxdepth_v if maxdepth_v is not None else depth_v
        if depth_source is not None:
            import numpy as np

            a = np.asarray(depth_source[:], dtype=np.float64)
            a = np.where(np.abs(a) > _FILL_GUARD, np.nan, a)
            if np.isfinite(a).any():
                max_water_depth_m = round(float(np.nanmax(a)), 4)

        # -- netCDF status var (0 = no error). ------------------------------ #
        status_v = _first_var(ds, ("status",))
        nc_status: int | None = None
        if status_v is not None:
            try:
                import numpy as np

                nc_status = int(np.asarray(status_v[:]).ravel()[0])
            except Exception:  # noqa: BLE001
                nc_status = None
    finally:
        ds.close()

    # -- stdout: timing + finished marker. --------------------------------- #
    avg_timestep_s: float | None = None
    runtime_s: float | None = None
    finished: bool = False
    stdout_uri, stdout_bytes = art.read_stdout_optional()
    if stdout_bytes is not None:
        stdout_text = stdout_bytes.decode("utf-8", errors="replace")
        m = _AVG_DT_RE.search(stdout_text)
        if m:
            avg_timestep_s = float(m.group(1))
        m = _TOTAL_TIME_RE.search(stdout_text)
        if m:
            runtime_s = float(m.group(1))
        finished = bool(_FINISHED_RE.search(stdout_text))
    else:
        notes.append("sfincs.stdout absent; timing + finished marker unavailable.")

    # SFINCS stdout in this build carries no CFL-limiting breakdown.
    cfl_limiting_pct = None
    cfl_limiting_cell = None
    notes.append(
        "instability = cfl_limiting_pct; this SFINCS build's stdout carried no "
        "CFL-limiting breakdown, so it is null."
    )

    # -- warnings. --------------------------------------------------------- #
    if mass_balance_pct is not None and abs(mass_balance_pct) > SFINCS_MB_WARN_PCT:
        warnings.append(
            f"Derived mass-balance residual {mass_balance_pct:.3f}% exceeds "
            f"{SFINCS_MB_WARN_PCT:.1f}% -- review the water budget / boundaries."
        )
    if (
        max_water_depth_m is not None
        and max_water_depth_m > SFINCS_IMPLAUSIBLE_DEPTH_M
    ):
        warnings.append(
            f"Max water depth {max_water_depth_m:.2f} m exceeds "
            f"{SFINCS_IMPLAUSIBLE_DEPTH_M:.0f} m -- physically implausible "
            "overland; possible instability."
        )
    if nc_status not in (0, None):
        warnings.append(
            f"SFINCS netCDF status flag = {nc_status} (0 = no error)."
        )
    if stdout_bytes is not None and not finished:
        warnings.append(
            "SFINCS stdout has no 'Simulation finished' marker -- run may not "
            "have completed."
        )

    notes.append(
        "healthy heuristic: status==ok AND finished AND netCDF status in {0, "
        "unset} AND (derived mass balance null OR within "
        f"{SFINCS_MB_WARN_PCT:.1f}%)."
    )

    # -- healthy roll-up. -------------------------------------------------- #
    healthy: bool | None
    mb_ok = mass_balance_pct is None or abs(mass_balance_pct) <= SFINCS_MB_WARN_PCT
    if status != "ok":
        healthy = False
    elif stdout_bytes is None:
        healthy = None
    elif nc_status not in (0, None):
        healthy = False
    else:
        healthy = bool(finished) and mb_ok

    engine_specific: dict[str, Any] = {
        "max_water_depth_m": max_water_depth_m,
        "cfl_limiting_pct": cfl_limiting_pct,
        "cfl_limiting_cell": cfl_limiting_cell,
        "avg_timestep_s": avg_timestep_s,
        "runtime_s": runtime_s,
        "cumprcp_m3": cumprcp_m3,
        "cuminf_m3": cuminf_m3,
        "storage_delta_m3": storage_delta_m3,
        "boundary_flux_m3": boundary_flux_m3,
        "finished": finished,
    }

    diagnostics_files = [map_uri]  # type: ignore[list-item]
    if stdout_uri:
        diagnostics_files.append(stdout_uri)

    return EngineDiagnostics(
        mass_balance_pct=mass_balance_pct,
        mass_balance_source=mass_balance_source,
        instability=cfl_limiting_pct,
        nonconverged_pct=None,
        dry_cells=None,
        healthy=healthy,
        warnings=warnings,
        engine_specific=engine_specific,
        notes=notes,
        diagnostics_files=diagnostics_files,
    )
