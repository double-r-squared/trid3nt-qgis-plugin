"""Durable-Case persistence + old-shape ``expires_at`` tolerance.

Cases are durable: ``upsert_case`` writes NO ``expires_at`` TTL stamp (the
ephemeral-Case TTL machinery was removed -- no local reader/reaper ever acted
on it). What remains under test:

- ``test_upsert_case_writes_no_expires_at`` -- a Case upsert never stamps a TTL.
- ``test_upsert_case_authed_byte_identical_to_legacy`` -- the stored doc is
  ``model_dump(mode='json')`` + ``_id`` + (when owned) ``user_id`` and nothing
  else.
- ``test_doc_to_case_summary_drops_stale_expires_at`` -- a LEGACY stored doc
  that still carries ``expires_at`` reads back fine; the storage-only key never
  reaches the wire ``CaseSummary``.
- ``test_get_case_tolerates_legacy_expires_at`` -- end-to-end old-shape proof:
  a doc seeded with ``expires_at`` on disk is read back without crashing and
  never surfaces the key.
- ``test_seed_chat_history_*`` -- the reconnect-resync emitter primitive.

Case tests run against the file-backed Persistence substrate so the raw stored
document can be inspected on disk.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trid3nt_server.persistence import (
    CASES_COLLECTION,
    DEFAULT_DATABASE,
    FileMCPClient,
    Persistence,
)
from trid3nt_server.emission.pipeline_emitter import PipelineEmitter
from trid3nt_contracts import new_ulid, now_utc
from trid3nt_contracts.case import CaseSummary


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _fresh_case(title: str = "Scratch flood scenario") -> CaseSummary:
    return CaseSummary(
        case_id=new_ulid(),
        title=title,
        created_at=datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc),
        status="active",
        bbox=(-82.0, 26.5, -81.8, 26.7),
        primary_hazard="flood",
        layer_summary=["nlcd-anon", "flood-depth-01HX"],
    )


def _raw_doc(tmp_path: Path, case_id: str) -> dict[str, Any]:
    """Read the raw stored projects document straight off disk."""
    coll_path = tmp_path / DEFAULT_DATABASE / f"{CASES_COLLECTION}.json"
    with coll_path.open("r", encoding="utf-8") as fh:
        store = json.load(fh)
    return store[case_id]


class _CapturingSink:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    async def __call__(self, text: str) -> None:
        self.frames.append(json.loads(text))

    def session_frames(self) -> list[dict[str, Any]]:
        return [f for f in self.frames if f["type"] == "session-state"]


# --------------------------------------------------------------------------- #
# upsert_case: durable (no TTL stamp)
# --------------------------------------------------------------------------- #


def test_upsert_case_writes_no_expires_at(tmp_path: Path) -> None:
    """A Case upsert never stamps a TTL -- Cases are durable forever."""
    p = Persistence(FileMCPClient(base_dir=tmp_path))
    case = _fresh_case()

    asyncio.run(p.upsert_case(case))

    doc = _raw_doc(tmp_path, case.case_id)
    assert "expires_at" not in doc, "upsert must NOT write a TTL stamp"


def test_upsert_case_authed_byte_identical_to_legacy(tmp_path: Path) -> None:
    """The stored doc is ``model_dump(mode='json')`` + ``_id`` + ``user_id``.

    Nothing else -- no ``expires_at`` and no other storage-only field leaks in.
    """
    p = Persistence(FileMCPClient(base_dir=tmp_path))
    case = _fresh_case()
    owner = new_ulid()

    asyncio.run(p.upsert_case(case, owner_user_id=owner))

    doc = _raw_doc(tmp_path, case.case_id)
    expected = case.model_dump(mode="json")
    expected["_id"] = case.case_id
    expected["user_id"] = owner
    assert doc == expected, "stored doc drifted from the expected shape"


# --------------------------------------------------------------------------- #
# Old-shape tolerance: a legacy ``expires_at`` never reaches the wire
# --------------------------------------------------------------------------- #


def test_doc_to_case_summary_drops_stale_expires_at() -> None:
    """_doc_to_case_summary strips a stored ``expires_at`` (storage-only)."""
    case = _fresh_case()
    doc = case.model_dump(mode="json")
    doc["_id"] = case.case_id
    doc["user_id"] = new_ulid()
    # Legacy TTL stamp still present on an old-shape record.
    doc["expires_at"] = int(now_utc().timestamp()) + 604800

    summary = Persistence._doc_to_case_summary(doc)
    dumped = summary.model_dump(mode="json")
    assert "expires_at" not in dumped, "expires_at leaked onto the wire CaseSummary"
    assert not hasattr(summary, "expires_at")


def test_get_case_tolerates_legacy_expires_at(tmp_path: Path) -> None:
    """End-to-end: a doc carrying a legacy ``expires_at`` reads back cleanly."""
    p = Persistence(FileMCPClient(base_dir=tmp_path))
    case = _fresh_case()

    # Seed an OLD-SHAPE stored doc directly: the persisted record still carries
    # a numeric ``expires_at`` TTL stamp from before the chop.
    body = case.model_dump(mode="json")
    body["_id"] = case.case_id
    body["expires_at"] = int(now_utc().timestamp()) + 604800
    asyncio.run(
        p._store.call_tool(
            "update-one",
            {
                "database": DEFAULT_DATABASE,
                "collection": CASES_COLLECTION,
                "filter": {"_id": case.case_id},
                "update": {"$set": body},
                "upsert": True,
            },
        )
    )
    # The raw stored doc DOES carry the legacy TTL stamp ...
    assert "expires_at" in _raw_doc(tmp_path, case.case_id)

    # ... but the wire CaseSummary read path tolerates + drops it (no crash).
    fetched = asyncio.run(p.get_case(case.case_id))
    assert fetched is not None
    assert fetched.case_id == case.case_id
    assert "expires_at" not in fetched.model_dump(mode="json")


# --------------------------------------------------------------------------- #
# seed_chat_history: reconnect-resync primitive
# --------------------------------------------------------------------------- #


def test_seed_chat_history_carries_into_next_snapshot() -> None:
    """A seeded chat history shows up in the next session-state snapshot."""
    sink = _CapturingSink()
    emitter = PipelineEmitter(session_id=new_ulid(), sink=sink)

    seeded = [
        {"role": "user", "content": "first turn"},
        {"role": "agent", "content": "ack"},
    ]
    emitter.seed_chat_history(seeded)
    asyncio.run(emitter.emit_session_state())

    frames = sink.session_frames()
    assert len(frames) == 1
    assert frames[0]["payload"]["chat_history"] == seeded


def test_seed_chat_history_defensive_copy() -> None:
    """seed_chat_history takes a copy; later caller mutation must not bleed in."""
    sink = _CapturingSink()
    emitter = PipelineEmitter(session_id=new_ulid(), sink=sink)

    seeded = [{"role": "user", "content": "only turn"}]
    emitter.seed_chat_history(seeded)
    seeded.append({"role": "agent", "content": "MUTATED AFTER SEED"})

    asyncio.run(emitter.emit_session_state())
    frames = sink.session_frames()
    assert frames[0]["payload"]["chat_history"] == [
        {"role": "user", "content": "only turn"}
    ]


def test_seed_chat_history_none_is_empty() -> None:
    """seed_chat_history(None) is tolerated and yields an empty history."""
    sink = _CapturingSink()
    emitter = PipelineEmitter(session_id=new_ulid(), sink=sink)

    emitter.seed_chat_history(None)  # type: ignore[arg-type]
    asyncio.run(emitter.emit_session_state())
    frames = sink.session_frames()
    assert frames[0]["payload"]["chat_history"] == []
