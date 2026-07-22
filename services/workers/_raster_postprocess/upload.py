"""Worker-local object-store upload (no agent client import).

The agent's ``postprocess_flood._upload_cog_to_runs_bucket`` reaches
``..tools.solver._get_s3_client`` — an AGENT seam. The worker must NOT import
agent code, so this module builds a worker-local boto3 / GCS client (the SAME
lazy, scheme-dispatched pattern the worker entrypoints already use) and uploads a
local COG to the runs bucket at a deterministic key.

In Phase 1 the worker's existing ``_expand_outputs`` ``*.tif`` sweep already
uploads every ``flood_depth_*.tif`` / ``wave_height_*.tif`` the postprocess wrote
into the deck dir, so this helper is the FALLBACK / direct path (used by tests
and any caller that writes COGs OUTSIDE the deck dir). The orchestrator writes
COGs straight into the deck dir, so the entrypoint sweep does the upload.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

LOG = logging.getLogger("grace2.worker.raster_postprocess.upload")

_S3_CLIENT: Any = None
_GCS_CLIENT: Any = None


def _split_object_uri(uri: str) -> tuple[str, str, str]:
    for scheme in ("s3", "gs"):
        prefix = f"{scheme}://"
        if uri.startswith(prefix):
            bucket, _, key = uri[len(prefix):].partition("/")
            if not bucket or not key:
                raise ValueError(f"malformed {scheme}:// URI: {uri!r}")
            return scheme, bucket, key
    raise ValueError(f"unsupported object URI scheme: {uri!r} (expected s3:// or gs://)")


def _s3_client() -> Any:
    """Lazily build the worker-local boto3 S3 client (Batch task-role chain)."""
    global _S3_CLIENT
    if _S3_CLIENT is None:
        import boto3  # type: ignore

        _S3_CLIENT = boto3.client(
            "s3", region_name=os.environ.get("AWS_REGION", "us-west-2")
        )
    return _S3_CLIENT


def _gcs_client() -> Any:
    global _GCS_CLIENT
    if _GCS_CLIENT is None:
        from google.cloud import storage  # type: ignore

        _GCS_CLIENT = storage.Client(
            project=os.environ.get("GCP_PROJECT", "grace-2-hazard-prod")
        )
    return _GCS_CLIENT


def upload_object(
    src: Path,
    uri: str,
    *,
    content_type: str | None = None,
    s3_client: Any = None,
) -> str:
    """Upload ``src`` to ``uri`` (s3:// or gs://); return the URI.

    ``s3_client`` lets a caller (or test) inject a client; otherwise the lazy
    worker-local client is built. NEVER imports agent code.
    """
    scheme, bucket, key = _split_object_uri(uri)
    LOG.info("uploading %s -> %s", src, uri)
    if scheme == "s3":
        client = s3_client or _s3_client()
        extra = {"ContentType": content_type} if content_type else {}
        with src.open("rb") as fh:
            client.put_object(Bucket=bucket, Key=key, Body=fh, **extra)
        return uri
    blob = _gcs_client().bucket(bucket).blob(key)
    if content_type:
        blob.upload_from_filename(str(src), content_type=content_type)
    else:
        blob.upload_from_filename(str(src))
    return uri


def upload_cog(
    local_cog: Path,
    runs_uri_for: Any,
    dest_filename: str,
    *,
    s3_client: Any = None,
) -> str:
    """Upload a COG to ``runs_uri_for(dest_filename)`` (the deterministic key).

    ``runs_uri_for`` is a callable ``rel -> uri`` (the entrypoint's
    ``lambda rel: _runs_uri(run_id, rel)``) so this helper stays agnostic of the
    bucket / scheme / run-id resolution the entrypoint already owns.
    """
    return upload_object(
        local_cog,
        runs_uri_for(dest_filename),
        content_type="image/tiff",
        s3_client=s3_client,
    )
