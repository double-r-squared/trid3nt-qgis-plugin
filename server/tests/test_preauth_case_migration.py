"""Pre-Auth case migration tests (job-0252, OQ-0115-CASE-USER-LINK).

Covers ``persistence.migrate_preauth_cases`` + the startup wrapper
``server._run_preauth_case_migration``:

- stamps every Case lacking a ``user_id`` with ``MIGRATION_ANON_UID``;
- is idempotent (a second run matches nothing);
- does not corrupt already-owned Cases or other collections;
- leaves the ``$exists:false`` leak clause gone (owner-less Cases are
  invisible to other users after the stamp).

(The cloud-only ``AUTH_REQUIRED`` sign-in gate this file used to cover was
removed with the cloud strip; the migration remains live on the file
backend, run once at every daemon startup.)

No live store -- persistence is the in-memory ``MockMCPClient``.
"""

from __future__ import annotations

import pytest

from grace2_agent.persistence import CASES_COLLECTION, Persistence
from grace2_agent.server import MIGRATION_ANON_UID
from grace2_contracts.case import CaseSummary
from grace2_contracts.common import new_ulid, now_utc


# --------------------------------------------------------------------------- #
# Mock MCP client (case + update-many aware)
# --------------------------------------------------------------------------- #


class MockMCPClient:
    """In-memory mock of the MCP persistence surface used by the migration.

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


def _fresh_case(title: str = "case") -> CaseSummary:
    return CaseSummary(
        case_id=new_ulid(),
        title=title,
        created_at=now_utc(),
        updated_at=now_utc(),
        status="active",
    )


# --------------------------------------------------------------------------- #
# Pre-Auth case migration: idempotent, non-corrupting, leak gone
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_migration_stamps_orphan_cases() -> None:
    """Cases lacking user_id are stamped with MIGRATION_ANON_UID; owned ones untouched."""
    mock = MockMCPClient()
    p = Persistence(mock)

    # Two orphan (pre-Auth) Cases -- no user_id.
    orphan_a = _fresh_case("orphan A")
    orphan_b = _fresh_case("orphan B")
    await p.upsert_case(orphan_a)
    await p.upsert_case(orphan_b)
    # One already-owned Case -- must NOT be touched.
    owned = _fresh_case("owned")
    real_uid = "real-owner-uid"
    await p.upsert_case(owned, owner_user_id=real_uid)

    modified = await p.migrate_preauth_cases(MIGRATION_ANON_UID)
    assert modified == 2

    store = mock._store[CASES_COLLECTION]
    assert store[orphan_a.case_id]["user_id"] == MIGRATION_ANON_UID
    assert store[orphan_b.case_id]["user_id"] == MIGRATION_ANON_UID
    # The already-owned Case keeps its real owner -- NOT clobbered.
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
