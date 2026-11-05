"""The card a keyed source asks its key on: what it draws, what launches the retry, and that no value rides the reply."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from trid3nt_contracts.payload_warning import PayloadConfirmationEnvelopePayload
from trid3nt_server.inputs.gate import pending
from trid3nt_server.inputs.gate.cards.key_ask import KeyDeclinedError, ask_for_key
from trid3nt_server.model.credentials import resolver
from trid3nt_server.tools.fetchers._fetch_common import KeyRejectedError, KeyRequiredError


class _Emitter:
    session_id = "sess-key-card"

    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send_envelope(self, message_type: str, payload: Any) -> None:
        self.sent.append(payload)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("TRID3NT_AIRNOW_API_KEY", raising=False)
    resolver._SESSION_CREDENTIALS.clear()
    yield
    resolver._SESSION_CREDENTIALS.clear()


async def _reply(decision: str, before=None) -> None:
    """Answer the open card as the plugin does: store the typed key over ``secret-add``, then send the decision."""
    for _ in range(2000):
        fresh = [(wid, fut) for wid, (_s, fut) in pending._PENDING_CONFIRMATIONS.items() if not fut.done()]
        if fresh:
            wid, fut = fresh[0]
            if before is not None:
                before()
            fut.set_result(PayloadConfirmationEnvelopePayload(warning_id=wid, decision=decision, revised_args=None))
            return
        await asyncio.sleep(0.005)
    raise AssertionError("no card was opened")


def _store(value: str):
    return lambda: resolver.set_session_credential("sess-key-card", "airnow", value)


async def _ask(asked, *replies):
    emitter = _Emitter()
    token = resolver.bind_session("sess-key-card")

    async def drive():
        for decision, before in replies:
            await _reply(decision, before)

    task = asyncio.ensure_future(drive())
    try:
        got = await asyncio.wait_for(ask_for_key("fetch_airnow_air_quality", asked, emitter=emitter), 10)
    except Exception as exc:  # noqa: BLE001 - the test reads the refusal
        got = exc
    finally:
        resolver.unbind_session(token)
    await task
    return emitter, got


def test_the_card_is_one_masked_row_named_by_the_credential() -> None:
    emitter, got = asyncio.run(_ask(KeyRequiredError("fetch_airnow_air_quality", "airnow", "EPA AirNow"),
                                    ("proceed", _store("typed-test-key"))))
    assert "typed-test-key" not in repr(emitter.sent)
    sheet = emitter.sent[0].param_sheet
    assert [(r.name, r.secret, r.value) for r in sheet.rows] == [("airnow", "required", None)]
    assert "EPA AirNow" in sheet.title and sheet.rows[0].source_badge == "EPA AirNow"
    assert got is True


def test_a_proceed_with_no_key_stored_launches_nothing_and_redraws() -> None:
    emitter, got = asyncio.run(_ask(KeyRequiredError("s", "airnow", "EPA AirNow"),
                                    ("proceed", None), ("proceed", _store("k"))))
    assert len(emitter.sent) == 2 and got is True
    assert emitter.sent[1].param_sheet.rows[0].secret == "required"
    assert "k" not in [r.value for r in emitter.sent[1].param_sheet.rows]


def test_a_rejected_key_is_drawn_rejected_and_only_a_different_key_retries() -> None:
    resolver.set_session_credential("sess-key-card", "airnow", "old-test-key")
    emitter, got = asyncio.run(_ask(KeyRejectedError("s", "airnow", "EPA AirNow"),
                                    ("proceed", None), ("proceed", _store("new-test-key"))))
    assert emitter.sent[0].param_sheet.rows[0].secret == "rejected"
    assert len(emitter.sent) == 2 and got is True


def test_a_cancel_is_a_decline_not_an_error() -> None:
    _, got = asyncio.run(_ask(KeyRequiredError("s", "airnow", "EPA AirNow"), ("cancel", None)))
    assert isinstance(got, KeyDeclinedError)
    assert got.declined is True and got.error_code == "USER_INPUT_CANCELLED"


def test_a_card_with_no_session_refuses_without_asking() -> None:
    got = asyncio.run(ask_for_key("t", KeyRequiredError("s", "airnow", "EPA AirNow"), emitter=None))
    assert got is False


def test_the_link_the_provider_sent_rides_the_row_note_and_no_other() -> None:
    emitter, _ = asyncio.run(_ask(KeyRequiredError("s", "airnow", "EPA AirNow", "https://example.org/k"),
                                  ("cancel", None)))
    assert "https://example.org/k" in emitter.sent[0].param_sheet.rows[0].note
    emitter, _ = asyncio.run(_ask(KeyRequiredError("s", "airnow", "EPA AirNow"), ("cancel", None)))
    assert "http" not in emitter.sent[0].param_sheet.rows[0].note
