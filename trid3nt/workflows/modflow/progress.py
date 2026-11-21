"""MODFLOW 6's own step, read off one stdout line.

The line is ``Solving:  Stress period: p    Time step: s``. The totals are the deck's TDIS statements: periods from NPER and the steps of
a period from its PERIODDATA row; an ATS-adapted run has no step total.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

__all__ = ["step_of"]

_SOLVING = re.compile(r"Solving:\s+Stress period:\s*(\d+)\s+Time step:\s*(\d+)")


def step_of(line: str, sheet: Mapping[str, Any]) -> dict[str, str] | None:
    """The step this stdout line states, or ``None`` for a line that states none."""
    hit = _SOLVING.search(line)
    if hit is None:
        return None
    period, step = int(hit.group(1)), int(hit.group(2))
    filled = sheet.get("filled") or {}
    periods = (filled.get("TDIS_NPER") or {}).get("value")
    rows = (filled.get("TDIS_PERIODDATA") or {}).get("value") or ()
    adapted = bool((filled.get("TDIS_ATS_FILERECORD") or {}).get("value"))
    steps = int(rows[period - 1][1]) if not adapted and 0 < period <= len(rows) else None
    return {"step": f"stress period {period}{f' of {int(periods)}' if periods else ''}, "
                    f"step {step}{f' of {steps}' if steps else ''}"}
