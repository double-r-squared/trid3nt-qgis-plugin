"""``set_config`` goes through the one input-review gate: a range refusal precedes the card, the card shows what changes, and only a proceed writes."""

from __future__ import annotations

import asyncio
import json

import pytest

from trid3nt_server import settings
from trid3nt_server.settings import reader
from trid3nt_server.inputs.gate import pending
from trid3nt_server.inputs.gate.input_review import GateCard
from trid3nt_server.render import pipeline_emitter as pe
from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.meta.set_config.set_config import SettingDeclinedError, set_config
from trid3nt_server.workflows.runtime.module import SlotRefused

from tests.gates.test_input_review_gate import _FakeEmitter, _drive


@pytest.fixture
def emitter(monkeypatch):
    fake = _FakeEmitter("sess-config")
    monkeypatch.setattr(pe, "current_emitter", lambda: fake)
    return fake


def _card(fake):
    payload = fake.sent[-1][1]
    return payload, payload.param_sheet.rows[0]


def test_the_tool_is_registered_and_states_its_scopes():
    entry = TOOL_REGISTRY["set_config"]
    assert entry.metadata.ttl_class == "live-no-cache"
    assert "scope" in (entry.fn.__doc__ or "")


@pytest.mark.asyncio
async def test_a_value_outside_the_range_refuses_before_any_card_opens(emitter):
    with pytest.raises(SlotRefused, match="FILL_MAX_DISTANCE"):
        await set_config("FILL_MAX_DISTANCE", 0)
    with pytest.raises(SlotRefused, match="scope"):
        await set_config("FILL_MAX_DISTANCE", 5, scope="forever")
    assert emitter.sent == []


@pytest.mark.asyncio
async def test_a_proceed_writes_the_user_file_and_the_card_names_what_changes(emitter, user_settings):
    driver = asyncio.create_task(_drive("proceed"))
    out = await set_config("fill_max_distance", 200)
    await driver
    payload, row = _card(emitter)
    assert row.name == "FILL_MAX_DISTANCE" and row.value == 200 and row.units == "px"
    assert row.bounds == (1.0, 1e12) and "100 (default)" in row.note and "user" in row.note
    assert "fills" in row.desc.lower() or "pixels" in row.desc
    assert out["scope"] == "user" and out["previous"] is None and out["in_force"] == 200
    assert json.loads(reader.user_file().read_text()) == {"FILL_MAX_DISTANCE": 200}


@pytest.mark.asyncio
async def test_a_cancel_changes_nothing(emitter):
    driver = asyncio.create_task(_drive("cancel"))
    with pytest.raises(SettingDeclinedError):
        await set_config("FILL_MAX_DISTANCE", 200)
    await driver
    assert not reader.user_file().exists()


@pytest.mark.asyncio
async def test_an_edit_on_the_card_is_held_to_the_range_and_the_edited_value_stands(emitter):
    seen: set[str] = set()
    first = asyncio.create_task(_drive("narrow_scope", {"FILL_MAX_DISTANCE": -5}, seen=seen))
    second = asyncio.create_task(_drive("narrow_scope", {"FILL_MAX_DISTANCE": 60}, seen=seen))
    third = asyncio.create_task(_drive("proceed", seen=seen))
    out = await set_config("FILL_MAX_DISTANCE", 200)
    await asyncio.gather(first, second, third)
    assert out["value"] == 60 and settings.resolve("FILL_MAX_DISTANCE") == (60, "user")


@pytest.mark.asyncio
async def test_run_scope_binds_the_sessions_next_run_and_a_reset_unbinds_it(emitter):
    driver = asyncio.create_task(_drive("proceed"))
    await set_config("FILL_MAX_DISTANCE", 30, scope="run")
    await driver
    assert settings.session_run("sess-config") == {"FILL_MAX_DISTANCE": 30}
    assert settings.resolve("FILL_MAX_DISTANCE") == (30, "run")
    assert not reader.user_file().exists()
    driver = asyncio.create_task(_drive("proceed"))
    await set_config("FILL_MAX_DISTANCE", reset=True, scope="run")
    await driver
    assert settings.session_run("sess-config") == {}
    settings.clear_session_run()


@pytest.mark.asyncio
async def test_with_no_session_the_call_refuses_rather_than_writes(monkeypatch):
    monkeypatch.setattr(pe, "current_emitter", lambda: None)
    with pytest.raises(Exception, match="NO_SESSION"):
        await set_config("FILL_MAX_DISTANCE", 200)
    assert not reader.user_file().exists()
