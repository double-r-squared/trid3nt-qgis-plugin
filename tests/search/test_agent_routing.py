"""Agent routing: the model tool-dispatch wiring.

An engine TEMPLATE is in ``TOOL_REGISTRY`` with no dissolved door name beside it,
``build_tool_declarations`` includes it, the ``stream_events`` adapter yields a
``FunctionCallEvent`` for a function_call part, and ``_stream_model_reply``
dispatches that call through ``_invoke_tool_via_emitter``."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from trid3nt_server import tools as agent_tools
from trid3nt_server.adapters.adapter import (
    FunctionCallEvent,
    TextDeltaEvent,
    build_tool_declarations,
    stream_events,
    system_prompt,
)


#: The engine template these three tests route through - a registered template
#: whose docstring is the model's only routing signal for its question class.
_TEMPLATE = "telemac_river_dye"




def test_engine_template_in_registry():
    """The template must be registered and every dissolved DOOR name gone."""
    import trid3nt_server.tools  # noqa: F401 -- template registration side-effect

    assert _TEMPLATE in agent_tools.TOOL_REGISTRY, f"{_TEMPLATE} must be registered"
    for door in ("run_sfincs", "run_model_flood_scenario", "run_telemac"):
        assert door not in agent_tools.TOOL_REGISTRY, (
            f"{door} was dissolved; the template stands alone"
        )




def test_build_tool_declarations_includes_the_template():
    """The declaration list must carry the template (no concierge in front)."""
    import trid3nt_server.tools  # noqa: F401 -- template registration side-effect

    decls = build_tool_declarations(agent_tools.TOOL_REGISTRY)
    names = [d.name for d in decls]
    assert _TEMPLATE in names, (
        f"{_TEMPLATE} missing from declarations; got: {sorted(names)}"
    )




@pytest.mark.asyncio
async def test_stream_events_yields_function_call_event(fake_llm):
    """stream_events demultiplexes a function_call turn into a FunctionCallEvent."""

    # Script one fake turn that emits a single function call; the scripted
    # provider surfaces it as a FunctionCallEvent (client arg is ignored).
    fake_llm.script([
        fake_llm.call(
            _TEMPLATE,
            {"location": "Fort Myers, FL", "substance": "dye"},
            call_id="call-abc123",
        ),
    ])

    events: list = []
    async for event in stream_events(
        None,
        "gemini-2.5-pro",
        "Track a dye release down the river at Fort Myers, FL",
        tool_declarations=[],  # declarations already built; skip here
        system_prompt=system_prompt(),
    ):
        events.append(event)

    assert len(events) == 1
    evt = events[0]
    assert isinstance(evt, FunctionCallEvent), (
        f"Expected FunctionCallEvent, got {type(evt)}"
    )
    assert evt.name == _TEMPLATE
    assert evt.args.get("location") == "Fort Myers, FL"
    assert evt.args.get("substance") == "dye"




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




def test_system_prompt_routes_the_registered_templates():
    """The prompt's tool list is built from the registry, so every registered
    template is reachable from it by name."""
    prompt = system_prompt()
    assert "telemac_rain_on_grid" in prompt
    assert _TEMPLATE in prompt




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
