"""Deferred study review messages cross a JSON transport."""

import json
from types import SimpleNamespace

import pytest

from trid3nt_contracts.common import Origin, SyntheticInput

from trid3nt.sdk.run_messages import RunEvent
from trid3nt.sdk.script_run import ReviewRequest, ReviewResponse
from trid3nt.inputs.review import ReviewOutcome


def test_review_request_and_reply_are_structured_transport_values():
    entry = SyntheticInput(param="npf_k", value=2.0, basis=Origin.DERIVED)
    request = ReviewRequest(id="ranking", tool_name="calibrate", entries=[entry], params={"tuned": ["npf_k"]})
    restored = ReviewRequest.model_validate(json.loads(request.model_dump_json()))
    assert restored == request
    decision = ReviewOutcome(proceed=True, entries=[entry], params={"tuned": ["sfr_rhk"]})
    reply = ReviewResponse.from_outcome(restored.id, decision)
    result = ReviewResponse.model_validate(json.loads(reply.model_dump_json()))
    assert result.id == "ranking"
    assert result.outcome() == decision


@pytest.mark.asyncio
async def test_parent_answers_the_existing_study_card(monkeypatch):
    from trid3nt.inputs import review
    from trid3nt_server.server.dispatch.script_child import _Relay, _messages

    request = ReviewRequest(id="ranking", tool_name="calibrate", entries=[], params={"tuned": ["npf_k"]})
    messages = iter([RunEvent.event("review", request.model_dump(mode="json")).model_dump(),
                     RunEvent.event("complete", None, {"ok": True}).model_dump()])
    sent = []

    def recv():
        value = next(messages, None)
        if value is None:
            raise EOFError
        return value

    async def gate(**kwargs):
        assert kwargs["tool_name"] == "calibrate"
        assert kwargs["params"] == {"tuned": ["npf_k"]}
        return ReviewOutcome(proceed=True, entries=[], params={"tuned": ["sfr_rhk"]})

    async def wait():
        return 0

    monkeypatch.setattr(review, "gate_input_review", gate)
    result = await _messages(SimpleNamespace(recv=recv, send=sent.append), SimpleNamespace(wait=wait),
                             _Relay(None), {"case": "reviewed"})
    assert result == ("complete", None, {"ok": True})
    assert sent[0] == {"case": "reviewed"}
    decision = ReviewResponse.model_validate(sent[1])
    assert decision.id == request.id and decision.params == {"tuned": ["sfr_rhk"]}
