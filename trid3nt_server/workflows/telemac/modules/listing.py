"""What a solved run's OWN listing says, read on the server.

The worker is the engine room: it runs a steering file and writes what the engine
printed, and everything derived from the listing is read HERE. Every function is
BEST-EFFORT: a parse that fails returns nothing and the run's layers stand."""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger("trid3nt_server.workflows.telemac.modules.listing")

__all__ = [
    "boundary_flux",
    "continuity_rel_error",
    "engine_demand",
]


#: How LECDON asks for a keyword it will not start without. The engine names the
#: keyword itself, on the line the phrase opens or on the ones under it, so what
#: reaches a reader is the engine's own sentence rather than a set this code
#: decided a run needs.
_LECDON_DEMAND = re.compile(
    r"IS MANDATORY|GIVE THE KEY-?WORDS?|GIVE THE CORRESPONDING|GIVE A VALUE"
    r"|NO FRICTION LAW IS PRESCRIBED")
#: Where a demand block ends: the banner the engine stops under.
_PLANTE = "PLANTE:"
#: How many lines under a demand carry its keyword names.
_DEMAND_LINES = 6


def engine_demand(listing_text: str) -> str | None:
    """What the engine ASKED FOR before it stopped, in its own words.

    Only the engine knows which open keyword THIS deck cannot run without."""
    lines = [line.rstrip() for line in (listing_text or "").splitlines()]
    starts = [i for i, line in enumerate(lines) if _LECDON_DEMAND.search(line)]
    if not starts:
        return None
    block: list[str] = []
    for line in lines[starts[-1]:starts[-1] + _DEMAND_LINES]:
        if not line.strip() or _PLANTE in line:
            break
        block.append(line.strip())
    return "; ".join(block) or None


#: TELEMAC-2D closes its water-volume balance once per listing period. The block
#: OPENS with its heading, carries one flux line per liquid boundary, and closes
#: with the relative error stamped with the time the whole block belongs to. The
#: heading is read too, so a tracer balance's own flux lines - printed under a
#: different heading and closed with a different error - cannot leak into it.
_BALANCE_HEAD = r"BALANCE OF WATER VOLUME"
_FLUX_BOUNDARY = r"FLUX BOUNDARY\s+(\d+)\s*:\s*([-+\d.Ee]+)"
_BALANCE_TIME = r"RELATIVE ERROR IN VOLUME AT T\s*=\s*([-+\d.Ee]+)\s*S"
_VOLUME_ERROR = _BALANCE_TIME + r"\s*:\s*([-+\d.Ee]+)"


def continuity_rel_error(listing_text: str) -> float | None:
    """The engine's OWN volume closure, off the last one it printed.

    The solver prints one every listing period; the LAST figure is the run's."""
    found = re.findall(_VOLUME_ERROR, listing_text or "")
    if not found:
        return None
    try:
        return float(found[-1][1])
    except ValueError:
        return None


def boundary_flux(listing_text: str, *, boundary: int
                  ) -> tuple[list[float], list[float]]:
    """The discharge through one LIQUID BOUNDARY over time, as the engine measured it.

    ``boundary`` is the 1-based number the solver walks its liquid boundaries in.
    ONE SIGN CONVENTION, stated here and nowhere else: outflow is positive; the
    listing's own is the opposite and is negated once, at the read."""
    times: list[float] = []
    flows: list[float] = []
    pending: dict[int, float] | None = None
    for line in (listing_text or "").splitlines():
        if re.search(_BALANCE_HEAD, line):
            pending = {}
            continue
        if pending is None:
            continue
        flux = re.search(_FLUX_BOUNDARY, line)
        if flux is not None:
            try:
                pending[int(flux.group(1))] = float(flux.group(2))
            except ValueError:
                continue
            continue
        stamp = re.search(_BALANCE_TIME, line)
        if stamp is None:
            continue
        if int(boundary) in pending:
            try:
                times.append(round(float(stamp.group(1)), 3))
                # Negated once, then plus zero so a printed 0 is not a -0.
                flows.append(round(-pending[int(boundary)], 6) + 0.0)
            except ValueError:
                pass
        pending = None
    return times, flows


