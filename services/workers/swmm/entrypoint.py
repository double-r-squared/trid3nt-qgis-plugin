"""SWMM AWS Batch worker entrypoint — thin shim around pyswmm (sprint-16 P7).

Near-verbatim copy of ``services/workers/sfincs/entrypoint.py`` (the worker
contract is solver-agnostic): accept ``--run-id`` / ``--manifest-uri`` (env
fallback), read the manifest by URI SCHEME, download every ``inputs[]`` into a
scratch dir, run pyswmm on the staged ``.inp``, glob + upload ``outputs[]``, and
ALWAYS write ``completion.json`` in the SAME schema so the agent's
``wait_for_completion`` (job-0041) reuses its S3 completion poll verbatim.

This is the SWMM CLOUD LANE (sprint-16 P7) — the scale-beyond-local path. The
dev primary path is pyswmm IN-PROCESS in the agent venv
(``workflows/run_swmm.run_swmm_local``); this image is for AWS Batch (and any
out-of-process worker lane), and runs the SAME pyswmm solve the local-exec
``LocalSolverSpec`` does (``services/workers/swmm/run_inp.py``), just inside a
container.

Contract (FR-CE-1/2/3 — IDENTICAL to the SFINCS worker, only the solver +
field names differ):

    Input  (env or CLI):
        --run-id RUN_ID
            Run identifier. Outputs land under
            <scheme>://${TRID3NT_RUNS_BUCKET}/${RUN_ID}/.
        --manifest-uri s3://bucket/path/setup.json
            JSON setup manifest. Schema:
                {
                  "inputs": [
                    {"gs_uri": "s3://.../mesh.inp", "dest": "mesh.inp"},
                    ...
                  ],
                  "swmm_args": ["mesh.inp"],         # optional argv to run_inp
                  "outputs": [                        # glob patterns to upload
                    "*.out",
                    "*.rpt",
                    "*.tif"
                  ]
                }
            All ``inputs`` are downloaded into the scratch dir before pyswmm
            runs; all ``outputs`` (glob expansion) are uploaded to the runs
            bucket after pyswmm exits. The ``gs_uri`` field NAME is legacy
            (worker-contract parity with SFINCS/MODFLOW); the VALUE is resolved
            by scheme (``s3://`` on Batch, ``gs://`` on a GCS box).

    Output:
        <scheme>://${TRID3NT_RUNS_BUCKET}/${RUN_ID}/<every output file>
        <scheme>://${TRID3NT_RUNS_BUCKET}/${RUN_ID}/completion.json
            Terminal manifest. Schema (mirrors the SFINCS completion schema —
            only the stdout/stderr field names carry the ``swmm_`` prefix so the
            ``LocalSolverSpec`` / completion readers stay symmetric):
                {
                  "run_id": "<run_id>",
                  "status": "ok" | "error",
                  "exit_code": <int>,
                  "swmm_stdout_uri": "<scheme>://.../swmm.stdout",
                  "swmm_stderr_uri": "<scheme>://.../swmm.stderr",
                  "swmm_args": ["mesh.inp"],
                  "output_uris": ["<scheme>://.../<path>", ...],
                  "started_at": "<ISO8601 Z>",
                  "finished_at": "<ISO8601 Z>",
                  "error": "<message>" | null
                }
            The agent's ``wait_for_completion`` polls this object; its presence
            with status="ok" or status="error" is the terminal signal. Truthful:
            this image asserts only that pyswmm executed and produced its
            ``.rpt``/``.out`` — NOT that the SWMM run is physically valid. The
            mass-balance honesty gate (Flow Routing Continuity error) is the
            agent-side classifier's job (``run_swmm.swmm_local_spec``'s
            ``classify_exit``), not this shim's.

Design notes:
    - pyswmm writes the ``.rpt`` + ``.out`` ALONGSIDE the ``.inp``; we chdir
      into the scratch dir before the run so the artifacts land where the
      output globs expect them.
    - Object I/O is dispatched BY URI SCHEME (``s3://`` via boto3, ``gs://`` via
      google-cloud-storage, lazy-imported) — byte-identical to the SFINCS
      worker. The runs-bucket OUTPUT scheme follows ``TRID3NT_OBJECT_STORE``
      (``s3`` → ``s3://``, default ``gcs`` → ``gs://``).
    - The smoke-run pattern: a tiny synthetic manifest with no ``inputs`` and a
      trivial ``.inp`` (or none) demonstrates the wiring; pyswmm returns a clean
      non-zero on a missing/invalid deck and the completion still records it.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import glob
import io
import json
import logging
import os
import shutil
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

LOG = logging.getLogger("trid3nt.worker.swmm")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)

SCRATCH = Path(os.environ.get("TRID3NT_SWMM_SCRATCH", "/opt/grace2/work"))
GCP_PROJECT = os.environ.get("GCP_PROJECT", "legacy-cloud-project")
RUNS_BUCKET = os.environ.get("TRID3NT_RUNS_BUCKET", "trid3nt-runs")


def _utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# Object-store abstraction — dispatch BY URI SCHEME (s3:// via boto3, gs:// via
# google-cloud-storage, both lazy-imported). Byte-identical to the SFINCS
# worker (services/workers/sfincs/entrypoint.py): the worker contract is
# solver-agnostic, so the staging/upload envelope is shared verbatim. The
# runs-bucket OUTPUT scheme follows TRID3NT_OBJECT_STORE (s3 → s3://, default
# gcs → gs://) so completion.json + outputs land in the store the agent polls.
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


def _output_scheme() -> str:
    """Runs-bucket output scheme — ``s3`` or ``gs`` (env ``TRID3NT_OBJECT_STORE``)."""
    b = (os.environ.get("TRID3NT_OBJECT_STORE") or "gcs").strip().lower()
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
    role via the standard credential chain). Lazy import so the GCS-only path
    never pays for boto3."""
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


def _resolve_inps(args: list[str], cwd: Path) -> list[str]:
    """Resolve the ``.inp`` deck(s) to run — argv first, else any in the scratch
    dir (mirrors ``run_inp.py``). Args carry bare filenames staged into ``cwd``.
    """
    inps = [a for a in args if a.endswith(".inp")]
    if not inps:
        inps = [p.name for p in sorted(cwd.glob("*.inp"))]
    return inps


def _run_swmm(args: list[str], cwd: Path) -> tuple[int, Path, Path]:
    """Run pyswmm on the staged ``.inp`` deck(s) in ``cwd``.

    pyswmm writes the ``.rpt`` + ``.out`` alongside each ``.inp``; we chdir into
    the scratch dir so the artifacts land where the output globs expect them.
    Captures stdout/stderr to files (the smoke-run evidence the SFINCS worker
    also produces). Returns ``(exit_code, stdout_path, stderr_path)``: 0 on a
    clean solve of every deck, non-zero on any pyswmm failure or a missing deck.
    """
    stdout_path = cwd / "swmm.stdout"
    stderr_path = cwd / "swmm.stderr"

    inps = _resolve_inps(args, cwd)
    rc = 0
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    try:
        with redirect_stdout(out_buf), redirect_stderr(err_buf):
            if not inps:
                err_buf.write("swmm worker: no .inp file given or found in CWD\n")
                rc = 2
            else:
                from pyswmm import Simulation  # type: ignore

                for inp in inps:
                    inp_path = cwd / inp
                    if not inp_path.exists():
                        err_buf.write(f"swmm worker: .inp not found: {inp}\n")
                        rc = 2
                        break
                    LOG.info("pyswmm run: %s (cwd=%s)", inp_path, cwd)
                    # Stepping the simulation to completion writes .rpt + .out
                    # alongside the .inp — symmetric with run_inp.run_one.
                    with Simulation(str(inp_path)) as sim:
                        for _ in sim:
                            pass
    except Exception as exc:  # noqa: BLE001 — record the pyswmm failure as exit!=0
        err_buf.write(f"pyswmm raised {type(exc).__name__}: {exc}\n")
        rc = 1

    stdout_path.write_text(out_buf.getvalue(), encoding="utf-8")
    stderr_path.write_text(err_buf.getvalue(), encoding="utf-8")
    LOG.info(
        "swmm exit=%d stdout_bytes=%d stderr_bytes=%d",
        rc,
        stdout_path.stat().st_size,
        stderr_path.stat().st_size,
    )
    return rc, stdout_path, stderr_path


def _expand_outputs(patterns: list[str], cwd: Path) -> list[Path]:
    seen: set[Path] = set()
    for pat in patterns:
        for hit in glob.glob(str(cwd / pat), recursive=True):
            p = Path(hit)
            if p.is_file():
                seen.add(p.resolve())
    return sorted(seen)


def _build_argv_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="trid3nt-swmm-entrypoint",
        description="SWMM AWS Batch worker entrypoint (FR-CE-1/2/3).",
    )
    p.add_argument(
        "--run-id",
        default=os.environ.get("TRID3NT_RUN_ID", "").strip(),
        help="Run identifier (also $TRID3NT_RUN_ID).",
    )
    p.add_argument(
        "--manifest-uri",
        default=os.environ.get("TRID3NT_MANIFEST_URI", "").strip(),
        help="s3:// / gs:// URI of the setup manifest (also $TRID3NT_MANIFEST_URI).",
    )
    return p


def _write_publish_manifest(run_id: str, pp_manifest: dict) -> str:
    """Write the worker postprocess ``publish_manifest.json`` (before completion)."""
    from services.workers._raster_postprocess import manifest as _manifest_mod

    body = json.dumps(pp_manifest, indent=2)
    uri = _runs_uri(run_id, _manifest_mod.MANIFEST_FILENAME)
    _scheme, _bucket, _key = _split_object_uri(uri)
    if _scheme == "s3":
        _s3_client().put_object(
            Bucket=_bucket, Key=_key,
            Body=body.encode("utf-8"), ContentType="application/json",
        )
    else:
        _gcs_client().bucket(_bucket).blob(_key).upload_from_string(
            body, content_type="application/json"
        )
    LOG.info("swmm postprocess: wrote %s", uri)
    return uri


def _write_completion(
    run_id: str,
    status: str,
    exit_code: int,
    output_uris: list[str],
    stdout_uri: str | None,
    stderr_uri: str | None,
    swmm_args: list[str],
    started_at: str,
    error: str | None,
    publish_manifest_uri: str | None = None,
    error_code: str | None = None,
) -> str:
    payload = {
        "run_id": run_id,
        "status": status,
        "exit_code": exit_code,
        "swmm_stdout_uri": stdout_uri,
        "swmm_stderr_uri": stderr_uri,
        "swmm_args": list(swmm_args),
        "output_uris": output_uris,
        "started_at": started_at,
        "finished_at": _utc_now(),
        "error": error,
        "publish_manifest_uri": publish_manifest_uri,
        "error_code": error_code,
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


def main(argv: list[str] | None = None) -> int:
    parser = _build_argv_parser()
    args = parser.parse_args(argv)

    run_id = args.run_id
    manifest_uri = args.manifest_uri
    if not run_id:
        LOG.error("run_id is required (pass --run-id or set $TRID3NT_RUN_ID)")
        return 2
    if not manifest_uri:
        LOG.error("manifest_uri is required (pass --manifest-uri or set $TRID3NT_MANIFEST_URI)")
        return 2

    LOG.info(
        "trid3nt-swmm-solver starting — project=%s run_id=%s manifest=%s "
        "object_store=%s",
        GCP_PROJECT,
        run_id,
        manifest_uri,
        _output_scheme(),
    )
    started_at = _utc_now()

    # Best-effort completion writing: even on hard error we attempt to write
    # completion.json so wait_for_completion (job-0041) sees a terminal state
    # instead of polling forever.
    output_uris: list[str] = []
    stdout_uri: str | None = None
    stderr_uri: str | None = None
    error_msg: str | None = None
    swmm_args: list[str] = []
    exit_code = 1
    status = "error"
    publish_manifest_uri: str | None = None
    error_code: str | None = None

    try:
        manifest = _read_manifest(manifest_uri)
        inputs = manifest.get("inputs", []) or []
        swmm_args = [str(a) for a in (manifest.get("swmm_args", []) or [])]
        outputs = manifest.get("outputs", []) or []
        postprocess_spec: dict = manifest.get("postprocess_spec") or {}

        scratch = _prepare_scratch()

        for item in inputs:
            # Manifest input entries keep the LEGACY field name ``gs_uri``; the
            # VALUE is resolved by scheme (s3:// on Batch, gs:// on a GCS box).
            input_uri = item["gs_uri"]
            dest = scratch / item["dest"]
            _download(input_uri, dest)

        rc, stdout_path, stderr_path = _run_swmm(swmm_args, scratch)

        # Always upload stdout/stderr so the smoke run produces evidence.
        stdout_uri = _upload(stdout_path, _runs_uri(run_id, "swmm.stdout"))
        stderr_uri = _upload(stderr_path, _runs_uri(run_id, "swmm.stderr"))

        for path in _expand_outputs(list(outputs), scratch):
            rel = path.relative_to(scratch).as_posix()
            uri = _upload(path, _runs_uri(run_id, rel))
            output_uris.append(uri)

        exit_code = rc
        status = "ok" if rc == 0 else "error"
        if rc != 0:
            error_msg = f"swmm worker exited with non-zero code {rc}"

        if rc == 0 and postprocess_spec:
            try:
                from services.workers._swmm_postprocess import run_swmm_postprocess
                pp = run_swmm_postprocess(
                    run_id=run_id,
                    scratch=scratch,
                    postprocess_spec=postprocess_spec,
                    runs_uri_for=lambda rel: _runs_uri(run_id, rel),
                )
                if pp.status == "ok" and pp.manifest is not None:
                    publish_manifest_uri = _write_publish_manifest(run_id, pp.manifest)
                    LOG.info("swmm postprocess ok: publish_manifest_uri=%s", publish_manifest_uri)
                else:
                    error_code = pp.error_code
                    LOG.warning("swmm postprocess honesty gate: %s %s", pp.error_code, pp.error_message)
            except Exception as pp_exc:
                LOG.warning("swmm postprocess failed (non-fatal): %s", pp_exc)

        if publish_manifest_uri and publish_manifest_uri not in output_uris:
            output_uris.append(publish_manifest_uri)

    except Exception as exc:  # pragma: no cover — defensive, logged + emitted
        LOG.exception("solver entrypoint failed")
        error_msg = f"{type(exc).__name__}: {exc}"
        exit_code = 1
        status = "error"

    _write_completion(
        run_id=run_id,
        status=status,
        exit_code=exit_code,
        output_uris=output_uris,
        stdout_uri=stdout_uri,
        stderr_uri=stderr_uri,
        swmm_args=swmm_args,
        started_at=started_at,
        error=error_msg,
        publish_manifest_uri=publish_manifest_uri,
        error_code=error_code,
    )
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
