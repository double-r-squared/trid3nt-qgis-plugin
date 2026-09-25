"""CONTINUE_FROM is an input of the run: it names a completed run, and this one
opens at the file that run's solve wrote. Offline.

Covered: the accept (the solved file reaches the step that reads the run's
continuation, the note and the journal line say which run was continued) and the
refusal by name of a run id whose journal line names no solved result.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from trid3nt_contracts.tool_registry import AtomicToolMetadata
from trid3nt_server.workflows.runtime import (
    Continued,
    Param,
    ParamRef,
    Ref,
    Step,
    Workflow,
    doors,
    journal,
)

_HERE = "tests.runtime.test_continue_from"

_OPENED: list[Any] = []


@pytest.fixture(autouse=True)
def _tmp_persistence(tmp_path, monkeypatch):
    monkeypatch.setenv("TRID3NT_DEV_PERSISTENCE_DIR", str(tmp_path / "persistence"))
    _OPENED.clear()
    from trid3nt_server.workflows.runtime import run_products

    async def _skip(run_id, *, charts):
        return []

    monkeypatch.setattr(run_products, "persist_run_products", _skip)
    yield


def open_water(*, where: str, continue_from: Any = None) -> dict[str, Any]:
    _OPENED.append(continue_from)
    return {"where": where}


def solve(*, opened: dict, tag: str) -> dict[str, Any]:
    return {"run_id": tag, "uri": f"s3://runs/{tag}/r2d.slf"}


def publish(*, solved: dict) -> "_Result":
    return _Result(solved["run_id"])


class _Result:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.layer_id = "L"
        self.fallback_note = None
        self.synthetic_inputs: list[Any] = []

    def model_copy(self, *, update: dict[str, Any]) -> "_Result":
        for key, val in update.items():
            setattr(self, key, val)
        return self


def _probe() -> Workflow:
    class Probe(Workflow):
        engine = "probe"
        solve_step = "solve"

        def steps(self):
            return [
                Step(runner=f"{_HERE}.open_water",
                     kwargs={"where": ParamRef("tag"),
                             "continue_from": Continued}).named("opened"),
                Step(runner=f"{_HERE}.solve",
                     kwargs={"opened": Ref("opened"), "tag": ParamRef("tag")}
                     ).named("solve"),
                Step(runner=f"{_HERE}.publish",
                     kwargs={"solved": Ref("solve")}).named("publish"),
            ]

    return Probe(
        metadata=AtomicToolMetadata(name="probe_continue", ttl_class="live-no-cache",
                                    source_class="workflow_dispatch",
                                    cacheable=False, engine="probe",
                                    tier="template"),
        params=(Param("tag", door=doors.USER, default="A", desc="the run's tag"),),
        template=SimpleNamespace())


@pytest.mark.asyncio
async def test_a_run_continues_from_the_file_a_journaled_run_solved():
    wf = _probe()
    await wf.run({"tag": "RUN1", "input_mode": "auto"})
    assert journal.read_records()[-1]["solved"] == "s3://runs/RUN1/r2d.slf"

    child = await wf.run({"tag": "RUN2", "input_mode": "auto",
                          "continue_from": "RUN1"})

    assert _OPENED == [None, "s3://runs/RUN1/r2d.slf"]
    assert "continuing run RUN1" in child.fallback_note
    line = journal.read_records()[-1]
    assert (line["run_id"], line["continued_from"]) == ("RUN2", "RUN1")


@pytest.mark.asyncio
async def test_a_run_id_with_no_solved_result_is_refused_by_name():
    wf = _probe()
    out = await wf.run({"tag": "RUN2", "input_mode": "auto",
                        "continue_from": "NOSUCH"})

    assert out["status"] == "error"
    assert out["error_code"] == "CONTINUATION_UNSOLVED"
    assert "continue_from='NOSUCH'" in out["error_message"]
    assert _OPENED == []
