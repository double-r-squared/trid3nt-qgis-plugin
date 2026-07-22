"""OpenQuake Engine PSHA AWS Batch worker entrypoint (sprint-17).

Near-verbatim copy of ``services/workers/swmm/entrypoint.py`` (the worker
contract is solver-agnostic): accept ``--run-id`` / ``--manifest-uri`` (env
fallback), read the build_spec by URI SCHEME, render a classical-PSHA OpenQuake
deck (``job.ini`` + source-model / GMPE logic-tree XML) into a scratch dir, run
``oq engine --run job.ini`` headless, glob + upload the exported hazard
curves/map CSVs, and ALWAYS write ``completion.json`` in the SAME schema so the
agent's ``wait_for_completion`` (job-0041) reuses its S3 completion poll verbatim.

This is the OpenQuake CLOUD LANE (a NEW engine — OpenQuake is RAM-hungry,
~2 GB/thread, so it never runs in-process in the agent venv like SWMM; it is a
containerized CLI on AWS Batch only). It mirrors the SWMM worker shape exactly,
differing ONLY in the solver invoked + the deck authoring (an OpenQuake deck
templated from a build_spec rather than a staged ``.inp``).

Contract (FR-CE-1/2/3 — IDENTICAL to the SWMM/SFINCS/MODFLOW workers; only the
solver + field names differ):

    Input  (env or CLI):
        --run-id RUN_ID
            Run identifier. Outputs land under
            <scheme>://${GRACE2_RUNS_BUCKET}/${RUN_ID}/.
        --manifest-uri s3://bucket/path/build_spec.json
            JSON build_spec. Schema (the agent composer writes this):
                {
                  "bbox": [min_lon, min_lat, max_lon, max_lat],
                  "imt": "PGA",
                  "poe": 0.10,
                  "investigation_time_years": 50,
                  "site_grid_spacing_km": 5.0,
                  "max_distance_km": 300.0,
                  "gmpe": "BooreAtkinson2008",
                  "a_value": 4.0, "b_value": 1.0,
                  "min_magnitude": 5.0, "max_magnitude": 7.5,
                  "outputs": ["output/*.csv", "*.csv"]   # optional override
                }
            The build_spec is read by SCHEME (s3:// on Batch, gs:// on a GCS box);
            the rendered deck files + the OpenQuake export CSVs are uploaded to
            the runs bucket after the engine exits.

    Output:
        <scheme>://${GRACE2_RUNS_BUCKET}/${RUN_ID}/<every output file>
        <scheme>://${GRACE2_RUNS_BUCKET}/${RUN_ID}/completion.json
            Terminal manifest. Schema mirrors the SWMM completion schema — only
            the stdout/stderr field names carry the ``oq_`` prefix so the
            completion readers stay symmetric:
                {
                  "run_id": "<run_id>",
                  "status": "ok" | "error",
                  "exit_code": <int>,
                  "oq_stdout_uri": "<scheme>://.../oq.stdout",
                  "oq_stderr_uri": "<scheme>://.../oq.stderr",
                  "oq_args": ["engine", "--run", "job.ini"],
                  "output_uris": ["<scheme>://.../<path>", ...],
                  "hazard_map_uri": "<scheme>://.../output/hazard_map-mean-PGA_...csv" | null,
                  "started_at": "<ISO8601 Z>",
                  "finished_at": "<ISO8601 Z>",
                  "error": "<message>" | null
                }
            The agent's ``wait_for_completion`` polls this object; its presence
            with status="ok" or status="error" is the terminal signal. Truthful:
            this image asserts only that the ``oq engine`` run exited 0 and
            produced hazard exports — NOT that the hazard model is physically
            calibrated.

Design notes:
    - Deck authoring is the PURE ``job_ini.render_openquake_deck`` (no I/O), so
      it unit-tests in isolation. The entrypoint just materializes the rendered
      files + runs the CLI.
    - Object I/O is dispatched BY URI SCHEME (``s3://`` via boto3, ``gs://`` via
      google-cloud-storage, lazy-imported) — byte-identical to the SWMM worker.
    - The OpenQuake export step (``--export-outputs`` / engine ``[output]``
      ``export_dir = output``) writes hazard-curve + hazard-map CSVs into the
      ``output/`` subdir of the scratch dir; the output globs capture them.
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

try:
    # Production package context (python -m services.workers.openquake.entrypoint).
    from .job_ini import render_openquake_deck
except ImportError:  # pragma: no cover — bare-name import (tests via sys.path).
    from job_ini import render_openquake_deck  # type: ignore[no-redef]

LOG = logging.getLogger("grace2.worker.openquake")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)

SCRATCH = Path(os.environ.get("GRACE2_OQ_SCRATCH", "/opt/grace2/work"))
GCP_PROJECT = os.environ.get("GCP_PROJECT", "grace-2-hazard-prod")
RUNS_BUCKET = os.environ.get("GRACE2_RUNS_BUCKET", "grace-2-hazard-prod-runs")

#: The OpenQuake CLI binary (overridable for a non-standard install).
OQ_BIN = os.environ.get("GRACE2_OQ_BIN", "oq")

#: Default output globs (the engine writes hazard exports under output/).
_DEFAULT_OUTPUT_GLOBS: tuple[str, ...] = (
    "output/*.csv",
    "*.csv",
    "job.ini",
    "*.xml",
    "*.tif",
)


def _utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# Object-store abstraction — dispatch BY URI SCHEME (s3:// via boto3, gs:// via
# google-cloud-storage, both lazy-imported). Byte-identical to the SWMM worker
# (services/workers/swmm/entrypoint.py): the worker contract is solver-agnostic.
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
    """Runs-bucket output scheme — ``s3`` or ``gs`` (env ``GRACE2_OBJECT_STORE``)."""
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
    standard credential chain). Lazy import so the GCS-only path never pays for
    boto3."""
    global _S3_CLIENT
    if _S3_CLIENT is None:
        import boto3  # type: ignore

        _S3_CLIENT = boto3.client(
            "s3", region_name=os.environ.get("AWS_REGION", "us-west-2")
        )
    return _S3_CLIENT


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
    """Read + parse the build_spec JSON, resolved BY SCHEME."""
    scheme, bucket, key = _split_object_uri(manifest_uri)
    LOG.info("reading build_spec %s", manifest_uri)
    if scheme == "s3":
        resp = _s3_client().get_object(Bucket=bucket, Key=key)
        text = resp["Body"].read().decode("utf-8")
    else:
        text = _gcs_client().bucket(bucket).blob(key).download_as_text()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("build_spec must be a JSON object")
    return data


def _prepare_scratch() -> Path:
    if SCRATCH.exists():
        shutil.rmtree(SCRATCH)
    SCRATCH.mkdir(parents=True, exist_ok=True)
    return SCRATCH


def _materialize_deck(build_spec: dict, cwd: Path) -> dict[str, str]:
    """Render the OpenQuake deck from the build_spec + write it into ``cwd``.

    Returns the {logical_name: written_filename} map (job.ini is the entrypoint).
    """
    deck = render_openquake_deck(build_spec)
    files = {
        "job_ini": deck.job_ini,
        "source_model_xml": deck.source_model_xml,
        "source_model_logic_tree_xml": deck.source_model_logic_tree_xml,
        "gmpe_logic_tree_xml": deck.gmpe_logic_tree_xml,
    }
    written: dict[str, str] = {}
    for logical, text in files.items():
        fname = deck.filenames[logical]
        (cwd / fname).write_text(text, encoding="utf-8")
        written[logical] = fname
    LOG.info("rendered OpenQuake deck into %s: %s", cwd, sorted(written.values()))
    return written


def _run_oq(cwd: Path) -> tuple[int, Path, Path, list[str]]:
    """Run ``oq engine --run job.ini --exports csv`` headless in ``cwd``.

    The engine writes hazard exports under ``cwd/output/`` (per the job.ini
    ``[output] export_dir = output``). Captures stdout/stderr to files (the
    smoke-run evidence the sibling workers also produce). Returns
    ``(exit_code, stdout_path, stderr_path, oq_args)``.
    """
    stdout_path = cwd / "oq.stdout"
    stderr_path = cwd / "oq.stderr"
    oq_args = ["engine", "--run", "job.ini", "--exports", "csv"]

    rc = 0
    try:
        with stdout_path.open("w", encoding="utf-8") as out_fh, stderr_path.open(
            "w", encoding="utf-8"
        ) as err_fh:
            LOG.info("oq run: %s %s (cwd=%s)", OQ_BIN, " ".join(oq_args), cwd)
            proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
                [OQ_BIN, *oq_args],
                cwd=str(cwd),
                stdout=out_fh,
                stderr=err_fh,
                check=False,
            )
            rc = int(proc.returncode)
    except FileNotFoundError as exc:
        stderr_path.write_text(
            f"openquake worker: '{OQ_BIN}' not found on PATH: {exc}\n",
            encoding="utf-8",
        )
        rc = 127
    except Exception as exc:  # noqa: BLE001 — record the failure as exit!=0
        with stderr_path.open("a", encoding="utf-8") as err_fh:
            err_fh.write(f"oq engine raised {type(exc).__name__}: {exc}\n")
        rc = 1

    LOG.info(
        "oq exit=%d stdout_bytes=%d stderr_bytes=%d",
        rc,
        stdout_path.stat().st_size if stdout_path.exists() else 0,
        stderr_path.stat().st_size if stderr_path.exists() else 0,
    )
    return rc, stdout_path, stderr_path, oq_args


def _expand_outputs(patterns: list[str], cwd: Path) -> list[Path]:
    seen: set[Path] = set()
    for pat in patterns:
        for hit in glob.glob(str(cwd / pat), recursive=True):
            p = Path(hit)
            if p.is_file():
                seen.add(p.resolve())
    return sorted(seen)


def resolve_hazard_map_csv(output_uris: list[str]) -> str | None:
    """Pick the hazard-MAP CSV from the uploaded output URIs (worker helper).

    OpenQuake's CSV export writes one ``hazard_map-mean-<IMT>_<...>.csv`` (the
    per-site map value at the requested PoE) alongside the ``hazard_curve-...``
    curves. The postprocess wants the MAP file (one value per site), so this
    helper picks it: prefer a name containing ``hazard_map``, falling back to
    any ``hazard`` CSV, else None. Pure (string-only) so it unit-tests in
    isolation — the worker-entrypoint-helper acceptance item.
    """
    csvs = [u for u in output_uris if u.lower().endswith(".csv")]
    for u in csvs:
        base = u.rsplit("/", 1)[-1].lower()
        if "hazard_map" in base or "hazard-map" in base:
            return u
    for u in csvs:
        if "hazard" in u.rsplit("/", 1)[-1].lower():
            return u
    return None


def _build_argv_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="grace2-openquake-entrypoint",
        description="OpenQuake PSHA AWS Batch worker entrypoint (FR-CE-1/2/3).",
    )
    p.add_argument(
        "--run-id",
        default=os.environ.get("GRACE2_RUN_ID", "").strip(),
        help="Run identifier (also $GRACE2_RUN_ID).",
    )
    p.add_argument(
        "--manifest-uri",
        default=os.environ.get("GRACE2_MANIFEST_URI", "").strip(),
        help="s3:// / gs:// URI of the build_spec (also $GRACE2_MANIFEST_URI).",
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
    LOG.info("openquake postprocess: wrote %s", uri)
    return uri


def _write_completion(
    run_id: str,
    status: str,
    exit_code: int,
    output_uris: list[str],
    stdout_uri: str | None,
    stderr_uri: str | None,
    oq_args: list[str],
    hazard_map_uri: str | None,
    started_at: str,
    error: str | None,
    publish_manifest_uri: str | None = None,
    error_code: str | None = None,
) -> str:
    payload = {
        "run_id": run_id,
        "status": status,
        "exit_code": exit_code,
        "oq_stdout_uri": stdout_uri,
        "oq_stderr_uri": stderr_uri,
        "oq_args": list(oq_args),
        "output_uris": output_uris,
        "hazard_map_uri": hazard_map_uri,
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
        LOG.error("run_id is required (pass --run-id or set $GRACE2_RUN_ID)")
        return 2
    if not manifest_uri:
        LOG.error("manifest_uri is required (pass --manifest-uri or set $GRACE2_MANIFEST_URI)")
        return 2

    LOG.info(
        "grace-2-openquake-solver starting — project=%s run_id=%s manifest=%s "
        "object_store=%s",
        GCP_PROJECT,
        run_id,
        manifest_uri,
        _output_scheme(),
    )
    started_at = _utc_now()

    output_uris: list[str] = []
    stdout_uri: str | None = None
    stderr_uri: str | None = None
    hazard_map_uri: str | None = None
    error_msg: str | None = None
    oq_args: list[str] = []
    exit_code = 1
    status = "error"
    publish_manifest_uri: str | None = None
    error_code: str | None = None

    try:
        build_spec = _read_manifest(manifest_uri)
        scratch = _prepare_scratch()

        _materialize_deck(build_spec, scratch)

        rc, stdout_path, stderr_path, oq_args = _run_oq(scratch)

        # Always upload stdout/stderr so the smoke run produces evidence.
        stdout_uri = _upload(stdout_path, _runs_uri(run_id, "oq.stdout"))
        stderr_uri = _upload(stderr_path, _runs_uri(run_id, "oq.stderr"))

        # An explicit outputs[] override in the build_spec wins; else defaults.
        outputs = build_spec.get("outputs") or list(_DEFAULT_OUTPUT_GLOBS)
        for path in _expand_outputs(list(outputs), scratch):
            rel = path.relative_to(scratch).as_posix()
            uri = _upload(path, _runs_uri(run_id, rel))
            output_uris.append(uri)

        hazard_map_uri = resolve_hazard_map_csv(output_uris)

        exit_code = rc
        status = "ok" if rc == 0 else "error"
        if rc != 0:
            error_msg = f"openquake worker exited with non-zero code {rc}"

        if rc == 0:
            try:
                from services.workers._openquake_postprocess import run_openquake_postprocess
                pp = run_openquake_postprocess(
                    run_id=run_id,
                    scratch=scratch,
                    build_spec=build_spec,
                    runs_uri_for=lambda rel: _runs_uri(run_id, rel),
                )
                if pp.status == "ok" and pp.manifest is not None:
                    publish_manifest_uri = _write_publish_manifest(run_id, pp.manifest)
                    LOG.info("openquake postprocess ok: publish_manifest_uri=%s", publish_manifest_uri)
                else:
                    error_code = pp.error_code
                    LOG.warning("openquake postprocess honesty gate: %s %s", pp.error_code, pp.error_message)
            except Exception as pp_exc:
                LOG.warning("openquake postprocess failed (non-fatal): %s", pp_exc)

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
        oq_args=oq_args,
        hazard_map_uri=hazard_map_uri,
        started_at=started_at,
        error=error_msg,
        publish_manifest_uri=publish_manifest_uri,
        error_code=error_code,
    )
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
