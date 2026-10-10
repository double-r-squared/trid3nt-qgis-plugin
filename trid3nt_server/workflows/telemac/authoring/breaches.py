"""TELEMAC-2D's breaches data file, as content: one dyke breach per drawn line.

The reader takes one value per line in a fixed order. Lateral-growth options past 2 read
extra lines this file does not write, so they refuse here.
"""

from __future__ import annotations

from typing import Any, Sequence

__all__ = ["BREACHES_FILENAME", "BreachRefused", "text"]

BREACHES_FILENAME = "breaches.txt"

# Initiation option 1: the breach opens at a stated time (no control level).
_AT_A_TIME = 1
# Lateral-growth options 1 (whole line lowered at once) and 2 (opening widened linearly over the duration).
_GROWTH_OPTIONS = (1, 2)


class BreachRefused(ValueError):
    """A breach the engine's reader would refuse, named before it is written."""


def text(breaches: Sequence[dict[str, Any]], *, initial_widths: bool) -> str:
    """The file for ``breaches``, each ``{line_xy, width_m, opens_at_s,
    duration_s, growth, final_bed_m, initial_width_m}`` in the mesh's metres."""
    lines = [str(len(breaches))]
    for n, breach in enumerate(breaches, start=1):
        growth = int(breach["growth"])
        if growth not in _GROWTH_OPTIONS:
            raise BreachRefused(
                f"breach {n} asks lateral-growth option {growth}; the options "
                f"this file writes are {list(_GROWTH_OPTIONS)} - the others read "
                "erosion parameters it does not carry.")
        if float(breach["width_m"]) <= 0.0:
            raise BreachRefused(
                f"breach {n} is {breach['width_m']} m wide; the polygon the engine "
                "lowers around the line needs a positive width.")
        xy = [(float(x), float(y)) for x, y in breach["line_xy"]]
        if len(xy) < 2:
            raise BreachRefused(f"breach {n} carries {len(xy)} point(s); a "
                                "breach is a line along the dyke crest.")
        # Reader's order: polygon width, initiation option, opening moment, duration, growth option, final bottom altitude, initial width, then the polyline.
        lines += [f"{float(breach['width_m']):.3f}", str(_AT_A_TIME),
                  f"{float(breach['opens_at_s']):.3f}",
                  f"{float(breach['duration_s']):.3f}", str(growth),
                  f"{float(breach['final_bed_m']):.3f}"]
        if initial_widths:
            lines.append(f"{float(breach['initial_width_m']):.3f}")
        lines += [str(len(xy)), *(f"{x:.3f}\t{y:.3f}" for x, y in xy)]
    return "\n".join(lines) + "\n"
