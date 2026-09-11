"""Per-turn top-k tool gating for the LOCAL (openai) provider path.

It applies ONLY under ``MODEL_PROVIDER=openai``, ``TRID3NT_TOOL_GATING_TOPK=0`` disables
it, and any fault leaves that turn UNGATED.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

from trid3nt_server.tools.search.tool_retrieval import CORE_FLOOR

__all__ = [
    "TOOL_GATING_TOPK_DEFAULT",
    "META_TOOL_FLOOR",
    "gating_topk",
    "named_tools_in_text",
    "gate_tool_registry",
    # POOR-FIT WIDENING
    "WIDEN_THRESHOLD_DEFAULT",
    "WIDEN_K",
    "gating_widen_threshold",
    "should_widen_for_poor_fit",
    # BENCH pre-dispatch block hook
    "BENCH_BLOCKED_WRONG_PICK",
    "BENCH_BLOCKED_CORRECT",
    "BenchBlockConfig",
    "BenchBlockedError",
    "parse_bench_block_config",
    "bench_block_decision",
]

logger = logging.getLogger("trid3nt_server.gates.tool_gating")

#: Default top-k for the openai-provider tool gate.
TOOL_GATING_TOPK_DEFAULT = 24

#: The always-include META floor: the core floor plus web_fetch, the open-web
#: escape hatch a gated model must always hold. It registers at daemon startup,
#: outside the tools package.
META_TOOL_FLOOR: frozenset[str] = frozenset(CORE_FLOOR) | frozenset({"web_fetch"})


def gating_topk() -> int:
    """Resolve ``TRID3NT_TOOL_GATING_TOPK`` (default 24; 0 disables the gate).

    Read per-call; a malformed or negative value falls back, never disables."""
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
    """Registered tool names the user NAMED: an exact name or its spaced form.

    Whole-word matches only; never raises, and non-string text gives ``set()``."""
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

    ``None``, and the caller keeps the full registry, when ``k <= 0``, when
    ``ranked`` is empty, or when the subset would not shrink it; never raises."""
    try:
        if k <= 0 or not ranked:
            return None
        from trid3nt_server.tools import mounted_tool_names

        # The gated set is the top-k ranking UNION the META floor UNION every
        # tool already visible this case-session (never hide a tool mid-task)
        # UNION any tool the user NAMED (an explicit ask is always honored).
        keep: set[str] = {name for name, _score in ranked[:k]}
        keep |= META_TOOL_FLOOR
        # A session-mounted tool is unrankable (the index predates it), so it
        # rides the floor for as long as it is mounted.
        keep |= set(mounted_tool_names())
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


# POOR-FIT WIDENING: when a turn's TOP retrieval score is under a calibrated
# threshold, the ranking is uncertain -- widen that turn's gate k once
# (24 -> 40) so recall does not silently drop on an ambiguous / vague ask.
# Over-inclusion is cheap; hiding the needed tool on a hard query is a
# silent break (the same asymmetry the whole gate is designed around).
#
# Calibration (offline, against the ``hashed`` deterministic dense fallback
# -- the local default when sentence-transformers is absent).
# retrieve_ranked_tools RRF top-1 score distributions over the routing_sweep
# input prompts + a degenerate poor-fit control set:
#
#   register            min      median   max      (n)
#   specific (good fit) 0.0376   0.0487   0.0492   (13)
#   vague               0.0280   0.0401   0.0487   (13)
#   poor-fit control    0.0164   0.0309   0.0456   (10)   ("hi", "ok",
#                                                          "asdf qwerty", ...)
#
# The specific (clearly-matched) queries FLOOR at 0.0376, while the ambiguous
# tail (vague + degenerate) lives below ~0.035. The default 0.035 sits just
# under the specific-query floor: a well-matched turn NEVER widens, and only a
# genuinely uncertain top-1 (< 0.035) triggers the one-shot widen. RRF scores
# are backend-dependent (this is the ``hashed`` fallback), so prod running a
# different LOCAL dense backend should recalibrate via the env override; the
# threshold is deliberately a lever, not a constant, for exactly this reason.
#: Default poor-fit widen threshold (see the calibration note above).
WIDEN_THRESHOLD_DEFAULT = 0.035
#: The widened per-turn gate k a poor-fit turn steps up to (from the 24 floor).
WIDEN_K = 40


def gating_widen_threshold() -> float:
    """Resolve ``TRID3NT_GATING_WIDEN_THRESHOLD``, the poor-fit widen cutoff.

    A malformed or negative value -- negative would widen EVERY turn -- falls
    back to the calibrated default."""
    raw = os.environ.get("TRID3NT_GATING_WIDEN_THRESHOLD")
    if raw is None:
        return WIDEN_THRESHOLD_DEFAULT
    try:
        val = float(str(raw).strip())
    except (TypeError, ValueError):
        return WIDEN_THRESHOLD_DEFAULT
    return val if val >= 0.0 else WIDEN_THRESHOLD_DEFAULT


def should_widen_for_poor_fit(
    ranked: list[tuple[str, float]], threshold: float
) -> bool:
    """True iff the turn's TOP retrieval score is under ``threshold`` (poor fit).

    An empty ranking is NOT a widen signal: the gate already fails open there."""
    try:
        if not ranked:
            return False
        return float(ranked[0][1]) < float(threshold)
    except (TypeError, ValueError, IndexError):
        return False


# BENCH PRE-DISPATCH BLOCK HOOK: a session-scoped, bench-only gate that decides
# -- BEFORE the tool fn is invoked -- whether a model-picked tool should be
# EXECUTED, BLOCKED as a wrong pick, or BLOCKED as a deliberately-not-executed
# correct pick. Blocking server-side and pre-dispatch is what makes it airtight:
# a client-side cancel lets the blocked tool briefly START before it lands.
# Armed only in bench mode via the session-config path; absent (the field is
# None) is normal operation with ZERO dispatch overhead.

#: Typed function-response error_code for a NON-MEMBER (wrong) tool pick that
#: was blocked without executing. The routing_sweep grader reads this off the
#: tool-io function_response and lands SELECTED_WRONG_BLOCKED / FALSE_POSITIVE.
BENCH_BLOCKED_WRONG_PICK = "BENCH_BLOCKED_WRONG_PICK"
#: Typed function-response error_code for a CORRECT (member) tool pick in the
#: block_at_invocation tier: validated but deliberately not executed. Grades
#: CORRECT_BLOCKED client-side.
BENCH_BLOCKED_CORRECT = "BENCH_BLOCKED_CORRECT"


@dataclass(frozen=True)
class BenchBlockConfig:
    """The armed bench block config: the three tool-name sets ride together."""

    #: The record's ACCEPTABLE picks. A tool outside all three sets is a WRONG
    #: pick.
    allow: frozenset[str] = field(default_factory=frozenset)
    #: The routing MECHANISM tools, which ride THROUGH and execute normally so
    #: the model can discover its way to the pick.
    always_allowed: frozenset[str] = field(default_factory=frozenset)
    #: Member picks that must be validated but NOT executed.
    block_at_invocation: frozenset[str] = field(default_factory=frozenset)


class BenchBlockedError(RuntimeError):
    """Raised (bench mode only) to block a tool at dispatch without executing.

    ``retryable=False`` -- a deliberate terminal outcome, never a transient
    fault; ``blocked_class`` is what the caller ends the turn on."""

    retryable = False

    def __init__(self, blocked_class: str, tool_name: str) -> None:
        self.blocked_class = blocked_class
        self.tool_name = tool_name
        # An INSTANCE attribute, not a class one: the tool-result summarizer
        # harvests it into the function response the grader reads.
        self.error_code = (
            BENCH_BLOCKED_WRONG_PICK
            if blocked_class == "wrong_pick"
            else BENCH_BLOCKED_CORRECT
        )
        super().__init__(
            f"bench block ({blocked_class}) for tool {tool_name!r}: not executed"
        )


def _name_frozenset(value: Any) -> frozenset[str]:
    """Coerce a raw JSON list/sequence of names into a clean frozenset[str]."""
    if not isinstance(value, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(n for n in value if isinstance(n, str) and n)


def parse_bench_block_config(payload: dict) -> BenchBlockConfig | None:
    """Parse a bench block config off a raw ``session-config`` payload dict.

    ``None`` when the key is absent, which is NOT a disarm -- the caller leaves
    whatever is armed alone; never raises, a malformed shape gives empty sets."""
    if not isinstance(payload, dict):
        return None
    # Read off the raw dict defensively, so this stays forward-compatible with
    # the typed contract the framework lane owns.
    raw = payload.get("bench_tool_block")
    if not isinstance(raw, dict):
        return None
    return BenchBlockConfig(
        allow=_name_frozenset(raw.get("allow")),
        always_allowed=_name_frozenset(raw.get("always_allowed")),
        block_at_invocation=_name_frozenset(raw.get("block_at_invocation")),
    )


def bench_block_decision(cfg: Any, tool_name: str) -> str | None:
    """The pre-dispatch fate of ``tool_name``: execute, wrong_pick, correct_blocked.

    ``None`` executes normally; never raises, so a non-config ``cfg`` executes."""
    if not isinstance(cfg, BenchBlockConfig):
        return None
    if tool_name in cfg.always_allowed:
        return None
    # ``block_at_invocation`` defines the block tier and is authoritative for a
    # correct-block, so it is tested before allow-membership: a tool in that set
    # is a member by construction.
    if tool_name in cfg.block_at_invocation:
        return "correct_blocked"
    if tool_name not in cfg.allow:
        return "wrong_pick"
    return None
