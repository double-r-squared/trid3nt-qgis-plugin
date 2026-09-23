"""Persist a run's own CHART SPEC under its prefix.

It lands beside the worker's ``completion.json`` in the run prefix, so the
artifact outlives the chat turn that emitted it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Mapping

logger = logging.getLogger("trid3nt_server.workflows.runtime.run_products")

__all__ = ["CHART_SPEC_KEY", "persist_run_products"]

CHART_SPEC_KEY = "chart_spec.json"


async def persist_run_products(run_id: str | None, *,
                               charts: Mapping[str, Any] | None) -> list[str]:
    """Write the chart specs under ``s3://<runs>/<run_id>/``.
    BEST-EFFORT: never raises, because a record that fails to write must not
    retract the run it records. Returns the uris that landed."""
    if not run_id or not charts:
        return []
    uri = await asyncio.to_thread(_put_json, run_id, CHART_SPEC_KEY, dict(charts))
    if not uri:
        return []
    logger.info("run %s products persisted: %s", run_id, uri)
    return [uri]


def _put_json(run_id: str, key: str, body: dict[str, Any]) -> str | None:
    try:
        from trid3nt_server import storage

        bucket = storage.runs_bucket()
        storage.client().put_object(Bucket=bucket, Key=f"{run_id}/{key}",
                        Body=json.dumps(body, indent=2, default=str).encode("utf-8"),
                        ContentType="application/json")
        return f"s3://{bucket}/{run_id}/{key}"
    except Exception as exc:  # noqa: BLE001 - a record write never fails a run
        logger.warning("run %s: %s not persisted (%s)", run_id, key, exc)
        return None
