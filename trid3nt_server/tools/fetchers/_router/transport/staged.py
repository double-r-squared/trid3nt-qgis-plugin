"""Resolve an ``s3://`` staged-dataset URL to the http form the transport reads.

A staged dataset is a published grid this repo converted once, validated against
its publication, and uploaded to the agent's own object store (see
``scripts/stage_*.py``). Its spec names the object by bucket and key -- never by
host -- because the host is deployment state: MinIO on the local build. This
resolves the bucket/key pair against ``AWS_ENDPOINT_URL`` at read time, mirroring
the plugin-side ``s3_to_http``.
"""

from __future__ import annotations

import os

__all__ = ["StagedEndpointNotConfigured", "is_staged_uri", "staged_object_url"]


class StagedEndpointNotConfigured(RuntimeError):
    """No local object-store endpoint is configured to resolve a staged uri.

    There is no legitimate real-AWS fallback for a staged dataset -- this repo's
    AWS account is decommissioned -- so an unset ``AWS_ENDPOINT_URL_S3`` /
    ``AWS_ENDPOINT_URL`` is always a deployment misconfiguration, never a signal
    that the object is absent. The caller (``raster_cog._direct_window_to_array``)
    maps this to a typed config/upstream router error, never a coverage answer.
    """


def is_staged_uri(url: str) -> bool:
    """Whether ``url`` names an object-store object rather than an http endpoint."""
    return url.startswith("s3://")


def staged_object_url(uri: str) -> str:
    """``s3://bucket/key`` -> the path-style http URL for the active endpoint.

    Raises :class:`StagedEndpointNotConfigured` when neither
    ``AWS_ENDPOINT_URL_S3`` nor ``AWS_ENDPOINT_URL`` is set -- there is no
    legitimate real-AWS target to fall back to. Raises ``ValueError`` for a uri
    carrying no key -- a bucket alone is never a readable object.
    """
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
