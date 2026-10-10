"""Resolve an ``s3://`` staged-dataset URL to the http form the transport reads.

A staged dataset is named by bucket and key, never by host, because the host is
deployment state; the pair resolves against ``AWS_ENDPOINT_URL`` at read time.
"""

from __future__ import annotations

import os

__all__ = ["StagedEndpointNotConfigured", "is_staged_uri", "staged_object_url"]


class StagedEndpointNotConfigured(RuntimeError):
    """No local object-store endpoint is configured; there is no real-AWS fallback, so this is never a signal that the object is absent."""


def is_staged_uri(url: str) -> bool:
    return url.startswith("s3://")


def staged_object_url(uri: str) -> str:
    """``s3://bucket/key`` -> the path-style http URL; raises :class:`StagedEndpointNotConfigured` with no endpoint, ``ValueError`` for a keyless uri."""
    rest = uri[len("s3://"):]
    bucket, _, key = rest.partition("/")
    if not bucket or not key:
        raise ValueError(f"staged object uri carries no key: {uri!r}")
    endpoint = (
        os.environ.get("AWS_ENDPOINT_URL_S3") or os.environ.get("AWS_ENDPOINT_URL") or ""
    ).strip()
    if not endpoint:
        raise StagedEndpointNotConfigured(
            "no AWS_ENDPOINT_URL_S3/AWS_ENDPOINT_URL configured -- cannot resolve "
            f"staged object {uri!r}; there is no real-AWS fallback (this repo's "
            "AWS account is decommissioned)"
        )
    return f"{endpoint.rstrip('/')}/{bucket}/{key}"
