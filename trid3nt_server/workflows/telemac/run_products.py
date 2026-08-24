"""Persist a run's own CHART SPEC and physical-answer METRICS under its prefix.

Verification reads the product, never a rebuild of it: a chart re-derived from
the scalars is a second implementation that can agree with the run by accident
and disagree with it silently. These land beside the worker's ``completion.json``
in the run prefix, so the artifacts outlive the chat turn that emitted them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Mapping

logger = logging.getLogger("trid3nt_server.workflows.telemac.run_products")

__all__ = ["CHART_SPEC_KEY", "METRICS_KEY", "persist_run_products"]

CHART_SPEC_KEY = "chart_spec.json"
METRICS_KEY = "metrics.json"


async def persist_run_products(run_id: str | None, *,
                               charts: Mapping[str, Any] | None,
                               metrics: Mapping[str, Any] | None) -> list[str]:
    """Write the chart specs + metrics under ``s3://<runs>/<run_id>/``.

    BEST-EFFORT: never raises. A record of a solved run that fails to write must
    not retract the run. Returns the uris that landed.
    """
    if not run_id:
        return []
    written: list[str] = []
    for key, body in ((CHART_SPEC_KEY, charts), (METRICS_KEY, metrics)):
        if not body:
            continue
        uri = await asyncio.to_thread(_put_json, run_id, key, dict(body))
        if uri:
            written.append(uri)
    if written:
        logger.info("run %s products persisted: %s", run_id, written)
    return written


def _put_json(run_id: str, key: str, body: dict[str, Any]) -> str | None:
    try:
        import boto3

        from trid3nt_server.data.simulation.solver.solver import _get_runs_bucket

        bucket = _get_runs_bucket()
        boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-west-2")) \
            .put_object(Bucket=bucket, Key=f"{run_id}/{key}",
                        Body=json.dumps(body, indent=2, default=str).encode("utf-8"),
                        ContentType="application/json")
        return f"s3://{bucket}/{run_id}/{key}"
    except Exception as exc:  # noqa: BLE001 - a record write never fails a run
        logger.warning("run %s: %s not persisted (%s)", run_id, key, exc)
        return None
