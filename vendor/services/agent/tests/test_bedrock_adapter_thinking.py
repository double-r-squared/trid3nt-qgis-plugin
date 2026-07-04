"""Narration thinking-strip — the Bedrock adapter must never leak model thinking.

ROOT CAUSE (confirmed): the Bedrock Converse path had NO reasoning handling.
``stream_bedrock``'s contentBlockDelta handler only matched ``delta['text']``
(-> TextDeltaEvent) and ``delta['toolUse']``; there was NO ``reasoningContent``
branch.  Two model families leak differently:

  * Claude emits reasoning in a SEPARATE ``reasoningContent`` block.  Before the
    fix it fell through silently (clean by accident); the fix DROPS it
    explicitly so the behaviour is documented and robust.
  * Amazon Nova writes literal ``<thinking>...</thinking>`` INLINE in the normal
    text block, so it arrives under ``delta['text']`` and streamed to chat as
    visible narration.  The tags arrive SPLIT across deltas, so a per-delta
    ``re.sub`` would fail — the fix is a streaming state machine
    (``_ThinkingStripper``).

These tests feed a synthetic Converse event stream (Claude-style
reasoningContent + Nova-style inline <thinking> split across multiple text
deltas) and assert NEITHER reaches TextDeltaEvent output while normal narration
before/after is preserved.  Unit-level coverage of the stripper state machine
is included so tag-split edge cases are pinned independently of the stream.

Run:
    python -m pytest services/agent/tests/test_bedrock_adapter_thinking.py -q
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from grace2_agent import bedrock_adapter as ba
from grace2_agent.adapter import TextDeltaEvent


# --------------------------------------------------------------------------- #
# Synthetic Converse stream helpers
# --------------------------------------------------------------------------- #


def _text_delta(text: str, idx: int = 0) -> dict[str, Any]:
    return {"contentBlockDelta": {"contentBlockIndex": idx, "delta": {"text": text}}}


def _reasoning_delta(text: str, idx: int = 0) -> dict[str, Any]:
    """A Claude-style reasoningContent delta (separate content block)."""
    return {
        "contentBlockDelta": {
            "contentBlockIndex": idx,
            "delta": {"reasoningContent": {"text": text}},
        }
    }


def _metadata_event() -> dict[str, Any]:
    return {"metadata": {"usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15}}}


class _FakeBedrockClient:
    """Returns a canned ``converse_stream`` response wrapping *events*."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events

    def converse_stream(self, **_kwargs: Any) -> dict[str, Any]:
        return {"stream": list(self._events)}


def _run(events: list[dict[str, Any]], monkeypatch) -> list[Any]:
    """Drive ``stream_bedrock`` over *events*; return the list of yielded events."""
    monkeypatch.setattr(ba, "_bedrock_client", lambda: _FakeBedrockClient(events))

    async def _collect() -> list[Any]:
        out: list[Any] = []
        async for ev in ba.stream_bedrock(contents=[], tool_declarations=None):
            out.append(ev)
        return out

    return asyncio.run(_collect())


def _text(events: list[Any]) -> str:
    return "".join(e.delta for e in events if isinstance(e, TextDeltaEvent))


# --------------------------------------------------------------------------- #
# Stream-level tests (the integration the kickoff requires)
# --------------------------------------------------------------------------- #


def test_claude_reasoning_block_is_dropped(monkeypatch):
    """A Claude-style reasoningContent block never reaches TextDeltaEvent."""
    events = [
        _reasoning_delta("Let me think about the flood depth here...", idx=0),
        _text_delta("The peak flood depth is 2.3 m.", idx=1),
        _metadata_event(),
    ]
    out = _run(events, monkeypatch)
    text = _text(out)
    assert "think" not in text.lower()
    assert text == "The peak flood depth is 2.3 m."


def test_nova_inline_thinking_split_across_deltas_is_stripped(monkeypatch):
    """Nova inline <thinking>...</thinking>, SPLIT across deltas, is suppressed;
    narration before AND after the span is preserved verbatim."""
    # The opening and closing tags are deliberately fragmented across delta
    # boundaries so a per-delta re.sub would fail to match.
    events = [
        _text_delta("Here is the result. "),
        _text_delta("<thin"),
        _text_delta("king>I should geocode "),
        _text_delta("first, then fetch the DEM"),
        _text_delta(" and run the model.</think"),
        _text_delta("ing> The flood layer is on the map."),
        _metadata_event(),
    ]
    out = _run(events, monkeypatch)
    text = _text(out)
    # No thinking content and no raw tags leak.
    assert "<thinking>" not in text
    assert "</thinking>" not in text
    assert "geocode" not in text
    assert "should" not in text
    # Real narration on BOTH sides of the thinking span survives.
    assert text == "Here is the result.  The flood layer is on the map."


def test_normal_narration_passes_through_untouched(monkeypatch):
    events = [
        _text_delta("I fetched "),
        _text_delta("the protected areas "),
        _text_delta("for Big Cypress."),
    ]
    out = _run(events, monkeypatch)
    assert _text(out) == "I fetched the protected areas for Big Cypress."


def test_unclosed_thinking_at_stream_end_drops_buffer(monkeypatch):
    """An unclosed <thinking> at end-of-stream drops the thinking text rather
    than leaking raw tags; narration BEFORE the open tag is still emitted."""
    events = [
        _text_delta("Working on it. "),
        _text_delta("<thinking>still reasoning when the stream ended"),
    ]
    out = _run(events, monkeypatch)
    text = _text(out)
    assert text == "Working on it. "
    assert "<thinking" not in text
    assert "reasoning" not in text


def test_literal_less_than_is_not_eaten(monkeypatch):
    """A bare '<' that is NOT a thinking tag must survive (e.g. '2 < 3')."""
    events = [
        _text_delta("Depth 2 < 3 m "),
        _text_delta("at the gauge."),
    ]
    out = _run(events, monkeypatch)
    assert _text(out) == "Depth 2 < 3 m at the gauge."


# --------------------------------------------------------------------------- #
# Stripper unit tests (state-machine edge cases)
# --------------------------------------------------------------------------- #


def test_stripper_single_delta_contains_full_span():
    s = ba._ThinkingStripper()
    out = s.feed("before <thinking>hidden</thinking> after")
    out += s.flush()
    assert out == "before  after"


def test_stripper_case_and_whitespace_tolerant():
    s = ba._ThinkingStripper()
    # case-insensitive body, whitespace after '<', after '/', and before '>'
    out = s.feed("a< THINKING >x</ Thinking >b")
    out += s.flush()
    assert out == "ab"


def test_stripper_tag_split_at_every_boundary():
    """Feed the span one character at a time — the state machine must still
    suppress the entire thinking span and keep the surrounding text."""
    s = ba._ThinkingStripper()
    full = "lead<thinking>secret</thinking>tail"
    out = "".join(s.feed(ch) for ch in full)
    out += s.flush()
    assert out == "leadtail"
    assert "secret" not in out


def test_stripper_partial_tag_then_plain_text():
    """A '<' followed by text that is NOT a thinking tag must be emitted once
    it is disambiguated (here '<b>' is not a thinking tag)."""
    s = ba._ThinkingStripper()
    out = s.feed("x<b")
    out += s.feed(">y")
    out += s.flush()
    assert out == "x<b>y"


def test_stripper_multiple_thinking_spans():
    s = ba._ThinkingStripper()
    out = s.feed("a<thinking>1</thinking>b<thinking>2</thinking>c")
    out += s.flush()
    assert out == "abc"


def test_stripper_partial_open_tag_prefix_at_eos_is_dropped():
    """A dangling partial '<thin' at end-of-stream must NOT leak as raw text —
    the buffer only ever holds an unfinished thinking-tag prefix, so flush
    drops it rather than emit a raw partial tag."""
    s = ba._ThinkingStripper()
    out = s.feed("ok <thin")  # '<thin' is held as a partial-open prefix
    assert out == "ok "
    out += s.flush()
    assert out == "ok "
    assert "<thin" not in out


# --------------------------------------------------------------------------- #
# Edge cases (FX4): attribute-bearing tags + trailing-partial preservation
# --------------------------------------------------------------------------- #


def test_stripper_attribute_bearing_open_tag_is_suppressed():
    """An OPEN tag carrying attributes ('<thinking foo>') must still be
    recognised and suppressed — the opening tag must NOT leak."""
    s = ba._ThinkingStripper()
    out = s.feed('before <thinking foo="bar">secret</thinking> after')
    out += s.flush()
    assert out == "before  after"
    assert "<thinking" not in out
    assert "foo" not in out
    assert "secret" not in out


def test_stripper_attribute_tag_split_across_deltas_is_suppressed():
    """An attribute-bearing open tag fragmented across deltas is still
    suppressed (the attribute span spills past a delta boundary)."""
    s = ba._ThinkingStripper()
    out = s.feed("lead <thinking ")
    out += s.feed('data-foo="b')
    out += s.feed('ar">hidden</thinking> tail')
    out += s.flush()
    assert out == "lead  tail"
    assert "thinking" not in out
    assert "data-foo" not in out
    assert "hidden" not in out


def test_stripper_thinkingx_is_not_a_thinking_tag():
    """A different tag whose name merely STARTS with 'thinking' (e.g.
    '<thinkingx>') is NOT the thinking tag and must pass through verbatim."""
    s = ba._ThinkingStripper()
    out = s.feed("a<thinkingx>b</thinkingx>c")
    out += s.flush()
    assert out == "a<thinkingx>b</thinkingx>c"


def test_stripper_trailing_real_less_than_is_preserved_at_eos():
    """A buffered partial that turns out to be genuine trailing narration —
    a lone '<' — is EMITTED at flush, not silently dropped."""
    s = ba._ThinkingStripper()
    out = s.feed("done<")  # 'done' emitted; '<' held as an ambiguous partial
    assert out == "done"
    out += s.flush()
    assert out == "done<"


def test_stripper_trailing_less_than_whitespace_is_preserved_at_eos():
    """A lone '<' plus trailing whitespace (still not committed to a tag) is
    real narration and survives flush."""
    s = ba._ThinkingStripper()
    out = s.feed("x < ")
    out += s.flush()
    assert out == "x < "


def test_stripper_unclosed_thinking_still_dropped_at_eos():
    """An unclosed <thinking> span at EOS is still DROPPED — the trailing-
    partial preservation must not resurrect suppressed thinking content."""
    s = ba._ThinkingStripper()
    out = s.feed("lead <thinking>still reasoning")
    out += s.flush()
    assert out == "lead "
    assert "reasoning" not in out
    assert "<thinking" not in out


def test_stripper_unfinished_thinking_tag_prefix_still_dropped_at_eos():
    """A committed-but-unfinished thinking-tag prefix ('<thi', '</th') at EOS
    is dropped (it is a real tag prefix, not narration)."""
    s = ba._ThinkingStripper()
    out = s.feed("hold </th")  # committed to a closing-tag prefix
    assert out == "hold "
    out += s.flush()
    assert out == "hold "
    assert "</th" not in out
