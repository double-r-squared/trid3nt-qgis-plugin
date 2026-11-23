"""The reviewed state crosses into a fresh interpreter without rebuilding the deck."""

from types import MappingProxyType

import pytest

from trid3nt import case
from trid3nt.inputs.slots import DOMAIN
from trid3nt.workflows.runtime.fill import Fill, production
from trid3nt.workflows.runtime.workflow import Deck


def test_snapshot_rebinds_the_pinned_workflow_and_slot_identities(tmp_path):
    from trid3nt.sdk.script_run import load_deck, save_deck

    source = case(tmp_path / "daemon").telemac_dye_release()
    destination = case(tmp_path / "child").telemac_dye_release()
    state = Fill(source._workflow)
    env = production(state)
    env.run["test"] = MappingProxyType({"slot": DOMAIN})
    deck = Deck(state, run_id="RUN")
    handed = load_deck(save_deck(deck), destination._workflow)
    assert handed.state.workflow is destination._workflow
    assert handed.state.env.inputs is handed.state.inputs
    assert handed.state.env.run["test"]["slot"] is DOMAIN


@pytest.mark.parametrize("code", ["CODEGEN_DECK_MISSING", "CODEGEN_LITERAL_UNSUPPORTED"])
@pytest.mark.asyncio
async def test_launch_with_no_script_uses_the_prepared_in_process_solve(tmp_path, monkeypatch, code):
    from trid3nt.workflows.runtime import script

    c = case(tmp_path / "case")
    model = c.calibrate() if code == "CODEGEN_DECK_MISSING" else c.telemac_dye_release()
    workflow = model._workflow
    state = Fill(workflow)
    deck = Deck(state)
    result = object()

    async def prepared(fill):
        assert fill is state
        return deck

    async def solved(staged):
        assert staged is deck
        return result

    async def child(*args):
        pytest.fail("a refused script must not start a child")

    monkeypatch.setattr(workflow, "prepared", prepared)
    monkeypatch.setattr(workflow, "_solved", solved)
    monkeypatch.setattr(script, "_HOST", lambda: lambda capture, deck: {"code": code})
    monkeypatch.setattr(script, "_RUN", child)
    with script.capture({}) as reviewed:
        assert await workflow._launched(state) is result
        assert reviewed.receipt == {"code": code}
