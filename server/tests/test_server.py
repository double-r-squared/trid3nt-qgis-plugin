"""Focused server.py dispatch-seam coverage.

NATE 2026-06-26: extends the F97 unique-layer-id mint guarantee to the
LIST-returning tool case. True-color / satellite tools
(``fetch_goes_animation``, ``fetch_goes_archive_animation``,
``fetch_goes_active_fire``, ``fetch_glm_lightning``, ``fetch_viirs_day_fire``)
return ``list[LayerURI]``. The original ``_restamp`` only re-stamped a SINGLE
``LayerURI`` (``isinstance(value, LayerURI)``), so list members kept
source-derived ids that can coincide; ``add_loaded_layer`` dedups by
COG-identity (TiTiler ``url=`` param), NOT by ``layer_id``, so two layers with
the same id both persist and collide on delete-by-id (deleting one tore down
BOTH). The fix re-stamps every ``LayerURI`` element while preserving the
sequence type so downstream ``isinstance(result, list)`` checks are unaffected.
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


_LIST_TOOL = "fetch_collision_prone_animation"
# Every frame returns the SAME source-derived id (the collision the fix
# targets) even though the frames point at genuinely different data.
_SOURCE_DERIVED_ID = "goes-2026-06-26"


@pytest.fixture(autouse=True)
def _stub_list_tool():
    """Register a fetcher that returns a LIST of LayerURIs sharing one id.

    The name is not a known scenario / solver tool, so neither the reuse
    short-circuit nor the confirm gate fires — we exercise the bare
    fresh-fetch mint path through ``_restamp`` for a list return.
    """
    original = agent_tools.TOOL_REGISTRY.get(_LIST_TOOL)
    reset_scenario_indexes_for_tests()
    reset_uri_registries_for_tests()

    def _fn(**_kw) -> list[LayerURI]:
        # Three frames, distinct uris (real distinct layers), colliding ids.
        return [
            LayerURI(
                layer_id=_SOURCE_DERIVED_ID,
                name=f"GOES true-color frame {i}",
                layer_type="raster",
                uri=f"s3://bucket/goes/frame-{i}.tif",
                style_preset="true_color",
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
        reset_scenario_indexes_for_tests()
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
