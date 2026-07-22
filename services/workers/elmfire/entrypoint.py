"""ELMFIRE AWS Batch worker entrypoint - wildfire spread (FIRE-4).

The ELMFIRE analogue of ``services/workers/geoclaw/entrypoint.py`` /
``services/workers/swan/entrypoint.py``. Same OBJECT-STORE-IN -> RUN ->
OBJECT-STORE-OUT envelope; SCHEME-AWARE (``s3://`` via boto3 when
``GRACE2_OBJECT_STORE=s3``, ``gs://`` via google-cloud-storage otherwise). The
worker contract is solver-agnostic, so the staging/upload/completion envelope
is copied verbatim from the GeoClaw worker; only the SOLVER step differs.

The ELMFIRE-specific differences from the GeoClaw shim:

  1. NO DECK AUTHORING. Unlike GeoClaw/SWAN, the ELMFIRE deck is built
     AGENT-SIDE by the FIRE-2 deck builder (``deck_builder.py`` next to this
     file, driven through ``run_elmfire.build_elmfire_deck``): the agent
     stages a READY deck (``inputs/*.tif`` + ``inputs/elmfire.data``) to the
     cache bucket and the manifest lists every deck file. This worker only
     stages, solves, and uploads - it never imports rasterio (the image
     carries none).

  2. THE SOLVE. ``elmfire_<VER> ./inputs/elmfire.data`` run from the scratch
     root, after recreating the deck's (empty, unstaged) ``outputs/`` +
     ``scratch/`` dirs - mirroring ``run_elmfire.elmfire_local_spec``'s
     ``mkdir -p outputs scratch`` byte-for-byte. The FIRE-2 namelist pins
     ``CONVERT_TO_GEOTIFF=.FALSE.`` so the solver writes ESRI BIL rasters
     (+ .hdr sidecars) into ``outputs/``. NOTE the solver DOES shell out to
     ``gdal_translate`` (PATH_TO_GDAL, default /usr/bin) to convert GeoTIFF
     inputs to ENVI BSQ for reading - the image MUST carry gdal-bin.

  3. THE HONESTY GATE. A run that exits 0 but leaves ``outputs/`` with no
     raster is classified ``ELMFIRE_OUTPUT_EMPTY`` (status=error) - the
     render-chokepoint honesty floor: a 'modeled' envelope with empty layers
     never reads status ok. (The agent-side ``postprocess_elmfire`` re-checks
     burned-cell content - ``ELMFIRE_NO_SPREAD`` stays agent-side because it
     needs rasterio.)

Contract (FR-CE-1/2/3 - IDENTICAL to the GeoClaw/SWAN workers):

    Input  (env or CLI):
        --run-id RUN_ID
        --manifest-uri s3://bucket/path/manifest.json
            JSON setup manifest (written by run_elmfire.stage_elmfire_manifest):
                {
                  "engine": "elmfire",
                  "run_id": "...",
                  "elmfire_args": ["./inputs/elmfire.data"],
                  "inputs": [
                    {"gs_uri": "s3://.../inputs/elmfire.data", "dest": "inputs/elmfire.data"},
                    {"gs_uri": "s3://.../inputs/fbfm40.tif",   "dest": "inputs/fbfm40.tif"},
                    ...
                  ],
                  "outputs": ["outputs/*.bil", "outputs/*.hdr",
                              "outputs/*.tif", "outputs/*.csv"],
                  "build_spec": { ... grid/weather/ignition provenance ... }
                }

    Output:
        <scheme>://${GRACE2_RUNS_BUCKET}/${RUN_ID}/<every matched output file>
        <scheme>://${GRACE2_RUNS_BUCKET}/${RUN_ID}/completion.json
            Terminal manifest (mirrors the GeoClaw completion schema; the
            stdout/stderr field names carry the ``elmfire_`` prefix).
            Truthful: this image asserts ELMFIRE executed AND produced at
            least one raster under outputs/ - not that the run is physically
            valid.
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

LOG = logging.getLogger("grace2.worker.elmfire")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)

SCRATCH = Path(os.environ.get("GRACE2_ELMFIRE_SCRATCH", "/opt/grace2/work"))
GCP_PROJECT = os.environ.get("GCP_PROJECT", "legacy-cloud-project")
RUNS_BUCKET = os.environ.get("GRACE2_RUNS_BUCKET", "trid3nt-runs")

#: The solver binary (release-pinned name; the Dockerfile bakes the matching
#: default, env overrides on a release bump - mirrors run_elmfire's
#: GRACE2_ELMFIRE_BINARY seam).
ELMFIRE_BINARY = os.environ.get("GRACE2_ELMFIRE_BINARY", "elmfire_2025.0526")

#: Default output globs (worker-side mirror of run_elmfire.ELMFIRE_OUTPUT_GLOBS;
#: the manifest's "outputs" list wins when present).
DEFAULT_OUTPUT_GLOBS: list[str] = [
    "outputs/*.bil",
    "outputs/*.hdr",
    "outputs/*.tif",
    "outputs/*.csv",
]

#: Extensions that count as "a raster came out" for the honesty gate (.hdr and
#: .csv sidecars alone do NOT satisfy it).
_RASTER_SUFFIXES: frozenset[str] = frozenset({".bil", ".tif"})


def _utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# Object-store abstraction - dispatch BY URI SCHEME (s3:// via boto3, gs:// via
# google-cloud-storage, both lazy-imported). Byte-identical to the GeoClaw/
# SWAN/SWMM workers: the worker contract is solver-agnostic.
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


def _output_scheme() -> str:
    """Runs-bucket output scheme - ``s3`` or ``gs`` (env ``GRACE2_OBJECT_STORE``)."""
    b = (os.environ.get("GRACE2_OBJECT_STORE") or "gcs").strip().lower()
    return "s3" if b in {"s3", "aws"} else "gs"


def _runs_uri(run_id: str, rel: str) -> str:
    """Compose ``{scheme}://{RUNS_BUCKET}/{run_id}/{rel}`` for an output object."""
    return f"{_output_scheme()}://{RUNS_BUCKET}/{run_id}/{rel}"


_GCS_CLIENT: Any = None


def _gcs_client() -> Any:
    """Lazily build (and cache) the google-cloud-storage client."""
    global _GCS_CLIENT
    if _GCS_CLIENT is None:
        from google.cloud import storage  # type: ignore

        _GCS_CLIENT = storage.Client(project=GCP_PROJECT)
    return _GCS_CLIENT


_S3_CLIENT: Any = None


def _s3_client() -> Any:
    """Lazily build (and cache) the boto3 S3 client (Batch task role via the
    standard credential chain)."""
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


# --------------------------------------------------------------------------- #
# The solve.
# --------------------------------------------------------------------------- #
def _run_elmfire(cwd: Path, elmfire_args: list[str]) -> tuple[int, Path, Path]:
    """Run the ELMFIRE solver headless in ``cwd``; capture stdout/stderr.

    Recreates the deck's (empty, unstaged) ``outputs/`` + ``scratch/`` dirs
    first - the namelist's OUTPUTS_DIRECTORY / SCRATCH keys point at them and
    the staged manifest only carries ``inputs/`` files (exact parity with the
    local-docker lane's ``mkdir -p outputs scratch``).
    """
    (cwd / "outputs").mkdir(parents=True, exist_ok=True)
    (cwd / "scratch").mkdir(parents=True, exist_ok=True)

    stdout_path = cwd / "elmfire.stdout"
    stderr_path = cwd / "elmfire.stderr"
    argv = [ELMFIRE_BINARY, *[str(a) for a in elmfire_args]]
    LOG.info("elmfire: exec %s (cwd=%s)", argv, cwd)
    with stdout_path.open("wb") as out_fh, stderr_path.open("wb") as err_fh:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            stdout=out_fh,
            stderr=err_fh,
            check=False,
        )
    rc = proc.returncode
    LOG.info(
        "elmfire exit=%d stdout_bytes=%d stderr_bytes=%d",
        rc,
        stdout_path.stat().st_size,
        stderr_path.stat().st_size,
    )
    return rc, stdout_path, stderr_path


def _expand_outputs(patterns: list[str], cwd: Path) -> list[Path]:
    """Glob over the scratch tree (files only, de-duplicated, sorted)."""
    seen: set[Path] = set()
    for pat in patterns:
        for hit in glob.glob(str(cwd / pat), recursive=True):
            p = Path(hit)
            if p.is_file():
                seen.add(p.resolve())
    return sorted(seen)


def _build_argv_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="grace2-elmfire-entrypoint",
        description="ELMFIRE AWS Batch worker entrypoint (FR-CE-1/2/3).",
    )
    p.add_argument(
        "--run-id",
        default=os.environ.get("GRACE2_RUN_ID", "").strip(),
        help="Run identifier (also $GRACE2_RUN_ID).",
    )
    p.add_argument(
        "--manifest-uri",
        default=os.environ.get("GRACE2_MANIFEST_URI", "").strip(),
        help="s3:// / gs:// URI of the setup manifest (also $GRACE2_MANIFEST_URI).",
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
    error_code: str | None = None,
) -> str:
    payload = {
        "run_id": run_id,
        "status": status,
        "exit_code": exit_code,
        "elmfire_stdout_uri": stdout_uri,
        "elmfire_stderr_uri": stderr_uri,
        "output_uris": output_uris,
        "started_at": started_at,
        "finished_at": _utc_now(),
        "error": error,
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
        LOG.error("run_id is required (pass --run-id or set $GRACE2_RUN_ID)")
        return 2
    if not manifest_uri:
        LOG.error("manifest_uri is required (pass --manifest-uri or set $GRACE2_MANIFEST_URI)")
        return 2

    LOG.info(
        "trid3nt-elmfire-solver starting - run_id=%s manifest=%s object_store=%s "
        "binary=%s",
        run_id,
        manifest_uri,
        _output_scheme(),
        ELMFIRE_BINARY,
    )
    started_at = _utc_now()

    output_uris: list[str] = []
    stdout_uri: str | None = None
    stderr_uri: str | None = None
    error_msg: str | None = None
    exit_code = 1
    status = "error"
    error_code: str | None = None

    try:
        manifest = _read_manifest(manifest_uri)
        inputs = manifest.get("inputs", []) or []
        elmfire_args = [
            str(a) for a in (manifest.get("elmfire_args") or ["./inputs/elmfire.data"])
        ]
        outputs = manifest.get("outputs") or DEFAULT_OUTPUT_GLOBS

        scratch = _prepare_scratch()

        # Stage the ready deck (inputs/*.tif + inputs/elmfire.data).
        for item in inputs:
            input_uri = item["gs_uri"]
            dest = scratch / item["dest"]
            _download(input_uri, dest)

        rc, stdout_path, stderr_path = _run_elmfire(scratch, elmfire_args)

        # Always upload stdout/stderr so the run produces evidence.
        stdout_uri = _upload(stdout_path, _runs_uri(run_id, "elmfire.stdout"))
        stderr_uri = _upload(stderr_path, _runs_uri(run_id, "elmfire.stderr"))

        matched = _expand_outputs(list(outputs), scratch)
        for path in matched:
            rel = path.relative_to(scratch).as_posix()
            uri = _upload(path, _runs_uri(run_id, rel))
            output_uris.append(uri)

        exit_code = rc
        status = "ok" if rc == 0 else "error"
        if rc != 0:
            error_msg = f"elmfire worker exited with non-zero code {rc}"

        # Honesty gate: exit 0 with NO raster under outputs/ is a FAILURE
        # (render-chokepoint floor - never a blank "modeled ok").
        if rc == 0 and not any(p.suffix.lower() in _RASTER_SUFFIXES for p in matched):
            status = "error"
            error_code = "ELMFIRE_OUTPUT_EMPTY"
            error_msg = (
                "ELMFIRE exited 0 but wrote no raster under outputs/ "
                f"(matched {len(matched)} files from globs {list(outputs)!r})"
            )
            LOG.error("%s", error_msg)

    except Exception as exc:  # pragma: no cover - defensive, logged + emitted
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
        started_at=started_at,
        error=error_msg,
        error_code=error_code,
    )
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
