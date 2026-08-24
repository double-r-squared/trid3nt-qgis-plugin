"""A scripted plugin, offline: read the card off the wire, answer it back.

Shared by every test that drives a declared gate without a daemon. The client is
REAL in the ways that matter - it reads the envelope the gate actually put on the
sink (serialized through ``Envelope``, parsed back from JSON) and replies with a
contract-validated payload into the same pending registry the WS loop resolves -
so what a test proves with it is the wire round trip, not a patched-out gate.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import pytest

from trid3nt_contracts import new_ulid
from trid3nt_contracts.payload_warning import (
    PayloadConfirmationEnvelopePayload,
    PayloadWarningEnvelopePayload,
)
from trid3nt_contracts.ws import (
    Envelope,
    SpatialInputRequestPayload,
    SpatialInputResponsePayload,
)
from trid3nt_server.emission import pipeline_emitter as pe
from trid3nt_server.gates import pending
from trid3nt_server.server import spatial as server_spatial

__all__ = ["CardClient", "answer_draw_card", "answer_form_card", "card_client"]


class CardClient:
    """A test client on the emitter's sink: it reads envelopes off the wire.

    Envelopes arrive as the JSON the daemon would send, and are parsed back into
    their contract models - so a payload this client cannot read is a payload the
    plugin could not read either.
    """

    def __init__(self) -> None:
        self.session_id = new_ulid()
        self.envelopes: list[dict] = []

    def begin_substeps(self, total):
        return None

    @contextlib.asynccontextmanager
    async def substep(self, raw_name):
        yield "child"

    async def send_envelope(self, message_type: str, payload) -> None:
        wire = Envelope(type=message_type, session_id=self.session_id,
                        payload=payload).model_dump_json()
        self.envelopes.append(json.loads(wire))

    async def next_envelope(self, message_type: str, timeout: float = 5.0) -> dict:
        for _ in range(int(timeout / 0.005)):
            for env in self.envelopes:
                if env["type"] == message_type:
                    self.envelopes.remove(env)
                    return env
            await asyncio.sleep(0.005)
        raise AssertionError(f"no {message_type} envelope arrived")


@pytest.fixture
def card_client(monkeypatch) -> CardClient:
    """A :class:`CardClient` bound as the current emitter for the whole run."""
    from trid3nt_server.declarative import interpreter as _interp

    client = CardClient()
    monkeypatch.setattr(pe, "current_emitter", lambda: client)
    monkeypatch.setattr(_interp, "current_emitter", lambda: client)
    return client


async def answer_form_card(client: CardClient, revised: dict | None,
                           decision: str = "narrow_scope"):
    """Read the form card, answer it exactly as the plugin would."""
    env = await client.next_envelope("tool-payload-warning")
    warning = PayloadWarningEnvelopePayload.model_validate(env["payload"])
    reply = PayloadConfirmationEnvelopePayload(
        warning_id=warning.warning_id, decision=decision, revised_args=revised)
    assert pending._resolve_pending_confirmation(client.session_id, reply)
    return warning


async def answer_draw_card(client: CardClient, response_kwargs: dict):
    """Read the draw card, answer it exactly as the plugin would."""
    env = await client.next_envelope("spatial-input-request")
    request = SpatialInputRequestPayload.model_validate(env["payload"])
    reply = SpatialInputResponsePayload(request_id=request.request_id,
                                        **response_kwargs)
    assert server_spatial._resolve_pending_spatial_input(client.session_id, reply)
    return request
