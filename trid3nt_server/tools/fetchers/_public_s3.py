"""Anonymous access to PUBLIC AWS S3 buckets, immune to ``AWS_ENDPOINT_URL``.

boto3 and s3fs/aiobotocore both honor ``AWS_ENDPOINT_URL`` globally, so an
unset-endpoint default would redirect anonymous public-bucket reads at whatever
private endpoint the environment names; these helpers pin the real AWS endpoint."""

from __future__ import annotations

from typing import Any


def public_endpoint(region: str = "us-east-1") -> str:
    return f"https://s3.{region}.amazonaws.com"


def public_s3_client(region: str = "us-east-1") -> Any:
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
    """kwargs for ``fsspec.filesystem('s3', ...)`` / ``fsspec.get_mapper`` forcing anonymous access against the real AWS endpoint."""
    return {
        "anon": True,
        "client_kwargs": {"endpoint_url": public_endpoint(region)},
    }
