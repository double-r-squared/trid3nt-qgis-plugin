"""A live workflow captures its reviewed script before it starts solving."""

import pytest

from trid3nt.workflows.runtime.fill import Fill
from trid3nt.workflows.runtime.workflow import Deck, RunResult, Workflow


@pytest.mark.asyncio
async def test_lifecycle_drafts_from_the_prepared_deck_and_keeps_the_solve(monkeypatch):
    from trid3nt.workflows.runtime import script

    events = []
    flow = object.__new__(Workflow)
    state = Fill(flow)
    reviewed = Deck(state, run_id="RUN")

    async def prepared(state):
        events.append("prepare")
        return reviewed

    async def solved(deck):
        assert deck is reviewed
        events.append("solve")
        return RunResult("value")

    monkeypatch.setattr(flow, "prepared", prepared)
    monkeypatch.setattr(flow, "_solved", solved)

    def collect(capture, deck):
        assert deck is reviewed
        assert capture.wire == {"spill_duration_s": 300}
        events.append("script")
        return {"code": "GATE_INPUT_REQUIRED"}

    monkeypatch.setattr(script, "_HOST", lambda: collect)
    with script.capture({"spill_duration_s": 300}) as record:
        result = await flow._launched(state)
        assert record.receipt == {"code": "GATE_INPUT_REQUIRED"}
    assert result.value == "value" and events == ["prepare", "script", "solve"]
