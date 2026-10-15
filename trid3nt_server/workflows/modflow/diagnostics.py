"""The MODFLOW diagnostics parser ``read_run_diagnostics`` dispatches to.

Reads what the run classifier already folded into ``completion.json`` rather
than re-parsing it; a balance the listing does not carry stays ``null``.
"""

from __future__ import annotations

import json
from typing import Any

from trid3nt_server.workflows.solver.diagnostics._common import (
    EngineDiagnostics,
    RunArtifacts,
)

__all__ = ["parse_modflow"]


def _completion_or_metrics(
    art: RunArtifacts, key: str, metrics: dict[str, Any] | None
) -> Any:
    """A field from completion.json extras, falling back to modflow_metrics.json."""
    val = art.completion.get(key)
    if val is None and metrics is not None:
        val = metrics.get(key)
    return val


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
