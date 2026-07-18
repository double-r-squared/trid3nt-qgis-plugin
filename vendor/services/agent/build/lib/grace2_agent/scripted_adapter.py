"""Scripted (replay) model-provider adapter -- a ZERO-COST deterministic LLM stand-in.

``MODEL_PROVIDER=scripted`` (aliases ``replay`` / ``fake``) makes the agent loop
REPLAY a canned transcript of tool calls instead of calling Bedrock, so the FULL
agent loop + tool dispatch + WebSocket + web UI can be exercised end-to-end with
NO Bedrock spend and fully deterministic behaviour. This is the cheap test/dev
sandbox: verify that a tool / plugin-wrap / engine is OPERABLE through the real
agent pipeline without paying for (or depending on) a live model.

It yields the SAME ``StreamEvent`` union the Gemini/Bedrock adapters yield
(``TextDeltaEvent`` / ``FunctionCallEvent`` / ``UsageMetadataEvent``), so
``server.py``'s dispatch loop, the per-turn validator, the PipelineEmitter, and
the web UI are all untouched -- this is a drop-in third provider on the existing
``MODEL_PROVIDER`` seam (next to ``bedrock_adapter.stream_bedrock``).

The transcript is a list of TURNS. Each turn optionally emits assistant text and
optionally ONE tool call. The adapter selects which turn to emit by counting the
ASSISTANT (``model``-role) turns already present in ``contents`` -- so it advances
exactly one script turn per agent-loop iteration as tool results feed back. A
turn with no ``tool_call`` is terminal (the assistant just speaks and stops).

Transcript sources, in precedence order:
  1. ``set_script(turns)`` -- an in-process override (used by tests).
  2. ``GRACE2_SCRIPTED_TRANSCRIPT_JSON`` -- inline JSON string of the turns list
     (or a ``{"turns": [...]}`` object).
  3. ``GRACE2_SCRIPTED_TRANSCRIPT`` -- path to a JSON file with the same shape.
  4. Fallback -- a single text turn so the loop terminates gracefully.

Turn shape::

    {"text": "I'll geocode that.", "tool_call": {"name": "geocode_place",
                                                  "args": {"query": "Mexico Beach"}}}
    {"text": "Done -- peak Hs is 8 m offshore."}   # terminal turn, no tool_call
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator
from typing import Any

from .adapter import FunctionCallEvent, StreamEvent, TextDeltaEvent, UsageMetadataEvent

logger = logging.getLogger("grace2_agent.scripted_adapter")

__all__ = [
    "model_provider_is_scripted",
    "stream_scripted",
    "set_script",
    "clear_script",
    "load_script",
]

#: MODEL_PROVIDER values that select this adapter.
_SCRIPTED_PROVIDERS = frozenset({"scripted", "replay", "fake"})

#: In-process transcript override (tests set this; takes precedence over env).
_SCRIPT_OVERRIDE: list[dict[str, Any]] | None = None


def model_provider_is_scripted() -> bool:
    """True when ``MODEL_PROVIDER`` selects the scripted/replay adapter."""
    return (os.environ.get("MODEL_PROVIDER") or "").strip().lower() in _SCRIPTED_PROVIDERS


def set_script(turns: list[dict[str, Any]] | None) -> None:
    """Install an in-process transcript (tests). Pass ``None`` to clear."""
    global _SCRIPT_OVERRIDE
    _SCRIPT_OVERRIDE = list(turns) if turns is not None else None


def clear_script() -> None:
    """Remove any in-process transcript override."""
    set_script(None)


def _coerce_turns(raw: Any) -> list[dict[str, Any]]:
    """Accept either a bare list of turns or a ``{"turns": [...]}`` object."""
    if isinstance(raw, dict):
        raw = raw.get("turns", [])
    if not isinstance(raw, list):
        return []
    return [t for t in raw if isinstance(t, dict)]


def load_script() -> list[dict[str, Any]]:
    """Resolve the active transcript from the override / env, in precedence order."""
    if _SCRIPT_OVERRIDE is not None:
        return _SCRIPT_OVERRIDE
    inline = os.environ.get("GRACE2_SCRIPTED_TRANSCRIPT_JSON")
    if inline:
        try:
            return _coerce_turns(json.loads(inline))
        except Exception as exc:  # noqa: BLE001
            logger.warning("scripted: bad GRACE2_SCRIPTED_TRANSCRIPT_JSON: %s", exc)
            return []
    path = os.environ.get("GRACE2_SCRIPTED_TRANSCRIPT")
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                return _coerce_turns(json.load(fh))
        except Exception as exc:  # noqa: BLE001
            logger.warning("scripted: could not read transcript %s: %s", path, exc)
            return []
    return []


def _role_of(content: Any) -> str | None:
    """Best-effort role extraction from a genai Content object OR a plain dict."""
    if isinstance(content, dict):
        return content.get("role")
    return getattr(content, "role", None)


def _turn_index(contents: Any) -> int:
    """The index of the NEXT assistant turn = count of ``model``-role contents.

    On the first call ``contents`` is ``[user]`` -> 0 model turns -> turn 0. After
    a tool call + its result feed back, one ``model`` (the prior tool-call turn)
    is present -> turn 1, and so on. Robust to dict- or object-shaped contents.
    """
    if not isinstance(contents, (list, tuple)):
        return 0
    return sum(1 for c in contents if _role_of(c) == "model")


async def stream_scripted(
    *,
    contents: Any,
    tool_declarations: Any = None,
    system_prompt: str | None = None,
    model: str | None = None,
) -> AsyncIterator[StreamEvent]:
    """Replay one transcript turn as ``StreamEvent``s (no model call, no cost).

    Mirrors the ``stream_bedrock`` keyword signature so it is a drop-in on the
    ``MODEL_PROVIDER`` switch. ``tool_declarations`` / ``system_prompt`` / ``model``
    are accepted for signature parity and intentionally ignored (the transcript
    is authored, not generated).
    """
    turns = load_script()
    idx = _turn_index(contents)

    if idx >= len(turns):
        # Past the end of the script (or empty script): emit a terminal line so
        # the agent loop has assistant text and STOPS (no tool call -> no further
        # iteration). Never loop forever.
        msg = (
            "[scripted adapter] transcript exhausted."
            if turns
            else "[scripted adapter] no transcript configured (set MODEL_PROVIDER=scripted "
            "+ GRACE2_SCRIPTED_TRANSCRIPT[_JSON] or call set_script())."
        )
        yield TextDeltaEvent(delta=msg)
        yield _usage_event()
        return

    turn = turns[idx]

    text = turn.get("text")
    if text:
        yield TextDeltaEvent(delta=str(text))

    tool_call = turn.get("tool_call")
    if isinstance(tool_call, dict) and tool_call.get("name"):
        args = tool_call.get("args")
        yield FunctionCallEvent(
            name=str(tool_call["name"]),
            call_id=str(tool_call.get("call_id") or f"scripted-{idx}"),
            args=args if isinstance(args, dict) else {},
        )

    yield _usage_event()


def _usage_event() -> UsageMetadataEvent:
    """A zero-cost usage record (scripted turns consume no model tokens)."""
    return UsageMetadataEvent(
        cached_content_token_count=0,
        total_token_count=0,
        prompt_token_count=0,
        candidates_token_count=0,
        cache_hit=False,
    )
