"""The GeoClaw diagnostics parser ``read_run_diagnostics`` dispatches to.

Reads what the run classifier already folded into ``completion.json`` rather
than re-parsing it; a balance the run does not carry stays ``null``.
"""

from __future__ import annotations

import json
from typing import Any

from trid3nt_server.workflows.solver.diagnostics._common import (
    EngineDiagnostics,
    RunArtifacts,
)

__all__ = ["parse_geoclaw"]


def _completion_or_metrics(
    art: RunArtifacts, key: str, metrics: dict[str, Any] | None
) -> Any:
    """A field from completion.json extras, falling back to geoclaw_metrics.json."""
    val = art.completion.get(key)
    if val is None and metrics is not None:
        val = metrics.get(key)
    return val


def parse_geoclaw(art: RunArtifacts, status: str) -> EngineDiagnostics:
    """Parse GeoClaw diagnostics from folded completion extras + the metrics."""
    notes: list[str] = []
    warnings: list[str] = []
    diagnostics_files: list[str] = []

    metrics: dict[str, Any] | None = None
    metrics_uri, metrics_bytes = art.read_output_optional(
        basename="geoclaw_metrics.json"
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
    initial_mass = _completion_or_metrics(art, "initial_mass", metrics)
    final_time = _completion_or_metrics(art, "final_time", metrics)
    tfinal = _completion_or_metrics(art, "tfinal", metrics)
    frames = _completion_or_metrics(art, "frames", metrics)
    engine_exit = _completion_or_metrics(art, "engine_exit_code", metrics)
    if correct_end is False:
        warnings.append("GeoClaw did not reach a correct end.")
        error = _completion_or_metrics(art, "error", metrics)
        if error:
            notes.append(str(error))
        if engine_exit == 0:
            notes.append(
                "the engine exited 0 anyway: GeoClaw's plain-Fortran stop "
                "still returns 0, so the verdict rests on its own sentences")

    healthy: bool | None
    if correct_end is False or status != "ok":
        healthy = False
    elif correct_end is True:
        healthy = True
    else:
        healthy = None
    notes.append(
        "healthy heuristic: status==ok AND correct_end is True (a run whose "
        "stdout never reached the Done-integrating line is unhealthy)."
    )

    return EngineDiagnostics(
        healthy=healthy,
        warnings=warnings,
        engine_specific={
            "correct_end": correct_end,
            "initial_mass": initial_mass,
            "final_time": final_time,
            "tfinal": tfinal,
            "frames": frames,
            "engine_exit_code": engine_exit,
            "wall_s": wall_s,
        },
        notes=notes,
        diagnostics_files=diagnostics_files,
    )
