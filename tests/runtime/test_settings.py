"""The settings layer: one dictionary, three levels, every read journaled.

A setting resolves dictionary default -> the user file -> the run; a run states one the way it states
a body keyword (``settings: NAME``); the fill and the launch journal what they read with its level."""

from __future__ import annotations

import asyncio
import json

import pytest

from trid3nt_server import settings
from trid3nt_server.settings import reader
from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.workflows.runtime import fill as fill_mod
from trid3nt_server.workflows.runtime import journal
from trid3nt_server.workflows.runtime.fill import Fill, fill
from trid3nt_server.workflows.runtime.module import SlotRefused
from trid3nt_server.workflows.runtime.workflow import RunResult


def test_every_entry_carries_its_family_default_and_where_the_default_comes_from():
    table = settings.dictionary()
    assert {"FILL_MAX_DISTANCE", "FILL_ALGORITHM", "MERGE_BUFFER_CELLS"} <= set(table)
    assert {slot.rubrique[0] for slot in table.values()} == {"bed", "notes", "gate", "render", "solve", "server", "model"}
    for name, slot in table.items():
        assert slot.source, name
        assert slot.desc, name
        assert not slot.is_open, name
        assert slot.check(slot.engine_default) == slot.engine_default
    assert table["FILL_MAX_DISTANCE"].engine_default == 100
    assert "The default is 100 pixels" in table["FILL_MAX_DISTANCE"].source
    assert table["MERGE_BUFFER_CELLS"].engine_default == 0
    assert "by default 0" in table["MERGE_BUFFER_CELLS"].source


def test_describe_keywords_serves_the_dictionary_with_its_source():
    from trid3nt_server.tools.search.describe_keywords.describe_keywords import describe_keywords

    found = describe_keywords(module="settings", query="how far a fill searches")
    top = found["matches"][0]["keywords"][0]
    assert top["keyword"] == "FILL_MAX_DISTANCE"
    assert top["engine_default"] == 100 and top["unit"] == "px" and "gdal.org" in top["source"]


def test_a_value_resolves_default_then_user_then_run(user_settings):
    assert settings.resolve("fill_max_distance") == (100, "default")
    user_settings(FILL_MAX_DISTANCE=250)
    assert settings.resolve("FILL_MAX_DISTANCE") == (250, "user")
    token = settings.bind_run({"FILL_MAX_DISTANCE": 40})
    try:
        assert settings.resolve("FILL_MAX_DISTANCE") == (40, "run")
    finally:
        settings.reset_run(token)
    assert settings.resolve("FILL_MAX_DISTANCE") == (250, "user")


def test_a_real_setting_read_is_a_float_and_an_unknown_name_offers_the_nearest():
    assert isinstance(settings.setting("FETCH_MARGIN"), float)
    with pytest.raises(SlotRefused, match="FILL_MAX_DISTANCE"):
        settings.setting("FILL_MAX_DISTANC")


def test_the_user_file_is_written_whole_held_to_its_range_and_reset_by_none(user_settings):
    path = settings.write_user("FILL_MAX_DISTANCE", 300)
    assert json.loads(path.read_text()) == {"FILL_MAX_DISTANCE": 300}
    with pytest.raises(SlotRefused):
        settings.write_user("FILL_MAX_DISTANCE", 0)
    with pytest.raises(SlotRefused):
        settings.write_user("SAMPLE_INTERP", "kriging")
    with pytest.raises(SlotRefused):
        settings.write_user("FILL_MAX_DISTANCE", 2.5)
    assert json.loads(path.read_text()) == {"FILL_MAX_DISTANCE": 300}
    settings.write_user("FILL_MAX_DISTANCE", None)
    assert json.loads(path.read_text()) == {}


def test_a_user_file_that_is_not_a_mapping_refuses_by_name(user_settings):
    reader.user_file().write_text("[1]")
    with pytest.raises(settings.SettingsFileError, match="mapping"):
        settings.setting("FILL_MAX_DISTANCE")


def test_a_run_states_a_setting_under_the_settings_body():
    stated, refused, rest = settings.split_keywords({
        "settings: FILL_MAX_DISTANCE": 200, "settings: SAMPLE_INTERP": "kriging",
        "settings: NOPE": 1, "LAW OF BOTTOM FRICTION": 4})
    assert stated == {"FILL_MAX_DISTANCE": 200}
    assert set(refused) == {"settings: SAMPLE_INTERP", "settings: NOPE"}
    assert rest == {"LAW OF BOTTOM FRICTION": 4}


def test_a_read_is_journaled_with_the_level_it_resolved_at(user_settings):
    user_settings(MERGE_MAX_CELLS=5)
    token = journal.bind_settings()
    run = settings.bind_run({"FETCH_MARGIN": 0.5})
    try:
        settings.setting("MERGE_MAX_CELLS")
        settings.setting("FETCH_MARGIN")
        settings.setting("DEM_MAX_CELLS")
    finally:
        settings.reset_run(run)
    read = journal.drain_settings(token)
    assert {k: v["level"] for k, v in read.items()} == {
        "MERGE_MAX_CELLS": "user", "FETCH_MARGIN": "run", "DEM_MAX_CELLS": "default"}
    assert read["MERGE_MAX_CELLS"]["value"] == 5


def test_the_record_lists_the_settings_the_run_read():
    from types import SimpleNamespace

    rec = journal.build_record(
        run_id="R", engine="telemac", module="telemac2d", inputs={},
        result=SimpleNamespace(mesh_size_m=1.0), wall_seconds=1.0, origin="headless", notes=[],
        settings={"FILL_MAX_DISTANCE": {"value": 200, "level": "run"}})
    assert rec["settings"] == [{"name": "FILL_MAX_DISTANCE", "value": 200, "level": "run"}]


def test_a_keyword_run_states_a_setting_and_the_run_journals_it_read(monkeypatch):
    """The fill and the launch each read what they read; the run holds both, at the level the run stated."""
    async def _produce(env, decl):
        settings.setting("SAMPLE_INTERP")
        return decl.name

    async def _launch(self, state):
        settings.setting("FILL_MAX_DISTANCE")
        settings.setting("FETCH_MARGIN")
        return RunResult(value=None)

    monkeypatch.setattr(fill_mod, "_produce", _produce)
    wf = TOOL_REGISTRY["telemac_ice_cover"].fn.workflow
    monkeypatch.setattr(type(wf), "launch", _launch)
    state = asyncio.run(fill(Fill(workflow=wf), {
        "input_mode": "auto", "seed": [0.5, 0.5], "event_time": "2024-06-01T00:00:00Z",
        "keywords": {"settings: FILL_MAX_DISTANCE": 200}}))
    run = asyncio.run(wf._launched(state))
    assert state.settings == {"FILL_MAX_DISTANCE": 200}
    assert run.settings["FILL_MAX_DISTANCE"] == {"value": 200, "level": "run"}
    assert run.settings["FETCH_MARGIN"]["level"] == "default"
    assert state.settings_read["SAMPLE_INTERP"]["level"] == "default"


def test_a_setting_stated_out_of_range_is_a_refused_keyword_not_a_run():
    wf = TOOL_REGISTRY["telemac_ice_cover"].fn.workflow
    state = asyncio.run(fill(Fill(workflow=wf), {
        "input_mode": "auto", "seed": [0.5, 0.5], "event_time": "2024-06-01T00:00:00Z",
        "keywords": {"settings: FILL_MAX_DISTANCE": 0}}))
    verdict = state.inputs["settings: FILL_MAX_DISTANCE"]
    assert verdict.code == "KEYWORD_REFUSED" and "FILL_MAX_DISTANCE" in verdict.reason
    assert not state.ready
