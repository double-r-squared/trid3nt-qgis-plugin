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
from trid3nt_server.tools.fetchers._router.executors import qgis_provider


@pytest.fixture()
def pending():
    asyncio.set_event_loop(asyncio.new_event_loop())
    running = asyncio.get_event_loop()
    fut = running.create_future()
    qgis_provider._PENDING_LAYER["row-key"] = ("session-1", fut)
    yield fut
    qgis_provider._PENDING_LAYER.pop("row-key", None)
    running.close()


def test_the_owner_session_resolves_its_own_request(pending) -> None:
    answer = LayerResponsePayload(key="row-key", uri="s3://bucket/staged.gpkg")
    assert qgis_provider.resolve_pending_layer("session-1", answer) is True
    assert pending.result() is answer
    assert "row-key" not in qgis_provider._PENDING_LAYER


def test_a_foreign_session_cannot_answer_it(pending) -> None:
    answer = LayerResponsePayload(key="row-key", uri="s3://bucket/staged.gpkg")
    assert qgis_provider.resolve_pending_layer("session-2", answer) is False
    assert not pending.done()


def test_an_unknown_key_is_refused_rather_than_invented() -> None:
    assert qgis_provider.resolve_pending_layer(
        "session-1", LayerResponsePayload(key="no-such-row")
    ) is False


def test_the_connection_loop_dispatches_the_answer() -> None:
    src = inspect.getsource(loop_module)
    assert 'elif msg_type == "layer-response":' in src
    assert "resolve_pending_layer(state.session_id, layer)" in src
    assert loop_module.resolve_pending_layer is qgis_provider.resolve_pending_layer


def test_a_malformed_answer_has_the_message_the_branch_reports() -> None:
    with pytest.raises(ValidationError) as caught:
        LayerResponsePayload.model_validate({"uri": "s3://bucket/x"})
    assert caught.value.errors()[0]["msg"]
