"""Per-turn top-k tool gating for the LOCAL (openai) provider path (Stage 3).

The routing bench's own recommendation: the openai adapter path sends ALL ~190
tool schemas every round, which both burns local context (the schemas alone are
most of a small model's num_ctx) and measurably hurts selection accuracy. This
module trims the per-turn tool list to the retrieval top-k PLUS a set of
always-include floors, mirroring the retrieval design already proven by
``retrieve_visible_tools`` (tools/discovery/tool_retrieval.py).

Scope (HARD): the gate applies ONLY when ``MODEL_PROVIDER=openai`` -- the
bedrock / scripted / vertex paths are byte-unchanged. ``TRID3NT_TOOL_GATING_TOPK``
sets k (default 24); ``0`` disables the gate entirely (all tools, the
pre-feature behavior).

The gated set for a turn is::

    top-k ranked tools for the user text (retrieve_ranked_tools)
    UNION the META floor (hot set + catalog_search/fetch + web_fetch --
          the discovery / render / analysis escape hatches that must never
          be retrieved out; see categories.HOT_SET_TOOLS)
    UNION every tool already used this case-session (the AllowedToolSet's
          dispatched + explicit tools -- never hide a tool mid-task)
    UNION any tool the user NAMED in the message (exact name or
          space-separated form -- an explicit ask must always be honored)

FAIL-OPEN: an empty/cold ranking, or any fault, leaves the registry ungated
for that turn (over-inclusion is cheap; hiding the needed tool is a silent
break). Pure functions -- the server owns wiring + logging.

ASCII only.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from trid3nt_server.categories import HOT_SET_TOOLS

__all__ = [
    "TOOL_GATING_TOPK_DEFAULT",
    "META_TOOL_FLOOR",
    "gating_topk",
    "named_tools_in_text",
    "gate_tool_registry",
]

logger = logging.getLogger("trid3nt_server.tool_gating")

#: Default top-k for the openai-provider tool gate (bench recommendation).
TOOL_GATING_TOPK_DEFAULT = 24

#: The always-include META floor: the hot set (categories.py conventions --
#: discover_dataset, spatial_query, publish_layer, code_exec_request,
#: geocode_location, zoom/list utilities, ...) plus the catalog discovery
#: pair and web_fetch, which register outside tools/__init__ and are the
#: "find anything else" escape hatches a gated model must always hold.
META_TOOL_FLOOR: frozenset[str] = frozenset(HOT_SET_TOOLS) | frozenset(
    {
        "catalog_search",
        "catalog_fetch",
        "web_fetch",
    }
)


def gating_topk() -> int:
    """Resolve ``TRID3NT_TOOL_GATING_TOPK`` (default 24; 0 disables the gate).

    Read per-call so tests / runtime flips are honored. Malformed / negative
    values fall back to the default (never silently disable).
    """
    raw = os.environ.get("TRID3NT_TOOL_GATING_TOPK")
    if raw is None:
        return TOOL_GATING_TOPK_DEFAULT
    try:
        val = int(str(raw).strip())
    except (TypeError, ValueError):
        return TOOL_GATING_TOPK_DEFAULT
    return val if val >= 0 else TOOL_GATING_TOPK_DEFAULT


_NON_WORD_RE = re.compile(r"[^a-z0-9_]+")


def named_tools_in_text(user_text: Any, names: Any) -> set[str]:
    """Registered tool names the user NAMED in the message (alias/anchor match).

    Conservative on purpose: a tool is "named" when its exact registry name
    (``fetch_dem``) appears in the text, or its space-separated form
    (``fetch dem``) appears as a whole-word phrase. Never raises; a non-string
    text returns the empty set.
    """
    if not isinstance(user_text, str) or not user_text.strip():
        return set()
    # Normalize: lowercase, punctuation -> space, collapsed whitespace, padded
    # so whole-phrase containment checks have boundaries on both ends.
    low = _NON_WORD_RE.sub(" ", user_text.lower())
    low = " " + " ".join(low.split()) + " "
    out: set[str] = set()
    for name in names or ():
        if not isinstance(name, str) or not name:
            continue
        nl = name.lower()
        if f" {nl} " in low:
            out.add(name)
            continue
        spaced = " " + nl.replace("_", " ") + " "
        if spaced in low:
            out.add(name)
    return out


def gate_tool_registry(
    user_text: str,
    registry: dict[str, Any],
    ranked: list[tuple[str, float]],
    k: int,
    used_tools: Any = None,
) -> dict[str, Any] | None:
    """Subset ``registry`` to the gated per-turn set, or ``None`` = do not gate.

    ``ranked`` is the scored retrieval ranking for the turn's user text
    (``retrieve_ranked_tools``); ``used_tools`` is the case-session's
    already-used tool names (AllowedToolSet dispatched + explicit). Returns
    ``None`` (caller keeps the full registry) when:

      * ``k <= 0`` (gate disabled), or
      * ``ranked`` is empty (cold index / no match -- FAIL-OPEN), or
      * the computed subset would not actually shrink the registry.

    Pure; never raises (any internal fault returns ``None`` = ungated).
    """
    try:
        if k <= 0 or not ranked:
            return None
        keep: set[str] = {name for name, _score in ranked[:k]}
        keep |= META_TOOL_FLOOR
        if used_tools:
            keep |= {t for t in used_tools if isinstance(t, str)}
        keep |= named_tools_in_text(user_text, registry.keys())
        subset = {name: entry for name, entry in registry.items() if name in keep}
        if not subset or len(subset) >= len(registry):
            return None
        return subset
    except Exception:  # noqa: BLE001 -- fail open, never break the turn
        logger.warning("gate_tool_registry: fault; failing open", exc_info=True)
        return None
