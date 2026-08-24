"""Tool-dispatch machinery: progress accounting, gate-expander name sets, and
terminal-composer classification.

The low-coupling, session-free helpers the dispatch loop leans on: the
loop-watchdog progress witness (``_dispatch_made_progress`` +
``_PROGRESS_RESULT_KEYS``), the post-deliverable wrap-up / empty-completion /
discovery-expand knobs, the tool-search + gate-expander name-set resolvers, and
the terminal-composer classifier. Each reads only the tool registry / contracts
-- never ``SessionState`` -- so they extract as a clean leaf. ``_core``
re-imports every name so its bare-global references resolve unchanged and the
package facade re-exposes them at ``trid3nt_server.server.<name>``.

Deliberately NOT here (entangled -- flagged for a later extraction): the
user-decision gate coroutines (``_maybe_gate_on_payload_warning``,
``_gate_on_code_exec``, ``_gate_on_solver_confirm``, ``_gate_with_turn_memory``)
-- they emit on the websocket via ``_core``'s send/envelope plumbing
(``_new_envelope`` / ``_send_error`` / ``_session_safe_send``) and read
``SessionState`` audit/decision fields; and the ``_gate_wait_timeout`` seam,
pinned to ``_core`` by the ``inspect.getsource`` gate-wait guard.
"""

from __future__ import annotations

import logging
from typing import Any

from trid3nt_contracts.execution import LayerURI

from trid3nt_server.tools import TOOL_REGISTRY

logger = logging.getLogger("trid3nt_server.server")


#: Result keys that mark a dispatch as having PRODUCED a real artifact -- a
#: published / registered layer, a stored object, a feature set. Used by the
#: loop-watchdog progress witness: a round that produces one of these is
#: ADVANCING the Case (a new layer/handle appears) even if the model
#: pathologically repeats the same call, so it is allowed to run to the step
#: cap / loop-exhausted envelope rather than being watchdog-aborted. A
#: bare-ack wedge shape (``{"ok": True}`` re-issued forever) carries none of
#: these and so loads the no-progress streak.
_PROGRESS_RESULT_KEYS: tuple[str, ...] = (
    "layer_id",
    "wms_url",
    "uri",
    "layer_uri",
    "feature_count",
)


def _dispatch_made_progress(result: Any) -> bool:
    """True iff a single tool dispatch produced a real artifact.

    A ``LayerURI`` return (any subclass) is always progress -- a renderable
    layer was produced. A dict carrying a layer/handle/feature signal
    (:data:`_PROGRESS_RESULT_KEYS`) is progress. Everything else -- a bare ack
    (``{"ok": True}``), ``None``, a primitive, an empty dict -- is NOT progress:
    that is the no-op-repeat shape the watchdog must catch.
    """
    if isinstance(result, LayerURI):
        return True
    if isinstance(result, dict):
        return any(
            result.get(k) not in (None, "", [], {})
            for k in _PROGRESS_RESULT_KEYS
        )
    return False


#: How many CONSECUTIVE no-progress model rounds we tolerate AFTER a
#: terminal composer has delivered its artifact before concluding the turn
#: cleanly. Symptom without this: a SFINCS flood publishes its depth layer
#: and the model, having nothing left to do, keeps emitting unproductive
#: function calls until it trips ``MAX_TURN_ITERATIONS`` and emits a
#: (harmless but sloppy) ``loop_exhausted`` frame. Once the deliverable is
#: in hand we (a) stamp the composer's function_response with a one-time
#: wrap-up directive so a well-behaved model just summarizes and stops, and
#: (b) keep this small safety budget: if the model spins
#: ``_POST_DELIVERABLE_WRAPUP_ROUNDS`` rounds in a row without producing
#: anything new, we conclude the turn cleanly instead of letting it run to
#: the cap. A round that produces genuine follow-up work
#: (``_dispatch_made_progress``) RESETS the streak, so legitimate
#: multi-deliverable flows are never cut off. This is NOT the runaway
#: guard: a turn that never produced a terminal deliverable still runs to
#: the cap / watchdog exactly as before.
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

#: EMPTY-COMPLETION RETRY: the local qwen3 model occasionally returns a
#: round with ZERO tool calls AND ZERO non-whitespace text. This is NOT
#: context overflow (that is the compaction/clip guard) -- the model has
#: room and simply emits nothing. The loop RETRIES the round with a
#: corrective user-role nudge appended (production tool-runner pattern:
#: OpenAI tool-runner / LangChain retry-with-nudge, not a blind resend),
#: BOUNDED by this cap so an always-empty model can never loop forever
#: (same safety discipline as the loop watchdog). Scoped to the LOCAL
#: (MODEL_PROVIDER=openai) path only -- a legitimately empty Bedrock round
#: must NOT change.
_EMPTY_COMPLETION_RETRY_CAP: int = 2

#: The corrective user-role nudge appended to ``contents`` before a retried
#: empty round (OPEN-16). Plain instruction -- either act (tool) or answer;
#: never another empty message.
_EMPTY_COMPLETION_NUDGE: str = (
    "Your previous response was empty. Either call the appropriate tool to "
    "fulfill the request, or reply with your answer. Do not return an empty "
    "message."
)

#: DISCOVERY-EXPANDS-GATE (task 2): the max number of NEW tool names the
#: tool-search tool's results may add to a turn's visible gate, summed across
#: the whole turn. Bounds the widening so a chatty search cannot re-expand the
#: gate back toward the full catalog it was meant to trim.
_DISCOVERY_EXPAND_CAP: int = 8


def _tool_search_tool_names() -> frozenset[str]:
    """The registered name(s) of the tool-search (data-discovery) tool.

    Resolved by REGISTRY LOOKUP off the discovery module's own registration
    metadata (``search_tools``, formerly ``discover_dataset``) rather than a
    hardcoded literal, so the parallel rename lands transparently. Any legacy
    alias still present in the live registry is also honored. Never raises: a
    resolution fault yields the empty set (the expand simply no-ops).
    """
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
    """The DEFAULT per-turn declarable tool set.

    Engine templates (``sfincs_flood``,
    ``modflow_*``, ``openquake_psha``, ...) are ordinary retrieval-pool members
    and are declarable by default like any tool -- the deleted engine doors no
    longer gate them. Only ``catalog`` (catalog-surfacing experiment, arm-flagged;
    no tool carries it in the DEFAULT config) and ``internal`` (an absorbed
    in-process seam, e.g. fetch_copernicus_dem folded into fetch_dem --
    registry-resolvable but never declared to the model) are withheld.

    Resolved by REGISTRY LOOKUP (never a literal). Mirrors the pool-side filter
    in ``tools.search.tool_retrieval`` (fail-open dump) so the
    default-declaration path and the retrieval pool stay identical.
    """
    _reg = {
        name: entry
        for name, entry in TOOL_REGISTRY.items()
        if getattr(entry.metadata, "tier", "general")
        not in ("catalog", "internal")
    }
    return _reg


def _gate_expander_tool_names() -> frozenset[str]:
    """The gate-expanders: the tool-search (data-discovery) tool(s).

    A call to one of these expands the turn's visible gate with the tool names
    its result names (``results[].tool_name``). See
    ``_tool_names_from_search_result`` for the extraction and the dispatch
    post-processing for the union + cap. Templates are now ordinary registry
    members -- there is no separate "door" concierge layer removing the
    engine-door gate-expanders; templates are ordinary retrieval-pool tools now.
    """
    return _tool_search_tool_names()


def _tool_names_from_search_result(result: Any) -> list[str]:
    """Extract the ranked tool names from a gate-expander result payload.

    ``search_tools`` returns ``{"results": [{"tool_name": <name>, ...}, ...]}``.
    Returns the names in listing order (best first), de-duplicated. Tolerant of a
    malformed / partial shape -- a non-conforming entry is skipped, never raised
    on.
    """
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
    """True iff ``tool_name`` is a top-level run-a-model composer.

    A terminal composer is a ``run_*`` workflow-dispatch tool (the
    ``run_model_*`` / ``run_*_job`` / ``swmm_urban_flood`` /
    ``openquake_psha`` family) -- the deliverable-producing entry
    points whose successful return IS the answer the user asked for. Helper
    workflow-dispatch tools that merely compute an intermediate
    (``compute_cross_section``, ``request_spatial_input``, ...) are
    deliberately EXCLUDED by the ``run_`` prefix: drawing geometry or computing
    a profile is mid-pipeline, not a turn-ending deliverable.
    """
    entry = TOOL_REGISTRY.get(tool_name)
    if entry is None:
        return False
    # Engine TEMPLATES carry deliverable-producing
    # names that do NOT start with ``run_`` (``modflow_contaminant_plume`` et al.).
    # A completed template IS a turn-ending deliverable, so ALSO latch any
    # tier="template" workflow-dispatch tool - otherwise the crisp-end wrap-up +
    # post-deliverable idle reset never fire and the turn spins to the loop cap.
    is_workflow_dispatch = (
        getattr(entry.metadata, "source_class", None) == "workflow_dispatch"
    )
    is_template = getattr(entry.metadata, "tier", "general") == "template"
    return is_workflow_dispatch and (tool_name.startswith("run_") or is_template)
