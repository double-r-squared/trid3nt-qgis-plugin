"""Resolve an ``s3://`` staged-dataset URL to the http form the transport reads.

A staged dataset is a published grid this repo converted once, validated against
its publication, and uploaded to the agent's own object store (see
``scripts/stage_*.py``). Its spec names the object by bucket and key -- never by
host -- because the host is deployment state: MinIO on the local build, real S3
otherwise. This resolves the bucket/key pair against ``AWS_ENDPOINT_URL`` at read
time, mirroring the plugin-side ``s3_to_http``.
"""

from __future__ import annotations

import os

__all__ = ["is_staged_uri", "staged_object_url"]


def is_staged_uri(url: str) -> bool:
    """Whether ``url`` names an object-store object rather than an http endpoint."""
    return url.startswith("s3://")


def staged_object_url(uri: str) -> str:
    """``s3://bucket/key`` -> the path-style http URL for the active endpoint.

    Falls back to the regional AWS virtual-host form when no endpoint override is
    configured. Raises ``ValueError`` for a uri carrying no key -- a bucket alone
    is never a readable object.
    """
    rest = uri[len("s3://"):]
    bucket, _, key = rest.partition("/")
    if not bucket or not key:
        raise ValueError(f"staged object uri carries no key: {uri!r}")
    endpoint = (
        os.environ.get("AWS_ENDPOINT_URL_S3") or os.environ.get("AWS_ENDPOINT_URL") or ""
    ).strip()
    if endpoint:
        return f"{endpoint.rstrip('/')}/{bucket}/{key}"
    region = os.environ.get("AWS_REGION", "us-east-1")
    return f"https://{bucket}.s3.{region}.amazonaws.com/{key}"
