"""MODFLOW 6 solver-worker entrypoint — thin shim around the `mf6` binary.

Sprint-13 / MOD-1 / job-0220 / FR-CE-1/2/3. The MODFLOW-6 analogue of
services/workers/sfincs/entrypoint.py. Same OBJECT-STORE-IN -> RUN ->
OBJECT-STORE-OUT envelope; SCHEME-AWARE like the SFINCS shim (``s3://`` via
boto3 when ``GRACE2_OBJECT_STORE=s3``, ``gs://`` via google-cloud-storage
otherwise — see the object-store abstraction below). The two MODFLOW-specific
differences from the SFINCS shim are:

  1. Subdirectory-preserving input layout. SFINCS takes a flat `sfincs.inp`
     deck; MODFLOW 6 uses a simulation namefile (`mfsim.nam`) that references
     GWF and GWT model namefiles in `gwf/` and `gwt/` subdirectories. The
     manifest's `inputs[].dest` carries the relative path (e.g.
     `gwf/gwf_model.nam`); we `mkdir -p` the parent before each download
     exactly as the SFINCS shim does, so the subdir tree is reconstructed in
     scratch. `mf6` runs in the scratch ROOT where `mfsim.nam` sits.

  2. Convergence guard via the list file (design doc § 8). MODFLOW 6 can exit
     0 while still emitting a convergence-failure warning to `mfsim.lst` when
     the outer-iteration tolerance is met only at the final iteration. The
     list file is authoritative. After `mf6` exits we parse `mfsim.lst` for
     the string "FAILED TO MEET SOLVER CONVERGENCE CRITERIA"; if present we
     override exit_code -> 2 and error -> "solver_diverged" even on a 0 exit.

Contract:

    Input  (env or CLI):
        --run-id RUN_ID
            Run identifier. Outputs land under
            gs://${GRACE2_RUNS_BUCKET}/${RUN_ID}/.
        --manifest-uri gs://bucket/path/manifest.json
            JSON setup manifest. Schema (design doc § 6):
                {
                  "inputs": [
                    {"gs_uri": "gs://.../mfsim.nam",      "dest": "mfsim.nam"},
                    {"gs_uri": "gs://.../gwf/gwf.nam",    "dest": "gwf/gwf_model.nam"},
                    {"gs_uri": "gs://.../gwt/gwt.nam",    "dest": "gwt/gwt_model.nam"},
                    ...
                  ],
                  "mf6_args": ["..."],          # optional argv to mf6 (usually [])
                  "model_crs": "EPSG:26915",    # MODFLOW-specific: model grid CRS
                                                # (read by the agent-side postprocess
                                                #  step for reprojection; the solver
                                                #  shim only echoes it into completion)
                  "outputs": [                   # glob patterns to upload
                    "gwf/gwf_model.hds",
                    "gwt/gwt_model.ucn",
                    "*.lst",
                    "mfsim.lst"
                  ]
                }
            All `inputs` are downloaded into the scratch dir (subdir layout
            preserved) before mf6 runs; all `outputs` (recursive glob) are
            uploaded to the runs bucket after mf6 exits.

    Output:
        gs://${GRACE2_RUNS_BUCKET}/${RUN_ID}/<every output file>
        gs://${GRACE2_RUNS_BUCKET}/${RUN_ID}/completion.json
            Terminal manifest. Schema (mirrors the SFINCS completion schema
            with mf6_* keys + a `converged` boolean + echoed `model_crs`):
                {
                  "run_id": "<run_id>",
                  "status": "ok" | "error",
                  "exit_code": <int>,        # 0 ok; 2 solver_diverged; other = error
                  "converged": <bool>,       # mfsim.lst convergence guard result
                  "model_crs": "<EPSG>" | null,
                  "mf6_stdout_uri": "gs://.../mf6.stdout",
                  "mf6_stderr_uri": "gs://.../mf6.stderr",
                  "output_uris": ["gs://.../<path>", ...],
                  "started_at": "<ISO8601 Z>",
                  "finished_at": "<ISO8601 Z>",
                  "error": "<message>" | null
                }
            The agent's wait-for-completion (job-0227) polls this object; its
            presence with status="ok" or status="error" is the terminal
            signal. Truthful: NOT in this image's scope to assert the run is
            physically meaningful — only that mf6 executed and the list file
            reports convergence.

Design notes:
    - mf6 takes its inputs from CWD (it reads `mfsim.nam` from the working
      directory by convention). We chdir into the scratch dir before exec.
    - We do NOT mount the object store; we download via the SDK resolved BY URI
      SCHEME (boto3 for ``s3://`` on AWS Batch; google-cloud-storage for
      ``gs://`` on the legacy Cloud Run path). Same reasoning as the SFINCS
      shim — outputs are bounded; explicit upload is auditable; no gcsfuse /
      s3fs complexity. job-0289 lesson: boto3 for S3, NOT s3fs/anonymous.
    - The smoke-pattern (kickoff verification): the fixtures/ deck under this
      package is a minimal 10x10 single-layer GWF model; staged into the cache
      bucket with a manifest pointing at it, the entrypoint reproduces the
      host smoke run inside the container.
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

LOG = logging.getLogger("grace2.worker.modflow")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)

MF6_BIN = os.environ.get("GRACE2_MF6_BIN", "/usr/local/bin/mf6")
SCRATCH = Path(os.environ.get("GRACE2_MF6_SCRATCH", "/opt/grace2/work"))
GCP_PROJECT = os.environ.get("GCP_PROJECT", "legacy-cloud-project")
RUNS_BUCKET = os.environ.get("GRACE2_RUNS_BUCKET", "trid3nt-runs")

# MODFLOW 6.4+ list-file string emitted when the outer solver loop exhausts
# its iteration budget without meeting the dvclose tolerance. Pinned to the
# 6.5.0 release we ship (design doc OQ-MOD-1). The mf6 binary can return
# exit 0 with this string present, so the list file — not the exit code — is
# the authoritative convergence signal.
CONVERGENCE_FAILURE_MARKER = "FAILED TO MEET SOLVER CONVERGENCE CRITERIA"
NORMAL_TERMINATION_MARKER = "Normal termination of simulation"


def _utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# Object-store abstraction (scheme-aware — same image on the legacy Cloud Run
# Job AND AWS Batch). MIRRORS services/workers/sfincs/entrypoint.py verbatim:
# the SFINCS shim is already scheme-aware and is the reference. We dispatch the
# I/O BY URI SCHEME: ``gs://`` via google-cloud-storage (lazy import — a pure-S3
# Batch image never pays for the GCP SDK), ``s3://`` via boto3 (job-0289 lesson:
# boto3, NOT s3fs/anonymous). The runs-bucket OUTPUT scheme follows
# ``GRACE2_OBJECT_STORE`` (``s3`` -> ``s3://``, default ``gcs`` -> ``gs://``) so
# completion.json + outputs land in the same store the agent polls. The GCS
# behavior is byte-identical to the pre-port path.
# --------------------------------------------------------------------------- #


def _split_object_uri(uri: str) -> tuple[str, str, str]:
    """Split ``s3://bucket/key`` / ``gs://bucket/key`` -> (scheme, bucket, key)."""
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


def _run_mf6(args: list[str], cwd: Path) -> tuple[int, Path, Path]:
    """Run mf6 in `cwd` (where mfsim.nam sits). Returns (returncode, stdout, stderr)."""
    stdout_path = cwd / "mf6.stdout"
    stderr_path = cwd / "mf6.stderr"
    cmd = [MF6_BIN, *args]
    LOG.info("exec: %s (cwd=%s)", " ".join(cmd), cwd)
    with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            stdout=out,
            stderr=err,
            check=False,
        )
    LOG.info(
        "mf6 exit=%d stdout_bytes=%d stderr_bytes=%d",
        proc.returncode,
        stdout_path.stat().st_size,
        stderr_path.stat().st_size,
    )
    return proc.returncode, stdout_path, stderr_path


def _check_convergence(cwd: Path) -> tuple[bool, str | None]:
    """Parse mfsim.lst for the convergence-failure marker (design doc § 8).

    Returns (converged, note). MODFLOW 6 can exit 0 with a convergence
    warning in the list file, so the list file is authoritative. If
    mfsim.lst is absent (mf6 never started, e.g. deck-invalid exit 1), we
    treat convergence as unknown -> not-converged with a note.
    """
    lst_path = cwd / "mfsim.lst"
    if not lst_path.exists():
        return False, "mfsim.lst absent (mf6 produced no list file)"
    try:
        text = lst_path.read_text(errors="replace")
    except OSError as exc:  # pragma: no cover — defensive
        return False, f"could not read mfsim.lst: {exc}"
    if CONVERGENCE_FAILURE_MARKER in text:
        return False, "solver_diverged"
    if NORMAL_TERMINATION_MARKER in text:
        return True, None
    # No failure marker AND no normal-termination marker: mf6 likely aborted
    # mid-run (input error surfaced to list file). Treat as not-converged.
    return False, "mfsim.lst has neither normal-termination nor convergence-failure marker"


def _expand_outputs(patterns: list[str], cwd: Path) -> list[Path]:
    """Recursive glob over the scratch tree (subdir-aware for gwf/ + gwt/)."""
    seen: set[Path] = set()
    for pat in patterns:
        for hit in glob.glob(str(cwd / pat), recursive=True):
            p = Path(hit)
            if p.is_file():
                seen.add(p.resolve())
    return sorted(seen)


def _build_argv_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="grace2-modflow-entrypoint",
        description="MODFLOW 6 Cloud Run Job entrypoint (FR-CE-1/2/3).",
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
            "s3:// URI of the agent-composed MODFLOW BUILD job_spec (also "
            "$GRACE2_BUILD_SPEC_URI). When set, the worker runs the FloPy deck "
            "BUILD (build_modflow_deck) BEFORE the mf6 solve + plume postprocess "
            "(heavy-compute offload), so the always-on agent never builds the deck "
            "or rasterizes the UCN. Takes precedence over --manifest-uri."
        ),
    )
    return p


def _write_completion(
    run_id: str,
    status: str,
    exit_code: int,
    converged: bool,
    model_crs: str | None,
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
        "converged": converged,
        "model_crs": model_crs,
        "mf6_stdout_uri": stdout_uri,
        "mf6_stderr_uri": stderr_uri,
        "output_uris": output_uris,
        # Build-mode (heavy-compute offload) fields. None on the legacy pre-built-
        # deck (--manifest-uri) path -> byte-identical completion.json there.
        # Mirrors the SFINCS worker's completion.json ``publish_manifest_uri`` /
        # ``deck`` / ``error_code`` block.
        "publish_manifest_uri": publish_manifest_uri,
        "deck": deck,
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


# --------------------------------------------------------------------------- #
# BUILD MODE (heavy-compute offload) — build_modflow_deck -> mf6 -> plume COG.
# The agent hands us a job_spec of confirmed MODFLOWRunArgs; the FloPy deck BUILD
# (which used to run in the always-on agent) runs HERE, then the SAME mf6 solve +
# convergence guard, then the plume raster postprocess (also formerly in-agent).
# Mirrors services/workers/sfincs/entrypoint.py's --build-spec-uri branch.
# --------------------------------------------------------------------------- #

#: Build-mode output globs (the deck is built FLAT in scratch; mf6 writes outputs
#: + the postprocess writes the plume COG there). A recursive net is
#: belt-and-suspenders. Mirrors run_modflow._compose_manifest's output set + the
#: postprocess *.tif.
_BUILD_OUTPUT_GLOBS: tuple[str, ...] = (
    "*.ucn", "*.hds", "*.cbc", "*.lst", "mfsim.lst", "*.tif",
    "**/*.ucn", "**/*.lst",
)


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
    LOG.info("modflow postprocess: wrote %s", uri)
    return uri



# Archetype dispatch table lives in the postprocess module (single source of
# truth for both the worker entrypoint and the agent-side tests).
from services.workers._modflow_postprocess.postprocess import (  # noqa: E402
    _ARCHETYPE_POSTPROCESS_RUNNERS,
)


def _dispatch_archetype_postprocess(
    archetype: str | None,
    run_id: str,
    scratch: Path,
    model_crs: str,
) -> Any:
    """Dispatch to the correct postprocess runner by archetype.

    Returns a ``ModflowPostprocessResult``. When ``archetype`` is None or not in
    the offload table, falls back to the plume runner (spill/contamination path).
    """
    from services.workers import _modflow_postprocess as _pp_mod

    runs_uri_fn = lambda rel: _runs_uri(run_id, rel)  # noqa: E731
    if archetype and archetype in _ARCHETYPE_POSTPROCESS_RUNNERS:
        runner_name = _ARCHETYPE_POSTPROCESS_RUNNERS[archetype]
        runner = getattr(_pp_mod, runner_name)
        LOG.info(
            "build_mode archetype postprocess: archetype=%s runner=%s",
            archetype, runner_name,
        )
        return runner(run_id, scratch, model_crs, runs_uri_fn)
    # Default: spill/plume path.
    LOG.info(
        "build_mode plume postprocess: archetype=%s (fallback to plume runner)",
        archetype,
    )
    return _pp_mod.run_plume_postprocess(run_id, scratch, model_crs, runs_uri_fn)


def _run_build_mode(run_id: str, build_spec_uri: str) -> dict:
    """Build the FloPy deck, run mf6, archetype-dispatch postprocess; return completion dict.

    The deck is built FLAT into the scratch dir (mf6 resolves ``mfsim.nam``
    package refs relative to CWD) and mf6 runs there; the postprocess COG(s) land
    in the same dir so the output sweep uploads them with no extra code.

    Postprocess dispatch:
      archetype=None (spill)                -> run_plume_postprocess (UCN->plume COG)
      archetype=sustainable_yield           -> run_drawdown_postprocess
      archetype=mine_dewatering             -> run_dewatering_postprocess
      archetype=regional_water_budget       -> run_budget_partition_postprocess
      archetype=MAR                         -> run_mounding_postprocess
      archetype=ASR                         -> run_asr_postprocess
      archetype=wetland_hydroperiod         -> run_wetland_hydroperiod_postprocess
    """
    from services.workers._modflow_build import (
        build_deck_kwargs_from_spec,
        validate_job_spec,
    )
    from services.workers.modflow import gwt_adapter

    result: dict = {
        "status": "error", "exit_code": 1, "converged": False,
        "model_crs": None, "output_uris": [], "stdout_uri": None,
        "stderr_uri": None, "publish_manifest_uri": None, "deck": None,
        "error": None, "error_code": None,
    }

    scratch = _prepare_scratch()
    spec = validate_job_spec(_read_manifest(build_spec_uri))
    deck_kwargs = build_deck_kwargs_from_spec(spec)

    deck_manifest = gwt_adapter.build_modflow_deck(
        workdir=str(scratch), write=True, **deck_kwargs
    )
    model_crs = getattr(deck_manifest, "model_crs", None)
    archetype = getattr(deck_manifest, "archetype", None)
    result["model_crs"] = model_crs
    result["deck"] = {
        "model_crs": model_crs,
        "archetype": archetype,
        "gwt_present": bool(getattr(deck_manifest, "gwt_present", True)),
        "spill_lat": float(getattr(deck_manifest, "spill_lat", 0.0)),
        "spill_lon": float(getattr(deck_manifest, "spill_lon", 0.0)),
        "nrow": int(getattr(deck_manifest, "nrow", 0)),
        "ncol": int(getattr(deck_manifest, "ncol", 0)),
    }

    rc, stdout_path, stderr_path = _run_mf6([], scratch)
    converged, conv_note = _check_convergence(scratch)
    result["converged"] = converged
    result["stdout_uri"] = _upload(stdout_path, _runs_uri(run_id, "mf6.stdout"))
    result["stderr_uri"] = _upload(stderr_path, _runs_uri(run_id, "mf6.stderr"))

    # Postprocess ONLY on a clean, converged solve. The runner is selected by the
    # archetype from the deck manifest; runs ON THE WORKER; COG(s) land in scratch
    # BEFORE the output sweep (so the *.tif glob ships them) + publish manifest.
    pp = None
    if rc == 0 and converged:
        pp = _dispatch_archetype_postprocess(
            archetype, run_id, scratch, model_crs or "EPSG:4326"
        )
        if pp.manifest is not None:
            result["publish_manifest_uri"] = _write_publish_manifest(
                run_id, pp.manifest
            )

    for path in _expand_outputs(list(_BUILD_OUTPUT_GLOBS), scratch):
        rel = path.relative_to(scratch).as_posix()
        result["output_uris"].append(_upload(path, _runs_uri(run_id, rel)))
    if result["publish_manifest_uri"]:
        result["output_uris"].append(result["publish_manifest_uri"])

    # Status resolution: solve first (mf6 rc + convergence), then the honesty gate
    # (a clean solve with an empty result is still a failure).
    if rc != 0:
        result.update(exit_code=rc, status="error",
                      error=f"mf6 exited with non-zero code {rc}",
                      error_code="MODFLOW_SOLVER_FAILED")
    elif not converged:
        result.update(exit_code=2, status="error",
                      error=conv_note or "solver_diverged",
                      error_code="MODFLOW_SOLVER_DIVERGED")
    elif pp is not None and pp.status == "error":
        result.update(exit_code=1, status="error",
                      error=pp.error_message
                      or f"postprocess honesty gate: {pp.error_code}",
                      error_code=pp.error_code)
    else:
        result.update(exit_code=0, status="ok", error=None, error_code=None)
    return result


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
        "trid3nt-modflow-solver starting — project=%s run_id=%s mode=%s src=%s "
        "object_store=%s",
        GCP_PROJECT,
        run_id,
        "build+solve" if build_mode else "solve",
        build_spec_uri if build_mode else manifest_uri,
        _output_scheme(),
    )
    started_at = _utc_now()

    # Best-effort completion writing: even on hard error we attempt to write
    # completion.json so wait-for-completion (job-0227) sees a terminal state
    # instead of polling forever.
    output_uris: list[str] = []
    stdout_uri: str | None = None
    stderr_uri: str | None = None
    error_msg: str | None = None
    exit_code = 1
    status = "error"
    converged = False
    model_crs: str | None = None
    publish_manifest_uri: str | None = None
    deck_provenance: dict | None = None
    error_code: str | None = None

    try:
        if build_mode:
            # ---- BUILD MODE (heavy-compute offload) --------------------------
            result = _run_build_mode(run_id, build_spec_uri)
            status = result["status"]
            exit_code = result["exit_code"]
            converged = result["converged"]
            model_crs = result["model_crs"]
            output_uris = result["output_uris"]
            stdout_uri = result["stdout_uri"]
            stderr_uri = result["stderr_uri"]
            publish_manifest_uri = result["publish_manifest_uri"]
            deck_provenance = result["deck"]
            error_msg = result["error"]
            error_code = result["error_code"]
        else:
            # ---- LEGACY SOLVE MODE (pre-built deck via --manifest-uri) -------
            manifest = _read_manifest(manifest_uri)
            inputs = manifest.get("inputs", []) or []
            mf6_args = manifest.get("mf6_args", []) or []
            outputs = manifest.get("outputs", []) or []
            model_crs = manifest.get("model_crs")

            scratch = _prepare_scratch()

            for item in inputs:
                # Manifest input entries keep the LEGACY field name ``gs_uri``;
                # the VALUE is resolved by scheme (s3:// on Batch, gs:// on Cloud
                # Run). dest may carry a subdir path (gwf/..., gwt/...);
                # _download mkdir -p's the parent, reconstructing the
                # mfsim.nam-referenced subdirectory tree in scratch.
                _download(item["gs_uri"], scratch / item["dest"])

            rc, stdout_path, stderr_path = _run_mf6(list(mf6_args), scratch)

            # Convergence guard — list file is authoritative (design doc § 8).
            converged, conv_note = _check_convergence(scratch)

            # Always upload stdout/stderr so the smoke run produces evidence.
            stdout_uri = _upload(stdout_path, _runs_uri(run_id, "mf6.stdout"))
            stderr_uri = _upload(stderr_path, _runs_uri(run_id, "mf6.stderr"))

            for path in _expand_outputs(list(outputs), scratch):
                rel = path.relative_to(scratch).as_posix()
                uri = _upload(path, _runs_uri(run_id, rel))
                output_uris.append(uri)

            # Exit-code resolution (design doc § 8):
            #   - mf6 nonzero  -> error, surface the raw code.
            #   - mf6 zero but list file shows divergence -> override
            #     exit_code=2 (solver_diverged), status=error.
            #   - mf6 zero and converged -> ok.
            if rc != 0:
                exit_code = rc
                status = "error"
                error_msg = f"mf6 exited with non-zero code {rc}"
            elif not converged:
                exit_code = 2
                status = "error"
                error_msg = conv_note or "solver_diverged"
            else:
                exit_code = 0
                status = "ok"

    except Exception as exc:  # pragma: no cover — defensive, logged + emitted
        LOG.exception("solver entrypoint failed")
        error_msg = f"{type(exc).__name__}: {exc}"
        exit_code = 1
        status = "error"
        converged = False

    _write_completion(
        run_id=run_id,
        status=status,
        exit_code=exit_code,
        converged=converged,
        model_crs=model_crs,
        output_uris=output_uris,
        stdout_uri=stdout_uri,
        stderr_uri=stderr_uri,
        started_at=started_at,
        error=error_msg,
        publish_manifest_uri=publish_manifest_uri,
        deck=deck_provenance,
        error_code=error_code,
    )
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
