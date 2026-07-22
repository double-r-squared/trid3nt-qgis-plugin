"""Stage 3 (ADR 0017 mechanism 3) -- dispatch-guard hardening.

Covered here (each guard: fire + no-fire + kill-switch):

  (c) FUZZY ENUM-ARG CORRECTION at the normalizer -- a string arg failing a
      ``Literal`` schema is difflib-corrected (cutoff 0.8) with a log line;
      below the cutoff the normal typed-error path owns it.
      Kill-switch: ``TRID3NT_ENUM_FUZZY=0``.

  (d) GEOCODE DRIFT WARNING -- after geocode_location, a later call whose bbox
      intersects neither the geocoded bbox nor the active AOI gets an advisory
      WARNING appended to its function_response (never blocks).
      Kill-switch: ``TRID3NT_GEOCODE_DRIFT_WARN=0``.

  (a) REFETCH DEDUPE kill-switch (``TRID3NT_FETCH_REUSE=0``) -- the F96
      short-circuit itself is covered by test_fetch_reuse_dispatch_f96 /
      test_scenario_reuse_fetch_f96; here we prove the switch disables it.

  (b) EXPENSIVE-SIM RESULT REUSE kill-switch (``TRID3NT_SCENARIO_REUSE=0``) --
      the job-0326 short-circuit + force_rerun escape are covered by
      test_scenario_reuse_dispatch_job0326; here we prove the switch.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal
from unittest.mock import MagicMock, patch

import pytest

from trid3nt_server import server as agent_server
from trid3nt_server import tools as agent_tools
from trid3nt_server.adapter import GeminiSettings
from trid3nt_server.scenario_reuse import reset_scenario_indexes_for_tests
from trid3nt_server.tool_arg_normalizer import (
    fuzzy_correct_enum_args,
    normalize_args,
)
from trid3nt_server.tools import RegisteredTool
from trid3nt_server.uri_registry import reset_uri_registries_for_tests
from trid3nt_contracts import new_ulid
from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata


# ---------------------------------------------------------------------------
# (c) fuzzy enum-arg correction
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# (d) geocode drift warning -- pure helper
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# (d) geocode drift warning -- through the live dispatch loop
# ---------------------------------------------------------------------------


@dataclass
class _FakeSocket:
    sent: list = field(default_factory=list)

    async def send(self, msg: str) -> None:
        try:
            self.sent.append(json.loads(msg))
        except (json.JSONDecodeError, TypeError):
            self.sent.append(msg)


def _make_fake_chunk_with_function_call(name: str, args: dict, call_id: str):
    fn_call = MagicMock()
    fn_call.name = name
    fn_call.id = call_id
    fn_call.args = args
    fake_part = MagicMock()
    fake_part.function_call = fn_call
    fake_part.text = None
    fake_content = MagicMock()
    fake_content.parts = [fake_part]
    fake_candidate = MagicMock()
    fake_candidate.content = fake_content
    fake_chunk = MagicMock()
    fake_chunk.candidates = [fake_candidate]
    fake_chunk.text = None
    return fake_chunk


def _make_fake_chunk_with_text(text: str):
    fake_part = MagicMock()
    fake_part.function_call = None
    fake_part.text = text
    fake_content = MagicMock()
    fake_content.parts = [fake_part]
    fake_candidate = MagicMock()
    fake_candidate.content = fake_content
    fake_chunk = MagicMock()
    fake_chunk.candidates = [fake_candidate]
    fake_chunk.text = text
    return fake_chunk


def _settings() -> GeminiSettings:
    return GeminiSettings(
        model="gemini-2.5-pro", project="t", location="us-central1", use_vertex=True
    )


async def _drive_geocode_then_fetch(fetch_bbox: list) -> list:
    """Round 1 geocodes, round 2 fetches with ``fetch_bbox``, round 3 narrates.

    Returns the contents list handed to the model so the fetch's
    function_response can be inspected for the drift warning.
    """
    rounds = {"n": 0}
    captured_contents: list = []

    def _script(**kwargs):
        rounds["n"] += 1
        captured_contents.append(kwargs.get("contents"))
        if rounds["n"] == 1:
            return iter(
                [
                    _make_fake_chunk_with_function_call(
                        "geocode_location", {"query": "Tampa"}, "c1"
                    )
                ]
            )
        if rounds["n"] == 2:
            return iter(
                [
                    _make_fake_chunk_with_function_call(
                        "fetch_dem", {"bbox": fetch_bbox}, "c2"
                    )
                ]
            )
        return iter([_make_fake_chunk_with_text("Done.")])

    async def _dispatch(_ws, _state, name, _args):
        if name == "geocode_location":
            return {"bbox": list(_GEOCODED), "name": "Tampa"}
        return {"status": "ok"}

    sock = _FakeSocket()
    state = agent_server.SessionState(session_id=new_ulid())
    with patch.object(agent_server, "build_client", return_value=MagicMock()), \
         patch.object(
             agent_server, "_invoke_tool_via_emitter", side_effect=_dispatch
         ), \
         patch.object(agent_server, "build_tool_declarations", return_value=[]):
        agent_server.build_client.return_value.models.generate_content_stream.side_effect = (
            lambda **kw: _script(**kw)
        )
        await agent_server._stream_gemini_reply(
            sock, state, _settings(), "show elevation data for Tampa", "research"
        )
    return captured_contents


def _fetch_response_payload(captured_contents) -> dict:
    """The fetch_dem function_response dict from the final round's contents."""
    final = captured_contents[-1]
    for content in final:
        for part in getattr(content, "parts", None) or []:
            fr = getattr(part, "function_response", None)
            if fr is not None and getattr(fr, "name", None) == "fetch_dem":
                return dict(fr.response)
    raise AssertionError("fetch_dem function_response not found in contents")


@pytest.mark.asyncio
async def test_drift_warning_fires_in_loop(monkeypatch):
    monkeypatch.delenv("TRID3NT_GEOCODE_DRIFT_WARN", raising=False)
    contents = await _drive_geocode_then_fetch([10.0, 45.0, 11.0, 46.0])
    resp = _fetch_response_payload(contents)
    assert "aoi_drift_warning" in resp, sorted(resp)
    assert "WARNING" in resp["aoi_drift_warning"]


@pytest.mark.asyncio
async def test_drift_warning_no_fire_when_bbox_matches(monkeypatch):
    monkeypatch.delenv("TRID3NT_GEOCODE_DRIFT_WARN", raising=False)
    contents = await _drive_geocode_then_fetch([-82.5, 27.95, -82.4, 28.05])
    resp = _fetch_response_payload(contents)
    assert "aoi_drift_warning" not in resp, sorted(resp)


@pytest.mark.asyncio
async def test_drift_warning_kill_switch(monkeypatch):
    monkeypatch.setenv("TRID3NT_GEOCODE_DRIFT_WARN", "0")
    contents = await _drive_geocode_then_fetch([10.0, 45.0, 11.0, 46.0])
    resp = _fetch_response_payload(contents)
    assert "aoi_drift_warning" not in resp, sorted(resp)


# ---------------------------------------------------------------------------
# (b) expensive-sim reuse kill-switch (guard itself: job-0326 tests)
# ---------------------------------------------------------------------------

_SIM_LAUNCHES: list[dict] = []


@pytest.fixture()
def _stub_expensive_tool():
    """Shadow run_modflow_job with a launch-counting stub (job-0326 pattern)."""
    name = "run_modflow_job"
    original = agent_tools.TOOL_REGISTRY.get(name)
    _SIM_LAUNCHES.clear()
    reset_scenario_indexes_for_tests()
    reset_uri_registries_for_tests()

    def _fn(spill_location_latlon=None, contaminant=None, **_kw) -> LayerURI:
        _SIM_LAUNCHES.append({"loc": spill_location_latlon})
        lat, lon = spill_location_latlon
        return LayerURI(
            layer_id=f"plume-run-{len(_SIM_LAUNCHES)}",
            name="Contaminant Plume",
            layer_type="raster",
            uri=f"https://example.test/wms?LAYERS=plume-{len(_SIM_LAUNCHES)}",
            style_preset="plume",
            bbox=(lon - 0.1, lat - 0.1, lon + 0.1, lat + 0.1),
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
        reset_scenario_indexes_for_tests()
        reset_uri_registries_for_tests()


_SIM_PARAMS = {
    "spill_location_latlon": [40.81, -96.71],
    "contaminant": "benzene",
}


@pytest.mark.asyncio
async def test_scenario_reuse_kill_switch_disables_short_circuit(
    _stub_expensive_tool, monkeypatch
):
    monkeypatch.setenv("TRID3NT_SCENARIO_REUSE", "0")
    ws = _FakeSocket()
    state = agent_server.SessionState(session_id=new_ulid())
    await agent_server._invoke_tool_via_emitter(
        ws, state, "run_modflow_job", dict(_SIM_PARAMS)
    )
    await agent_server._invoke_tool_via_emitter(
        ws, state, "run_modflow_job", dict(_SIM_PARAMS)
    )
    assert len(_SIM_LAUNCHES) == 2, (
        "TRID3NT_SCENARIO_REUSE=0 must disable the reuse short-circuit"
    )


@pytest.mark.asyncio
async def test_scenario_reuse_default_still_fires(
    _stub_expensive_tool, monkeypatch
):
    """Sanity: with the switch unset the job-0326 short-circuit still works."""
    monkeypatch.delenv("TRID3NT_SCENARIO_REUSE", raising=False)
    ws = _FakeSocket()
    state = agent_server.SessionState(session_id=new_ulid())
    await agent_server._invoke_tool_via_emitter(
        ws, state, "run_modflow_job", dict(_SIM_PARAMS)
    )
    await agent_server._invoke_tool_via_emitter(
        ws, state, "run_modflow_job", dict(_SIM_PARAMS)
    )
    assert len(_SIM_LAUNCHES) == 1


# ---------------------------------------------------------------------------
# (a) refetch-dedupe kill-switch (guard itself: F96 tests)
# ---------------------------------------------------------------------------

_FETCHES: list[dict] = []


@pytest.fixture()
def _stub_fetch_tool():
    """Shadow fetch_wdpa_protected_areas (fetch-class, not confirm-gated)."""
    name = "fetch_wdpa_protected_areas"
    original = agent_tools.TOOL_REGISTRY.get(name)
    _FETCHES.clear()
    reset_scenario_indexes_for_tests()
    reset_uri_registries_for_tests()

    def _fn(bbox=None, **_kw) -> LayerURI:
        _FETCHES.append({"bbox": bbox})
        # Raster-shaped display URI (the F96 fixture pattern) so the emitter
        # keeps the layer without attempting a vector densify read.
        return LayerURI(
            layer_id=f"wdpa-{len(_FETCHES)}",
            name="WDPA Protected Areas",
            layer_type="raster",
            uri=(
                "https://titiler.example/cog/tiles/WebMercatorQuad/"
                f"{{z}}/{{x}}/{{y}}.png?url=s3://x/wdpa-{len(_FETCHES)}.tif"
            ),
            style_preset="",
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
        reset_scenario_indexes_for_tests()
        reset_uri_registries_for_tests()


_WDPA_BBOX = [-81.0, 25.0, -80.0, 26.0]


@pytest.mark.asyncio
async def test_fetch_reuse_kill_switch_disables_short_circuit(
    _stub_fetch_tool, monkeypatch
):
    monkeypatch.setenv("TRID3NT_FETCH_REUSE", "0")
    # Same Case-AOI anchor as the fire test below -- the ONLY variable in
    # this pair is the kill-switch.
    monkeypatch.setattr(
        agent_server, "_turn_case_bbox", lambda state: list(_WDPA_BBOX)
    )
    ws = _FakeSocket()
    state = agent_server.SessionState(session_id=new_ulid())
    await agent_server._invoke_tool_via_emitter(
        ws, state, "fetch_wdpa_protected_areas", {"bbox": list(_WDPA_BBOX)}
    )
    await agent_server._invoke_tool_via_emitter(
        ws, state, "fetch_wdpa_protected_areas", {"bbox": list(_WDPA_BBOX)}
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
        agent_server, "_turn_case_bbox", lambda state: list(_WDPA_BBOX)
    )
    ws = _FakeSocket()
    state = agent_server.SessionState(session_id=new_ulid())
    await agent_server._invoke_tool_via_emitter(
        ws, state, "fetch_wdpa_protected_areas", {"bbox": list(_WDPA_BBOX)}
    )
    await agent_server._invoke_tool_via_emitter(
        ws, state, "fetch_wdpa_protected_areas", {"bbox": list(_WDPA_BBOX)}
    )
    assert len(_FETCHES) == 1, "F96 refetch dedupe regressed"
