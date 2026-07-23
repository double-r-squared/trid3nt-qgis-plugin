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
    UNION the META floor (hot set + search_data_catalog/fetch_from_catalog + web_fetch --
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
from dataclasses import dataclass, field
from typing import Any

from trid3nt_server.categories import HOT_SET_TOOLS

__all__ = [
    "TOOL_GATING_TOPK_DEFAULT",
    "META_TOOL_FLOOR",
    "gating_topk",
    "named_tools_in_text",
    "gate_tool_registry",
    # POOR-FIT WIDENING (task 3)
    "WIDEN_THRESHOLD_DEFAULT",
    "WIDEN_K",
    "gating_widen_threshold",
    "should_widen_for_poor_fit",
    # BENCH pre-dispatch block hook (task 1)
    "BENCH_BLOCKED_WRONG_PICK",
    "BENCH_BLOCKED_CORRECT",
    "BenchBlockConfig",
    "BenchBlockedError",
    "parse_bench_block_config",
    "bench_block_decision",
]

logger = logging.getLogger("trid3nt_server.tool_gating")

#: Default top-k for the openai-provider tool gate (bench recommendation).
TOOL_GATING_TOPK_DEFAULT = 24

#: The always-include META floor: the hot set (categories.py conventions --
#: search_tools, spatial_query, publish_layer, code_exec_request,
#: geocode_location, zoom/list utilities, ...) plus the catalog discovery
#: pair and web_fetch, which register outside tools/__init__ and are the
#: "find anything else" escape hatches a gated model must always hold.
META_TOOL_FLOOR: frozenset[str] = frozenset(HOT_SET_TOOLS) | frozenset(
    {
        "search_data_catalog",
        "fetch_from_catalog",
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


# ===========================================================================
# POOR-FIT WIDENING (task 3): when a turn's TOP retrieval score is under a
# calibrated threshold, the ranking is uncertain -- widen that turn's gate k
# once (24 -> 40) so recall does not silently drop on an ambiguous / vague
# ask. Over-inclusion is cheap; hiding the needed tool on a hard query is a
# silent break (the same asymmetry the whole gate is designed around).
#
# CALIBRATION (measured, LANE A 2026-07-22, offline against the ``hashed``
# deterministic dense fallback -- the local default when sentence-transformers
# is absent). retrieve_ranked_tools RRF top-1 score distributions over the
# routing_sweep input prompts + a degenerate poor-fit control set:
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
    """Resolve ``TRID3NT_GATING_WIDEN_THRESHOLD`` (default WIDEN_THRESHOLD_DEFAULT).

    Read per-call so tests / runtime flips are honored. A malformed value (or a
    negative one, which would widen on EVERY turn -- never the intent) falls
    back to the calibrated default.
    """
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

    ``ranked`` is ``retrieve_ranked_tools``'s scored output for the turn. An
    empty ranking (cold index / no match) is NOT a poor-fit widen signal -- the
    whole gate already fails OPEN to the full registry on an empty ranking, so
    widening there is moot; return False. Never raises.
    """
    try:
        if not ranked:
            return False
        return float(ranked[0][1]) < float(threshold)
    except (TypeError, ValueError, IndexError):
        return False


# ===========================================================================
# BENCH PRE-DISPATCH BLOCK HOOK (task 1): a session-scoped, bench-only gate
# that decides -- BEFORE the tool fn is invoked -- whether a model-picked tool
# should be EXECUTED, BLOCKED as a wrong pick, or BLOCKED as a deliberately-not-
# executed correct pick. Armed only in bench mode via the session-config path;
# absent (the field is None) = normal operation with ZERO dispatch overhead.
#
# This replaces the routing_sweep engine's racy v1 client-side "cancel on the
# first material tool-call" policy (the blocked tool could briefly START before
# the cancel landed) with a server-side block that is airtight BEFORE any fetch.

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
    """The armed bench block config (all three tool-name sets ride together).

    * ``allow`` -- the record's ACCEPTABLE picks (the correct tools). A picked
      tool outside this set (and outside ``always_allowed`` /
      ``block_at_invocation``) is a WRONG pick.
    * ``always_allowed`` -- the routing MECHANISM tools (discovery / bookkeeping
      meta-tools + any record-level always_allowed): they ride THROUGH and
      execute normally so the model can actually discover its way to the pick.
    * ``block_at_invocation`` -- member picks that must be validated but NOT
      executed (the block tier: the deliberately-not-run correct answer).
    """

    allow: frozenset[str] = field(default_factory=frozenset)
    always_allowed: frozenset[str] = field(default_factory=frozenset)
    block_at_invocation: frozenset[str] = field(default_factory=frozenset)


class BenchBlockedError(RuntimeError):
    """Raised (bench mode only) to block a tool at dispatch without executing.

    Carries the typed ``error_code`` (BENCH_BLOCKED_WRONG_PICK /
    BENCH_BLOCKED_CORRECT) as an INSTANCE attribute so
    ``adapter.summarize_tool_result`` harvests it into the function-response the
    grader reads. ``retryable=False``: a bench block is a deliberate terminal
    outcome, never a transient fault to retry. ``blocked_class`` is the coarse
    decision ("wrong_pick" | "correct_blocked") the server reads to decide
    whether to end the turn.
    """

    retryable = False

    def __init__(self, blocked_class: str, tool_name: str) -> None:
        self.blocked_class = blocked_class
        self.tool_name = tool_name
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

    Reads the namespaced ``bench_tool_block`` key DEFENSIVELY (the framework
    lane owns the typed contract; this stays forward-compatible with a raw
    dict). Returns:

      * a ``BenchBlockConfig`` when the key holds an object with the three
        name sets (armed),
      * ``None`` when the key is absent (leave whatever is already armed
        untouched -- the caller distinguishes "absent" from "disarm"),

    Never raises: a malformed shape degrades to empty sets, never a crash.
    """
    if not isinstance(payload, dict):
        return None
    raw = payload.get("bench_tool_block")
    if not isinstance(raw, dict):
        return None
    return BenchBlockConfig(
        allow=_name_frozenset(raw.get("allow")),
        always_allowed=_name_frozenset(raw.get("always_allowed")),
        block_at_invocation=_name_frozenset(raw.get("block_at_invocation")),
    )


def bench_block_decision(cfg: Any, tool_name: str) -> str | None:
    """Decide the pre-dispatch fate of ``tool_name`` under the armed config.

    Returns one of:
      * ``None`` -- execute normally (an always-allowed mechanism tool, or a
        correct run-tier pick),
      * ``"wrong_pick"`` -- a non-member pick: block + end the turn,
      * ``"correct_blocked"`` -- a member pick in the block tier: validate args,
        then block WITHOUT executing.

    ``block_at_invocation`` is authoritative for a correct-block (it defines the
    block tier), so it is checked before the allow-membership test -- a tool in
    that set is by construction a member. Never raises (a non-config ``cfg``
    yields ``None`` = execute).
    """
    if not isinstance(cfg, BenchBlockConfig):
        return None
    if tool_name in cfg.always_allowed:
        return None
    if tool_name in cfg.block_at_invocation:
        return "correct_blocked"
    if tool_name not in cfg.allow:
        return "wrong_pick"
    return None
