"""Focused dispatch-seam coverage: the unique-layer-id mint over a LIST return.

A tool returning a list of layers had only its single-layer case re-stamped, so
list members kept source-derived ids that can coincide - and since the
accumulator dedups by COG identity rather than by id, two layers shared an id and
a delete by id tore down both. Every element is re-stamped, sequence type intact."""

from __future__ import annotations

import pytest

from trid3nt_server import server
from trid3nt_server import tools as agent_tools
from trid3nt_server.tools import RegisteredTool
from trid3nt_server.render.uri_registry import reset_uri_registries_for_tests
from trid3nt_contracts.common import new_ulid
from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


_LIST_TOOL = "fetch_collision_prone_animation"
# Every frame returns the SAME source-derived id (the collision the fix
# targets) even though the frames point at genuinely different data.
_SOURCE_DERIVED_ID = "goes-2026-06-26"


@pytest.fixture(autouse=True)
def _stub_list_tool():
    """Register a fetcher returning a LIST of layers that share one id.

    The name is neither a scenario nor a solver tool, so neither the reuse
    short-circuit nor the confirm gate fires and the bare mint path runs."""
    original = agent_tools.TOOL_REGISTRY.get(_LIST_TOOL)
    reset_uri_registries_for_tests()

    def _fn(**_kw) -> list[LayerURI]:
        # Three frames, distinct uris (real distinct layers), colliding ids.
        return [
            LayerURI(
                layer_id=_SOURCE_DERIVED_ID,
                name=f"GOES true-color frame {i}",
                layer_type="raster",
                uri=f"s3://bucket/goes/frame-{i}.tif",
                role="primary",
            )
            for i in range(3)
        ]

    meta = AtomicToolMetadata(
        name=_LIST_TOOL, ttl_class="live-no-cache", cacheable=False
    )
    agent_tools.TOOL_REGISTRY[_LIST_TOOL] = RegisteredTool(
        metadata=meta, fn=_fn, module=__name__
    )
    try:
        yield
    finally:
        if original is not None:
            agent_tools.TOOL_REGISTRY[_LIST_TOOL] = original
        else:
            agent_tools.TOOL_REGISTRY.pop(_LIST_TOOL, None)
        reset_uri_registries_for_tests()


def _is_ulid(value: str) -> bool:
    return isinstance(value, str) and len(value) == 26


@pytest.mark.asyncio
async def test_list_returning_tool_restamps_every_member() -> None:
    """A list-returning tool yields all-distinct, freshly-minted layer_ids and
    preserves the list type."""
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())

    result = await server._invoke_tool_via_emitter(ws, state, _LIST_TOOL, {})

    # The sequence type is preserved (list stays list) so downstream
    # isinstance(result, list) auto-publish / uri_registry logic is unaffected.
    assert isinstance(result, list)
    assert len(result) == 3
    ids = [layer.layer_id for layer in result]
    # Every member is a freshly-minted ULID ...
    for lid in ids:
        assert _is_ulid(lid), f"member id {lid!r} is not a minted ULID"
        assert lid != _SOURCE_DERIVED_ID
    # ... and they are all DISTINCT (the core guarantee — no two frames share
    # an id that would collapse onto one source and tear down on delete).
    assert len(set(ids)) == len(ids), "list members collided on layer_id"
