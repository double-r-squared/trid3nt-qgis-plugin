"""What the box receives: the authored run directory, uploaded, and its manifest.

Nothing here reads a mesh, a geometry or a physical value - it moves files and
names them. The manifest is written LAST, so a manifest exists only for a run
whose every file is already where the launcher will look, and the staged
directory is checked against itself before any of it leaves the daemon."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from trid3nt_contracts import new_ulid

from ..errors import TelemacError
from .staged_check import check_staged_run

__all__ = ["case_section", "new_rundir", "stage_run", "stage_telemac_manifest"]


def case_section(*, module: str, steering: str, results: list[str],
                 server_facts: Mapping[str, Any],
                 user_fortran: Sequence[str] = (),
                 coupling: str | None = None,
                 continue_from: str | None = None,
                 cores: int = 1) -> dict[str, Any]:
    """The CASE a worker runs: which engine, which file, what it must produce.

    ``cores`` is the partition the steering file's own PARALLEL PROCESSORS
    states, carried so the launcher and the engine are told the same number; a
    serial run states neither. ``server_facts`` is copied into the worker's
    metrics verbatim, never re-derived."""
    return {"module": module, "steering": steering,
            **({"user_fortran": list(user_fortran)} if user_fortran else {}),
            **({"coupling": coupling} if coupling else {}),
            **({"continue_from": continue_from} if continue_from else {}),
            **({"cores": int(cores)} if int(cores) > 1 else {}),
            "results": list(results), "server_facts": dict(server_facts)}


# ``inputs`` rows are ``{gs_uri, dest}``: what the launcher stages into the run
# directory before the container starts, which is why the worker needs no network.
# An authored run's section is ``case``.
def stage_telemac_manifest(*, section: str, config: Mapping[str, Any],
                           run_tag: str, outputs: list[str],
                           inputs: list[dict[str, str]] | None = None,
                           prefix: str | None = None,
                           extra: Mapping[str, Any] | None = None) -> str:
    """Write the worker manifest to the cache bucket -> its ``s3://`` URI.

    ``section`` is the dispatch key, ``prefix`` the staging word; they differ."""
    cache_bucket = (os.environ.get("TRID3NT_CACHE_BUCKET") or "").strip()
    if not cache_bucket:
        raise TelemacError(
            "TRID3NT_CACHE_BUCKET must be set to stage the TELEMAC manifest.",
            error_code="TELEMAC_STAGING_FAILED")
    from trid3nt_server import storage

    manifest = {section: dict(config), "run_id": run_tag,
                "inputs": list(inputs or []), "telemac_args": [],
                "outputs": list(outputs), **dict(extra or {})}
    key = f"{prefix or section}/{run_tag}/manifest.json"
    storage.client().put_object(
        Bucket=cache_bucket, Key=key,
        Body=json.dumps(manifest, indent=2).encode("utf-8"),
        ContentType="application/json")
    return f"s3://{cache_bucket}/{key}"


def _run_directory(run_tag: str) -> Path:
    rundir = Path(os.environ.get("TRID3NT_RUNS_DIR", "/tmp")) / f"telemac-{run_tag}"
    rundir.mkdir(parents=True, exist_ok=True)
    return rundir


def _cache_bucket() -> str:
    bucket = (os.environ.get("TRID3NT_CACHE_BUCKET") or "").strip()
    if not bucket:
        raise TelemacError("TRID3NT_CACHE_BUCKET must be set to stage an authored run.",
                           error_code="TELEMAC_STAGING_FAILED")
    return bucket


def _upload_authored(rundir: Path, run_tag: str, names: Sequence[str],
                     prefix: str) -> list[dict[str, str]]:
    """Upload every file this authoring wrote -> the manifest rows staging them."""
    from trid3nt_server import storage

    bucket = _cache_bucket()
    s3 = storage.client()
    rows: list[dict[str, str]] = []
    for name in names:
        key = f"{prefix}/{run_tag}/{name}"
        s3.put_object(Bucket=bucket, Key=key, Body=(rundir / name).read_bytes())
        rows.append({"gs_uri": f"s3://{bucket}/{key}", "dest": name})
    return rows


def _write_manifest(case: Mapping[str, Any], run_tag: str, *, outputs: list[str],
                    inputs: list[dict[str, str]], prefix: str) -> str:
    """Write the worker manifest for an authored case -> its ``s3://`` URI.

    Written by the one manifest writer, under the ``case`` dispatch key."""
    return stage_telemac_manifest(
        section="case", config=case, run_tag=run_tag, outputs=outputs,
        inputs=inputs, prefix=prefix)


def new_rundir() -> tuple[str, Path]:
    """A fresh run tag and the directory the run is authored into."""
    run_tag = new_ulid()
    return run_tag, _run_directory(run_tag)


async def stage_run(rundir: Path, run_tag: str, *, module: str, steering: str,
                    results: list[str], outputs: list[str],
                    mesh_inputs: list[dict[str, str]], prefix: str,
                    sheet: Mapping[str, Any], server_facts: Mapping[str, Any],
                    result_basename: str, user_fortran: Sequence[str] = (),
                    coupling: str | None = None,
                    continue_from: str | None = None,
                    cores: int = 1) -> dict[str, Any]:
    """An authored run directory -> the staged run the box receives.

    The manifest is written LAST, so it exists only for a fully staged run."""
    # Every file the authoring wrote, under its path INSIDE the run directory:
    # the oil module's user fortran is a directory the engine compiles, so the
    # walk is recursive and the manifest dest carries the same relative path.
    authored = sorted(str(p.relative_to(rundir))
                      for p in rundir.rglob("*") if p.is_file())
    inputs = [*mesh_inputs,
              *await asyncio.to_thread(_upload_authored, rundir, run_tag,
                                       authored, prefix)]
    case = case_section(
        module=module, steering=steering, results=results,
        user_fortran=user_fortran, coupling=coupling,
        continue_from=continue_from, cores=cores, server_facts=server_facts)
    # THE LAST READ BEFORE THE IMAGE: the directory the box will receive,
    # checked against itself. A run whose steering names a file nobody staged,
    # whose boundary file is numbered against another walk, whose bed has a
    # hole, whose clock disagrees with its own window or whose partition the box
    # cannot seat dies in the first second of the solve, so it refuses here.
    await asyncio.to_thread(
        check_staged_run, rundir, steering=steering, inputs=inputs,
        written_by_the_engine=[*results, *outputs],
        duration_s=server_facts.get("duration_s"), cores=cores)
    manifest_uri = await asyncio.to_thread(
        _write_manifest, case, run_tag, outputs=outputs, inputs=inputs,
        prefix=prefix)
    return {"run_tag": run_tag, "rundir": str(rundir), "sheet": dict(sheet),
            "case": case, "manifest_uri": manifest_uri, "authored": authored,
            "outputs": outputs, "inputs": inputs,
            "result_basename": result_basename}
