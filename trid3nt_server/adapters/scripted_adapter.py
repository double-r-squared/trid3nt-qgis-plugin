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
  2. ``TRID3NT_SCRIPTED_TRANSCRIPT_JSON`` -- inline JSON string of the turns list
     (or a ``{"turns": [...]}`` object).
  3. ``TRID3NT_SCRIPTED_TRANSCRIPT`` -- path to a JSON file with the same shape.
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
from collections.abc import AsyncIterator, Callable
from typing import Any

from .adapter import FunctionCallEvent, StreamEvent, TextDeltaEvent, UsageMetadataEvent

logger = logging.getLogger("trid3nt_server.adapters.scripted_adapter")

__all__ = [
    "model_provider_is_scripted",
    "stream_scripted",
    "set_script",
    "clear_script",
    "load_script",
    # Test-harness (fake-provider) seam -- see the harness section below.
    "install_harness",
    "reset_harness",
    "harness_active",
    "harness_calls",
    "text_turn",
    "call_turn",
    "calls_turn",
    "raise_turn",
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
    inline = os.environ.get("TRID3NT_SCRIPTED_TRANSCRIPT_JSON")
    if inline:
        try:
            return _coerce_turns(json.loads(inline))
        except Exception as exc:  # noqa: BLE001
            logger.warning("scripted: bad TRID3NT_SCRIPTED_TRANSCRIPT_JSON: %s", exc)
            return []
    path = os.environ.get("TRID3NT_SCRIPTED_TRANSCRIPT")
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


# ---------------------------------------------------------------------------
# Test-harness (fake-provider) seam.
#
# The agent-loop test suite drives the REAL server dispatch under
# ``MODEL_PROVIDER=scripted`` and feeds fake model turns through this harness --
# the single replacement for the retired "patch ``build_client`` + feed fake
# ``generate_content_stream`` chunks" pattern (see the ``fake_llm`` conftest
# fixture). A test installs a turn source, drives the loop, then inspects
# ``harness_calls()`` for the ``contents`` the server built between turns.
#
# A fake turn is a plain dict (the JSON-transcript shape, extended):
#   {"text": "..."}                         -- one narration delta
#   {"tool_call": {"name","args","call_id"?,"thought_signature"?}}
#   {"tool_calls": [ {..}, {..} ]}           -- parallel calls in ONE round
#   {"raise": <BaseException>}               -- inject a model-stream error
#   {"usage": {"total_token_count": ...}}    -- emit a UsageMetadataEvent
# A turn may combine text + call(s) + usage. Absent keys emit nothing, so a bare
# ``{}`` is a genuinely empty round (the qwen3 empty-completion shape) and
# ``None`` (a fixed list run past its end) yields a terminal narration so the
# loop stops. Usage is emitted ONLY when a turn carries a ``usage`` key -- the
# direct-adapter tests assert exact event counts, so no phantom UsageMetadataEvent
# is injected.
#
# The turn SOURCE is either a list of turn dicts (advanced by an internal
# per-call counter) or a callable ``(call_index:int, contents) -> turn`` for
# dynamic tests (e.g. the circuit-breaker suite that decides the next turn from
# an external counter). Advance is CALL-SEQUENCED (one turn per stream_scripted
# call), which -- unlike the production transcript path's contents-model-role
# counting -- correctly handles a round that emits MULTIPLE tool calls (the loop
# appends >1 model Content for such a round).
# ---------------------------------------------------------------------------

#: Installed fake-turn source (list of turn dicts OR a (index, contents)->turn
#: callable). ``None`` => harness inactive (production transcript path runs).
_HARNESS_SOURCE: list[dict[str, Any]] | Callable[[int, Any], Any] | None = None
#: Per-call advance counter (one turn consumed per ``stream_scripted`` call).
_HARNESS_INDEX: int = 0
#: Recorded ``stream_scripted`` calls (contents built between turns) for tests.
_HARNESS_CALLS: list[dict[str, Any]] = []


def install_harness(source: list[dict[str, Any]] | Callable[[int, Any], Any]) -> None:
    """Install a call-sequenced fake-turn source; reset the index + call log."""
    global _HARNESS_SOURCE, _HARNESS_INDEX, _HARNESS_CALLS
    _HARNESS_SOURCE = source
    _HARNESS_INDEX = 0
    _HARNESS_CALLS = []


def reset_harness() -> None:
    """Clear the harness source, advance counter, and recorded calls."""
    global _HARNESS_SOURCE, _HARNESS_INDEX, _HARNESS_CALLS
    _HARNESS_SOURCE = None
    _HARNESS_INDEX = 0
    _HARNESS_CALLS = []


def harness_active() -> bool:
    """True when a fake-turn source is installed (test harness in control)."""
    return _HARNESS_SOURCE is not None


def harness_calls() -> list[dict[str, Any]]:
    """The recorded ``stream_scripted`` calls: each a dict with ``contents``,
    ``tool_declarations``, ``system_prompt``, ``model``, ``index``. Tests read
    ``harness_calls()[i]["contents"]`` in place of the retired
    ``_capture_and_stream`` kwargs snapshot."""
    return _HARNESS_CALLS


def text_turn(text: str) -> dict[str, Any]:
    """A fake turn that streams one narration delta."""
    return {"text": text}


def call_turn(
    name: str,
    args: dict[str, Any] | None = None,
    call_id: str | None = None,
    thought_signature: bytes | None = None,
) -> dict[str, Any]:
    """A fake turn that emits ONE tool call (optionally text via merge)."""
    tc: dict[str, Any] = {"name": name, "args": args or {}}
    if call_id is not None:
        tc["call_id"] = call_id
    if thought_signature is not None:
        tc["thought_signature"] = thought_signature
    return {"tool_call": tc}


def calls_turn(*calls: dict[str, Any]) -> dict[str, Any]:
    """A fake turn that emits N tool calls in ONE round (parallel bundling).

    Each argument is a ``{"name","args","call_id"?,"thought_signature"?}`` dict.
    """
    return {"tool_calls": list(calls)}


def raise_turn(exc: BaseException) -> dict[str, Any]:
    """A fake turn that raises ``exc`` from the model stream (error injection)."""
    return {"raise": exc}


def _usage_event_from(usage: dict[str, Any]) -> UsageMetadataEvent:
    """Build a UsageMetadataEvent from a turn's ``usage`` dict."""
    ct = usage.get("cached_content_token_count")
    return UsageMetadataEvent(
        cached_content_token_count=ct,
        total_token_count=usage.get("total_token_count"),
        prompt_token_count=usage.get("prompt_token_count"),
        candidates_token_count=usage.get("candidates_token_count"),
        cache_hit=bool(ct and ct > 0),
    )


def _events_from_turn(turn: Any, index: int) -> list[StreamEvent]:
    """Resolve a fake turn dict into the ordered ``StreamEvent`` list it emits.

    A ``{"raise": exc}`` turn raises ``exc`` here (before any event), matching
    the old fake ``generate_content_stream`` ``side_effect`` that raised. A
    ``None`` turn (fixed list run past its end) yields a single terminal
    narration so the agent loop stops.
    """
    if turn is None:
        return [TextDeltaEvent(delta="[scripted harness] transcript exhausted.")]
    # Escape hatch: a turn already expressed as a list of StreamEvents.
    if isinstance(turn, (list, tuple)):
        return list(turn)
    if not isinstance(turn, dict):
        return []
    exc = turn.get("raise")
    if exc is not None:
        raise exc
    events: list[StreamEvent] = []
    text = turn.get("text")
    if text:
        events.append(TextDeltaEvent(delta=str(text)))
    calls: list[dict[str, Any]] = []
    single = turn.get("tool_call")
    if isinstance(single, dict):
        calls.append(single)
    for tc in turn.get("tool_calls") or []:
        if isinstance(tc, dict):
            calls.append(tc)
    for i, tc in enumerate(calls):
        if not tc.get("name"):
            continue
        sig = tc.get("thought_signature")
        args = tc.get("args")
        events.append(
            FunctionCallEvent(
                name=str(tc["name"]),
                call_id=str(tc.get("call_id") or f"scripted-{index}-{i}"),
                args=args if isinstance(args, dict) else {},
                # Mirror the retired Vertex producer's guard: a non-bytes
                # signature (e.g. a MagicMock leak) is coerced to None so we
                # never feed garbage back to the model.
                thought_signature=sig if isinstance(sig, (bytes, bytearray)) else None,
            )
        )
    usage = turn.get("usage")
    if usage is not None:
        events.append(_usage_event_from(usage))
    return events


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

    When a test harness source is installed (``install_harness``), this routes
    to the CALL-SEQUENCED fake-turn path (records the call, advances one turn)
    instead of the contents-counting production transcript path.
    """
    if harness_active():
        global _HARNESS_INDEX
        index = _HARNESS_INDEX
        _HARNESS_INDEX += 1
        # Shallow-snapshot ``contents`` at CALL time: the server loop appends new
        # Content objects to the SAME list in place between turns, so storing the
        # live reference would make every recorded call show the final mutated
        # list. The Content objects themselves are not mutated, so a shallow copy
        # captures each turn's state faithfully.
        _HARNESS_CALLS.append(
            {
                "contents": list(contents)
                if isinstance(contents, (list, tuple))
                else contents,
                "tool_declarations": tool_declarations,
                "system_prompt": system_prompt,
                "model": model,
                "index": index,
            }
        )
        src = _HARNESS_SOURCE
        if callable(src):
            turn = src(index, contents)
        elif isinstance(src, (list, tuple)):
            turn = src[index] if index < len(src) else None
        else:
            turn = None
        for ev in _events_from_turn(turn, index):
            yield ev
        return

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
            "+ TRID3NT_SCRIPTED_TRANSCRIPT[_JSON] or call set_script())."
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
