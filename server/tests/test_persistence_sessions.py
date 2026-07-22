"""job-0203 (Wave 4.11 M4): D.6 session-record persistence + FileMCPClient operators.

Covers the two Phase-1 acceptance surfaces:

1. ``Persistence.touch_session`` / ``get_session_record`` /
   ``upsert_session_record`` round-trips — on BOTH the mock MCP client
   (protocol-shape assertions) and the live ``FileMCPClient`` substrate
   (semantics assertions).
2. ``FileMCPClient._apply_update`` Mongo-faithful operator semantics,
   including the job-0230 chart-drop regression: a ``$push`` onto the
   ``sessions`` collection must actually land on the dev substrate.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from grace2_agent.persistence import (
    FileMCPClient,
    Persistence,
    SESSIONS_COLLECTION,
)
from grace2_contracts import new_ulid
from grace2_contracts.collections import SessionDocument


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


class RecordingMCPClient:
    """Mock MCP client that records calls and replays canned responses."""

    def __init__(self, responses: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._responses = list(responses or [])

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.calls.append((name, dict(arguments or {})))
        if self._responses:
            return self._responses.pop(0)
        return {}


@pytest.fixture()
def file_persistence(tmp_path):
    return Persistence(FileMCPClient(base_dir=tmp_path))


# --------------------------------------------------------------------------- #
# touch_session — protocol shape (mock client)
# --------------------------------------------------------------------------- #


def _healthy_doc(sid: str) -> dict[str, Any]:
    return {
        "document": {
            "_id": sid,
            "schema_version": "v1",
            "created_at": "2026-06-09T00:00:00Z",
            "last_active_at": "2026-06-09T00:00:00Z",
            "expires_at": "2026-07-09T00:00:00Z",
        }
    }


def test_touch_session_sends_upsert_with_ttl_fields_then_verifies_header():
    sid = new_ulid()
    mcp = RecordingMCPClient(responses=[{}, _healthy_doc(sid)])
    p = Persistence(mcp)
    asyncio.run(p.touch_session(sid, case_id="01CASEULID000000000000000A"))

    # update-one (upsert) + find-one (header verify); healthy doc → no repair
    assert [c[0] for c in mcp.calls] == ["update-one", "find-one"]
    name, args = mcp.calls[0]
    assert args["collection"] == SESSIONS_COLLECTION
    assert args["filter"] == {"_id": sid}
    assert args["upsert"] is True
    update = args["update"]
    assert set(update["$set"]) == {"last_active_at", "expires_at"}
    assert update["$setOnInsert"]["schema_version"] == "v1"
    assert "created_at" in update["$setOnInsert"]
    assert update["$addToSet"] == {"project_ids": "01CASEULID000000000000000A"}


def test_touch_session_without_case_omits_addtoset():
    sid = new_ulid()
    mcp = RecordingMCPClient(responses=[{}, _healthy_doc(sid)])
    p = Persistence(mcp)
    asyncio.run(p.touch_session(sid))
    _, args = mcp.calls[0]
    assert "$addToSet" not in args["update"]


def test_touch_session_expires_after_last_active():
    sid = new_ulid()
    mcp = RecordingMCPClient(responses=[{}, _healthy_doc(sid)])
    p = Persistence(mcp)
    asyncio.run(p.touch_session(sid))
    set_ = mcp.calls[0][1]["update"]["$set"]
    assert set_["expires_at"] > set_["last_active_at"]


def test_touch_session_repairs_headerless_doc():
    sid = new_ulid()
    headerless = {"document": {"_id": sid, "charts": [{"chart_id": "early"}]}}
    mcp = RecordingMCPClient(responses=[{}, headerless, {}])
    p = Persistence(mcp)
    asyncio.run(p.touch_session(sid))
    assert [c[0] for c in mcp.calls] == ["update-one", "find-one", "update-one"]
    repair = mcp.calls[2][1]["update"]["$set"]
    assert repair["schema_version"] == "v1"
    assert "created_at" in repair


# --------------------------------------------------------------------------- #
# Session record lifecycle — live FileMCPClient substrate
# --------------------------------------------------------------------------- #


def test_first_touch_creates_valid_session_document(file_persistence):
    sid = new_ulid()

    async def run():
        await file_persistence.touch_session(sid)
        return await file_persistence.get_session_record(sid)

    doc = asyncio.run(run())
    assert isinstance(doc, SessionDocument)
    assert doc.id == sid
    assert doc.schema_version == "v1"
    assert doc.expires_at > doc.last_active_at
    assert doc.project_ids == []


def test_second_touch_advances_activity_preserves_created_at(file_persistence):
    sid = new_ulid()

    async def run():
        await file_persistence.touch_session(sid)
        first = await file_persistence.get_session_record(sid)
        await asyncio.sleep(0.02)
        await file_persistence.touch_session(sid)
        second = await file_persistence.get_session_record(sid)
        return first, second

    first, second = asyncio.run(run())
    assert second.created_at == first.created_at  # $setOnInsert held
    assert second.last_active_at > first.last_active_at
    assert second.expires_at > first.expires_at


def test_touch_addtoset_dedupes_project_ids(file_persistence):
    sid = new_ulid()
    case_a, case_b = new_ulid(), new_ulid()

    async def run():
        await file_persistence.touch_session(sid, case_id=case_a)
        await file_persistence.touch_session(sid, case_id=case_a)  # dupe
        await file_persistence.touch_session(sid, case_id=case_b)
        return await file_persistence.get_session_record(sid)

    doc = asyncio.run(run())
    assert sorted(doc.project_ids) == sorted([case_a, case_b])


def test_upsert_session_record_round_trip(file_persistence):
    from grace2_contracts import now_utc

    sid = new_ulid()
    now = now_utc()
    doc = SessionDocument(
        _id=sid,
        created_at=now,
        last_active_at=now,
        expires_at=now,
        client_fingerprint="fp-abc",
    )

    async def run():
        await file_persistence.upsert_session_record(doc)
        return await file_persistence.get_session_record(sid)

    back = asyncio.run(run())
    assert back.id == sid
    assert back.client_fingerprint == "fp-abc"


def test_get_session_record_missing_returns_none(file_persistence):
    assert asyncio.run(file_persistence.get_session_record(new_ulid())) is None


# --------------------------------------------------------------------------- #
# job-0230 regression: chart $push lands on the dev substrate and the
# session record stays readable (extras dropped on typed read)
# --------------------------------------------------------------------------- #


def test_chart_push_lands_and_typed_read_tolerates_extras(file_persistence, tmp_path):
    sid = new_ulid()
    chart = {"chart_id": "c1", "title": "Histogram", "vega_lite_spec": {"mark": "bar"}}

    async def run():
        await file_persistence.touch_session(sid)
        # Mirror server._persist_chart_record's raw MCP call shape exactly.
        await file_persistence._mcp.call_tool(
            "update-one",
            {
                "database": file_persistence._db,
                "collection": SESSIONS_COLLECTION,
                "filter": {"_id": sid},
                "update": {"$push": {"charts": chart}},
                "upsert": True,
            },
        )
        raw = await file_persistence._mcp.call_tool(
            "find-one",
            {
                "database": file_persistence._db,
                "collection": SESSIONS_COLLECTION,
                "filter": {"_id": sid},
            },
        )
        typed = await file_persistence.get_session_record(sid)
        return raw, typed

    raw, typed = asyncio.run(run())
    stored = raw["document"]
    assert stored["charts"] == [chart]  # the pre-M4 substrate dropped this
    assert isinstance(typed, SessionDocument)  # extras tolerated on read


def test_chart_push_on_headerless_doc_then_touch_backfills_header(file_persistence):
    """Chart arrives BEFORE any touch (job-0230 ordering) — the later touch
    must backfill the D.6 header without clobbering the charts array."""
    sid = new_ulid()

    async def run():
        await file_persistence._mcp.call_tool(
            "update-one",
            {
                "database": file_persistence._db,
                "collection": SESSIONS_COLLECTION,
                "filter": {"_id": sid},
                "update": {"$push": {"charts": {"chart_id": "early"}}},
                "upsert": True,
            },
        )
        await file_persistence.touch_session(sid)
        raw = await file_persistence._mcp.call_tool(
            "find-one",
            {
                "database": file_persistence._db,
                "collection": SESSIONS_COLLECTION,
                "filter": {"_id": sid},
            },
        )
        typed = await file_persistence.get_session_record(sid)
        return raw["document"], typed

    stored, typed = asyncio.run(run())
    assert stored["charts"] == [{"chart_id": "early"}]
    assert typed is not None and typed.schema_version == "v1"


# --------------------------------------------------------------------------- #
# FileMCPClient._apply_update operator semantics
# --------------------------------------------------------------------------- #


def test_apply_update_setoninsert_only_on_insert():
    doc = {"_id": "x", "created_at": "T0"}
    FileMCPClient._apply_update(
        doc, {"$setOnInsert": {"created_at": "T1"}}, inserting=False
    )
    assert doc["created_at"] == "T0"
    fresh = {"_id": "y"}
    FileMCPClient._apply_update(
        fresh, {"$setOnInsert": {"created_at": "T1"}}, inserting=True
    )
    assert fresh["created_at"] == "T1"


def test_apply_update_push_creates_and_appends():
    doc: dict = {"_id": "x"}
    FileMCPClient._apply_update(doc, {"$push": {"charts": 1}}, inserting=False)
    FileMCPClient._apply_update(doc, {"$push": {"charts": 1}}, inserting=False)
    assert doc["charts"] == [1, 1]  # $push does NOT dedupe


def test_apply_update_addtoset_dedupes_dict_values():
    doc: dict = {"_id": "x"}
    v = {"a": 1}
    FileMCPClient._apply_update(doc, {"$addToSet": {"ids": v}}, inserting=False)
    FileMCPClient._apply_update(doc, {"$addToSet": {"ids": {"a": 1}}}, inserting=False)
    assert doc["ids"] == [{"a": 1}]


def test_apply_update_unknown_operator_raises():
    with pytest.raises(NotImplementedError):
        FileMCPClient._apply_update({}, {"$inc": {"n": 1}}, inserting=False)


def test_apply_update_combined_operators_one_call():
    fresh: dict = {"_id": "s"}
    FileMCPClient._apply_update(
        fresh,
        {
            "$set": {"last_active_at": "T1"},
            "$setOnInsert": {"created_at": "T1"},
            "$addToSet": {"project_ids": "case-1"},
        },
        inserting=True,
    )
    assert fresh == {
        "_id": "s",
        "last_active_at": "T1",
        "created_at": "T1",
        "project_ids": ["case-1"],
    }
