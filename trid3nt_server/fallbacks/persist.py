"""Persist a run's fallback activations as a bucket-side audit artifact.

One object per run at
``s3://<runs-bucket>/<run_id>/fallback_activations.json``.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Sequence

logger = logging.getLogger("trid3nt_server.fallbacks.persist")

__all__ = ["ACTIVATIONS_KEY", "persist_run_activations"]

#: Object name under the run prefix. Stable so an auditor can find it by path.
ACTIVATIONS_KEY = "fallback_activations.json"


def _row(a: Any) -> dict[str, Any]:
    if isinstance(a, dict):
        return dict(a)
    dump = getattr(a, "model_dump", None)
    return dict(dump()) if callable(dump) else {"rung": str(a)}


def persist_run_activations(
    run_id: str | None,
    activations: Sequence[Any] | None,
    *,
    capability_note: str | None = None,
) -> str | None:
    """Write ``activations`` under the run prefix; return the uri or ``None``.
    BEST-EFFORT: never raises - an audit write must not fail a solved run.
    ``None`` means nothing to record, or a write that degraded."""
    if not run_id or not activations:
        return None
    try:
        from trid3nt_server import storage


        bucket = storage.runs_bucket()
        key = f"{run_id}/{ACTIVATIONS_KEY}"
        payload = {
            "schema_version": 1,
            "run_id": str(run_id),
            "note": capability_note,
            "activations": [_row(a) for a in activations],
        }
        from trid3nt_server import storage

        # A sidecar of its own: the activations are a server-side fact about
        # the inputs the composer fetched, not something the worker wrote.
        storage.client().put_object(
            Bucket=bucket,
            Key=key,
            Body=json.dumps(payload, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
        uri = f"s3://{bucket}/{key}"
        logger.info("fallback activations persisted -> %s (%d row(s))", uri,
                    len(payload["activations"]))
        return uri
    except Exception as exc:  # noqa: BLE001 -- an audit write never fails a run
        logger.warning(
            "fallback activations NOT persisted for run %s (non-fatal): %s",
            run_id, exc,
        )
        return None
