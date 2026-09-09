"""Shared precondition for any script that builds a boto3 S3 client against
this repo's local object store. ``boto3.client("s3", endpoint_url=os.environ
.get("AWS_ENDPOINT_URL"))`` resolves to REAL AWS with whatever ambient
credentials are on the box when the env var is unset (``endpoint_url=None`` is
"use the default AWS endpoint" to boto3, not "no override"). This repo's AWS
account is decommissioned -- there is no legitimate real-AWS target -- so an
unset or AWS-hosted endpoint is always a misconfiguration, never a valid
target, and must fail loud instead of silently reaching a real bucket.
"""

from __future__ import annotations

import os
import sys

__all__ = ["require_local_endpoint", "local_endpoint_or_none"]


def local_endpoint_or_none() -> str | None:
    """``AWS_ENDPOINT_URL`` if it names a non-AWS host, else ``None``.

    Never exits -- for a best-effort caller (a smoke/staging step wrapped in
    its own try/except) that should skip the step rather than crash the whole
    script when the local object store is not configured. Use
    :func:`require_local_endpoint` for a script whose primary job needs the
    object store, where a missing endpoint should fail loud instead.
    """
    endpoint = os.environ.get("AWS_ENDPOINT_URL", "").strip()
    if not endpoint or "amazonaws.com" in endpoint:
        return None
    return endpoint


def require_local_endpoint() -> str:
    """Return ``AWS_ENDPOINT_URL``, refusing to fall back to real AWS.

    Exits the process with a clear message when the var is unset or names an
    AWS-hosted host. Callers pass the return value straight through as
    ``boto3.client(..., endpoint_url=require_local_endpoint())``.
    """
    endpoint = local_endpoint_or_none()
    if endpoint is not None:
        return endpoint
    raw = os.environ.get("AWS_ENDPOINT_URL", "").strip()
    if raw:
        sys.exit(
            f"AWS_ENDPOINT_URL={raw!r} points at real AWS -- this repo's AWS "
            "account is decommissioned, so there is no legitimate real-AWS target. "
            "Source the MinIO env block instead: set -a; source .env.local; set +a"
        )
    sys.exit(
        "AWS_ENDPOINT_URL is not set -- refusing to build an S3 client that "
        "would silently fall back to real AWS with ambient credentials. "
        "Source the MinIO env block first: set -a; source .env.local; set +a"
    )
