"""The object store every run reaches through: one client, the bucket names.

The client is a process-wide seam a deployment or a test binds once; absent a
binding it is built lazily from the ambient environment, so a process that
stores nothing never pays for boto3. The runs bucket has two readings: a read of
a past run falls back to a default name, while a run that is about to UPLOAD
refuses an unset environment rather than filling a bucket nobody provisioned.
"""

from __future__ import annotations

import os
from typing import Any

__all__ = ["StorageError", "client", "local_runs_bucket", "runs_bucket",
           "set_client", "set_runs_bucket", "split_object_uri"]


class StorageError(RuntimeError):
    """The object store is unreachable, unconfigured, or handed a bad URI.

    The ``error_code`` attribute carries the typed code a downstream wrapper
    re-emits verbatim rather than re-deriving one."""

    error_code: str = "STORAGE_UNAVAILABLE"


_CLIENT: Any | None = None
_RUNS_BUCKET: str | None = None


def set_client(bound: Any) -> None:
    """Bind the boto3 S3 client used for ALL object-store I/O.

    ``None`` restores the lazy default, which reads its endpoint and credentials
    from the ambient environment."""
    global _CLIENT
    _CLIENT = bound


def set_runs_bucket(name: str | None) -> None:
    """Override the runs-bucket name. ``None`` restores the env-based default."""
    global _RUNS_BUCKET
    _RUNS_BUCKET = name


def client() -> Any:
    """The bound S3 client, or the lazily constructed boto3 default.

    boto3, never s3fs, which falls back to anonymous credentials."""
    if _CLIENT is not None:
        return _CLIENT
    try:
        import boto3  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise StorageError(
            f"boto3 not importable: {exc}; the object store requires boto3 for "
            "staging and upload."
        ) from exc
    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-west-2"))


def runs_bucket() -> str:
    """The overridden runs bucket, or ``TRID3NT_RUNS_BUCKET``, or the default name."""
    if _RUNS_BUCKET is not None:
        return _RUNS_BUCKET
    return os.environ.get("TRID3NT_RUNS_BUCKET", "trid3nt-runs")


def local_runs_bucket() -> str:
    """The runs bucket a solve uploads into, with NO default at all.

    An unset ``TRID3NT_RUNS_BUCKET`` fails loudly: a default would let every run
    upload into a bucket nobody provisioned and call it a success."""
    if _RUNS_BUCKET is not None:
        return _RUNS_BUCKET
    bucket = (os.environ.get("TRID3NT_RUNS_BUCKET") or "").strip()
    if not bucket:
        raise StorageError(
            "TRID3NT_RUNS_BUCKET must be set when TRID3NT_SOLVER_BACKEND="
            "local-docker; there is no default runs bucket.")
    return bucket


def split_object_uri(uri: str) -> tuple[str, str, str]:
    """Split ``s3://bucket/key`` into ``(scheme, bucket, key)``.

    Only ``s3://`` is supported; anything else raises ``StorageError``."""
    prefix = "s3://"
    if uri.startswith(prefix):
        bucket, _, key = uri[len(prefix):].partition("/")
        if not bucket or not key:
            raise StorageError(f"malformed s3:// URI: {uri!r}")
        return "s3", bucket, key
    raise StorageError(f"unsupported object URI scheme: {uri!r} (expected s3://)")
