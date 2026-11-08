"""Environment-knob configuration helpers for the WebSocket server.

Every helper is a pure ``env -> value`` reader with no session coupling: read
LIVE rather than as an import-time snapshot unless noted, and fail safe to the
documented default on a malformed value."""

from __future__ import annotations

import os

from trid3nt_server.settings import setting

def _tool_retrieval_k() -> int:
    """The TOOL_RETRIEVAL_K setting: the ceiling on the tools a turn's matches add, which retrieve_visible_tools clamps."""
    return int(setting("TOOL_RETRIEVAL_K"))


def _env_flag(name: str, default: bool = True) -> bool:
    """Boolean env flag, read LIVE: '0'/'off'/'false'/'no' -> False,
    '1'/'on'/'true'/'yes' -> True, unset/unknown -> ``default``."""
    raw = (os.environ.get(name) or "").strip().lower()
    if raw in ("0", "off", "false", "no"):
        return False
    if raw in ("1", "on", "true", "yes"):
        return True
    return default


def _tool_choice_timeout_s() -> float:
    """Bounded wait for a ``tool-choice`` reply to the tool-candidates card
    (the TOOL_CHOICE_TIMEOUT_S setting); an unanswered picker
    degrades to autonomous routing rather than hanging the turn."""
    return setting("TOOL_CHOICE_TIMEOUT_S")
