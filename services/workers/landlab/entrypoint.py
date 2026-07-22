"""Landlab AWS Batch worker entrypoint — thin shim around the component chain.

Sprint-17 — NEW engine. The Landlab analogue of
``services/workers/swmm/entrypoint.py`` / ``services/workers/modflow/
entrypoint.py``: the SAME OBJECT-STORE-IN -> RUN -> OBJECT-STORE-OUT envelope,
SCHEME-AWARE (``s3://`` via boto3 when ``TRID3NT_OBJECT_STORE=s3``, ``gs://`` via
google-cloud-storage otherwise — byte-identical staging/upload envelope to the
SWMM/MODFLOW shims; the worker contract is solver-agnostic).

The Landlab-specific difference from SWMM (a pre-staged ``.inp`` deck) is that
Landlab BUILDS its grid inside the worker from a DEM COG + a ``build_spec`` (the
same shape MODFLOW uses — the deck is authored in the worker, not staged): the
manifest carries the run PARAMETERS (analysis + soil/rainfall) and points at a
staged DEM COG; the worker reads the DEM, builds a ``RasterModelGrid`` over the
AOI, runs the documented component chain (``component_chain.run_component_chain``
— LandslideProbability or OverlandFlow), and writes the output field as a COG
back to the runs bucket.

Contract (FR-CE-1/2/3 — IDENTICAL completion schema to SWMM/MODFLOW, only the
solver + stdout/stderr field names carry the ``landlab_`` prefix):

    Input  (env or CLI):
        --run-id RUN_ID
            Run identifier. Outputs land under
            <scheme>://${TRID3NT_RUNS_BUCKET}/${RUN_ID}/.
        --manifest-uri s3://bucket/path/build_spec.json
            JSON setup manifest. Schema:
                {
                  "inputs": [
                    {"gs_uri": "s3://.../dem.tif", "dest": "dem.tif"}
                  ],
                  "build_spec": {                  # the Landlab run parameters
                    "analysis": "landslide_probability",
                    "target_resolution_m": 30.0,
                    "soil_cohesion_pa": 10000.0,
                    ...
                  },
                  "dem_dest": "dem.tif",           # which input is the DEM
                  "outputs": [                      # glob patterns to upload
                    "*.tif"
                  ]
                }
            ``inputs`` are downloaded into the scratch dir (the DEM COG); the
            worker builds the grid from ``dem_dest`` and runs ``build_spec``;
            ``outputs`` glob-uploads the produced field COG(s).

    Output:
        <scheme>://${TRID3NT_RUNS_BUCKET}/${RUN_ID}/landlab_field.tif
        <scheme>://${TRID3NT_RUNS_BUCKET}/${RUN_ID}/completion.json
            Terminal manifest (mirrors the SWMM/MODFLOW completion schema; the
            stdout/stderr keys carry the ``landlab_`` prefix + a typed
            ``result`` block with the narration scalars the agent postprocess
            re-reads):
                {
                  "run_id": "<run_id>",
                  "status": "ok" | "error",
                  "exit_code": <int>,
                  "analysis": "landslide_probability",
                  "landlab_stdout_uri": "<scheme>://.../landlab.stdout",
                  "landlab_stderr_uri": "<scheme>://.../landlab.stderr",
                  "result": {                       # typed narration scalars
                    "unstable_area_fraction": 0.12,
                    "min_factor_of_safety": 0.93,
                    "mean_probability_of_failure": 0.21,
                    "output_field_name": "landslide__probability_of_failure"
                  },
                  "output_uris": ["<scheme>://.../landlab_field.tif", ...],
                  "started_at": "<ISO8601 Z>",
                  "finished_at": "<ISO8601 Z>",
                  "error": "<message>" | null
                }
            The agent's ``wait_for_completion`` polls this object; its presence
            with status="ok"/"error" is the terminal signal. Truthful: this
            image asserts only that the component chain ran and wrote a field
            COG — the susceptibility interpretation is the composer's narration.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import glob
import json
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Any

LOG = logging.getLogger("trid3nt.worker.landlab")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)

SCRATCH = Path(os.environ.get("TRID3NT_LANDLAB_SCRATCH", "/opt/grace2/work"))
GCP_PROJECT = os.environ.get("GCP_PROJECT", "legacy-cloud-project")
RUNS_BUCKET = os.environ.get("TRID3NT_RUNS_BUCKET", "trid3nt-runs")

#: The default produced field COG filename (the postprocess/composer reads this
#: + any glob matches from completion.json output_uris).
FIELD_COG_NAME = "landlab_field.tif"

#: levers STEP 3: per-secondary-field COG filename. The agent maps the
#: ``<token>`` back onto an OutputQuantitySpec
#: (drainage_area / slope / relative_wetness / discharge / factor_of_safety).
def _secondary_cog_name(token: str) -> str:
    return f"landlab_secondary_{token}.tif"


def _utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# Object-store abstraction — dispatch BY URI SCHEME (s3:// via boto3, gs:// via
# google-cloud-storage, both lazy-imported). Byte-identical to the SWMM/MODFLOW
# workers: the worker contract is solver-agnostic, so the staging/upload
# envelope is shared verbatim. The runs-bucket OUTPUT scheme follows
# TRID3NT_OBJECT_STORE (s3 -> s3://, default gcs -> gs://).
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


# --------------------------------------------------------------------------- #
# DEM read + resample + grid build + chain run + COG write.
# --------------------------------------------------------------------------- #
def _read_dem_for_grid(
    dem_path: Path, target_resolution_m: float
) -> tuple[Any, float, Any, str]:
    """Read the DEM COG and resample to ``target_resolution_m`` (projected metres).

    The Landlab ``RasterModelGrid`` is a UNIFORM-spacing grid in metres, so the
    DEM is reprojected to a metric CRS (UTM auto-picked from the DEM centroid)
    and resampled to the target cell size. Returns ``(dem_array, resolution_m,
    transform, crs)`` — ``dem_array`` is a numpy ``(H, W)`` float array (NaN for
    no-data), ``transform`` + ``crs`` georegister the OUTPUT field COG so it
    aligns with the AOI exactly (the postprocess reprojects it to EPSG:4326).
    """
    import numpy as np
    import rasterio
    from rasterio.warp import (
        Resampling,
        calculate_default_transform,
        reproject,
    )

    with rasterio.open(dem_path) as src:
        src_crs = src.crs
        # Pick a metric CRS: if the DEM is already projected (linear units), keep
        # it; if it is geographic (degrees), reproject to the AOI-centroid UTM.
        if src_crs is not None and src_crs.is_geographic:
            cen_lon = 0.5 * (src.bounds.left + src.bounds.right)
            cen_lat = 0.5 * (src.bounds.bottom + src.bounds.top)
            utm_zone = int((cen_lon + 180.0) / 6.0) + 1
            epsg = (32600 if cen_lat >= 0 else 32700) + utm_zone
            dst_crs = rasterio.crs.CRS.from_epsg(epsg)
        else:
            dst_crs = src_crs or rasterio.crs.CRS.from_epsg(3857)

        res = float(target_resolution_m)
        transform, width, height = calculate_default_transform(
            src.crs,
            dst_crs,
            src.width,
            src.height,
            *src.bounds,
            resolution=res,
        )
        src_nodata = src.nodata
        dst = np.full((height, width), np.nan, dtype="float64")
        reproject(
            source=rasterio.band(src, 1),
            destination=dst,
            src_transform=src.transform,
            src_crs=src.crs,
            src_nodata=src_nodata,
            dst_transform=transform,
            dst_crs=dst_crs,
            dst_nodata=float("nan"),
            resampling=Resampling.bilinear,
        )
        if src_nodata is not None:
            dst = np.where(dst == src_nodata, np.nan, dst)

    LOG.info(
        "DEM read+resampled: %dx%d @ %.2f m crs=%s",
        height,
        width,
        res,
        dst_crs,
    )
    return dst, res, transform, str(dst_crs)


def _write_field_cog(
    field: Any,
    out_path: Path,
    *,
    transform: Any,
    crs: str,
) -> None:
    """Write the chain output field ``(H, W)`` to a single-band GeoTIFF/COG.

    Written in the grid's projected-metres CRS with the DEM-derived transform
    (NaN no-data preserved). The agent-side postprocess reprojects it to
    EPSG:4326 (same pattern as ``postprocess_swmm._write_depth_cog_4326``), so
    this stays a plain metric-CRS GTiff — the agent owns the 4326 warp + the
    CRS round-trip guard.
    """
    import numpy as np
    import rasterio

    arr = np.asarray(field, dtype="float32")
    nrows, ncols = arr.shape
    profile = {
        "driver": "GTiff",
        "dtype": "float32",
        "count": 1,
        "height": nrows,
        "width": ncols,
        "crs": crs,
        "transform": transform,
        "nodata": float("nan"),
        "compress": "LZW",
    }
    with rasterio.open(out_path, "w", **profile) as dst:
        dst.write(arr, 1)
    LOG.info("wrote field COG -> %s (%dx%d)", out_path, nrows, ncols)


def _run_landlab(
    build_spec: dict[str, Any], dem_path: Path, cwd: Path
) -> tuple[dict[str, Any], Path]:
    """Build the grid from the DEM + run the component chain + write the COG.

    Returns ``(result_block, field_cog_path)`` — the typed narration block for
    completion.json + the produced field COG. Raises on a chain / IO failure
    (caught by ``main`` and recorded as exit!=0 in completion.json).
    """
    from services.workers.landlab.component_chain import run_component_chain

    target_res = float(build_spec.get("target_resolution_m", 30.0))
    dem, resolution_m, transform, crs = _read_dem_for_grid(dem_path, target_res)

    chain = run_component_chain(
        dem, resolution_m=resolution_m, build_spec=build_spec
    )

    field_cog = cwd / FIELD_COG_NAME
    _write_field_cog(chain.field, field_cog, transform=transform, crs=crs)

    # levers STEP 3: write each additional field the chain computed as its own
    # COG (the agent reprojects + publishes them as context layers). A field
    # that is all-NaN / absent is skipped (no empty layer). The
    # ``secondary_field_files`` map (token -> relative filename) goes in the
    # result block so the agent maps each onto its OutputQuantitySpec.
    import numpy as _np

    secondary_files: dict[str, str] = {}
    for token, grid_field in (chain.secondary_fields or {}).items():
        arr = _np.asarray(grid_field, dtype="float64")
        if arr.size == 0 or not _np.any(_np.isfinite(arr)):
            continue
        sec_name = _secondary_cog_name(token)
        _write_field_cog(arr, cwd / sec_name, transform=transform, crs=crs)
        secondary_files[token] = sec_name

    result_block = {
        "analysis": chain.analysis,
        "unstable_area_fraction": float(chain.unstable_area_fraction),
        "min_factor_of_safety": float(chain.min_factor_of_safety),
        "mean_probability_of_failure": float(chain.mean_probability_of_failure),
        "output_field_name": chain.output_field_name,
        "resolution_m": float(resolution_m),
        "grid_crs": crs,
        "secondary_field_files": secondary_files,
    }
    return result_block, field_cog


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
        prog="trid3nt-landlab-entrypoint",
        description="Landlab AWS Batch worker entrypoint (FR-CE-1/2/3).",
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
    """Write the worker postprocess ``publish_manifest.json`` before completion."""
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
    LOG.info("landlab postprocess: wrote %s", uri)
    return uri


def _write_completion(
    run_id: str,
    status: str,
    exit_code: int,
    analysis: str | None,
    result: dict[str, Any] | None,
    output_uris: list[str],
    stdout_uri: str | None,
    stderr_uri: str | None,
    started_at: str,
    error: str | None,
    publish_manifest_uri: str | None = None,
    error_code: str | None = None,
) -> str:
    payload = {
        "run_id": run_id,
        "status": status,
        "exit_code": exit_code,
        "analysis": analysis,
        "result": result,
        "landlab_stdout_uri": stdout_uri,
        "landlab_stderr_uri": stderr_uri,
        "output_uris": output_uris,
        "publish_manifest_uri": publish_manifest_uri,
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
        "trid3nt-landlab-solver starting — project=%s run_id=%s manifest=%s "
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
    result_block: dict[str, Any] | None = None
    analysis: str | None = None
    publish_manifest_uri: str | None = None
    error_code: str | None = None
    exit_code = 1
    status = "error"

    # Capture chain stdout/stderr to files so a smoke run produces evidence
    # (mirrors the SWMM worker's stdout/stderr upload).
    import io
    from contextlib import redirect_stderr, redirect_stdout

    out_buf = io.StringIO()
    err_buf = io.StringIO()

    try:
        manifest = _read_manifest(manifest_uri)
        inputs = manifest.get("inputs", []) or []
        build_spec = manifest.get("build_spec", {}) or {}
        dem_dest = manifest.get("dem_dest") or "dem.tif"
        outputs = manifest.get("outputs", []) or ["*.tif"]
        analysis = str(build_spec.get("analysis", "landslide_probability"))

        scratch = _prepare_scratch()

        for item in inputs:
            # Manifest input entries keep the LEGACY field name ``gs_uri``; the
            # VALUE is resolved by scheme (s3:// on Batch, gs:// on a GCS box).
            input_uri = item["gs_uri"]
            dest = scratch / item["dest"]
            _download(input_uri, dest)

        dem_path = scratch / dem_dest
        if not dem_path.exists():
            raise FileNotFoundError(
                f"DEM input not found at {dem_path} (dem_dest={dem_dest!r})"
            )

        try:
            with redirect_stdout(out_buf), redirect_stderr(err_buf):
                result_block, field_cog = _run_landlab(build_spec, dem_path, scratch)
            exit_code = 0
            status = "ok"
        except Exception as exc:  # noqa: BLE001 — record chain failure as exit!=0
            err_buf.write(f"landlab chain raised {type(exc).__name__}: {exc}\n")
            LOG.exception("landlab component chain failed")
            error_msg = f"{type(exc).__name__}: {exc}"
            exit_code = 1
            status = "error"

        # Always write + upload stdout/stderr (the smoke-run evidence).
        stdout_path = scratch / "landlab.stdout"
        stderr_path = scratch / "landlab.stderr"
        stdout_path.write_text(out_buf.getvalue(), encoding="utf-8")
        stderr_path.write_text(err_buf.getvalue(), encoding="utf-8")
        stdout_uri = _upload(stdout_path, _runs_uri(run_id, "landlab.stdout"))
        stderr_uri = _upload(stderr_path, _runs_uri(run_id, "landlab.stderr"))

        # Postprocess: write publish_manifest.json (worker-side COG + manifest).
        if exit_code == 0:
            try:
                from services.workers._landlab_postprocess import run_landlab_postprocess
                pp = run_landlab_postprocess(
                    run_id=run_id,
                    scratch=scratch,
                    analysis=analysis or "landslide_probability",
                    result_block=result_block,
                    runs_uri_for=lambda rel: _runs_uri(run_id, rel),
                )
                if pp.status == "ok" and pp.manifest is not None:
                    publish_manifest_uri = _write_publish_manifest(run_id, pp.manifest)
                    LOG.info(
                        "landlab postprocess ok: publish_manifest_uri=%s",
                        publish_manifest_uri,
                    )
                else:
                    error_code = pp.error_code
                    LOG.warning(
                        "landlab postprocess honesty gate: %s %s",
                        pp.error_code, pp.error_message,
                    )
            except Exception as pp_exc:  # noqa: BLE001
                LOG.warning("landlab postprocess failed (non-fatal): %s", pp_exc)

        for path in _expand_outputs(list(outputs), scratch):
            rel = path.relative_to(scratch).as_posix()
            uri = _upload(path, _runs_uri(run_id, rel))
            output_uris.append(uri)
        if publish_manifest_uri and publish_manifest_uri not in output_uris:
            output_uris.append(publish_manifest_uri)

        if status == "ok" and not output_uris:
            status = "error"
            exit_code = 1
            error_msg = "landlab chain produced no output COG to upload"

    except Exception as exc:  # pragma: no cover — defensive, logged + emitted
        LOG.exception("solver entrypoint failed")
        error_msg = f"{type(exc).__name__}: {exc}"
        exit_code = 1
        status = "error"

    _write_completion(
        run_id=run_id,
        status=status,
        exit_code=exit_code,
        analysis=analysis,
        result=result_block,
        output_uris=output_uris,
        stdout_uri=stdout_uri,
        stderr_uri=stderr_uri,
        started_at=started_at,
        error=error_msg,
        publish_manifest_uri=publish_manifest_uri,
        error_code=error_code,
    )
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
