"""A step failure carries a sentence, or it is not a failure anything can record.

The typed error is where every runner's refusal lands, so an exception that says
nothing is named by its type there rather than reaching the packet as a red step
with an empty message.
"""

from __future__ import annotations

import pytest

from trid3nt_server.workflows.runtime.errors import (
    NamelessFailureError,
    StepFailedError,
    said,
)


class _Silent(RuntimeError):
    """A refusal raised with no arguments, which stringifies to nothing."""


def test_an_exception_that_says_nothing_is_named_by_its_type() -> None:
    assert said(_Silent()) == "_Silent (raised saying nothing)"
    assert said(RuntimeError("the reader found no primary layer")) == (
        "the reader found no primary layer")


def test_a_step_failure_raised_with_no_sentence_is_itself_refused() -> None:
    with pytest.raises(NamelessFailureError) as caught:
        StepFailedError("", step="outputs")

    assert "outputs" in str(caught.value)
    assert caught.value.error_code == "FAILURE_UNNAMED"


def test_a_step_failure_over_a_silent_cause_still_says_what_stopped_it() -> None:
    failure = StepFailedError(f"step 'outputs' failed: {said(_Silent())}",
                              error_code="STEP_FAILED", step="outputs")

    assert "raised saying nothing" in str(failure)


@pytest.mark.asyncio
async def test_a_runner_that_raises_silently_reaches_the_caller_named() -> None:
    from trid3nt_server.workflows.runtime.interpreter import _call_fn

    def runner():
        raise _Silent()

    with pytest.raises(StepFailedError) as caught:
        await _call_fn(runner, {}, "outputs")

    assert "outputs" in str(caught.value)
    assert "raised saying nothing" in str(caught.value)


def test_the_failure_envelope_states_a_sentence_for_a_silent_refusal() -> None:
    """The envelope's message is the only thing a card can print."""
    from trid3nt_server.workflows.runtime.workflow import Workflow

    envelope = Workflow._error(
        _AnyWorkflow(), "TELEMAC_INTERNAL_ERROR", _Silent())

    assert envelope["error_message"] == "_Silent (raised saying nothing)"


class _AnyWorkflow:
    """Any workflow: ``_error`` reads nothing off the instance."""
