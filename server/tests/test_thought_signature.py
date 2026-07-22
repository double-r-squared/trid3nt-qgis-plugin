"""Gemini 3 thought_signature plumbing tests (job-B10).

Pre-dispatch verification found that the multi-turn loop adapter was
silently dropping ``Part.thought_signature`` on the producer side and
never echoing it back on the replayed model turn. On Gemini 3 (Vertex)
this would trigger a ``thought-signature mismatch`` 400 on the second
turn of any tool-using conversation. Gemini 2.5 (the current
``GEMINI_DEFAULT_MODEL``) does not surface signatures, so the wire is
None today — the plumbing is forward-compat so the bug does not return
the moment ``TRID3NT_GEMINI_MODEL=gemini-3-pro`` is flipped.

Coverage:

1. ``stream_events_with_contents`` producer harvests ``part.thought_signature``
   off the SDK ``Part`` and surfaces it on ``FunctionCallEvent``.
2. ``build_function_call_content`` attaches the signature to the wrapping
   ``Part`` (NOT the ``FunctionCall``, which has no signature field in
   google-genai types.py — see the field on line 2044 of types.py).
3. ``build_function_call_content`` is a no-op when signature is None
   (Gemini 2.5 path); the resulting Part carries no signature.
4. ``build_contents_from_history`` preserves a persisted ``parts_blob``
   across reconstruction — function_call / function_response Parts and
   the thought_signature round-trip cleanly through encode/decode.
5. The replayed Content carries the same byte-for-byte signature on the
   reconstructed Part.
6. Malformed ``parts_blob`` is tolerated — fall back to the text path.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from google.genai import types as genai_types

from trid3nt_server.adapter import (
    FunctionCallEvent,
    build_contents_from_history,
    build_function_call_content,
    encode_parts_blob,
    stream_events_with_contents,
)


# Reusable fake-Part / fake-chunk builders. Mirror google-genai's chunk
# shape: ``chunk.candidates[i].content.parts[j]`` carries the function_call
# (and now thought_signature) for each emitted decision.
def _fake_chunk_with_signed_function_call(
    name: str,
    args: dict,
    call_id: str,
    signature: bytes | None,
):
    fn_call = MagicMock()
    fn_call.name = name
    fn_call.id = call_id
    fn_call.args = args
    fake_part = MagicMock()
    fake_part.function_call = fn_call
    fake_part.text = None
    # The SDK field is ``Part.thought_signature: Optional[bytes]``. We set
    # it directly on the mock so ``getattr(part, "thought_signature", None)``
    # finds it; MagicMock would otherwise return a MagicMock instance.
    fake_part.thought_signature = signature
    fake_content = MagicMock()
    fake_content.parts = [fake_part]
    fake_candidate = MagicMock()
    fake_candidate.content = fake_content
    fake_chunk = MagicMock()
    fake_chunk.candidates = [fake_candidate]
    fake_chunk.text = None
    return fake_chunk


# ---------------------------------------------------------------------------
# Test 1: producer harvests signature off the Part
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_producer_harvests_thought_signature_off_part():
    """Gemini emits a function_call Part with a thought_signature; the
    producer surfaces it on the FunctionCallEvent."""
    sig = b"\x01\x02\x03\x04\x05\x06opaque-thought-sig"
    chunk = _fake_chunk_with_signed_function_call(
        "geocode_location", {"query": "Fort Myers, FL"}, "call-1", sig
    )
    fake_client = MagicMock()
    fake_client.models.generate_content_stream.return_value = iter([chunk])

    events: list = []
    contents = [
        genai_types.Content(role="user", parts=[genai_types.Part(text="test")])
    ]
    async for evt in stream_events_with_contents(fake_client, "gemini-3-pro", contents):
        events.append(evt)

    assert len(events) == 1
    assert isinstance(events[0], FunctionCallEvent)
    assert events[0].thought_signature == sig, (
        f"signature not harvested: got {events[0].thought_signature!r}"
    )


@pytest.mark.asyncio
async def test_producer_handles_missing_signature_on_25():
    """Gemini 2.5 surfaces no signature; FunctionCallEvent carries None."""
    chunk = _fake_chunk_with_signed_function_call(
        "geocode_location", {"query": "x"}, "c", signature=None
    )
    fake_client = MagicMock()
    fake_client.models.generate_content_stream.return_value = iter([chunk])

    events: list = []
    contents = [
        genai_types.Content(role="user", parts=[genai_types.Part(text="test")])
    ]
    async for evt in stream_events_with_contents(
        fake_client, "gemini-2.5-pro", contents
    ):
        events.append(evt)

    assert len(events) == 1
    assert isinstance(events[0], FunctionCallEvent)
    assert events[0].thought_signature is None


@pytest.mark.asyncio
async def test_producer_rejects_non_bytes_signature():
    """A pathological signature that isn't bytes (e.g. a MagicMock leakage)
    is coerced to None so we never feed garbage back to Gemini."""
    chunk = _fake_chunk_with_signed_function_call(
        "geocode_location", {"query": "x"}, "c", signature="not-bytes"  # type: ignore[arg-type]
    )
    fake_client = MagicMock()
    fake_client.models.generate_content_stream.return_value = iter([chunk])

    events: list = []
    contents = [
        genai_types.Content(role="user", parts=[genai_types.Part(text="t")])
    ]
    async for evt in stream_events_with_contents(fake_client, "gemini-3-pro", contents):
        events.append(evt)

    assert events[0].thought_signature is None


# ---------------------------------------------------------------------------
# Test 2: build_function_call_content attaches signature to the wrapping Part
# ---------------------------------------------------------------------------


def test_build_function_call_content_attaches_signature_to_part():
    """``thought_signature`` is set on the wrapping Part — not the
    FunctionCall (which has no signature field in google-genai types.py)."""
    sig = b"\xdeadbeef-opaque-thought-sig"
    content = build_function_call_content(
        "geocode_location",
        {"query": "Fort Myers, FL"},
        call_id="call-1",
        thought_signature=sig,
    )
    assert content.role == "model"
    assert len(content.parts) == 1
    part = content.parts[0]
    # The Part carries the signature; FunctionCall does NOT (and could not —
    # google-genai's FunctionCall has no thought_signature field).
    assert part.thought_signature == sig, (
        f"signature lost: {part.thought_signature!r}"
    )
    assert part.function_call is not None
    assert part.function_call.name == "geocode_location"
    assert part.function_call.id == "call-1"
    # Belt-and-braces: FunctionCall has no signature attr.
    assert getattr(part.function_call, "thought_signature", None) is None


def test_build_function_call_content_no_signature_is_no_op():
    """Gemini 2.5 path: ``thought_signature=None`` produces a Part with no
    signature attached. No exception, no field set."""
    content = build_function_call_content(
        "geocode_location", {"query": "x"}, call_id="call-1", thought_signature=None
    )
    part = content.parts[0]
    assert part.thought_signature is None
    assert part.function_call.name == "geocode_location"


def test_build_function_call_content_omitted_signature_default():
    """The signature parameter defaults to None — backward-compat for
    callers that don't know about Gemini 3 yet."""
    content = build_function_call_content("foo", {"a": 1}, call_id="c")
    part = content.parts[0]
    assert part.thought_signature is None
    assert part.function_call.name == "foo"


# ---------------------------------------------------------------------------
# Test 3: build_contents_from_history preserves parts_blob across rebuild
# ---------------------------------------------------------------------------


def test_parts_blob_round_trips_function_call_with_signature():
    """encode_parts_blob → build_contents_from_history reconstructs the
    exact function_call Part with the original thought_signature bytes."""
    sig = b"sig-bytes-\x00\x01\x02"
    original = build_function_call_content(
        "fetch_wdpa_protected_areas",
        {"bbox": [-82.0, 26.5, -81.7, 26.8]},
        call_id="call-wdpa",
        thought_signature=sig,
    )
    blob = encode_parts_blob(original.parts)
    assert isinstance(blob, bytes) and len(blob) > 0

    chat_history = [
        {"role": "model", "parts_blob": blob},
    ]
    contents = build_contents_from_history("next user turn", chat_history)
    # Two entries: the rehydrated model turn + the new user_text.
    assert len(contents) == 2
    rehydrated = contents[0]
    assert rehydrated.role == "model"
    assert len(rehydrated.parts) == 1
    part = rehydrated.parts[0]
    assert part.function_call is not None
    assert part.function_call.name == "fetch_wdpa_protected_areas"
    assert part.function_call.args == {"bbox": [-82.0, 26.5, -81.7, 26.8]}
    assert part.function_call.id == "call-wdpa"
    assert part.thought_signature == sig, (
        f"signature did not survive blob round-trip: {part.thought_signature!r}"
    )


def test_parts_blob_round_trips_function_response():
    """function_response Parts also round-trip cleanly through the blob."""
    from trid3nt_server.adapter import build_function_response_content

    original = build_function_response_content(
        "fetch_wdpa_protected_areas",
        {"tool": "fetch_wdpa_protected_areas", "status": "ok", "result": {"count": 2}},
        call_id="call-wdpa",
    )
    blob = encode_parts_blob(original.parts)
    chat_history = [{"role": "user", "parts_blob": blob}]
    contents = build_contents_from_history("next", chat_history)
    assert len(contents) == 2
    part = contents[0].parts[0]
    assert part.function_response is not None
    assert part.function_response.name == "fetch_wdpa_protected_areas"
    assert part.function_response.response["status"] == "ok"
    assert part.function_response.id == "call-wdpa"


def test_parts_blob_takes_precedence_over_text():
    """When both ``parts_blob`` and ``text`` are present, the blob wins —
    parts_blob is the full-fidelity path."""
    sig = b"\x10\x20\x30"
    original = build_function_call_content("foo", {"x": 1}, "c", thought_signature=sig)
    blob = encode_parts_blob(original.parts)
    chat_history = [
        {"role": "agent", "parts_blob": blob, "text": "fallback text — should be ignored"},
    ]
    contents = build_contents_from_history("u", chat_history)
    # Rehydrated turn used the blob, not the text.
    part = contents[0].parts[0]
    assert part.function_call is not None
    assert part.function_call.name == "foo"
    assert part.thought_signature == sig
    # No text Part — the blob carries no text.
    assert part.text is None or part.text == ""


def test_malformed_parts_blob_falls_back_to_text():
    """A malformed parts_blob does not break the conversation; we fall
    back to the text path so a single bad row never wedges history."""
    chat_history = [
        {"role": "user", "parts_blob": b"\xff\xfe-not-json", "text": "fallback works"},
    ]
    contents = build_contents_from_history("now", chat_history)
    # Two entries: the text-fallback rebuild + the new user_text.
    assert len(contents) == 2
    assert contents[0].parts[0].text == "fallback works"


def test_legacy_text_only_entries_still_work():
    """Entries with no ``parts_blob`` still rebuild from ``text`` (legacy
    persistence rows)."""
    history = [
        {"role": "user", "text": "Hello"},
        {"role": "agent", "text": "Hi there"},
    ]
    contents = build_contents_from_history("Next?", history)
    assert len(contents) == 3
    assert contents[0].role == "user"
    assert contents[0].parts[0].text == "Hello"
    assert contents[1].role == "model"  # agent → model
    assert contents[1].parts[0].text == "Hi there"
    assert contents[2].parts[0].text == "Next?"


def test_empty_blob_falls_back_to_text():
    """An empty list parts_blob is treated as missing, not a crash."""
    chat_history = [
        {"role": "user", "parts_blob": [], "text": "use text"},
    ]
    contents = build_contents_from_history("next", chat_history)
    assert contents[0].parts[0].text == "use text"


# ---------------------------------------------------------------------------
# Test 4: signature plumbing is forward-compat (no crash without Gemini 3)
# ---------------------------------------------------------------------------


def test_signature_plumbing_no_crash_on_25_default():
    """Gemini 2.5 surfaces no signature; the whole producer→builder chain
    handles None without raising or attaching a stray field."""
    event = FunctionCallEvent(name="x", call_id="c", args={"a": 1})
    assert event.thought_signature is None
    content = build_function_call_content(
        event.name,
        event.args,
        event.call_id,
        thought_signature=event.thought_signature,
    )
    assert content.parts[0].thought_signature is None
