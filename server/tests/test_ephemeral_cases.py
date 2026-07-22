"""#147 ephemeral-cases + reconnect-resync PRIMITIVES (DORMANT until wired).

These tests cover the back-compat persistence + emitter primitives added for
the #147 track. Every primitive defaults to current behavior; these tests pin
both the new opt-in behavior AND the byte-identical legacy default:

- ``test_upsert_case_ephemeral_writes_future_numeric_expires_at`` — an
  ephemeral Case lands a NUMERIC epoch ``expires_at`` in the future (the
  DynamoDB-native TTL attr, NOT the ISO string sessions use).
- ``test_upsert_case_default_writes_no_expires_at`` — the default (and authed)
  call shape writes NO ``expires_at`` (durable forever).
- ``test_upsert_case_authed_byte_identical_to_legacy`` — an authed upsert
  (``ephemeral`` omitted) writes the EXACT same stored doc as before the kwarg
  existed (no ``expires_at`` leaks in).
- ``test_touch_case_advances_expires_at`` — ``touch_case`` slides the numeric
  TTL window forward on an existing ephemeral Case.
- ``test_touch_case_uses_explicit_ttl_seconds`` — an explicit ``ttl_seconds``
  is honored over the default ``CASES_ANON_TTL_SECONDS``.
- ``test_doc_to_case_summary_drops_expires_at`` — the storage-only TTL stamp
  NEVER reaches the wire ``CaseSummary``.
- ``test_get_case_never_surfaces_expires_at`` — end-to-end read path proof.
- ``test_seed_chat_history_carries_into_next_snapshot`` — a seeded chat
  history shows up in the next ``emit_session_state`` snapshot.
- ``test_seed_chat_history_defensive_copy`` — seeding takes a copy; later
  mutation of the caller's list does not bleed into the emitter mirror.

All Case tests run against the file-backed Persistence substrate so the raw
stored document (with ``expires_at``) can be inspected on disk.
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
from trid3nt_server.pipeline_emitter import PipelineEmitter
from trid3nt_contracts import new_ulid, now_utc
from trid3nt_contracts.case import CaseSummary
from trid3nt_contracts.collections import CASES_ANON_TTL_SECONDS


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _fresh_case(title: str = "Anonymous scratch flood scenario") -> CaseSummary:
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
# upsert_case: ephemeral vs durable (default)
# --------------------------------------------------------------------------- #


def test_upsert_case_ephemeral_writes_future_numeric_expires_at(tmp_path: Path) -> None:
    """ephemeral=True stamps a NUMBER epoch expires_at in the future."""
    p = Persistence(FileMCPClient(base_dir=tmp_path))
    case = _fresh_case()
    before = int(now_utc().timestamp())

    asyncio.run(p.upsert_case(case, ephemeral=True))

    doc = _raw_doc(tmp_path, case.case_id)
    assert "expires_at" in doc, "ephemeral Case must carry an expires_at TTL stamp"
    exp = doc["expires_at"]
    # DynamoDB-native TTL needs a NUMBER epoch, NOT the ISO string sessions use.
    assert isinstance(exp, int), f"expires_at must be a numeric epoch, got {type(exp)!r}"
    assert not isinstance(exp, bool)
    # In the future by roughly the configured window.
    assert exp >= before + CASES_ANON_TTL_SECONDS - 5
    assert exp <= int(now_utc().timestamp()) + CASES_ANON_TTL_SECONDS + 5


def test_upsert_case_default_writes_no_expires_at(tmp_path: Path) -> None:
    """The default (ephemeral omitted) writes NO expires_at — durable forever."""
    p = Persistence(FileMCPClient(base_dir=tmp_path))
    case = _fresh_case()

    asyncio.run(p.upsert_case(case))

    doc = _raw_doc(tmp_path, case.case_id)
    assert "expires_at" not in doc, "default upsert must NOT write a TTL stamp"


def test_upsert_case_explicit_non_ephemeral_writes_no_expires_at(tmp_path: Path) -> None:
    """ephemeral=False is identical to the default — no TTL stamp."""
    p = Persistence(FileMCPClient(base_dir=tmp_path))
    case = _fresh_case()

    asyncio.run(p.upsert_case(case, ephemeral=False))

    doc = _raw_doc(tmp_path, case.case_id)
    assert "expires_at" not in doc


def test_upsert_case_authed_byte_identical_to_legacy(tmp_path: Path) -> None:
    """An authed upsert (ephemeral omitted) is byte-identical to the legacy doc.

    The legacy stored doc was ``model_dump(mode='json')`` + ``_id`` + (when
    owned) ``user_id`` — and NOTHING else. The new kwarg must not perturb it.
    """
    p = Persistence(FileMCPClient(base_dir=tmp_path))
    case = _fresh_case()
    owner = new_ulid()

    asyncio.run(p.upsert_case(case, owner_user_id=owner))

    doc = _raw_doc(tmp_path, case.case_id)
    expected = case.model_dump(mode="json")
    expected["_id"] = case.case_id
    expected["user_id"] = owner
    assert doc == expected, "authed upsert doc drifted from the legacy shape"


# --------------------------------------------------------------------------- #
# touch_case: slide the TTL window
# --------------------------------------------------------------------------- #


def test_touch_case_advances_expires_at(tmp_path: Path) -> None:
    """touch_case moves the numeric expires_at forward on an existing Case."""
    p = Persistence(FileMCPClient(base_dir=tmp_path))
    case = _fresh_case()

    # Seed an ephemeral Case with a short window so a touch with the default
    # (7-day) window is unambiguously larger.
    asyncio.run(p.upsert_case(case, ephemeral=True, owner_user_id=None))
    # Overwrite expires_at with a stale, near value to make the advance obvious.
    asyncio.run(p.touch_case(case.case_id, ttl_seconds=10))
    stale = _raw_doc(tmp_path, case.case_id)["expires_at"]

    asyncio.run(p.touch_case(case.case_id))  # default = CASES_ANON_TTL_SECONDS
    advanced = _raw_doc(tmp_path, case.case_id)["expires_at"]

    assert isinstance(advanced, int)
    assert advanced > stale, "touch_case must slide the TTL window forward"
    assert advanced >= int(now_utc().timestamp()) + CASES_ANON_TTL_SECONDS - 5


def test_touch_case_uses_explicit_ttl_seconds(tmp_path: Path) -> None:
    """An explicit ttl_seconds overrides the default window."""
    p = Persistence(FileMCPClient(base_dir=tmp_path))
    case = _fresh_case()
    asyncio.run(p.upsert_case(case, ephemeral=True))

    custom_ttl = 12345
    before = int(now_utc().timestamp())
    asyncio.run(p.touch_case(case.case_id, ttl_seconds=custom_ttl))

    exp = _raw_doc(tmp_path, case.case_id)["expires_at"]
    assert before + custom_ttl - 5 <= exp <= int(now_utc().timestamp()) + custom_ttl + 5


def test_touch_case_swallows_errors(tmp_path: Path) -> None:
    """touch_case is fire-and-forget: a backend hiccup never raises."""

    class _BoomMCP:
        async def call_tool(self, *_a: Any, **_k: Any) -> Any:
            raise RuntimeError("simulated backend failure")

    p = Persistence(_BoomMCP())
    # Must not raise even though the underlying call_tool blows up.
    asyncio.run(p.touch_case("nonexistent-case"))


# --------------------------------------------------------------------------- #
# Read path: expires_at must NEVER reach the wire CaseSummary
# --------------------------------------------------------------------------- #


def test_doc_to_case_summary_drops_expires_at() -> None:
    """_doc_to_case_summary strips the storage-only expires_at TTL stamp."""
    case = _fresh_case()
    doc = case.model_dump(mode="json")
    doc["_id"] = case.case_id
    doc["user_id"] = new_ulid()
    doc["expires_at"] = int(now_utc().timestamp()) + CASES_ANON_TTL_SECONDS

    summary = Persistence._doc_to_case_summary(doc)
    dumped = summary.model_dump(mode="json")
    assert "expires_at" not in dumped, "expires_at leaked onto the wire CaseSummary"
    assert not hasattr(summary, "expires_at")


def test_get_case_never_surfaces_expires_at(tmp_path: Path) -> None:
    """End-to-end: an ephemeral Case read back carries no expires_at on the wire."""
    p = Persistence(FileMCPClient(base_dir=tmp_path))
    case = _fresh_case()
    asyncio.run(p.upsert_case(case, ephemeral=True))

    # The raw stored doc DOES carry the TTL stamp ...
    assert "expires_at" in _raw_doc(tmp_path, case.case_id)

    # ... but the wire CaseSummary read path does NOT.
    fetched = asyncio.run(p.get_case(case.case_id))
    assert fetched is not None
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
