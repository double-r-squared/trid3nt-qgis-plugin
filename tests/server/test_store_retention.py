"""Retention by reference: a live Case pins what its runs consumed.

An object named on the journal record of a run whose layer a Case still holds
outlives its TTL class; an object nothing points at goes the moment its window
has rolled; deleting the Case releases what it held.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from trid3nt_contracts.case import CaseSummary
from trid3nt_contracts.common import new_ulid
from trid3nt_server import storage
from trid3nt_server.persistence import FileMCPClient, Persistence
from trid3nt_server.retention import reap

BUCKET = "trid3nt-cache-test"
DEM = "cache/static-30d/dem/aaaaaaaa.tif"
DEM_SIDECAR = "cache/static-30d/dem/aaaaaaaa.provenance.json"
GAUGE = "cache/static-30d/gauge/bbbbbbbb.json"
SUPPLIED = "cache/static-30d/supplied/cccccccc.tif"
LAYER_URI = "s3://trid3nt-runs/01RUN/flood_depth.tif"

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
#: A different ``%Y-%m`` window from NOW, so the static-30d key that addressed it
#: can no longer be computed.
SPENT = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
FRESH = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)


class _FakeStore:
    """An object store with just the list and the delete the reaper calls."""

    def __init__(self, objects: dict[str, datetime]) -> None:
        self.objects = dict(objects)

    def get_paginator(self, name: str) -> "_FakeStore":
        return self

    def paginate(self, **kwargs: object) -> list[dict]:
        prefix = str(kwargs.get("Prefix") or "")
        return [{"Contents": [{"Key": key, "LastModified": stamp}
                              for key, stamp in sorted(self.objects.items())
                              if key.startswith(prefix)]}]

    def delete_object(self, *, Bucket: str, Key: str) -> None:
        self.objects.pop(Key, None)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _FakeStore:
    """The cache bucket, the journal and the object store, all under tmp_path."""
    monkeypatch.setenv("TRID3NT_CACHE_BUCKET", BUCKET)
    monkeypatch.setenv("TRID3NT_DEV_PERSISTENCE_DIR", str(tmp_path))
    record = {
        "run_id": "01RUN",
        "outputs": [{"uri": LAYER_URI, "dataset_uris": []}],
        "sheet": [{"name": "bed", "real_source": f"s3://{BUCKET}/{DEM}"}],
    }
    (tmp_path / "run_journal.jsonl").write_text(json.dumps(record) + "\n",
                                                encoding="utf-8")
    fake = _FakeStore({DEM: SPENT, DEM_SIDECAR: SPENT, GAUGE: SPENT})
    storage.set_client(fake)
    yield fake
    storage.set_client(None)


def _case_holding(layer_id: str, name: str, uri: str) -> CaseSummary:
    return CaseSummary(
        case_id=new_ulid(),
        title="St. Clair ice run",
        created_at=NOW,
        updated_at=NOW,
        loaded_layer_summaries=[{"layer_id": layer_id, "name": name,
                                 "layer_type": "raster", "uri": uri,
                                 "visible": True, "role": "primary",
                                 "temporal": False}],
    )


def _case_holding_the_layer() -> CaseSummary:
    return _case_holding("flood-depth", "flood depth", LAYER_URI)


def test_a_referenced_object_survives_its_ttl(store: _FakeStore,
                                              tmp_path: Path) -> None:
    """The Case holds the run's layer, so the DEM the run's record names stays -
    with its provenance sidecar, which is half of what makes it a cache hit -
    while the object no record points at goes."""
    db = FileMCPClient(base_dir=tmp_path / "db")
    asyncio.run(Persistence(db).upsert_case(_case_holding_the_layer()))

    deleted = asyncio.run(reap(now=NOW, client=db))

    assert deleted == [GAUGE]
    assert set(store.objects) == {DEM, DEM_SIDECAR}


def test_an_unreferenced_object_goes_and_a_current_one_stays(
        store: _FakeStore, tmp_path: Path) -> None:
    """No Case holds the run's layer, so nothing is pinned: every spent object
    goes and one still inside its window stays."""
    store.objects[GAUGE] = FRESH
    db = FileMCPClient(base_dir=tmp_path / "db")

    deleted = asyncio.run(reap(now=NOW, client=db))

    assert set(deleted) == {DEM, DEM_SIDECAR}
    assert set(store.objects) == {GAUGE}


def test_a_deleted_case_releases_its_objects(store: _FakeStore,
                                             tmp_path: Path) -> None:
    """A Case that pinned the DEM is deleted; the next sweep takes it. An
    archived Case still holds what it held."""
    db = FileMCPClient(base_dir=tmp_path / "db")
    p = Persistence(db)
    case = _case_holding_the_layer()
    asyncio.run(p.upsert_case(case))
    asyncio.run(p.archive_case(case.case_id))

    assert asyncio.run(reap(now=NOW, client=db)) == [GAUGE]

    asyncio.run(p.delete_case(case.case_id))

    assert set(asyncio.run(reap(now=NOW, client=db))) == {DEM, DEM_SIDECAR}
    assert store.objects == {}


def test_a_case_held_object_no_run_names_survives_and_is_released(
        store: _FakeStore, tmp_path: Path) -> None:
    """A layer the person supplied is held by the Case alone - no run record
    names it, so the Case is the only thing pinning it. It outlives its window
    while the Case exists and goes once the Case is deleted."""
    store.objects[SUPPLIED] = SPENT
    db = FileMCPClient(base_dir=tmp_path / "db")
    p = Persistence(db)
    case = _case_holding("shoreline", "shoreline", f"s3://{BUCKET}/{SUPPLIED}")
    asyncio.run(p.upsert_case(case))

    assert SUPPLIED not in asyncio.run(reap(now=NOW, client=db))
    assert SUPPLIED in store.objects

    asyncio.run(p.delete_case(case.case_id))

    assert SUPPLIED in asyncio.run(reap(now=NOW, client=db))
    assert SUPPLIED not in store.objects
