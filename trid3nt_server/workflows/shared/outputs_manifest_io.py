"""Host-exec ``outputs.json`` writer -- the agent-side producer half.

The docker-worker engines (SFINCS/GeoClaw/SWAN) write ``outputs.json`` from
inside their container entrypoint via the worker mirror
(``workers/_raster_postprocess/outputs_manifest.py``). The HOST-EXEC engines
(SWMM in-process pyswmm; Landlab exec-from-source) have no separate worker
process to own the write -- the AGENT itself, in the same postprocess that
rasterizes the frames, is the writer (schema §5.1 "host-exec engines"). This
module is that writer's object-store shim: it serializes the entries via the
PURE-STDLIB contracts writer (``trid3nt_contracts.outputs_manifest``) and PUTs
the whole array to ``<scheme>://<runs_bucket>/<run_id>/outputs.json`` -- the
EXACT prefix ``outputs_seam.read_outputs_manifest`` reads back.

Scheme-aware (mirrors ``postprocess_landlab._upload_geojson_to_runs_bucket``):
``s3`` via the solver boto3 client, ``gs``/``file`` via fsspec. The bucket
resolves through the SAME ``_get_runs_bucket`` the seam reader uses, so the
write target and the read target never drift. Best-effort by contract -- the
caller wraps this in a try/except and degrades to peak-only (never sinks the
run), per "failure retracts nothing".
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("trid3nt_server.workflows.shared.outputs_manifest_io")

__all__ = ["write_outputs_manifest"]

#: gs:// fallback bucket (parity with the COG uploaders); AWS/local set
#: TRID3NT_RUNS_BUCKET explicitly.
RUNS_BUCKET_DEFAULT: str = "trid3nt-runs"


def write_outputs_manifest(
    *,
    run_id: str,
    engine: str,
    entries: list[dict[str, Any]],
    runs_bucket: str | None = None,
) -> str:
    """Serialize + PUT ``outputs.json`` under the run prefix; return its URI.

    ``entries`` are pre-built via ``trid3nt_contracts.outputs_manifest.build_entry``
    (the flat ``{kind, quantity, name, uri, t?, units?}`` core + the OPTIONAL
    ``bbox`` / ``band_stats`` render hints). The whole array is written as ONE
    atomic-per-object PUT (schema §2 safe-append; a host-exec engine produces all
    entries in one postprocess pass, so the degenerate at-exit whole-array write
    is the only path here). Raises on an object-store failure so the caller can
    log + degrade to peak-only (best-effort -- never sinks the run).
    """
    from trid3nt_contracts.outputs_manifest import append_entries

    from trid3nt_server.data.cache import storage_scheme

    text = append_entries(None, engine=engine, run_id=run_id, new=list(entries))
    body = text.encode("utf-8")

    scheme = storage_scheme()
    bucket = runs_bucket
    if not bucket:
        try:
            from trid3nt_server.data.simulation.solver.solver import _get_runs_bucket

            bucket = _get_runs_bucket()
        except Exception:  # noqa: BLE001 -- fall back to the env/default
            bucket = os.environ.get("TRID3NT_RUNS_BUCKET") or RUNS_BUCKET_DEFAULT
    key = f"{run_id}/outputs.json"
    uri = f"{scheme}://{bucket}/{key}"

    if scheme == "s3":
        from trid3nt_server.data.simulation.solver.solver import _get_s3_client

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
