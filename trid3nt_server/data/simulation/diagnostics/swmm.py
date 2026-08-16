"""Internal SWMM diagnostics parser for ``read_run_diagnostics`` (NOT registered).

Reads the EPA-SWMM ``.rpt`` status report:

- ``Runoff Quantity Continuity`` -> ``Continuity Error (%)`` (runoff mass balance)
- ``Flow Routing Continuity`` -> ``Continuity Error (%)`` (the routing mass
  balance -> the envelope's top-level ``mass_balance_pct``, ``"reported"``)
- ``Highest Flow Instability Indexes`` -> the max per-link index (instability)
- ``% of Steps Not Converging`` (Routing Time Step Summary) -> nonconvergence
- ``Node Flooding Summary`` / ``Node Surcharge Summary`` -> flooded / surcharged
  node counts
- ``Flooding Loss`` (Flow Routing Continuity table) -> total flood volume

Confirmed against a real ``mesh.rpt`` fixture (build-contract 5.1). ASCII only.
"""

from __future__ import annotations

import re
from typing import Any

from ._common import (
    DiagnosticsParseError,
    EngineDiagnostics,
    RunArtifacts,
)

__all__ = ["parse_swmm"]

# --- Healthy / warning thresholds (named constants with source comments). --- #

#: SWMM routing continuity error within this magnitude reads "healthy" in the
#: coarse roll-up. The EPA-SWMM manual treats a routing continuity error under
#: ~1% as good practice for a well-posed model (build-contract 3.1 SWMM band).
SWMM_CONTINUITY_HEALTHY_PCT: float = 1.0

#: A continuity error above this magnitude is surfaced as a warning (still not a
#: gate). 5% is the commonly cited SWMM upper bound above which a run's mass
#: balance should be reviewed.
SWMM_CONTINUITY_WARN_PCT: float = 5.0

#: SWMM's per-link Flow Instability Index runs 0..(N); a value at/above this is
#: worth flagging. SWMM caps the reported index at 5.
SWMM_FII_WARN_INDEX: int = 5

_CONTINUITY_ERROR_RE = re.compile(r"Continuity Error \(%\)[^\-\d]*(-?\d+(?:\.\d+)?)")
_PAREN_INT_RE = re.compile(r"\((\d+)\)")
_STEPS_NOT_CONVERGING_RE = re.compile(
    r"% of Steps Not Converging\s*:\s*(-?\d+(?:\.\d+)?)"
)
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


#: A SWMM section terminator: a line that is ONLY asterisks (the next section's
#: top banner). The continuity tables' "****  -----" and "**** Volume" lines
#: carry trailing non-asterisk text and do NOT match, so a continuity block
#: correctly runs to the next titled section.
_PURE_BANNER_RE = re.compile(r"\n[ \t]*\*{3,}[ \t]*\n")
_UNDERLINE_BANNER_RE = re.compile(r"[ \t]*\*{3,}[ \t]*\n")


def _section_slice(text: str, header: str) -> str | None:
    """Return the body from ``header`` to the next titled section (or EOF).

    Starts the body AFTER the header line, and skips the header's OWN
    immediately-following pure-asterisk underline (a title wrapped in banners,
    e.g. "Highest Flow Instability Indexes", would otherwise yield an empty
    slice). The body ends at the next pure-asterisk banner line.
    """
    idx = text.find(header)
    if idx < 0:
        return None
    nl = text.find("\n", idx)
    if nl < 0:
        return ""
    rest = text[nl + 1:]
    under = _UNDERLINE_BANNER_RE.match(rest)
    if under:
        rest = rest[under.end():]
    nxt = _PURE_BANNER_RE.search(rest)
    end = nxt.start() if nxt else len(rest)
    return rest[:end]


def _continuity_error_after(text: str, header: str) -> float | None:
    """The ``Continuity Error (%)`` value inside the named continuity block."""
    block = _section_slice(text, header)
    if block is None:
        return None
    m = _CONTINUITY_ERROR_RE.search(block)
    return float(m.group(1)) if m else None


def _flooding_loss_volume(text: str) -> float | None:
    """Second column (10^6 ltr) of the Flow Routing Continuity ``Flooding Loss``."""
    block = _section_slice(text, "Flow Routing Continuity")
    if block is None:
        return None
    for line in block.splitlines():
        if line.strip().startswith("Flooding Loss"):
            nums = _NUM_RE.findall(line)
            if len(nums) >= 2:
                return float(nums[1])
            if nums:
                return float(nums[0])
    return None


def _max_flow_instability(text: str) -> int | None:
    """Largest per-link index in the ``Highest Flow Instability Indexes`` block."""
    block = _section_slice(text, "Highest Flow Instability Indexes")
    if block is None:
        return None
    vals = [int(x) for x in _PAREN_INT_RE.findall(block)]
    return max(vals) if vals else None


def _count_summary_nodes(text: str, header: str) -> int | None:
    """Count node rows in a Node Flooding / Surcharge Summary block.

    ``0`` when SWMM printed "No nodes were ...". ``None`` when the section is
    absent entirely (engine did not report it). Otherwise the count of data
    rows (a leading node-id token followed by numeric columns).
    """
    block = _section_slice(text, header)
    if block is None:
        return None
    if "No nodes were" in block:
        return 0
    n = 0
    for line in block.splitlines():
        s = line.strip()
        if not s or s.startswith("-") or s.startswith("*"):
            continue
        parts = s.split()
        # A data row starts with a node id and has at least one numeric column.
        if len(parts) >= 2 and any(_NUM_RE.fullmatch(p) for p in parts[1:]):
            n += 1
    return n if n > 0 else 0


def parse_swmm(art: RunArtifacts, status: str) -> EngineDiagnostics:
    """Parse the SWMM ``.rpt`` into normalized diagnostics."""
    rpt_uri, rpt_bytes = art.read_output_required(suffix=".rpt")
    try:
        text = rpt_bytes.decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        raise DiagnosticsParseError(
            "swmm", art.run_id, rpt_uri, f"could not decode .rpt: {exc}"
        ) from exc

    if "Flow Routing Continuity" not in text and "Runoff Quantity Continuity" not in text:
        raise DiagnosticsParseError(
            "swmm",
            art.run_id,
            rpt_uri,
            "no SWMM continuity section found -- not a SWMM status report",
        )

    notes: list[str] = []
    warnings: list[str] = []

    runoff_pct = _continuity_error_after(text, "Runoff Quantity Continuity")
    routing_pct = _continuity_error_after(text, "Flow Routing Continuity")
    max_fii = _max_flow_instability(text)
    flood_volume = _flooding_loss_volume(text)
    flooded = _count_summary_nodes(text, "Node Flooding Summary")
    surcharged = _count_summary_nodes(text, "Node Surcharge Summary")

    m = _STEPS_NOT_CONVERGING_RE.search(text)
    nonconverged_pct = float(m.group(1)) if m else None

    # Warnings (honest thresholds, not gates).
    if routing_pct is not None and abs(routing_pct) > SWMM_CONTINUITY_WARN_PCT:
        warnings.append(
            f"Flow-routing continuity error {routing_pct:.3f}% exceeds "
            f"{SWMM_CONTINUITY_WARN_PCT:.1f}% -- review mass balance."
        )
    if runoff_pct is not None and abs(runoff_pct) > SWMM_CONTINUITY_WARN_PCT:
        warnings.append(
            f"Runoff continuity error {runoff_pct:.3f}% exceeds "
            f"{SWMM_CONTINUITY_WARN_PCT:.1f}%."
        )
    if nonconverged_pct is not None and nonconverged_pct > 0.0:
        warnings.append(
            f"{nonconverged_pct:.2f}% of routing steps did not converge."
        )
    if max_fii is not None and max_fii >= SWMM_FII_WARN_INDEX:
        warnings.append(
            f"Peak flow instability index {max_fii} at/above {SWMM_FII_WARN_INDEX} "
            "(SWMM max) -- numerical instability likely."
        )

    # Coarse healthy roll-up (raw fields remain authoritative).
    healthy: bool | None
    if status != "ok":
        healthy = False
    elif routing_pct is None:
        healthy = None
    else:
        healthy = (
            abs(routing_pct) <= SWMM_CONTINUITY_HEALTHY_PCT
            and (nonconverged_pct in (None, 0.0))
            and (max_fii is None or max_fii < SWMM_FII_WARN_INDEX)
        )
    notes.append(
        "healthy heuristic: status==ok AND abs(flow-routing continuity) <= "
        f"{SWMM_CONTINUITY_HEALTHY_PCT:.1f}% AND no nonconvergence AND flow "
        f"instability index < {SWMM_FII_WARN_INDEX}."
    )
    if flood_volume is not None:
        notes.append(
            "flood_volume is the SWMM Flow-Routing 'Flooding Loss' total in "
            "10^6 litres."
        )

    engine_specific: dict[str, Any] = {
        "runoff_continuity_pct": runoff_pct,
        "flow_routing_continuity_pct": routing_pct,
        "max_flow_instability_index": max_fii,
        "flooded_nodes": flooded,
        "surcharged_nodes": surcharged,
        "flood_volume": flood_volume,
    }

    return EngineDiagnostics(
        mass_balance_pct=routing_pct,
        mass_balance_source="reported" if routing_pct is not None else None,
        instability=max_fii,
        nonconverged_pct=nonconverged_pct,
        dry_cells=None,
        healthy=healthy,
        warnings=warnings,
        engine_specific=engine_specific,
        notes=notes,
        diagnostics_files=[rpt_uri],
    )
