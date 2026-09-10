"""The in-memory read-through a fetcher test installs in place of the cache.

Mints ``s3://`` uris and honours hit / miss / write against the dict the test
inspects afterwards; ``live-no-cache`` short-circuits exactly as the real shim.
"""

from __future__ import annotations


def make_read_through_s3_injector(store: dict[str, bytes]):
    """Return a drop-in ``read_through`` replacement backed by an in-memory store.

    Mints ``s3://`` URIs, honours hit / miss / write against ``store`` keyed by object
    KEY, and short-circuits ``live-no-cache`` as the real shim does."""
    from trid3nt_server.tools.cache import (
        CACHE_BUCKET,
        cache_path,
        compute_cache_key,
        is_cacheable,
        ReadThroughResult,
    )

    def _patched(metadata, params, ext, fetch_fn, **kw):
        bucket = kw.get("bucket") or CACHE_BUCKET
        source_id = kw.get("source_id") or (metadata.source_class or metadata.name)
        now = kw.get("now")
        force_refresh = kw.get("force_refresh", False)
        if not is_cacheable(metadata):
            return ReadThroughResult(uri=None, data=fetch_fn(), hit=False)
        key = compute_cache_key(source_id, params, metadata.ttl_class, now=now)
        path = cache_path(metadata.source_class, metadata.ttl_class, key, ext)
        uri = f"s3://{bucket}/{path}"
        if not force_refresh and path in store:
            return ReadThroughResult(uri=uri, data=store[path], hit=True)
        data = fetch_fn()
        store[path] = data
        return ReadThroughResult(uri=uri, data=data, hit=False)

    return _patched
