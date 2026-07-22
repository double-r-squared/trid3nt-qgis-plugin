"""#147 ephemeral-cases + reconnect-resync SERVER WIRING (the call-sites).

``test_ephemeral_cases.py`` pins the DORMANT primitives
(``upsert_case(ephemeral=...)`` / ``touch_case`` / ``seed_chat_history``). This
module pins that ``server.py`` actually CALLS them at the right sites, with the
load-bearing ``is_anonymous`` gate:

(a) CASE-CREATE: a Case created on an ANONYMOUS session gets a FUTURE numeric
    ``expires_at`` (ephemeral); an AUTHENTICATED session's Case gets NONE
    (durable forever). Driven through the REAL ``_auto_create_case_from_root``
    create site against file-backed Persistence so the raw stored doc is
    inspectable on disk.

(b) ACTIVITY HEARTBEAT: ``_touch_session_record`` calls ``touch_case`` for an
    anon active Case and does NOT for an authed one (and never for a None
    active Case). Driven with a touch-counting spy Persistence.

(d) SAFETY (load-bearing): an anon Case whose activity heartbeat just fired has
    a FUTURE ``expires_at`` — i.e. activity keeps the Case warm so a sweep
    would NOT reap it. Driven end-to-end through file-backed Persistence
    (create ephemeral -> fire heartbeat -> inspect raw doc).

Test (c) (reconnect replay seeds emitter chat_history) lives in
``test_resume_replays_case_layers.py`` alongside the layer-replay tests it
extends.

All file-backed tests inspect the RAW stored doc (with ``expires_at``) on disk.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from trid3nt_server import server
from trid3nt_server.persistence import (
    CASES_COLLECTION,
    FileMCPClient,
    Persistence,
)
from trid3nt_contracts import new_ulid, now_utc
from trid3nt_contracts.collections import CASES_ANON_TTL_SECONDS


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


class FakeWS:
    """Minimal WS stand-in (mirrors the server tests' FakeWS)."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False

    async def send(self, text: str) -> None:
        if self.closed:
            raise ConnectionError("socket closed")
        self.sent.append(text)


def _file_persistence(tmp_path: Path) -> Persistence:
    return Persistence(FileMCPClient(base_dir=tmp_path / "store"))


def _raw_case_doc(tmp_path: Path, case_id: str) -> dict[str, Any]:
    """Read the raw stored projects document straight off disk."""
    # FileMCPClient writes under <base_dir>/<database>/<collection>.json.
    store_root = tmp_path / "store"
    db_dir = next(d for d in store_root.iterdir() if d.is_dir())
    coll_path = db_dir / f"{CASES_COLLECTION}.json"
    with coll_path.open("r", encoding="utf-8") as fh:
        store = json.load(fh)
    return store[case_id]


def _only_case_id(tmp_path: Path) -> str:
    store_root = tmp_path / "store"
    db_dir = next(d for d in store_root.iterdir() if d.is_dir())
    coll_path = db_dir / f"{CASES_COLLECTION}.json"
    with coll_path.open("r", encoding="utf-8") as fh:
        store = json.load(fh)
    keys = list(store.keys())
    assert len(keys) == 1, f"expected exactly one stored Case, got {keys}"
    return keys[0]


class _TouchSpyPersistence:
    """Counts ``touch_session`` / ``touch_case`` calls (the heartbeat sites)."""

    def __init__(self) -> None:
        self.touch_session_calls: list[tuple[str, str | None]] = []
        self.touch_case_calls: list[str] = []

    async def touch_session(self, session_id: str, *, case_id: str | None = None) -> None:
        self.touch_session_calls.append((session_id, case_id))

    async def touch_case(self, case_id: str, *, ttl_seconds: int | None = None) -> None:
        self.touch_case_calls.append(case_id)


@pytest.fixture(autouse=True)
def _clean_registries():
    saved_p = server.get_persistence()
    saved_reg = dict(server._SESSION_ACTIVE_CASE)
    saved_named = set(server._AUTONAMED_CASES)
    server._SESSION_ACTIVE_CASE.clear()
    yield
    server.set_persistence(saved_p)
    server._SESSION_ACTIVE_CASE.clear()
    server._SESSION_ACTIVE_CASE.update(saved_reg)
    server._AUTONAMED_CASES.clear()
    server._AUTONAMED_CASES.update(saved_named)


# --------------------------------------------------------------------------- #
# (a) CASE-CREATE: ephemeral for anon, durable for authed
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_auto_create_anon_case_is_ephemeral(tmp_path: Path) -> None:
    """An anonymous root prompt mints a Case with a FUTURE numeric expires_at."""
    server.set_persistence(_file_persistence(tmp_path))
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    state.is_anonymous = True  # anonymous session
    state.authenticated_user_id = new_ulid()  # sticky-anon ULID (still anon)

    before = int(now_utc().timestamp())
    new_case_id = await server._auto_create_case_from_root(ws, state, "flood Fort Myers")
    assert new_case_id is not None

    doc = _raw_case_doc(tmp_path, new_case_id)
    assert "expires_at" in doc, "an anon Case MUST carry an ephemeral TTL stamp"
    exp = doc["expires_at"]
    assert isinstance(exp, int) and not isinstance(exp, bool)
    assert exp >= before + CASES_ANON_TTL_SECONDS - 5, "expires_at must be in the future"


@pytest.mark.asyncio
async def test_auto_create_authed_case_is_durable(tmp_path: Path) -> None:
    """An authenticated root prompt mints a DURABLE Case (no expires_at)."""
    server.set_persistence(_file_persistence(tmp_path))
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    state.is_anonymous = False  # authenticated session
    state.authenticated_user_id = new_ulid()

    new_case_id = await server._auto_create_case_from_root(ws, state, "flood Fort Myers")
    assert new_case_id is not None

    doc = _raw_case_doc(tmp_path, new_case_id)
    assert "expires_at" not in doc, "an authed Case must be durable (no TTL stamp)"


# --------------------------------------------------------------------------- #
# (b) ACTIVITY HEARTBEAT: touch_case fires for anon, not for authed
# --------------------------------------------------------------------------- #


def test_touch_session_record_touches_case_for_anon() -> None:
    """_touch_session_record slides the Case TTL for an ANON active Case."""
    spy = _TouchSpyPersistence()
    server.set_persistence(spy)
    case_id = new_ulid()
    state = server.SessionState(session_id=new_ulid())
    state.is_anonymous = True
    server._set_session_active_case(state.session_id, case_id)

    asyncio.run(server._touch_session_record(state))

    assert spy.touch_session_calls, "session heartbeat must still fire"
    assert spy.touch_case_calls == [case_id], (
        "an anon active Case must get a touch_case TTL slide on activity"
    )


def test_touch_session_record_skips_case_for_authed() -> None:
    """An AUTHED active Case gets NO touch_case (durable; never TTL-written)."""
    spy = _TouchSpyPersistence()
    server.set_persistence(spy)
    case_id = new_ulid()
    state = server.SessionState(session_id=new_ulid())
    state.is_anonymous = False
    server._set_session_active_case(state.session_id, case_id)

    asyncio.run(server._touch_session_record(state))

    assert spy.touch_session_calls, "session heartbeat still fires for authed"
    assert spy.touch_case_calls == [], (
        "an authed Case must NEVER receive a touch_case TTL write"
    )


def test_touch_session_record_skips_case_when_no_active_case() -> None:
    """No active Case -> no touch_case even for an anon session (None guard)."""
    spy = _TouchSpyPersistence()
    server.set_persistence(spy)
    state = server.SessionState(session_id=new_ulid())
    state.is_anonymous = True
    # No active case set -> state.active_case_id is None.

    asyncio.run(server._touch_session_record(state))

    assert spy.touch_case_calls == [], "no active Case -> nothing to touch"


def test_touch_session_record_uses_explicit_case_id_for_anon() -> None:
    """The explicit case_id arg is the Case touched (create/open path shape)."""
    spy = _TouchSpyPersistence()
    server.set_persistence(spy)
    explicit = new_ulid()
    state = server.SessionState(session_id=new_ulid())
    state.is_anonymous = True
    # active_case_id is None, but the create/open sites pass case_id explicitly.

    asyncio.run(server._touch_session_record(state, case_id=explicit))

    assert spy.touch_case_calls == [explicit]


# --------------------------------------------------------------------------- #
# (d) SAFETY (load-bearing): activity keeps an anon Case warm (future TTL)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_heartbeat_keeps_anon_case_warm(tmp_path: Path) -> None:
    """An anon Case whose heartbeat just fired has a FUTURE expires_at.

    This is the load-bearing safety property: a sweep keys off ``expires_at``,
    and the activity heartbeat (``_touch_session_record`` on every persisted
    turn) slides it forward, so an ACTIVELY-USED anon Case is never reaped.
    """
    server.set_persistence(_file_persistence(tmp_path))
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    state.is_anonymous = True
    state.authenticated_user_id = new_ulid()

    # 1) anon root prompt -> ephemeral Case created with a TTL stamp.
    case_id = await server._auto_create_case_from_root(ws, state, "flood Fort Myers")
    assert case_id is not None
    created_exp = _raw_case_doc(tmp_path, case_id)["expires_at"]

    # 2) Make the existing stamp look STALE so the heartbeat advance is provable.
    p = server.get_persistence()
    await p.touch_case(case_id, ttl_seconds=10)
    stale_exp = _raw_case_doc(tmp_path, case_id)["expires_at"]
    assert stale_exp < created_exp

    # 3) Activity (a persisted turn) fires the heartbeat -> TTL slides FORWARD.
    now_floor = int(now_utc().timestamp())
    await server._touch_session_record(state)
    warm_exp = _raw_case_doc(tmp_path, case_id)["expires_at"]

    assert warm_exp > stale_exp, "activity must slide the TTL window forward"
    # WOULD SURVIVE A SWEEP: the refreshed expires_at is comfortably in the
    # future (a sweep reaps only docs whose expires_at <= now).
    assert warm_exp >= now_floor + CASES_ANON_TTL_SECONDS - 5
    assert warm_exp > now_floor


@pytest.mark.asyncio
async def test_heartbeat_never_resurrects_durable_authed_case(tmp_path: Path) -> None:
    """An authed Case stays TTL-free even after repeated activity heartbeats."""
    server.set_persistence(_file_persistence(tmp_path))
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    state.is_anonymous = False
    state.authenticated_user_id = new_ulid()

    case_id = await server._auto_create_case_from_root(ws, state, "flood Fort Myers")
    assert case_id is not None
    assert "expires_at" not in _raw_case_doc(tmp_path, case_id)

    # Fire the heartbeat a few times — an authed Case must never gain a TTL.
    for _ in range(3):
        await server._touch_session_record(state)

    assert "expires_at" not in _raw_case_doc(tmp_path, case_id), (
        "activity must not stamp a TTL onto an authed (durable) Case"
    )
