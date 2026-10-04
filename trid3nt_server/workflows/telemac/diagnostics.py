"""The TELEMAC diagnostics parser ``read_run_diagnostics`` dispatches to by engine name.

Reads what the run classifier already folded into ``completion.json`` rather than
re-parsing it; a balance the listing does not carry stays ``null``.
"""

from __future__ import annotations

import json
from typing import Any

from trid3nt_server.workflows.solver.diagnostics._common import (
    EngineDiagnostics,
    RunArtifacts,
)

from .modules.listing import continuity_rel_error

__all__ = ["parse_telemac"]

def _listing_mass_balance_pct(text: str) -> float | None:
    """The run's own volume closure off the listing, as an absolute percent."""
    closure = continuity_rel_error(text)
    return None if closure is None else round(abs(closure) * 100.0, 6)


def _completion_or_metrics(
    art: RunArtifacts, key: str, metrics: dict[str, Any] | None
) -> Any:
    """A field from completion.json extras, falling back to telemac_metrics.json."""
    val = art.completion.get(key)
    if val is None and metrics is not None:
        val = metrics.get(key)
    return val


def parse_telemac(art: RunArtifacts, status: str) -> EngineDiagnostics:
    """Parse TELEMAC diagnostics from folded completion extras + the listing."""
    notes: list[str] = []
    warnings: list[str] = []
    diagnostics_files: list[str] = []

    # Fallback metrics source (only read if a completion extra is missing).
    metrics: dict[str, Any] | None = None
    metrics_uri, metrics_bytes = art.read_output_optional(
        basename="telemac_metrics.json"
    )
    if metrics_bytes is not None:
        try:
            metrics = json.loads(metrics_bytes)
            if not isinstance(metrics, dict):
                metrics = None
        except Exception:  # noqa: BLE001 -- optional fallback; keep going honestly
            metrics = None

    correct_end = _completion_or_metrics(art, "correct_end", metrics)
    npoin = _completion_or_metrics(art, "npoin", metrics)
    nelem = _completion_or_metrics(art, "nelem", metrics)
    wall_s = _completion_or_metrics(art, "wall_s", metrics)

    # -- listing mass balance (optional). ---------------------------------- #
    listing_uri, listing_bytes = art.read_output_optional(basename="full_listing.log")
    listing_text: str | None = None
    if listing_bytes is not None:
        listing_text = listing_bytes.decode("utf-8", errors="replace")
        diagnostics_files.append(listing_uri)  # type: ignore[arg-type]
    else:
        tail = _completion_or_metrics(art, "listing_tail", metrics)
        listing_text = tail if isinstance(tail, str) and tail else None
        notes.append(
            "full_listing.log absent; read the folded listing_tail, which is the "
            "END of the listing only - a balance line above the excerpt is not "
            "in this reading."
            if listing_text is not None
            else "full_listing.log absent and no listing_tail folded; "
                 "mass_balance_pct left null."
        )
    listing_mass_balance_pct: float | None = (
        _listing_mass_balance_pct(listing_text) if listing_text is not None else None
    )
    if listing_text is not None and listing_mass_balance_pct is None:
        notes.append(
            "the listing carried no RELATIVE ERROR IN VOLUME line "
            "(a crashed / truncated listing); mass_balance_pct left null."
        )

    if metrics_uri is not None:
        diagnostics_files.append(metrics_uri)

    # -- warnings. --------------------------------------------------------- #
    if correct_end is False:
        warnings.append("TELEMAC did not reach CORRECT END OF RUN.")

    notes.append(
        "healthy heuristic: status==ok AND correct_end is True (a run that did "
        "not reach CORRECT END OF RUN is unhealthy)."
    )

    # -- healthy roll-up (keys off correct_end). --------------------------- #
    healthy: bool | None
    if correct_end is False or status != "ok":
        healthy = False
    elif correct_end is True:
        healthy = True
    else:
        healthy = None

    engine_specific: dict[str, Any] = {
        "correct_end": correct_end,
        "npoin": npoin,
        "nelem": nelem,
        "wall_s": wall_s,
        "listing_mass_balance_pct": listing_mass_balance_pct,
    }

    return EngineDiagnostics(
        mass_balance_pct=listing_mass_balance_pct,
        mass_balance_source="reported" if listing_mass_balance_pct is not None else None,
        instability=None,
        nonconverged_pct=None,
        dry_cells=None,
        healthy=healthy,
        warnings=warnings,
        engine_specific=engine_specific,
        notes=notes,
        diagnostics_files=diagnostics_files,
    )
