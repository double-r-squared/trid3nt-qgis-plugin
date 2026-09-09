"""job-0326: server-side reuse guard short-circuits a redundant expensive re-run.

These exercise the REAL ``_invoke_tool_via_emitter`` dispatch path with a stub
tool registered under a guarded ``EXPENSIVE_SCENARIO_TOOLS`` name (NOT gated by
SOLVER_CONFIRM_TOOLS, so the test needs no confirm-card plumbing). The stub
counts solver launches and returns a live concentration layer; the guard reads
the call's params and the result layer_id, never the composer behind the name.

  * A repeat dispatch with identical args REUSES the existing layer — the stub
    solver is launched only ONCE — and the second call's function_response
    carries the "reused_existing / not re-run" signal.
  * A dispatch with CHANGED args (different spill location) RUNS again.
  * ``force_rerun=True`` bypasses the guard.

The first dispatch still emits the layer onto the map (session-state), and the
reuse short-circuit re-loads the same layer (dedup by uri keeps one entry).
"""

from __future__ import annotations

import json

import pytest

from trid3nt_server import server
from trid3nt_server import tools as agent_tools
from trid3nt_server.scenario_reuse import reset_scenario_indexes_for_tests
from trid3nt_server.tools import RegisteredTool
from trid3nt_server.emission.uri_registry import reset_uri_registries_for_tests
from trid3nt_contracts.common import new_ulid
from trid3nt_contracts.telemac_contracts import TelemacDyeLayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


# launch counter shared with the stub tool fixture
_LAUNCHES: list[dict] = []


def _make_plume(layer_id: str, lat: float, lon: float) -> TelemacDyeLayerURI:
    # The emit guardrail drops non-renderable rasters, so the layer carries a
    # readable uri.
    return TelemacDyeLayerURI(
        layer_id=layer_id,
        name="Contaminant Plume",
        layer_type="raster",
        uri=f"s3://runs/{layer_id}/peak_concentration.tif",
        bbox=(lon - 0.1, lat - 0.1, lon + 0.1, lat + 0.1),
        dye_cmax_mgl=12.5,
    )


@pytest.fixture(autouse=True)
def _stub_scenario_tool():
    """Shadow the guarded name with a launch-counting stub returning a plume layer."""
    name = "modflow_contaminant_plume"
    original = agent_tools.TOOL_REGISTRY.get(name)
    _LAUNCHES.clear()
    reset_scenario_indexes_for_tests()
    reset_uri_registries_for_tests()

    def _fn(spill_location_latlon=None, contaminant=None, **_kw) -> TelemacDyeLayerURI:
        _LAUNCHES.append(
            {"spill_location_latlon": spill_location_latlon, "contaminant": contaminant}
        )
        lat, lon = spill_location_latlon
        layer_id = f"plume-run-{len(_LAUNCHES)}"
        return _make_plume(layer_id, float(lat), float(lon))

    meta = AtomicToolMetadata(
        name=name, ttl_class="live-no-cache", cacheable=False
    )
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


_PARAMS = {
    "spill_location_latlon": [40.81, -96.71],
    "contaminant": "benzene",
    "release_rate_kg_s": 0.5,
    "duration_days": 30,
}


@pytest.mark.asyncio
async def test_repeat_expensive_run_reuses_without_relaunch() -> None:
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())

    # First run: the solver launches and the plume layer lands on the map.
    first = await server._invoke_tool_via_emitter(
        ws, state, "modflow_contaminant_plume", dict(_PARAMS)
    )
    assert isinstance(first, TelemacDyeLayerURI)
    assert len(_LAUNCHES) == 1
    first_layer_id = first.layer_id

    # Second IDENTICAL run: short-circuit — the solver does NOT launch again.
    second = await server._invoke_tool_via_emitter(
        ws, state, "modflow_contaminant_plume", dict(_PARAMS)
    )
    assert len(_LAUNCHES) == 1, "redundant expensive solver re-ran (guard failed)"

    # The short-circuit returns the reuse-marker dict pointing at the EXISTING
    # layer, with an explicit "not re-run" note for the model.
    assert isinstance(second, dict)
    assert second.get("reused") is True
    assert second.get("status") == "reused_existing"
    assert second.get("layer_id") == first_layer_id
    assert "not re-run" in second.get("note", "").lower()

    # The map still shows exactly one plume layer (dedup by uri).
    plume_layers = [
        l for l in state.emitter.loaded_layers if l.layer_id == first_layer_id
    ]
    assert len(plume_layers) == 1


@pytest.mark.asyncio
async def test_changed_args_run_again() -> None:
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())

    await server._invoke_tool_via_emitter(
        ws, state, "modflow_contaminant_plume", dict(_PARAMS)
    )
    assert len(_LAUNCHES) == 1

    # A DIFFERENT spill location is a genuinely different request -> RUN.
    changed = dict(_PARAMS)
    changed["spill_location_latlon"] = [41.5, -97.6]
    result = await server._invoke_tool_via_emitter(
        ws, state, "modflow_contaminant_plume", changed
    )
    assert len(_LAUNCHES) == 2, "changed request was wrongly short-circuited"
    assert isinstance(result, TelemacDyeLayerURI)


@pytest.mark.asyncio
async def test_force_rerun_bypasses_guard() -> None:
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())

    await server._invoke_tool_via_emitter(
        ws, state, "modflow_contaminant_plume", dict(_PARAMS)
    )
    assert len(_LAUNCHES) == 1

    forced = dict(_PARAMS)
    forced["force_rerun"] = True
    result = await server._invoke_tool_via_emitter(
        ws, state, "modflow_contaminant_plume", forced
    )
    assert len(_LAUNCHES) == 2, "force_rerun did not bypass the reuse guard"
    assert isinstance(result, TelemacDyeLayerURI)


@pytest.mark.asyncio
async def test_first_run_emits_session_state_with_layer() -> None:
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    first = await server._invoke_tool_via_emitter(
        ws, state, "modflow_contaminant_plume", dict(_PARAMS)
    )
    session_states = [
        e
        for e in (json.loads(s) for s in ws.sent)
        if e.get("type") == "session-state"
    ]
    assert session_states
    layer_ids = [
        l.get("layer_id")
        for l in session_states[-1].get("payload", {}).get("loaded_layers", [])
    ]
    assert first.layer_id in layer_ids
