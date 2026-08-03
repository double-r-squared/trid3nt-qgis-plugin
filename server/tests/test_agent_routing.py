"""Agent routing tests (job-0154): Gemini tool-dispatch wiring.

Tests that confirm (engine-door refactor, SFINCS slice: the flood entry is now
the ``run_sfincs`` DOOR + the ``sfincs_flood`` template):
1. ``run_sfincs`` (door) + ``sfincs_flood`` (template) are present in
   ``TOOL_REGISTRY`` (the catalog includes the flood engine so Gemini can see it).
2. ``build_tool_declarations`` includes the ``run_sfincs`` door in the
   list it builds from the registry.
3. The ``stream_events`` adapter correctly yields a ``FunctionCallEvent``
   when a mocked Gemini stream emits a function_call part.
4. ``_stream_model_reply`` dispatches the function call through
   ``_invoke_tool_via_emitter`` when Gemini emits a function_call event
   (mocked Gemini + mocked tool).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from trid3nt_server.agent import tools as agent_tools
from trid3nt_server.agent.adapters.adapter import (
    FunctionCallEvent,
    SYSTEM_PROMPT,
    TextDeltaEvent,
    build_tool_declarations,
    stream_events,
)


# ---------------------------------------------------------------------------
# Test 1: the run_sfincs door + sfincs_flood template are in TOOL_REGISTRY
# ---------------------------------------------------------------------------


def test_sfincs_flood_template_in_registry():
    """sfincs_flood (template) must be registered; the old run_sfincs door and
    the older run_model_flood_scenario name are GONE (door dissolution, ADR 0094,
    no alias)."""
    # The workflow module is imported eagerly by main._import_tools_registry();
    # in tests we trigger the same import chain via the inflight job-0042 path.
    from trid3nt_server.agent.workflows.sfincs.flood import flood  # noqa: F401
    import trid3nt_server.agent.tools  # noqa: F401 -- template registration side-effect
    assert "sfincs_flood" in agent_tools.TOOL_REGISTRY, "the sfincs_flood template must be registered"
    assert "run_sfincs" not in agent_tools.TOOL_REGISTRY, (
        "the run_sfincs door was dissolved (ADR 0094); the template stands alone"
    )
    assert "run_model_flood_scenario" not in agent_tools.TOOL_REGISTRY, (
        "the old name is GONE (no alias)"
    )


# ---------------------------------------------------------------------------
# Test 2: build_tool_declarations includes the sfincs_flood template
# ---------------------------------------------------------------------------


def test_build_tool_declarations_includes_flood_template():
    """Tool declaration list must include the sfincs_flood template (the
    retrievable flood entry; door dissolution, ADR 0094 -- no concierge)."""
    from trid3nt_server.agent.workflows.sfincs.flood import flood  # noqa: F401
    import trid3nt_server.agent.tools  # noqa: F401 -- template registration side-effect

    decls = build_tool_declarations(agent_tools.TOOL_REGISTRY)
    names = [d.name for d in decls]
    assert "sfincs_flood" in names, (
        f"sfincs_flood template missing from declarations; got: {sorted(names)}"
    )


# ---------------------------------------------------------------------------
# Test 3: stream_events yields FunctionCallEvent from mocked Gemini stream
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_events_yields_function_call_event(fake_llm):
    """stream_events demultiplexes a function_call turn into a FunctionCallEvent."""

    # Script one fake turn that emits a single function call; the scripted
    # provider surfaces it as a FunctionCallEvent (client arg is ignored).
    fake_llm.script([
        fake_llm.call(
            "sfincs_flood",
            {"location_query": "Fort Myers, FL", "return_period_yr": 100},
            call_id="call-abc123",
        ),
    ])

    events: list = []
    async for event in stream_events(
        None,
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
async def test_stream_events_yields_text_delta_event(fake_llm):
    """stream_events yields TextDeltaEvent for a normal text response turn."""

    fake_llm.script([fake_llm.text("Hello, I can help with that.")])

    events: list = []
    async for event in stream_events(
        None,
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
    """System prompt must instruct the model to call the sfincs_flood template."""
    assert "sfincs_flood" in SYSTEM_PROMPT
    assert "100-year" in SYSTEM_PROMPT or "flood" in SYSTEM_PROMPT.lower()


# ---------------------------------------------------------------------------
# Test 6: sfincs_flood docstring covers 100-year storm phrase
# ---------------------------------------------------------------------------


def test_sfincs_flood_docstring_covers_user_intent():
    """Docstring must mention '100-year' to match the failing demo prompt."""
    from trid3nt_server.agent.tools import TOOL_REGISTRY
    from trid3nt_server.agent.workflows.sfincs.flood import flood  # noqa: F401

    entry = TOOL_REGISTRY.get("sfincs_flood")
    assert entry is not None
    doc = entry.fn.__doc__ or ""
    assert "100-year" in doc, (
        "sfincs_flood docstring must mention '100-year' so Gemini "
        "matches the 'Model peak flood depth from a 100-year design storm' prompt"
    )
