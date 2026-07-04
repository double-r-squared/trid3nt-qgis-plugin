"""Test Case layer persistence + emitter rehydration (job-0172 Part B).

When a tool publishes a layer inside an active Case, the agent now:

1. Appends the ``ProjectLayerSummary`` to ``Case.loaded_layer_summaries``
   (and the layer_id to ``Case.layer_summary``) via ``upsert_case``.
2. On a subsequent ``case-open``, ``get_session_state`` reads the
   persisted list into ``CaseSessionState.loaded_layers`` so a Case
   re-open repopulates the LayerPanel deterministically.
3. The ``PipelineEmitter.reset_loaded_layers`` method seeds the
   per-connection accumulator from a persisted snapshot.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from grace2_agent.persistence import (
    CASES_COLLECTION,
    CHAT_COLLECTION,
    Persistence,
)
from grace2_agent.pipeline_emitter import PipelineEmitter
from grace2_contracts.case import CaseSummary
from grace2_contracts.common import new_ulid, now_utc
from grace2_contracts.collections import ProjectLayerSummary


class FakeMCPClient:
    """In-memory MCP client supporting projects + chat collections."""

    def __init__(self) -> None:
        self.collections: dict[str, dict[str, dict]] = {}

    def _store(self, name: str) -> dict[str, dict]:
        return self.collections.setdefault(name, {})

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        args = arguments or {}
        coll = args.get("collection")
        store = self._store(coll)
        if name == "find-one":
            filt = args.get("filter", {})
            for doc in store.values():
                if all(doc.get(k) == v for k, v in filt.items()):
                    return {"document": doc}
            return {"document": None}
        if name == "find":
            filt = args.get("filter", {})
            results = [
                d
                for d in store.values()
                if all(d.get(k) == v for k, v in filt.items() if k != "$or")
            ]
            return {"documents": results}
        if name == "update-one":
            filt = args.get("filter", {})
            update = args.get("update", {}).get("$set", {})
            uid = filt.get("_id")
            if uid is None:
                return {"matchedCount": 0, "modifiedCount": 0}
            if uid in store:
                store[uid].update(update)
            elif args.get("upsert"):
                store[uid] = dict(update)
            return {"matchedCount": 1, "modifiedCount": 1}
        if name == "insert-one":
            doc = args.get("document", {})
            store[doc["_id"]] = doc
            return {"insertedId": doc["_id"]}
        return {}


@pytest.mark.asyncio
async def test_case_loaded_layer_summaries_persists_and_hydrates() -> None:
    """``upsert_case`` writes layers; ``get_session_state`` reads them back."""
    client = FakeMCPClient()
    p = Persistence(client)

    # Create a Case
    case_id = new_ulid()
    case = CaseSummary(
        case_id=case_id,
        title="Hurricane Ian Demo",
        created_at=now_utc(),
        updated_at=now_utc(),
    )
    await p.upsert_case(case)

    # Simulate publish_layer firing: agent updates the Case with the new layer
    layer = ProjectLayerSummary(
        layer_id="L_flood_001",
        name="flood depth",
        layer_type="raster",
        uri="https://qgis.example/wms?LAYERS=flood",
        style_preset="flood_depth_v1",
        visible=True,
        role="primary",
        temporal=False,
    )
    fresh = case.model_copy(
        update={
            "loaded_layer_summaries": [layer.model_dump(mode="json")],
            "layer_summary": [layer.layer_id],
        }
    )
    await p.upsert_case(fresh)

    # Re-open the Case — hydration must surface the persisted layer
    session = await p.get_session_state(case_id)
    assert len(session.loaded_layers) == 1
    assert session.loaded_layers[0]["layer_id"] == "L_flood_001"
    assert session.case.layer_summary == ["L_flood_001"]


@pytest.mark.asyncio
async def test_pipeline_emitter_reset_loaded_layers_seeds_from_snapshot() -> None:
    """``reset_loaded_layers`` replaces the in-memory accumulator."""
    sent: list[str] = []

    async def sink(text: str) -> None:
        sent.append(text)

    emitter = PipelineEmitter(session_id="01" * 13, sink=sink)
    assert emitter.loaded_layers == []

    seeded = [
        {
            "layer_id": "L1",
            "name": "n1",
            "layer_type": "raster",
            "uri": "u1",
            "style_preset": "p",
            "visible": True,
            "role": "primary",
            "temporal": False,
        },
        {
            "layer_id": "L2",
            "name": "n2",
            "layer_type": "vector",
            "uri": "u2",
            "style_preset": "p",
            "visible": True,
            "role": "primary",
            "temporal": False,
        },
    ]
    emitter.reset_loaded_layers(seeded)
    assert len(emitter.loaded_layers) == 2
    assert emitter.loaded_layers[0].layer_id == "L1"
    assert emitter.loaded_layers[1].uri == "u2"

    # Empty list flushes
    emitter.reset_loaded_layers([])
    assert emitter.loaded_layers == []

    # None flushes too
    emitter.reset_loaded_layers(seeded)
    emitter.reset_loaded_layers(None)
    assert emitter.loaded_layers == []


@pytest.mark.asyncio
async def test_pipeline_emitter_reset_skips_malformed_entries() -> None:
    """Malformed layer dicts are skipped, not crashing the reset."""
    async def sink(text: str) -> None:
        return None

    emitter = PipelineEmitter(session_id="02" * 13, sink=sink)
    mixed = [
        {
            "layer_id": "L1",
            "name": "ok",
            "layer_type": "raster",
            "uri": "u1",
            "style_preset": "p",
            "visible": True,
            "role": "primary",
            "temporal": False,
        },
        {"bogus": "shape"},  # missing required fields
        "not-a-dict",  # type: ignore[list-item]
        {
            "layer_id": "L2",
            "name": "ok2",
            "layer_type": "raster",
            "uri": "u2",
            "style_preset": "p",
            "visible": True,
            "role": "primary",
            "temporal": False,
        },
    ]
    emitter.reset_loaded_layers(mixed)  # type: ignore[arg-type]
    # Only the two well-formed entries should survive.
    layer_ids = sorted(layer.layer_id for layer in emitter.loaded_layers)
    assert layer_ids == ["L1", "L2"]
