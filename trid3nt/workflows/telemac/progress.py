"""The TELEMAC time loop's own step, read off one listing line.

The line is ``ITERATION n TIME: ... ( t S)``; a step total or an end time is the deck's own statement, never an estimate.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

__all__ = ["step_of"]

_ITERATION = re.compile(r"^\s*ITERATION\s+(\d+)\s+TIME:(.*)$")
_SECONDS = re.compile(r"([0-9][0-9.]*(?:E[+-]?\d+)?)\s*S\s*\)?\s*$")


def _stated(sheet: Mapping[str, Any], name: str) -> Any:
    return (sheet.get("filled") or {}).get(name, {}).get("value")


def step_of(line: str, sheet: Mapping[str, Any]) -> dict[str, str] | None:
    """The step this listing line states, or ``None`` for a line that states none.
    ``step i of N`` where the deck states NUMBER OF TIME STEPS under a fixed step, ``step i`` otherwise, then the simulated time
    against the deck's DURATION when it states one."""
    hit = _ITERATION.match(line)
    if hit is None:
        return None
    number, clock = int(hit.group(1)), _SECONDS.search(hit.group(2))
    steps = _stated(sheet, "NUMBER_OF_TIME_STEPS")
    fixed = not _stated(sheet, "VARIABLE_TIME_STEP")
    text = f"step {number} of {int(steps)}" if steps and fixed else f"step {number}"
    if clock is None:
        return {"step": text}
    duration = _stated(sheet, "DURATION")
    seen = f"t = {float(clock.group(1)):.6g} s"
    return {"step": text, "t": f"{seen} of {float(duration):.6g} s" if duration else seen}
