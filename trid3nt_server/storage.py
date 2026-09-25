"""The object store every run reaches through: one client, the bucket names.

The client is a process-wide seam a deployment or a test binds once; absent a
binding it is built lazily from the store's own settings - the endpoint and the
key pair the stack's ``.env.local`` states - so a process that stores nothing
never pays for boto3. The runs bucket has two readings: a read of
a past run falls back to a default name, while a run that is about to UPLOAD
refuses an unset environment rather than filling a bucket nobody provisioned.
"""

from __future__ import annotations

import os
from pathlib import Path
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


#: The stack's own settings file, the one every process the stack starts sources.
_SETTINGS_FILE = Path(__file__).resolve().parents[1] / ".env.local"

#: What the store is reached by. Each is passed to boto3 explicitly, because a
#: client left to find its own credentials takes whatever the process inherited
#: - a shell's ``~/.aws`` profile the store has never heard of.
_SETTINGS = ("AWS_ENDPOINT_URL", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
             "AWS_REGION")


def set_client(bound: Any) -> None:
    """Bind the boto3 S3 client used for ALL object-store I/O.

    ``None`` restores the lazy default, built from the store's own settings."""
    global _CLIENT
    _CLIENT = bound


def set_runs_bucket(name: str | None) -> None:
    """Override the runs-bucket name. ``None`` restores the env-based default."""
    global _RUNS_BUCKET
    _RUNS_BUCKET = name


def _settings() -> dict[str, str]:
    """The store's endpoint, key pair and region: the settings file's, then the
    process's for a setting the file does not state."""
    stated: dict[str, str] = {}
    if _SETTINGS_FILE.is_file():
        for line in _SETTINGS_FILE.read_text(encoding="utf-8").splitlines():
            name, sep, value = line.strip().partition("=")
            if sep and name.strip() in _SETTINGS:
                stated[name.strip()] = value.strip().strip("'\"")
    return {name: stated.get(name) or os.environ.get(name, "")
            for name in _SETTINGS}


def client() -> Any:
    """The bound S3 client, or one built from the store's settings and nothing else.

    boto3, never s3fs, which falls back to anonymous credentials. A store with
    no endpoint stated refuses rather than reaching for a cloud nobody named."""
    if _CLIENT is not None:
        return _CLIENT
    try:
        import boto3  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise StorageError(
            f"boto3 not importable: {exc}; the object store requires boto3 for "
            "staging and upload."
        ) from exc
    stated = _settings()
    missing = [name for name in _SETTINGS[:3] if not stated[name]]
    if missing:
        raise StorageError(
            f"the object store's {', '.join(missing)} is stated neither in "
            f"{_SETTINGS_FILE} nor in the environment, so there is no store to "
            "reach.")
    return boto3.client("s3", endpoint_url=stated["AWS_ENDPOINT_URL"],
                        aws_access_key_id=stated["AWS_ACCESS_KEY_ID"],
                        aws_secret_access_key=stated["AWS_SECRET_ACCESS_KEY"],
                        region_name=stated["AWS_REGION"] or "us-east-1")


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
