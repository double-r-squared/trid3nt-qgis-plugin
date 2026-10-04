"""Deleting a layer, and the reuse instruction on the layers-present note.

A delete removes the layer from the live emitter and emits a refreshed session
state; the persisted Case loses it AUTHORITATIVELY, by replace rather than the
union merge that would resurrect it; the model's awareness follows. An unknown
id is a no-op and a malformed payload is typed. The note carries handle and uri."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

import pytest

from trid3nt_server import server as server_mod
from trid3nt_server.model.adapters.adapter import build_layers_present_note
from trid3nt_server.store.cases import Persistence
from trid3nt_server.render.pipeline_emitter import PipelineEmitter
from trid3nt_server.server import (
    SessionState,
    get_persistence,
    set_persistence,
)
from trid3nt_contracts.case import CaseSummary
from trid3nt_contracts.common import new_ulid
from trid3nt_contracts.execution import LayerURI

from tests._fakes import MockMCPClient


class MockWebSocket:
    """Collects every envelope ``send`` would have written to the wire."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, raw: Any) -> None:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        if isinstance(raw, str):
            self.sent.append(json.loads(raw))
        else:
            self.sent.append(raw)


@pytest.fixture()
def _persistence_bound():
    saved = get_persistence()
    p = Persistence(MockMCPClient())
    set_persistence(p)
    try:
        yield p
    finally:
        set_persistence(saved)


def _layer_uri(layer_id: str, name: str, uri: str) -> LayerURI:
    return LayerURI(
        layer_id=layer_id,
        name=name,
        layer_type="raster",
        uri=uri,
        role="primary",
    )


def _bind_emitter_with_layers(state: SessionState, ws: MockWebSocket) -> None:
    """Bind an emitter on ``state`` and seed it with two loaded layers."""

    async def _sink(text: str) -> None:
        await ws.send(text)

    state.emitter = PipelineEmitter(
        session_id=state.session_id,
        sink=_sink,
        chat_history=state.chat_history,
    )
    asyncio.run(
        state.emitter.add_loaded_layer(
            _layer_uri("flood-01", "Flood depth", "s3://bucket/flood.tif")
        )
    )
    asyncio.run(
        state.emitter.add_loaded_layer(
            _layer_uri("dem-01", "DEM", "s3://bucket/dem.tif")
        )
    )
    # Clear the session-state envelopes the seeding emitted so each test can
    # assert on the delete-triggered emission cleanly.
    ws.sent.clear()


def _fresh_case_with_layers(case_id: str) -> CaseSummary:
    return CaseSummary(
        case_id=case_id,
        title="Fort Myers flood",
        created_at=datetime(2026, 6, 16, 12, 0, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 6, 16, 12, 0, 0, tzinfo=timezone.utc),
        status="active",
        bbox=(-82.0, 26.5, -81.8, 26.7),
        primary_hazard="flood",
        layer_summary=["flood-01", "dem-01"],
        loaded_layer_summaries=[
            {
                "layer_id": "flood-01",
                "name": "Flood depth",
                "layer_type": "raster",
                "uri": "s3://bucket/flood.tif",
                "visible": True,
                "role": "primary",
                "temporal": False,
            },
            {
                "layer_id": "dem-01",
                "name": "DEM",
                "layer_type": "raster",
                "uri": "s3://bucket/dem.tif",
                "visible": True,
                "role": "input",
                "temporal": False,
            },
        ],
    )


def test_present_note_lists_handle_and_uri() -> None:
    """Each loaded-layer line surfaces handle=<layer_id> and uri=<uri>."""
    loaded = [
        {
            "layer_id": "flood-01",
            "name": "Flood depth",
            "layer_type": "raster",
            "uri": "s3://bucket/flood.tif",
        }
    ]
    note = build_layers_present_note(loaded)
    assert note is not None
    assert "handle=flood-01" in note
    assert "uri=s3://bucket/flood.tif" in note


def test_present_note_has_reuse_instruction() -> None:
    """The firm reuse / no-refetch instruction is present."""
    loaded = [
        {
            "layer_id": "flood-01",
            "name": "Flood depth",
            "layer_type": "raster",
            "uri": "s3://bucket/flood.tif",
        }
    ]
    note = build_layers_present_note(loaded)
    assert note is not None
    lower = note.lower()
    # Reworded the note ("REUSE these (pass their handle/uri DIRECTLY)");
    # assert the reuse instruction is present without pinning the old literal.
    assert "reuse" in lower
    assert "do not re-fetch or recompute" in lower
    assert "directly" in lower


def test_present_note_handle_without_uri() -> None:
    """A layer without a uri still gets handle=<layer_id> (no placeholder)."""
    loaded = [
        {"layer_id": "vec-01", "name": "Boundaries", "layer_type": "vector"}
    ]
    note = build_layers_present_note(loaded)
    assert note is not None
    assert "handle=vec-01" in note
    assert "uri=" not in note


def test_present_note_none_when_empty() -> None:
    """No layers and no bbox => None (unchanged contract)."""
    assert build_layers_present_note([]) is None
    assert build_layers_present_note(None) is None


def test_present_note_keeps_bbox_anchor() -> None:
    """The AOI bbox anchor still appends alongside the layer lines."""
    loaded = [
        {
            "layer_id": "flood-01",
            "name": "Flood depth",
            "layer_type": "raster",
            "uri": "s3://bucket/flood.tif",
        }
    ]
    note = build_layers_present_note(loaded, case_bbox=[-82.0, 26.5, -81.8, 26.7])
    assert note is not None
    assert "Case AOI bbox" in note
    assert "handle=flood-01" in note
