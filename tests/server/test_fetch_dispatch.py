"""``fetch(source, args)`` IS a call of the source: the dispatch translates it before anything keys on the name.

Offline over every spec-served source. The source's own fn is a stub that surfaces its layer through the same emission seam
route() uses, so what is compared is the dispatch: the arguments the source receives, the gates and the reuse guard (asked
by the source's name), the card steps, and exactly one layer on the map - not the fetch itself."""

from __future__ import annotations

import json

import pytest

from trid3nt_contracts.common import new_ulid
from trid3nt_contracts.execution import LayerURI

from trid3nt_server import server
from trid3nt_server.render.uri_registry import reset_uri_registries_for_tests
from trid3nt_server.tools.fetchers._router.registration import get_spec, registered_spec_names
from trid3nt_server.tools.fetchers._router.emit_on_fetch import maybe_emit_input_on_fetch


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


@pytest.fixture(scope="module", autouse=True)
def _registry():
    import trid3nt_server.main as _main

    _main._import_tools_registry()


@pytest.fixture
def world(monkeypatch):
    """The source stubbed to surface one layer through the emit-on-fetch seam, and every gate and guard recording the name it is asked about."""
    from trid3nt_server import tools as agent_tools
    from trid3nt_server.inputs.gate import confirm
    from trid3nt_server.server.dispatch import emitter

    reset_uri_registries_for_tests()
    asked: dict[str, list] = {"fn": [], "payload": [], "gate": [], "reuse": [], "seam": []}

    async def payload(ws, state, tool_name, params):
        asked["payload"].append((tool_name, dict(params)))
        return True, params

    async def gate(ws, state, tool_name, params):
        asked["gate"].append((tool_name, dict(params)))
        return True, params

    def reuse(state, tool_name, params):
        asked["reuse"].append((tool_name, dict(params)))
        return None

    monkeypatch.setattr(confirm, "_maybe_gate_on_payload_warning", payload)
    monkeypatch.setattr(confirm, "_gate_with_turn_memory", gate)
    monkeypatch.setattr(emitter, "_reusable_fetch_layer", reuse)
    from trid3nt_server.render import layer_uri_emit

    published = layer_uri_emit.publish_input_layer

    async def seam(emit, layer, **kw):
        asked["seam"].append((layer.name, layer.layer_id))
        return await published(emit, layer, **kw)

    monkeypatch.setattr(layer_uri_emit, "publish_input_layer", seam)
    saved: dict[str, object] = {}

    def stub(name):
        spec = get_spec(name)
        saved[name] = agent_tools.TOOL_REGISTRY[name]

        def _fn(**kwargs):
            asked["fn"].append((name, dict(kwargs)))
            layer = LayerURI(layer_id="src", name=name, layer_type="vector",
                             uri=f"s3://trid3nt-cache/{name}.fgb", role="context")
            maybe_emit_input_on_fetch(spec, kwargs, layer, visualize=None, purpose="p")
            return layer

        agent_tools.TOOL_REGISTRY[name] = agent_tools.RegisteredTool(
            metadata=saved[name].metadata, fn=_fn, module=saved[name].module)

    yield asked, stub
    for name, entry in saved.items():
        agent_tools.TOOL_REGISTRY[name] = entry
    reset_uri_registries_for_tests()


def _first_param(name: str) -> dict:
    return {next(iter(get_spec(name).params)): "x"}


async def _dispatch(call_name, params, session_id=None):
    ws = FakeWS()
    state = server.SessionState(session_id=session_id or new_ulid())
    result = await server._invoke_tool_via_emitter(ws, state, call_name, params)
    steps = sorted({step.get("tool_name") for frame in map(json.loads, ws.sent)
                    for step in (frame.get("payload", {}).get("steps") or [])})
    return result, steps, [row.layer_id for row in state.emitter.loaded_layers]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(registered_spec_names()))
async def test_a_fetch_is_the_direct_call(world, name):
    asked, stub = world
    stub(name)
    args = _first_param(name)

    direct, direct_steps, direct_layers = await _dispatch(name, dict(args))
    direct_asked = {k: list(v) for k, v in asked.items()}
    for v in asked.values():
        v.clear()
    via, via_steps, via_layers = await _dispatch("fetch", {"source": name, "args": dict(args)})

    assert asked == direct_asked
    assert all(call[0] == name for key in asked for call in asked[key]), "something keyed on the name fetch"
    assert via_steps == direct_steps and name in via_steps and "fetch" not in via_steps
    assert asked["seam"] == [], "the input-copy seam fired: the layer would be emitted twice"
    assert len(via_layers) == len(direct_layers) == 1
    assert len(via.layer_id) == len(direct.layer_id) == 26


@pytest.mark.asyncio
async def test_a_wrapper_that_does_not_translate_emits_the_layer_twice(world, monkeypatch):
    """Fail proof: the call dispatched under the name fetch, the source called from inside it, puts the layer on the map beside its own copy."""
    from trid3nt_server import tools as agent_tools
    from trid3nt_server.server.dispatch import emitter

    asked, stub = world
    name = "fetch_nhd_hr_waterbodies"
    stub(name)
    source_fn = agent_tools.TOOL_REGISTRY[name].fn
    monkeypatch.setattr(emitter, "resolve_fetch", lambda params, held=None: ("fetch", dict(params)))
    monkeypatch.setitem(
        agent_tools.TOOL_REGISTRY, "fetch",
        agent_tools.RegisteredTool(metadata=agent_tools.TOOL_REGISTRY["fetch"].metadata,
                                   fn=lambda **kw: source_fn(**kw["args"]), module=__name__))
    await _dispatch("fetch", {"source": name, "args": _first_param(name)})
    assert len(asked["seam"]) == 1


@pytest.mark.asyncio
async def test_a_source_the_registry_serves_but_a_template_is_not_fetchable():
    from trid3nt_server.tools.search.fetch import FetchSourceError

    with pytest.raises(FetchSourceError, match="no source named 'telemac_dye_release'"):
        await _dispatch("fetch", {"source": "telemac_dye_release", "args": {}})


@pytest.mark.asyncio
async def test_an_unknown_source_and_an_unknown_argument_are_refused_by_name_with_the_nearest():
    from trid3nt_server.tools.search.fetch import FetchArgError, FetchSourceError

    with pytest.raises(FetchSourceError, match="fetch_fema_nfhl_zones"):
        await _dispatch("fetch", {"source": "fetch_femaa_nfhl_zones", "args": {}})
    with pytest.raises(FetchArgError, match="'zones' is not a parameter of fetch_fema_nfhl_zones; did you mean 'zone'"):
        await _dispatch("fetch", {"source": "fetch_fema_nfhl_zones", "args": {"bbox": [0, 0, 1, 1], "zones": "A"}})
    with pytest.raises(FetchArgError, match="goes inside args"):
        await _dispatch("fetch", {"source": "fetch_fema_nfhl_zones", "bbox": [0, 0, 1, 1]})


def test_the_source_name_is_taken_without_its_prefix_and_args_may_arrive_as_text():
    from trid3nt_server.tools.search.fetch import resolve_fetch

    name, kwargs = resolve_fetch({"source": "fema_nfhl_zones", "args": '{"bbox": [0, 0, 1, 1]}'})
    assert (name, kwargs) == ("fetch_fema_nfhl_zones", {"bbox": [0, 0, 1, 1]})


@pytest.mark.asyncio
async def test_a_handle_find_sources_held_is_sent_as_the_row_showed(world, monkeypatch):
    """Two dispatches of one session: find_sources holds the row, fetch("S<n>") calls the source with that request.
    The world is the one source's own rows, so no other network's station listing is read."""
    from importlib import import_module

    asked, stub = world
    name = "fetch_nws_river_forecast"
    stub(name)
    monkeypatch.setattr(import_module("trid3nt_server.tools.search.find_sources.find_sources"),
                        "sources_with_coverage", lambda: [(name, row) for row in get_spec(name).coverage])
    session = new_ulid()
    found, _steps, _layers = await _dispatch(
        "find_sources", {"need": "water level series", "place": [-122.80, 45.45, -122.60, 45.65],
                         "window": ["2026-11-09T19:00:00Z", "2026-11-10T01:00:00Z"]}, session)
    row = next(r for r in found["results"] if r["source"] == name)
    await _dispatch("fetch", {"source": row["handle"]}, session)
    (called, kwargs), = asked["fn"]
    assert called == name and kwargs["bbox"] == row["box"] and kwargs["include_series"] is True
