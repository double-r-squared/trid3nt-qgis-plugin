"""The MODFLOW diagnostics parser ``read_run_diagnostics`` dispatches to.

Reads what the run classifier already folded into ``completion.json`` rather
than re-parsing it; a balance the listing does not carry stays ``null``.
"""

from __future__ import annotations

import json
import re
from typing import Any

from trid3nt_server.workflows.solver.diagnostics._common import (
    EngineDiagnostics,
    RunArtifacts,
)

__all__ = ["parse_modflow", "convergence_failure"]


def _completion_or_metrics(
    art: RunArtifacts, key: str, metrics: dict[str, Any] | None
) -> Any:
    """A field from completion.json extras, falling back to modflow_metrics.json."""
    val = art.completion.get(key)
    if val is None and metrics is not None:
        val = metrics.get(key)
    return val


_NOT_CONVERGED = re.compile(
    r"Solution (\d+) did not converge for stress period (\d+) and time step (\d+)")
_PACKAGE_ROW = re.compile(
    r"^\s*Package\s+(\d+)\s+(\S+)\s+\d+_\w+-(\S+?)-\((\d+)\)-(\w+)\s*$", re.M)


_ITEM = {"LAK": "lake", "SFR": "reach", "MAW": "well"}


def convergence_failure(listing: str) -> str | None:
    """The sentence mfsim.lst's own first non-converged solution supports, or ``None`` where it holds none.

    Names the stress period, time step, the period's first step length and the packages the solution's
    last outer iteration still reports (mf6 prints them only under a SUMMARY or ALL IMS print option)."""
    hit = _NOT_CONVERGED.search(listing)
    if hit is None:
        return None
    sln, period, step = hit.groups()
    head = listing[:hit.start()]
    parts = [f"solution {sln} did not converge in stress period {period}, time step {step}"]
    opening = re.search(rf"STRESS PERIOD NO\.\s+{period},[^\n]*\n(?:[^\n]*\n){{0,3}}?"
                        r"\s*INITIAL TIME STEP SIZE =\s+(\S+)", head)
    if opening:
        unit = re.search(r"SIMULATION TIME UNIT IS (\w+)", head)
        parts.append(f"the period opens with a step of {opening.group(1)}"
                     + (f" {unit.group(1).lower()}" if unit and unit.group(1) != "UNDEFINED" else ""))
    closure = re.findall(r"OUTER ITERATION CONVERGENCE CRITERION\s+\(DVCLOSE\) =\s+(\S+)", head)
    if len(closure) >= int(sln):
        parts.append(f"outer DVCLOSE {closure[int(sln) - 1]}")
    table = head.rfind(f"SLN_{sln} OUTER ITERATION SUMMARY")
    rows = list(_PACKAGE_ROW.finditer(listing[table:hit.start()])) if table >= 0 else []
    if not rows:
        return "; ".join(parts) + (
            "; the listing's outer iteration table names no package" if table >= 0
            else "; the listing carries no outer iteration table (IMS PRINT_OPTION)")
    last = max(int(r.group(1)) for r in rows)
    held = [f"{r.group(3)} {_ITEM.get(r.group(3).split('_')[0], 'number')} {r.group(4)} ({r.group(5)} {r.group(2)})"
            for r in rows
            if int(r.group(1)) == last]
    return "; ".join(parts) + f"; at its last outer iteration these packages had not closed: {', '.join(held)}"


def parse_modflow(art: RunArtifacts, status: str) -> EngineDiagnostics:
    """Parse MODFLOW diagnostics from folded completion extras + the metrics."""
    notes: list[str] = []
    warnings: list[str] = []
    diagnostics_files: list[str] = []

    metrics: dict[str, Any] | None = None
    metrics_uri, metrics_bytes = art.read_output_optional(
        basename="modflow_metrics.json"
    )
    if metrics_bytes is not None:
        try:
            metrics = json.loads(metrics_bytes.decode("utf-8"))
            if not isinstance(metrics, dict):
                metrics = None
        except Exception:  # noqa: BLE001 -- optional fallback; keep going honestly
            metrics = None
    if metrics_uri is not None:
        diagnostics_files.append(metrics_uri)

    correct_end = _completion_or_metrics(art, "correct_end", metrics)
    wall_s = _completion_or_metrics(art, "wall_s", metrics)
    module = _completion_or_metrics(art, "module", metrics)
    mode = _completion_or_metrics(art, "mode", metrics)
    normal = _completion_or_metrics(art, "normal_termination", metrics)
    if normal is False:
        notes.append("mf6's listing never printed its normal-termination "
                     "sentence, so a clean exit was not a finished solve")
    if correct_end is False:
        warnings.append("MODFLOW did not reach a correct end.")
        error = _completion_or_metrics(art, "error", metrics)
        if error:
            notes.append(str(error))

    healthy: bool | None
    if correct_end is False or status != "ok":
        healthy = False
    elif correct_end is True:
        healthy = True
    else:
        healthy = None
    notes.append(
        "healthy heuristic: status==ok AND correct_end is True (a run whose "
        "mf6 never printed normal termination is unhealthy)."
    )

    return EngineDiagnostics(
        healthy=healthy,
        warnings=warnings,
        engine_specific={
            "correct_end": correct_end,
            "normal_termination": normal,
            "module": module,
            "mode": mode,
            "wall_s": wall_s,
        },
        notes=notes,
        diagnostics_files=diagnostics_files,
    )
