"""GeoClaw (Clawpack) AWS Batch worker entrypoint — shallow-water inundation.

The GeoClaw analogue of ``services/workers/swmm/entrypoint.py`` /
``services/workers/modflow/entrypoint.py``. Same OBJECT-STORE-IN -> RUN ->
OBJECT-STORE-OUT envelope; SCHEME-AWARE (``s3://`` via boto3 when
``GRACE2_OBJECT_STORE=s3``, ``gs://`` via google-cloud-storage otherwise). The
worker contract is solver-agnostic, so the staging/upload/completion envelope is
copied verbatim from the SWMM worker; only the SOLVER step + a deck-authoring
step differ.

The GeoClaw-specific differences from the SWMM shim:

  1. DECK AUTHORING. SWMM takes a ready ``.inp`` deck; GeoClaw is configured by a
     Python ``setrun.py`` that the worker AUTHORS from a staged ``build_spec``
     (the agent stages a build_spec JSON + a topo DEM, NOT a ready deck). We call
     ``setrun_builder.build_geoclaw_deck`` (deterministic, clawpack-free) to write
     ``setrun.py`` + the scenario source file (qinit.xyz / maketopo.py), then run
     the Clawpack ``make`` / ``runclaw`` machinery against it.

  2. The DEM is converted to GeoClaw topotype-3 (ESRI ASCII with a GeoClaw
     header) before the solve — GeoClaw reads ASCII/topotype topo, not COG. The
     agent stages the DEM (any rasterio-readable form, typically an ESRI ASCII
     ``.asc``); we accept it as-is and reference it from setrun.

  3. The solver output is GeoClaw fort.q frames under ``_output/`` (the AMR
     ASCII dumps). The output globs capture ``_output/fort.q*`` + ``_output/fort.t*``
     + ``_output/fort.h*`` (the headers postprocess needs) so the agent-side
     ``postprocess_geoclaw`` rasterizes each frame -> depth COG.

Contract (FR-CE-1/2/3 — IDENTICAL to the SWMM worker, only the solver + a
``build_spec`` manifest field + the output globs differ):

    Input  (env or CLI):
        --run-id RUN_ID
        --manifest-uri s3://bucket/path/manifest.json
            JSON setup manifest. Schema:
                {
                  "inputs": [
                    {"gs_uri": "s3://.../topo.asc", "dest": "topo.asc"},
                    {"gs_uri": "s3://.../dtopo.tt3", "dest": "dtopo.tt3"},  # optional
                  ],
                  "build_spec": { ... setrun_builder.GeoClawBuildSpec dict ... },
                  "outputs": ["_output/fort.q*", "_output/fort.t*",
                              "_output/fort.h*", "deck_manifest.json"]
                }

    Output:
        <scheme>://${GRACE2_RUNS_BUCKET}/${RUN_ID}/<every output file>
        <scheme>://${GRACE2_RUNS_BUCKET}/${RUN_ID}/completion.json
            Terminal manifest (mirrors the SWMM completion schema; the
            stdout/stderr field names carry the ``geoclaw_`` prefix). Truthful:
            this image asserts only that Clawpack executed and produced fort.q
            output — NOT that the run is physically valid.
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

LOG = logging.getLogger("grace2.worker.geoclaw")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)

SCRATCH = Path(os.environ.get("GRACE2_GEOCLAW_SCRATCH", "/opt/grace2/work"))
GCP_PROJECT = os.environ.get("GCP_PROJECT", "legacy-cloud-project")
RUNS_BUCKET = os.environ.get("GRACE2_RUNS_BUCKET", "trid3nt-runs")


def _utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# Object-store abstraction — dispatch BY URI SCHEME (s3:// via boto3, gs:// via
# google-cloud-storage, both lazy-imported). Byte-identical to the SWMM/MODFLOW
# worker: the worker contract is solver-agnostic.
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
# GeoClaw deck-build + solve.
# --------------------------------------------------------------------------- #
def _author_deck(build_spec: dict, cwd: Path) -> Any:
    """Author the GeoClaw deck (setrun.py + scenario source) into ``cwd``.

    Delegates to ``setrun_builder.build_geoclaw_deck`` (deterministic,
    clawpack-free). Returns the ``DeckManifest``.
    """
    from services.workers.geoclaw.setrun_builder import build_geoclaw_deck

    return build_geoclaw_deck(build_spec, cwd)


#: Cap on the topotype-3 topo grid size PER AXIS. The agent stages a full-res
#: topo/bathy DEM (e.g. ~28 million cells / ~400 MB ASCII over an offshore-extended
#: domain) -- GeoClaw samples the (high-res) topo onto the COMPUTATIONAL grid, so a
#: topo far finer than the finest AMR cell (tens of metres) is wasted I/O that
#: bloats the read/write and slows every step. We integer-decimate any DEM finer
#: than this cap (uniform stride, square cells preserved) so the topo stays a few
#: million cells max while remaining finer than the finest grid cell. General: a
#: DEM already under the cap is left untouched.
_GEOCLAW_TOPO_MAX_CELLS_PER_AXIS: int = 2000


#: Scenarios whose driver is an OFFSHORE seafloor (Okada) source. For these the
#: computational domain extends into the ocean, so a topo cell with NO data (the
#: UTM->4326 warp-corner NaNs) must initialize WET (deep ocean) rather than as dry
#: land -- otherwise the source has no water column to displace. Mirrors the
#: composer's GEOCLAW_OFFSHORE_SCENARIOS.
_OFFSHORE_SCENARIOS: frozenset[str] = frozenset({"tsunami"})

#: Flat-ocean validation gate (P0.3) thresholds. After staging/merging the topo
#: for an OFFSHORE (tsunami) scenario the bathymetry that reaches the solver MUST
#: be genuinely-negative ocean -- a flat ~0 m land-DEM fill holds no water column,
#: so the Okada source has nothing to displace and the run inundates nothing
#: (Total mass ~ 1e5 vs the working proof's ~1e9). The gate asserts BOTH:
#:   - ``min(B) < _OFFSHORE_FLAT_MIN_DEPTH_M`` (somewhere genuinely deep), AND
#:   - the fraction of cells below ``_OFFSHORE_FLAT_WET_THRESHOLD_M`` is at least
#:     ``_OFFSHORE_FLAT_MIN_WET_FRACTION`` (a real ocean area, not a noise sliver).
#: A flat ~-0.7 m fake ocean fails both. A genuine (even shallow-shelf) ocean
#: passes. On failure the worker raises GeoClawBathymetryFlatError so a future
#: bathymetry regression is a LOUD typed failure, never a silent dry solve.
_OFFSHORE_FLAT_MIN_DEPTH_M: float = -5.0
_OFFSHORE_FLAT_WET_THRESHOLD_M: float = -2.0
_OFFSHORE_FLAT_MIN_WET_FRACTION: float = 0.05


class GeoClawBathymetryFlatError(RuntimeError):
    """The staged OFFSHORE topo is effectively flat / non-negative -- no genuine
    ocean reaches the solver, so the run would inundate nothing.

    Raised by the flat-ocean validation gate (P0.3) instead of running a doomed
    dry simulation. ``error_code`` rides into the completion manifest so the
    failure is a named, debuggable typed error (not a silent zero-inundation)."""

    error_code: str = "GEOCLAW_BATHYMETRY_FLAT"


def _convert_one_topo_to_topotype3(path: Path, *, offshore: bool) -> dict[str, float]:
    """Rewrite ONE staged topo DEM in place as a genuine GeoClaw topotype-3 ASCII.

    The agent stages the topo/bathy DEM as a GeoTIFF/COG (the reprojected
    EPSG:4326 raster), but ``setrun.py`` references it as ``[3, "topo.asc"]`` --
    GeoClaw topotype 3 = ESRI/GeoClaw-header ASCII. GeoClaw's Fortran reader CANNOT
    parse GeoTIFF bytes, so a GeoTIFF staged as ``topo.asc`` loads NO bathymetry;
    the still-water IC ``h = max(0, sea_level - B)`` then finds no cell below sea
    level and ``Total mass at initial time`` is 0 -- the domain runs DRY and no
    tsunami is generated. We read the raster with rasterio and re-emit a real
    topotype-3 ASCII (negative = below sea level preserved; NO sign flip).

    nodata handling (P0.2): the reprojected raster has NaN regions in the warp
    corners. For an INLAND (dam_break) run missing cells become the highest value
    (dry land) so it is not spuriously flooded. For an OFFSHORE (tsunami) run the
    ocean depth MUST come from real bathymetry (the ETOPO base the composer now
    forces), NEVER from a land-DEM min: filling with ``nanmin`` of a land-only DEM
    manufactures a flat ~-0.7 m fake ocean that silently passes downstream. So we
    fill offshore missing cells with the deepest GENUINE ocean value ONLY when the
    DEM actually contains below-water cells; if it has NONE, we fill as land (the
    P0.3 gate then fails loudly rather than inventing an ocean).

    Returns a stats dict ``{min, max, wet_fraction}`` (over the final, decimated +
    filled band) so the caller can run the flat-ocean validation gate. A file that
    is ALREADY a GeoClaw topotype-3 ASCII is not a valid GDAL raster header
    (value-first layout), so ``rasterio.open`` raises and the caller leaves it
    untouched (idempotent).
    """
    import numpy as np  # noqa: WPS433 - worker image deps
    import rasterio  # noqa: WPS433
    from clawpack.geoclaw import topotools  # noqa: WPS433

    with rasterio.open(str(path)) as ds:
        band = ds.read(1).astype("float64")
        nod = ds.nodata
        tr = ds.transform
        nx, ny = int(ds.width), int(ds.height)
        # An axis-aligned ESRI grid needs no rotation/shear and square cells.
        if abs(tr.b) > 1e-12 or abs(tr.d) > 1e-12:
            raise ValueError("topo transform is rotated; cannot emit topotype-3")
        dx, dy = float(tr.a), float(tr.e)
        if abs(abs(dx) - abs(dy)) > 1e-6 * max(abs(dx), abs(dy)):
            raise ValueError(
                f"topo cells not square (dx={dx}, dy={dy}); topotype-3 needs one cellsize"
            )
        # cell-CENTER coordinates; raster rows run north -> south (dy < 0).
        xs = tr.c + (np.arange(nx) + 0.5) * dx
        ys = tr.f + (np.arange(ny) + 0.5) * dy

    # Downsample a too-fine DEM by an integer stride (uniform spacing + square
    # cells preserved) so the topotype-3 ASCII stays a few million cells max --
    # GeoClaw samples topo onto the (coarser) computational grid, so a DEM finer
    # than the finest AMR cell only bloats read/write. A DEM already under the cap
    # is untouched. Done BEFORE the nodata fill so the fill follows the new grid.
    stride = 1
    cap = int(_GEOCLAW_TOPO_MAX_CELLS_PER_AXIS)
    if cap > 0 and max(nx, ny) > cap:
        stride = int(np.ceil(max(nx, ny) / float(cap)))
    if stride > 1:
        band = band[::stride, ::stride]
        xs = xs[::stride]
        ys = ys[::stride]
        new_ny, new_nx = band.shape
        LOG.info(
            "topo downsample: %s %dx%d -> %dx%d (stride=%d, cell %.6g -> %.6g deg)",
            path.name,
            nx,
            ny,
            new_nx,
            new_ny,
            stride,
            abs(dx),
            abs(dx) * stride,
        )
        nx, ny = new_nx, new_ny

    # Mask numeric nodata -> NaN (NaN nodata is already NaN).
    if nod is not None and not np.isnan(np.float64(nod)):
        band = np.where(band == np.float64(nod), np.nan, band)
    finite = np.isfinite(band)
    if not finite.any():
        raise ValueError("topo has no finite cells")
    if not finite.all():
        if offshore:
            # Ocean fill must be REAL bathymetry, not a land-DEM min (P0.2). Use
            # the deepest GENUINE below-water cell only when the DEM contains
            # ocean; if it has none, fill as land (highest value) so the P0.3
            # flat-ocean gate fails loudly instead of faking a thin ocean.
            ocean = finite & (band < _OFFSHORE_FLAT_WET_THRESHOLD_M)
            fill = (
                float(np.nanmin(band))
                if bool(ocean.any())
                else float(np.nanmax(band))
            )
        else:
            fill = float(np.nanmax(band))
        band = np.where(finite, band, fill)

    # topotools.Topography expects ASCENDING x and y (south-up Z). The write
    # re-orders to GeoClaw's north-up topotype-3 layout with a correct header.
    xorder = np.argsort(xs)
    yorder = np.argsort(ys)
    xs_asc = xs[xorder]
    ys_asc = ys[yorder]
    Z = band[np.ix_(yorder, xorder)]

    topo = topotools.Topography()
    topo.set_xyZ(xs_asc, ys_asc, Z)
    tmp = path.with_name(path.name + ".tt3.tmp")
    topo.write(str(tmp), topo_type=3)
    os.replace(str(tmp), str(path))
    min_b = float(np.nanmin(band))
    max_b = float(np.nanmax(band))
    wet_fraction = float(np.count_nonzero(band < _OFFSHORE_FLAT_WET_THRESHOLD_M))
    wet_fraction = wet_fraction / float(band.size) if band.size else 0.0
    LOG.info(
        "topo normalize: %s -> topotype-3 ASCII (%dx%d, min=%.2f max=%.2f, "
        "wet_frac<%.0fm=%.3f, offshore=%s)",
        path.name,
        nx,
        ny,
        min_b,
        max_b,
        _OFFSHORE_FLAT_WET_THRESHOLD_M,
        wet_fraction,
        offshore,
    )
    return {"min": min_b, "max": max_b, "wet_fraction": wet_fraction}


def _validate_offshore_bathymetry(primary_name: str, stats: dict[str, float] | None) -> None:
    """Flat-ocean validation gate (P0.3) for an OFFSHORE (tsunami) primary topo.

    Asserts the staged bathymetry that reaches the solver is genuinely-negative
    ocean (deep somewhere AND a real wet area), else raises
    ``GeoClawBathymetryFlatError`` so a flat ~0 m land-DEM fill becomes a LOUD
    typed failure instead of a silent zero-inundation dry solve. ``stats`` is the
    dict ``_convert_one_topo_to_topotype3`` returned for the primary topo; ``None``
    (conversion skipped/failed) is a non-fatal warning -- the run then fails loudly
    on its own downstream rather than mislabelling the cause here.
    """
    if stats is None:
        LOG.warning(
            "flat-ocean gate: no stats for offshore primary topo %s (conversion "
            "skipped/failed); skipping the gate -- the solve will surface any "
            "bathymetry problem downstream",
            primary_name,
        )
        return
    min_b = float(stats.get("min", 0.0))
    wet_fraction = float(stats.get("wet_fraction", 0.0))
    deep_enough = min_b < _OFFSHORE_FLAT_MIN_DEPTH_M
    wet_enough = wet_fraction >= _OFFSHORE_FLAT_MIN_WET_FRACTION
    if deep_enough and wet_enough:
        LOG.info(
            "flat-ocean gate PASS: %s min=%.2f m wet_frac=%.3f "
            "(need min<%.1f m and wet_frac>=%.2f)",
            primary_name, min_b, wet_fraction,
            _OFFSHORE_FLAT_MIN_DEPTH_M, _OFFSHORE_FLAT_MIN_WET_FRACTION,
        )
        return
    raise GeoClawBathymetryFlatError(
        f"GEOCLAW_BATHYMETRY_FLAT: the staged OFFSHORE topo {primary_name!r} is "
        f"effectively flat / non-negative (min={min_b:.2f} m, wet_fraction below "
        f"{_OFFSHORE_FLAT_WET_THRESHOLD_M:.0f} m = {wet_fraction:.3f}); a genuine "
        f"ocean requires min < {_OFFSHORE_FLAT_MIN_DEPTH_M:.1f} m AND wet_fraction "
        f">= {_OFFSHORE_FLAT_MIN_WET_FRACTION:.2f}. No real negative bathymetry "
        "reached the solver, so the Okada source has no water column to displace "
        "and the run would inundate nothing. Refusing the doomed dry simulation "
        "(check the topobathy source covers the full offshore-extended domain)."
    )


def _normalize_topo_files(scratch: Path, build_spec: dict) -> None:
    """Convert every staged topo DEM (primary + extra) to topotype-3 ASCII.

    Called AFTER staging + BEFORE the deck authors ``setrun.py`` (which keeps
    referencing the same filenames). Best-effort per file: a conversion failure is
    logged and the original is left in place so the run proceeds and fails loudly
    downstream rather than here.

    For an OFFSHORE (tsunami) scenario the PRIMARY topo is then run through the
    flat-ocean validation gate (P0.3) -- a genuinely-flat ocean raises
    ``GeoClawBathymetryFlatError`` (NOT swallowed) so the worker fails with a named
    typed error instead of running a zero-inundation dry solve.
    """
    if not isinstance(build_spec, dict):
        return
    scenario = str(build_spec.get("scenario") or "").strip().lower()
    offshore = scenario in _OFFSHORE_SCENARIOS
    primary_name = str(build_spec.get("topo_file") or "topo.asc")
    names = [primary_name]
    names.extend(str(f) for f in (build_spec.get("extra_topo_files") or []))
    primary_stats: dict[str, float] | None = None
    for name in names:
        path = scratch / name
        if not path.exists():
            continue
        try:
            stats = _convert_one_topo_to_topotype3(path, offshore=offshore)
            if name == primary_name:
                primary_stats = stats
        except Exception as exc:  # noqa: BLE001 - best-effort; keep the original
            LOG.warning(
                "topo normalize: could not convert %s to topotype-3 (%s); "
                "leaving it as staged",
                path,
                exc,
            )
    # Flat-ocean gate (P0.3): offshore primary topo MUST be genuinely-negative.
    if offshore:
        _validate_offshore_bathymetry(primary_name, primary_stats)


def _run_geoclaw(cwd: Path) -> tuple[int, Path, Path]:
    """Author-then-solve GeoClaw headless in ``cwd``; capture stdout/stderr.

    Runs, in order, inside ``cwd``:
      1. (tsunami synthetic source only) ``python maketopo.py`` to write dtopo.tt3.
      2. ``make .output`` (Clawpack's standard headless solve target) which
         compiles the GeoClaw Fortran (if needed) and runs the solver, writing
         fort.q frames under ``_output/``.

    GeoClaw ships an ``$CLAW`` Makefile machinery; the ``.output`` rule lives in
    ``$(CLAW)/clawutil/src/Makefile.common`` and is only usable once a
    per-application ``Makefile`` (setting CLAW_PKG/EXE/SETRUN_FILE/OUTDIR + the
    GeoClaw module/source lists) includes it. The deck author
    (``setrun_builder.build_geoclaw_deck``) WRITES that ``Makefile`` into ``cwd``
    alongside ``setrun.py`` -- without it ``make .output`` fails instantly with
    "No rule to make target '.output'". Returns
    ``(exit_code, stdout_path, stderr_path)``: 0 on a clean solve, non-zero on a
    compile/solve failure.

    The Clawpack import is INSIDE the generated ``maketopo.py`` + the Fortran
    build — never imported by this module — so the deck-author path stays testable
    without a Fortran toolchain.
    """
    stdout_path = cwd / "geoclaw.stdout"
    stderr_path = cwd / "geoclaw.stderr"

    out_fh = stdout_path.open("wb")
    err_fh = stderr_path.open("wb")
    rc = 0
    try:
        # Step 1: synthesize the dtopo if a maketopo.py was authored (tsunami).
        if (cwd / "maketopo.py").exists():
            LOG.info("geoclaw: running maketopo.py (synthetic dtopo)")
            proc = subprocess.run(
                [sys.executable, "maketopo.py"],
                cwd=str(cwd),
                stdout=out_fh,
                stderr=err_fh,
                check=False,
            )
            if proc.returncode != 0:
                LOG.error("maketopo.py failed rc=%d", proc.returncode)
                return proc.returncode, stdout_path, stderr_path

        # Step 2: the headless Clawpack solve. ``make .output`` is the canonical
        # target; GRACE2_GEOCLAW_MAKE overrides it for test/alternative drivers.
        make_target = os.environ.get("GRACE2_GEOCLAW_MAKE", "make .output")
        LOG.info("geoclaw: exec %s (cwd=%s)", make_target, cwd)
        proc = subprocess.run(
            make_target,
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
        "geoclaw exit=%d stdout_bytes=%d stderr_bytes=%d",
        rc,
        stdout_path.stat().st_size,
        stderr_path.stat().st_size,
    )
    return rc, stdout_path, stderr_path


def _expand_outputs(patterns: list[str], cwd: Path) -> list[Path]:
    """Recursive glob over the scratch tree (captures ``_output/fort.*``)."""
    seen: set[Path] = set()
    for pat in patterns:
        for hit in glob.glob(str(cwd / pat), recursive=True):
            p = Path(hit)
            if p.is_file():
                seen.add(p.resolve())
    return sorted(seen)


def _build_argv_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="grace2-geoclaw-entrypoint",
        description="GeoClaw (Clawpack) AWS Batch worker entrypoint (FR-CE-1/2/3).",
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
    LOG.info("geoclaw postprocess: wrote %s", uri)
    return uri


def _write_completion(
    run_id: str,
    status: str,
    exit_code: int,
    output_uris: list[str],
    stdout_uri: str | None,
    stderr_uri: str | None,
    scenario: str | None,
    started_at: str,
    error: str | None,
    publish_manifest_uri: str | None = None,
    error_code: str | None = None,
) -> str:
    payload = {
        "run_id": run_id,
        "status": status,
        "exit_code": exit_code,
        "geoclaw_stdout_uri": stdout_uri,
        "geoclaw_stderr_uri": stderr_uri,
        "scenario": scenario,
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


#: Default output globs - the GeoClaw AMR ASCII frames + headers postprocess reads,
#: plus the fgmax (max depth/speed/arrival) + gauge time-series outputs.
DEFAULT_OUTPUT_GLOBS: list[str] = [
    "_output/fort.q*",
    "_output/fort.t*",
    "_output/fort.h*",
    "_output/fort.b*",
    "_output/fgmax*.txt",
    "_output/fgmax_grids.data",
    "_output/gauge*.txt",
    "deck_manifest.json",
    "*.tif",
]


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
        "trid3nt-geoclaw-solver starting - project=%s run_id=%s manifest=%s "
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
    scenario: str | None = None
    exit_code = 1
    status = "error"
    publish_manifest_uri: str | None = None
    error_code: str | None = None

    try:
        manifest = _read_manifest(manifest_uri)
        inputs = manifest.get("inputs", []) or []
        build_spec = manifest.get("build_spec") or {}
        outputs = manifest.get("outputs") or DEFAULT_OUTPUT_GLOBS
        if isinstance(build_spec, dict):
            scenario = str(build_spec.get("scenario") or "") or None

        scratch = _prepare_scratch()

        # Stage inputs (topo DEM + optional dtopo / surge hydrograph).
        for item in inputs:
            input_uri = item["gs_uri"]
            dest = scratch / item["dest"]
            _download(input_uri, dest)

        # Convert the staged topo DEM(s) from GeoTIFF/COG to genuine topotype-3
        # ESRI ASCII -- setrun.py references them as [3, "topo.asc"] and GeoClaw's
        # Fortran reader cannot parse GeoTIFF bytes, so without this the bathymetry
        # never loads and the run starts DRY (Total mass = 0 -> no tsunami).
        _normalize_topo_files(scratch, build_spec if isinstance(build_spec, dict) else {})

        # Author the deck (setrun.py + scenario source) into the scratch dir.
        deck_manifest = _author_deck(build_spec, scratch)
        scenario = deck_manifest.scenario
        LOG.info(
            "geoclaw deck authored: scenario=%s files=%s driver=%s",
            deck_manifest.scenario,
            deck_manifest.files_written,
            deck_manifest.driver_descriptor,
        )

        rc, stdout_path, stderr_path = _run_geoclaw(scratch)

        # Always upload stdout/stderr so the run produces evidence.
        stdout_uri = _upload(stdout_path, _runs_uri(run_id, "geoclaw.stdout"))
        stderr_uri = _upload(stderr_path, _runs_uri(run_id, "geoclaw.stderr"))

        for path in _expand_outputs(list(outputs), scratch):
            rel = path.relative_to(scratch).as_posix()
            uri = _upload(path, _runs_uri(run_id, rel))
            output_uris.append(uri)

        exit_code = rc
        status = "ok" if rc == 0 else "error"
        if rc != 0:
            error_msg = f"geoclaw worker exited with non-zero code {rc}"

        if rc == 0:
            try:
                from services.workers._geoclaw_postprocess import run_geoclaw_postprocess
                pp = run_geoclaw_postprocess(
                    run_id=run_id,
                    scratch=scratch,
                    build_spec=build_spec if isinstance(build_spec, dict) else {},
                    runs_uri_for=lambda rel: _runs_uri(run_id, rel),
                )
                if pp.status == "ok" and pp.manifest is not None:
                    publish_manifest_uri = _write_publish_manifest(run_id, pp.manifest)
                    LOG.info("geoclaw postprocess ok: publish_manifest_uri=%s", publish_manifest_uri)
                else:
                    error_code = pp.error_code
                    LOG.warning("geoclaw postprocess honesty gate: %s %s", pp.error_code, pp.error_message)
            except Exception as pp_exc:
                LOG.warning("geoclaw postprocess failed (non-fatal): %s", pp_exc)

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
        scenario=scenario,
        started_at=started_at,
        error=error_msg,
        publish_manifest_uri=publish_manifest_uri,
        error_code=error_code,
    )
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
