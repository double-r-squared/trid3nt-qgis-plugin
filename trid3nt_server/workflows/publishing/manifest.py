"""The ``outputs.json`` writer.

PUTs the serialized entries to ``<scheme>://<runs_bucket>/<run_id>/outputs.json``,
the exact prefix the outputs seam reads back, resolving the bucket the same way."""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("trid3nt_server.workflows.publishing.manifest")

__all__ = ["write_outputs_manifest"]

#: Last-resort bucket name; a deployment sets TRID3NT_RUNS_BUCKET explicitly.
RUNS_BUCKET_DEFAULT: str = "trid3nt-runs"


def write_outputs_manifest(
    *,
    run_id: str,
    engine: str,
    entries: list[dict[str, Any]],
    runs_bucket: str | None = None,
) -> str:
    """Serialize + PUT ``outputs.json`` under the run prefix; return its URI.
    ``entries`` are pre-built manifest entries and the whole array goes in ONE PUT;
    raises on an object-store failure for the caller to degrade on."""
    from trid3nt_contracts.outputs_manifest import append_entries

    from trid3nt_server.tools.cache import storage_scheme

    text = append_entries(None, engine=engine, run_id=run_id, new=list(entries))
    body = text.encode("utf-8")

    scheme = storage_scheme()
    bucket = runs_bucket
    if not bucket:
        try:
            from trid3nt_server.workflows.solver.solver import _get_runs_bucket

            bucket = _get_runs_bucket()
        except Exception:  # noqa: BLE001 -- fall back to the env/default
            bucket = os.environ.get("TRID3NT_RUNS_BUCKET") or RUNS_BUCKET_DEFAULT
    key = f"{run_id}/outputs.json"
    uri = f"{scheme}://{bucket}/{key}"

    if scheme == "s3":
        from trid3nt_server.workflows.solver.solver import _get_s3_client

        _get_s3_client().put_object(
            Bucket=bucket, Key=key, Body=body, ContentType="application/json"
        )
    else:
        import fsspec  # type: ignore

        with fsspec.open(uri, "wb") as fh:
            fh.write(body)

    logger.info(
        "write_outputs_manifest run_id=%s engine=%s entries=%d -> %s",
        run_id,
        engine,
        len(entries),
        uri,
    )
    return uri
