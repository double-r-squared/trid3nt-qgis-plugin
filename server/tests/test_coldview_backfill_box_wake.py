"""COLDVIEW FRESHNESS BACKFILL (box-wake) - close the snapshot-staleness gap.

Root cause: the case-view snapshot (``case-views/{id}.json``) and the thin
manifest (``case-manifests/{id}.json``) are ONLY ever (re)written while the
agent box is UP -- the 4 mutation triggers (create / rename / layer-publish /
turn-close) plus case-open. There is NO box-off / wake-time materialization
path, so a Case that gained layers and was then left as the box auto-stopped
(or whose newest snapshot predates its current layers) shows a STALE / empty
cold face indefinitely: the exact "can't see it until I connect" symptom.

Server-half fix:
  1. ``Persistence.list_all_active_case_ids`` -- owner-agnostic enumerator of
     every LIVE Case id (tombstones excluded), for the sweep.
  2. ``server._run_coldview_backfill`` -- a box-wake startup sweep that
     re-materializes the snapshot + manifest for EVERY live Case off the
     persisted ``projects`` doc (no live session / emitter needed), so a
     box-off owned Case serves a CURRENT cold face without a warm re-open.
  3. ``run_server`` fires it as a tracked fire-and-forget task right after the
     pre-Auth migration (so it never delays accepting the waking connection).

Pure in-process tests -- no asyncio server, no network, no live model. The
persistence layer uses the in-memory ``MockMCPClient`` and an injected fake S3
put, exactly like ``test_coldview_snapshot_durability_j1.py``.
"""

from __future__ import annotations

import ast
import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from grace2_agent import server
from grace2_agent.persistence import (
    CASE_VIEWS_BUCKET,
    Persistence,
    case_manifest_key,
    case_view_snapshot_key,
)
from grace2_contracts.case import CaseSummary
from grace2_contracts.collections import ProjectLayerSummary
from grace2_contracts.common import new_ulid

# Reuse the in-memory MCP backend the persistence suite uses.
from .test_persistence import MockMCPClient


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _reset_persistence_and_tasks():
    """Isolate the process-global persistence singleton + snapshot-task set."""
    server._BG_SNAPSHOT_TASKS.clear()
    _saved = server.get_persistence()
    yield
    server._BG_SNAPSHOT_TASKS.clear()
    server.set_persistence(_saved)


class _FakeS3:
    """Captures every (bucket, key) the backfill PUTs to S3."""

    def __init__(self) -> None:
        self.keys: list[str] = []
        self.buckets: list[str] = []

    def put(self, bucket: str, key: str, body: bytes, metadata: dict) -> None:
        self.buckets.append(bucket)
        self.keys.append(key)


def _route_writers_through(p: Persistence, fake: _FakeS3) -> None:
    """Patch BOTH persistence writers to PUT through the fake S3 (no network)."""
    orig_snap = p.write_case_view_snapshot
    orig_manifest = p.write_case_manifest

    async def _snap(cid, **kw):
        kw.setdefault("s3_put", fake.put)
        return await orig_snap(cid, **kw)

    async def _manifest(cid, **kw):
        kw.setdefault("s3_put", fake.put)
        return await orig_manifest(cid, **kw)

    p.write_case_view_snapshot = _snap  # type: ignore[assignment]
    p.write_case_manifest = _manifest  # type: ignore[assignment]


def _make_case(*, with_layers: bool, status: str = "active") -> CaseSummary:
    case_id = new_ulid()
    loaded: list[dict] = []
    layer_ids: list[str] = []
    if with_layers:
        raster_id = new_ulid()
        layer_ids = [raster_id]
        loaded = [
            ProjectLayerSummary(
                layer_id=raster_id,
                name="Flood depth",
                layer_type="raster",
                uri="s3://trid3nt-runs/abc/flood.tif",
                style_preset="flood-depth",
                visible=True,
                role="primary",
                temporal=False,
            ).model_dump(mode="json")
        ]
    return CaseSummary(
        case_id=case_id,
        title="Hurricane Ian - Fort Myers" if with_layers else "Empty Case",
        created_at=datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc),
        status=status,  # type: ignore[arg-type]
        bbox=(-82.0, 26.5, -81.8, 26.7),
        primary_hazard="flood",
        layer_summary=layer_ids,
        loaded_layer_summaries=loaded,
    )


def _seed_cases() -> tuple[Persistence, list[str]]:
    """Seed an empty Case + a with-layers Case (both live) + one archived.

    Returns ``(p, [live_case_ids])`` -- the archived Case id is NOT returned
    because the sweep must skip it.
    """
    p = Persistence(MockMCPClient())
    empty = _make_case(with_layers=False)
    populated = _make_case(with_layers=True)
    archived = _make_case(with_layers=True, status="archived")
    asyncio.run(p.upsert_case(empty))
    asyncio.run(p.upsert_case(populated))
    asyncio.run(p.upsert_case(archived))
    return p, [empty.case_id, populated.case_id]


# --------------------------------------------------------------------------- #
# 1. The persistence enumerator returns every LIVE Case id, tombstones excluded.
# --------------------------------------------------------------------------- #


def test_list_all_active_case_ids_returns_live_only() -> None:
    p, live_ids = _seed_cases()
    got = asyncio.run(p.list_all_active_case_ids())
    assert sorted(got) == sorted(live_ids), (
        "enumerator must return both LIVE Cases and exclude the archived one"
    )


def test_list_all_active_case_ids_empty_when_no_cases() -> None:
    p = Persistence(MockMCPClient())
    assert asyncio.run(p.list_all_active_case_ids()) == []


# --------------------------------------------------------------------------- #
# 2. The box-wake sweep re-materializes snapshot + manifest for EVERY live Case
#    -- INCLUDING the stale/empty one (the exact symptom) -- and skips tombstones.
# --------------------------------------------------------------------------- #


def test_backfill_writes_snapshot_and_manifest_for_every_live_case() -> None:
    p, live_ids = _seed_cases()
    fake = _FakeS3()
    _route_writers_through(p, fake)
    server.set_persistence(p)

    asyncio.run(server._run_coldview_backfill())

    # Every live Case got BOTH a snapshot and a manifest PUT (2 keys each).
    expected = set()
    for cid in live_ids:
        expected.add(case_view_snapshot_key(cid))
        expected.add(case_manifest_key(cid))
    assert set(fake.keys) == expected, (
        "the sweep must write a fresh snapshot AND manifest for every live Case "
        "(including the empty one) and skip the archived Case"
    )
    # All writes landed in the case-views bucket.
    assert set(fake.buckets) == {CASE_VIEWS_BUCKET}
    # The archived Case's keys are absent (tombstone never re-materialized).
    assert len(fake.keys) == 2 * len(live_ids)


# --------------------------------------------------------------------------- #
# 3. Best-effort: no Persistence binding / disabled toggle -> clean no-op.
# --------------------------------------------------------------------------- #


def test_backfill_noop_when_no_persistence() -> None:
    server.set_persistence(None)
    # Must not raise; nothing to write.
    asyncio.run(server._run_coldview_backfill())


def test_backfill_disabled_by_env_flag(monkeypatch) -> None:
    p, _ids = _seed_cases()
    fake = _FakeS3()
    _route_writers_through(p, fake)
    server.set_persistence(p)
    # Flip the module-level toggle OFF (the env is read at import; patch the flag).
    monkeypatch.setattr(server, "_COLDVIEW_BACKFILL_ENABLED", False)

    asyncio.run(server._run_coldview_backfill())

    assert fake.keys == [], "disabled sweep must write nothing"


# --------------------------------------------------------------------------- #
# 4. One bad Case never aborts the sweep (per-Case best-effort).
# --------------------------------------------------------------------------- #


def test_backfill_survives_a_failing_case() -> None:
    p, live_ids = _seed_cases()
    fake = _FakeS3()

    # First write raises; the rest must still run (and be counted).
    calls = {"n": 0}
    orig_snap = p.write_case_view_snapshot

    async def _flaky_snap(cid, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("S3 down for this one")
        kw.setdefault("s3_put", fake.put)
        return await orig_snap(cid, **kw)

    orig_manifest = p.write_case_manifest

    async def _manifest(cid, **kw):
        kw.setdefault("s3_put", fake.put)
        return await orig_manifest(cid, **kw)

    p.write_case_view_snapshot = _flaky_snap  # type: ignore[assignment]
    p.write_case_manifest = _manifest  # type: ignore[assignment]
    server.set_persistence(p)

    # Must NOT raise despite one Case's snapshot blowing up.
    asyncio.run(server._run_coldview_backfill())

    # The OTHER Case's snapshot still wrote; both manifests still wrote.
    snap_keys = {case_view_snapshot_key(c) for c in live_ids}
    manifest_keys = {case_manifest_key(c) for c in live_ids}
    written = set(fake.keys)
    # Exactly one snapshot survived (the non-flaky one) + both manifests.
    assert len(written & snap_keys) == len(live_ids) - 1
    assert manifest_keys <= written


# --------------------------------------------------------------------------- #
# 5. SOURCE-LEVEL contract: run_server fires the sweep as a TRACKED
#    fire-and-forget task (drained on shutdown, GC-safe) -- not awaited inline
#    (so it never delays accepting the waking connection).
# --------------------------------------------------------------------------- #


def _function_named(tree: ast.Module, name: str) -> ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"async function {name!r} not found in server.py")


def test_run_server_fires_backfill_as_tracked_background_task() -> None:
    src = Path(server.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = _function_named(tree, "run_server")

    # The sweep is wrapped in create_task (fire-and-forget, not awaited inline).
    detached = []
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
                    and arg.func.id == "_run_coldview_backfill"
                ):
                    detached.append(arg.func.id)
    assert detached, (
        "run_server must launch _run_coldview_backfill via asyncio.create_task "
        "(fire-and-forget so it never delays accepting the waking connection)"
    )

    # It is NOT awaited inline (that would block startup behind the full sweep).
    awaited = {
        n.value.func.id
        for n in ast.walk(fn)
        if isinstance(n, ast.Await)
        and isinstance(n.value, ast.Call)
        and isinstance(n.value.func, ast.Name)
    }
    assert "_run_coldview_backfill" not in awaited, (
        "_run_coldview_backfill must be fire-and-forget, not awaited inline"
    )
