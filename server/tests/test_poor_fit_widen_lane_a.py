"""POOR-FIT WIDENING (LANE A, task 3).

When a turn's TOP retrieval score is under a calibrated threshold
(``TRID3NT_GATING_WIDEN_THRESHOLD``, default 0.035 -- measured against the
hashed dense fallback; see tool_gating.py), the ranking is uncertain, so the
openai-path gate k is widened once (24 -> 40) to protect recall on a vague ask.

Covered: the pure threshold resolver + poor-fit predicate, then the live gating
block -- the widen re-ranks at k=40 (logged) ONLY on a poor fit, and a good fit
is left at k=24.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from trid3nt_server import server as agent_server
from trid3nt_server.adapter import GeminiSettings, TextDeltaEvent
from trid3nt_server.tool_gating import (
    WIDEN_K,
    WIDEN_THRESHOLD_DEFAULT,
    gating_widen_threshold,
    should_widen_for_poor_fit,
)
from trid3nt_contracts import new_ulid


# ---------------------------------------------------------------------------
# Pure helpers.
# ---------------------------------------------------------------------------


def test_widen_threshold_default(monkeypatch):
    monkeypatch.delenv("TRID3NT_GATING_WIDEN_THRESHOLD", raising=False)
    assert gating_widen_threshold() == WIDEN_THRESHOLD_DEFAULT


def test_widen_threshold_env_override(monkeypatch):
    monkeypatch.setenv("TRID3NT_GATING_WIDEN_THRESHOLD", "0.05")
    assert gating_widen_threshold() == 0.05


def test_widen_threshold_malformed_and_negative_fall_back(monkeypatch):
    monkeypatch.setenv("TRID3NT_GATING_WIDEN_THRESHOLD", "not-a-float")
    assert gating_widen_threshold() == WIDEN_THRESHOLD_DEFAULT
    monkeypatch.setenv("TRID3NT_GATING_WIDEN_THRESHOLD", "-1")
    assert gating_widen_threshold() == WIDEN_THRESHOLD_DEFAULT


def test_should_widen_true_on_poor_fit():
    assert should_widen_for_poor_fit([("t", 0.01), ("u", 0.005)], 0.035) is True


def test_should_widen_false_on_good_fit():
    assert should_widen_for_poor_fit([("t", 0.9)], 0.035) is False


def test_should_widen_false_on_empty_ranking():
    # An empty ranking is the fail-open case, NOT a poor-fit widen signal.
    assert should_widen_for_poor_fit([], 0.035) is False


def test_widen_k_exceeds_gate_floor():
    assert WIDEN_K > 24


# ---------------------------------------------------------------------------
# Live gating block (openai path).
# ---------------------------------------------------------------------------


def _settings() -> GeminiSettings:
    return GeminiSettings(
        model="qwen", project="t", location="us-central1", use_vertex=False
    )


async def _fake_stream(*_args, **_kwargs):
    # One text delta, no function calls -> the turn loop exits after round 1.
    yield TextDeltaEvent(delta="ok")


async def _drive_and_record_ks(top_score: float, monkeypatch) -> list[int]:
    """Drive one openai-path turn; return the k values retrieve_ranked_tools was
    called with. A poor fit re-ranks at WIDEN_K; a good fit does not."""
    monkeypatch.setenv("MODEL_PROVIDER", "openai")
    monkeypatch.delenv("TRID3NT_TOOL_GATING_TOPK", raising=False)  # default 24
    monkeypatch.delenv("TRID3NT_GATING_WIDEN_THRESHOLD", raising=False)  # 0.035

    ks: list[int] = []

    def _fake_ranked(user_text, k=25):
        ks.append(int(k))
        # A single real-registry name so gate_tool_registry has something to
        # look at; the score drives the widen decision.
        return [("geocode_location", float(top_score))]

    state = agent_server.SessionState(session_id=new_ulid())
    sock = MagicMock()

    async def _noop_send(msg):
        return None

    sock.send = _noop_send
    with patch.object(agent_server, "build_client", return_value=MagicMock()), patch.object(
        agent_server, "build_tool_declarations", return_value=[]
    ), patch.object(
        agent_server, "stream_events_with_contents", _fake_stream
    ), patch(
        "trid3nt_server.tools.discovery.tool_retrieval.retrieve_ranked_tools",
        _fake_ranked,
    ):
        await agent_server._stream_gemini_reply(
            sock, state, _settings(), "hmm something vague", "research"
        )
    return ks


@pytest.mark.asyncio
async def test_widen_fires_on_poor_fit(monkeypatch, caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="trid3nt_server.server"):
        ks = await _drive_and_record_ks(top_score=0.01, monkeypatch=monkeypatch)
    assert 24 in ks, ks
    assert WIDEN_K in ks, f"poor fit must re-rank at k={WIDEN_K}: {ks}"
    assert any(
        "POOR-FIT widen" in r.message for r in caplog.records
    ), "the widen must be logged"


@pytest.mark.asyncio
async def test_widen_does_not_fire_on_good_fit(monkeypatch):
    # NB: retrieve_ranked_tools is ALSO called at k=8 by the ADR-0018
    # tool-candidates gate downstream -- unrelated to the widen. Assert the
    # widen specifically: the gate ranks at 24 and NEVER re-ranks at WIDEN_K.
    ks = await _drive_and_record_ks(top_score=0.9, monkeypatch=monkeypatch)
    assert 24 in ks, ks
    assert WIDEN_K not in ks, f"good fit must NOT re-rank at k={WIDEN_K}: {ks}"
