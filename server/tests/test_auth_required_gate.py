"""Production auth-hardening DELTA tests (job-0252, sprint-13.5 Stage 1).

Covers the two things this job adds on top of the Wave 2 connect handshake
(``auth_handshake.py`` — tested separately in ``test_auth_handshake.py``):

1. **The ``AUTH_REQUIRED`` gate** (``grace2_agent.auth.auth_required`` +
   ``server._handle_auth_token`` / ``_ensure_auth_handshake``):
   - ``AUTH_REQUIRED`` unset / "false" → today's anonymous behavior is
     preserved EXACTLY (a forged/expired/absent token falls back to
     anonymous and the connection proceeds).
   - ``AUTH_REQUIRED`` true → a forged/expired/absent token (i.e. an
     anonymous resolution) is REJECTED: an A.6 ``AUTH_FAILED`` error
     envelope is emitted and the socket is closed with the A.5 close code
     ``4401``. There is NO anonymous fallback on the required path.
   - A VALID token under ``AUTH_REQUIRED=true`` still binds + proceeds.

2. **The pre-Auth case migration** (``persistence.migrate_preauth_cases`` +
   ``server._run_preauth_case_migration``):
   - stamps every Case lacking a ``user_id`` with ``MIGRATION_ANON_UID``;
   - is idempotent (a second run matches nothing);
   - does not corrupt already-owned Cases or other collections;
   - leaves the ``$exists:false`` leak clause gone (owner-less Cases are
     invisible to other users after the stamp).

Decision #6 (sprint-13-5-decisions.md): production REQUIRES sign-in;
anonymous stays dev-only behind ``AUTH_REQUIRED=false``.

No live Firebase, no live Mongo, no Gemini/Vertex calls — the Firebase
verify path is mocked via ``set_verify_hook`` and persistence is the
in-memory ``MockMCPClient``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from grace2_agent import auth as agent_auth
from grace2_agent.auth import (
    AUTH_CLOSE_CODE,
    AUTH_FAILED_ERROR_CODE,
    AUTH_REQUIRED_DEFAULT,
    AUTH_REQUIRED_ENV,
    MIGRATION_ANON_UID,
    auth_required,
)
from grace2_agent.auth_handshake import set_verify_hook
from grace2_agent.persistence import CASES_COLLECTION, Persistence
from grace2_contracts.case import CaseSummary
from grace2_contracts.common import new_ulid, now_utc


# --------------------------------------------------------------------------- #
# Mock MCP client (case + update-many aware)
# --------------------------------------------------------------------------- #


class MockMCPClient:
    """In-memory mock of the MongoDB MCP server surface used by this job.

    Honors ``insert-one`` / ``update-one`` / ``update-many`` / ``find`` /
    ``find-one`` with just enough filter semantics (equality, ``$or``,
    ``$exists``, ``$nin``) for the migration + listing under test.
    """

    def __init__(self) -> None:
        self._store: dict[str, dict[str, dict]] = {}
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name, arguments=None):  # noqa: D401
        args = dict(arguments or {})
        self.calls.append((name, args))
        coll = args.get("collection") or "_default"
        store = self._store.setdefault(coll, {})

        if name == "insert-one":
            doc = args["document"]
            store[doc["_id"]] = doc
            return {"insertedId": doc["_id"]}

        if name == "update-one":
            filt = args.get("filter", {})
            set_ = args.get("update", {}).get("$set", {})
            upsert = args.get("upsert", False)
            target_id = filt.get("_id")
            if target_id and target_id in store:
                store[target_id].update(set_)
            elif upsert and target_id:
                store[target_id] = {**set_, "_id": target_id}
            return {"matchedCount": 1, "modifiedCount": 1}

        if name == "update-many":
            filt = args.get("filter", {})
            set_ = args.get("update", {}).get("$set", {})
            modified = 0
            for doc in store.values():
                if self._matches(doc, filt):
                    doc.update(set_)
                    modified += 1
            return {"matchedCount": modified, "modifiedCount": modified}

        if name == "find-one":
            filt = args.get("filter", {})
            for doc in store.values():
                if self._matches(doc, filt):
                    return {"document": doc}
            return {"document": None}

        if name == "find":
            filt = args.get("filter", {})
            return {"documents": [d for d in store.values() if self._matches(d, filt)]}

        raise NotImplementedError(f"mock MCP: unknown tool {name!r}")

    @staticmethod
    def _matches(doc: dict, filt: dict) -> bool:
        for k, v in filt.items():
            if k == "$or":
                if not any(MockMCPClient._matches(doc, sub) for sub in v):
                    return False
                continue
            if isinstance(v, dict) and "$exists" in v:
                present = k in doc
                if v["$exists"] is False and present:
                    return False
                if v["$exists"] is True and not present:
                    return False
                continue
            if isinstance(v, dict) and "$nin" in v:
                if doc.get(k) in v["$nin"]:
                    return False
                continue
            if doc.get(k) != v:
                return False
        return True


class _FakeWS:
    """WS stand-in with ``send`` AND ``close`` (the reject path closes)."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed_with: tuple[int, str] | None = None

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed_with = (code, reason)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _restore_seams():
    """Restore the verify hook + clear AUTH_REQUIRED between tests."""
    yield
    set_verify_hook(None)


@pytest.fixture()
def _clear_env(monkeypatch):
    monkeypatch.delenv(AUTH_REQUIRED_ENV, raising=False)
    return monkeypatch


def _fresh_case(title: str = "case", owner: str | None = None) -> CaseSummary:
    return CaseSummary(
        case_id=new_ulid(),
        title=title,
        created_at=now_utc(),
        updated_at=now_utc(),
        status="active",
    )


# --------------------------------------------------------------------------- #
# 1. auth_required() env precedence
# --------------------------------------------------------------------------- #


def test_auth_required_default_is_false(_clear_env) -> None:
    """SHIPPED DEFAULT: AUTH_REQUIRED unset → gate OFF (false).

    Guards the DEFAULT-FLIP DECISION: the running dev agent has no env set
    and must keep its anonymous-fallback behavior on restart.
    """
    assert AUTH_REQUIRED_DEFAULT == "false"
    assert auth_required() is False


@pytest.mark.parametrize("raw", ["true", "TRUE", "1", "yes", "on", " True "])
def test_auth_required_truthy_tokens(_clear_env, raw) -> None:
    _clear_env.setenv(AUTH_REQUIRED_ENV, raw)
    assert auth_required() is True


@pytest.mark.parametrize("raw", ["false", "0", "", "no", "off", "garbage"])
def test_auth_required_falsy_tokens(_clear_env, raw) -> None:
    _clear_env.setenv(AUTH_REQUIRED_ENV, raw)
    assert auth_required() is False


def test_auth_required_read_at_call_time(_clear_env) -> None:
    """Env is read per-call (not import-time) so Cloud Run injection works."""
    assert auth_required() is False
    _clear_env.setenv(AUTH_REQUIRED_ENV, "true")
    assert auth_required() is True
    _clear_env.setenv(AUTH_REQUIRED_ENV, "false")
    assert auth_required() is False


# --------------------------------------------------------------------------- #
# 2. AUTH_REQUIRED=true rejects forged / absent tokens
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_forged_token_rejected_when_required(_clear_env) -> None:
    """AUTH_REQUIRED=true + forged token → 4401 close + AUTH_FAILED, no bind."""
    from grace2_agent.server import (
        SessionState,
        _handle_auth_token,
        set_persistence,
    )

    set_persistence(Persistence(MockMCPClient()))
    set_verify_hook(lambda token: None)  # forged/expired: verification fails
    _clear_env.setenv(AUTH_REQUIRED_ENV, "true")

    state = SessionState(session_id=new_ulid())
    ws = _FakeWS()
    ok = await _handle_auth_token(
        ws,  # type: ignore[arg-type]
        state,
        {"token": "forged.jwt.value", "anonymous": False},
    )

    # Caller must stop processing.
    assert ok is False
    # No anonymous bind happened on the required path.
    assert state.authenticated_user_id is None
    assert state.auth_handshake_complete is False
    # A.5: socket closed with 4401.
    assert ws.closed_with is not None
    assert ws.closed_with[0] == AUTH_CLOSE_CODE == 4401
    # A.6: an AUTH_FAILED error envelope reached the wire before the close.
    errors = [e for e in ws.sent if e["type"] == "error"]
    assert len(errors) == 1
    assert errors[0]["payload"]["error_code"] == AUTH_FAILED_ERROR_CODE == "AUTH_FAILED"
    # No auth-ack was emitted.
    assert not any(e["type"] == "auth-ack" for e in ws.sent)

    set_persistence(None)


@pytest.mark.asyncio
async def test_non_auth_first_envelope_rejected_when_required(_clear_env) -> None:
    """AUTH_REQUIRED=true + a client that never sends auth-token → rejected.

    The implicit-anonymous fallback (``_ensure_auth_handshake``) must NOT
    bind on the required path; it rejects with 4401 + AUTH_FAILED.
    """
    from grace2_agent.server import (
        SessionState,
        _ensure_auth_handshake,
        set_persistence,
    )

    set_persistence(Persistence(MockMCPClient()))
    _clear_env.setenv(AUTH_REQUIRED_ENV, "true")

    state = SessionState(session_id=new_ulid())
    ws = _FakeWS()
    ok = await _ensure_auth_handshake(ws, state)  # type: ignore[arg-type]

    assert ok is False
    assert state.auth_handshake_complete is False
    assert state.authenticated_user_id is None
    assert ws.closed_with is not None and ws.closed_with[0] == 4401
    errors = [e for e in ws.sent if e["type"] == "error"]
    assert errors and errors[0]["payload"]["error_code"] == "AUTH_FAILED"

    set_persistence(None)


@pytest.mark.asyncio
async def test_valid_token_accepted_when_required(_clear_env) -> None:
    """AUTH_REQUIRED=true + VALID token → binds + proceeds (no reject)."""
    from grace2_agent.server import (
        SessionState,
        _handle_auth_token,
        set_persistence,
    )

    set_persistence(Persistence(MockMCPClient()))
    fixed_uid = "fb-real-uid-required"
    set_verify_hook(lambda token: {"uid": fixed_uid, "email": "n@example.com"})
    _clear_env.setenv(AUTH_REQUIRED_ENV, "true")

    state = SessionState(session_id=new_ulid())
    ws = _FakeWS()
    ok = await _handle_auth_token(
        ws,  # type: ignore[arg-type]
        state,
        {"token": "eyJ.real.jwt", "anonymous": False},
    )

    assert ok is True
    assert state.is_anonymous is False
    assert state.firebase_uid == fixed_uid
    assert state.auth_handshake_complete is True
    assert ws.closed_with is None  # not rejected
    assert any(e["type"] == "auth-ack" for e in ws.sent)

    set_persistence(None)


# --------------------------------------------------------------------------- #
# 3. AUTH_REQUIRED=false preserves today's anonymous behavior EXACTLY
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_forged_token_falls_back_anonymous_when_not_required(_clear_env) -> None:
    """Gate OFF (default): a forged token still falls back to anonymous + binds."""
    from grace2_agent.server import (
        SessionState,
        _handle_auth_token,
        set_persistence,
    )

    set_persistence(Persistence(MockMCPClient()))
    set_verify_hook(lambda token: None)  # forged
    # AUTH_REQUIRED unset → default "false".

    state = SessionState(session_id=new_ulid())
    ws = _FakeWS()
    ok = await _handle_auth_token(
        ws,  # type: ignore[arg-type]
        state,
        {"token": "forged.jwt.value", "anonymous": False},
    )

    assert ok is True  # connection proceeds (no reject)
    assert state.is_anonymous is True  # anonymous fallback bound
    assert state.auth_handshake_complete is True
    assert ws.closed_with is None
    assert any(e["type"] == "auth-ack" for e in ws.sent)

    set_persistence(None)


@pytest.mark.asyncio
async def test_implicit_anonymous_fallback_when_not_required(_clear_env) -> None:
    """Gate OFF: a non-auth-token first envelope still trips anonymous bind."""
    from grace2_agent.server import (
        SessionState,
        _ensure_auth_handshake,
        set_persistence,
    )

    set_persistence(Persistence(MockMCPClient()))

    state = SessionState(session_id=new_ulid())
    ws = _FakeWS()
    ok = await _ensure_auth_handshake(ws, state)  # type: ignore[arg-type]

    assert ok is True
    assert state.is_anonymous is True
    assert state.auth_handshake_complete is True
    assert ws.closed_with is None
    assert any(e["type"] == "auth-ack" for e in ws.sent)

    set_persistence(None)


# --------------------------------------------------------------------------- #
# 4. Pre-Auth case migration: idempotent, non-corrupting, leak gone
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_migration_stamps_orphan_cases() -> None:
    """Cases lacking user_id are stamped with MIGRATION_ANON_UID; owned ones untouched."""
    mock = MockMCPClient()
    p = Persistence(mock)

    # Two orphan (pre-Auth) Cases — no user_id.
    orphan_a = _fresh_case("orphan A")
    orphan_b = _fresh_case("orphan B")
    await p.upsert_case(orphan_a)
    await p.upsert_case(orphan_b)
    # One already-owned Case — must NOT be touched.
    owned = _fresh_case("owned")
    real_uid = "fb-real-owner"
    await p.upsert_case(owned, owner_user_id=real_uid)

    modified = await p.migrate_preauth_cases(MIGRATION_ANON_UID)
    assert modified == 2

    store = mock._store[CASES_COLLECTION]
    assert store[orphan_a.case_id]["user_id"] == MIGRATION_ANON_UID
    assert store[orphan_b.case_id]["user_id"] == MIGRATION_ANON_UID
    # The already-owned Case keeps its real owner — NOT clobbered.
    assert store[owned.case_id]["user_id"] == real_uid


@pytest.mark.asyncio
async def test_migration_idempotent() -> None:
    """A second migration run matches nothing (every Case now has user_id)."""
    mock = MockMCPClient()
    p = Persistence(mock)
    await p.upsert_case(_fresh_case("orphan"))

    first = await p.migrate_preauth_cases(MIGRATION_ANON_UID)
    assert first == 1
    second = await p.migrate_preauth_cases(MIGRATION_ANON_UID)
    assert second == 0  # no-op


@pytest.mark.asyncio
async def test_migration_does_not_corrupt_other_collections() -> None:
    """The migration only ever writes the projects collection."""
    mock = MockMCPClient()
    p = Persistence(mock)
    await p.upsert_case(_fresh_case("orphan"))
    # Seed a doc in another collection that must remain untouched.
    await mock.call_tool(
        "insert-one",
        {"collection": "sessions", "document": {"_id": "s1", "data": "keep"}},
    )

    await p.migrate_preauth_cases(MIGRATION_ANON_UID)

    assert mock._store["sessions"]["s1"] == {"_id": "s1", "data": "keep"}
    # Every projects write was an update-many on the projects collection only.
    migration_writes = [
        a for (n, a) in mock.calls if n == "update-many"
    ]
    assert migration_writes, "expected an update-many call"
    for a in migration_writes:
        assert a["collection"] == CASES_COLLECTION


@pytest.mark.asyncio
async def test_migrated_case_visible_to_migration_owner_only() -> None:
    """After migration, an orphan Case is visible to MIGRATION_ANON_UID, not others.

    Proves the ``$exists:false`` leak clause is gone: an owner-less Case is
    invisible to an arbitrary user, and after the stamp it belongs to the
    synthetic migration owner.
    """
    mock = MockMCPClient()
    p = Persistence(mock)
    orphan = _fresh_case("orphan")
    await p.upsert_case(orphan)

    # Before migration: invisible to everyone (leak clause gone).
    assert await p.list_cases_for_user(new_ulid()) == []
    assert await p.list_cases_for_user(MIGRATION_ANON_UID) == []

    await p.migrate_preauth_cases(MIGRATION_ANON_UID)

    # After migration: visible to the migration owner, still not to others.
    listed = await p.list_cases_for_user(MIGRATION_ANON_UID)
    assert [c.case_id for c in listed] == [orphan.case_id]
    assert await p.list_cases_for_user(new_ulid()) == []


@pytest.mark.asyncio
async def test_server_run_migration_wrapper_best_effort() -> None:
    """``server._run_preauth_case_migration`` runs against the bound singleton
    and is a safe no-op when Persistence is unbound."""
    from grace2_agent.server import (
        _run_preauth_case_migration,
        set_persistence,
    )

    # Unbound: must not raise.
    set_persistence(None)
    await _run_preauth_case_migration()

    # Bound: stamps the orphan.
    mock = MockMCPClient()
    p = Persistence(mock)
    orphan = _fresh_case("orphan")
    await p.upsert_case(orphan)
    set_persistence(p)
    await _run_preauth_case_migration()
    assert mock._store[CASES_COLLECTION][orphan.case_id]["user_id"] == MIGRATION_ANON_UID

    set_persistence(None)


# --------------------------------------------------------------------------- #
# 5. Gate-ordering hygiene (job-0252b): the AUTH_REQUIRED-rejected path must
#    NOT provision/persist an ephemeral anonymous users row BEFORE the gate
#    rejects the socket — zero collection writes on the rejected path.
# --------------------------------------------------------------------------- #

from grace2_agent.persistence import USERS_COLLECTION  # noqa: E402

#: MCP tool verbs that mutate a collection (anything that is NOT a pure read).
_WRITE_VERBS = frozenset(
    {"insert-one", "insert-many", "update-one", "update-many", "delete-one", "delete-many"}
)


def _write_calls(mock: "MockMCPClient") -> list[tuple[str, dict]]:
    """Every call that mutates a collection (any non-read verb)."""
    return [(n, a) for (n, a) in mock.calls if n in _WRITE_VERBS]


def _users_calls(mock: "MockMCPClient") -> list[tuple[str, dict]]:
    """Every call (read OR write) that touches the ``users`` collection."""
    return [(n, a) for (n, a) in mock.calls if a.get("collection") == USERS_COLLECTION]


@pytest.mark.asyncio
async def test_gate_on_forged_token_writes_no_user_row(_clear_env) -> None:
    """AUTH_REQUIRED=true + forged token → 4401 AND zero users-collection writes.

    The minor live-verify finding from the job-0252 panel: ``authenticate_token``
    used to provision (and persist) an ephemeral anonymous ``UserDocument`` on
    every failure path BEFORE the server gate inspected ``is_anonymous`` and
    rejected — junk-row growth under hostile load. The rejection must now
    short-circuit BEFORE any provisioning: no write to ANY collection, and no
    ``users`` access at all on the rejected path.
    """
    from grace2_agent.server import (
        SessionState,
        _handle_auth_token,
        set_persistence,
    )

    mock = MockMCPClient()
    set_persistence(Persistence(mock))
    set_verify_hook(lambda token: None)  # forged/expired: verification fails
    _clear_env.setenv(AUTH_REQUIRED_ENV, "true")

    state = SessionState(session_id=new_ulid())
    ws = _FakeWS()
    ok = await _handle_auth_token(
        ws,  # type: ignore[arg-type]
        state,
        {"token": "forged.jwt.value", "anonymous": False},
    )

    # Rejection still happens (A.5 4401 + A.6 AUTH_FAILED), exactly as before.
    assert ok is False
    assert state.authenticated_user_id is None
    assert ws.closed_with is not None and ws.closed_with[0] == AUTH_CLOSE_CODE == 4401
    assert any(
        e["type"] == "error" and e["payload"]["error_code"] == AUTH_FAILED_ERROR_CODE
        for e in ws.sent
    )
    # The hygiene property: NO write touched any collection, and the users
    # collection was never accessed (no provisioning read OR write).
    assert _write_calls(mock) == [], f"unexpected collection writes: {_write_calls(mock)}"
    assert _users_calls(mock) == [], f"unexpected users access: {_users_calls(mock)}"
    # The users store stays empty (the panel's on-disk junk-row check).
    assert mock._store.get(USERS_COLLECTION, {}) == {}

    set_persistence(None)


@pytest.mark.asyncio
async def test_gate_on_no_token_envelope_writes_no_user_row(_clear_env) -> None:
    """AUTH_REQUIRED=true + non-auth first envelope → 4401 AND zero users writes.

    ``_ensure_auth_handshake`` already checks ``auth_required()`` before
    calling ``authenticate_token``, so this path never provisioned. This pins
    that property so a future refactor can't reintroduce the write.
    """
    from grace2_agent.server import (
        SessionState,
        _ensure_auth_handshake,
        set_persistence,
    )

    mock = MockMCPClient()
    set_persistence(Persistence(mock))
    _clear_env.setenv(AUTH_REQUIRED_ENV, "true")

    state = SessionState(session_id=new_ulid())
    ws = _FakeWS()
    ok = await _ensure_auth_handshake(ws, state)  # type: ignore[arg-type]

    assert ok is False
    assert state.authenticated_user_id is None
    assert ws.closed_with is not None and ws.closed_with[0] == 4401
    assert _write_calls(mock) == [], f"unexpected collection writes: {_write_calls(mock)}"
    assert _users_calls(mock) == [], f"unexpected users access: {_users_calls(mock)}"
    assert mock._store.get(USERS_COLLECTION, {}) == {}

    set_persistence(None)


@pytest.mark.asyncio
async def test_gate_on_forged_token_with_anon_hint_does_not_read_or_write(
    _clear_env,
) -> None:
    """AUTH_REQUIRED=true + empty token carrying an anonymous_user_id hint →
    rejected WITHOUT the sticky-reuse read or any write.

    Even the sticky-anonymous REUSE lookup (``get_user_by_id``, a users-
    collection ``find-one``) must be skipped on the gated path — the result is
    destined for rejection, so there is nothing to rebind.
    """
    from grace2_agent.server import (
        SessionState,
        _handle_auth_token,
        set_persistence,
    )

    mock = MockMCPClient()
    p = Persistence(mock)
    set_persistence(p)
    _clear_env.setenv(AUTH_REQUIRED_ENV, "true")

    # Seed a real anonymous user row that the hint WOULD reuse if read.
    from grace2_contracts.user import User

    seeded = User(
        user_id=new_ulid(),
        firebase_uid=None,
        email=None,
        display_name=None,
        created_at=now_utc(),
        is_active=True,
        prefs={},
        is_anonymous=True,
    )
    await p.upsert_user(seeded)
    calls_after_seed = len(mock.calls)

    state = SessionState(session_id=new_ulid())
    ws = _FakeWS()
    ok = await _handle_auth_token(
        ws,  # type: ignore[arg-type]
        state,
        {"token": "", "anonymous_user_id": seeded.user_id},
    )

    assert ok is False
    assert state.authenticated_user_id is None
    assert ws.closed_with is not None and ws.closed_with[0] == 4401
    # NOTHING happened on the MCP after the seed: no reuse read, no write.
    assert mock.calls[calls_after_seed:] == [], (
        f"unexpected MCP traffic on rejected path: {mock.calls[calls_after_seed:]}"
    )

    set_persistence(None)


@pytest.mark.asyncio
async def test_gate_off_forged_token_provisions_and_persists_user(_clear_env) -> None:
    """REGRESSION PIN — gate OFF: a forged token still provisions AND PERSISTS
    an anonymous users row, byte-identical to pre-job-0252b behavior.

    This is the property that protects the live demo agent (which has no
    AUTH_REQUIRED set): the gate-OFF path must keep writing the anonymous user.
    """
    from grace2_agent.server import (
        SessionState,
        _handle_auth_token,
        set_persistence,
    )

    mock = MockMCPClient()
    set_persistence(Persistence(mock))
    set_verify_hook(lambda token: None)  # forged
    # AUTH_REQUIRED unset → default "false".

    state = SessionState(session_id=new_ulid())
    ws = _FakeWS()
    ok = await _handle_auth_token(
        ws,  # type: ignore[arg-type]
        state,
        {"token": "forged.jwt.value", "anonymous": False},
    )

    assert ok is True  # proceeds
    assert state.is_anonymous is True
    assert state.authenticated_user_id is not None
    # The anonymous user WAS persisted to the users collection (the regression
    # the no-persist short-circuit must NOT touch when the gate is off).
    users_writes = [
        (n, a) for (n, a) in _write_calls(mock) if a.get("collection") == USERS_COLLECTION
    ]
    assert users_writes, "gate-off forged token must still persist an anonymous user"
    persisted = mock._store.get(USERS_COLLECTION, {})
    assert state.authenticated_user_id in persisted
    assert persisted[state.authenticated_user_id]["is_anonymous"] is True

    set_persistence(None)


@pytest.mark.asyncio
async def test_gate_on_valid_token_provisions_real_user(_clear_env) -> None:
    """AUTH_REQUIRED=true + VALID token → the REAL (non-anonymous) user is
    provisioned/bound and persisted — the gate short-circuit only suppresses
    the ANONYMOUS write, never the real-identity provision.
    """
    from grace2_agent.server import (
        SessionState,
        _handle_auth_token,
        set_persistence,
    )

    mock = MockMCPClient()
    set_persistence(Persistence(mock))
    fixed_uid = "fb-real-uid-0252b"
    set_verify_hook(lambda token: {"uid": fixed_uid, "email": "owner@example.com"})
    _clear_env.setenv(AUTH_REQUIRED_ENV, "true")

    state = SessionState(session_id=new_ulid())
    ws = _FakeWS()
    ok = await _handle_auth_token(
        ws,  # type: ignore[arg-type]
        state,
        {"token": "eyJ.real.jwt", "anonymous": False},
    )

    assert ok is True
    assert state.is_anonymous is False
    assert state.firebase_uid == fixed_uid
    assert ws.closed_with is None
    # A real user row was provisioned (first-login auto-provision) + persisted.
    persisted = mock._store.get(USERS_COLLECTION, {})
    assert state.authenticated_user_id in persisted
    assert persisted[state.authenticated_user_id]["firebase_uid"] == fixed_uid
    assert persisted[state.authenticated_user_id]["is_anonymous"] is False

    set_persistence(None)
