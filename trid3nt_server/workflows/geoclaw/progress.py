"""GeoClaw's own progress, read off one stdout line.

The step is adaptive, so there is no step total: the line states the time the coarse step reached and each fgout frame it wrote.
The end time and the frame count are the deck's own statements, never estimates.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

__all__ = ["step_of"]

_REACHED = re.compile(r"AMRCLAW:.*final t =\s*([0-9.]+E[+-]?\d+)")
_FRAME = re.compile(r"Writing fgout grid #\s*(\d+)\s+frame\s+(\d+)")


def step_of(line: str, sheet: Mapping[str, Any]) -> dict[str, str] | None:
    """The parts this stdout line states: the time reached, or a frame written. ``None`` for any other line."""
    filled = sheet.get("filled") or {}
    reached = _REACHED.search(line)
    if reached is not None:
        end = (filled.get("CLAW_TFINAL") or {}).get("value")
        seen = f"t = {float(reached.group(1)):.6g} s"
        return {"t": f"{seen} of {float(end):.6g} s" if end is not None else seen}
    frame = _FRAME.search(line)
    if frame is None:
        return None
    grids = (filled.get("FGOUT_FGOUT_GRIDS") or {}).get("value") or ()
    number, k = int(frame.group(1)), int(frame.group(2))
    total = next((int(g[2]) for g in grids if int(g[0]) == number), None)
    name = f"grid {number} frame" if len(grids) > 1 else "frame"
    return {f"frame{number}": f"{name} {k} of {total}" if total else f"{name} {k}"}
