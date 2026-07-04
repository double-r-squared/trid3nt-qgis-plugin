"""DynamoDB-backed Persistence tests (sprint-14-aws).

The AWS migration adds ``DynamoMCPClient`` (``dynamo_backend.py``) as a third
backend behind the single ``persistence.MCPClientProtocol`` seam, selected by
the env ``GRACE2_PERSISTENCE_BACKEND`` (``file`` default, ``dynamodb`` opt-in).
These tests exercise that backend through the UNMODIFIED ``Persistence``
wrapper to prove behavioral parity with ``FileMCPClient`` / the live Mongo
path.

No live AWS is touched. ``moto`` is the preferred mock (listed in the job's
``dependencies`` field) but is NOT installed in this env, so these tests run
against a thin in-memory fake (``_FakeDynamoResource``) that implements the
subset of the boto3 resource API ``DynamoMCPClient`` uses — ``Table()`` +
``put_item`` / ``get_item`` / ``query`` (KeyConditionExpression + IndexName) /
``scan`` (with pagination keys). The fake is intentionally minimal but
faithful: it serializes through the SAME float<->Decimal marshaling the real
resource API enforces, so the marshaling code is genuinely exercised.

Coverage:
- ``test_create_and_get_case`` — Case upsert + get round-trips.
- ``test_owner_scoping_non_owner_cannot_read`` — a Case owned by user A is NOT
  returned in user B's ``list_cases_for_user`` (GSI + $or owner scoping).
- ``test_session_state_round_trip`` — append_chat_message + get_session_state
  rehydrates ordered chat history.
- ``test_chart_append_and_ordered_rehydration`` — chart ``$push`` onto the
  sessions doc + get_session_state replays payloads in emitted_at order,
  unwrapping ``.payload`` (matches the file/Mongo behavior exactly).
- ``test_float_marshaling_round_trip`` — bbox floats + Vega-Lite spec floats
  survive the Decimal round-trip and re-validate.
- ``test_migrate_preauth_cases_update_many`` — update-many with $exists:false.
- ``test_user_firebase_uid_gsi`` — get_user_by_firebase_uid via GSI Query.
- ``test_secrets_is_active_filter`` — list_secrets_refs filters is_active.
- ``test_backend_selection_by_env`` — resolve_persistence_backend +
  make_persistence_for_backend honor GRACE2_PERSISTENCE_BACKEND (default file).
"""

from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from grace2_agent import dynamo_backend
from grace2_agent.dynamo_backend import (
    DynamoMCPClient,
    _alias_for,
    _from_ddb,
    _to_ddb,
    make_dynamo_persistence,
)
from grace2_agent.persistence import (
    PERSISTENCE_BACKEND_ENV,
    Persistence,
    make_persistence_for_backend,
    resolve_persistence_backend,
)
from grace2_contracts.case import CaseChatMessage, CaseSummary
from grace2_contracts.chart_contracts import ChartEmissionPayload, SessionChartRecord
from grace2_contracts.common import new_ulid, now_utc
from grace2_contracts.secrets import SecretRecord
from grace2_contracts.user import User


# --------------------------------------------------------------------------- #
# In-memory fake of the boto3 DynamoDB resource API
# --------------------------------------------------------------------------- #


class _FakeCondition:
    """Captures ``Key(attr).eq(value)`` for the fake's KeyConditionExpression."""

    def __init__(self, attr: str, value: Any) -> None:
        self.attr = attr
        self.value = value


class _FakeKey:
    def __init__(self, attr: str) -> None:
        self._attr = attr

    def eq(self, value: Any) -> _FakeCondition:
        return _FakeCondition(self._attr, value)


def _enforce_no_native_float(item: Any) -> None:
    """The real resource API rejects native floats; assert the fake does too.

    Walks the item and raises if a ``float`` slipped through ``_to_ddb`` — this
    is what makes the float-marshaling test meaningful (a real moto/boto3 path
    would raise here too).
    """
    if isinstance(item, float):
        raise TypeError("Float types are not supported. Use Decimal types instead.")
    if isinstance(item, dict):
        for v in item.values():
            _enforce_no_native_float(v)
    elif isinstance(item, (list, tuple, set, frozenset)):
        for v in item:
            _enforce_no_native_float(v)


class _FakeTable:
    """Minimal in-memory DynamoDB table.

    Key schema is derived from the table NAME suffix (alias) via the same
    ``dynamo_backend`` maps the client uses, so PK/SK and GSIs line up with
    what the client expects. Items are stored keyed by their primary key
    tuple. Stored values go through the same Decimal marshaling the resource
    API enforces, so reads return Decimals (the client's ``_from_ddb`` converts
    them back).
    """

    def __init__(self, name: str, prefix: str) -> None:
        self.name = name
        alias = name[len(prefix):] if name.startswith(prefix) else name
        self._alias = alias
        self._pk = dynamo_backend._pk_attr(alias)
        self._sk = dynamo_backend._sk_attr(alias)
        self._gsis = dynamo_backend._TABLE_GSIS.get(alias, {})
        self._items: dict[tuple, dict] = {}

    def _key_tuple(self, item: dict) -> tuple:
        if self._sk is not None:
            return (item[self._pk], item[self._sk])
        return (item[self._pk],)

    # --- write -------------------------------------------------------- #

    def put_item(self, *, Item: dict) -> dict:
        _enforce_no_native_float(Item)
        # Store a deep copy so the client can't mutate our store by reference.
        self._items[self._key_tuple(Item)] = copy.deepcopy(Item)
        return {}

    # --- read --------------------------------------------------------- #

    def get_item(self, *, Key: dict) -> dict:
        if self._sk is not None:
            k = (Key[self._pk], Key[self._sk])
        else:
            k = (Key[self._pk],)
        item = self._items.get(k)
        if item is None:
            return {}
        return {"Item": copy.deepcopy(item)}

    def query(
        self,
        *,
        KeyConditionExpression: _FakeCondition,
        IndexName: str | None = None,
        ExclusiveStartKey: Any = None,
    ) -> dict:
        cond = KeyConditionExpression
        attr = cond.attr
        val = cond.value
        out = [
            copy.deepcopy(it)
            for it in self._items.values()
            if it.get(attr) == val
        ]
        # No pagination split needed at test volumes; single page.
        return {"Items": out}

    def scan(self, *, ExclusiveStartKey: Any = None) -> dict:
        return {"Items": [copy.deepcopy(it) for it in self._items.values()]}


class _FakeDynamoResource:
    def __init__(self, prefix: str) -> None:
        self._prefix = prefix
        self._tables: dict[str, _FakeTable] = {}

    def Table(self, name: str) -> _FakeTable:  # noqa: N802 — boto3 API casing
        tbl = self._tables.get(name)
        if tbl is None:
            tbl = _FakeTable(name, self._prefix)
            self._tables[name] = tbl
        return tbl


@pytest.fixture(autouse=True)
def _patch_boto3_key(monkeypatch):
    """Point ``boto3.dynamodb.conditions.Key`` at the fake for query paths."""
    import boto3.dynamodb.conditions as conditions

    monkeypatch.setattr(conditions, "Key", _FakeKey)
    yield


def _new_persistence(prefix: str = "grace2_") -> tuple[Persistence, _FakeDynamoResource]:
    res = _FakeDynamoResource(prefix)
    p = make_dynamo_persistence(table_prefix=prefix, resource=res)
    return p, res


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _fresh_case(title: str = "Hurricane Ian — Fort Myers flood scenario") -> CaseSummary:
    return CaseSummary(
        case_id=new_ulid(),
        title=title,
        created_at=datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc),
        status="active",
        bbox=(-82.0, 26.5, -81.8, 26.7),
        primary_hazard="flood",
        layer_summary=["nlcd-fort-myers", "flood-depth-01HX"],
    )


def _fresh_chat(case_id: str, role: str, content: str, *, when: datetime) -> CaseChatMessage:
    return CaseChatMessage(
        message_id=new_ulid(),
        case_id=case_id,
        role=role,
        content=content,
        created_at=when,
    )


def _chart_payload(title: str) -> ChartEmissionPayload:
    return ChartEmissionPayload(
        chart_id=new_ulid(),
        title=title,
        vega_lite_spec={
            "mark": "bar",
            "encoding": {"x": {"field": "depth"}, "y": {"field": "count"}},
            "data": {"values": [{"depth": 0.5, "count": 12}, {"depth": 1.25, "count": 7}]},
        },
        created_turn_id="turn-1",
    )


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_create_and_get_case():
    p, _ = _new_persistence()
    case = _fresh_case()

    async def run():
        await p.upsert_case(case, owner_user_id="user-A")
        got = await p.get_case(case.case_id)
        return got

    got = asyncio.run(run())
    assert got is not None
    assert got.case_id == case.case_id
    assert got.title == case.title
    assert got.bbox == case.bbox  # floats survived the Decimal round-trip
    assert got.primary_hazard == "flood"


def test_owner_scoping_non_owner_cannot_read():
    p, _ = _new_persistence()
    case_a = _fresh_case("User A's private flood case")
    case_b = _fresh_case("User B's habitat case")

    async def run():
        await p.upsert_case(case_a, owner_user_id="user-A")
        await p.upsert_case(case_b, owner_user_id="user-B")
        a_list = await p.list_cases_for_user("user-A")
        b_list = await p.list_cases_for_user("user-B")
        return a_list, b_list

    a_list, b_list = asyncio.run(run())
    a_ids = {c.case_id for c in a_list}
    b_ids = {c.case_id for c in b_list}
    assert case_a.case_id in a_ids
    assert case_a.case_id not in b_ids  # non-owner cannot read A's case
    assert case_b.case_id in b_ids
    assert case_b.case_id not in a_ids


def test_owner_scoping_via_owner_user_id_or_branch():
    """A Case stamped only with owner_user_id (legacy shape) lists for the owner
    via the $or:[{user_id},{owner_user_id}] GSI-union path."""
    p, res = _new_persistence()
    case = _fresh_case()

    async def run():
        # Write directly with owner_user_id set instead of user_id to exercise
        # the second $or branch / second GSI.
        body = case.model_dump(mode="json")
        body["_id"] = case.case_id
        body["owner_user_id"] = "user-C"
        await p._mcp.call_tool(
            "update-one",
            {
                "database": "x",
                "collection": "projects",
                "filter": {"_id": case.case_id},
                "update": {"$set": body},
                "upsert": True,
            },
        )
        return await p.list_cases_for_user("user-C")

    listed = asyncio.run(run())
    assert {c.case_id for c in listed} == {case.case_id}


def test_archived_and_deleted_cases_excluded():
    p, _ = _new_persistence()
    live = _fresh_case("Live case")
    arch = _fresh_case("Archived case")

    async def run():
        await p.upsert_case(live, owner_user_id="user-D")
        await p.upsert_case(arch, owner_user_id="user-D")
        await p.archive_case(arch.case_id)
        return await p.list_cases_for_user("user-D")

    listed = asyncio.run(run())
    ids = {c.case_id for c in listed}
    assert live.case_id in ids
    assert arch.case_id not in ids  # $nin status filter + Python guard


def test_session_state_round_trip():
    p, _ = _new_persistence()
    case = _fresh_case()
    t0 = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)
    m1 = _fresh_chat(case.case_id, "user", "model the flood", when=t0)
    m2 = _fresh_chat(case.case_id, "agent", "fetching DEM", when=t0 + timedelta(seconds=5))
    m3 = _fresh_chat(case.case_id, "user", "now habitat", when=t0 + timedelta(seconds=10))

    async def run():
        await p.upsert_case(case, owner_user_id="user-A")
        # Append out of order to prove the rehydration sort works.
        await p.append_chat_message(m2)
        await p.append_chat_message(m1)
        await p.append_chat_message(m3)
        return await p.get_session_state(case.case_id)

    state = asyncio.run(run())
    assert state.case.case_id == case.case_id
    contents = [m.content for m in state.chat_history]
    assert contents == ["model the flood", "fetching DEM", "now habitat"]


def test_upsert_chat_message_walks_running_to_terminal_in_place():
    """Durable-card lifecycle on the LIVE backend (Dynamo): a SOLVE card
    persisted ``running`` at mint, then UPSERTED to ``complete`` by the SAME
    stable ``message_id``, yields EXACTLY ONE row carrying the terminal state +
    its original ``created_at`` position (no duplicate running+complete cards)."""
    from grace2_contracts.case import ToolCardRecord

    p, _ = _new_persistence()
    case = _fresh_case()
    t0 = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)
    card_id = new_ulid()

    def _row(state: str, when: datetime) -> CaseChatMessage:
        rec = ToolCardRecord(
            tool_name="sfincs:solve", state=state, label="sfincs solve"  # type: ignore[arg-type]
        )
        return CaseChatMessage(
            message_id=card_id,
            case_id=case.case_id,
            role="tool",
            content=rec.model_dump_json(),
            tool_card=rec,
            created_at=when,
        )

    async def run():
        await p.upsert_case(case, owner_user_id="user-A")
        # Mint: persist the running card.
        await p.upsert_chat_message(_row("running", t0))
        # Terminal: a LATER timestamp must NOT reorder the row (created_at pins on
        # first insert via $setOnInsert).
        await p.upsert_chat_message(_row("complete", t0 + timedelta(seconds=90)))
        return await p.get_session_state(case.case_id)

    state = asyncio.run(run())
    tool_rows = [m for m in state.chat_history if m.role == "tool"]
    assert len(tool_rows) == 1, "running -> terminal upserts ONE row, no duplicate"
    assert tool_rows[0].tool_card.state == "complete"
    assert tool_rows[0].created_at == t0, "created_at pins on first insert"


def test_chart_append_and_ordered_rehydration():
    """charts $push onto the sessions doc, then get_session_state replays
    payloads in emitted_at order, unwrapping .payload — matches file/Mongo."""
    p, _ = _new_persistence()
    case = _fresh_case()
    c_late = _chart_payload("Damage distribution")
    c_early = _chart_payload("Population exposure")

    async def run():
        await p.upsert_case(case, owner_user_id="user-A")
        # Emulate server.py's chart-persist site: $push SessionChartRecords onto
        # the sessions doc keyed by case_id. Push the LATER chart first to prove
        # the emitted_at sort (not insertion order) drives replay.
        rec_late = SessionChartRecord(
            session_id=case.case_id,
            payload=c_late,
            emitted_at=now_utc() + timedelta(seconds=30),
        )
        rec_early = SessionChartRecord(
            session_id=case.case_id,
            payload=c_early,
            emitted_at=now_utc(),
        )
        for rec in (rec_late, rec_early):
            await p._mcp.call_tool(
                "update-one",
                {
                    "database": "x",
                    "collection": "sessions",
                    "filter": {"_id": case.case_id},
                    "update": {"$push": {"charts": rec.model_dump(mode="json")}},
                    "upsert": True,
                },
            )
        return await p.get_session_state(case.case_id)

    state = asyncio.run(run())
    assert len(state.charts) == 2
    # Replayed in emitted_at order: early first, late second.
    assert state.charts[0]["title"] == "Population exposure"
    assert state.charts[1]["title"] == "Damage distribution"
    # Each replayed item is the unwrapped ChartEmissionPayload (not the record).
    assert state.charts[0]["envelope_type"] == "chart-emission"
    assert "vega_lite_spec" in state.charts[0]
    # Re-validate to prove the float-bearing spec round-tripped intact.
    ChartEmissionPayload.model_validate(state.charts[0])


def test_float_marshaling_round_trip():
    """bbox + Vega-Lite floats survive the Decimal coercion and re-validate."""
    # Unit-level: the marshaling helpers are inverse on a nested float payload.
    payload = {
        "_id": "x",
        "bbox": [-82.0, 26.5, -81.8, 26.7],
        "nested": {"vals": [0.5, 1.25, 3], "flag": True, "name": "", "n": None},
    }
    ddb = _to_ddb(payload)
    _enforce_no_native_float(ddb)  # no native floats remain
    # Decimals on the way out.
    assert isinstance(ddb["bbox"][0], Decimal)
    back = _from_ddb(ddb)
    assert back["bbox"] == [-82.0, 26.5, -81.8, 26.7]
    assert back["nested"]["vals"] == [0.5, 1.25, 3]
    assert back["nested"]["vals"][2] == 3 and isinstance(back["nested"]["vals"][2], int)
    assert back["nested"]["flag"] is True
    assert back["nested"]["name"] == ""
    assert back["nested"]["n"] is None


def test_migrate_preauth_cases_update_many():
    p, _ = _new_persistence()
    orphan1 = _fresh_case("Pre-auth case 1")
    orphan2 = _fresh_case("Pre-auth case 2")
    owned = _fresh_case("Already owned")

    async def run():
        # Two orphans (no user_id) + one owned.
        await p.upsert_case(orphan1)  # no owner_user_id -> no user_id stamp
        await p.upsert_case(orphan2)
        await p.upsert_case(owned, owner_user_id="real-user")
        modified = await p.migrate_preauth_cases("anon-sentinel")
        # The two orphans now belong to the sentinel.
        anon_list = await p.list_cases_for_user("anon-sentinel")
        # Re-run is idempotent (no orphans left).
        modified2 = await p.migrate_preauth_cases("anon-sentinel")
        return modified, {c.case_id for c in anon_list}, modified2

    modified, anon_ids, modified2 = asyncio.run(run())
    assert modified == 2
    assert anon_ids == {orphan1.case_id, orphan2.case_id}
    assert modified2 == 0  # idempotent


def test_user_firebase_uid_gsi():
    p, _ = _new_persistence()
    user = User(
        user_id=new_ulid(),
        firebase_uid="fb-uid-123",
        display_name="Ada",
        created_at=now_utc(),
    )

    async def run():
        await p.upsert_user(user)
        return await p.get_user_by_firebase_uid("fb-uid-123")

    got = asyncio.run(run())
    assert got is not None
    assert got.user_id == user.user_id
    assert got.firebase_uid == "fb-uid-123"


def test_secrets_is_active_filter():
    p, _ = _new_persistence()
    sec = SecretRecord(
        secret_id=new_ulid(),
        provider="ebird",
        vault_ref="grace2/ebird/key-1",
        is_active=True,
        added_at=now_utc(),
    )

    async def run():
        # Stamp owner via update-one $set (mirrors secrets_handler flow).
        await p.upsert_secret_ref(sec)
        await p._mcp.call_tool(
            "update-one",
            {
                "database": "x",
                "collection": "secrets",
                "filter": {"_id": sec.secret_id},
                "update": {"$set": {"user_id": "user-S"}},
            },
        )
        active = await p.list_secrets_refs("user-S")
        # Revoke -> excluded from the active listing.
        await p.revoke_secret(sec.secret_id)
        after = await p.list_secrets_refs("user-S")
        return active, after

    active, after = asyncio.run(run())
    assert {s.secret_id for s in active} == {sec.secret_id}
    assert after == []  # is_active=False excluded


def test_audit_insert_only():
    p, res = _new_persistence()

    async def run():
        await p.append_audit("catalog_amendment", {"tool": "fetch_gbif", "by": "user-A"})
        # Read back via the raw find surface.
        out = await p._mcp.call_tool(
            "find", {"database": "x", "collection": "audit_log", "filter": {}}
        )
        return out

    out = asyncio.run(run())
    docs = out["documents"]
    assert len(docs) == 1
    assert docs[0]["event_type"] == "catalog_amendment"
    assert docs[0]["payload"]["tool"] == "fetch_gbif"


def test_find_sort_and_limit():
    """The telemetry-dashboard read shape: find {} sort -1 limit N."""
    p, _ = _new_persistence()

    async def run():
        for i in range(5):
            await p._mcp.call_tool(
                "insert-one",
                {
                    "database": "x",
                    "collection": "tool_call_telemetry",
                    "document": {"_id": f"t{i}", "called_at_utc": f"2026-06-0{i+1}", "tool": "x"},
                },
            )
        return await p._mcp.call_tool(
            "find",
            {
                "database": "x",
                "collection": "tool_call_telemetry",
                "filter": {},
                "sort": {"called_at_utc": -1},
                "limit": 3,
            },
        )

    out = asyncio.run(run())
    docs = out["documents"]
    assert [d["_id"] for d in docs] == ["t4", "t3", "t2"]  # newest-first, capped at 3


def test_alias_mapping():
    assert _alias_for("projects") == "cases"
    assert _alias_for("case_chat_messages") == "chat"
    assert _alias_for("sessions") == "sessions"
    assert _alias_for("users") == "users"
    assert _alias_for("secrets") == "secrets"
    assert _alias_for("audit_log") == "audit"
    assert _alias_for("tool_call_telemetry") == "telemetry"
    # Unmapped collection falls back to a sanitized name (no crash).
    assert _alias_for("brand_new_collection") == "brand_new_collection"


# --------------------------------------------------------------------------- #
# Backend selection
# --------------------------------------------------------------------------- #


def test_backend_selection_by_env(monkeypatch):
    # Default (unset) -> file.
    monkeypatch.delenv(PERSISTENCE_BACKEND_ENV, raising=False)
    assert resolve_persistence_backend() == "file"

    # Explicit file.
    monkeypatch.setenv(PERSISTENCE_BACKEND_ENV, "file")
    assert resolve_persistence_backend() == "file"

    # Garbage -> still file (default-safe).
    monkeypatch.setenv(PERSISTENCE_BACKEND_ENV, "postgres")
    assert resolve_persistence_backend() == "file"

    # dynamodb (case-insensitive, whitespace-tolerant).
    monkeypatch.setenv(PERSISTENCE_BACKEND_ENV, "  DynamoDB ")
    assert resolve_persistence_backend() == "dynamodb"


def test_make_persistence_for_backend_default_is_file(monkeypatch, tmp_path):
    monkeypatch.delenv(PERSISTENCE_BACKEND_ENV, raising=False)
    monkeypatch.setenv("GRACE2_DEV_PERSISTENCE_DIR", str(tmp_path))
    p = make_persistence_for_backend()
    # Default backend is the file shim — does NOT touch boto3/DynamoDB.
    from grace2_agent.persistence import FileMCPClient

    assert isinstance(p._mcp, FileMCPClient)


def test_make_persistence_for_backend_dynamodb_builds_dynamo(monkeypatch):
    """When env=dynamodb, the factory builds a DynamoMCPClient. We inject a fake
    boto3 resource via monkeypatching the resource constructor so no live AWS
    call is made."""
    monkeypatch.setenv(PERSISTENCE_BACKEND_ENV, "dynamodb")

    captured = {}

    # A1 FIX 2: the production resource is now built with a bounded botocore
    # Config (connect_timeout/read_timeout/retries) so a stalled DynamoDB call
    # can't freeze the WS loop. The fake must accept the new ``config`` kwarg
    # (and we assert the bounded values are passed through).
    def _fake_resource(service, region_name=None, config=None):
        captured["service"] = service
        captured["region"] = region_name
        captured["config"] = config
        return _FakeDynamoResource(dynamo_backend.DEFAULT_TABLE_PREFIX)

    import boto3

    monkeypatch.setattr(boto3, "resource", _fake_resource)
    p = make_persistence_for_backend()
    assert isinstance(p._mcp, DynamoMCPClient)
    assert captured["service"] == "dynamodb"
    # Region resolves via the AWS_REGION/AWS_DEFAULT_REGION/us-west-2 chain.
    import os

    expected_region = (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-west-2"
    )
    assert captured["region"] == expected_region
    # A1 FIX 2: bounded timeouts + retries are applied.
    cfg = captured["config"]
    assert cfg is not None
    assert cfg.connect_timeout == 2
    assert cfg.read_timeout == 3
    assert cfg.retries == {"max_attempts": 2, "mode": "standard"}


def test_ddb_item_strips_none_gsi_key_preserves_other_none():
    """job-0296: an anonymous user has firebase_uid=None (a GSI key on 'users').
    DynamoDB rejects a NULL-typed GSI key attribute, so _ddb_item must omit it,
    while preserving non-key None values for $exists parity with the file backend."""
    from grace2_agent.dynamo_backend import _ddb_item

    item = _ddb_item(
        {"_id": "u1", "firebase_uid": None, "tier": "anon", "display_name": None},
        "users",
    )
    assert "firebase_uid" not in item       # GSI key None -> stripped
    assert item["_id"] == "u1"
    assert item["display_name"] is None      # non-key None -> preserved (parity)
    # cases: null user_id / owner_user_id GSI keys also stripped
    case_item = _ddb_item({"_id": "c1", "user_id": None, "owner_user_id": None}, "cases")
    assert "user_id" not in case_item and "owner_user_id" not in case_item
