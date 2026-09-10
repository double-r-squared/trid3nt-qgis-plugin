"""Shared precondition for a script building a boto3 S3 client against the local
object store. To boto3 ``endpoint_url=None`` means "use the default AWS
endpoint", not "no override", so an unset ``AWS_ENDPOINT_URL`` silently reaches
real AWS - never a legitimate target here, and so a failure that must be loud.
"""

from __future__ import annotations

import os
import sys

__all__ = ["require_local_endpoint", "local_endpoint_or_none"]


def local_endpoint_or_none() -> str | None:
    """``AWS_ENDPOINT_URL`` if it names a non-AWS host, else ``None``. Never
    exits: the best-effort form, for a caller that should skip its step rather
    than crash when the local object store is not configured."""
    endpoint = os.environ.get("AWS_ENDPOINT_URL", "").strip()
    if not endpoint or "amazonaws.com" in endpoint:
        return None
    return endpoint


def require_local_endpoint() -> str:
    """Return ``AWS_ENDPOINT_URL``, refusing to fall back to real AWS: exits the
    process with a clear message when the var is unset or names an AWS host."""
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
