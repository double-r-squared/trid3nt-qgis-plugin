"""FR-DC-3 cache shim — read-through / write-on-miss with content-addressed keys.

This module owns the agent-side cache shim that mediates every external-API
atomic-tool fetch (FR-CE-8). The shim is the SOLE writer of the ``cache/``
prefix on the production cache bucket provisioned by job-0031:

    gs://grace-2-hazard-prod-cache/cache/<ttl-class>/<source-class>/<hash>.<ext>

Note the layout follows the LIVE substrate from job-0031, NOT the FR-DC-1
literal (``cache/<source-class>/<hash>.<ext>``). job-0031 nested TTL class
above source class so the bucket's GCS Object Lifecycle Management policy
can run on FOUR rules forever instead of one-per-source-class. The
``OQ-INFRA-31-FR-DC-1`` schema-pushback proposes the matching SRS amendment.

Cache-key derivation (FR-DC-3):

    key = sha256(source_id || canonical_params_json || ttl_bucket_vintage)[:32]

- ``canonical_params_json`` sorts keys, omits ``None``/default values, and
  quantizes ranges (bbox to source-native resolution if a hint is passed,
  dates to the TTL bucket boundary).
- ``ttl_bucket_vintage`` is the current TTL-class window boundary:
  - ``static-30d`` -> ``"2026-06"`` (year-month)
  - ``semi-static-7d`` -> ``"2026-W23"`` (ISO year-week)
  - ``dynamic-1h`` -> ``"2026-06-07T03:00:00Z"`` (top-of-hour UTC)
  - ``live-no-cache`` -> ``"live"`` placeholder (read_through short-circuits
    so the key never lands in GCS, but compute_cache_key remains pure).

Deduplication (FR-DC-4):
The content-addressed key guarantees two callers asking for the same input
produce the same path. No explicit lock is needed — last-writer-wins on
simultaneous misses produces byte-identical artifacts because the key
already factored in everything that would differ.

Cancellation (Invariant 8):
``read_through`` is a blocking I/O call. It must be invoked from a context
that the agent's WebSocket cancel chain (server.py M1 handler) can cancel
via ``asyncio.CancelledError``. Do NOT introduce a separate cancel mechanism.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from grace2_contracts.tool_registry import AtomicToolMetadata, TTLClass

__all__ = [
    "CACHE_BUCKET",
    "CACHE_KEY_HEX_LEN",
    "compute_cache_key",
    "cache_path",
    "ttl_bucket_vintage",
    "is_cacheable",
    "read_through",
    "ReadThroughResult",
]

logger = logging.getLogger("grace2_agent.tools.cache")

#: Production cache bucket name (AWS S3). Override via env var
#: ``GRACE2_CACHE_BUCKET`` for non-prod runs.
CACHE_BUCKET = "grace2-hazard-cache-226996537797"

#: Truncation length for the sha256 hex digest. 32 hex chars = 128 bits of
#: collision resistance — birthday-bound probability of collision after 2^64
#: keys is negligible for the workload described in §3.9. TENTATIVE per the
#: kickoff (longer narrows collision probability at the cost of path length).
CACHE_KEY_HEX_LEN = 32


def _canonicalize_params(params: dict[str, Any]) -> str:
    """Deterministic JSON serialization of the params dict.

    Rules (FR-DC-3 canonicalized_params):
    - Sort keys.
    - Omit ``None`` values (treat-as-default).
    - No whitespace ('separators=(",", ":")' for compactness + determinism).
    - ``default=str`` so datetimes / Decimal / etc. serialize stably without
      the caller having to pre-format them. (This is intentionally lenient —
      a caller passing an unhashable object gets a stable string-form rather
      than a TypeError; the shim's contract is determinism, not type purity.)

    NOTE: The kickoff calls out bbox-to-source-native-resolution quantization
    and date-range-to-TTL-bucket-boundary quantization. Those are domain-
    specific transformations the CALLER applies before handing the params
    dict to the shim — the shim only canonicalizes whatever it receives. This
    keeps the shim engine-agnostic; the bbox-resolution table and the date-
    quantization rules belong in the engine-owned fetcher modules (job-0033),
    not in the agent's cache surface.
    """
    pruned = {k: v for k, v in params.items() if v is not None}
    return json.dumps(pruned, sort_keys=True, separators=(",", ":"), default=str)


def ttl_bucket_vintage(ttl_class: TTLClass, now: datetime | None = None) -> str:
    """Return the current TTL-class window-boundary string.

    For each TTL class, two calls inside the same window produce the same
    vintage string and thus the same cache key; a boundary crossing forces a
    refresh. The window boundary is computed in UTC.

    - ``static-30d`` -> ``YYYY-MM`` (year-month — coarse but the lifecycle
      policy evicts after 30 days regardless, so per-month bucketing keeps
      keys stable for the entire month and lets the eviction policy do its
      job. Slightly more reuse than per-day; well under 30-day eviction.)
    - ``semi-static-7d`` -> ``YYYY-Www`` (ISO year-week).
    - ``dynamic-1h`` -> top-of-hour UTC ISO-Z (``YYYY-MM-DDTHH:00:00Z``).
    - ``live-no-cache`` -> the literal ``"live"`` (never lands in GCS; see
      ``read_through`` which short-circuits).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if ttl_class == "static-30d":
        return now.strftime("%Y-%m")
    if ttl_class == "semi-static-7d":
        iso_year, iso_week, _ = now.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"
    if ttl_class == "dynamic-1h":
        top_of_hour = now.replace(minute=0, second=0, microsecond=0)
        return top_of_hour.strftime("%Y-%m-%dT%H:00:00Z")
    if ttl_class == "live-no-cache":
        return "live"
    raise ValueError(f"unknown ttl_class: {ttl_class!r}")


def compute_cache_key(
    source_id: str,
    params: dict[str, Any],
    ttl_class: TTLClass,
    *,
    now: datetime | None = None,
) -> str:
    """Compute the content-addressed cache key per FR-DC-3.

    Args:
        source_id: stable identifier for the upstream data source (often the
            ``source_class`` from the tool's ``AtomicToolMetadata``, possibly
            with sub-source detail like ``"atcf:IAN"``).
        params: the call parameters affecting the response. Caller is
            expected to have pre-quantized bbox / date ranges per the
            domain-specific rules in §3.9 / FR-DC-3.
        ttl_class: one of the four FR-DC-2 classes.
        now: time of fetch (default: now UTC). Tests pin this for determinism
            across runs.

    Returns:
        A 32-hex-char prefix of the SHA-256 digest. Same inputs (including
        TTL-bucket vintage) ALWAYS produce the same key; a TTL-bucket-boundary
        crossing changes the vintage and therefore the key.
    """
    vintage = ttl_bucket_vintage(ttl_class, now=now)
    canonical = _canonicalize_params(params)
    raw = f"{source_id}||{canonical}||{vintage}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return digest[:CACHE_KEY_HEX_LEN]


def cache_path(source_class: str, ttl_class: TTLClass, key: str, ext: str) -> str:
    """Construct the object path under the cache bucket.

    Matches the job-0031 LIVE bucket layout:
        ``cache/<ttl-class>/<source-class>/<key>.<ext>``

    NOT the FR-DC-1 literal (``cache/<source-class>/<hash>.<ext>``); see
    module docstring for the rationale (4-rule lifecycle policy at scale).
    """
    ext_clean = ext.lstrip(".")
    return f"cache/{ttl_class}/{source_class}/{key}.{ext_clean}"


def is_cacheable(metadata: AtomicToolMetadata) -> bool:
    """Wrap the FR-DC-6 enumeration check.

    A tool is cacheable iff ``metadata.cacheable`` is True AND its TTL class
    is not ``"live-no-cache"``. The ``AtomicToolMetadata`` model_validator
    enforces the consistency of these fields at construction time; this
    helper exists for call sites that prefer a positive boolean over an
    inline expression.
    """
    return metadata.cacheable and metadata.ttl_class != "live-no-cache"


# ---------------------------------------------------------------------------
# read_through — the read-through / write-on-miss entry point.
# ---------------------------------------------------------------------------


class ReadThroughResult:
    """Result of a ``read_through`` call.

    Attributes:
        uri: ``s3://bucket/path`` of the cached artifact, or ``None`` for
            ``live-no-cache`` reads which deliberately do not persist.
        data: the artifact bytes (from the cache hit or freshly fetched).
        hit: True if the response came from the cache, False if fetched.
    """

    __slots__ = ("uri", "data", "hit")

    def __init__(self, uri: str | None, data: bytes, hit: bool) -> None:
        self.uri = uri
        self.data = data
        self.hit = hit

    def __repr__(self) -> str:  # pragma: no cover — diagnostic
        return f"ReadThroughResult(uri={self.uri!r}, hit={self.hit}, bytes={len(self.data)})"


def storage_scheme() -> str:
    """Object-store scheme for cache artifacts.

    GCP is decommissioned: the agent's only object store is AWS S3, so this
    always resolves to ``"s3"``. Kept as a function (rather than a constant)
    because ``publish_layer`` and other call sites import it as the
    single source of truth for the cache-URI scheme.
    """
    return "s3"


def _obj_uri(bucket: str, path: str) -> str:
    return f"s3://{bucket}/{path}"


def _split_s3_uri(uri: str) -> tuple[str, str]:
    rest = uri[len("s3://"):]
    bucket, _, obj_key = rest.partition("/")
    return bucket, obj_key


def read_object_bytes_s3(uri: str) -> bytes:
    """Read an ``s3://`` object fully into memory via boto3 (sprint-14-aws).

    Shared by every tool download-helper so the per-tool ``gs://`` staging
    paths gain s3 support with a one-line guard. boto3 (NOT s3fs) per the
    job-0289 lesson: s3fs/aiobotocore falls back to anonymous on the EC2
    instance role."""
    import boto3

    bucket, obj_key = _split_s3_uri(uri)
    s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-west-2"))
    return s3.get_object(Bucket=bucket, Key=obj_key)["Body"].read()


def _read_through_s3(
    uri: str, fetch_fn: Any, force_refresh: bool, metadata: Any, key: str, ext: str
) -> "ReadThroughResult":
    """S3 read-through via **boto3** (sprint-14-aws job-0289).

    boto3 reliably resolves the EC2 instance-role credentials via IMDS; s3fs/
    aiobotocore fell back to anonymous here ("No AWSAccessKey was presented").
    Best-effort like the GCS path (job-0288c): any storage failure degrades to
    fetch-fresh-uncached. S3 TTL eviction is a bucket lifecycle rule, so no
    per-object customTime is written."""
    import boto3
    from botocore.exceptions import ClientError

    bucket, obj_key = _split_s3_uri(uri)
    s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-west-2"))
    if not force_refresh:
        try:
            resp = s3.get_object(Bucket=bucket, Key=obj_key)
            data = resp["Body"].read()
            logger.info("read_through hit (s3) tool=%s key=%s bytes=%d", metadata.name, key, len(data))
            return ReadThroughResult(uri=uri, data=data, hit=True)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code not in ("NoSuchKey", "404", "NoSuchBucket"):
                logger.warning("read_through s3 read degraded tool=%s: %s", metadata.name, exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("read_through s3 read degraded tool=%s: %s", metadata.name, exc)

    data = fetch_fn()
    content_type = {
        "json": "application/json", "geojson": "application/json",
        "tif": "image/tiff", "fgb": "application/octet-stream",
        "nc": "application/x-netcdf", "grib2": "application/x-grib2",
    }.get(ext.lstrip("."), "application/octet-stream")
    try:
        s3.put_object(Bucket=bucket, Key=obj_key, Body=data, ContentType=content_type)
        logger.info("read_through miss-write (s3) tool=%s key=%s bytes=%d", metadata.name, key, len(data))
    except Exception as exc:  # noqa: BLE001 — write is best-effort
        logger.warning("read_through s3 write degraded tool=%s: %s; returning uncached", metadata.name, exc)
    return ReadThroughResult(uri=uri, data=data, hit=False)


def read_through(
    metadata: AtomicToolMetadata,
    params: dict[str, Any],
    ext: str,
    fetch_fn: Callable[[], bytes],
    *,
    bucket: str | None = None,
    source_id: str | None = None,
    force_refresh: bool = False,
    storage_client: Any | None = None,
    now: datetime | None = None,
) -> ReadThroughResult:
    """Read-through / write-on-miss shim for one atomic-tool fetch.

    Flow per FR-DC-3:

    1. If ``metadata.cacheable`` is False / ``ttl_class == "live-no-cache"``:
       always miss; invoke ``fetch_fn``; do NOT write; return with
       ``uri=None``, ``hit=False``. This honors FR-DC-6.
    2. Otherwise: compute cache key + path. Look up
       ``s3://<bucket>/<cache_path>``. If present, return the URI + bytes.
       The bucket lifecycle policy handles eviction so presence == valid.
    3. On miss (or ``force_refresh=True``): invoke ``fetch_fn()``; write the
       fresh bytes to S3 via boto3; return URI + bytes. TTL eviction is a
       bucket lifecycle rule, so no per-object expiry metadata is written.
    4. On ``fetch_fn`` failure: do NOT write a sentinel; re-raise so the
       agent surface (FR-AS-11) can decide whether to retry, clarify, or
       fall back.

    Args:
        metadata: the tool's registered ``AtomicToolMetadata``.
        params: the call parameters (already domain-quantized).
        ext: artifact extension (e.g. ``"tif"``, ``"fgb"``, ``"json"``).
        fetch_fn: a zero-arg callable that produces the fresh bytes. The
            shim is sync because boto3 S3 uploads are sync; long-running
            fetches must be invoked from a context that the agent's cancel
            chain can interrupt.
        bucket: cache bucket name (default ``CACHE_BUCKET``).
        source_id: identifier for the upstream source, defaults to
            ``metadata.source_class``. Pass an override for sub-source detail
            like ``"atcf:IAN"``.
        force_refresh: if True, bypass the cache lookup and always invoke
            ``fetch_fn`` (FR-DC-6 ``cache=false`` per-call opt-in). The
            fresh response is still written through.
        storage_client: legacy/no-op parameter retained for backward
            compatibility with the many tool call sites that thread a
            ``_storage_client`` kwarg through. GCP is decommissioned, so the
            read-through always routes through boto3/S3; this argument is
            ignored.
        now: optional timestamp pin for tests / TTL-bucket determinism.

    Returns:
        ``ReadThroughResult(uri, data, hit)``.
    """
    del storage_client  # GCP decommissioned — S3-only read-through.
    # sprint-14-aws (job-0290b): the env override WINS over caller-supplied
    # buckets — several tools pass the legacy CACHE_BUCKET constant explicitly,
    # which on AWS named a nonexistent GCP bucket and silently degraded every
    # cache write (observed live: hillshade COG upload). Tests run with the
    # env unset, so explicit-bucket test fixtures are unaffected.
    bucket = os.environ.get("GRACE2_CACHE_BUCKET") or bucket or CACHE_BUCKET
    source_id = source_id or (metadata.source_class or metadata.name)

    # FR-DC-6 short-circuit: uncacheable tools never touch the bucket.
    if not is_cacheable(metadata):
        data = fetch_fn()
        logger.info(
            "read_through live-no-cache tool=%s bytes=%d", metadata.name, len(data)
        )
        return ReadThroughResult(uri=None, data=data, hit=False)

    # source_class is guaranteed non-empty for cacheable tools by the
    # AtomicToolMetadata cross-field validator; assert defensively.
    if not metadata.source_class:
        raise ValueError(
            f"cacheable tool {metadata.name!r} has no source_class — model_validator "
            "should have caught this; refusing to write under cache/<None>/."
        )

    key = compute_cache_key(source_id, params, metadata.ttl_class, now=now)
    path = cache_path(metadata.source_class, metadata.ttl_class, key, ext)

    # GCP is decommissioned: the cache lives in S3. The whole read-through
    # routes through boto3 and mints an ``s3://`` URI. The legacy
    # ``from google.cloud import storage`` default-client builder is GONE —
    # google-cloud-storage is no longer an agent dependency.
    uri = f"s3://{bucket}/{path}"
    return _read_through_s3(uri, fetch_fn, force_refresh, metadata, key, ext)
