"""Retention over the object store: what a live Case pins, and what ages out.

A run's journal record pins the objects it consumed while a Case holds one of its published layers.
Everything else follows its TTL class: past its window the key can no longer be computed, so the
bytes go. Only the read-through cache is swept; a run product carries no TTL class.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Iterator, Mapping, Sequence

from trid3nt_server.store import objects as storage
from trid3nt_server.tools.cache import CACHE_BUCKET, ttl_bucket_vintage
from trid3nt_server.workflows.runtime import journal

__all__ = ["pinned_uris", "reap"]

logger = logging.getLogger("trid3nt_server.store.sweep")

#: The one prefix under the cache bucket this module deletes from.
_PREFIX = "cache/"

#: The sidecar suffix ``read_through`` writes beside an object it caches.
_SIDECAR = ".provenance"


def _strings(value: Any) -> Iterator[str]:
    """Every string anywhere in a nested record."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def _uris(rows: Sequence[Any]) -> set[str]:
    """The objects a set of layer rows names: each row's own uri and its datasets."""
    found: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        for uri in (row.get("uri"), *(row.get("dataset_uris") or ())):
            if isinstance(uri, str) and uri:
                found.add(uri)
    return found


async def _live_case_layers(client: Any) -> list[Any]:
    """Every layer row held by a Case that still EXISTS - archived included,
    deleted excluded: an archived Case is hidden, not gone, so what it holds is
    still held."""
    from trid3nt_server.store.cases import CASES_COLLECTION, DEFAULT_DATABASE

    doc = await client.call_tool("find", {
        "database": DEFAULT_DATABASE, "collection": CASES_COLLECTION,
        "filter": {"status": {"$nin": ["deleted"]}},
    })
    rows: list[Any] = []
    for case in (doc or {}).get("documents") or ():
        if isinstance(case, Mapping):
            rows.extend(case.get("loaded_layer_summaries") or ())
    return rows


async def pinned_uris(client: Any = None) -> set[str]:
    """Every uri a live Case pins: the layers its rows hold, and everything named
    on the journal record of a run that published one of them."""
    from trid3nt_server.store.cases import FileMCPClient

    held = _uris(await _live_case_layers(client or FileMCPClient()))
    pinned = set(held)
    for record in await asyncio.to_thread(journal.read_records):
        if _uris(record.get("outputs") or ()) & held:
            pinned.update(_strings(record))
    return pinned


def _family(key: str) -> str:
    """The object key stripped of its extension and the sidecar's second one: an object without its provenance sidecar is a cache miss."""
    stem = key.rsplit(".", 1)[0]
    return stem[: -len(_SIDECAR)] if stem.endswith(_SIDECAR) else stem


def _spent(key: str, modified: datetime, now: datetime) -> bool:
    """Has this object's TTL window rolled? The window is hashed into the key; a path whose class cannot be read is never spent."""
    parts = key.split("/")
    if len(parts) != 4:
        return False
    try:
        return (ttl_bucket_vintage(parts[1], now=modified)
                != ttl_bucket_vintage(parts[1], now=now))
    except ValueError:
        return False


def _sweep(bucket: str, pinned: set[str], now: datetime) -> list[str]:
    """BLOCKING: delete every spent, unpinned object under the cache prefix."""
    client = storage.client()
    prefix = f"s3://{bucket}/"
    families = {_family(uri[len(prefix):]) for uri in pinned
                if uri.startswith(prefix + _PREFIX)}
    deleted: list[str] = []
    pages = client.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=_PREFIX)
    for page in pages:
        for obj in page.get("Contents") or ():
            key = str(obj.get("Key") or "")
            modified = obj.get("LastModified")
            if not isinstance(modified, datetime) or _family(key) in families:
                continue
            if not _spent(key, modified, now):
                continue
            client.delete_object(Bucket=bucket, Key=key)
            deleted.append(key)
    return deleted


async def reap(*, now: datetime | None = None, client: Any = None) -> list[str]:
    """Delete every cache object no live Case pins and past its class window.
    Answers the keys it deleted."""
    pinned = await pinned_uris(client)
    # The bucket ``read_through`` writes, resolved the way it resolves it.
    bucket = os.environ.get("TRID3NT_CACHE_BUCKET") or CACHE_BUCKET
    deleted = await asyncio.to_thread(
        _sweep, bucket, pinned, now or datetime.now(timezone.utc))
    logger.info("retention: %d spent objects deleted from %s, %d uris pinned",
                len(deleted), bucket, len(pinned))
    return deleted
