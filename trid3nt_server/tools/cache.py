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
    "cache_key_for",
    "cache_path",
    "parse_cache_path",
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

#: Override via ``TRID3NT_CACHE_BUCKET`` for non-prod runs.
CACHE_BUCKET = "trid3nt-cache"

#: 32 hex chars = 128 bits; collision probability is negligible at this key volume.
CACHE_KEY_HEX_LEN = 32


def _canonicalize_params(params: dict[str, Any]) -> str:
    """Deterministic JSON for the params dict: sorted, ``None`` pruned, compact; an unserializable value gets a stable string form."""
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
    record_shape: str = "",
) -> str:
    """A 32-hex-char SHA-256 prefix over source id, canonical params, the TTL vintage
    and ``record_shape``. ``params`` must already be domain-quantized by the CALLER (bbox to source resolution, dates to the TTL boundary).
    ``record_shape`` digests what decides a record's content (columns, units, zero, decoder), so a correction reaches cached AOIs;
    an empty one leaves the key unchanged."""
    vintage = ttl_bucket_vintage(ttl_class, now=now)
    canonical = _canonicalize_params(params)
    raw = f"{source_id}||{canonical}||{vintage}"
    if record_shape:
        raw = f"{raw}||{record_shape}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return digest[:CACHE_KEY_HEX_LEN]


def cache_key_for(
    metadata: AtomicToolMetadata,
    params: dict[str, Any],
    *,
    source_id: str | None = None,
    now: datetime | None = None,
    record_shape: str = "",
) -> str:
    """The key this tool reads and writes ``params`` under. ``read_through`` calls
    it for the object it stores, so a caller asking the key a fetch WOULD land on
    gets the same string or none at all."""
    return compute_cache_key(
        source_id or metadata.source_class or metadata.name,
        params,
        metadata.ttl_class,
        now=now,
        record_shape=record_shape,
    )


def cache_path(source_class: str, ttl_class: TTLClass, key: str, ext: str) -> str:
    """Object path under the cache bucket, TTL class above source class:
    ``cache/<ttl-class>/<source-class>/<key>.<ext>``. The bucket's lifecycle rules
    are written against that nesting."""
    ext_clean = ext.lstrip(".")
    return f"cache/{ttl_class}/{source_class}/{key}.{ext_clean}"


def parse_cache_path(uri: str | None) -> tuple[str, str] | None:
    """The inverse of :func:`cache_path`: a cached artifact's uri to its source
    class and key, ``None`` for any uri this module did not write. A layer whose
    uri parses IS a fetched artifact, and the key is the one its fetch computed."""
    if not uri:
        return None
    body = uri.split("://", 1)[-1]
    parts = body.split("/")
    try:
        i = parts.index("cache")
    except ValueError:
        return None
    if len(parts) - i != 4:
        return None
    _ttl, source_class, filename = parts[i + 1:]
    key = filename.rsplit(".", 1)[0]
    if not source_class or not key:
        return None
    return source_class, key


def is_cacheable(metadata: AtomicToolMetadata) -> bool:
    """True iff ``metadata.cacheable`` and the TTL class is not
    ``"live-no-cache"``; ``AtomicToolMetadata`` enforces that pairing at
    construction."""
    return metadata.cacheable and metadata.ttl_class != "live-no-cache"


# Fetch-time facts unrecoverable from cached bytes (which legs painted a composite, tiles used, a silent degrade)
# ride a sibling ``<key>.provenance.json``, persisted on miss and replayed on every hit. Small, never secret-bearing.
# A sidecar older than ``PROVENANCE_SCHEMA`` is a MISS: a stale one keeps serving a stale account of the bytes for the TTL bucket.


class ProvenanceRecorder:
    """Single-slot sink for fetch-time provenance. ``data`` stays ``None`` until
    :func:`record_provenance` fills it during a fresh fetch, or ``read_through``
    replays it from the persisted sidecar on a cache hit."""

    __slots__ = ("data",)

    def __init__(self) -> None:
        self.data: dict[str, Any] | None = None


#: The recorder bound for the current fetch; a contextvar so nested delegates reach it without changing ``fetch_fn``'s signature.
_ACTIVE_RECORDER: contextvars.ContextVar[ProvenanceRecorder | None] = (
    contextvars.ContextVar("trid3nt_provenance_recorder", default=None)
)


#: Stamped into recorded provenance and required of replayed sidecars; BUMP when a provenance field becomes load-bearing for honesty.
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
    """Read an ``s3://`` object fully into memory through the ONE store seam, so an
    injected client sees every read. boto3 only, never s3fs: s3fs falls back to
    anonymous access here and returns corrupt bytes."""
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
    """S3 read-through via boto3; any storage failure degrades to fetch-fresh-uncached
    rather than raising. With a :class:`ProvenanceRecorder` the sidecar is replayed on a hit and written on a miss."""
    from botocore.exceptions import ClientError

    from trid3nt_server.store import objects as storage

    bucket, obj_key = _split_s3_uri(uri)
    s3 = storage.client()
    if not force_refresh:
        try:
            resp = s3.get_object(Bucket=bucket, Key=obj_key)
            data = resp["Body"].read()
            prov = _read_sidecar_s3(s3, bucket, obj_key) if provenance is not None else None
            if provenance is not None and not sidecar_is_current(prov):
                # A sidecar older than the current schema is a MISS: replaying it would serve the stale fetch's own claims for the rest of the TTL bucket.
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
    record_shape: str = "",
) -> ReadThroughResult:
    """Read-through / write-on-miss for one atomic-tool fetch. An uncacheable tool
    always misses and returns ``uri=None``; a ``fetch_fn`` failure is RE-RAISED,
    never cached as a sentinel. ``storage_client`` is accepted and ignored."""
    del storage_client
    # The env override WINS over a caller-supplied bucket: an explicit wrong bucket silently degrades every cache write.
    bucket = os.environ.get("TRID3NT_CACHE_BUCKET") or bucket or CACHE_BUCKET
    source_id = source_id or (metadata.source_class or metadata.name)

    # Uncacheable tools never touch the bucket; the recorder still binds so result-model fields populate (no sidecar).
    if not is_cacheable(metadata):
        with _bind_recorder(provenance):
            data = fetch_fn()
        logger.info(
            "read_through live-no-cache tool=%s bytes=%d", metadata.name, len(data)
        )
        prov = provenance.data if provenance is not None else None
        return ReadThroughResult(uri=None, data=data, hit=False, provenance=prov)

    if not metadata.source_class:
        raise ValueError(
            f"cacheable tool {metadata.name!r} has no source_class — model_validator "
            "should have caught this; refusing to write under cache/<None>/."
        )

    key = cache_key_for(metadata, params, source_id=source_id, now=now,
                        record_shape=record_shape)
    path = cache_path(metadata.source_class, metadata.ttl_class, key, ext)

    uri = f"s3://{bucket}/{path}"
    return _read_through_s3(uri, fetch_fn, force_refresh, metadata, key, ext, provenance)
