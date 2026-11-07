"""Environment-knob configuration helpers for the WebSocket server.

Every helper is a pure ``env -> value`` reader with no session coupling: read
LIVE rather than as an import-time snapshot unless noted, and fail safe to the
documented default on a malformed value."""

from __future__ import annotations

import os

from trid3nt_server.settings import setting

# Tool-retrieval K, the discover top-k for retrieve_visible_tools. Surfacing is
# unconditional; K is the only lever (retrieve_visible_tools clamps it).
def _tool_retrieval_k() -> int:
    """Resolve TRID3NT_TOOL_RETRIEVAL_K (default 25); fall back to the default on any parse error. Read per call so a test can override via the env."""
    from ..tools.search.tool_retrieval import DEFAULT_K

    raw = os.environ.get("TRID3NT_TOOL_RETRIEVAL_K")
    if raw is None:
        return DEFAULT_K
    try:
        return int(raw)
    except (TypeError, ValueError):
        return DEFAULT_K


def _env_flag(name: str, default: bool = True) -> bool:
    """Boolean env flag, read LIVE: '0'/'off'/'false'/'no' -> False,
    '1'/'on'/'true'/'yes' -> True, unset/unknown -> ``default``."""
    raw = (os.environ.get(name) or "").strip().lower()
    if raw in ("0", "off", "false", "no"):
        return False
    if raw in ("1", "on", "true", "yes"):
        return True
    return default


def _ambiguity_margin_threshold() -> float:
    """The AMBIGUITY_MARGIN setting: the relative top-1 vs top-2 margin under which AUTO mode still surfaces the candidates card; 0 asks never."""
    # RRF fused scores are rank-compressed: a tool that is rank-1 on every channel beats a
    # consistent rank-2 by only ~1.6% relative, while a genuine cross-channel tie lands well under
    # ~1%. The 0.01 default fires only on real channel disagreement, not a consistent ranking.
    return float(setting("AMBIGUITY_MARGIN"))


def _tool_choice_timeout_s() -> float:
    """Bounded wait for a ``tool-choice`` reply to the tool-candidates card
    (the TOOL_CHOICE_TIMEOUT_S setting); an unanswered picker
    degrades to autonomous routing rather than hanging the turn."""
    return setting("TOOL_CHOICE_TIMEOUT_S")
