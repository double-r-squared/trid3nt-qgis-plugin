"""job-0366 (F96): server-side reuse guard short-circuits a redundant RE-FETCH.

The Wave B F96 track shipped the pure helper (find_reusable_fetched_layer) + the
prompt steer, but the DETERMINISTIC backstop that actually prevents the duplicate
— wiring the helper into ``_invoke_tool_via_emitter`` — fell between the two
tracks. job-0366 wires it (mirroring the run_model_* reuse guard, job-0326).

The real headline case (NATE 2026-06-17): a bare "resize the bbox to encompass
all the protected areas" follow-up carries NO bbox of its own — it targets the
layer already on the map, so the requested AOI resolves to the Case AOI. A loaded
same-kind layer then answers it -> short-circuit instead of re-fetching a
duplicate. ``ProjectLayerSummary`` does not (yet) carry a per-layer bbox, so the
match runs through the Case-AOI-equivalence path of find_reusable_fetched_layer;
``_turn_case_bbox`` is monkeypatched here to supply that Case AOI hermetically.

  * a repeat fetch at the Case AOI (bare follow-up, or explicit == Case AOI)
    REUSES the loaded layer — the fetcher runs only ONCE — and the response
    carries the "reused_existing / not re-fetched" signal;
  * a fetch at a genuinely different/larger AOI re-fetches;
  * ``force_refetch=True`` bypasses the guard.
"""

from __future__ import annotations

import pytest

from trid3nt_server import server
from trid3nt_server import tools as agent_tools
from trid3nt_server.scenario_reuse import reset_scenario_indexes_for_tests
from trid3nt_server.tools import RegisteredTool
from trid3nt_server.uri_registry import reset_uri_registries_for_tests
from trid3nt_contracts.common import new_ulid
from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


_FETCHES: list[dict] = []
_CASE_AOI = (-96.8, 40.7, -96.6, 40.9)


@pytest.fixture(autouse=True)
def _stub_fetch_dem(monkeypatch):
    name = "fetch_dem"
    original = agent_tools.TOOL_REGISTRY.get(name)
    _FETCHES.clear()
    reset_scenario_indexes_for_tests()
    reset_uri_registries_for_tests()
    # Supply a Case AOI hermetically (avoids persistence/active_case plumbing).
    monkeypatch.setattr(server, "_turn_case_bbox", lambda state: _CASE_AOI)

    def _fn(bbox=None, **_kw) -> LayerURI:
        _FETCHES.append({"bbox": bbox})
        bb = tuple(bbox) if bbox else _CASE_AOI
        return LayerURI(
            layer_id=f"dem-{len(_FETCHES)}",
            name="Elevation (DEM)",  # carries the 'dem' kind marker via name
            layer_type="raster",
            uri="https://titiler.example/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png?url=s3://x/dem.tif",
            style_preset="dem",
            bbox=bb,  # type: ignore[arg-type]
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


def _assert_reused(res) -> None:
    assert isinstance(res, dict), f"expected reuse dict, got {type(res)}"
    assert res.get("reused") is True
    assert res.get("status") == "reused_existing"
    assert "not re-fetch" in res.get("note", "").lower()


@pytest.mark.asyncio
async def test_bare_followup_reuses_loaded_layer() -> None:
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())

    first = await server._invoke_tool_via_emitter(
        ws, state, "fetch_dem", {"bbox": list(_CASE_AOI)}
    )
    assert isinstance(first, LayerURI)
    assert len(_FETCHES) == 1

    # Bare follow-up: no bbox -> requested AOI resolves to the Case AOI -> a
    # same-kind loaded layer answers it -> short-circuit, no re-fetch.
    second = await server._invoke_tool_via_emitter(ws, state, "fetch_dem", {})
    assert len(_FETCHES) == 1, "bare follow-up wrongly re-fetched (F96 backstop failed)"
    _assert_reused(second)


@pytest.mark.asyncio
async def test_explicit_case_aoi_refetch_reuses() -> None:
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    await server._invoke_tool_via_emitter(ws, state, "fetch_dem", {"bbox": list(_CASE_AOI)})
    assert len(_FETCHES) == 1

    again = await server._invoke_tool_via_emitter(
        ws, state, "fetch_dem", {"bbox": list(_CASE_AOI)}
    )
    assert len(_FETCHES) == 1, "explicit same-AOI re-fetch wrongly ran"
    _assert_reused(again)


@pytest.mark.asyncio
async def test_different_aoi_refetches() -> None:
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    await server._invoke_tool_via_emitter(ws, state, "fetch_dem", {"bbox": list(_CASE_AOI)})
    assert len(_FETCHES) == 1

    larger = {"bbox": [-97.5, 40.0, -96.0, 41.5]}
    res = await server._invoke_tool_via_emitter(ws, state, "fetch_dem", larger)
    assert len(_FETCHES) == 2, "different AOI was wrongly short-circuited"
    assert isinstance(res, LayerURI)


@pytest.mark.asyncio
async def test_force_refetch_bypasses_guard() -> None:
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    await server._invoke_tool_via_emitter(ws, state, "fetch_dem", {"bbox": list(_CASE_AOI)})
    assert len(_FETCHES) == 1

    forced = {"bbox": list(_CASE_AOI), "force_refetch": True}
    res = await server._invoke_tool_via_emitter(ws, state, "fetch_dem", forced)
    assert len(_FETCHES) == 2, "force_refetch did not bypass the reuse guard"
    assert isinstance(res, LayerURI)
