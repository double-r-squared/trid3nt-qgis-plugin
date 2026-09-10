"""Turn orchestration: candidate emission, routing/stage labels, per-turn dispatch + persist."""

from __future__ import annotations

import asyncio
import os
import re
import logging
from trid3nt_contracts import new_ulid, now_utc
from trid3nt_contracts.ws import AgentMessageChunkPayload, SessionStatePayload
from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.emission.pipeline_emitter import current_turn_case
from trid3nt_server.main import MAX_TURNS_PER_SESSION
from trid3nt_server.server.config import _ambiguity_margin_threshold, _tool_choice_timeout_s
from trid3nt_server.server.dispatch.aoi import _bbox_overlaps
from trid3nt_server.server.dispatch.emitter import _ensure_emitter
from trid3nt_server.server.dispatch.persist import _persist_chat_turn
from trid3nt_server.server.interactions import _pop_pending_tool_choice, _register_pending_tool_choice
from trid3nt_server.server.session.case_state import _persist_session_active_case
from trid3nt_server.server.session.state import SessionState, _CASE_SYNC_NEVER
from trid3nt_server.server.spatial import _coerce_bbox4
from trid3nt_server.server.turn.cases import _auto_create_case_from_root, _emit_auto_case_open, _sync_case_context
from trid3nt_server.server.turn.wire import _new_envelope, _session_safe_send
from typing import Any
from websockets.asyncio.server import ServerConnection

logger = logging.getLogger("trid3nt_server.server")

# ---------------------------------------------------------------------------
# Turn config seams. Every mechanism here carries an env kill-switch, so a live
# regression can be flipped off without a code change.
# ---------------------------------------------------------------------------


def _session_routing_mode(state: "SessionState") -> str:
    """Routing-visibility mode for this session: the per-session setting, else
    the env default, else auto. Consent gates are NEVER mode-dependent - the
    mode governs tool-selection VISIBILITY only."""
    mode = getattr(state, "routing_mode", None)
    if isinstance(mode, str) and mode in ("auto", "ask"):
        return mode
    env = (os.environ.get("TRID3NT_MODE") or "auto").strip().lower()
    return env if env in ("auto", "ask") else "auto"

#: Max candidates surfaced on one tool-candidates card (avoid flooding).
_TOOL_CANDIDATES_MAX = 4

#: Continuation nudge, injected at most ONCE per turn as user-role content when
#: a turn ends with tool results but no assistant text since the last tool
#: round, or only geocoded while the user asked for data or analysis.
_CONTINUATION_NUDGE: str = (
    "You have tool results but have not answered the user yet. Summarize "
    "the results for the user now, and if their requested data or analysis "
    "is not complete, continue with the appropriate tool calls."
)

#: Data/analysis-intent heuristic for the bare-geocode backstop. Deliberately
#: broad verbs/nouns -- a pure "where is X" locate ask matches none of these.
_DATA_INTENT_RE = re.compile(
    r"\b(show|display|map|fetch|get|load|download|overlay|plot|chart|graph|"
    r"visuali[sz]e|analy[sz]e|analysis|model|simulat\w*|comput\w*|calculat\w*|"
    r"estimat\w*|assess\w*|data|imagery|satellite|layer|flood\w*|fire|smoke|"
    r"earthquake|rainfall|precipitation|storm|surge|wind|population|"
    r"buildings?|roads?|elevation|terrain|dem|landcover|damage|risk|hazard|"
    r"depth|extent|inundat\w*)\b",
    re.IGNORECASE,
)

def _asks_for_data_or_analysis(user_text: Any) -> bool:
    """True when the user's message asks for data/analysis (not a bare locate)."""
    return bool(isinstance(user_text, str) and _DATA_INTENT_RE.search(user_text))

#: Analysis-flow stages in pipeline order. The label derivation tie-breaks
#: toward the EARLIEST stage, so a multi-step turn reads as a forward march.
_STAGE_ORDER: tuple[str, ...] = (
    "acquisition",
    "preprocessing",
    "analysis",
    "visualization",
)

def _stage_label_for_tool(tool_name: str) -> str:
    """The coarse analysis-flow stage for ONE tool name, by name prefix;
    anything unrecognized falls back to ``"tool-selection"``."""
    if tool_name.startswith(
        ("fetch_", "geocode_", "discover_", "catalog_", "search_")
    ):
        return "acquisition"
    if tool_name.startswith(
        ("clip_", "merge_", "fill_", "cut_", "import_", "digitize_", "extract_")
    ):
        return "preprocessing"
    if tool_name.startswith(
        ("publish_", "generate_", "export_", "zoom", "compose_", "chart", "plot")
    ):
        return "visualization"
    if tool_name.startswith(
        ("compute_", "run_", "model_", "spatial_", "query_", "analyze_", "aggregate_")
    ) or tool_name.startswith("code_exec"):
        return "analysis"
    return "tool-selection"

def _stage_label_for_candidates(ranked: list[tuple[str, float]]) -> str:
    """Derive one ``stage_label`` from the TOP candidates: the plurality stage,
    tie-broken toward the earliest, since a single rank-1 pick is a brittle
    signal. An all-fallback candidate set yields the fallback."""
    counts: dict[str, int] = {}
    for name, _score in ranked[:_TOOL_CANDIDATES_MAX]:
        stage = _stage_label_for_tool(name)
        if stage == "tool-selection":
            continue
        counts[stage] = counts.get(stage, 0) + 1
    if not counts:
        return "tool-selection"
    # Plurality; ties broken toward the earliest pipeline stage (_STAGE_ORDER).
    best = max(
        counts.items(),
        key=lambda kv: (kv[1], -_STAGE_ORDER.index(kv[0])),
    )
    return best[0]

def _tool_summary_line(entry: Any) -> str:
    """First docstring line of a registered tool, for the candidates card."""
    doc = getattr(getattr(entry, "fn", None), "__doc__", None) or ""
    first = doc.strip().splitlines()[0].strip() if doc.strip() else ""
    return first[:140]

def _geocode_drift_note(
    args: Any, geocode_bbox: Any, active_aoi: Any
) -> str | None:
    """Warning text when a call's bbox intersects NEITHER the turn's geocoded
    bbox nor the active AOI, else ``None``. Advisory only: the dispatch is never
    blocked, and a call with no coercible bbox is skipped."""
    if not isinstance(args, dict):
        return None
    for key in ("bbox", "aoi_bbox"):
        cand = _coerce_bbox4(args.get(key))
        if cand is None:
            continue
        if _bbox_overlaps(cand, geocode_bbox):
            return None
        if active_aoi is not None and _bbox_overlaps(cand, active_aoi):
            return None
        gc = _coerce_bbox4(geocode_bbox)
        return (
            f"WARNING: this call's {key} {[round(v, 4) for v in cand]} does "
            f"not intersect the geocoded location bbox "
            f"{[round(v, 4) for v in gc] if gc else geocode_bbox}"
            + (" or the active AOI" if active_aoi is not None else "")
            + ". The area of interest may have drifted -- verify the "
            "coordinates before relying on this result."
        )
    return None

def _union_pinned_tool(
    pinned: str | None,
    retrieval_registry: dict,
    state: SessionState,
) -> dict:
    """Union a user-pinned tool into both the monotonic visible set and the
    retrieval-visible registry, so it stays visible and its declaration is
    built. Returns a NEW registry only when the pin widened it."""
    if pinned and pinned in TOOL_REGISTRY:
        state.visible_tools.add(pinned)
        if pinned not in retrieval_registry:
            retrieval_registry = dict(retrieval_registry)
            retrieval_registry[pinned] = TOOL_REGISTRY[pinned]
    return retrieval_registry

async def _handle_max_turns_reached(
    websocket: ServerConnection, state: SessionState
) -> None:
    """Emit the cap-hit envelopes - the session state carrying the cap status,
    then a closing message - instead of dispatching, once the session's turn
    count exceeds the limit. No tool call runs."""
    _ensure_emitter(websocket, state)
    # Re-emit session-state with the cap status so the client can render a
    # "session full" indicator.
    closing_payload = SessionStatePayload(
        chat_history=state.chat_history,
        status="max_turns_reached",
    )
    await websocket.send(
        _new_envelope("session-state", state.session_id, closing_payload)
    )
    # Send a closing agent-message-chunk so the user sees a human-readable
    # explanation in the chat panel.
    message_id = new_ulid()
    closing_text = (
        "This session has reached its turn limit "
        f"({MAX_TURNS_PER_SESSION} turns). "
        "No further tool calls will be dispatched. "
        "Start a new session to continue working."
    )
    await websocket.send(
        _new_envelope(
            "agent-message-chunk",
            state.session_id,
            AgentMessageChunkPayload(
                message_id=message_id, delta=closing_text, done=False
            ),
        )
    )
    await websocket.send(
        _new_envelope(
            "agent-message-chunk",
            state.session_id,
            AgentMessageChunkPayload(message_id=message_id, delta="", done=True),
        )
    )
    logger.info(
        "max-turns-reached session=%s turn_count=%d limit=%d",
        state.session_id,
        state.turn_count,
        MAX_TURNS_PER_SESSION,
    )

async def _maybe_emit_tool_candidates(
    websocket: ServerConnection,
    state: SessionState,
    user_text: str,
    exclude_tools: "set[str] | None" = None,
) -> tuple[str | None, list[str]]:
    """Surface the retrieval-ranked candidates BEFORE dispatch and wait, under a
    bounded timeout, for the user's pick; returns that pin, if any, and the
    notes to ride into the turn."""
    # It fires in ask mode, or in auto when the top-1 against top-2 margin is
    # under the ambiguity threshold. On timeout or fault the turn proceeds
    # AUTONOMOUSLY: the picker is an optimization, never a wall.
    mode = _session_routing_mode(state)
    threshold = _ambiguity_margin_threshold()
    if mode != "ask" and threshold <= 0.0:
        return None, []

    from trid3nt_server.tools.search.tool_retrieval import retrieve_ranked_tools

    ranked = retrieve_ranked_tools(user_text, k=8)
    if exclude_tools:
        # Drop this turn's already-dispatched tools, so each later round
        # advances the stage label instead of re-offering the same picks.
        ranked = [(n, s) for (n, s) in ranked if n not in exclude_tools]
    if not ranked:
        # Cold index / no match / all excluded: nothing to offer -- autonomous.
        return None, []

    reason: str | None = None
    if mode == "ask":
        reason = "ask_mode"
    elif len(ranked) >= 2 and ranked[0][1] > 0.0:
        margin = (ranked[0][1] - ranked[1][1]) / ranked[0][1]
        if margin < threshold:
            reason = "ambiguity"
    if reason is None:
        return None, []

    candidates = [
        {
            "tool_name": name,
            "summary": _tool_summary_line(TOOL_REGISTRY.get(name)),
            "score": round(float(score), 6),
        }
        for name, score in ranked[:_TOOL_CANDIDATES_MAX]
    ]
    timeout_s = _tool_choice_timeout_s()
    request_id = new_ulid()
    payload = {
        "request_id": request_id,
        "stage_label": _stage_label_for_candidates(ranked),
        "candidates": candidates,
        "reason": reason,
        "timeout_s": timeout_s,
    }

    import json as _json

    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _register_pending_tool_choice(state.session_id, request_id, fut)
    try:
        await _session_safe_send(
            websocket,
            state.session_id,
            _json.dumps(
                {
                    "type": "tool-candidates",
                    "id": new_ulid(),
                    "ts": now_utc().isoformat().replace("+00:00", "Z"),
                    "session_id": state.session_id,
                    "case_id": current_turn_case(),
                    "payload": payload,
                }
            ),
        )
        logger.info(
            "tool-candidates emitted session=%s request_id=%s reason=%s "
            "n=%d top=%s timeout=%.0fs",
            state.session_id,
            request_id,
            reason,
            len(candidates),
            ranked[0][0],
            timeout_s,
        )
        reply = await asyncio.wait_for(fut, timeout=timeout_s)
    except asyncio.TimeoutError:
        logger.info(
            "tool-candidates TIMEOUT session=%s request_id=%s (%.0fs) -- "
            "proceeding autonomously",
            state.session_id,
            request_id,
            timeout_s,
        )
        return None, [
            "(The tool-selection card was not answered in time -- proceed "
            "autonomously with your best tool choice.)"
        ]
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 -- the picker must never break the turn
        logger.warning(
            "tool-candidates gate fault session=%s -- proceeding autonomously",
            state.session_id,
            exc_info=True,
        )
        return None, []
    finally:
        _pop_pending_tool_choice(request_id)

    # Defensive dict parse (contracts lane declares the typed model later).
    tool_name: str | None = None
    free_text: str | None = None
    if isinstance(reply, dict):
        tn = reply.get("tool_name")
        ft = reply.get("free_text")
        if isinstance(tn, str) and tn.strip():
            tool_name = tn.strip()
        if isinstance(ft, str) and ft.strip():
            free_text = ft.strip()

    notes: list[str] = []
    pinned: str | None = None
    if tool_name:
        if tool_name in TOOL_REGISTRY:
            pinned = tool_name
            notes.append(
                f"[User tool choice] Use the tool '{tool_name}' for this "
                "request."
            )
            logger.info(
                "tool-choice PINNED session=%s request_id=%s tool=%s",
                state.session_id,
                request_id,
                tool_name,
            )
        else:
            logger.warning(
                "tool-choice named unknown tool %r session=%s -- ignored",
                tool_name,
                state.session_id,
            )
    if free_text:
        notes.append(f"[User clarification] {free_text}")
        logger.info(
            "tool-choice free-text session=%s request_id=%s len=%d",
            state.session_id,
            request_id,
            len(free_text),
        )
    return pinned, notes

async def _prepare_user_turn(
    websocket: ServerConnection,
    state: SessionState,
    text: str,
    *,
    client_case_id: str | None = None,
) -> tuple[str, dict] | None:
    """Pre-dispatch sequence for one user message, returning the parsed
    directive or ``None`` for the model path; it runs BEFORE the turn task is
    created, so the turn observes the final Case context."""
    # The order is load-bearing: rebind the active-Case pointer to the client's
    # stamp, sync this connection to that Case, auto-create a Case for a
    # non-directive prompt that has none, pin the turn, persist the user row,
    # and only then emit the open for an auto-created Case.
    # The client's stamped Case is the authority for this turn, rebound before
    # anything reads the pointer, so the whole turn - context sync, AOI, every
    # write - follows the Case the user is actually viewing rather than a
    # pointer that drifted while the socket reconnected. The sync marker is
    # invalidated so the corrected Case's history and layers reload, and the
    # pointer is persisted so it survives a restart.
    if client_case_id is not None and client_case_id != state.active_case_id:
        logger.info(
            "user-message re-binding active case session=%s server=%s client=%s",
            state.session_id,
            state.active_case_id,
            client_case_id,
        )
        state.active_case_id = client_case_id
        state.case_context_synced_to = _CASE_SYNC_NEVER
        await _persist_session_active_case(state, client_case_id)
    await _sync_case_context(websocket, state)
    directive = _parse_invoke_directive(text)
    auto_case_id: str | None = None
    if directive is None and state.active_case_id is None:
        auto_case_id = await _auto_create_case_from_root(
            websocket, state, text
        )
    # Pin the turn's Case binding NOW, after the auto-create hand-off and
    # before the first write: everything this turn persists follows the pin, and
    # a mid-stream case switch must not re-aim it.
    state.current_turn_case_id = state.active_case_id
    await _persist_chat_turn(state, role="user", content=text)
    if auto_case_id is not None:
        await _emit_auto_case_open(websocket, state, auto_case_id)
    return directive

def _parse_invoke_directive(text: str) -> tuple[str, dict] | None:
    """Parse an ``/invoke <tool_name> <json-params>`` directive into its tool
    and params, else ``None``. The directive shape is a debug surface and
    deliberately not part of the wire protocol."""
    if not text.startswith("/invoke "):
        return None
    rest = text[len("/invoke ") :].strip()
    # Split on first whitespace: "<tool_name> <json>"
    parts = rest.split(None, 1)
    if not parts:
        return None
    tool_name = parts[0]
    if len(parts) == 1:
        return tool_name, {}
    import json as _json

    try:
        params = _json.loads(parts[1])
        if not isinstance(params, dict):
            return None
    except Exception:  # noqa: BLE001
        return None
    return tool_name, params
