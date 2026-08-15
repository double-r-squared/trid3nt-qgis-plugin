"""Cache shim — read-through / write-on-miss with content-addressed keys.

This module owns the agent-side cache shim that mediates every external-API
atomic-tool fetch. The shim is the SOLE writer of the ``cache/``
prefix on the production cache bucket provisioned:

    s3://<cache-bucket>/cache/<ttl-class>/<source-class>/<hash>.<ext>

(the bucket is ``CACHE_BUCKET`` below, overridable via ``TRID3NT_CACHE_BUCKET``
-- locally that points at the MinIO ``trid3nt-cache`` bucket)

Note the layout nests TTL class above source class (not source class
above TTL class) so the bucket's GCS Object Lifecycle Management policy
can run on FOUR rules forever instead of one-per-source-class.

Cache-key derivation:

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

Deduplication:
The content-addressed key guarantees two callers asking for the same input
produce the same path. No explicit lock is needed — last-writer-wins on
simultaneous misses produces byte-identical artifacts because the key
already factored in everything that would differ.

Cancellation:
``read_through`` is a blocking I/O call. It must be invoked from a context
that the agent's WebSocket cancel chain (server.py's message handler) can
cancel via ``asyncio.CancelledError``. Do NOT introduce a separate cancel
mechanism.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import logging
import os
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata, TTLClass

__all__ = [
    "CACHE_BUCKET",
    "CACHE_KEY_HEX_LEN",
    "compute_cache_key",
    "cache_path",
    "ttl_bucket_vintage",
    "is_cacheable",
    "read_through",
    "ReadThroughResult",
    "ProvenanceRecorder",
    "record_provenance",
]

logger = logging.getLogger("trid3nt_server.agent.tools.cache")

#: Production cache bucket name (AWS S3). Override via env var
#: ``TRID3NT_CACHE_BUCKET`` for non-prod runs.
CACHE_BUCKET = "trid3nt-cache"

#: Truncation length for the sha256 hex digest. 32 hex chars = 128 bits of
#: collision resistance — birthday-bound probability of collision after 2^64
#: keys is negligible for the workload described in §3.9. TENTATIVE per the
#: kickoff (longer narrows collision probability at the cost of path length).
CACHE_KEY_HEX_LEN = 32


def _canonicalize_params(params: dict[str, Any]) -> str:
    """Deterministic JSON serialization of the params dict.

    Rules (canonicalized_params):
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
    quantization rules belong in the engine-owned fetcher modules,
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
    """Compute the content-addressed cache key.

    Args:
        source_id: stable identifier for the upstream data source (often the
            ``source_class`` from the tool's ``AtomicToolMetadata``, possibly
            with sub-source detail like ``"atcf:IAN"``).
        params: the call parameters affecting the response. Caller is
            expected to have pre-quantized bbox / date ranges (source-native
            resolution for bbox, TTL bucket boundary for dates) before
            handing params to this function.
        ttl_class: one of the four TTL classes.
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

    Matches the LIVE bucket layout:
        ``cache/<ttl-class>/<source-class>/<key>.<ext>``

    NOT the flat ``cache/<source-class>/<hash>.<ext>`` layout; see
    module docstring for the rationale (4-rule lifecycle policy at scale).
    """
    ext_clean = ext.lstrip(".")
    return f"cache/{ttl_class}/{source_class}/{key}.{ext_clean}"


def is_cacheable(metadata: AtomicToolMetadata) -> bool:
    """Wrap the cacheable/TTL-class consistency check.

    A tool is cacheable iff ``metadata.cacheable`` is True AND its TTL class
    is not ``"live-no-cache"``. The ``AtomicToolMetadata`` model_validator
    enforces the consistency of these fields at construction time; this
    helper exists for call sites that prefer a positive boolean over an
    inline expression.
    """
    return metadata.cacheable and metadata.ttl_class != "live-no-cache"


# ---------------------------------------------------------------------------
# Fetch-time provenance channel — a cache-replayable sidecar from fetch to
# envelope.
#
# Some fetch-time facts are UNRECOVERABLE from the final cached bytes: which of
# a multi-source composite's legs actually painted a merged COG, how many tiles
# contributed, whether a leg silently degraded. The single-band float32 topobathy
# COG carries no per-source attribution, and on a cache HIT ``read_through`` never
# calls ``fetch_fn`` (the executor that would recompute those facts does not run).
#
# This is the general, MINIMAL channel that closes that gap: during a
# NON-cached fetch the executor/delegate records a small typed provenance dict via
# :func:`record_provenance`; ``read_through`` persists it as a SIBLING object next
# to the cached artifact (``<key>.provenance.json``); on EVERY return (fresh OR a
# cache hit) the recorder carries the SAME provenance the original fetch recorded,
# which the router hands to the envelope hook. Strictly ADDITIVE: a caller that
# passes no recorder is byte-identical to before (no sidecar object, no extra I/O),
# so every prior spec is unaffected. Size-bounded (a small dict) and NEVER
# secret-bearing (source-attribution counts + honest warnings only).
# ---------------------------------------------------------------------------


class ProvenanceRecorder:
    """A single-slot sink an executor writes fetch-time provenance into.

    ``data`` is ``None`` until :func:`record_provenance` (called by the delegate
    during a fresh fetch) fills it, OR ``read_through`` replays it from the
    persisted sidecar on a cache hit. The router creates one per provenance-enabled
    ``route()`` call and reads ``.data`` back for the envelope hook.
    """

    __slots__ = ("data",)

    def __init__(self) -> None:
        self.data: dict[str, Any] | None = None


#: The recorder bound for the CURRENT fetch (contextvar so a nested delegate call
#: reaches it without threading it through the ``fetch_fn`` byte-only signature).
_ACTIVE_RECORDER: contextvars.ContextVar[ProvenanceRecorder | None] = (
    contextvars.ContextVar("trid3nt_provenance_recorder", default=None)
)


def record_provenance(data: dict[str, Any]) -> None:
    """Record a fetch-time provenance dict for the artifact being produced.

    Called by an executor/delegate DURING a fresh fetch. A strict no-op when no
    recorder is bound (an uninstrumented call path), so it is always safe to call.
    The dict must be small, JSON-serializable, and carry NO secret values.
    """
    rec = _ACTIVE_RECORDER.get()
    if rec is not None:
        rec.data = dict(data)


@contextlib.contextmanager
def _bind_recorder(recorder: ProvenanceRecorder | None) -> Iterator[None]:
    """Bind ``recorder`` as the active provenance sink for the enclosed fetch."""
    if recorder is None:
        yield
        return
    token = _ACTIVE_RECORDER.set(recorder)
    try:
        yield
    finally:
        _ACTIVE_RECORDER.reset(token)


def _sidecar_key(obj_key: str) -> str:
    """The provenance sidecar object key sitting next to ``<key>.<ext>``."""
    stem = obj_key.rsplit(".", 1)[0]
    return f"{stem}.provenance.json"


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
        provenance: the fetch-time provenance dict when a
            :class:`ProvenanceRecorder` was passed -- the SAME dict on a fresh
            fetch or a cache-hit replay -- else ``None``.
    """

    __slots__ = ("uri", "data", "hit", "provenance")

    def __init__(
        self,
        uri: str | None,
        data: bytes,
        hit: bool,
        provenance: dict[str, Any] | None = None,
    ) -> None:
        self.uri = uri
        self.data = data
        self.hit = hit
        self.provenance = provenance

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
    """Read an ``s3://`` object fully into memory via boto3.

    Shared by every tool download-helper so the per-tool ``gs://`` staging
    paths gain s3 support with a one-line guard. boto3 (NOT s3fs) per the
     lesson: s3fs/aiobotocore falls back to anonymous on the EC2
    instance role."""
    import boto3

    bucket, obj_key = _split_s3_uri(uri)
    s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-west-2"))
    return s3.get_object(Bucket=bucket, Key=obj_key)["Body"].read()


def _read_sidecar_s3(s3: Any, bucket: str, obj_key: str) -> dict[str, Any] | None:
    """Best-effort read of the provenance sidecar next to ``obj_key``.

    Returns the parsed dict, or ``None`` when absent / unreadable -- an object
    cached before the channel existed simply has no sidecar, so the envelope hook
    falls back to its declared defaults (additive, no regression)."""
    from botocore.exceptions import ClientError

    try:
        resp = s3.get_object(Bucket=bucket, Key=_sidecar_key(obj_key))
        return json.loads(resp["Body"].read().decode("utf-8"))
    except ClientError:
        return None
    except Exception as exc:  # noqa: BLE001 -- a malformed sidecar never blocks the read
        logger.warning("read_through provenance sidecar read degraded: %s", exc)
        return None


def _write_sidecar_s3(s3: Any, bucket: str, obj_key: str, provenance: dict[str, Any]) -> None:
    """Best-effort write of the provenance sidecar next to ``obj_key``."""
    try:
        s3.put_object(
            Bucket=bucket,
            Key=_sidecar_key(obj_key),
            Body=json.dumps(provenance, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            ContentType="application/json",
        )
    except Exception as exc:  # noqa: BLE001 -- write is best-effort (the layer still resolves)
        logger.warning("read_through provenance sidecar write degraded: %s", exc)


def _read_through_s3(
    uri: str,
    fetch_fn: Any,
    force_refresh: bool,
    metadata: Any,
    key: str,
    ext: str,
    provenance: "ProvenanceRecorder | None" = None,
) -> "ReadThroughResult":
    """S3 read-through via **boto3**.

    boto3 reliably resolves the EC2 instance-role credentials via IMDS; s3fs/
    aiobotocore fell back to anonymous here ("No AWSAccessKey was presented").
    Best-effort like the GCS path: any storage failure degrades to
    fetch-fresh-uncached. S3 TTL eviction is a bucket lifecycle rule, so no
    per-object customTime is written.

    When a :class:`ProvenanceRecorder` is passed, the fetch-time
    provenance rides alongside the artifact: on a HIT it is replayed from the
    ``<key>.provenance.json`` sidecar; on a MISS the recorder is bound around
    ``fetch_fn`` (so the delegate's :func:`record_provenance` fills it) and the
    result is persisted as the sidecar. Strictly no-op when ``provenance`` is None."""
    import boto3
    from botocore.exceptions import ClientError

    bucket, obj_key = _split_s3_uri(uri)
    s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-west-2"))
    if not force_refresh:
        try:
            resp = s3.get_object(Bucket=bucket, Key=obj_key)
            data = resp["Body"].read()
            logger.info("read_through hit (s3) tool=%s key=%s bytes=%d", metadata.name, key, len(data))
            prov = _read_sidecar_s3(s3, bucket, obj_key) if provenance is not None else None
            if provenance is not None:
                provenance.data = prov
            return ReadThroughResult(uri=uri, data=data, hit=True, provenance=prov)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code not in ("NoSuchKey", "404", "NoSuchBucket"):
                logger.warning("read_through s3 read degraded tool=%s: %s", metadata.name, exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("read_through s3 read degraded tool=%s: %s", metadata.name, exc)

    with _bind_recorder(provenance):
        data = fetch_fn()
    content_type = {
        "json": "application/json", "geojson": "application/json",
        "tif": "image/tiff", "fgb": "application/octet-stream",
        "nc": "application/x-netcdf", "grib2": "application/x-grib2",
    }.get(ext.lstrip("."), "application/octet-stream")
    try:
        s3.put_object(Bucket=bucket, Key=obj_key, Body=data, ContentType=content_type)
        logger.info("read_through miss-write (s3) tool=%s key=%s bytes=%d", metadata.name, key, len(data))
        if provenance is not None and provenance.data is not None:
            _write_sidecar_s3(s3, bucket, obj_key, provenance.data)
    except Exception as exc:  # noqa: BLE001 — write is best-effort
        logger.warning("read_through s3 write degraded tool=%s: %s; returning uncached", metadata.name, exc)
    prov = provenance.data if provenance is not None else None
    return ReadThroughResult(uri=uri, data=data, hit=False, provenance=prov)


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
    provenance: "ProvenanceRecorder | None" = None,
) -> ReadThroughResult:
    """Read-through / write-on-miss shim for one atomic-tool fetch.

    Flow:

    1. If ``metadata.cacheable`` is False / ``ttl_class == "live-no-cache"``:
       always miss; invoke ``fetch_fn``; do NOT write; return with
       ``uri=None``, ``hit=False``.
    2. Otherwise: compute cache key + path. Look up
       ``s3://<bucket>/<cache_path>``. If present, return the URI + bytes.
       The bucket lifecycle policy handles eviction so presence == valid.
    3. On miss (or ``force_refresh=True``): invoke ``fetch_fn()``; write the
       fresh bytes to S3 via boto3; return URI + bytes. TTL eviction is a
       bucket lifecycle rule, so no per-object expiry metadata is written.
    4. On ``fetch_fn`` failure: do NOT write a sentinel; re-raise so the
       agent surface can decide whether to retry, clarify, or
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
            ``fetch_fn`` (the ``cache=false`` per-call opt-in). The
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
    # the env override WINS over caller-supplied
    # buckets — several tools pass the legacy CACHE_BUCKET constant explicitly,
    # which on AWS named a nonexistent GCP bucket and silently degraded every
    # cache write (observed live: hillshade COG upload). Tests run with the
    # env unset, so explicit-bucket test fixtures are unaffected.
    bucket = os.environ.get("TRID3NT_CACHE_BUCKET") or bucket or CACHE_BUCKET
    source_id = source_id or (metadata.source_class or metadata.name)

    # Uncacheable-tools short-circuit: they never touch the bucket. The
    # provenance recorder still binds around the fetch so an uncacheable source can
    # populate result-model fields (no sidecar persisted -- nothing to replay).
    if not is_cacheable(metadata):
        with _bind_recorder(provenance):
            data = fetch_fn()
        logger.info(
            "read_through live-no-cache tool=%s bytes=%d", metadata.name, len(data)
        )
        prov = provenance.data if provenance is not None else None
        return ReadThroughResult(uri=None, data=data, hit=False, provenance=prov)

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
    return _read_through_s3(uri, fetch_fn, force_refresh, metadata, key, ext, provenance)
