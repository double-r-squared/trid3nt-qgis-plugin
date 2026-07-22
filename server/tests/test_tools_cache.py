"""Unit + integration tests for the cache shim (job-0032, FR-DC-3, FR-DC-6).

Coverage:
- Cache-key determinism: identical inputs at the same vintage produce the
  same key.
- Cache-key vintage separation: different TTL bucket vintages produce
  different keys.
- TTL-bucket vintage strings for each of the four classes.
- ``cache_path`` matches the job-0031 live layout
  (``cache/<ttl-class>/<source-class>/<hash>.<ext>``).
- ``is_cacheable`` for each of the four TTL classes (parametrized).
- Read-through-on-hit: pre-seeded S3 object is returned verbatim and
  ``fetch_fn`` is NOT invoked.
- Write-on-miss: ``fetch_fn`` is invoked, the object lands in the bucket,
  and the ``s3://`` URI is returned.
- ``live-no-cache`` short-circuit: ``fetch_fn`` invoked, no S3 write.
- ``force_refresh=True``: lookup skipped, fetcher invoked, write executed.
- ``fetch_fn`` failure re-raises without writing a sentinel.

GCP is decommissioned (S3-only read-through): these tests drive the boto3 S3
path via an in-memory ``boto3.client`` double, NOT the old injected
``google.cloud.storage`` client.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from grace2_contracts.tool_registry import AtomicToolMetadata

from grace2_agent.tools.cache import (
    CACHE_KEY_HEX_LEN,
    cache_path,
    compute_cache_key,
    is_cacheable,
    read_through,
    ttl_bucket_vintage,
)


# ---------------------------------------------------------------------------
# Pure-function tests
# ---------------------------------------------------------------------------


def test_cache_key_is_deterministic_for_same_inputs():
    """Same source_id + params + vintage produce byte-identical keys."""
    pinned = datetime(2026, 6, 7, 3, 30, 0, tzinfo=timezone.utc)
    k1 = compute_cache_key(
        "dem", {"bbox": [-90.5, 32.0, -90.0, 32.5]}, "static-30d", now=pinned
    )
    k2 = compute_cache_key(
        "dem", {"bbox": [-90.5, 32.0, -90.0, 32.5]}, "static-30d", now=pinned
    )
    assert k1 == k2
    assert len(k1) == CACHE_KEY_HEX_LEN
    # Hex chars only.
    int(k1, 16)


def test_cache_key_separates_across_ttl_bucket_vintages():
    """Different TTL-bucket vintages produce different keys for same inputs.

    Acceptance criterion: ``dynamic-1h`` keys for the SAME params 90 minutes
    apart produce DIFFERENT keys; ``static-30d`` keys 5 days apart produce
    the SAME key.
    """
    params = {"bbox": [0.0, 0.0, 1.0, 1.0]}

    # dynamic-1h: 90 minutes apart -> different vintage strings -> different keys
    t1 = datetime(2026, 6, 7, 3, 10, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 7, 4, 40, 0, tzinfo=timezone.utc)  # +1h30m -> next bucket
    k1 = compute_cache_key("nwis_iv", params, "dynamic-1h", now=t1)
    k2 = compute_cache_key("nwis_iv", params, "dynamic-1h", now=t2)
    assert k1 != k2

    # static-30d: 5 days apart, same calendar month -> same vintage -> same key
    t3 = datetime(2026, 6, 7, 3, 10, 0, tzinfo=timezone.utc)
    t4 = datetime(2026, 6, 12, 3, 10, 0, tzinfo=timezone.utc)
    k3 = compute_cache_key("dem", params, "static-30d", now=t3)
    k4 = compute_cache_key("dem", params, "static-30d", now=t4)
    assert k3 == k4


def test_cache_key_separates_across_source_ids_and_params():
    pinned = datetime(2026, 6, 7, 3, 30, 0, tzinfo=timezone.utc)
    base = compute_cache_key("dem", {"bbox": [0, 0, 1, 1]}, "static-30d", now=pinned)
    other_source = compute_cache_key(
        "buildings", {"bbox": [0, 0, 1, 1]}, "static-30d", now=pinned
    )
    other_params = compute_cache_key(
        "dem", {"bbox": [0, 0, 1, 2]}, "static-30d", now=pinned
    )
    assert base != other_source
    assert base != other_params


def test_cache_key_canonicalization_ignores_none_and_key_order():
    """Canonicalization drops None values and sorts keys.

    Two calls that differ only in dict-key ordering or in including/omitting
    a None value should map to the same key.
    """
    pinned = datetime(2026, 6, 7, 3, 30, 0, tzinfo=timezone.utc)
    a = compute_cache_key("x", {"a": 1, "b": 2}, "static-30d", now=pinned)
    b = compute_cache_key("x", {"b": 2, "a": 1}, "static-30d", now=pinned)
    c = compute_cache_key(
        "x", {"a": 1, "b": 2, "optional": None}, "static-30d", now=pinned
    )
    assert a == b == c


def test_ttl_bucket_vintage_per_class():
    pinned = datetime(2026, 6, 7, 3, 30, 45, tzinfo=timezone.utc)
    assert ttl_bucket_vintage("static-30d", now=pinned) == "2026-06"
    assert ttl_bucket_vintage("semi-static-7d", now=pinned) == "2026-W23"
    assert ttl_bucket_vintage("dynamic-1h", now=pinned) == "2026-06-07T03:00:00Z"
    assert ttl_bucket_vintage("live-no-cache", now=pinned) == "live"


def test_cache_path_matches_job_0031_layout():
    """cache_path produces cache/<ttl-class>/<source-class>/<hash>.<ext>."""
    p = cache_path("dem", "static-30d", "abc123", "tif")
    assert p == "cache/static-30d/dem/abc123.tif"

    # Accepts ext with or without leading dot.
    p2 = cache_path("buildings", "semi-static-7d", "deadbeef", ".fgb")
    assert p2 == "cache/semi-static-7d/buildings/deadbeef.fgb"


@pytest.mark.parametrize(
    "ttl_class, cacheable, expected",
    [
        ("static-30d", True, True),
        ("semi-static-7d", True, True),
        ("dynamic-1h", True, True),
        ("live-no-cache", False, False),
    ],
)
def test_is_cacheable_per_ttl_class(ttl_class, cacheable, expected):
    md = AtomicToolMetadata(
        name="t",
        ttl_class=ttl_class,
        source_class="x" if cacheable else None,
        cacheable=cacheable,
    )
    assert is_cacheable(md) is expected


# ---------------------------------------------------------------------------
# read_through integration tests (S3-only — boto3 in-memory double)
# ---------------------------------------------------------------------------
#
# GCP is decommissioned: the read-through writes/reads via boto3 S3. These
# tests monkeypatch ``boto3.client`` to an in-memory double that models the
# subset of the S3 API the cache shim touches: ``get_object`` /
# ``put_object`` raising ``ClientError(NoSuchKey)`` on a miss.
# ---------------------------------------------------------------------------


class _FakeS3Client:
    """In-memory boto3 S3 double for the cache read-through path."""

    def __init__(self, store: dict[tuple[str, str], bytes]) -> None:
        self.store = store
        # Track the most recent put for inspection (content-type etc.).
        self.last_put: dict[str, Any] | None = None

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        from botocore.exceptions import ClientError

        try:
            data = self.store[(Bucket, Key)]
        except KeyError:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "not found"}},
                "GetObject",
            )
        return {"Body": _Body(data)}

    def put_object(
        self, *, Bucket: str, Key: str, Body: bytes, ContentType: str | None = None
    ) -> dict[str, Any]:
        self.store[(Bucket, Key)] = Body
        self.last_put = {
            "Bucket": Bucket,
            "Key": Key,
            "ContentType": ContentType,
        }
        return {}


class _Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


@pytest.fixture()
def fake_s3(monkeypatch) -> _FakeS3Client:
    """Monkeypatch ``boto3.client('s3', ...)`` to an in-memory double.

    The cache shim builds its S3 client lazily via ``boto3.client`` inside
    ``read_object_bytes_s3`` / ``_read_through_s3``; patching the factory
    routes every call through the shared in-memory store.
    """
    import boto3

    store: dict[tuple[str, str], bytes] = {}
    client = _FakeS3Client(store)

    def _factory(service_name: str, *args: Any, **kwargs: Any) -> _FakeS3Client:
        assert service_name == "s3"
        return client

    monkeypatch.setattr(boto3, "client", _factory)
    return client


def _cacheable_md() -> AtomicToolMetadata:
    return AtomicToolMetadata(
        name="fetch_demo",
        ttl_class="static-30d",
        source_class="demo",
        cacheable=True,
    )


_CACHE_BUCKET = "grace2-hazard-cache-226996537797"


def test_read_through_hit_returns_bytes_and_skips_fetch_fn(fake_s3):
    md = _cacheable_md()
    pinned = datetime(2026, 6, 7, 3, 0, 0, tzinfo=timezone.utc)

    # Pre-seed the cache at the path the shim will look up.
    key = compute_cache_key(
        md.source_class, {"bbox": [0, 0, 1, 1]}, md.ttl_class, now=pinned
    )
    path = cache_path(md.source_class, md.ttl_class, key, "tif")
    fake_s3.store[(_CACHE_BUCKET, path)] = b"cached-payload"

    invoked = {"n": 0}

    def fetch_fn() -> bytes:
        invoked["n"] += 1
        return b"FRESH"

    result = read_through(
        metadata=md,
        params={"bbox": [0, 0, 1, 1]},
        ext="tif",
        fetch_fn=fetch_fn,
        now=pinned,
    )

    assert result.hit is True
    assert result.data == b"cached-payload"
    assert result.uri == f"s3://{_CACHE_BUCKET}/{path}"
    assert invoked["n"] == 0  # fetch_fn not invoked on hit


def test_read_through_miss_writes_to_s3(fake_s3):
    md = _cacheable_md()
    pinned = datetime(2026, 6, 7, 3, 0, 0, tzinfo=timezone.utc)

    def fetch_fn() -> bytes:
        return b"freshly-fetched"

    result = read_through(
        metadata=md,
        params={"bbox": [0, 0, 1, 1]},
        ext="tif",
        fetch_fn=fetch_fn,
        now=pinned,
    )

    key = compute_cache_key(
        md.source_class, {"bbox": [0, 0, 1, 1]}, md.ttl_class, now=pinned
    )
    expected_path = cache_path(md.source_class, md.ttl_class, key, "tif")

    assert result.hit is False
    assert result.data == b"freshly-fetched"
    assert result.uri == f"s3://{_CACHE_BUCKET}/{expected_path}"
    # Persisted in the store at the expected path.
    assert fake_s3.store[(_CACHE_BUCKET, expected_path)] == b"freshly-fetched"
    # Content-type is inferred from the extension (.tif -> image/tiff).
    assert fake_s3.last_put is not None
    assert fake_s3.last_put["ContentType"] == "image/tiff"


def test_read_through_live_no_cache_skips_s3(fake_s3):
    """FR-DC-6: live-no-cache tools never touch the bucket."""
    md = AtomicToolMetadata(
        name="qgis_process",
        ttl_class="live-no-cache",
        source_class=None,
        cacheable=False,
    )
    invoked = {"n": 0}

    def fetch_fn() -> bytes:
        invoked["n"] += 1
        return b"live-data"

    result = read_through(
        metadata=md,
        params={"x": 1},
        ext="json",
        fetch_fn=fetch_fn,
    )

    assert result.hit is False
    assert result.data == b"live-data"
    assert result.uri is None
    assert invoked["n"] == 1
    # Nothing written to the bucket.
    assert fake_s3.store == {}


def test_read_through_force_refresh_bypasses_hit(fake_s3):
    """force_refresh=True invokes fetch_fn even when cache is populated."""
    md = _cacheable_md()
    pinned = datetime(2026, 6, 7, 3, 0, 0, tzinfo=timezone.utc)
    key = compute_cache_key(
        md.source_class, {"bbox": [0, 0, 1, 1]}, md.ttl_class, now=pinned
    )
    path = cache_path(md.source_class, md.ttl_class, key, "tif")
    fake_s3.store[(_CACHE_BUCKET, path)] = b"old-cached-payload"

    invoked = {"n": 0}

    def fetch_fn() -> bytes:
        invoked["n"] += 1
        return b"fresh-payload"

    result = read_through(
        metadata=md,
        params={"bbox": [0, 0, 1, 1]},
        ext="tif",
        fetch_fn=fetch_fn,
        now=pinned,
        force_refresh=True,
    )
    assert result.hit is False
    assert result.data == b"fresh-payload"
    assert invoked["n"] == 1
    # Fresh data has overwritten the old entry.
    assert fake_s3.store[(_CACHE_BUCKET, path)] == b"fresh-payload"


def test_read_through_fetch_failure_reraises_without_sentinel(fake_s3):
    """On fetch_fn failure: no sentinel written; exception bubbles."""
    md = _cacheable_md()

    class UpstreamUnavailable(RuntimeError):
        pass

    def fetch_fn() -> bytes:
        raise UpstreamUnavailable("3dep returned 503")

    with pytest.raises(UpstreamUnavailable):
        read_through(
            metadata=md,
            params={"bbox": [0, 0, 1, 1]},
            ext="tif",
            fetch_fn=fetch_fn,
        )
    # Nothing was written.
    assert fake_s3.store == {}
