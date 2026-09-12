"""Environment-knob configuration helpers for the WebSocket server.

Every helper is a pure ``env -> value`` reader with no session coupling: read
LIVE rather than as an import-time snapshot unless noted, and fail safe to the
documented default on a malformed value."""

from __future__ import annotations

import os

# Tool-retrieval K, the discover top-k for retrieve_visible_tools. Surfacing is
# unconditional; K is the only lever (retrieve_visible_tools clamps it).
def _tool_retrieval_k() -> int:
    """Resolve TRID3NT_TOOL_RETRIEVAL_K (default 25); fall back to the default on
    any parse error. Read per-call so a test can override via the env without a
    module reload."""
    from ..tools.search.tool_retrieval import DEFAULT_K

    raw = os.environ.get("TRID3NT_TOOL_RETRIEVAL_K")
    if raw is None:
        return DEFAULT_K
    try:
        return int(raw)
    except (TypeError, ValueError):
        return DEFAULT_K


# The default decision window (seconds) the credential, region-choice and
# solver-confirm gates share; the code-exec gate has its own below.
CODE_EXEC_CONFIRM_TIMEOUT_SECONDS: int = int(
    os.environ.get("TRID3NT_CODE_EXEC_CONFIRM_TIMEOUT", "300")
)

# The code-exec gate (``run_pyqgis``) has its OWN bounded approval window that
# applies in every lane. When no confirmation answers the card in time the gate
# raises the typed ``CodeExecApprovalTimeoutError``, so the model narrates
# honestly and the turn COMPLETES. Read LIVE, not as an import-time snapshot.
CODE_EXEC_APPROVAL_TIMEOUT_DEFAULT_S: float = 180.0


def _code_exec_approval_timeout_s() -> float:
    """Effective approval-wait window for the code-exec confirm gate
    (``TRID3NT_CODE_EXEC_APPROVAL_TIMEOUT_S``, default 180s); a malformed or
    non-positive value takes the default, never an unbounded or zero wait."""
    raw = os.environ.get("TRID3NT_CODE_EXEC_APPROVAL_TIMEOUT_S")
    if raw is None:
        return CODE_EXEC_APPROVAL_TIMEOUT_DEFAULT_S
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return CODE_EXEC_APPROVAL_TIMEOUT_DEFAULT_S
    if value <= 0:
        return CODE_EXEC_APPROVAL_TIMEOUT_DEFAULT_S
    return value


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
    """Measured-ambiguity threshold (``TRID3NT_AMBIGUITY_MARGIN``): the relative
    top-1 vs top-2 margin under which AUTO mode still surfaces the candidates
    card; ``0`` disables ambiguity asks and a malformed value takes the default."""
    # RRF fused scores are rank-compressed: a tool that is rank-1 on every
    # channel beats a consistent rank-2 by only ~1.6% relative, while a genuine
    # cross-channel tie lands well under ~1%. The 0.01 default therefore fires
    # only on real channel disagreement, not on a consistently ordered ranking.
    raw = os.environ.get("TRID3NT_AMBIGUITY_MARGIN")
    if raw is None:
        return 0.01
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.01
    return max(0.0, value)


def _tool_choice_timeout_s() -> float:
    """Bounded wait for a ``tool-choice`` reply to the tool-candidates card
    (``TRID3NT_TOOL_CHOICE_TIMEOUT_S``, default 45); an unanswered picker
    degrades to autonomous routing rather than hanging the turn."""
    raw = os.environ.get("TRID3NT_TOOL_CHOICE_TIMEOUT_S")
    if raw is None:
        return 45.0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 45.0
    return value if value > 0 else 45.0
