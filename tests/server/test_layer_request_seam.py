"""The daemon side of the borrowed-provider pair: the connection loop answers a
layer-response the way it answers a processing-response, and the pending answer
is owner-checked so a sibling session cannot resolve another's request."""

from __future__ import annotations

import asyncio
import inspect

import pytest
from pydantic import ValidationError

from trid3nt_contracts.ws import LayerResponsePayload
from trid3nt_server.server.protocol import loop as loop_module
from trid3nt_server.inputs.gate.pending import _PENDING_LAYER


@pytest.fixture()
def pending():
    asyncio.set_event_loop(asyncio.new_event_loop())
    running = asyncio.get_event_loop()
    fut = running.create_future()
    _PENDING_LAYER["row-key"] = ("session-1", fut)
    yield fut
    _PENDING_LAYER.pop("row-key", None)
    running.close()


def test_the_owner_session_resolves_its_own_request(pending) -> None:
    answer = LayerResponsePayload(key="row-key", uri="s3://bucket/staged.gpkg")
    assert _PENDING_LAYER.resolve("session-1", "row-key", answer) is True
    assert pending.result() is answer
    assert "row-key" not in _PENDING_LAYER


def test_a_foreign_session_cannot_answer_it(pending) -> None:
    answer = LayerResponsePayload(key="row-key", uri="s3://bucket/staged.gpkg")
    assert _PENDING_LAYER.resolve("session-2", "row-key", answer) is False
    assert not pending.done()


def test_an_unknown_key_is_refused_rather_than_invented() -> None:
    assert _PENDING_LAYER.resolve(
        "session-1", "no-such-row", LayerResponsePayload(key="no-such-row")
    ) is False


def test_the_connection_loop_dispatches_the_answer() -> None:
    src = inspect.getsource(loop_module)
    assert 'elif msg_type == "layer-response":' in src
    assert "_PENDING_LAYER.resolve(state.session_id, layer.key, layer)" in src
    assert loop_module._PENDING_LAYER is _PENDING_LAYER


def test_a_malformed_answer_has_the_message_the_branch_reports() -> None:
    with pytest.raises(ValidationError) as caught:
        LayerResponsePayload.model_validate({"uri": "s3://bucket/x"})
    assert caught.value.errors()[0]["msg"]
