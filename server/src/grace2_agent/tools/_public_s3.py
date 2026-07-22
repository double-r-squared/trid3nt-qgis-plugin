"""_public_s3.py -- anonymous access to PUBLIC AWS S3 buckets, immune to
AWS_ENDPOINT_URL.

The TRID3NT Local (offline) build points ``AWS_ENDPOINT_URL`` at MinIO so the
agent's own storage (runs/cache buckets) stays on-disk. boto3 (>=1.28) and
s3fs/aiobotocore BOTH honor that env var globally, which silently redirects the
anonymous PUBLIC NOAA open-data reads (``noaa-goesNN`` GLM granules, the
``hrrrzarr`` Herbie mirror) to MinIO -- listings come back empty / Access
Denied and the tools fail with misleading "no data upstream" errors (found by
the 2026-07-06 local tool sweep: fetch_glm_lightning, fetch_hrrr_forecast,
fetch_hrrr_smoke).

An UNSIGNED/anonymous client never has a reason to target a private endpoint,
so these helpers pin the real AWS endpoint explicitly. Cloud behavior is
unchanged (there the env var is unset and the pin equals the default).
"""

from __future__ import annotations

from typing import Any


def public_endpoint(region: str = "us-east-1") -> str:
    """The real AWS S3 endpoint for ``region`` (public open-data buckets)."""
    return f"https://s3.{region}.amazonaws.com"


def public_s3_client(region: str = "us-east-1") -> Any:
    """Anonymous (UNSIGNED) boto3 S3 client pinned to the real AWS endpoint."""
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config

    return boto3.client(
        "s3",
        region_name=region,
        endpoint_url=public_endpoint(region),
        config=Config(signature_version=UNSIGNED),
    )


def public_s3fs_kwargs(region: str = "us-east-1") -> dict[str, Any]:
    """kwargs for ``fsspec.filesystem('s3', ...)`` / ``fsspec.get_mapper``
    that force anonymous access against the real AWS endpoint."""
    return {
        "anon": True,
        "client_kwargs": {"endpoint_url": public_endpoint(region)},
    }
