"""Lane A1 — agent transport keystone (non-blocking Dynamo + gate-replay + C1/C2).

Covers the four A1 fixes that keep the agent's asyncio WS loop responsive and
the tool-card stream durable across reconnects:

  * FIX 1 — NON-BLOCKING DYNAMO: every boto3 call in ``DynamoMCPClient`` runs
    in a worker thread (``asyncio.to_thread``) so it never freezes the loop;
    the per-``_id`` ``asyncio.Lock`` keeps a concurrent read-modify-write
    (``$push``/``$addToSet`` onto the same doc) from clobbering.
  * FIX 4 — GATE REPLAY: ``SessionState.did_fresh_resume`` distinguishes a
    genuine fresh-socket resume (replay the active Case's layers — job-0356)
    from the 25s keepalive ping (skip the layer replay; the BLINK).
  * C1 — tool-card IO persistence (the rehydration fix): ``_persist_tool_card``
    populates the live ``ToolIoPayload`` field names (``raw_args`` /
    ``function_response`` / the truncation metadata) on the TYPED
    ``ToolCardRecord`` — the integration path W2 reads off ``m.tool_card`` on
    ``get_session_state`` replay — so a Case reopen rehydrates the expander. The
    ``content`` JSON twin mirrors the same values for non-contract consumers.
  * C2 — turn-complete/idle signal: ``_emit_turn_complete`` emits the
    W2-pinned ``turn-complete`` envelope shape.

Named ``test_a1_*`` so it never collides with lane A2's bedrock-adapter tests.
The boto3-resource fake mirrors ``test_dynamo_persistence.py`` (the convention
in this suite) so the async conversion is exercised against the same surface.
"""

from __future__ import annotations

import asyncio
import copy
import json
import time
from typing import Any

import pytest

from grace2_agent import dynamo_backend
from grace2_agent.dynamo_backend import DynamoMCPClient, make_dynamo_persistence


# --------------------------------------------------------------------------- #
# In-memory boto3-resource fake (mirrors test_dynamo_persistence.py)
# --------------------------------------------------------------------------- #


class _FakeCondition:
    def __init__(self, attr: str, value: Any) -> None:
        self.attr = attr
        self.value = value


class _FakeKey:
    def __init__(self, attr: str) -> None:
        self._attr = attr

    def eq(self, value: Any) -> _FakeCondition:
        return _FakeCondition(self._attr, value)


class _FakeTable:
    def __init__(self, name: str, prefix: str, *, put_delay: float = 0.0) -> None:
        alias = name[len(prefix):] if name.startswith(prefix) else name
        self._alias = alias
        self._pk = dynamo_backend._pk_attr(alias)
        self._sk = dynamo_backend._sk_attr(alias)
        self._items: dict[tuple, dict] = {}
        #: optional per-call sleep so the test can prove the call runs OFF the
        #: event loop (a blocking sleep inside ``to_thread`` must not stall the
        #: loop's other coroutines).
        self._put_delay = put_delay
        self.thread_names: list[str] = []

    def _key_tuple(self, item: dict) -> tuple:
        if self._sk is not None:
            return (item[self._pk], item[self._sk])
        return (item[self._pk],)

    def put_item(self, *, Item: dict) -> dict:
        import threading

        self.thread_names.append(threading.current_thread().name)
        if self._put_delay:
            time.sleep(self._put_delay)
        self._items[self._key_tuple(Item)] = copy.deepcopy(Item)
        return {}

    def get_item(self, *, Key: dict) -> dict:
        if self._sk is not None:
            k = (Key[self._pk], Key[self._sk])
        else:
            k = (Key[self._pk],)
        item = self._items.get(k)
        return {"Item": copy.deepcopy(item)} if item is not None else {}

    def query(self, *, KeyConditionExpression, IndexName=None, ExclusiveStartKey=None):
        attr, val = KeyConditionExpression.attr, KeyConditionExpression.value
        return {"Items": [copy.deepcopy(it) for it in self._items.values() if it.get(attr) == val]}

    def scan(self, *, ExclusiveStartKey=None):
        return {"Items": [copy.deepcopy(it) for it in self._items.values()]}


class _FakeResource:
    def __init__(self, prefix: str, *, put_delay: float = 0.0) -> None:
        self._prefix = prefix
        self._put_delay = put_delay
        self._tables: dict[str, _FakeTable] = {}

    def Table(self, name: str) -> _FakeTable:  # noqa: N802 — boto3 API casing
        tbl = self._tables.get(name)
        if tbl is None:
            tbl = _FakeTable(name, self._prefix, put_delay=self._put_delay)
            self._tables[name] = tbl
        return tbl


@pytest.fixture(autouse=True)
def _patch_boto3_key(monkeypatch):
    import boto3.dynamodb.conditions as conditions

    monkeypatch.setattr(conditions, "Key", _FakeKey)
    yield


# --------------------------------------------------------------------------- #
# FIX 1 — non-blocking Dynamo + per-_id lock atomicity
# --------------------------------------------------------------------------- #


def test_a1_dynamo_calls_run_off_event_loop():
    """A blocking put_item inside ``to_thread`` must NOT run on the loop thread.

    Proof the WS loop is never frozen by a Dynamo call: the put runs on a
    worker thread (non-MainThread), and a concurrent coroutine keeps ticking
    while the (artificially slowed) put is in flight.
    """
    res = _FakeResource("t_", put_delay=0.15)
    c = DynamoMCPClient(table_prefix="t_", resource=res)

    async def run() -> tuple[list[str], int]:
        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            for _ in range(20):
                await asyncio.sleep(0.005)
                ticks += 1

        t = asyncio.create_task(ticker())
        await c.call_tool(
            "insert-one", {"collection": "sessions", "document": {"_id": "S", "x": 1}}
        )
        await t
        tbl = res.Table("t_sessions")
        return tbl.thread_names, ticks

    thread_names, ticks = asyncio.run(run())
    assert thread_names, "put_item never ran"
    assert all(n != "MainThread" for n in thread_names), thread_names
    # The loop kept ticking while the 150ms put was in flight.
    assert ticks >= 10, ticks


def test_a1_concurrent_push_is_atomic_under_lock():
    """20 concurrent ``$push`` onto the SAME _id must all land (no lost write).

    Without the per-``_id`` lock, the read-modify-write windows interleave
    across worker threads and last-writer-wins drops mutations — exactly the
    session ``charts`` / ``project_ids`` accumulator corruption FIX 1 guards.
    """
    res = _FakeResource("t_")
    c = DynamoMCPClient(table_prefix="t_", resource=res)

    async def run() -> list[int]:
        await c.call_tool(
            "insert-one",
            {"collection": "sessions", "document": {"_id": "S1", "charts": []}},
        )

        async def push(n: int):
            return await c.call_tool(
                "update-one",
                {
                    "collection": "sessions",
                    "filter": {"_id": "S1"},
                    "update": {"$push": {"charts": n}},
                },
            )

        await asyncio.gather(*[push(i) for i in range(20)])
        doc = (
            await c.call_tool(
                "find-one", {"collection": "sessions", "filter": {"_id": "S1"}}
            )
        )["document"]
        return sorted(doc["charts"])

    assert asyncio.run(run()) == list(range(20))


def test_a1_dynamo_round_trip_unaffected():
    """insert / find / update-upsert / scan-fallback still round-trip identically."""
    p = make_dynamo_persistence(table_prefix="t_", resource=_FakeResource("t_"))

    async def run():
        # upsert + setOnInsert
        r = await p._mcp.call_tool(
            "update-one",
            {
                "collection": "users",
                "filter": {"_id": "U1"},
                "update": {"$set": {"name": "a"}, "$setOnInsert": {"created": 1}},
                "upsert": True,
            },
        )
        assert r["matchedCount"] == 1, r
        u = (
            await p._mcp.call_tool(
                "find-one", {"collection": "users", "filter": {"_id": "U1"}}
            )
        )["document"]
        assert u == {"_id": "U1", "name": "a", "created": 1}, u
        # scan fallback (empty filter)
        fr = await p._mcp.call_tool("find", {"collection": "users", "filter": {}})
        assert len(fr["documents"]) == 1, fr

    asyncio.run(run())


def test_a1_bounded_timeout_config_applied(monkeypatch):
    """FIX 2: the real boto3 resource is built with a bounded botocore Config."""
    captured: dict[str, Any] = {}

    import boto3

    def _fake_resource(service, **kwargs):
        captured.update(kwargs)

        class _R:
            def Table(self, name):  # noqa: N802
                return object()

        return _R()

    monkeypatch.setattr(boto3, "resource", _fake_resource)
    # No injected resource -> the constructor builds a real one (mocked).
    DynamoMCPClient(table_prefix="t_")
    cfg = captured.get("config")
    assert cfg is not None, "no botocore Config passed"
    assert cfg.connect_timeout == 2, cfg.connect_timeout
    assert cfg.read_timeout == 3, cfg.read_timeout
    assert cfg.retries == {"max_attempts": 2, "mode": "standard"}, cfg.retries


# --------------------------------------------------------------------------- #
# FIX 4 — gate-replay flag (genuine fresh resume vs 25s keepalive ping)
# --------------------------------------------------------------------------- #


def test_a1_did_fresh_resume_default_false():
    """A brand-new connection's SessionState gates the first resume as fresh."""
    from grace2_agent.server import SessionState

    st = SessionState(session_id="0" * 26)
    assert st.did_fresh_resume is False


def test_a1_session_resume_gates_layer_replay_to_first_resume(monkeypatch):
    """Only the FIRST bare resume replays layers; keepalive pings skip it.

    Drives ``_handle_session_resume`` three times on one SessionState (the
    per-connection state). Replay must run exactly ONCE (the genuine fresh
    resume); the 2nd/3rd (keepalive pings) must NOT re-run the Dynamo
    layer replay — the BLINK fix. ``emit_session_state`` (the pong) fires
    every time.
    """
    import grace2_agent.server as server
    from grace2_agent.server import SessionState

    st = SessionState(session_id="0" * 26)

    replay_calls = 0
    emit_calls = 0

    async def _fake_replay(state):
        nonlocal replay_calls
        replay_calls += 1

    class _FakeEmitter:
        async def emit_session_state(self):
            nonlocal emit_calls
            emit_calls += 1

    async def _noop(*a, **k):
        return None

    def _ensure_emitter(ws, state):
        if state.emitter is None:
            state.emitter = _FakeEmitter()

    monkeypatch.setattr(server, "_replay_active_case_layers", _fake_replay)
    monkeypatch.setattr(server, "_ensure_emitter", _ensure_emitter)
    monkeypatch.setattr(server, "_rebind_live_turns", lambda *a, **k: 0)
    monkeypatch.setattr(server, "_emit_case_list", _noop)
    monkeypatch.setattr(server, "_emit_turn_complete", _noop)

    async def run():
        for _ in range(3):
            await server._handle_session_resume(object(), st)

    asyncio.run(run())

    assert replay_calls == 1, f"layer replay ran {replay_calls}x (BLINK if >1)"
    assert emit_calls == 3, emit_calls  # pong every resume
    assert st.did_fresh_resume is True


def test_a1_rebound_resume_defers_replay_to_a_later_keepalive(monkeypatch):
    """When a live turn is rebound on the first resume, the layer replay is
    deferred — a later (non-rebound) resume performs the one-time seed.

    Guards the dedup rationale: a rebound first resume must NOT burn the
    genuine-resume token before this connection's emitter is ever seeded.
    """
    import grace2_agent.server as server
    from grace2_agent.server import SessionState

    st = SessionState(session_id="0" * 26)
    replay_calls = 0
    rebinds = iter([2, 0, 0])  # first resume rebinds a live turn; rest don't

    async def _fake_replay(state):
        nonlocal replay_calls
        replay_calls += 1

    class _FakeEmitter:
        async def emit_session_state(self):
            return None

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(server, "_replay_active_case_layers", _fake_replay)
    monkeypatch.setattr(
        server, "_ensure_emitter",
        lambda ws, s: setattr(s, "emitter", _FakeEmitter()) if s.emitter is None else None,
    )
    monkeypatch.setattr(server, "_rebind_live_turns", lambda *a, **k: next(rebinds))
    monkeypatch.setattr(server, "_emit_case_list", _noop)
    monkeypatch.setattr(server, "_emit_turn_complete", _noop)

    async def run():
        for _ in range(3):
            await server._handle_session_resume(object(), st)

    asyncio.run(run())
    # rebound first resume deferred; 2nd (bare) resume seeded once; 3rd skipped.
    assert replay_calls == 1, replay_calls
    assert st.did_fresh_resume is True


# --------------------------------------------------------------------------- #
# C1 — tool-card IO persistence (live ToolIoPayload field names in content)
# --------------------------------------------------------------------------- #


def test_a1_tool_card_io_persisted_on_typed_record(monkeypatch):
    """C1 (the rehydration fix): ``_persist_tool_card`` populates raw_args +
    function_response (+ truncation metadata) on the TYPED ``ToolCardRecord``
    — the integration path W2 reads off ``m.tool_card`` on replay — under the
    LIVE ``ToolIoPayload`` field names. The content JSON twin mirrors them."""
    import grace2_agent.server as server
    from grace2_agent.server import SessionState

    captured: dict[str, Any] = {}

    async def _fake_persist_chat_turn(state, *, role, content, **kw):
        captured["role"] = role
        captured["content"] = content
        captured["tool_card"] = kw.get("tool_card")

    monkeypatch.setattr(server, "_persist_chat_turn", _fake_persist_chat_turn)

    st = SessionState(session_id="0" * 26)
    st.emitter = None  # force the wall-clock fallback (no emitter step)

    async def run():
        from datetime import datetime, timezone

        await server._persist_tool_card(
            st,
            tool_name="fetch_3dep_dem",
            label="Fetching DEM",
            card_state="complete",
            started_at_fallback=datetime.now(timezone.utc),
            duration_ms_fallback=1234,
            case_id="C" * 26,
            raw_args={"bbox": [1, 2, 3, 4]},
            function_response={"status": "ok", "uri": "s3://b/k.tif"},
            io_is_error=False,
        )

    asyncio.run(run())
    assert captured["role"] == "tool"
    # C1 — the IO now rides the TYPED record (the integration path), NOT just
    # the content JSON. W2 reads these off ``m.tool_card`` on get_session_state
    # replay so the expander rehydrates on Case reopen.
    card = captured["tool_card"]
    assert card is not None
    assert json.loads(card.raw_args) == {"bbox": [1, 2, 3, 4]}
    assert json.loads(card.function_response)["uri"] == "s3://b/k.tif"
    assert card.is_error is False
    assert card.args_truncated is False
    assert card.response_truncated is False
    assert isinstance(card.args_bytes, int)
    assert isinstance(card.response_bytes, int)
    # The content JSON twin mirrors the same values (belt-and-suspenders).
    body = json.loads(captured["content"])
    for k in (
        "raw_args",
        "function_response",
        "args_truncated",
        "response_truncated",
        "args_bytes",
        "response_bytes",
        "is_error",
    ):
        assert k in body, f"missing {k} in persisted content: {sorted(body)}"
    assert json.loads(body["raw_args"]) == {"bbox": [1, 2, 3, 4]}
    assert json.loads(body["function_response"])["uri"] == "s3://b/k.tif"
    assert body["is_error"] is False


def test_a1_tool_card_without_io_has_none_io(monkeypatch):
    """The /invoke path (no IO supplied) leaves the typed record's IO fields at
    None (pre-C1 documents replay unchanged; the chevron stays absent)."""
    import grace2_agent.server as server
    from grace2_agent.server import SessionState

    captured: dict[str, Any] = {}

    async def _fake_persist_chat_turn(state, *, role, content, **kw):
        captured["content"] = content
        captured["tool_card"] = kw.get("tool_card")

    monkeypatch.setattr(server, "_persist_chat_turn", _fake_persist_chat_turn)
    st = SessionState(session_id="0" * 26)
    st.emitter = None

    async def run():
        from datetime import datetime, timezone

        await server._persist_tool_card(
            st,
            tool_name="compute_slope",
            label="Computing slope",
            card_state="complete",
            started_at_fallback=datetime.now(timezone.utc),
            duration_ms_fallback=10,
            case_id="C" * 26,
        )

    asyncio.run(run())
    card = captured["tool_card"]
    assert card is not None
    # No IO supplied -> typed record's IO fields are None (the expander is absent
    # on replay), exactly as for a pre-C1 card.
    assert card.raw_args is None
    assert card.function_response is None
    assert card.is_error is None
    body = json.loads(captured["content"])
    assert body["tool_name"] == "compute_slope"
    # The keys exist on the dump but carry null (the typed record's defaults).
    assert body["raw_args"] is None
    assert body["function_response"] is None


# --------------------------------------------------------------------------- #
# C2 — turn-complete/idle signal (W2-pinned envelope shape)
# --------------------------------------------------------------------------- #


def test_a1_emit_turn_complete_envelope_shape():
    """``_emit_turn_complete`` emits a ``turn-complete`` envelope whose payload
    matches the W2-pinned ``TurnCompletePayload`` (envelope_type/pipeline_id/
    final_state, all optional)."""
    import grace2_agent.server as server
    from grace2_agent.server import SessionState

    sent: list[str] = []

    class _FakeWS:
        async def send(self, data: str) -> None:
            sent.append(data)

    st = SessionState(session_id="0" * 26)

    async def run():
        await server._emit_turn_complete(
            _FakeWS(), st, pipeline_id="P" * 26, final_state="complete"
        )

    asyncio.run(run())
    assert len(sent) == 1, sent
    env = json.loads(sent[0])
    assert env["type"] == "turn-complete"
    assert env["session_id"] == "0" * 26
    pl = env["payload"]
    assert pl["envelope_type"] == "turn-complete"
    assert pl["pipeline_id"] == "P" * 26
    assert pl["final_state"] == "complete"


def test_a1_emit_turn_complete_bare_idle():
    """A whole-turn idle (no pipeline) emits a bare-but-valid turn-complete."""
    import grace2_agent.server as server
    from grace2_agent.server import SessionState

    sent: list[str] = []

    class _FakeWS:
        async def send(self, data: str) -> None:
            sent.append(data)

    st = SessionState(session_id="0" * 26)
    asyncio.run(server._emit_turn_complete(_FakeWS(), st))
    pl = json.loads(sent[0])["payload"]
    assert pl["pipeline_id"] is None
    assert pl["final_state"] is None


def test_a1_emit_turn_complete_swallows_send_failure():
    """A half-closed socket (send raises) never propagates out of the idle signal."""
    import grace2_agent.server as server
    from grace2_agent.server import SessionState

    class _DeadWS:
        async def send(self, data: str) -> None:
            raise ConnectionError("socket closed")

    st = SessionState(session_id="0" * 26)
    # Must not raise.
    asyncio.run(server._emit_turn_complete(_DeadWS(), st))
