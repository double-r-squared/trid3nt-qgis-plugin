"""Environment-knob configuration helpers for the WebSocket server.

Every helper
here is a pure ``env -> value`` reader with no session coupling: read LIVE (not
an import-time snapshot) unless noted, fail-safe to the documented default on a
malformed value, and honor the ``TRID3NT_*`` env idiom so a live regression can
be flipped without a code change. Moved verbatim (behavior-preserving); ``_core``
re-imports these names so bare-global references and monkeypatch targets on
``trid3nt_server.server.<name>`` resolve exactly as the monolith's did.
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Tool-retrieval mode (tool-retrieval kickoff -- orchestrator half).
#
# Three modes, read once at import time following the TRID3NT_SYNC_TOOL_OFFLOAD
# env idiom (NO code change to flip):
#
#   off     (DEFAULT) -- the catalog is the FULL flat registry, untouched. This
#                        is BYTE-IDENTICAL to the pre-feature behavior: no
#                        retrieval is even computed, no shadow record is logged.
#   shadow            -- compute the WOULD-BE-visible set via
#                        retrieve_visible_tools and LOG it as shadow telemetry,
#                        but STILL build declarations over the FULL registry.
#                        ZERO behavior change (the model sees all tools); the
#                        log feeds the recall@k dashboard.
#   enforce           -- subset TOOL_REGISTRY to the visible set BEFORE building
#                        declarations (and UNION the visible set into the Case's
#                        monotonic AllowedToolSet so a once-visible tool never
#                        leaves within a Case). Locked OFF on cloud until recall@k
#                        proves >= 0.99/flow.
#
# K is the discover top-k for retrieve_visible_tools (default 25; the function
# clamps to [1, MAX_K]).
_TOOL_RETRIEVAL_VALID_MODES = frozenset({"off", "shadow", "enforce"})
_TOOL_RETRIEVAL_MODE = (
    os.environ.get("TRID3NT_TOOL_RETRIEVAL", "off").strip().lower()
)
if _TOOL_RETRIEVAL_MODE not in _TOOL_RETRIEVAL_VALID_MODES:
    # Unknown value -> fail-safe to the no-op default (never silently enforce).
    _TOOL_RETRIEVAL_MODE = "off"


def _tool_retrieval_k() -> int:
    """Resolve TRID3NT_TOOL_RETRIEVAL_K (default 25); fall back to the default on
    any parse error. Read per-call so a test can override via the env without a
    module reload."""
    from ..agent.tools.search.tool_retrieval import DEFAULT_K

    raw = os.environ.get("TRID3NT_TOOL_RETRIEVAL_K")
    if raw is None:
        return DEFAULT_K
    try:
        return int(raw)
    except (TypeError, ValueError):
        return DEFAULT_K


def _tool_retrieval_mode() -> str:
    """Current tool-retrieval mode. Reads the env LIVE (not the import-time
    snapshot) so a test / runtime flip is honored; unknown -> 'off' (fail-safe
    to the no-op default, never silently enforce)."""
    mode = os.environ.get("TRID3NT_TOOL_RETRIEVAL", "off").strip().lower()
    return mode if mode in _TOOL_RETRIEVAL_VALID_MODES else "off"


# The ``code_exec_request`` confirm gate validity window (seconds). Running
# arbitrary Python is a deliberate user decision; on expiry the gate fails
# closed (CONFIRMATION_TIMEOUT) and the sandbox does not run. The code-exec
# gate itself no longer waits on this constant (see
# ``_code_exec_approval_timeout_s``); it is retained because the credential /
# region-choice / solver-confirm gates borrow it as their default wait window.
CODE_EXEC_CONFIRM_TIMEOUT_SECONDS: int = int(
    os.environ.get("TRID3NT_CODE_EXEC_CONFIRM_TIMEOUT", "300")
)

# Honest timeout on unanswered code-exec approvals: the code-exec gate gets its
# OWN bounded approval window that applies in EVERY lane (deliberately bypassing
# the F6 24h local override). When no confirmation envelope answers the card in
# time, the gate raises the typed ``CodeExecApprovalTimeoutError`` so the LLM
# receives a structured function_response, narrates honestly, and the TURN
# COMPLETES. Read LIVE (not an import-time snapshot) so runtime flips are honored.
CODE_EXEC_APPROVAL_TIMEOUT_DEFAULT_S: float = 180.0


def _code_exec_approval_timeout_s() -> float:
    """Effective approval-wait window (seconds) for the code-exec confirm gate.

    Env override ``TRID3NT_CODE_EXEC_APPROVAL_TIMEOUT_S``; default 180s.
    Malformed / non-positive values fall back to the default (never an
    unbounded or zero wait).
    """
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
    """Measured-ambiguity threshold (``TRID3NT_AMBIGUITY_MARGIN``).

    RELATIVE top-1 vs top-2 retrieval-score margin under which AUTO mode still
    surfaces the tool-candidates card. Calibration: RRF fused scores are
    rank-compressed -- a tool that is rank-1 on every channel beats a
    consistent rank-2 by only ~1.6% relative, while a genuine cross-channel
    tie (each of two tools rank-1 somewhere) lands well under ~1%. The 0.01
    default therefore fires ONLY on genuine channel disagreement, not on any
    consistently-ordered ranking. ``0`` disables ambiguity asks entirely (the
    kill switch; ask mode is unaffected). Malformed -> default.
    """
    raw = os.environ.get("TRID3NT_AMBIGUITY_MARGIN")
    if raw is None:
        return 0.01
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.01
    return max(0.0, value)


def _tool_choice_timeout_s() -> float:
    """Bounded wait (seconds) for a ``tool-choice`` reply to the
    ``tool-candidates`` card (``TRID3NT_TOOL_CHOICE_TIMEOUT_S``, default 45).

    Deliberately BYPASSES the F6 24h local-lane ``_gate_wait_timeout``
    override (the code-exec-gate precedent): an unanswered picker must
    degrade to autonomous routing, never hang the turn.
    """
    raw = os.environ.get("TRID3NT_TOOL_CHOICE_TIMEOUT_S")
    if raw is None:
        return 45.0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 45.0
    return value if value > 0 else 45.0
