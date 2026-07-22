"""SWAN (Simulating WAves Nearshore) AWS Batch worker entrypoint.

FILE-ONLY SCAFFOLD (Phase 1; runs only inside the gated worker image -- never on
the agent box). The SWAN analogue of ``services/workers/geoclaw/entrypoint.py``.
Same OBJECT-STORE-IN -> RUN -> OBJECT-STORE-OUT envelope; SCHEME-AWARE (``s3://``
via boto3 when ``GRACE2_OBJECT_STORE=s3``, ``gs://`` via google-cloud-storage
otherwise). The worker contract is solver-agnostic, so the staging/upload/
completion envelope is copied verbatim from the GeoClaw worker; only the SOLVER
step + a deck-authoring step + the honesty-gate differ.

The SWAN-specific differences from the GeoClaw shim:

  1. DECK AUTHORING. GeoClaw authors a Python setrun.py; SWAN is configured by an
     ASCII ``.swn`` command file (copied to the file literally named ``INPUT``)
     that the worker AUTHORS from a staged ``build_spec`` (the agent stages a
     build_spec JSON + a bathy DEM, NOT a ready deck). We call
     ``deck_builder.build_swan_deck`` (deterministic, swan-free) which writes the
     ``INPUT`` command file + the bottom (bathymetry) input array, sampling the
     staged DEM onto the SWAN bottom grid via a ``depth_fn``.

  2. THE SOLVE. ``swanrun -input <casename> -omp $OMP_NUM_THREADS`` runs the
     headless OpenMP SWAN binary. ``swanrun`` APPENDS ``.swn`` to the case name,
     copies ``<casename>.swn`` to the file named ``INPUT``, then invokes
     ``swan.exe`` (which reads ``INPUT`` literally). The deck author writes
     ``swan_run.swn`` (``deck_builder.SWN_FILENAME``), so the case name is
     ``swan_run``. SWAN writes the gridded BLOCK output to ``swan_out.mat``, plus
     a ``PRINT`` diagnostics file and (on warnings/errors) an ``Errfile``.

  3. THE HONESTY GATE. SWAN often exits 0 even on a non-converged / errored run,
     and writes nonfatal warnings to the PRINT/Errfile. We classify the
     PRINT/Errfile (a real ERROR/SEVERE line, or no swan_out.mat) as a FAILURE so
     completion.json status is honest -- mirroring the MODFLOW list-file
     convergence guard / SWMM continuity-error gate. A 'complete' run with no
     wave output never reads status ok.

Contract (FR-CE-1/2/3 -- IDENTICAL to the GeoClaw worker, only the solver + the
``.swn`` deck author + the honesty gate differ):

    Input  (env or CLI):
        --run-id RUN_ID
        --manifest-uri s3://bucket/path/manifest.json
            JSON setup manifest. Schema:
                {
                  "inputs": [
                    {"gs_uri": "s3://.../bathy.tif", "dest": "bathy.tif"},
                    {"gs_uri": "s3://.../wind.dat", "dest": "wind.dat"},  # optional
                  ],
                  "build_spec": { ... deck_builder.SwanBuildSpec dict ... },
                  "outputs": ["swan_out.mat", "PRINT", "Errfile",
                              "deck_manifest.json", "swan.stdout", "swan.stderr"]
                }

    Output:
        <scheme>://${GRACE2_RUNS_BUCKET}/${RUN_ID}/<every output file>
        <scheme>://${GRACE2_RUNS_BUCKET}/${RUN_ID}/completion.json
            Terminal manifest (mirrors the GeoClaw completion schema; the
            stdout/stderr field names carry the ``swan_`` prefix). Truthful: this
            image asserts SWAN executed AND produced a non-empty swan_out.mat with
            no SEVERE/ERROR in the PRINT/Errfile -- not that the run is physically
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

LOG = logging.getLogger("grace2.worker.swan")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)

SCRATCH = Path(os.environ.get("GRACE2_SWAN_SCRATCH", "/opt/grace2/work"))
GCP_PROJECT = os.environ.get("GCP_PROJECT", "grace-2-hazard-prod")
RUNS_BUCKET = os.environ.get("GRACE2_RUNS_BUCKET", "grace-2-hazard-prod-runs")


def _utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# Object-store abstraction -- dispatch BY URI SCHEME (s3:// via boto3, gs:// via
# google-cloud-storage, both lazy-imported). Byte-identical to the GeoClaw worker.
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
    """Runs-bucket output scheme -- ``s3`` or ``gs`` (env ``GRACE2_OBJECT_STORE``)."""
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
# SWAN bathy-DEM sampler -> the deck_builder depth_fn.
# --------------------------------------------------------------------------- #
def _build_depth_fn(bathy_path: Path) -> Any:
    """Build a ``depth_fn(lon, lat) -> depth_m`` that samples the staged DEM.

    The staged ``fetch_topobathy`` DEM is positive-UP NAVD88 elevation (land > 0,
    seabed < 0). SWAN wants positive-DOWN still-water DEPTH, so we NEGATE the
    elevation: depth = -elevation (a 5 m-deep seabed at elevation -5 -> depth +5).
    Land cells (elevation > 0) become negative depth, which SWAN treats as dry.
    Returns ``None`` when rasterio / the DEM is unavailable (deck_builder then
    writes a flat demo bathymetry).
    """
    try:
        import numpy as np
        import rasterio
        from rasterio.warp import transform as _warp_transform
    except Exception as exc:  # noqa: BLE001
        LOG.warning("rasterio unavailable; using flat demo bathymetry: %s", exc)
        return None
    if not bathy_path.exists():
        LOG.warning("bathy DEM %s missing; using flat demo bathymetry", bathy_path)
        return None
    try:
        ds = rasterio.open(str(bathy_path))
    except Exception as exc:  # noqa: BLE001
        LOG.warning("could not open bathy DEM %s: %s; flat demo", bathy_path, exc)
        return None

    band = ds.read(1)
    nodata = ds.nodata
    dst_crs = ds.crs

    # The SWAN bottom grid is sampled at EPSG:4326 lon/lat nodes, but a staged
    # ``fetch_topobathy`` DEM is frequently in a PROJECTED CRS -- CUDEM coastal
    # tiles arrive in UTM (the live 2026-06-23 Mexico Beach tile is EPSG:32616,
    # metres). ``rasterio.DatasetReader.index`` interprets its arguments in the
    # DATASET CRS, so feeding raw lon/lat (-85.4, 29.9) into a UTM DEM lands
    # every query a million rows/columns out of bounds -> EVERY node falls back
    # to the flat 10 m demo depth. That produced a uniform bottom
    # (depth_min==depth_max==10.0, wet_cells=10201/10201) which sails past the
    # all-dry guard yet carries no real bathymetry -> SWAN solved a flat basin
    # and the wave raster was an invisible boundary sliver. Reproject the query
    # point 4326 -> dst_crs before indexing. A geographic DEM (lon/lat already)
    # needs no transform.
    _needs_reproj = bool(dst_crs) and not dst_crs.is_geographic
    _nodata_finite = nodata is not None and np.isfinite(float(nodata))

    # Sample bookkeeping: a DEM that covers NONE of the AOI grid (every node out
    # of bounds / no-data) is the silent-flat-demo failure mode -- the caller
    # inspects these to FAIL LOUD instead of solving a flat basin.
    stats: dict[str, int] = {"total": 0, "fallback": 0}

    def _depth(lon: float, lat: float) -> float:
        stats["total"] += 1
        x, y = lon, lat
        if _needs_reproj:
            try:
                xs, ys = _warp_transform("EPSG:4326", dst_crs, [lon], [lat])
                x, y = float(xs[0]), float(ys[0])
            except Exception:  # noqa: BLE001
                stats["fallback"] += 1
                return 10.0
        try:
            row, col = ds.index(x, y)
        except Exception:  # noqa: BLE001
            stats["fallback"] += 1
            return 10.0
        if 0 <= row < band.shape[0] and 0 <= col < band.shape[1]:
            elev = float(band[row, col])
            if _nodata_finite and np.isclose(elev, float(nodata)):
                stats["fallback"] += 1
                return 10.0
            if not np.isfinite(elev):
                stats["fallback"] += 1
                return 10.0
            return -elev  # positive-up elevation -> positive-down depth
        stats["fallback"] += 1
        return 10.0

    _depth.sample_stats = stats  # type: ignore[attr-defined]
    return _depth


# --------------------------------------------------------------------------- #
# SWAN deck-build + solve.
# --------------------------------------------------------------------------- #
def _author_deck(build_spec: dict, cwd: Path, bathy_path: Path) -> Any:
    """Author the SWAN deck (INPUT command file + bottom input) into ``cwd``.

    Delegates to ``deck_builder.build_swan_deck`` (deterministic, swan-free),
    passing a ``depth_fn`` that samples the staged bathy DEM. Returns the
    ``SwanDeckManifest``.
    """
    from services.workers.swan.deck_builder import build_swan_deck

    depth_fn = _build_depth_fn(bathy_path)
    manifest = build_swan_deck(build_spec, cwd, depth_fn=depth_fn)

    # Coverage guard: if a DEM was staged + opened but EVERY bottom node fell
    # back to the flat demo depth, the DEM does not overlap the AOI grid at all
    # (e.g. a CRS the sampler could not honour, or the wrong tile). A flat bottom
    # silently passes the all-dry guard (it reads as uniformly "wet"), so without
    # this check SWAN would solve a meaningless flat basin and paint an empty
    # raster. Fail loud + named instead (data-source norm: never a silent
    # all-flat dead-end).
    stats = getattr(depth_fn, "sample_stats", None) if depth_fn is not None else None
    if stats and stats.get("total", 0) > 0:
        fallback = stats.get("fallback", 0)
        total = stats["total"]
        LOG.info(
            "swan bathy sampling: %d/%d nodes fell back to flat demo depth "
            "(DEM coverage of the AOI grid)",
            fallback,
            total,
        )
        if fallback >= total:
            raise SwanBathyCoverageError(
                "SWAN bathy DEM covers NONE of the AOI grid: all %d bottom "
                "nodes fell back to the flat demo depth. The staged "
                "fetch_topobathy DEM does not overlap the AOI (wrong tile or an "
                "un-handled CRS). A flat bottom would silently pass the all-dry "
                "guard and SWAN would solve a meaningless flat basin -- failing "
                "loud instead. Stage a topo-bathymetry DEM that covers the AOI." % total
            )
    return manifest


class SwanBathyCoverageError(RuntimeError):
    """The staged bathy DEM covers NONE of the SWAN AOI grid (all-fallback).

    Distinct from ``SwanAllDryGridError``: there the bottom rendered but every
    cell is dry; here the bottom could not be sampled from the DEM at all (every
    node out of bounds / no-data), so the deck author wrote the flat demo depth
    everywhere. A flat 10 m bottom reads as uniformly WET, so it would slip past
    the all-dry guard and SWAN would solve a flat basin (the live 2026-06-23
    Mexico Beach invisible-raster bug: a UTM DEM sampled with lon/lat queries).
    """

    error_code = "SWAN_BATHY_NO_COVERAGE"


class SwanAllDryGridError(RuntimeError):
    """The rendered SWAN bottom grid has NO wet cells (every cell < DEPMIN).

    SWAN's bottom convention is positive-DOWN DEPTH: a cell is WET (active) only
    when its depth >= DEPMIN (0.05 m). When every cell is below DEPMIN the whole
    computational grid is dry/inactive, so SWAN "prepares computation", does ZERO
    sweeps, writes no ``swan_out.mat``, and prints "Normal end of run" in
    milliseconds -- the live 2026-06-23 Mexico Beach 33 ms no-op. This is almost
    always (a) a SIGN error in the bottom render, or (b) a LAND-ONLY DEM fed to a
    coastal AOI (e.g. the ``fetch_topobathy`` -> land-only ``fetch_dem`` fallback),
    so EVERY cell renders as land (negative depth). Raising a typed, named error
    here turns the opaque no-op into an actionable failure naming depth min/max.
    """

    error_code = "SWAN_ALL_DRY_GRID"


def _assert_bottom_has_wet_cells(cwd: Path) -> tuple[float, float, int, int]:
    """Assert the authored ``bottom.bot`` has >=1 wet cell, else raise SwanAllDryGridError.

    Reads the FREE-format depth grid the deck author wrote (positive-DOWN depth)
    and counts cells at or above DEPMIN (the SWAN wet threshold). A grid with NO
    wet cell is the exact all-dry no-op signature, so we FAIL FAST with the
    depth min/max named -- never let SWAN no-op opaquely. The DEPMIN + bottom
    filename are read from the deck_builder so this can never drift from the deck.

    Returns ``(depth_min, depth_max, wet_cell_count, total_cell_count)`` on success.
    Cells equal to the SWAN exception value (no-data) are excluded from the stats.
    """
    from services.workers.swan.deck_builder import (
        SWAN_DEPMIN_M,
        SWAN_EXCEPTION_VALUE,
    )

    bottom_path = cwd / "bottom.bot"
    if not bottom_path.exists():
        raise SwanAllDryGridError(
            "SWAN bottom grid 'bottom.bot' was not authored (no bathymetry to check)"
        )

    depths: list[float] = []
    text = bottom_path.read_text(errors="replace")
    for line in text.splitlines():
        for tok in line.split():
            try:
                v = float(tok)
            except ValueError:
                continue
            # Drop the SWAN exception / no-data sentinel from the stats.
            if abs(v - SWAN_EXCEPTION_VALUE) < 1e-6:
                continue
            depths.append(v)

    total = len(depths)
    if total == 0:
        raise SwanAllDryGridError(
            "SWAN bottom grid 'bottom.bot' has no numeric depth cells"
        )

    depth_min = min(depths)
    depth_max = max(depths)
    wet = sum(1 for d in depths if d >= SWAN_DEPMIN_M)
    LOG.info(
        "swan bottom grid: depth_min=%.3f depth_max=%.3f wet_cells=%d/%d "
        "(DEPMIN=%.3f m, wet = depth >= DEPMIN)",
        depth_min,
        depth_max,
        wet,
        total,
        SWAN_DEPMIN_M,
    )
    if wet == 0:
        raise SwanAllDryGridError(
            "SWAN bottom grid is ALL DRY -- 0 of %d cells are wet (depth >= DEPMIN "
            "%.3f m). depth range [%.3f, %.3f] m (positive-DOWN). This is the "
            "all-dry no-op: SWAN would prepare computation, run zero sweeps, write "
            "no swan_out.mat. Cause is almost always a LAND-ONLY DEM fed to a "
            "coastal AOI (fetch_topobathy -> land-only fetch_dem fallback) or a "
            "bottom-sign error. Stage a real seamless topo-bathymetry DEM with "
            "below-datum (negative-elevation -> positive-depth) sea cells."
            % (total, SWAN_DEPMIN_M, depth_min, depth_max)
        )
    return depth_min, depth_max, wet, total


def _run_swan(cwd: Path) -> tuple[int, Path, Path]:
    """Run SWAN headless in ``cwd``; capture stdout/stderr.

    Runs ``swanrun -input <casename> -omp $OMP_NUM_THREADS`` (the canonical
    headless SWAN launch). CRITICAL: the TU Delft ``swanrun`` launcher APPENDS
    ``.swn`` to the ``-input`` argument -- it looks for ``<casename>.swn``, copies
    it to the file literally named ``INPUT``, then invokes ``swan.exe`` (which
    reads ``INPUT``). So the argument MUST be the bare case name (NO ``.swn``, and
    NOT ``INPUT``). The deck author writes ``deck_builder.SWN_FILENAME``
    (``swan_run.swn``), so the case name is ``deck_builder.SWN_CASENAME``
    (``swan_run``).

    The earlier ``-input INPUT`` made swanrun hunt for the nonexistent
    ``INPUT.swn`` and abort with "file INPUT.swn does not exist" (exit 1) BEFORE
    SWAN ever solved -- the live 2026-06-23 Mexico Beach failure. We import the
    case name from the deck author so the runner + the authored ``.swn`` filename
    can never drift apart again.

    Returns ``(exit_code, stdout_path, stderr_path)``: 0 on a clean solve,
    non-zero on a solver failure.
    """
    from services.workers.swan.deck_builder import SWN_CASENAME

    stdout_path = cwd / "swan.stdout"
    stderr_path = cwd / "swan.stderr"

    omp = os.environ.get("OMP_NUM_THREADS", "1").strip() or "1"
    # GRACE2_SWAN_RUN overrides the launch for test/alternative drivers.
    default_cmd = f"swanrun -input {SWN_CASENAME} -omp {omp}"
    swan_cmd = os.environ.get("GRACE2_SWAN_RUN", default_cmd)

    out_fh = stdout_path.open("wb")
    err_fh = stderr_path.open("wb")
    rc = 0
    try:
        LOG.info("swan: exec %s (cwd=%s)", swan_cmd, cwd)
        proc = subprocess.run(
            swan_cmd,
            shell=True,
            cwd=str(cwd),
            stdout=out_fh,
            stderr=err_fh,
            check=False,
        )
        rc = proc.returncode
    finally:
        out_fh.close()
        err_fh.close()

    LOG.info(
        "swan exit=%d stdout_bytes=%d stderr_bytes=%d",
        rc,
        stdout_path.stat().st_size,
        stderr_path.stat().st_size,
    )
    return rc, stdout_path, stderr_path


def classify_swan_outcome(cwd: Path, exit_code: int) -> tuple[str, str | None]:
    """Classify a SWAN run as ('ok', None) or ('error', reason) -- the honesty gate.

    SWAN can exit 0 even on a non-converged / errored run and writes nonfatal
    warnings to ``PRINT`` / ``Errfile``. The gate (mirroring the MODFLOW list-file
    convergence guard / SWMM continuity gate) fails the run when:

      - the solver exit code is non-zero; OR
      - no ``swan_out.mat`` gridded output was produced (an empty solve); OR
      - the ``Errfile`` / ``PRINT`` carries a SEVERE / ERROR line (a real failure,
        as opposed to the routine "** Warning :" lines SWAN always emits).

    Pure file inspection -- unit-testable on a synthetic cwd. Returns
    ``(status, error_message_or_None)``.
    """
    if exit_code != 0:
        return "error", f"swan exited with non-zero code {exit_code}"

    mat = cwd / "swan_out.mat"
    if not mat.exists() or mat.stat().st_size == 0:
        return "error", "swan produced no swan_out.mat gridded output (empty solve)"

    # Scan the Errfile + PRINT for a real error line. SWAN tags severity as
    # "** Severe error" / "** Error" (fatal) vs "** Warning" (routine).
    for diag_name in ("Errfile", "PRINT", "swan.stderr"):
        diag = cwd / diag_name
        if not diag.exists():
            continue
        try:
            text = diag.read_text(errors="replace")
        except Exception:  # noqa: BLE001
            continue
        for line in text.splitlines():
            low = line.lower()
            if "severe error" in low or "** error" in low or "fatal" in low:
                return "error", f"SWAN reported a fatal error in {diag_name}: {line.strip()}"

    return "ok", None


def _expand_outputs(patterns: list[str], cwd: Path) -> list[Path]:
    """Recursive glob over the scratch tree (captures swan_out.mat + diagnostics)."""
    seen: set[Path] = set()
    for pat in patterns:
        for hit in glob.glob(str(cwd / pat), recursive=True):
            p = Path(hit)
            if p.is_file():
                seen.add(p.resolve())
    return sorted(seen)


def _build_argv_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="grace2-swan-entrypoint",
        description="GRACE-2 SWAN spectral wave AWS Batch worker entrypoint (FR-CE-1/2/3).",
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
    LOG.info("swan postprocess: wrote %s", uri)
    return uri


def _write_completion(
    run_id: str,
    status: str,
    exit_code: int,
    output_uris: list[str],
    stdout_uri: str | None,
    stderr_uri: str | None,
    mode: str | None,
    started_at: str,
    error: str | None,
    error_code: str | None = None,
    publish_manifest_uri: str | None = None,
) -> str:
    payload = {
        "run_id": run_id,
        "status": status,
        "exit_code": exit_code,
        "swan_stdout_uri": stdout_uri,
        "swan_stderr_uri": stderr_uri,
        "mode": mode,
        "output_uris": output_uris,
        "started_at": started_at,
        "finished_at": _utc_now(),
        "error": error,
        "error_code": error_code,
        "publish_manifest_uri": publish_manifest_uri,
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


#: Default output globs -- the SWAN gridded output + diagnostics the postprocess /
#: honesty gate read.
DEFAULT_OUTPUT_GLOBS: list[str] = [
    "swan_out.mat",
    "deck_manifest.json",
    "PRINT",
    "Errfile",
    "swan.stdout",
    "swan.stderr",
    "*.tif",
]

#: Diagnostic-file globs uploaded UNCONDITIONALLY (even on an early failure /
#: exception) so the next Batch run is conclusive. swanrun names the SWAN print
#: file after the case (``swan_run.prt``) OR writes a literal ``PRINT``; SWAN
#: writes errors to ``Errfile`` / ``Errpts`` / ``swan_run.erf``. The PRINT file
#: carries SWAN's active-sea-point count + grid diagnostics that confirm the
#: all-dry-grid theory -- it must reach S3 regardless of how the run ends. The
#: ``bottom.bot`` + authored ``swan_run.swn`` / ``INPUT`` are included so the deck
#: that produced the run is always inspectable.
DIAGNOSTIC_GLOBS: list[str] = [
    "PRINT",
    "*.prt",
    "Errfile",
    "Errpts",
    "*.erf",
    "swan_run.swn",
    "INPUT",
    "bottom.bot",
    "deck_manifest.json",
]


def _upload_diagnostics(run_id: str, cwd: Path) -> list[str]:
    """Upload SWAN's PRINT / Errfile / deck diagnostics to the runs bucket.

    Highest-value evidence path: the PRINT file holds SWAN's active-sea-point
    count + grid diagnostics. Best-effort per file (one failed upload never
    blocks the others) and safe to call from BOTH the success path and the
    exception handler -- so the diagnostics survive even when the run dies on the
    node before the normal output-expansion loop. Returns the uploaded URIs.
    """
    uris: list[str] = []
    for path in _expand_outputs(list(DIAGNOSTIC_GLOBS), cwd):
        rel = path.relative_to(cwd).as_posix()
        try:
            uris.append(_upload(path, _runs_uri(run_id, rel)))
        except Exception as exc:  # noqa: BLE001 -- one bad upload must not block others
            LOG.warning("diagnostic upload failed for %s: %s", rel, exc)
    return uris


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
        "grace-2-swan-solver starting - project=%s run_id=%s manifest=%s "
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
    error_msg: str | None = None
    error_code: str | None = None
    publish_manifest_uri: str | None = None
    mode: str | None = None
    exit_code = 1
    status = "error"
    scratch: Path | None = None

    try:
        manifest = _read_manifest(manifest_uri)
        inputs = manifest.get("inputs", []) or []
        build_spec = manifest.get("build_spec") or {}
        outputs = manifest.get("outputs") or DEFAULT_OUTPUT_GLOBS
        if isinstance(build_spec, dict):
            mode = str(build_spec.get("mode") or "") or None

        scratch = _prepare_scratch()

        # Stage inputs (bathy DEM + optional wind grid).
        bathy_path = scratch / "bathy.tif"
        for item in inputs:
            input_uri = item["gs_uri"]
            dest = scratch / item["dest"]
            _download(input_uri, dest)

        # Author the deck (INPUT command file + bottom input) into the scratch dir,
        # sampling the staged DEM onto the SWAN bottom grid.
        deck_manifest = _author_deck(build_spec, scratch, bathy_path)
        mode = deck_manifest.mode
        LOG.info(
            "swan deck authored: mode=%s files=%s driver=%s",
            deck_manifest.mode,
            deck_manifest.files_written,
            deck_manifest.driver_descriptor,
        )

        # All-dry guard (FAIL FAST, pre-solve): if the rendered bottom grid has NO
        # wet cell (every cell < DEPMIN) SWAN would no-op silently. Raise a typed,
        # named SWAN_ALL_DRY_GRID error naming depth min/max BEFORE wasting a solve.
        # The authored bottom.bot is still uploaded by the diagnostics path below.
        _assert_bottom_has_wet_cells(scratch)

        rc, stdout_path, stderr_path = _run_swan(scratch)

        # Always upload stdout/stderr so the run produces evidence.
        stdout_uri = _upload(stdout_path, _runs_uri(run_id, "swan.stdout"))
        stderr_uri = _upload(stderr_path, _runs_uri(run_id, "swan.stderr"))

        for path in _expand_outputs(list(outputs), scratch):
            rel = path.relative_to(scratch).as_posix()
            uri = _upload(path, _runs_uri(run_id, rel))
            output_uris.append(uri)

        # Honesty gate: classify the SWAN outcome from the PRINT/Errfile + output.
        status, error_msg = classify_swan_outcome(scratch, rc)
        exit_code = rc if status == "ok" else (rc or 1)

        if status == "ok":
            try:
                from services.workers._swan_postprocess import run_swan_postprocess
                pp = run_swan_postprocess(
                    run_id=run_id,
                    scratch=scratch,
                    build_spec=build_spec,
                    runs_uri_for=lambda rel: _runs_uri(run_id, rel),
                )
                if pp.status == "ok" and pp.manifest is not None:
                    publish_manifest_uri = _write_publish_manifest(run_id, pp.manifest)
                    LOG.info("swan postprocess ok: publish_manifest_uri=%s", publish_manifest_uri)
                else:
                    error_code = pp.error_code
                    LOG.warning("swan postprocess honesty gate: %s %s", pp.error_code, pp.error_message)
            except Exception as pp_exc:
                LOG.warning("swan postprocess failed (non-fatal): %s", pp_exc)

        if publish_manifest_uri and publish_manifest_uri not in output_uris:
            output_uris.append(publish_manifest_uri)

    except SwanBathyCoverageError as exc:
        LOG.error("swan bathy no-coverage: %s", exc)
        error_msg = str(exc)
        error_code = SwanBathyCoverageError.error_code
        exit_code = 1
        status = "error"
    except SwanAllDryGridError as exc:
        LOG.error("swan all-dry grid: %s", exc)
        error_msg = str(exc)
        error_code = SwanAllDryGridError.error_code
        exit_code = 1
        status = "error"
    except Exception as exc:  # pragma: no cover -- defensive, logged + emitted
        LOG.exception("solver entrypoint failed")
        error_msg = f"{type(exc).__name__}: {exc}"
        exit_code = 1
        status = "error"

    # ALWAYS upload SWAN's diagnostics (PRINT / Errfile / authored deck) -- even on
    # an early failure / exception -- so the next Batch run is conclusive (the PRINT
    # file carries SWAN's active-sea-point count + grid diagnostics). Best-effort:
    # never let a diagnostic-upload error mask the real outcome.
    if scratch is not None:
        try:
            diag_uris = _upload_diagnostics(run_id, scratch)
            for u in diag_uris:
                if u not in output_uris:
                    output_uris.append(u)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("diagnostic upload sweep failed: %s", exc)

    _write_completion(
        run_id=run_id,
        status=status,
        exit_code=exit_code,
        output_uris=output_uris,
        stdout_uri=stdout_uri,
        stderr_uri=stderr_uri,
        mode=mode,
        started_at=started_at,
        error=error_msg,
        error_code=error_code,
        publish_manifest_uri=publish_manifest_uri,
    )
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
