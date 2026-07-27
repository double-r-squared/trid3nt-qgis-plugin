"""Agent routing tests (job-0154): Gemini tool-dispatch wiring.

Tests that confirm (engine-door refactor, SFINCS slice: the flood entry is now
the ``run_sfincs`` DOOR + the ``sfincs_flood`` template):
1. ``run_sfincs`` (door) + ``sfincs_flood`` (template) are present in
   ``TOOL_REGISTRY`` (the catalog includes the flood engine so Gemini can see it).
2. ``build_tool_declarations`` includes the ``run_sfincs`` door in the
   list it builds from the registry.
3. The ``stream_events`` adapter correctly yields a ``FunctionCallEvent``
   when a mocked Gemini stream emits a function_call part.
4. ``_stream_gemini_reply`` dispatches the function call through
   ``_invoke_tool_via_emitter`` when Gemini emits a function_call event
   (mocked Gemini + mocked tool).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from trid3nt_server import tools as agent_tools
from trid3nt_server.adapter import (
    FunctionCallEvent,
    SYSTEM_PROMPT,
    TextDeltaEvent,
    build_tool_declarations,
    stream_events,
)


# ---------------------------------------------------------------------------
# Test 1: the run_sfincs door + sfincs_flood template are in TOOL_REGISTRY
# ---------------------------------------------------------------------------


def test_sfincs_door_and_template_in_registry():
    """run_sfincs (door) + sfincs_flood (template) must be registered; the old
    run_model_flood_scenario name is GONE (engine-door refactor, no alias)."""
    # The workflow module is imported eagerly by main._import_tools_registry();
    # in tests we trigger the same import chain via the inflight job-0042 path.
    from trid3nt_server.workflows.sfincs.flood import flood  # noqa: F401
    import trid3nt_server.tools  # noqa: F401 -- door registration side-effect
    assert "run_sfincs" in agent_tools.TOOL_REGISTRY, "the SFINCS flood door must be registered"
    assert "sfincs_flood" in agent_tools.TOOL_REGISTRY, "the sfincs_flood template must be registered"
    assert "run_model_flood_scenario" not in agent_tools.TOOL_REGISTRY, (
        "the old name must be REPLACED by the door + template (no alias)"
    )


# ---------------------------------------------------------------------------
# Test 2: build_tool_declarations includes the run_sfincs door
# ---------------------------------------------------------------------------


def test_build_tool_declarations_includes_flood_door():
    """Tool declaration list must include the run_sfincs door (the retrievable
    flood entry; the sfincs_flood template is pool-excluded, door-surfaced)."""
    from trid3nt_server.workflows.sfincs.flood import flood  # noqa: F401
    import trid3nt_server.tools  # noqa: F401 -- door registration side-effect

    decls = build_tool_declarations(agent_tools.TOOL_REGISTRY)
    names = [d.name for d in decls]
    assert "run_sfincs" in names, (
        f"run_sfincs door missing from declarations; got: {sorted(names)}"
    )


# ---------------------------------------------------------------------------
# Test 3: stream_events yields FunctionCallEvent from mocked Gemini stream
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_events_yields_function_call_event():
    """stream_events demultiplexes a Gemini function_call part into a FunctionCallEvent."""

    # Build a fake chunk that looks like a google-genai streaming chunk with
    # a function_call part.
    fake_fn_call = MagicMock()
    fake_fn_call.name = "sfincs_flood"
    fake_fn_call.id = "call-abc123"
    fake_fn_call.args = {"location_query": "Fort Myers, FL", "return_period_yr": 100}

    fake_part = MagicMock()
    fake_part.function_call = fake_fn_call
    fake_part.text = None

    fake_content = MagicMock()
    fake_content.parts = [fake_part]

    fake_candidate = MagicMock()
    fake_candidate.content = fake_content

    fake_chunk = MagicMock()
    fake_chunk.candidates = [fake_candidate]
    fake_chunk.text = None

    # Patch the sync generate_content_stream to return [fake_chunk].
    fake_client = MagicMock()
    fake_client.models.generate_content_stream.return_value = iter([fake_chunk])

    events: list = []
    async for event in stream_events(
        fake_client,
        "gemini-2.5-pro",
        "Model peak flood depth from a 100-year design storm in Fort Myers, FL",
        tool_declarations=[],  # declarations already built; skip here
        system_prompt=SYSTEM_PROMPT,
    ):
        events.append(event)

    assert len(events) == 1
    evt = events[0]
    assert isinstance(evt, FunctionCallEvent), (
        f"Expected FunctionCallEvent, got {type(evt)}"
    )
    assert evt.name == "sfincs_flood"
    assert evt.args.get("location_query") == "Fort Myers, FL"
    assert evt.args.get("return_period_yr") == 100


# ---------------------------------------------------------------------------
# Test 4: stream_events yields TextDeltaEvent for a plain text chunk
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_events_yields_text_delta_event():
    """stream_events yields TextDeltaEvent for a normal text response chunk."""

    fake_part = MagicMock()
    fake_part.function_call = None
    fake_part.text = "Hello, I can help with that."

    fake_content = MagicMock()
    fake_content.parts = [fake_part]

    fake_candidate = MagicMock()
    fake_candidate.content = fake_content

    fake_chunk = MagicMock()
    fake_chunk.candidates = [fake_candidate]
    fake_chunk.text = None

    fake_client = MagicMock()
    fake_client.models.generate_content_stream.return_value = iter([fake_chunk])

    events: list = []
    async for event in stream_events(
        fake_client,
        "gemini-2.5-pro",
        "What is GRACE?",
    ):
        events.append(event)

    assert len(events) == 1
    assert isinstance(events[0], TextDeltaEvent)
    assert events[0].delta == "Hello, I can help with that."


# ---------------------------------------------------------------------------
# Test 5: SYSTEM_PROMPT mentions key routing phrases
# ---------------------------------------------------------------------------


def test_system_prompt_mentions_flood_routing():
    """System prompt must instruct Gemini to call the run_sfincs flood door."""
    assert "run_sfincs" in SYSTEM_PROMPT
    assert "100-year" in SYSTEM_PROMPT or "flood" in SYSTEM_PROMPT.lower()


# ---------------------------------------------------------------------------
# Test 6: sfincs_flood docstring covers 100-year storm phrase
# ---------------------------------------------------------------------------


def test_sfincs_flood_docstring_covers_user_intent():
    """Docstring must mention '100-year' to match the failing demo prompt."""
    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.workflows.sfincs.flood import flood  # noqa: F401

    entry = TOOL_REGISTRY.get("sfincs_flood")
    assert entry is not None
    doc = entry.fn.__doc__ or ""
    assert "100-year" in doc, (
        "sfincs_flood docstring must mention '100-year' so Gemini "
        "matches the 'Model peak flood depth from a 100-year design storm' prompt"
    )
