"""A step that fails NAMES why, or the machinery refuses to record it failed.

A run that stops in silence leaves a red card and nothing to read off it, so the
code and the sentence are not optional at either seam: the transition that marks
a step failed, and the verdict the snapshot states over the steps.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from trid3nt_contracts import new_ulid

from trid3nt_server.render.pipeline_emitter import (
    NamelessFailureError,
    PipelineEmitter,
)


class _Sink:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    async def __call__(self, text: str) -> None:
        self.frames.append(json.loads(text))


@pytest.fixture()
def emitter() -> PipelineEmitter:
    return PipelineEmitter(session_id=new_ulid(), sink=_Sink())


@pytest.mark.asyncio
@pytest.mark.parametrize("code, message", [
    ("", "the reader found no primary layer"),
    ("NO_PRIMARY_LAYER", ""),
    ("", ""),
    ("   ", "   "),
])
async def test_a_failure_with_no_name_is_refused_rather_than_recorded(
        emitter: PipelineEmitter, code: str, message: str) -> None:
    step_id = await emitter.add_step(name="outputs", tool_name="run_workflow")
    await emitter.mark_running(step_id)

    with pytest.raises(NamelessFailureError) as caught:
        await emitter.mark_failed(step_id, error_code=code, error_message=message)

    assert step_id in str(caught.value)
    assert emitter.current_snapshot().steps[0].state == "running"


@pytest.mark.asyncio
async def test_a_named_failure_is_recorded_with_its_code_and_sentence(
        emitter: PipelineEmitter) -> None:
    step_id = await emitter.add_step(name="fetch", tool_name="fetch_gauge")
    await emitter.mark_running(step_id)

    await emitter.mark_failed(step_id, error_code="UPSTREAM_API_ERROR",
                              error_message="USGS answered 503")

    failed = emitter.current_snapshot().steps[0]
    assert (failed.state, failed.error_code) == ("failed", "UPSTREAM_API_ERROR")
    assert failed.error_message == "USGS answered 503"


@pytest.mark.asyncio
async def test_the_verdict_refuses_to_read_failed_off_a_step_that_names_nothing(
        emitter: PipelineEmitter) -> None:
    """The packet's verdict is stated over the steps, so it reads the names too."""
    step_id = await emitter.add_step(name="outputs", tool_name="run_workflow")
    await emitter.mark_running(step_id)
    await emitter.mark_failed(step_id, error_code="STEP_FAILED",
                              error_message="the solve stopped")
    emitter._steps[step_id].error_code = None

    with pytest.raises(NamelessFailureError):
        emitter.current_snapshot()
