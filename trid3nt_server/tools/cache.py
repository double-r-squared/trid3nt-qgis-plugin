"""Read-through / write-on-miss cache shim; the sole writer of the ``cache/``
prefix, at ``cache/<ttl-class>/<source-class>/<key>.<ext>``. Keys are content-
addressed, so simultaneous misses converge on byte-identical artifacts and no
lock is needed. ``read_through`` blocks: it must be called from a context the
cancel chain can interrupt via ``asyncio.CancelledError``."""

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
    "PROVENANCE_SCHEMA",
    "sidecar_is_current",
]

logger = logging.getLogger("trid3nt_server.tools.cache")

#: Production cache bucket name (AWS S3). Override via env var
#: ``TRID3NT_CACHE_BUCKET`` for non-prod runs.
CACHE_BUCKET = "trid3nt-cache"

#: Truncation length for the sha256 hex digest. 32 hex chars = 128 bits, so the
#: birthday-bound collision probability is negligible at this key volume; a longer
#: prefix narrows it further at the cost of path length.
CACHE_KEY_HEX_LEN = 32


def _canonicalize_params(params: dict[str, Any]) -> str:
    """Deterministic JSON for the params dict: sorted, ``None`` pruned, compact.
    ``default=str`` is deliberate - an unserializable value gets a stable string
    form rather than a TypeError; the contract is determinism, not type purity."""
    pruned = {k: v for k, v in params.items() if v is not None}
    return json.dumps(pruned, sort_keys=True, separators=(",", ":"), default=str)


def ttl_bucket_vintage(ttl_class: TTLClass, now: datetime | None = None) -> str:
    """The TTL class's current window boundary, in UTC. Two calls inside one
    window produce the same string and therefore the same cache key; crossing the
    boundary forces a refresh. ``live-no-cache`` yields the literal ``"live"``."""
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
    """A 32-hex-char SHA-256 prefix over source id, canonical params and the TTL
    vintage. ``params`` must already be domain-quantized by the CALLER (bbox to
    source-native resolution, dates to the TTL boundary) - the shim never does."""
    vintage = ttl_bucket_vintage(ttl_class, now=now)
    canonical = _canonicalize_params(params)
    raw = f"{source_id}||{canonical}||{vintage}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return digest[:CACHE_KEY_HEX_LEN]


def cache_path(source_class: str, ttl_class: TTLClass, key: str, ext: str) -> str:
    """Object path under the cache bucket, TTL class above source class:
    ``cache/<ttl-class>/<source-class>/<key>.<ext>``. The bucket's lifecycle rules
    are written against that nesting."""
    ext_clean = ext.lstrip(".")
    return f"cache/{ttl_class}/{source_class}/{key}.{ext_clean}"


def is_cacheable(metadata: AtomicToolMetadata) -> bool:
    """True iff ``metadata.cacheable`` and the TTL class is not
    ``"live-no-cache"``; ``AtomicToolMetadata`` enforces that pairing at
    construction."""
    return metadata.cacheable and metadata.ttl_class != "live-no-cache"


# ---------------------------------------------------------------------------
# Fetch-time provenance channel: a cache-replayable sidecar from fetch to envelope.
#
# Some fetch-time facts are UNRECOVERABLE from the cached bytes - which of a
# composite's legs actually painted a merged COG, how many tiles contributed,
# whether a leg silently degraded. A single-band float32 COG carries no per-source
# attribution, and on a cache HIT ``read_through`` never calls ``fetch_fn``, so
# nothing recomputes them. During a NON-cached fetch the executor records a small
# typed dict via :func:`record_provenance`; ``read_through`` persists it as a
# SIBLING object (``<key>.provenance.json``) and replays it on every later hit, so
# a fresh return and a hit carry the SAME provenance. The dict must stay small and
# never secret-bearing.
#
# The sidecar carries ``PROVENANCE_SCHEMA``. A cached artifact whose sidecar
# predates it is a MISS: an artifact's honesty lives in its provenance, so a stale
# sidecar would keep serving a stale account of the bytes for the rest of the TTL
# bucket - the exact way a landed fix fails to reach an already-cached AOI.
# ---------------------------------------------------------------------------


class ProvenanceRecorder:
    """Single-slot sink for fetch-time provenance. ``data`` stays ``None`` until
    :func:`record_provenance` fills it during a fresh fetch, or ``read_through``
    replays it from the persisted sidecar on a cache hit."""

    __slots__ = ("data",)

    def __init__(self) -> None:
        self.data: dict[str, Any] | None = None


#: The recorder bound for the CURRENT fetch (contextvar so a nested delegate call
#: reaches it without threading it through the ``fetch_fn`` byte-only signature).
_ACTIVE_RECORDER: contextvars.ContextVar[ProvenanceRecorder | None] = (
    contextvars.ContextVar("trid3nt_provenance_recorder", default=None)
)


#: Stamped into every recorded provenance dict and REQUIRED of every replayed
#: sidecar; an older-schema sidecar makes the cached object a MISS. BUMP whenever
#: a provenance field becomes load-bearing for honesty.
PROVENANCE_SCHEMA = 3

#: The sidecar key carrying :data:`PROVENANCE_SCHEMA`.
_SCHEMA_FIELD = "provenance_schema"


def record_provenance(data: dict[str, Any]) -> None:
    """Record fetch-time provenance for the artifact being produced. A strict
    no-op when no recorder is bound, so it is always safe to call; the dict must
    be small, JSON-serializable and carry NO secret values."""
    rec = _ACTIVE_RECORDER.get()
    if rec is not None:
        rec.data = {**data, _SCHEMA_FIELD: PROVENANCE_SCHEMA}


def sidecar_is_current(prov: dict[str, Any] | None) -> bool:
    """Whether a replayed provenance sidecar was written by the CURRENT schema."""
    return isinstance(prov, dict) and prov.get(_SCHEMA_FIELD) == PROVENANCE_SCHEMA


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


class _StaleProvenance(Exception):
    """Internal: the cached object's sidecar predates :data:`PROVENANCE_SCHEMA`."""


def _sidecar_key(obj_key: str) -> str:
    """The provenance sidecar object key sitting next to ``<key>.<ext>``."""
    stem = obj_key.rsplit(".", 1)[0]
    return f"{stem}.provenance.json"


# ---------------------------------------------------------------------------
# read_through - the read-through / write-on-miss entry point.
# ---------------------------------------------------------------------------


class ReadThroughResult:
    """One ``read_through`` return. ``uri`` is ``None`` for ``live-no-cache``
    reads, which deliberately do not persist; ``provenance`` is the SAME dict on a
    fresh fetch and on a cache-hit replay, or ``None`` with no recorder."""

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

    def __repr__(self) -> str:  # pragma: no cover - diagnostic
        return f"ReadThroughResult(uri={self.uri!r}, hit={self.hit}, bytes={len(self.data)})"


def storage_scheme() -> str:
    """The one source of truth for the cache-URI scheme; call sites import this
    rather than hard-coding it."""
    return "s3"


def _obj_uri(bucket: str, path: str) -> str:
    return f"s3://{bucket}/{path}"


def _split_s3_uri(uri: str) -> tuple[str, str]:
    rest = uri[len("s3://"):]
    bucket, _, obj_key = rest.partition("/")
    return bucket, obj_key


def read_object_bytes_s3(uri: str) -> bytes:
    """Read an ``s3://`` object fully into memory through the ONE store seam.

    Shared by every tool download-helper. It delegates rather than building its
    own client so a test that injects one client sees every read - boto3, never
    s3fs, because s3fs falls back to anonymous access and returns corrupt bytes.
    """
    from trid3nt_server.workflows.solver.solver import _read_object_bytes

    return _read_object_bytes(uri)


def _read_sidecar_s3(s3: Any, bucket: str, obj_key: str) -> dict[str, Any] | None:
    """The parsed sidecar dict, or ``None`` when absent or unreadable - an object
    cached before the channel existed simply has no sidecar."""
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
    """S3 read-through via boto3; any storage failure degrades to
    fetch-fresh-uncached rather than raising. With a :class:`ProvenanceRecorder`
    the sidecar is replayed on a hit and written on a miss; without one, no-op."""
    from botocore.exceptions import ClientError

    from trid3nt_server.workflows.solver.solver import _get_s3_client

    bucket, obj_key = _split_s3_uri(uri)
    s3 = _get_s3_client()
    if not force_refresh:
        try:
            resp = s3.get_object(Bucket=bucket, Key=obj_key)
            data = resp["Body"].read()
            prov = _read_sidecar_s3(s3, bucket, obj_key) if provenance is not None else None
            if provenance is not None and not sidecar_is_current(prov):
                # A provenance-bearing source whose sidecar predates the current
                # schema: REFETCH. Replaying it would hand back the stale fetch's
                # own claims about the bytes (which legs painted, which warning
                # was owed) for the rest of the TTL bucket, so a fix to those
                # claims would not reach a cached AOI at all.
                logger.warning(
                    "read_through provenance sidecar STALE (schema %r != %d) "
                    "tool=%s key=%s -- treating the cached object as a MISS",
                    (prov or {}).get(_SCHEMA_FIELD), PROVENANCE_SCHEMA,
                    metadata.name, key,
                )
                raise _StaleProvenance
            logger.info("read_through hit (s3) tool=%s key=%s bytes=%d", metadata.name, key, len(data))
            if provenance is not None:
                provenance.data = prov
            return ReadThroughResult(uri=uri, data=data, hit=True, provenance=prov)
        except _StaleProvenance:
            pass
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
    except Exception as exc:  # noqa: BLE001 - write is best-effort
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
    """Read-through / write-on-miss for one atomic-tool fetch. An uncacheable tool
    always misses and returns ``uri=None`` without writing; a ``fetch_fn`` failure
    is re-raised rather than cached as a sentinel, so the caller decides whether to
    retry or fall back. ``storage_client`` is accepted and ignored. Sync, and
    blocking: call it where the cancel chain can interrupt it."""
    del storage_client
    # The env override WINS over a caller-supplied bucket: several tools pass the
    # CACHE_BUCKET constant explicitly, and an explicit wrong bucket degrades every
    # cache write silently. Tests run with the env unset, so explicit-bucket
    # fixtures are unaffected.
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

    # source_class is guaranteed non-empty for a cacheable tool by the
    # AtomicToolMetadata cross-field validator; assert defensively.
    if not metadata.source_class:
        raise ValueError(
            f"cacheable tool {metadata.name!r} has no source_class — model_validator "
            "should have caught this; refusing to write under cache/<None>/."
        )

    key = compute_cache_key(source_id, params, metadata.ttl_class, now=now)
    path = cache_path(metadata.source_class, metadata.ttl_class, key, ext)

    uri = f"s3://{bucket}/{path}"
    return _read_through_s3(uri, fetch_fn, force_refresh, metadata, key, ext, provenance)
