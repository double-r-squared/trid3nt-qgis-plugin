"""Unit tests for the fetch-time provenance channel (ADR 0110).

The general, minimal, cache-replayable sidecar from fetch to envelope: a delegate
records a small typed dict during a NON-cached fetch; ``read_through`` persists it
as a ``<key>.provenance.json`` sibling and replays it byte-for-byte on a cache hit;
a caller that passes no recorder is byte-identical to before (strict no-op).
"""

from __future__ import annotations

from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.agent.tools import cache as cache_mod
from trid3nt_server.agent.tools.cache import (
    ProvenanceRecorder,
    read_through,
    record_provenance,
    _sidecar_key,
)


def _md() -> AtomicToolMetadata:
    return AtomicToolMetadata(
        name="prov_probe",
        ttl_class="static-30d",
        source_class="prov_probe",
        cacheable=True,
        supports_global_query=False,
        payload_mb_estimator_name="estimate_payload_mb",
    )


def test_record_provenance_is_noop_without_recorder() -> None:
    """A delegate that records with no bound recorder never raises (safe always)."""
    record_provenance({"anything": 1})  # no active recorder -> silent no-op


def test_miss_records_sidecar_then_hit_replays_identical(fake_s3: Any) -> None:
    """MISS binds the recorder around fetch_fn (delegate records); the sidecar is
    persisted; a later HIT replays the SAME dict from the sidecar without re-fetch."""
    prov = {"bathymetry_present": False, "cudem_tile_count": 0, "warn": "land_absent"}

    calls = {"n": 0}

    def _fetch() -> bytes:
        calls["n"] += 1
        record_provenance(prov)
        return b"COGBYTES"

    # MISS: recorder bound, fetch runs, provenance recorded + persisted.
    rec1 = ProvenanceRecorder()
    r1 = read_through(metadata=_md(), params={"bbox": [1, 2, 3, 4]}, ext="tif",
                      fetch_fn=_fetch, provenance=rec1)
    assert r1.hit is False
    assert r1.provenance == prov
    assert rec1.data == prov
    assert calls["n"] == 1
    # The sidecar object sits next to the artifact.
    obj_key = r1.uri.split("/", 3)[3]
    assert _sidecar_key(obj_key) in fake_s3.store

    # HIT: no re-fetch; provenance replayed IDENTICAL from the sidecar.
    rec2 = ProvenanceRecorder()
    r2 = read_through(metadata=_md(), params={"bbox": [1, 2, 3, 4]}, ext="tif",
                      fetch_fn=_fetch, provenance=rec2)
    assert r2.hit is True
    assert calls["n"] == 1, "cache hit must NOT re-run fetch_fn"
    assert r2.provenance == prov, "cache-hit replay must equal the original fetch provenance"
    assert rec2.data == prov


def test_no_recorder_is_byte_identical_no_sidecar(fake_s3: Any) -> None:
    """A caller that passes no recorder writes NO sidecar and returns provenance=None
    (the strict no-op that keeps every prior spec byte-identical)."""
    def _fetch() -> bytes:
        record_provenance({"should": "be ignored"})  # no recorder bound
        return b"X"

    r = read_through(metadata=_md(), params={"k": 1}, ext="tif", fetch_fn=_fetch)
    assert r.provenance is None
    obj_key = r.uri.split("/", 3)[3]
    assert _sidecar_key(obj_key) not in fake_s3.store
    # Exactly one object (the artifact), no sidecar.
    assert list(fake_s3.store) == [obj_key]


def test_legacy_object_without_sidecar_replays_none(fake_s3: Any) -> None:
    """An object cached BEFORE the channel (no sidecar) -> provenance None on hit,
    so the envelope hook falls back to its declared defaults (no regression)."""
    md = _md()
    from trid3nt_server.agent.tools.cache import cache_path, compute_cache_key
    key = compute_cache_key(md.source_class, {"k": 9}, md.ttl_class)
    path = cache_path(md.source_class, md.ttl_class, key, "tif")
    fake_s3.store[path] = b"LEGACY"  # artifact present, NO sidecar

    rec = ProvenanceRecorder()
    r = read_through(metadata=md, params={"k": 9}, ext="tif",
                     fetch_fn=lambda: b"unused", provenance=rec)
    assert r.hit is True
    assert r.provenance is None
    assert rec.data is None
