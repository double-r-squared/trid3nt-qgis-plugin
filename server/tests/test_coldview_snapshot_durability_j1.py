"""COLDVIEW DURABILITY (J1) - case-view snapshot write must not be raced by
daemon shutdown.

Root cause (reports/design/coldview_layers_fix.md): opening a Case with the
daemon down showed the case + chat but NO layers because the cold source
of truth -- ``s3://RUNS_BUCKET/case-views/{case_id}.json`` -- was STALE. The
layer-publish snapshot REBUILD was a DETACHED fire-and-forget task added to
``_BG_SNAPSHOT_TASKS`` and never drained, so the process could stop AFTER the
turn returned but BEFORE the detached S3 PUT landed, leaving the cold object
at its prior (often empty) contents. Chat survived because it persists
synchronously.

J1 fix (server.py only):
  1. The layer-publish site AWAITS ``_persist_case_view_snapshot`` +
     ``_persist_case_manifest`` inline (durable before the turn returns).
  2. ``_drain_bg_snapshot_tasks`` (called from ``run_server``'s shutdown
     ``finally``) gathers outstanding writes with a bounded timeout so a
     graceful SIGTERM flushes them.

These are pure in-process tests -- no asyncio server, no network, no live model.
The persistence layer uses the in-memory ``MockMCPClient`` and an injected fake
S3 put, exactly like ``test_case_view_snapshot.py``.
"""

from __future__ import annotations

import ast
import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from trid3nt_server import server
from trid3nt_server.persistence import (
    CASE_VIEWS_BUCKET,
    Persistence,
    case_view_snapshot_key,
)
from trid3nt_contracts.case import CaseSummary
from trid3nt_contracts.collections import ProjectLayerSummary
from trid3nt_contracts.common import new_ulid

# Reuse the in-memory MCP backend the persistence suite uses.
from .test_persistence import MockMCPClient


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _reset_liveness_and_tasks():
    """Isolate the process-global live-turn registry + snapshot-task set."""
    server._SESSION_LIVE_TURNS.clear()
    server._BG_SNAPSHOT_TASKS.clear()
    _saved_persistence = server.get_persistence()
    yield
    server._SESSION_LIVE_TURNS.clear()
    server._BG_SNAPSHOT_TASKS.clear()
    server.set_persistence(_saved_persistence)


class _FakeS3:
    """Captures the (bucket, key, body) the snapshot write PUTs to S3."""

    def __init__(self) -> None:
        self.bucket: str | None = None
        self.key: str | None = None
        self.body: bytes | None = None
        self.call_count = 0

    def put(self, bucket: str, key: str, body: bytes, metadata: dict) -> None:
        self.bucket = bucket
        self.key = key
        self.body = body
        self.call_count += 1


def _seed_case_with_layers() -> tuple[Persistence, str, str]:
    """Seed a Case carrying a raster + a vector layer; return (p, case_id, vid)."""
    p = Persistence(MockMCPClient())
    case_id = new_ulid()
    raster_id = new_ulid()
    vector_id = new_ulid()
    raster = ProjectLayerSummary(
        layer_id=raster_id,
        name="Flood depth",
        layer_type="raster",
        uri="s3://trid3nt-runs/abc/flood.tif",
        style_preset="flood-depth",
        visible=True,
        role="primary",
        temporal=False,
    )
    vector = ProjectLayerSummary(
        layer_id=vector_id,
        name="Buildings",
        layer_type="vector",
        uri="s3://trid3nt-runs/abc/buildings.geojson",
        style_preset="buildings",
        visible=True,
        role="context",
        temporal=False,
    )
    case = CaseSummary(
        case_id=case_id,
        title="Hurricane Ian - Fort Myers",
        created_at=datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc),
        status="active",
        bbox=(-82.0, 26.5, -81.8, 26.7),
        primary_hazard="flood",
        layer_summary=[raster_id, vector_id],
        loaded_layer_summaries=[
            raster.model_dump(mode="json"),
            vector.model_dump(mode="json"),
        ],
    )
    asyncio.run(p.upsert_case(case))
    return p, case_id, vector_id


# --------------------------------------------------------------------------- #
# 1. The layer-publish path AWAITS + WRITES the snapshot (loaded_layers present)
#    before returning -- it is no longer detached into _BG_SNAPSHOT_TASKS.
# --------------------------------------------------------------------------- #


def test_publish_snapshot_persist_is_awaited_and_writes_loaded_layers() -> None:
    """``_persist_case_view_snapshot`` (the awaited publish-site call) flushes
    the cold ``case-views/{case_id}.json`` with the published layers BEFORE it
    returns -- proving the write is durable, not detached."""
    import json

    p, case_id, _vid = _seed_case_with_layers()
    fake = _FakeS3()
    # Route the production write through the fake S3 put (no network).
    orig = p.write_case_view_snapshot

    async def _routed(cid, **kw):
        kw.setdefault("s3_put", fake.put)
        return await orig(cid, **kw)

    p.write_case_view_snapshot = _routed  # type: ignore[assignment]
    server.set_persistence(p)

    state = server.SessionState(session_id="sess-pub")
    state.current_turn_case_id = case_id  # pin the turn's Case

    # The publish site does exactly this (now awaited, not create_task'd).
    asyncio.run(server._persist_case_view_snapshot(state, case_id=case_id))

    # The cold object was PUT to S3 (durable) before the await returned.
    assert fake.call_count == 1
    assert fake.bucket == CASE_VIEWS_BUCKET
    assert fake.key == case_view_snapshot_key(case_id)
    written = json.loads(fake.body.decode("utf-8"))
    layers = written["session_state"]["loaded_layers"]
    # The published layers are present in the cold-renderable array.
    assert layers, "loaded_layers must be non-empty in the cold snapshot"
    assert len(layers) == 2


def _function_named(tree: ast.Module, name: str) -> ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"async function {name!r} not found in server.py")


def _snapshot_create_task_calls(fn: ast.AST) -> list[str]:
    """Names of snapshot/manifest persists wrapped in ``create_task`` in ``fn``."""
    detached: list[str] = []
    for call in ast.walk(fn):
        if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "create_task"
        ):
            for arg in call.args:
                if (
                    isinstance(arg, ast.Call)
                    and isinstance(arg.func, ast.Name)
                    and arg.func.id
                    in {"_persist_case_view_snapshot", "_persist_case_manifest"}
                ):
                    detached.append(arg.func.id)
    return detached


def test_publish_site_awaits_snapshot_not_create_task() -> None:
    """SOURCE-LEVEL contract: ``_invoke_tool_via_emitter`` -- the tool-dispatch
    coroutine that owns BOTH layer-publish snapshot sites (the dispatch
    finally-block AND the ``publish_layer`` WMS-string wrap-site) -- AWAITS the
    snapshot + manifest persist and NEVER wraps them in ``asyncio.create_task``
    (the box-stop-raced detach that left ``case-views/{case_id}.json`` stale).
    Parsing the AST locks the durability so a future refactor cannot silently
    re-detach the layer-publish write."""
    src = Path(server.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = _function_named(tree, "_invoke_tool_via_emitter")

    awaited = {
        n.value.func.id
        for n in ast.walk(fn)
        if isinstance(n, ast.Await)
        and isinstance(n.value, ast.Call)
        and isinstance(n.value.func, ast.Name)
    }
    assert "_persist_case_view_snapshot" in awaited, (
        "_invoke_tool_via_emitter must AWAIT _persist_case_view_snapshot at the "
        "layer-publish site (durable before the turn returns)"
    )
    assert "_persist_case_manifest" in awaited, (
        "_invoke_tool_via_emitter must AWAIT _persist_case_manifest at the "
        "layer-publish site"
    )

    detached = _snapshot_create_task_calls(fn)
    assert not detached, (
        "the layer-publish snapshot/manifest write must be AWAITED inline, "
        f"but found detached create_task wrappers for: {detached}"
    )


def test_turn_close_snapshot_stays_detached_and_is_drained() -> None:
    """The turn-close site (``_dispatch_gemini_and_persist``) MAY stay
    fire-and-forget -- it refreshes chat, not the layer set -- and is covered by
    the shutdown drain rather than an inline await. This asserts the
    detach is INTENTIONAL there (so a reviewer does not mistake it for the
    publish-site regression) while the drain helper exists to flush it."""
    src = Path(server.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    turn_close = _function_named(tree, "_dispatch_gemini_and_persist")
    # The turn-close site detaches (acceptable: drained on shutdown).
    assert _snapshot_create_task_calls(turn_close), (
        "expected the turn-close site to detach its chat-refresh snapshot "
        "(drained by _drain_bg_snapshot_tasks on shutdown)"
    )
    # The drain helper exists to flush those detached writes on graceful stop.
    assert hasattr(server, "_drain_bg_snapshot_tasks")


# --------------------------------------------------------------------------- #
# 2. The shutdown drain flushes pending snapshot tasks.
# --------------------------------------------------------------------------- #


def test_shutdown_drain_flushes_pending_snapshot_tasks() -> None:
    """``_drain_bg_snapshot_tasks`` awaits outstanding detached writes to
    completion, so a graceful stop lands the cold snapshot before exit."""

    async def _run() -> None:
        landed: list[str] = []

        async def _write(tag: str) -> None:
            # Simulate a short S3 PUT that has NOT yet completed at shutdown.
            await asyncio.sleep(0.01)
            landed.append(tag)

        for tag in ("snap", "manifest"):
            t = asyncio.create_task(_write(tag))
            server._BG_SNAPSHOT_TASKS.add(t)
            t.add_done_callback(server._BG_SNAPSHOT_TASKS.discard)

        # Before drain, the writes are still pending.
        assert landed == []
        assert any(not t.done() for t in server._BG_SNAPSHOT_TASKS)

        await server._drain_bg_snapshot_tasks()

        # After drain, BOTH writes completed (flushed before shutdown).
        assert sorted(landed) == ["manifest", "snap"]
        assert not any(not t.done() for t in server._BG_SNAPSHOT_TASKS)

    asyncio.run(_run())


def test_shutdown_drain_is_bounded_and_does_not_hang() -> None:
    """A pathologically slow PUT cannot hang shutdown: the drain abandons it
    after the bounded timeout rather than blocking teardown forever."""

    async def _run() -> None:
        async def _never() -> None:
            await asyncio.sleep(3600)  # would hang shutdown without the bound

        t = asyncio.create_task(_never())
        server._BG_SNAPSHOT_TASKS.add(t)
        t.add_done_callback(server._BG_SNAPSHOT_TASKS.discard)

        # A tiny timeout: the drain returns promptly (does NOT wait 3600s).
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await server._drain_bg_snapshot_tasks(timeout=0.05)
        elapsed = loop.time() - t0
        assert elapsed < 1.0, "drain must be bounded, not block on a slow PUT"

        t.cancel()  # cleanup the abandoned task

    asyncio.run(_run())


def test_shutdown_drain_noop_when_nothing_pending() -> None:
    """No outstanding writes -> the drain is a clean no-op (no error)."""
    assert len(server._BG_SNAPSHOT_TASKS) == 0
    asyncio.run(server._drain_bg_snapshot_tasks())
    assert len(server._BG_SNAPSHOT_TASKS) == 0


def test_shutdown_drain_survives_a_failing_write() -> None:
    """A write that RAISES does not break the drain (return_exceptions=True):
    the other pending writes still flush."""

    async def _run() -> None:
        landed: list[str] = []

        async def _ok() -> None:
            await asyncio.sleep(0.01)
            landed.append("ok")

        async def _boom() -> None:
            await asyncio.sleep(0.01)
            raise RuntimeError("S3 down")

        for coro in (_ok(), _boom()):
            t = asyncio.create_task(coro)
            server._BG_SNAPSHOT_TASKS.add(t)
            t.add_done_callback(server._BG_SNAPSHOT_TASKS.discard)

        # Must NOT raise despite one write blowing up.
        await server._drain_bg_snapshot_tasks()
        assert landed == ["ok"]

    asyncio.run(_run())
