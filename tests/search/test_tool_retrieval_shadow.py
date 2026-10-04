"""Tool-retrieval enforce over the built-in surfacing path.

Enforce is unconditional and K the only lever: it subsets the registry to the
visible set, the CORE FLOOR stays a subset and the Case's visible set never
shrinks. A retrieval error or an empty result FAILS OPEN to the default
declarable registry. The per-turn selection event fires. ASCII only."""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import patch

import pytest

from trid3nt_server.model.adapters.model_selection import ModelSettings
from trid3nt_contracts import new_ulid


@dataclass
class _FakeSocket:
    sent: list[str] = field(default_factory=list)

    async def send(self, msg: str) -> None:  # noqa: D401 — protocol shim
        self.sent.append(msg)


def _make_text_chunk(text: str):
    """A fake turn (scripted-provider dict) emitting one narration delta."""
    return {"text": text}


def _settings() -> ModelSettings:
    return ModelSettings(
        model="gemini-2.5-pro",
    )


def _non_template_names() -> set[str]:
    """The names in the DEFAULT declarable registry: ``TOOL_REGISTRY`` minus the
    pool-hidden ``internal`` tier. What reaches
    ``build_tool_declarations`` is a NEW filtered dict, not the live registry."""
    from trid3nt_server.tools import TOOL_REGISTRY

    return {
        name
        for name, entry in TOOL_REGISTRY.items()
        if getattr(entry.metadata, "tier", "general") != "internal"
    }


async def _drive_one_turn(
    fake_llm,
    *,
    chunks: list,
    user_text: str = "show me the flood map",
    state=None,
    dispatch=None,
):
    """Drive ONE _stream_model_reply turn (enforce is unconditional).

    Returns (state, registries_seen, dispatch_log) where registries_seen is the
    list of objects passed to build_tool_declarations (one per turn iteration).
    """
    from trid3nt_server import server as agent_server
    from trid3nt_server.server import SessionState

    fake_llm.script(chunks)

    registries_seen: list = []

    def _capture_build(reg):
        registries_seen.append(reg)
        return []

    dispatch_log: list[tuple[str, dict]] = []

    async def _fake_invoke(_ws, _state, name, args):
        dispatch_log.append((name, args))
        if dispatch is not None:
            return dispatch(name, args)
        return {"ok": True}

    sock = _FakeSocket()
    if state is None:
        state = SessionState(session_id=new_ulid())

    with patch.object(
             agent_server, "_invoke_tool_via_emitter", side_effect=_fake_invoke
         ), \
         patch.object(
             agent_server, "build_tool_declarations", side_effect=_capture_build
         ):
        await agent_server._stream_model_reply(
            sock, state, _settings(), user_text, "research"
        )
    return state, registries_seen, dispatch_log


@pytest.mark.asyncio
async def test_fail_open_on_retrieval_error(fake_llm):
    from trid3nt_server.tools import TOOL_REGISTRY
    import trid3nt_server.tools.search.tool_retrieval as tr

    def _boom(*_a, **_k):
        raise RuntimeError("index exploded")

    with patch.object(tr, "retrieve_visible_tools", side_effect=_boom):
        _state, regs, _disp = await _drive_one_turn(
            fake_llm, chunks=[_make_text_chunk("done")]
        )
    # FAIL-OPEN: never trimmed -- falls back to the DEFAULT declarable registry.
    assert set(regs[0]) == _non_template_names()
    assert regs[0] is not TOOL_REGISTRY


@pytest.mark.asyncio
async def test_fail_open_on_empty_result(fake_llm):
    from trid3nt_server.tools import TOOL_REGISTRY
    import trid3nt_server.tools.search.tool_retrieval as tr

    # An empty would-be set must FAIL-OPEN (never empty / core-only catalog).
    with patch.object(tr, "retrieve_visible_tools", return_value=set()):
        _state, regs, _disp = await _drive_one_turn(
            fake_llm, chunks=[_make_text_chunk("done")]
        )
    assert set(regs[0]) == _non_template_names()
    assert regs[0] is not TOOL_REGISTRY


@pytest.mark.asyncio
async def test_enforce_subsets_registry_and_keeps_core_floor(fake_llm):
    from trid3nt_server.tools.search.tool_retrieval import CORE_FLOOR
    from trid3nt_server.tools import TOOL_REGISTRY
    import trid3nt_server.tools.search.tool_retrieval as tr

    # Pick a small real subset of registered tools that includes the core floor.
    floor = {t for t in CORE_FLOOR if t in TOOL_REGISTRY}
    visible = set(floor) | {"fetch_dem"}
    visible &= set(TOOL_REGISTRY)

    with patch.object(tr, "retrieve_visible_tools", return_value=visible):
        _state, regs, _disp = await _drive_one_turn(
            fake_llm, chunks=[_make_text_chunk("done")]
        )

    sent = regs[0]
    # Enforce -> a NEW (subset) dict, NOT the live registry.
    assert sent is not TOOL_REGISTRY
    sent_names = set(sent)
    # It is a strict subset of the full registry.
    assert sent_names <= set(TOOL_REGISTRY)
    assert len(sent_names) < len(TOOL_REGISTRY)
    # CORE FLOOR is a subset of what was sent.
    assert floor <= sent_names
    # fetch_dem (the requested tool) survived.
    assert "fetch_dem" in sent_names


@pytest.mark.asyncio
async def test_enforce_visible_set_is_monotonic_across_turns(fake_llm):
    from trid3nt_server.server import SessionState
    from trid3nt_server.tools import TOOL_REGISTRY
    import trid3nt_server.tools.search.tool_retrieval as tr

    state = SessionState(session_id=new_ulid())

    real = [t for t in ("fetch_dem", "fetch_cudem", "geocode_location") if t in TOOL_REGISTRY]
    assert real, "expected at least one real tool to test with"

    # Turn 1: retrieval surfaces real[0].
    with patch.object(tr, "retrieve_visible_tools", return_value={real[0]}):
        await _drive_one_turn(
            fake_llm, chunks=[_make_text_chunk("a")], state=state
        )
    after_turn1 = set(state.visible_tools)
    assert real[0] in after_turn1

    # Turn 2: retrieval surfaces a DIFFERENT real tool. real[0] must NOT leave.
    other = real[1] if len(real) > 1 else real[0]
    with patch.object(tr, "retrieve_visible_tools", return_value={other}):
        _state, regs, _disp = await _drive_one_turn(
            fake_llm, chunks=[_make_text_chunk("b")], state=state
        )
    after_turn2 = set(state.visible_tools)
    # MONOTONIC: the set only grows -- everything from turn 1 is still present.
    assert after_turn1 <= after_turn2
    assert real[0] in after_turn2
    # And the catalog sent on turn 2 includes the once-visible real[0].
    assert real[0] in set(regs[0])


