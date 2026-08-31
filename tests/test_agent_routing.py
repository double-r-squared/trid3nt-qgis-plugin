"""Agent routing tests: model tool-dispatch wiring.

1. An engine TEMPLATE is present in ``TOOL_REGISTRY`` and the dissolved door
   names are gone (door dissolution, ADR 0094 -- no alias, no concierge).
2. ``build_tool_declarations`` includes it in the list it builds from the
   registry.
3. The ``stream_events`` adapter yields a ``FunctionCallEvent`` when a mocked
   stream emits a function_call part.
4. ``_stream_model_reply`` dispatches the function call through
   ``_invoke_tool_via_emitter``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from trid3nt_server import tools as agent_tools
from trid3nt_server.adapters.adapter import (
    FunctionCallEvent,
    SYSTEM_PROMPT,
    TextDeltaEvent,
    build_tool_declarations,
    stream_events,
)


#: The engine template these three tests route through - a registered template
#: whose docstring is the model's only routing signal for its question class.
_TEMPLATE = "telemac_river_dye"


# ---------------------------------------------------------------------------
# Test 1: the template is in TOOL_REGISTRY and no door name survives
# ---------------------------------------------------------------------------


def test_engine_template_in_registry():
    """The template must be registered and every dissolved DOOR name gone."""
    import trid3nt_server.tools  # noqa: F401 -- template registration side-effect

    assert _TEMPLATE in agent_tools.TOOL_REGISTRY, f"{_TEMPLATE} must be registered"
    for door in ("run_sfincs", "run_model_flood_scenario", "run_telemac"):
        assert door not in agent_tools.TOOL_REGISTRY, (
            f"{door} was dissolved (ADR 0094); the template stands alone"
        )


# ---------------------------------------------------------------------------
# Test 2: build_tool_declarations includes the template
# ---------------------------------------------------------------------------


def test_build_tool_declarations_includes_the_template():
    """The declaration list must carry the template (no concierge in front)."""
    import trid3nt_server.tools  # noqa: F401 -- template registration side-effect

    decls = build_tool_declarations(agent_tools.TOOL_REGISTRY)
    names = [d.name for d in decls]
    assert _TEMPLATE in names, (
        f"{_TEMPLATE} missing from declarations; got: {sorted(names)}"
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


def test_template_docstring_covers_user_intent():
    """The docstring must carry the question's own words - it is the routing signal."""
    from trid3nt_server.tools import TOOL_REGISTRY

    entry = TOOL_REGISTRY.get(_TEMPLATE)
    assert entry is not None
    doc = entry.fn.__doc__ or ""
    assert "downstream" in doc, (
        f"{_TEMPLATE} docstring must carry the phrasing a user asks in, or the "
        "model has nothing to match against"
    )
