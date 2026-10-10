"""What a solved run's own listing says, read on the server.

The worker only runs the steering file; everything derived from the listing is read here.
Every function is best-effort: a failed parse returns nothing and the layers stand.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger("trid3nt_server.workflows.telemac.modules.listing")

__all__ = [
    "boundary_flux",
    "continuity_rel_error",
    "worst_closure",
    "engine_demand",
    "final_balance",
    "gaia_mass_balance",
    "nestor_volumes",
]

# GAIA prints its closure per class in kg, cut at the end-of-run marker so the final balance is read.
_GAIA_HEADING = "FINAL MASS-BALANCE OF SEDIMENTS"
_GAIA_BLOCK_END = r"END OF TIME LOOP|CORRECT END OF RUN"
# The listing label -> the metric name, and the places it survives at.
_GAIA_FIELDS: tuple[tuple[str, str, int], ...] = (
    ("CUMULATED DEPOSITION", "sediment_deposited_mass_kg", 4),
    ("CUMULATED EROSION", "sediment_eroded_mass_kg", 4),
    ("CUMULATED BED EVOLUTIONS", "sediment_net_bed_mass_kg", 6),
    ("CUMULATED LOST MASS", "sediment_mass_lost_kg", 8),
)


# What NESTOR reports per action: printed label -> metric name; every line opens with ``?>``. The criterion dig reports the LAST maintenance period and resets its sum, so the run's figure is the sum over prints. The timed dump's line reports the volume it was given, not measured, and is not read.
_NESTOR_FIELDS: tuple[tuple[str, str], ...] = (
    (r"dug volume\s*\[m\^3\]", "dug_volume_m3"),
    (r"dumped vol\s*\[m\^3\]", "dumped_volume_m3"),
    (r"relocated volume\s*\[m\*\*3\]", "relocated_volume_m3"),
    (r"removed volume\s*\[m\*\*3\]", "removed_volume_m3"),
)
# NESTOR marks every line from its initialisation banner on, so the marker's absence means no dredge was armed.
_NESTOR_MARK = "?>"
# An action prints this on activation and its volume line only when a pass finishes, so together they say whether a pass ran and completed; the activation line carries the action type.
_NESTOR_START = re.compile(r"\?>\s*(?:re)?start action\s*:\s*(\S+)")
# An activation's instant on the bed's clock; the nominal start prints under the same words behind ``nominal``, which the space class excludes.
_NESTOR_START_TIME = re.compile(r"\?>\s+start time\s*\[s\]\s*:\s*([-+\d.EeDd]+)")


def nestor_volumes(listing_text: str) -> dict[str, Any]:
    """What the dredge moved, off the lines NESTOR printed into the listing.

    Each figure is the sum over the actions and periods that reported it, absent if never printed;
    ``dredge_report`` says why a bed that moved printed no volume.
    """
    text = listing_text or ""
    out: dict[str, Any] = {}
    for pattern, name in _NESTOR_FIELDS:
        found = re.findall(r"\?>\s*" + pattern + r"\s*:\s*([-+\d.EeDd]+)", text)
        values = []
        for raw in found:
            try:
                values.append(float(raw.replace("D", "E").replace("d", "e")))
            except ValueError:
                continue
        if values:
            out[name] = round(sum(values), 6) + 0.0
    if _NESTOR_MARK not in text:
        return out
    out["dredge_report"] = _dredge_report(text, finished=bool(out))
    return out


def _dredge_report(listing_text: str, *, finished: bool) -> str:
    if finished:
        return ("the volumes are the engine's own report lines, summed over the "
                "passes that finished inside the run's clock")
    started = _NESTOR_START.findall(listing_text)
    if not started:
        return ("no dredging pass began inside the run's clock, so the engine "
                "printed no volume: the schedule starts later than the run "
                "reaches on the bed's clock")
    when = [float(raw.replace("D", "E").replace("d", "e"))
            for raw in _NESTOR_START_TIME.findall(listing_text)]
    at = f" at {when[-1]:g} s" if when else ""
    return (f"a {started[-1]} pass started{at} and had not cut to grade when the "
            "run's clock ended; the engine prints a volume only for a pass that "
            "finishes, so the bed moved with no volume to state it")


# How LECDON asks for a keyword it will not start without; the engine names the keyword on the line the phrase opens or those under it.
_LECDON_DEMAND = re.compile(
    r"IS MANDATORY|GIVE THE KEY-?WORDS?|GIVE THE CORRESPONDING|GIVE A VALUE"
    r"|NO FRICTION LAW IS PRESCRIBED")
# Where a demand block ends: the banner the engine stops under.
_PLANTE = "PLANTE:"
# How many lines under a demand carry its keyword names.
_DEMAND_LINES = 6


def engine_demand(listing_text: str) -> str | None:
    """What the engine asked for before it stopped, in its own words.

    Only the engine knows which open keyword this deck cannot run without.
    """
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

    The authoritative masses; a field the listing did not print is absent.
    """
    # Adding 0.0 collapses ``-0.0`` (a tiny negative residue that survives ``max(value, 0.0)``) onto the positive zero; a genuinely negative mass is untouched.
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


# TELEMAC-2D closes its volume balance once per listing period: heading, one flux line per liquid boundary, then the relative error with the block's time. The heading is read so a tracer balance's flux lines cannot leak in.
_BALANCE_HEAD = r"BALANCE OF WATER VOLUME"
_FLUX_BOUNDARY = r"FLUX BOUNDARY\s+(\d+)\s*:\s*([-+\d.Ee]+)"
_BALANCE_TIME = r"RELATIVE ERROR IN VOLUME AT T\s*=\s*([-+\d.Ee]+)\s*S"
_VOLUME_ERROR = _BALANCE_TIME + r"\s*:\s*([-+\d.Ee]+)"


def continuity_rel_error(listing_text: str) -> float | None:
    """The engine's own volume closure, off the last one it printed.

    The solver prints one every listing period; the last is the run's.
    """
    found = re.findall(_VOLUME_ERROR, listing_text or "")
    if not found:
        return None
    try:
        return float(found[-1][1])
    except ValueError:
        return None


# Every closure printed as a relative error: water volume per period and cumulated, each tracer's balance, GAIA's sediment mass against the active layer, total and cumulated.
_CLOSURE = re.compile(
    r"^\s*((?:CUMULATED )?RELATIVE ERROR[^\n]*?)\s*[:=]\s*"
    r"([-+]?\d+(?:\.\d*)?(?:[EeDd][-+]?\d+)?)\s*$", re.MULTILINE)


def worst_closure(listing_text: str) -> dict[str, Any] | None:
    """The worst relative closure over the run - water, tracer or sediment - with the engine's line naming which and when."""
    worst: dict[str, Any] | None = None
    for phrase, raw in _CLOSURE.findall(listing_text or ""):
        try:
            value = float(raw.replace("D", "E").replace("d", "e"))
        except ValueError:
            continue
        if worst is None or abs(value) > abs(worst["relative_error"]):
            worst = {"relative_error": value, "line": " ".join(phrase.split())}
    return worst


def boundary_flux(listing_text: str, *, boundary: int
                  ) -> tuple[list[float], list[float]]:
    """The discharge through one liquid boundary over time, as the engine measured it.

    ``boundary`` is the 1-based number the solver walks its liquid boundaries in. Outflow is positive;
    the listing's convention is the opposite and is negated once, at the read.
    """
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
                # Negated once, plus zero so a printed 0 is not -0.
                flows.append(round(-pending[int(boundary)], 6) + 0.0)
            except ValueError:
                pass
        pending = None
    return times, flows


# The whole-run closure under this heading, in m3: begin and end volumes, boundary crossings (entering positive), source terms, losses. The SCS-CN runoff routine prints gross rainfall in metres, the engine's own depth.
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

    A figure the listing did not print is absent; accumulated rainfall depth rides beside them when printed.
    """
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
