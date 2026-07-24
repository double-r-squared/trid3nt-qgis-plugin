"""Internal MODFLOW 6 diagnostics parser for ``read_run_diagnostics`` (NOT registered).

Reads the MF6 listing files retained in the run prefix:

- ``mfsim.lst`` -- simulation-level convergence + timing. Convergence uses the
  SAME markers the run classifier trusts (``FAILED TO MEET SOLVER CONVERGENCE
  CRITERIA`` / ``Normal termination of simulation``, reused from
  ``workflows.run_modflow``), and total time steps come from the per-step
  ``Solving:  Stress period ... Time step ...`` lines.
- the per-model ``<model>.lst`` (e.g. ``gwf_model.lst``) -- the ``VOLUME BUDGET
  FOR ENTIRE MODEL`` blocks whose ``PERCENT DISCREPANCY = ...`` line gives the
  volumetric budget error (max abs over stress periods/time steps -> the
  envelope's top-level ``mass_balance_pct``, ``"reported"``), plus any
  dry-cell notices.

The ``PERCENT DISCREPANCY`` and ``VOLUME BUDGET`` line formats were CONFIRMED
against a real ``gwf_model.lst`` fixture (build-contract 3.1 / 5.1). ASCII only.
"""

from __future__ import annotations

import re
from typing import Any

from ._common import (
    DiagnosticsArtifactMissing,
    DiagnosticsParseError,
    EngineDiagnostics,
    RunArtifacts,
    basename_of,
)

__all__ = ["parse_modflow"]

# --- Healthy / warning thresholds (named constants with source comments). --- #

#: MF6 volumetric budget PERCENT DISCREPANCY within this magnitude reads
#: "healthy". A well-posed MF6 run closes its volume budget far tighter than
#: 1%; the MF6 manual treats >1% as a mass-balance problem (build-contract 3.1
#: MODFLOW band).
MODFLOW_DISCREPANCY_HEALTHY_PCT: float = 1.0

#: A budget discrepancy above this magnitude is surfaced as a warning.
MODFLOW_DISCREPANCY_WARN_PCT: float = 1.0

#: Convergence markers -- IMPORTED lazily from ``workflows.run_modflow`` so the
#: reader classifies a run the same way the run tool does (single source of
#: truth; do not re-hardcode the strings here).

_PERCENT_DISCREPANCY_RE = re.compile(
    r"PERCENT DISCREPANCY\s*=\s*(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)"
)
_SOLVING_STEP_RE = re.compile(r"Solving:\s*Stress period:", re.IGNORECASE)
#: Conservative dry-cell notice matcher. MF6 does NOT emit a per-cell dry
#: notice in the listing by default, so a match count of 0 means "no explicit
#: dry-cell warnings in the listing" -- NOT a guarantee no cell went dry. The
#: exact dry-cell line text could not be confirmed against a real dry-cell run
#: (no such fixture; mf6io doc 403-blocked per research.md), so this matches the
#: documented MODFLOW-lineage phrasing and is unit-tested against a synthetic
#: line only.
_DRY_CELL_RE = re.compile(r"(CELL[^\n]*\bDRY\b|\bDRY\b[^\n]*CELL)", re.IGNORECASE)


def _convergence_markers() -> tuple[str, str]:
    """(failure_marker, normal_termination_marker) from ``workflows.run_modflow``."""
    from trid3nt_server.workflows.run_modflow import (
        CONVERGENCE_FAILURE_MARKER,
        NORMAL_TERMINATION_MARKER,
    )

    return CONVERGENCE_FAILURE_MARKER, NORMAL_TERMINATION_MARKER


def _max_abs_discrepancy(text: str) -> float | None:
    """Max absolute PERCENT DISCREPANCY across all budget blocks in a listing."""
    vals = [abs(float(x)) for x in _PERCENT_DISCREPANCY_RE.findall(text)]
    return max(vals) if vals else None


def parse_modflow(art: RunArtifacts, status: str) -> EngineDiagnostics:
    """Parse MF6 ``mfsim.lst`` + the per-model ``<model>.lst`` listings."""
    notes: list[str] = []
    warnings: list[str] = []
    diagnostics_files: list[str] = []

    # -- mfsim.lst: convergence + time-step count. ------------------------- #
    sim_uri, sim_bytes = art.read_output_optional(basename="mfsim.lst")
    if sim_bytes is None:
        sim_uri, sim_bytes = art.read_output_optional(suffix="mfsim.lst")
    fail_marker, normal_marker = _convergence_markers()
    nonconverged_steps: int | None = None
    total_steps: int | None = None
    normal_termination: bool | None = None
    if sim_bytes is not None:
        sim_text = sim_bytes.decode("utf-8", errors="replace")
        diagnostics_files.append(sim_uri)  # type: ignore[arg-type]
        nonconverged_steps = sim_text.count(fail_marker)
        total_steps = len(_SOLVING_STEP_RE.findall(sim_text))
        normal_termination = normal_marker in sim_text
    else:
        notes.append("mfsim.lst absent; convergence inferred from completion.json only.")

    # -- per-model <model>.lst: budget discrepancy + dry cells. ------------ #
    model_lsts = [
        uri
        for uri in art.output_uris
        if basename_of(uri).endswith(".lst") and basename_of(uri) != "mfsim.lst"
    ]
    if not model_lsts:
        raise DiagnosticsArtifactMissing(
            "modflow",
            art.run_id,
            "<model>.lst",
            "no per-model MF6 listing in output_uris",
        )

    per_model: list[dict[str, Any]] = []
    dry_cells_total = 0
    saw_dry = False
    for uri in model_lsts:
        data = art.read_uri(uri)
        text = data.decode("utf-8", errors="replace")
        if "PERCENT DISCREPANCY" not in text and "VOLUME BUDGET" not in text:
            raise DiagnosticsParseError(
                "modflow",
                art.run_id,
                uri,
                "no VOLUME BUDGET / PERCENT DISCREPANCY block found",
            )
        disc = _max_abs_discrepancy(text)
        per_model.append(
            {"model": basename_of(uri), "percent_discrepancy_pct": disc}
        )
        n_dry = len(_DRY_CELL_RE.findall(text))
        if n_dry:
            saw_dry = True
            dry_cells_total += n_dry
        diagnostics_files.append(uri)

    disc_vals = [
        pm["percent_discrepancy_pct"]
        for pm in per_model
        if pm["percent_discrepancy_pct"] is not None
    ]
    percent_discrepancy_pct = max(disc_vals) if disc_vals else None
    # 0 explicit dry-cell notices found -> report 0 (not null): the listing WAS
    # read and carried none. saw_dry documents whether any matched at all.
    dry_cells = dry_cells_total

    # -- converged: completion.json field wins; else derive from mfsim.lst. - #
    converged: bool | None = art.completion.get("converged")
    if converged is None:
        if normal_termination is None:
            converged = None
        else:
            converged = bool(normal_termination) and (nonconverged_steps or 0) == 0

    nonconverged_pct: float | None
    if nonconverged_steps is None:
        nonconverged_pct = None
    elif total_steps:
        nonconverged_pct = round(100.0 * nonconverged_steps / total_steps, 4)
    else:
        nonconverged_pct = 0.0 if nonconverged_steps == 0 else None

    # -- warnings. --------------------------------------------------------- #
    if (
        percent_discrepancy_pct is not None
        and percent_discrepancy_pct > MODFLOW_DISCREPANCY_WARN_PCT
    ):
        warnings.append(
            f"MF6 volume-budget discrepancy {percent_discrepancy_pct:.4g}% "
            f"exceeds {MODFLOW_DISCREPANCY_WARN_PCT:.1f}% -- mass balance out of band."
        )
    if nonconverged_steps:
        warnings.append(
            f"{nonconverged_steps} time step(s) failed to meet solver convergence "
            "criteria."
        )
    if converged is False:
        warnings.append("MF6 run did not reach normal termination / convergence.")
    if saw_dry and dry_cells:
        warnings.append(f"{dry_cells} dry-cell notice(s) in the model listing.")

    notes.append(
        "healthy heuristic: status==ok AND converged AND abs(percent "
        f"discrepancy) <= {MODFLOW_DISCREPANCY_HEALTHY_PCT:.1f}%."
    )
    notes.append(
        "dry_cells counts explicit dry-cell notices in the model listing; MF6 "
        "does not emit per-cell dry notices by default, so 0 means 'none in the "
        "listing', not a guarantee no cell went dry."
    )

    # -- healthy roll-up. -------------------------------------------------- #
    healthy: bool | None
    if status != "ok":
        healthy = False
    elif converged is False:
        healthy = False
    elif percent_discrepancy_pct is None or converged is None:
        healthy = None
    else:
        healthy = (
            converged is True
            and percent_discrepancy_pct <= MODFLOW_DISCREPANCY_HEALTHY_PCT
        )

    engine_specific: dict[str, Any] = {
        "percent_discrepancy_pct": percent_discrepancy_pct,
        "converged": converged,
        "nonconverged_steps": nonconverged_steps,
        "dry_cells": dry_cells,
        "per_model": per_model,
    }

    return EngineDiagnostics(
        mass_balance_pct=percent_discrepancy_pct,
        mass_balance_source="reported" if percent_discrepancy_pct is not None else None,
        instability=None,
        nonconverged_pct=nonconverged_pct,
        dry_cells=dry_cells,
        healthy=healthy,
        warnings=warnings,
        engine_specific=engine_specific,
        notes=notes,
        diagnostics_files=diagnostics_files,
    )
