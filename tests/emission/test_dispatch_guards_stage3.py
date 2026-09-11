"""Dispatch-guard hardening: each guard fires, does not over-fire, and can be off.

A string arg failing a ``Literal`` schema is fuzzy-corrected above a cutoff and
left to the typed-error path below it. After a geocode, a call whose bbox
intersects neither the geocoded bbox nor the active AOI gets an advisory warning
that never blocks. The fetch reuse short-circuit has a kill-switch."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal
from unittest.mock import patch

import pytest

from trid3nt_server import server as agent_server
from trid3nt_server import tools as agent_tools
from trid3nt_server.adapters.adapter import ModelSettings
from trid3nt_server.tools.tool_arg_normalizer import (
    fuzzy_correct_enum_args,
    normalize_args,
)
from trid3nt_server.tools import RegisteredTool
from trid3nt_server.emission.uri_registry import reset_uri_registries_for_tests
from trid3nt_contracts import new_ulid
from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata




def _enum_fn(
    style: Literal["flood_depth", "ndvi", "truecolor"],
    mode: Literal["fast", "full"] | None = None,
    bbox: list | None = None,
):
    """Stub tool fn with Literal params for the normalizer tests."""


def test_enum_fuzzy_fires_on_near_miss(monkeypatch):
    monkeypatch.delenv("TRID3NT_ENUM_FUZZY", raising=False)
    out = normalize_args(
        "stub_tool", {"style": "truecolour", "bbox": [0, 0, 1, 1]}, _enum_fn
    )
    assert out["style"] == "truecolor"
    assert out["bbox"] == [0, 0, 1, 1]


def test_enum_fuzzy_fires_on_case_and_separator(monkeypatch):
    monkeypatch.delenv("TRID3NT_ENUM_FUZZY", raising=False)
    out = normalize_args("stub_tool", {"style": "Flood-Depth"}, _enum_fn)
    assert out["style"] == "flood_depth"
    out = normalize_args("stub_tool", {"mode": "FULL"}, _enum_fn)
    assert out["mode"] == "full"


def test_enum_fuzzy_no_fire_below_cutoff(monkeypatch):
    """A genuinely-wrong value stays put -- the tool's typed error owns it."""
    monkeypatch.delenv("TRID3NT_ENUM_FUZZY", raising=False)
    out = normalize_args(
        "stub_tool", {"style": "population_density"}, _enum_fn
    )
    assert out["style"] == "population_density"


def test_enum_fuzzy_no_fire_on_exact_value(monkeypatch):
    monkeypatch.delenv("TRID3NT_ENUM_FUZZY", raising=False)
    out = normalize_args("stub_tool", {"style": "ndvi"}, _enum_fn)
    assert out["style"] == "ndvi"


def test_enum_fuzzy_kill_switch(monkeypatch):
    monkeypatch.setenv("TRID3NT_ENUM_FUZZY", "0")
    out = normalize_args("stub_tool", {"style": "truecolour"}, _enum_fn)
    assert out["style"] == "truecolour"


def test_enum_fuzzy_ignores_non_literal_params(monkeypatch):
    monkeypatch.delenv("TRID3NT_ENUM_FUZZY", raising=False)
    # bbox has no Literal choices -- untouched even though it's a string.
    out = fuzzy_correct_enum_args("stub_tool", {"bbox": "0,0,1,1"}, _enum_fn)
    assert out["bbox"] == "0,0,1,1"



_GEOCODED = [-82.6, 27.9, -82.3, 28.1]  # Tampa-ish


def test_drift_note_fires_on_disjoint_bbox():
    note = agent_server._geocode_drift_note(
        {"bbox": [10.0, 45.0, 11.0, 46.0]}, _GEOCODED, None
    )
    assert note is not None and "WARNING" in note


def test_drift_note_no_fire_on_intersecting_bbox():
    note = agent_server._geocode_drift_note(
        {"bbox": [-82.5, 27.95, -82.4, 28.05]}, _GEOCODED, None
    )
    assert note is None


def test_drift_note_no_fire_when_active_aoi_covers():
    # Disjoint from the geocode bbox but inside the user's drawn AOI -> OK.
    note = agent_server._geocode_drift_note(
        {"bbox": [10.0, 45.0, 11.0, 46.0]},
        _GEOCODED,
        [9.0, 44.0, 12.0, 47.0],
    )
    assert note is None


def test_drift_note_no_fire_without_bbox_arg():
    assert agent_server._geocode_drift_note({"query": "x"}, _GEOCODED, None) is None
    assert agent_server._geocode_drift_note(None, _GEOCODED, None) is None




@dataclass
class _FakeSocket:
    sent: list = field(default_factory=list)

    async def send(self, msg: str) -> None:
        try:
            self.sent.append(json.loads(msg))
        except (json.JSONDecodeError, TypeError):
            self.sent.append(msg)


def _make_fake_chunk_with_function_call(name: str, args: dict, call_id: str):
    return {"tool_call": {"name": name, "args": args, "call_id": call_id}}


def _make_fake_chunk_with_text(text: str):
    return {"text": text}


def _settings() -> ModelSettings:
    return ModelSettings(
        model="gemini-2.5-pro", project="t", location="us-central1", use_vertex=True
    )


async def _drive_geocode_then_fetch(fake_llm, fetch_bbox: list) -> list:
    """Round 1 geocodes, round 2 fetches with ``fetch_bbox``, round 3 narrates.

    Returns the contents list handed to the model so the fetch's
    function_response can be inspected for the drift warning.
    """
    fake_llm.script([
        _make_fake_chunk_with_function_call(
            "geocode_location", {"query": "Tampa"}, "c1"
        ),
        _make_fake_chunk_with_function_call(
            "fetch_dem", {"bbox": fetch_bbox}, "c2"
        ),
        _make_fake_chunk_with_text("Done."),
    ])

    async def _dispatch(_ws, _state, name, _args):
        if name == "geocode_location":
            return {"bbox": list(_GEOCODED), "name": "Tampa"}
        return {"status": "ok"}

    sock = _FakeSocket()
    state = agent_server.SessionState(session_id=new_ulid())
    with patch.object(
             agent_server, "_invoke_tool_via_emitter", side_effect=_dispatch
         ), \
         patch.object(agent_server, "build_tool_declarations", return_value=[]):
        await agent_server._stream_model_reply(
            sock, state, _settings(), "show elevation data for Tampa", "research"
        )
    return [call["contents"] for call in fake_llm.calls]


def _fetch_response_payload(captured_contents) -> dict:
    """The fetch_dem function_response dict from the final round's contents."""
    final = captured_contents[-1]
    for content in final:
        for part in content.parts:
            fr = part.response
            if fr is not None and fr.name == "fetch_dem":
                return dict(fr.result)
    raise AssertionError("fetch_dem function_response not found in contents")


@pytest.mark.asyncio
async def test_drift_warning_fires_in_loop(monkeypatch, fake_llm):
    monkeypatch.delenv("TRID3NT_GEOCODE_DRIFT_WARN", raising=False)
    contents = await _drive_geocode_then_fetch(fake_llm, [10.0, 45.0, 11.0, 46.0])
    resp = _fetch_response_payload(contents)
    assert "aoi_drift_warning" in resp, sorted(resp)
    assert "WARNING" in resp["aoi_drift_warning"]


@pytest.mark.asyncio
async def test_drift_warning_no_fire_when_bbox_matches(monkeypatch, fake_llm):
    monkeypatch.delenv("TRID3NT_GEOCODE_DRIFT_WARN", raising=False)
    contents = await _drive_geocode_then_fetch(fake_llm, [-82.5, 27.95, -82.4, 28.05])
    resp = _fetch_response_payload(contents)
    assert "aoi_drift_warning" not in resp, sorted(resp)


@pytest.mark.asyncio
async def test_drift_warning_kill_switch(monkeypatch, fake_llm):
    monkeypatch.setenv("TRID3NT_GEOCODE_DRIFT_WARN", "0")
    contents = await _drive_geocode_then_fetch(fake_llm, [10.0, 45.0, 11.0, 46.0])
    resp = _fetch_response_payload(contents)
    assert "aoi_drift_warning" not in resp, sorted(resp)



_FETCHES: list[dict] = []


@pytest.fixture()
def _stub_fetch_tool():
    """Shadow fetch_buildings (fetch-class, not confirm-gated, and a
    recognized F96 fetched KIND so the dedupe has something to key on)."""
    name = "fetch_buildings"
    original = agent_tools.TOOL_REGISTRY.get(name)
    _FETCHES.clear()
    reset_uri_registries_for_tests()

    def _fn(bbox=None, **_kw) -> LayerURI:
        _FETCHES.append({"bbox": bbox})
        # Raster-shaped so the emitter keeps the layer without attempting a
        # vector densify read.
        return LayerURI(
            layer_id=f"buildings-{len(_FETCHES)}",
            name="Building Footprints",
            layer_type="raster",
            uri=f"s3://x/buildings-{len(_FETCHES)}.tif",
            bbox=tuple(bbox),
        )

    meta = AtomicToolMetadata(name=name, ttl_class="live-no-cache", cacheable=False)
    agent_tools.TOOL_REGISTRY[name] = RegisteredTool(
        metadata=meta, fn=_fn, module=__name__
    )
    try:
        yield
    finally:
        if original is not None:
            agent_tools.TOOL_REGISTRY[name] = original
        else:
            agent_tools.TOOL_REGISTRY.pop(name, None)
        reset_uri_registries_for_tests()


_BUILDINGS_BBOX = [-81.0, 25.0, -80.0, 26.0]


@pytest.mark.asyncio
async def test_fetch_reuse_kill_switch_disables_short_circuit(
    _stub_fetch_tool, monkeypatch
):
    monkeypatch.setenv("TRID3NT_FETCH_REUSE", "0")
    # Same Case-AOI anchor as the fire test below -- the ONLY variable in
    # this pair is the kill-switch.
    monkeypatch.setattr(
        agent_server, "_turn_case_bbox", lambda state: list(_BUILDINGS_BBOX)
    )
    ws = _FakeSocket()
    state = agent_server.SessionState(session_id=new_ulid())
    await agent_server._invoke_tool_via_emitter(
        ws, state, "fetch_buildings", {"bbox": list(_BUILDINGS_BBOX)}
    )
    await agent_server._invoke_tool_via_emitter(
        ws, state, "fetch_buildings", {"bbox": list(_BUILDINGS_BBOX)}
    )
    assert len(_FETCHES) == 2, (
        "TRID3NT_FETCH_REUSE=0 must disable the refetch dedupe"
    )


@pytest.mark.asyncio
async def test_fetch_reuse_default_still_fires(_stub_fetch_tool, monkeypatch):
    """Sanity: with the switch unset the F96 dedupe short-circuits the refetch."""
    monkeypatch.delenv("TRID3NT_FETCH_REUSE", raising=False)
    # ProjectLayerSummary carries no per-layer bbox, so the F96 comparison
    # anchors on the Case AOI (same hermetic seam the F96 dispatch test uses).
    monkeypatch.setattr(
        agent_server, "_turn_case_bbox", lambda state: list(_BUILDINGS_BBOX)
    )
    ws = _FakeSocket()
    state = agent_server.SessionState(session_id=new_ulid())
    await agent_server._invoke_tool_via_emitter(
        ws, state, "fetch_buildings", {"bbox": list(_BUILDINGS_BBOX)}
    )
    await agent_server._invoke_tool_via_emitter(
        ws, state, "fetch_buildings", {"bbox": list(_BUILDINGS_BBOX)}
    )
    assert len(_FETCHES) == 1, "F96 refetch dedupe regressed"
