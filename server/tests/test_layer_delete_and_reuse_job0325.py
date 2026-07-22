"""job-0325 — F53 (delete layers) + F54 (reuse-not-refetch) coverage.

F53 (server.py ``_handle_layer_delete`` + ``_delete_case_loaded_layer``):
- delete removes the layer from the live emitter's ``loaded_layers`` and emits
  a refreshed ``session-state`` (Map.tsx replace-not-reconcile then drops the
  overlay — no Map.tsx change);
- the persisted ``CaseSummary`` loses the layer AUTHORITATIVELY (replace, not
  the union merge of ``_persist_case_loaded_layers`` which would resurrect it);
- the agent's loaded-layers awareness reflects the delete (the emitter snapshot
  no longer carries the layer, so ``build_layers_present_note`` stops listing
  it);
- a non-existent layer_id is a harmless no-op; a malformed payload surfaces a
  typed ``TOOL_PARAMS_INVALID``.

F54 (adapter.py ``build_layers_present_note``):
- the per-layer line now carries ``handle=`` (== the layer_id) and ``uri=``
  when present so the model can pass the artifact straight to a tool;
- the firm reuse / no-refetch instruction is appended.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

import pytest

from trid3nt_server import server as server_mod
from trid3nt_server.adapter import build_layers_present_note
from trid3nt_server.persistence import Persistence
from trid3nt_server.pipeline_emitter import PipelineEmitter
from trid3nt_server.server import (
    SessionState,
    _delete_case_loaded_layer,
    _handle_layer_delete,
    get_persistence,
    set_persistence,
)
from trid3nt_contracts.case import CaseSummary
from trid3nt_contracts.common import new_ulid
from trid3nt_contracts.execution import LayerURI

from .test_persistence import MockMCPClient


# --------------------------------------------------------------------------- #
# Mocks / fixtures
# --------------------------------------------------------------------------- #


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
        style_preset="flood_depth",
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
                "style_preset": "flood_depth",
                "visible": True,
                "role": "primary",
                "temporal": False,
            },
            {
                "layer_id": "dem-01",
                "name": "DEM",
                "layer_type": "raster",
                "uri": "s3://bucket/dem.tif",
                "style_preset": "dem",
                "visible": True,
                "role": "input",
                "temporal": False,
            },
        ],
    )


# --------------------------------------------------------------------------- #
# F53 — server-side delete
# --------------------------------------------------------------------------- #


def test_layer_delete_removes_from_emitter_and_emits_session_state(
    _persistence_bound: Persistence,
) -> None:
    """A ``layer-delete`` drops the layer from the live emitter and emits a
    fresh ``session-state`` WITHOUT that layer (Map.tsx then removes it)."""
    case_id = new_ulid()
    asyncio.run(_persistence_bound.upsert_case(_fresh_case_with_layers(case_id)))

    ws = MockWebSocket()
    state = SessionState(session_id=new_ulid())
    _bind_emitter_with_layers(state, ws)
    state.active_case_id = case_id

    asyncio.run(_handle_layer_delete(ws, state, {"layer_id": "flood-01"}))

    # Emitter accumulator no longer carries the deleted layer.
    remaining = {layer.layer_id for layer in state.emitter.loaded_layers}
    assert remaining == {"dem-01"}

    # A session-state was emitted, and its loaded_layers omits flood-01.
    sess = [e for e in ws.sent if e["type"] == "session-state"]
    assert sess, "expected a session-state emission after delete"
    final_ids = [
        layer["layer_id"] for layer in sess[-1]["payload"]["loaded_layers"]
    ]
    assert "flood-01" not in final_ids
    assert "dem-01" in final_ids


def test_layer_delete_reinlines_surviving_vectors_before_emit(
    _persistence_bound: Persistence,
) -> None:
    """NATE 2026-06-26: a delete must NOT transiently drop sibling vector
    layers. Seed two vector layers, only ONE pre-inlined; the delete path
    re-inlines the missing one BEFORE emit so EVERY surviving vector layer in
    the emitted session-state carries ``inline_geojson`` (the client never
    fetches s3:// directly — job-0175)."""
    case_id = new_ulid()
    asyncio.run(_persistence_bound.upsert_case(_fresh_case_with_layers(case_id)))

    ws = MockWebSocket()
    state = SessionState(session_id=new_ulid())

    async def _sink(text: str) -> None:
        await ws.send(text)

    state.emitter = PipelineEmitter(
        session_id=state.session_id,
        sink=_sink,
        chat_history=state.chat_history,
    )
    # Three vector layers loaded directly into the accumulator (the persisted
    # ProjectLayerSummary dict shape, like _fresh_case_with_layers). We bypass
    # add_loaded_layer's inline read so we can control which payloads are
    # present on THIS socket: vec-keep-A is pre-inlined, vec-keep-B is NOT
    # (its payload is "missing" on this socket), vec-del will be deleted.
    def _vec_summary(layer_id: str, name: str, uri: str) -> dict:
        return {
            "layer_id": layer_id,
            "name": name,
            "layer_type": "vector",
            "uri": uri,
            "style_preset": "vector_outline",
            "visible": True,
            "role": "input",
            "temporal": False,
        }

    state.emitter.reset_loaded_layers(
        [
            _vec_summary("vec-keep-A", "Boundaries A", "s3://bucket/a.geojson"),
            _vec_summary("vec-keep-B", "Boundaries B", "s3://bucket/b.geojson"),
            _vec_summary("vec-del", "Boundaries Del", "s3://bucket/del.geojson"),
        ]
    )
    # Only vec-keep-A has its inline payload on this socket.
    state.emitter._inline_geojson_by_layer_id["vec-keep-A"] = {
        "type": "FeatureCollection",
        "features": [],
    }

    # Monkeypatch the emitter's reinline to populate the MISSING survivor
    # (vec-keep-B) — mirrors the real re-read repopulating the side-table.
    async def _fake_reinline() -> int:
        emitter = state.emitter
        added = 0
        for layer in emitter.loaded_layers:
            if layer.layer_type != "vector":
                continue
            if layer.layer_id in emitter._inline_geojson_by_layer_id:
                continue
            emitter._inline_geojson_by_layer_id[layer.layer_id] = {
                "type": "FeatureCollection",
                "features": [],
            }
            added += 1
        return added

    state.emitter.reinline_vector_layers = _fake_reinline  # type: ignore[assignment]
    state.active_case_id = case_id
    ws.sent.clear()

    asyncio.run(_handle_layer_delete(ws, state, {"layer_id": "vec-del"}))

    sess = [e for e in ws.sent if e["type"] == "session-state"]
    assert sess, "expected a session-state emission after delete"
    survivors = sess[-1]["payload"]["loaded_layers"]
    survivor_ids = {layer["layer_id"] for layer in survivors}
    assert survivor_ids == {"vec-keep-A", "vec-keep-B"}
    # EVERY surviving vector layer ships with renderable inline_geojson —
    # including vec-keep-B, whose payload was missing until the re-inline.
    for layer in survivors:
        if layer["layer_type"] == "vector":
            assert "inline_geojson" in layer, (
                f"surviving vector {layer['layer_id']} shipped without "
                "inline_geojson"
            )


def test_layer_delete_persists_authoritatively_no_resurrection(
    _persistence_bound: Persistence,
) -> None:
    """The persisted Case loses the layer (replace, not union) so it cannot
    resurrect on reopen."""
    case_id = new_ulid()
    asyncio.run(_persistence_bound.upsert_case(_fresh_case_with_layers(case_id)))

    ws = MockWebSocket()
    state = SessionState(session_id=new_ulid())
    _bind_emitter_with_layers(state, ws)
    state.active_case_id = case_id

    asyncio.run(_handle_layer_delete(ws, state, {"layer_id": "flood-01"}))

    fetched = asyncio.run(_persistence_bound.get_case(case_id))
    assert fetched is not None
    assert fetched.layer_summary == ["dem-01"]
    persisted_ids = [
        d.get("layer_id") for d in fetched.loaded_layer_summaries
    ]
    assert persisted_ids == ["dem-01"]


def test_layer_delete_awareness_drops_from_present_note(
    _persistence_bound: Persistence,
) -> None:
    """After delete, the agent's loaded-layers note (built from the emitter
    snapshot) no longer lists the deleted layer."""
    case_id = new_ulid()
    asyncio.run(_persistence_bound.upsert_case(_fresh_case_with_layers(case_id)))

    ws = MockWebSocket()
    state = SessionState(session_id=new_ulid())
    _bind_emitter_with_layers(state, ws)
    state.active_case_id = case_id

    asyncio.run(_handle_layer_delete(ws, state, {"layer_id": "flood-01"}))

    snapshot = [
        layer.model_dump(mode="json") for layer in state.emitter.loaded_layers
    ]
    note = build_layers_present_note(snapshot)
    assert note is not None
    assert "flood-01" not in note
    assert "dem-01" in note


def test_layer_delete_unknown_layer_is_noop(
    _persistence_bound: Persistence,
) -> None:
    """Deleting a layer_id that isn't loaded leaves the set unchanged and
    emits no error."""
    case_id = new_ulid()
    asyncio.run(_persistence_bound.upsert_case(_fresh_case_with_layers(case_id)))

    ws = MockWebSocket()
    state = SessionState(session_id=new_ulid())
    _bind_emitter_with_layers(state, ws)
    state.active_case_id = case_id

    asyncio.run(_handle_layer_delete(ws, state, {"layer_id": "does-not-exist"}))

    remaining = {layer.layer_id for layer in state.emitter.loaded_layers}
    assert remaining == {"flood-01", "dem-01"}
    assert not [e for e in ws.sent if e["type"] == "error"]
    # Persisted set is untouched.
    fetched = asyncio.run(_persistence_bound.get_case(case_id))
    assert fetched is not None
    assert set(fetched.layer_summary) == {"flood-01", "dem-01"}


def test_layer_delete_missing_layer_id_emits_typed_error(
    _persistence_bound: Persistence,
) -> None:
    """A malformed payload (no/empty layer_id) surfaces TOOL_PARAMS_INVALID."""
    ws = MockWebSocket()
    state = SessionState(session_id=new_ulid())
    _bind_emitter_with_layers(state, ws)

    asyncio.run(_handle_layer_delete(ws, state, {"layer_id": ""}))

    errs = [e for e in ws.sent if e["type"] == "error"]
    assert errs, "expected a typed error for empty layer_id"
    assert errs[0]["payload"]["error_code"] == "TOOL_PARAMS_INVALID"
    # Emitter untouched.
    remaining = {layer.layer_id for layer in state.emitter.loaded_layers}
    assert remaining == {"flood-01", "dem-01"}


def test_delete_case_loaded_layer_replace_not_union(
    _persistence_bound: Persistence,
) -> None:
    """The persistence helper REMOVES the layer (replace semantics) — the
    opposite of _persist_case_loaded_layers' union merge."""
    case_id = new_ulid()
    asyncio.run(_persistence_bound.upsert_case(_fresh_case_with_layers(case_id)))

    state = SessionState(session_id=new_ulid())
    asyncio.run(_delete_case_loaded_layer(state, "dem-01", case_id=case_id))

    fetched = asyncio.run(_persistence_bound.get_case(case_id))
    assert fetched is not None
    assert fetched.layer_summary == ["flood-01"]
    assert [d.get("layer_id") for d in fetched.loaded_layer_summaries] == [
        "flood-01"
    ]


# --------------------------------------------------------------------------- #
# F54 — reuse-not-refetch note
# --------------------------------------------------------------------------- #


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
    # job-0326 reworded the note ("REUSE these (pass their handle/uri DIRECTLY)");
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
