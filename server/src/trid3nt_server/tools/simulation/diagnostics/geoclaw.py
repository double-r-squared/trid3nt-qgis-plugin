"""Internal GeoClaw diagnostics parser for ``read_run_diagnostics`` (NOT registered).

GeoClaw's sanity signal is mass conservation: the ``geoclaw.stdout`` line
``Total mass at initial time:  <value>`` (the known diagnostic gold -- ~1e5
means no wave was generated, 1e9+ means a real wave). When the run also reports
a final-time total mass, ``mass_ratio = final/initial`` and the top-level
``mass_balance_pct = (mass_ratio - 1) * 100`` (``"derived"``); otherwise both
are ``null`` (honesty floor -- never invented). Gauge / frame counts come from
the run's ``output_uris``; ``Courant number ... is larger than input cfl_max``
lines are surfaced as instability warnings.

Confirmed against a real ``geoclaw.stdout`` fixture (build-contract 5.1).
ASCII only.
"""

from __future__ import annotations

import re
from typing import Any

from ._common import (
    DiagnosticsArtifactMissing,
    EngineDiagnostics,
    RunArtifacts,
)

__all__ = ["parse_geoclaw"]

# --- Named thresholds (source comments). --- #

#: A GeoClaw initial total mass below this ceiling suggests NO wave was
#: generated (the diagnostic-gold memory: ~1e5 = no wave, 1e9+ = real wave).
#: Sits an order of magnitude above the no-wave floor and well below a real
#: wave's mass.
GEOCLAW_NO_WAVE_MASS_CEILING: float = 1.0e7

#: |mass_ratio - 1| above this (5%) is flagged as a mass-conservation warning
#: when a final mass is reported.
GEOCLAW_MASS_RATIO_WARN: float = 0.05

_INITIAL_MASS_RE = re.compile(
    r"Total mass at initial time:\s*([-\d.]+(?:[dDeE][+-]?\d+)?)"
)
#: Any other total-mass report line (a per-frame / final-time mass, when the
#: run config prints one). The LAST such value is treated as the final mass.
_OTHER_MASS_RE = re.compile(
    r"Total mass at (?!initial time)[^\n:]*:\s*([-\d.]+(?:[dDeE][+-]?\d+)?)"
)
_COURANT_RE = re.compile(r"Courant number\s*=\s*[-\d.dDeE+]+\s*is larger than")


def _fortran_float(text: str) -> float:
    """Parse a Fortran float that may use a ``D`` exponent (``1.23D+04``)."""
    return float(text.replace("D", "E").replace("d", "e"))


def parse_geoclaw(art: RunArtifacts, status: str) -> EngineDiagnostics:
    """Parse GeoClaw stdout mass conservation + gauge/frame inventory."""
    stdout_uri, stdout_bytes = art.read_stdout_optional()
    if stdout_bytes is None:
        raise DiagnosticsArtifactMissing(
            "geoclaw",
            art.run_id,
            "geoclaw.stdout",
            "stdout carries the mass-conservation signal",
        )
    text = stdout_bytes.decode("utf-8", errors="replace")

    notes: list[str] = []
    warnings: list[str] = []

    m_init = _INITIAL_MASS_RE.search(text)
    mass_initial = _fortran_float(m_init.group(1)) if m_init else None

    other = _OTHER_MASS_RE.findall(text)
    mass_final = _fortran_float(other[-1]) if other else None

    mass_ratio: float | None = None
    mass_balance_pct: float | None = None
    mass_balance_source: str | None = None
    if mass_initial is not None and mass_final is not None and mass_initial != 0.0:
        mass_ratio = mass_final / mass_initial
        mass_balance_pct = round((mass_ratio - 1.0) * 100.0, 6)
        mass_balance_source = "derived"

    n_gauges = art.count_outputs(contains="gauge")
    n_frames = art.count_outputs(contains="fort.q")
    if n_frames == 0:
        # Fall back to the stdout frame markers when fort.q were not retained.
        n_frames = len(re.findall(r"output files done at time", text))

    n_courant = len(_COURANT_RE.findall(text))

    # -- warnings. --------------------------------------------------------- #
    if mass_initial is not None and mass_initial < GEOCLAW_NO_WAVE_MASS_CEILING:
        warnings.append(
            f"Initial total mass {mass_initial:.4g} is below "
            f"{GEOCLAW_NO_WAVE_MASS_CEILING:.0e} -- the wave may not have been "
            "generated (GeoClaw no-wave signal)."
        )
    if mass_ratio is not None and abs(mass_ratio - 1.0) > GEOCLAW_MASS_RATIO_WARN:
        warnings.append(
            f"Mass ratio final/initial = {mass_ratio:.4f} deviates from 1.0 by "
            f"more than {GEOCLAW_MASS_RATIO_WARN:.0%} -- mass not conserved."
        )
    if n_courant:
        warnings.append(
            f"{n_courant} Courant-number-exceeded warning(s) (CFL > cfl_max) -- "
            "adaptive stepping stressed; check stability."
        )

    if mass_final is None:
        notes.append(
            "GeoClaw stdout reported only an initial total mass (no final-time "
            "mass); mass_ratio / mass_balance_pct left null (honesty floor)."
        )
    notes.append(
        "healthy heuristic: status==ok AND initial mass above the no-wave "
        "ceiling AND (mass ratio within band OR no final mass reported)."
    )

    # -- healthy roll-up. -------------------------------------------------- #
    healthy: bool | None
    low_mass = (
        mass_initial is not None and mass_initial < GEOCLAW_NO_WAVE_MASS_CEILING
    )
    bad_ratio = (
        mass_ratio is not None and abs(mass_ratio - 1.0) > GEOCLAW_MASS_RATIO_WARN
    )
    if status != "ok":
        healthy = False
    elif mass_initial is None:
        healthy = None
    elif low_mass or bad_ratio:
        healthy = False
    else:
        healthy = True

    engine_specific: dict[str, Any] = {
        "mass_initial": mass_initial,
        "mass_final": mass_final,
        "mass_ratio": round(mass_ratio, 6) if mass_ratio is not None else None,
        "n_gauges": n_gauges,
        "n_frames": n_frames,
    }

    return EngineDiagnostics(
        mass_balance_pct=mass_balance_pct,
        mass_balance_source=mass_balance_source,
        instability=n_courant if n_courant else None,
        nonconverged_pct=None,
        dry_cells=None,
        healthy=healthy,
        warnings=warnings,
        engine_specific=engine_specific,
        notes=notes,
        diagnostics_files=[stdout_uri],  # type: ignore[list-item]
    )
