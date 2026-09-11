"""What a solved run's OWN listing says, read on the server.

The worker is the engine room: it runs a steering file and writes what the engine
printed, and everything derived from the listing is read HERE. Every function is
BEST-EFFORT: a parse that fails returns nothing and the primary layer stands."""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger("trid3nt_server.workflows.telemac.modules.listing")

__all__ = [
    "boundary_flux",
    "continuity_rel_error",
    "engine_demand",
    "final_balance",
    "gaia_mass_balance",
]

#: GAIA prints its closure once per class under this heading, in kg. The block is
#: cut at the end-of-run marker so a run that printed intermediate balances is
#: read at its FINAL one.
_GAIA_HEADING = "FINAL MASS-BALANCE OF SEDIMENTS"
_GAIA_BLOCK_END = r"END OF TIME LOOP|CORRECT END OF RUN"
#: The listing label -> the metric name, and how many places it survives at.
_GAIA_FIELDS: tuple[tuple[str, str, int], ...] = (
    ("CUMULATED DEPOSITION", "sediment_deposited_mass_kg", 4),
    ("CUMULATED EROSION", "sediment_eroded_mass_kg", 4),
    ("CUMULATED BED EVOLUTIONS", "sediment_net_bed_mass_kg", 6),
    ("CUMULATED LOST MASS", "sediment_mass_lost_kg", 8),
)


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


def gaia_mass_balance(listing_text: str) -> dict[str, Any]:
    """GAIA's own closure out of the solver listing - deposited/eroded/net/lost kg.

    The authoritative masses; a field the listing did not print is absent."""
    # ZERO HAS NO SIGN, and the sign is fixed HERE so no consumer has to know: a
    # residual the listing prints as a tiny negative rounds to ``-0.0``, which
    # survives ``max(value, 0.0)`` unchanged and would reach the reader as a
    # negative deposited mass beside a map showing deposition. Adding 0.0
    # collapses the negative zero onto the positive one; a genuinely negative
    # mass is untouched.
    start = re.search(_GAIA_HEADING, listing_text or "")
    if start is None:
        return {}
    block = (listing_text or "")[start.end():]
    end = re.search(_GAIA_BLOCK_END, block)
    if end is not None:
        block = block[:end.start()]
    out: dict[str, Any] = {}
    for label, name, places in _GAIA_FIELDS:
        found = re.search(re.escape(label) + r"\s*=\s*([-\d.Ee+]+)", block)
        if found is None:
            continue
        try:
            out[name] = round(float(found.group(1)), places) + 0.0
        except ValueError:
            continue
    return out


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


#: The engine closes its whole run once, under this heading, in m3: what it
#: began and ended with, what crossed the liquid boundaries (entering positive),
#: what the source terms added, and what it lost. The SCS-CN runoff routine
#: prints the gross rainfall it accumulated in metres, so the depth that fell is
#: the engine's own figure rather than a re-derivation from the deck.
_FINAL_HEAD = r"FINAL BALANCE OF WATER VOLUME"
_FINAL_FIELDS: tuple[tuple[str, str], ...] = (
    (r"INITIAL VOLUME\s*:\s*([-+\d.Ee]+)", "initial_volume_m3"),
    (r"FINAL VOLUME\s*:\s*([-+\d.Ee]+)", "final_volume_m3"),
    (r"VOLUME THAT ENTERED THE DOMAIN\s*:\s*([-+\d.Ee]+)", "boundary_volume_m3"),
    (r"VOLUME ADDED BY SOURCE TERM\s*:\s*([-+\d.Ee]+)", "source_volume_m3"),
    (r"TOTAL VOLUME LOST\s*:\s*([-+\d.Ee]+)", "lost_volume_m3"),
)
_ACCUMULATED_RAIN = r"ACCUMULATED RAINFALL\s*:\s*([-+\d.Ee]+)\s*M\b"


def final_balance(listing_text: str) -> dict[str, Any]:
    """The engine's whole-run water balance, off the final block it printed.

    A figure the listing did not print is absent; the accumulated rainfall depth
    rides beside them when the runoff routine printed one."""
    text = listing_text or ""
    out: dict[str, Any] = {}
    start = re.search(_FINAL_HEAD, text)
    if start is not None:
        block = text[start.end():]
        for pattern, name in _FINAL_FIELDS:
            found = re.search(pattern, block)
            if found is None:
                continue
            try:
                out[name] = float(found.group(1))
            except ValueError:
                continue
    rain = re.findall(_ACCUMULATED_RAIN, text)
    if rain:
        try:
            out["rain_depth_m"] = float(rain[-1])
        except ValueError:
            pass
    return out
