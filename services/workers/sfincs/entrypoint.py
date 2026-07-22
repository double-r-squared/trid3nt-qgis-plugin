"""SFINCS Cloud Run Job entrypoint — thin shim around the upstream binary.

Contract (sprint-07 / M5 / FR-CE-1/2/3):

    Input  (env or CLI):
        --run-id RUN_ID
            Run identifier. Outputs land under
            gs://${GRACE2_RUNS_BUCKET}/${RUN_ID}/.
        --manifest-uri gs://bucket/path/setup.json
            JSON setup manifest. Schema:
                {
                  "inputs": [
                    {"gs_uri": "gs://.../dem.tif", "dest": "dem.tif"},
                    {"gs_uri": "gs://.../sfincs.inp", "dest": "sfincs.inp"},
                    ...
                  ],
                  "sfincs_args": ["..."],           # optional argv to sfincs
                  "outputs": [                       # glob patterns to upload
                    "sfincs_map.nc",
                    "*.nc",
                    "*.tif"
                  ]
                }
            All `inputs` are downloaded into the scratch dir before SFINCS
            runs; all `outputs` (glob expansion) are uploaded to the runs
            bucket after SFINCS exits.

    Output:
        gs://${GRACE2_RUNS_BUCKET}/${RUN_ID}/<every output file>
        gs://${GRACE2_RUNS_BUCKET}/${RUN_ID}/completion.json
            Terminal manifest. Schema:
                {
                  "run_id": "<run_id>",
                  "status": "ok" | "error",
                  "exit_code": <int>,
                  "sfincs_stdout_uri": "gs://.../sfincs.stdout",
                  "sfincs_stderr_uri": "gs://.../sfincs.stderr",
                  "output_uris": ["gs://.../<path>", ...],
                  "started_at": "<ISO8601 Z>",
                  "finished_at": "<ISO8601 Z>",
                  "error": "<message>" | null
                }
            The agent's `wait_for_completion` (job-0041) polls this object;
            its presence with status="ok" or status="error" is the terminal
            signal. Truthful: NOT in this image's scope to assert the SFINCS
            run is physically valid — only that the binary executed.

Design notes:
    - The SFINCS binary at /usr/local/bin/sfincs takes its inputs from CWD:
      the classic SFINCS deck expects sfincs.inp + grid + forcings in the
      run directory. We chdir into the scratch dir before exec.
    - We do NOT mount GCS; we download via google-cloud-storage SDK. The
      runs bucket mount adds gcsfuse complexity for no benefit on this
      worker's M5 footprint (outputs are bounded; explicit upload is
      auditable).
    - The smoke-run pattern (kickoff verification): a tiny synthetic
      manifest with no `inputs` and an `sfincs_args` that asks SFINCS to
      `--help` or fails gracefully demonstrates the wiring even with no
      valid model deck.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import glob
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

LOG = logging.getLogger("grace2.worker.sfincs")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)

SFINCS_BIN = os.environ.get("GRACE2_SFINCS_BIN", "/usr/local/bin/sfincs")
SCRATCH = Path(os.environ.get("GRACE2_SFINCS_SCRATCH", "/opt/grace2/work"))
GCP_PROJECT = os.environ.get("GCP_PROJECT", "legacy-cloud-project")
RUNS_BUCKET = os.environ.get("GRACE2_RUNS_BUCKET", "trid3nt-runs")


def _utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# Object-store abstraction (sprint-16 — same image on Cloud Run Job AND AWS
# Batch). The legacy Cloud Run path is GCS-only; AWS Batch runs the SAME image
# against S3. We dispatch the I/O BY URI SCHEME (mirroring the agent's
# tools/solver.py ``_read_object_bytes`` scheme dispatch): ``gs://`` via
# google-cloud-storage (lazy import — a pure-S3 Batch image never pays for the
# GCP SDK), ``s3://`` via boto3. The runs-bucket OUTPUT scheme follows
# ``GRACE2_OBJECT_STORE`` (``s3`` → ``s3://``, default ``gcs`` → ``gs://``) so
# completion.json + outputs land in the same store the agent polls. The GCS
# behavior is byte-identical to the pre-sprint-16 path.
# --------------------------------------------------------------------------- #


def _split_object_uri(uri: str) -> tuple[str, str, str]:
    """Split ``s3://bucket/key`` / ``gs://bucket/key`` → (scheme, bucket, key)."""
    for scheme in ("s3", "gs"):
        prefix = f"{scheme}://"
        if uri.startswith(prefix):
            bucket, _, key = uri[len(prefix):].partition("/")
            if not bucket or not key:
                raise ValueError(f"malformed {scheme}:// URI: {uri!r}")
            return scheme, bucket, key
    raise ValueError(f"unsupported object URI scheme: {uri!r} (expected s3:// or gs://)")


# Legacy alias retained for any external caller / test that imports it.
def _parse_gs_uri(uri: str) -> tuple[str, str]:
    scheme, bucket, key = _split_object_uri(uri)
    if scheme != "gs":
        raise ValueError(f"not a gs:// URI: {uri!r}")
    return bucket, key


def _output_scheme() -> str:
    """Runs-bucket output scheme — ``s3`` or ``gs`` (env ``GRACE2_OBJECT_STORE``)."""
    b = (os.environ.get("GRACE2_OBJECT_STORE") or "gcs").strip().lower()
    return "s3" if b in {"s3", "aws"} else "gs"


def _runs_uri(run_id: str, rel: str) -> str:
    """Compose ``{scheme}://{RUNS_BUCKET}/{run_id}/{rel}`` for an output object."""
    return f"{_output_scheme()}://{RUNS_BUCKET}/{run_id}/{rel}"


_GCS_CLIENT: Any = None


def _gcs_client() -> Any:
    """Lazily build (and cache) the google-cloud-storage client.

    Lazy so a pure-S3 Batch image (no GCP creds, possibly no SDK) never imports
    it. Only reached when a ``gs://`` URI is actually handled.
    """
    global _GCS_CLIENT
    if _GCS_CLIENT is None:
        from google.cloud import storage  # type: ignore

        _GCS_CLIENT = storage.Client(project=GCP_PROJECT)
    return _GCS_CLIENT


_S3_CLIENT: Any = None


def _s3_client() -> Any:
    """Lazily build (and cache) the boto3 S3 client (resolves the Batch task
    role via the standard credential chain). Lazy import so the GCS-only Cloud
    Run path never pays for boto3."""
    global _S3_CLIENT
    if _S3_CLIENT is None:
        import boto3  # type: ignore

        _S3_CLIENT = boto3.client(
            "s3", region_name=os.environ.get("AWS_REGION", "us-west-2")
        )
    return _S3_CLIENT


def _download(uri: str, dest: Path) -> None:
    """Download one object to ``dest``, resolved BY SCHEME (s3:// or gs://)."""
    scheme, bucket, key = _split_object_uri(uri)
    dest.parent.mkdir(parents=True, exist_ok=True)
    LOG.info("downloading %s -> %s", uri, dest)
    if scheme == "s3":
        resp = _s3_client().get_object(Bucket=bucket, Key=key)
        with dest.open("wb") as fh:
            shutil.copyfileobj(resp["Body"], fh)
        return
    _gcs_client().bucket(bucket).blob(key).download_to_filename(str(dest))


def _upload(src: Path, uri: str) -> str:
    """Upload ``src`` to ``uri``, resolved BY SCHEME (s3:// or gs://)."""
    scheme, bucket, key = _split_object_uri(uri)
    LOG.info("uploading %s -> %s", src, uri)
    if scheme == "s3":
        with src.open("rb") as fh:
            _s3_client().put_object(Bucket=bucket, Key=key, Body=fh)
        return uri
    _gcs_client().bucket(bucket).blob(key).upload_from_filename(str(src))
    return uri


def _read_manifest(manifest_uri: str) -> dict:
    """Read + parse the setup manifest JSON, resolved BY SCHEME."""
    scheme, bucket, key = _split_object_uri(manifest_uri)
    LOG.info("reading manifest %s", manifest_uri)
    if scheme == "s3":
        resp = _s3_client().get_object(Bucket=bucket, Key=key)
        text = resp["Body"].read().decode("utf-8")
    else:
        text = _gcs_client().bucket(bucket).blob(key).download_as_text()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("manifest must be a JSON object")
    return data


def _prepare_scratch() -> Path:
    if SCRATCH.exists():
        shutil.rmtree(SCRATCH)
    SCRATCH.mkdir(parents=True, exist_ok=True)
    return SCRATCH


def _run_sfincs(args: list[str], cwd: Path) -> tuple[int, Path, Path]:
    stdout_path = cwd / "sfincs.stdout"
    stderr_path = cwd / "sfincs.stderr"
    cmd = [SFINCS_BIN, *args]
    LOG.info("exec: %s (cwd=%s)", " ".join(cmd), cwd)
    with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            stdout=out,
            stderr=err,
            check=False,
        )
    LOG.info("sfincs exit=%d stdout_bytes=%d stderr_bytes=%d",
             proc.returncode, stdout_path.stat().st_size, stderr_path.stat().st_size)
    return proc.returncode, stdout_path, stderr_path


def _expand_outputs(patterns: list[str], cwd: Path) -> list[Path]:
    seen: set[Path] = set()
    for pat in patterns:
        for hit in glob.glob(str(cwd / pat)):
            p = Path(hit)
            if p.is_file():
                seen.add(p.resolve())
    return sorted(seen)


# --------------------------------------------------------------------------- #
# POSTPROCESS — NetCDF -> COG on the LOCAL solve output (no S3 download). Moves
# the heavy raster postprocess OFF the always-on agent box (postprocess-offload
# spike, Phases 0+1). Shares the GPL-free substrate in
# services/workers/_raster_postprocess/ with the quadtree (sfincs_deckbuilder)
# worker, so the regular-grid SFINCS path does NOT regress to raw-NetCDF.
# --------------------------------------------------------------------------- #


def run_raster_postprocess(
    run_id: str,
    scratch: Path,
) -> tuple[dict | None, str | None, str | None]:
    """Run the shared depth postprocess on the LOCAL ``sfincs_map.nc``.

    Writes overview-bearing COGs into ``scratch`` (so the entrypoint's ``*.tif``
    sweep ships them — the regular-grid manifest's outputs glob already includes
    ``*.tif``), builds the publish manifest, and applies the empty-field honesty
    gate. The regular-grid path reads its bbox off the NetCDF 1D x/y coords (no
    spec bbox needed). The wave pass is skipped (the regular-grid SFINCS worker
    has no SnapWave field).

    Returns ``(manifest_dict | None, status_override | None, error_code | None)``.
    NEVER raises: any failure logs + returns ``(None, None, None)`` so the raw
    sfincs_map.nc still uploads and the agent's legacy on-box path can run
    (transition fallback).
    """
    local_nc = scratch / "sfincs_map.nc"
    if not local_nc.exists():
        LOG.warning(
            "raster postprocess: no local sfincs_map.nc in %s — skipping.", scratch
        )
        return None, None, None
    try:
        from services.workers._raster_postprocess import postprocess as _pp
    except Exception as exc:  # noqa: BLE001 — shared pkg missing -> legacy fallback
        LOG.warning("raster postprocess: shared package import failed (%s)", exc)
        return None, None, None

    runs_uri_for = lambda rel: _runs_uri(run_id, rel)  # noqa: E731
    try:
        depth = _pp.run_postprocess(
            local_nc, run_id=run_id, deck_dir=scratch, runs_uri_for=runs_uri_for,
            kind="depth", engine="sfincs",
        )
    except Exception as exc:  # noqa: BLE001 — defensive; legacy fallback
        LOG.exception("raster postprocess: depth pass crashed (%s)", exc)
        return None, None, None

    if depth.status == "error":
        return depth.manifest, "error", depth.error_code
    LOG.info(
        "raster postprocess: built manifest with %d layer(s) (%d frames)",
        len(depth.manifest.get("layers", [])), depth.manifest.get("frame_count", 0),
    )
    return depth.manifest, None, None


def _build_argv_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="grace2-sfincs-entrypoint",
        description="SFINCS Cloud Run Job entrypoint (FR-CE-1/2/3).",
    )
    p.add_argument(
        "--run-id",
        default=os.environ.get("GRACE2_RUN_ID", "").strip(),
        help="Run identifier (also $GRACE2_RUN_ID).",
    )
    p.add_argument(
        "--manifest-uri",
        default=os.environ.get("GRACE2_MANIFEST_URI", "").strip(),
        help="gs:// URI of the setup manifest (also $GRACE2_MANIFEST_URI).",
    )
    p.add_argument(
        "--build-spec-uri",
        default=os.environ.get("GRACE2_BUILD_SPEC_URI", "").strip(),
        help=(
            "s3:// URI of the agent-composed SFINCS BUILD job_spec (also "
            "$GRACE2_BUILD_SPEC_URI). When set, the worker runs the hydromt "
            "model BUILD (heavy-compute offload) BEFORE the solve + postprocess, "
            "so the always-on agent never loads a DEM/NetCDF. Takes precedence "
            "over --manifest-uri."
        ),
    )
    return p


def _write_completion(
    run_id: str,
    status: str,
    exit_code: int,
    output_uris: list[str],
    stdout_uri: str | None,
    stderr_uri: str | None,
    started_at: str,
    error: str | None,
    publish_manifest_uri: str | None = None,
    deck: dict | None = None,
    error_code: str | None = None,
) -> str:
    payload = {
        "run_id": run_id,
        "status": status,
        "exit_code": exit_code,
        "sfincs_stdout_uri": stdout_uri,
        "sfincs_stderr_uri": stderr_uri,
        "output_uris": output_uris,
        "publish_manifest_uri": publish_manifest_uri,
        # Build-mode (heavy-compute offload) provenance: autoscale / grid_res /
        # nlcd gate result the worker's hydromt build produced. None on the
        # legacy pre-built-deck (--manifest-uri) path. Mirrors the deckbuilder
        # (quadtree) worker's completion.json ``deck`` block.
        "deck": deck,
        # The A.6 open-set error code (e.g. LULC_MAPPING_MISMATCH) when the build
        # or the honesty gate failed, so the agent maps it into the SAME failed
        # AssessmentEnvelope it produced for the in-agent build. None on success.
        "error_code": error_code,
        "started_at": started_at,
        "finished_at": _utc_now(),
        "error": error,
    }
    completion_uri = _runs_uri(run_id, "completion.json")
    scheme, bucket, key = _split_object_uri(completion_uri)
    body = json.dumps(payload, indent=2)
    if scheme == "s3":
        _s3_client().put_object(
            Bucket=bucket,
            Key=key,
            Body=body.encode("utf-8"),
            ContentType="application/json",
        )
    else:
        _gcs_client().bucket(bucket).blob(key).upload_from_string(
            body, content_type="application/json"
        )
    LOG.info("wrote completion -> %s", completion_uri)
    return completion_uri


class _BuildAborted(Exception):
    """Internal sentinel: the hydromt build failed with a typed SFINCSSetupError.

    Its ``result`` dict is already populated with the error_code, so the outer
    handler just skips to writing completion.json (no generic re-wrap)."""


def _write_publish_manifest(run_id: str, pp_manifest: dict) -> str:
    """Write the worker postprocess ``publish_manifest.json`` (before completion)."""
    from services.workers._raster_postprocess import manifest as _manifest_mod

    body = json.dumps(pp_manifest, indent=2)
    uri = _runs_uri(run_id, _manifest_mod.MANIFEST_FILENAME)
    _scheme, _bucket, _key = _split_object_uri(uri)
    if _scheme == "s3":
        _s3_client().put_object(
            Bucket=_bucket, Key=_key,
            Body=body.encode("utf-8"),
            ContentType="application/json",
        )
    else:
        _gcs_client().bucket(_bucket).blob(_key).upload_from_string(
            body, content_type="application/json"
        )
    LOG.info("raster postprocess: wrote %s", uri)
    return uri


def _solve_postprocess_sweep(
    run_id: str,
    run_dir: Path,
    sfincs_args: list[str],
    output_globs: list[str],
) -> dict:
    """Run SFINCS in ``run_dir`` -> postprocess -> sweep outputs (shared tail).

    Used by BOTH the legacy pre-built-deck (--manifest-uri) path and the new
    build-mode (--build-spec-uri) path: the deck already sits in ``run_dir``
    (downloaded flat for the manifest path, built into ``<scratch>/deck`` for the
    build path), so from the solve onward the two modes are IDENTICAL. Writes the
    ``publish_manifest.json`` BEFORE the caller writes completion.json (Spot-
    reclaim atomicity). Returns a dict the caller folds into completion.json.
    """
    rc, stdout_path, stderr_path = _run_sfincs(list(sfincs_args), run_dir)
    # Always upload stdout/stderr so the smoke run produces evidence.
    stdout_uri = _upload(stdout_path, _runs_uri(run_id, "sfincs.stdout"))
    stderr_uri = _upload(stderr_path, _runs_uri(run_id, "sfincs.stderr"))

    # ---- RASTER POSTPROCESS (NetCDF -> COG on the LOCAL run_dir) --------
    # Runs ON THE WORKER (postprocess-offload): write overview-bearing COGs into
    # run_dir BEFORE the output sweep (so the *.tif glob ships them), build the
    # publish manifest, apply the empty-field honesty gate. Clean solve only.
    output_uris: list[str] = []
    publish_manifest_uri: str | None = None
    pp_status_override: str | None = None
    pp_error_code: str | None = None
    if rc == 0:
        pp_manifest, pp_status_override, pp_error_code = run_raster_postprocess(
            run_id, run_dir
        )
        if pp_manifest is not None:
            publish_manifest_uri = _write_publish_manifest(run_id, pp_manifest)

    for path in _expand_outputs(list(output_globs), run_dir):
        rel = path.relative_to(run_dir).as_posix()
        uri = _upload(path, _runs_uri(run_id, rel))
        output_uris.append(uri)
    if publish_manifest_uri:
        output_uris.append(publish_manifest_uri)

    if rc != 0:
        status = "error"
        error_msg = f"sfincs exited with non-zero code {rc}"
        error_code = "SOLVER_FAILED"
    elif pp_status_override == "error":
        # Clean solve but an empty flood field -> honesty gate (Invariant 1).
        status = "error"
        error_msg = (
            f"raster postprocess honesty gate: {pp_error_code} "
            "(solve clean but the flood field is empty)"
        )
        error_code = pp_error_code
    else:
        status = "ok"
        error_msg = None
        error_code = None

    return {
        "status": status,
        "exit_code": rc,
        "error": error_msg,
        "error_code": error_code,
        "output_uris": output_uris,
        "stdout_uri": stdout_uri,
        "stderr_uri": stderr_uri,
        "publish_manifest_uri": publish_manifest_uri,
    }


def main(argv: list[str] | None = None) -> int:
    parser = _build_argv_parser()
    args = parser.parse_args(argv)

    run_id = args.run_id
    manifest_uri = args.manifest_uri
    build_spec_uri = getattr(args, "build_spec_uri", "") or ""
    if not run_id:
        LOG.error("run_id is required (pass --run-id or set $GRACE2_RUN_ID)")
        return 2
    if not manifest_uri and not build_spec_uri:
        LOG.error(
            "one of --manifest-uri / --build-spec-uri is required "
            "(also $GRACE2_MANIFEST_URI / $GRACE2_BUILD_SPEC_URI)"
        )
        return 2

    build_mode = bool(build_spec_uri)
    LOG.info(
        "trid3nt-sfincs-solver starting — project=%s run_id=%s mode=%s src=%s "
        "object_store=%s",
        GCP_PROJECT,
        run_id,
        "build+solve" if build_mode else "solve",
        build_spec_uri if build_mode else manifest_uri,
        _output_scheme(),
    )
    started_at = _utc_now()

    # Best-effort completion writing: even on hard error we attempt to write
    # completion.json so wait_for_completion (job-0041) sees a terminal state
    # instead of polling forever.
    result: dict = {
        "status": "error",
        "exit_code": 1,
        "error": None,
        "error_code": None,
        "output_uris": [],
        "stdout_uri": None,
        "stderr_uri": None,
        "publish_manifest_uri": None,
    }
    deck_provenance: dict | None = None

    try:
        scratch = _prepare_scratch()
        if build_mode:
            # ---- BUILD MODE (heavy-compute offload) --------------------------
            # The agent handed us a job_spec of already-fetched input COG URIs +
            # the serialized forcing/options; run the hydromt model BUILD HERE
            # (the 16 GB in-agent driver), then the SAME solve + postprocess as
            # the legacy path on the freshly-built deck. A build failure (NLCD
            # gate, forcing sanity, hydromt) surfaces its A.6 error_code so the
            # agent reproduces the SAME failed AssessmentEnvelope.
            from services.workers._sfincs_build import (
                build_sfincs_deck,
                validate_job_spec,
            )
            from services.workers._sfincs_build.deck import SFINCSSetupError

            spec = validate_job_spec(_read_manifest(build_spec_uri))
            try:
                deck_provenance = build_sfincs_deck(spec, scratch, _download)
            except SFINCSSetupError as exc:
                LOG.warning("worker build failed: %s (%s)", exc.error_code, exc)
                result.update(
                    status="error",
                    exit_code=1,
                    error=str(exc),
                    error_code=exc.error_code,
                )
                raise _BuildAborted() from exc
            run_dir = Path(deck_provenance["deck_dir"])
            result = _solve_postprocess_sweep(
                run_id, run_dir, [], ["sfincs_map.nc", "*.nc", "*.tif"]
            )
        else:
            # ---- LEGACY SOLVE MODE (pre-built deck via --manifest-uri) -------
            manifest = _read_manifest(manifest_uri)
            inputs = manifest.get("inputs", []) or []
            sfincs_args = manifest.get("sfincs_args", []) or []
            outputs = manifest.get("outputs", []) or []
            for item in inputs:
                # Entries keep the LEGACY field name ``gs_uri``; the VALUE is
                # resolved by scheme (s3:// on Batch, gs:// on Cloud Run).
                _download(item["gs_uri"], scratch / item["dest"])
            result = _solve_postprocess_sweep(
                run_id, scratch, list(sfincs_args), list(outputs)
            )

    except _BuildAborted:
        pass  # result already carries the typed build failure
    except Exception as exc:  # pragma: no cover — defensive, logged + emitted
        LOG.exception("solver entrypoint failed")
        result.update(
            status="error",
            exit_code=1,
            error=f"{type(exc).__name__}: {exc}",
        )

    _write_completion(
        run_id=run_id,
        status=result["status"],
        exit_code=result["exit_code"],
        output_uris=result["output_uris"],
        stdout_uri=result["stdout_uri"],
        stderr_uri=result["stderr_uri"],
        started_at=started_at,
        error=result["error"],
        publish_manifest_uri=result["publish_manifest_uri"],
        deck=deck_provenance,
        error_code=result["error_code"],
    )
    return result["exit_code"]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
