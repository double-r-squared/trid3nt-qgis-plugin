"""Tool-dispatch machinery: progress accounting, gate-expander name sets, and
terminal-composer classification.

Every helper here reads only the tool registry and the contracts, never
``SessionState``."""

from __future__ import annotations

import logging
from typing import Any

from trid3nt_contracts.execution import LayerURI

from trid3nt_server.tools import TOOL_REGISTRY

logger = logging.getLogger("trid3nt_server.server")


#: Result keys that mark a dispatch as having PRODUCED a real artifact. A round
#: producing one of these is ADVANCING the Case even if the model repeats the
#: same call, so it runs to the step cap rather than being watchdog-aborted; a
#: bare-ack wedge carries none of them and loads the no-progress streak.
_PROGRESS_RESULT_KEYS: tuple[str, ...] = (
    "layer_id",
    "uri",
    "layer_uri",
    "feature_count",
)


def _dispatch_made_progress(result: Any) -> bool:
    """True iff one dispatch produced a real artifact: any ``LayerURI``, or a
    dict carrying a layer, handle or feature signal. A bare ack, ``None``, a
    primitive or an empty dict is the no-op-repeat shape the watchdog catches."""
    if isinstance(result, LayerURI):
        return True
    if isinstance(result, dict):
        return any(
            result.get(k) not in (None, "", [], {})
            for k in _PROGRESS_RESULT_KEYS
        )
    return False


#: How many CONSECUTIVE no-progress model rounds are tolerated AFTER a terminal
#: composer delivered its artifact before the turn concludes cleanly. A round
#: that produces genuine follow-up work RESETS the streak, so a legitimate
#: multi-deliverable flow is never cut off. This is not the runaway guard: a
#: turn that never produced a deliverable still runs to the cap.
_POST_DELIVERABLE_WRAPUP_ROUNDS: int = 2

#: The one-time wrap-up directive stamped onto a terminal composer's
#: function_response the moment it delivers (see ``_is_terminal_composer``).
_DELIVERABLE_COMPLETE_DIRECTIVE: str = (
    "DELIVERABLE COMPLETE: this run produced its primary result and any "
    "layers are already published to the user's map. Unless the user "
    "explicitly asked for ADDITIONAL analysis beyond this, do NOT call more "
    "tools -- give a brief (1-3 sentence) final summary of what was produced "
    "and stop. Calling further tools now will not improve the answer."
)

#: EMPTY-COMPLETION RETRY: a round with zero tool calls AND zero non-whitespace
#: text is retried once with a corrective nudge appended, bounded by this cap so
#: an always-empty model can never loop forever. Scoped to the local provider
#: path: a legitimately empty round from another provider must not change.
_EMPTY_COMPLETION_RETRY_CAP: int = 2

#: The corrective user-role nudge appended before a retried empty round: either
#: act or answer, never another empty message.
_EMPTY_COMPLETION_NUDGE: str = (
    "Your previous response was empty. Either call the appropriate tool to "
    "fulfill the request, or reply with your answer. Do not return an empty "
    "message."
)

#: The max number of NEW tool names the tool-search results may add to a turn's
#: visible gate, summed across the turn: a chatty search must not re-expand the
#: gate back toward the full catalog it was meant to trim.
_DISCOVERY_EXPAND_CAP: int = 8


def _tool_search_tool_names() -> frozenset[str]:
    """The registered name(s) of the tool-search tool, by registry lookup off
    its own metadata rather than a literal; a resolution fault yields the empty
    set and the expand simply no-ops."""
    names: set[str] = set()
    try:
        from trid3nt_server.tools.search.search_tools.search_tools import _SEARCH_TOOLS_METADATA

        if getattr(_SEARCH_TOOLS_METADATA, "name", None):
            names.add(_SEARCH_TOOLS_METADATA.name)
    except Exception:  # noqa: BLE001 -- module shape drift must not break dispatch
        logger.debug("discovery-expand: search_tools metadata lookup failed",
                     exc_info=True)
    for _legacy in ("discover_dataset",):
        if _legacy in TOOL_REGISTRY:
            names.add(_legacy)
    return frozenset(names)


def _default_declarable_registry() -> dict[str, Any]:
    """The DEFAULT per-turn declarable tool set, resolved by registry lookup:
    every tool except the ``catalog`` and ``internal`` tiers, which are
    registry-resolvable but never declared to the model."""
    _reg = {
        name: entry
        for name, entry in TOOL_REGISTRY.items()
        if getattr(entry.metadata, "tier", "general")
        not in ("catalog", "internal")
    }
    return _reg


def _gate_expander_tool_names() -> frozenset[str]:
    """The gate-expanders: calling one widens the turn's visible gate with the
    tool names its result names."""
    return _tool_search_tool_names()


def _tool_names_from_search_result(result: Any) -> list[str]:
    """Extract the ranked tool names from a gate-expander result, in listing
    order and de-duplicated; a malformed or partial entry is skipped rather than
    raised on."""
    if not isinstance(result, dict):
        return []
    rows = result.get("results")
    if not isinstance(rows, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = row.get("tool_name")
        if isinstance(name, str) and name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _is_terminal_composer(tool_name: str) -> bool:
    """True iff ``tool_name`` is a top-level run-a-model composer, whose return
    IS the answer; a helper that computes an intermediate is excluded, because
    drawing geometry or a profile is mid-pipeline, not a deliverable."""
    entry = TOOL_REGISTRY.get(tool_name)
    if entry is None:
        return False
    # Engine templates carry deliverable-producing names that do not start with
    # ``run_``, and a completed template IS a turn-ending deliverable, so the
    # template tier latches too; otherwise the wrap-up never fires and the turn
    # spins to the loop cap.
    is_workflow_dispatch = (
        getattr(entry.metadata, "source_class", None) == "workflow_dispatch"
    )
    is_template = getattr(entry.metadata, "tier", "general") == "template"
    return is_workflow_dispatch and (tool_name.startswith("run_") or is_template)
