"""Internal TELEMAC diagnostics parser for ``read_run_diagnostics`` (NOT registered).

TELEMAC's run classifier already folds ``telemac_metrics.json`` into
``completion.json`` (``correct_end`` / ``npoin`` / ``nelem`` / ``nptfr`` /
``wall_s``), so this parser READS those folded extras rather than re-parsing
(build-contract 3.1: prefer the completion.json extras). ``correct_end`` drives
``healthy`` (a TELEMAC run that did not reach CORRECT END OF RUN is unhealthy).
When ``full_listing.log`` carries a mass-balance line it is parsed into the
top-level ``mass_balance_pct`` (``"reported"``); where the listing file is absent
the folded ``listing_tail`` excerpt is read in its place, so a run that died
before its listing was uploaded still narrates from the solver's own words.
Otherwise ``null`` -- a failed run whose listing crashed before any balance line
reports ``null``, never a fabricated value (honesty floor).

ASCII only.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ._common import (
    EngineDiagnostics,
    RunArtifacts,
)

__all__ = ["parse_telemac"]

#: TELEMAC prints the relative mass-balance error as a fraction; these are the
#: listing phrasings across TELEMAC-2D versions. The value is a FRACTION
#: (e.g. ``0.12E-03``) which is converted to a percent (x100). Tested against a
#: synthesized listing only -- the sole MinIO TELEMAC run is a crashed one whose
#: listing ends before any balance line (build-contract open issue).
_MASS_BALANCE_RE = re.compile(
    r"RELATIVE ERROR IN (?:MASS[- ]BALANCE|VOLUME)[^:\n]*:\s*"
    r"(-?\d+(?:\.\d+)?(?:[dDeE][+-]?\d+)?)"
)


def _fortran_float(text: str) -> float:
    return float(text.replace("D", "E").replace("d", "e"))


def _listing_mass_balance_pct(text: str) -> float | None:
    """Max abs RELATIVE-ERROR mass-balance value in the listing, as a percent."""
    vals = [abs(_fortran_float(x)) for x in _MASS_BALANCE_RE.findall(text)]
    if not vals:
        return None
    return round(max(vals) * 100.0, 6)


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
            "the listing carried no RELATIVE-ERROR mass-balance line "
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
