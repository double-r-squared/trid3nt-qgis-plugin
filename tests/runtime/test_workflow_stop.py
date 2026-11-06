"""A stopped solve publishes what the run wrote, labelled partial, and ends as the stop."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from trid3nt_server.inputs.run_notes import bind_notes, drain_notes
from trid3nt_server.workflows.runtime.workflow import RunResult, Workflow
from trid3nt_server.workflows.solver.stop import SolveStopped, Stopped

_STAND_IN = SimpleNamespace(solve_step="solve", name="probe")


def _stop() -> SolveStopped:
    stop = SolveStopped("RUN1", "step 500, t = 367.5 s")
    stop.solved = {"run_id": "RUN1", "uri": "s3://runs/RUN1/r.slf"}
    return stop


def test_the_publish_gets_the_partial_sentence_and_the_stop_carries_what_it_returned():
    seen = {}

    async def products(handle, partial):
        seen.update(handle=handle, partial=partial)
        return "RECORD"

    run: dict = {}
    with pytest.raises(Stopped) as raised:
        asyncio.run(Workflow.stopped(_STAND_IN, _stop(), run, products))

    assert seen["partial"] == "stopped by the user at step 500, t = 367.5 s"
    assert seen["handle"]["run_id"] == "RUN1" and run["solve"] is seen["handle"]
    assert raised.value.value == "RECORD" and isinstance(raised.value, asyncio.CancelledError)
    assert raised.value.why == seen["partial"]


def test_a_run_whose_output_cannot_be_read_publishes_nothing_and_says_why():
    async def products(handle, partial):
        raise ValueError("the SELAFIN reader could not read r.slf")

    token = bind_notes()
    with pytest.raises(Stopped) as raised:
        asyncio.run(Workflow.stopped(_STAND_IN, _stop(), {}, products))
    said = drain_notes(token)

    assert raised.value.value is None
    assert said and "nothing of what it wrote was published" in said[0] and "SELAFIN reader" in said[0]


def test_a_stop_with_no_stated_step_says_so():
    assert SolveStopped("R", None).sentence() == "stopped by the user before the engine stated a step"


def test_the_journal_record_states_the_stop_and_a_finished_run_states_none():
    from trid3nt_server.workflows.runtime import journal

    kept = dict(run_id="R", engine="telemac", module="telemac2d", inputs={}, result=None,
                wall_seconds=1.0, origin="live", notes=[])
    assert journal.build_record(**kept, stopped="stopped by the user at step 5")["stopped"] == "stopped by the user at step 5"
    assert journal.build_record(**kept)["stopped"] is None
