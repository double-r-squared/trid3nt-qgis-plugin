"""Canopy-height ML-inference AWS Batch worker entrypoint.

The canopy-height tool's CLOUD LANE. Near-verbatim copy of the OpenQuake / SWAN
worker shape (the worker contract is solver-agnostic): accept ``--run-id`` /
``--manifest-uri`` (env fallback), read the build_spec by URI SCHEME, download the
staged sub-metre RGB COG, run Meta's pretrained HighResCanopyHeight model (a
DINOv2 ViT backbone + DPT decoder, Apache-2.0) via OpenGeoAI's ``geoai`` wrapper
to produce a single-band float32 canopy-height-in-metres COG, upload it, and
ALWAYS write ``completion.json`` in the SAME schema so the agent's
``wait_for_completion`` reuses its S3 completion poll verbatim.

This is an "AI-using-AI" inference worker: the outer agent picks the AOI + model
variant; this worker instantiates the inner model and calls ``predict()``. It
runs on the SAME CPU SPOT Batch substrate the physics engines use (the spike
verdict: the quantized model is CPU-runnable; NO GPU compute environment for v1).

Contract (FR-CE-1/2/3 -- IDENTICAL to the OpenQuake/SWAN/SWMM workers; only the
inference + field names differ):

    Input (env or CLI):
        --run-id RUN_ID
            Run identifier. Outputs land under
            <scheme>://${GRACE2_RUNS_BUCKET}/${RUN_ID}/.
        --manifest-uri s3://bucket/path/build_spec.json
            JSON build_spec. Schema (the agent stages this):
                {
                  "inputs": [{"gs_uri": "s3://.../rgb.tif", "dest": "rgb.tif"}],
                  "build_spec": {
                    "model_variant": "compressed_SSLhuge_aerial",
                    "input_file": "rgb.tif",
                    "output_file": "canopy_height.tif",
                    "bbox": [min_lon, min_lat, max_lon, max_lat]  # optional
                  },
                  "outputs": ["canopy_height.tif", "canopy.stdout", "canopy.stderr"]
                }
            The build_spec is read by SCHEME (s3:// on Batch, gs:// on a GCS box);
            the RGB COG in inputs[] is downloaded, the model runs, and the canopy
            COG is uploaded to the runs bucket.

    Output:
        <scheme>://${GRACE2_RUNS_BUCKET}/${RUN_ID}/canopy_height.tif
        <scheme>://${GRACE2_RUNS_BUCKET}/${RUN_ID}/completion.json
            Terminal manifest. Schema mirrors the sibling workers; the
            stdout/stderr field names carry the ``canopy_`` prefix so the
            completion readers stay symmetric:
                {
                  "run_id": "<run_id>",
                  "status": "ok" | "error",
                  "exit_code": <int>,
                  "canopy_stdout_uri": "<scheme>://.../canopy.stdout",
                  "canopy_stderr_uri": "<scheme>://.../canopy.stderr",
                  "model_variant": "<variant>",
                  "output_uris": ["<scheme>://.../canopy_height.tif", ...],
                  "canopy_height_uri": "<scheme>://.../canopy_height.tif" | null,
                  "started_at": "<ISO8601 Z>",
                  "finished_at": "<ISO8601 Z>",
                  "error": "<message>" | null
                }
            Truthful: this image asserts only that the inference ran and produced a
            non-empty canopy raster -- NOT that the estimate is a measurement (it
            is a MODEL ESTIMATE, Tolan et al. MAE ~2.5 m aerial). An all-nodata /
            all-zero output reads status="error" (the honesty floor).

Design notes:
    - The COG-write (``write_canopy_cog``) is PURE (rasterio only, no inference),
      so it unit-tests in isolation with a synthetic height array + an input RGB.
    - ``run_canopy_inference`` wraps geoai's ``CanopyHeightEstimation.predict()``;
      it is lazily imported so the COG-write test never needs torch/geoai.
    - Object I/O is dispatched BY URI SCHEME (``s3://`` via boto3, ``gs://`` via
      google-cloud-storage, lazy-imported) -- byte-identical to the OpenQuake
      worker.
    - The weights are baked into the image at build time (no runtime download of
      749 MB on the SPOT box; no dependence on Meta's bucket staying public at run
      time -- the spike's weights-permanence mitigation). ``GEOAI_CANOPY_CACHE``
      points geoai at the baked weights dir.
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

LOG = logging.getLogger("grace2.worker.canopy")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s -- %(message)s",
)

SCRATCH = Path(os.environ.get("GRACE2_CANOPY_SCRATCH", "/opt/grace2/work"))
GCP_PROJECT = os.environ.get("GCP_PROJECT", "legacy-cloud-project")
RUNS_BUCKET = os.environ.get("GRACE2_RUNS_BUCKET", "trid3nt-runs")

#: Default model variant (the CPU-friendly quantized aerial-tuned Meta model).
DEFAULT_MODEL_VARIANT = os.environ.get(
    "GRACE2_CANOPY_MODEL_VARIANT", "compressed_SSLhuge_aerial"
)

#: The dir the baked Meta weights live in (geoai reads from here -- no runtime
#: download). Mirrors the spike's "bake weights into the image" decision.
GEOAI_CANOPY_CACHE = os.environ.get("GEOAI_CANOPY_CACHE", "/opt/grace2/weights/canopy")

#: Default output globs (the canopy COG + the run stdout/stderr).
_DEFAULT_OUTPUT_GLOBS: tuple[str, ...] = (
    "canopy_height.tif",
    "canopy.stdout",
    "canopy.stderr",
)


def _utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# Object-store abstraction -- dispatch BY URI SCHEME (s3:// via boto3, gs:// via
# google-cloud-storage, both lazy-imported). Byte-identical to the OpenQuake
# worker (services/workers/openquake/entrypoint.py).
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
    """Lazily build (and cache) the boto3 S3 client (Batch task role)."""
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


def _download(uri: str, dest: Path) -> None:
    """Download one staged input to ``dest``, resolved BY SCHEME."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    scheme, bucket, key = _split_object_uri(uri)
    LOG.info("downloading %s -> %s", uri, dest)
    if scheme == "s3":
        resp = _s3_client().get_object(Bucket=bucket, Key=key)
        with dest.open("wb") as fh:
            shutil.copyfileobj(resp["Body"], fh)
        return
    _gcs_client().bucket(bucket).blob(key).download_to_filename(str(dest))


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


# --------------------------------------------------------------------------- #
# COG-write -- PURE rasterio (no inference). Unit-tested in isolation with a
# synthetic height array + an input RGB to copy georeferencing from.
# --------------------------------------------------------------------------- #


def write_canopy_cog(height_array: Any, src_rgb_path: Path, out_path: Path) -> Path:
    """Write a single-band float32 canopy-height-in-metres COG.

    Copies the CRS / transform / extent from the input RGB GeoTIFF
    (``src_rgb_path``) so the canopy raster aligns pixel-for-pixel with the
    imagery it was inferred from -- exactly the contract the spike states (same
    georeferencing, single-band float32 metres, LZW-compressed). The output is a
    valid COG (tiled + internal overviews) so TiTiler serves it directly.

    Args:
        height_array: a 2-D numpy array of canopy heights in metres (the model
            output). Coerced to float32. Negative values are clamped to 0.
        src_rgb_path: the input RGB GeoTIFF to copy CRS/transform/shape from.
        out_path: destination path for the canopy COG.

    Returns:
        ``out_path``.

    Raises:
        ValueError: the height array shape does not match the source raster.
    """
    import numpy as np
    import rasterio
    from rasterio.enums import Resampling

    arr = np.asarray(height_array, dtype="float32")
    if arr.ndim != 2:
        raise ValueError(f"height_array must be 2-D; got shape {arr.shape}")
    # Canopy height is non-negative; clamp tiny negative model noise to 0.
    arr = np.where(np.isfinite(arr), np.maximum(arr, 0.0), np.float32("nan"))

    with rasterio.open(str(src_rgb_path)) as src:
        if (src.height, src.width) != arr.shape:
            raise ValueError(
                f"height_array shape {arr.shape} != source raster "
                f"({src.height}, {src.width})"
            )
        profile = src.profile.copy()

    profile.update(
        driver="GTiff",
        dtype="float32",
        count=1,
        nodata=float("nan"),
        compress="lzw",
        tiled=True,
        blockxsize=256,
        blockysize=256,
        predictor=3,  # float predictor -- better LZW ratio on smooth height
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(str(out_path), "w", **profile) as dst:
        dst.write(arr, 1)
        # Internal overviews -> a valid COG TiTiler serves without re-tiling.
        factors = [f for f in (2, 4, 8, 16) if min(arr.shape) // f >= 1]
        if factors:
            dst.build_overviews(factors, Resampling.average)
            dst.update_tags(ns="rio_overview", resampling="average")
    LOG.info("wrote canopy COG -> %s (shape=%s)", out_path, arr.shape)
    return out_path


def canopy_cog_is_nonempty(out_path: Path) -> bool:
    """Honesty gate: True iff the canopy COG has at least one finite, positive
    height pixel. An all-nodata / all-zero raster is NOT a successful estimate."""
    import numpy as np
    import rasterio

    with rasterio.open(str(out_path)) as ds:
        band = ds.read(1, masked=True)
    finite = np.ma.masked_invalid(band)
    if finite.count() == 0:
        return False
    return bool(np.nanmax(finite.filled(np.nan)) > 0.0)


# --------------------------------------------------------------------------- #
# Inference -- geoai HighResCanopyHeight wrapper (lazy import; not in the
# COG-write test path).
# --------------------------------------------------------------------------- #


def run_canopy_inference(
    rgb_path: Path, out_path: Path, *, model_variant: str
) -> Path:
    """Run Meta HighResCanopyHeight over ``rgb_path`` -> canopy COG ``out_path``.

    Wraps OpenGeoAI's ``geoai.CanopyHeightEstimation(model=<variant>).predict(...)``
    (the "AI-using-AI" entrypoint). geoai writes a single-band metres GeoTIFF; we
    pass the baked-weights cache dir so no 749 MB runtime download happens. Lazy
    import so the COG-write unit test never needs torch/geoai. The full chain (RGB
    in -> ViT+DPT -> metres GeoTIFF out) is exercised only in the image build-time
    smoke + the live E2E, never in CI.
    """
    from geoai.canopy import CanopyHeightEstimation  # type: ignore

    LOG.info(
        "running canopy inference variant=%s rgb=%s -> %s (weights=%s)",
        model_variant,
        rgb_path,
        out_path,
        GEOAI_CANOPY_CACHE,
    )
    estimator = CanopyHeightEstimation(
        model=model_variant,
        cache_dir=GEOAI_CANOPY_CACHE,
    )
    estimator.predict(str(rgb_path), str(out_path))
    return out_path


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
        prog="grace2-canopy-entrypoint",
        description="Canopy-height ML-inference AWS Batch worker (FR-CE-1/2/3).",
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


def _write_completion(
    run_id: str,
    status: str,
    exit_code: int,
    output_uris: list[str],
    stdout_uri: str | None,
    stderr_uri: str | None,
    model_variant: str | None,
    canopy_height_uri: str | None,
    started_at: str,
    error: str | None,
) -> str:
    payload = {
        "run_id": run_id,
        "status": status,
        "exit_code": exit_code,
        "canopy_stdout_uri": stdout_uri,
        "canopy_stderr_uri": stderr_uri,
        "model_variant": model_variant,
        "output_uris": output_uris,
        "canopy_height_uri": canopy_height_uri,
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
        LOG.error("run_id is required (pass --run-id or set $GRACE2_RUN_ID)")
        return 2
    if not manifest_uri:
        LOG.error("manifest_uri is required (pass --manifest-uri or set $GRACE2_MANIFEST_URI)")
        return 2

    LOG.info(
        "trid3nt-canopy-solver starting -- project=%s run_id=%s manifest=%s "
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
    canopy_height_uri: str | None = None
    error_msg: str | None = None
    model_variant: str | None = None
    exit_code = 1
    status = "error"

    scratch = _prepare_scratch()
    stdout_path = scratch / "canopy.stdout"
    stderr_path = scratch / "canopy.stderr"

    try:
        manifest = _read_manifest(manifest_uri)
        build_spec = manifest.get("build_spec") or {}
        model_variant = str(build_spec.get("model_variant") or DEFAULT_MODEL_VARIANT)
        input_file = str(build_spec.get("input_file") or "rgb.tif")
        output_file = str(build_spec.get("output_file") or "canopy_height.tif")

        # Download every staged input (the RGB COG) by scheme.
        for entry in manifest.get("inputs") or []:
            uri = entry.get("gs_uri") or entry.get("uri")
            dest = entry.get("dest") or Path(str(uri)).name
            if not uri:
                continue
            _download(str(uri), scratch / str(dest))

        rgb_path = scratch / input_file
        if not rgb_path.exists():
            raise FileNotFoundError(
                f"staged RGB input {input_file!r} not found after download"
            )

        out_path = scratch / output_file
        stdout_path.write_text(
            f"canopy inference variant={model_variant} input={input_file}\n",
            encoding="utf-8",
        )
        # Run the inference (geoai ViT+DPT). Any failure is captured to stderr +
        # surfaced as a non-zero exit (the sibling-worker error contract).
        try:
            run_canopy_inference(rgb_path, out_path, model_variant=model_variant)
        except Exception as exc:  # noqa: BLE001 -- record as exit!=0
            stderr_path.write_text(
                f"canopy inference raised {type(exc).__name__}: {exc}\n",
                encoding="utf-8",
            )
            raise

        if not out_path.exists():
            raise RuntimeError(
                f"canopy inference produced no output file {output_file!r}"
            )
        # Honesty floor: an all-nodata / all-zero canopy raster is NOT success.
        if not canopy_cog_is_nonempty(out_path):
            raise RuntimeError(
                "canopy inference produced an empty (all-nodata / all-zero) "
                "raster -- not a valid canopy-height estimate"
            )

        # Always upload stdout/stderr so the run produces evidence.
        if not stderr_path.exists():
            stderr_path.write_text("", encoding="utf-8")
        stdout_uri = _upload(stdout_path, _runs_uri(run_id, "canopy.stdout"))
        stderr_uri = _upload(stderr_path, _runs_uri(run_id, "canopy.stderr"))

        # Upload the canopy COG (+ any extra outputs the globs catch).
        outputs = manifest.get("outputs") or list(_DEFAULT_OUTPUT_GLOBS)
        for path in _expand_outputs(list(outputs), scratch):
            rel = path.relative_to(scratch).as_posix()
            uri = _upload(path, _runs_uri(run_id, rel))
            output_uris.append(uri)
            if rel == output_file:
                canopy_height_uri = uri

        if canopy_height_uri is None:
            # Defensive: the COG exists but the glob missed it -- upload it
            # explicitly so the agent always gets a handle.
            canopy_height_uri = _upload(out_path, _runs_uri(run_id, output_file))
            if canopy_height_uri not in output_uris:
                output_uris.append(canopy_height_uri)

        exit_code = 0
        status = "ok"

    except Exception as exc:  # pragma: no cover -- defensive, logged + emitted
        LOG.exception("canopy entrypoint failed")
        error_msg = f"{type(exc).__name__}: {exc}"
        exit_code = 1
        status = "error"
        # Best-effort: still upload whatever stdout/stderr we have for evidence.
        try:
            if stdout_path.exists() and stdout_uri is None:
                stdout_uri = _upload(stdout_path, _runs_uri(run_id, "canopy.stdout"))
            if stderr_path.exists() and stderr_uri is None:
                stderr_uri = _upload(stderr_path, _runs_uri(run_id, "canopy.stderr"))
        except Exception:  # noqa: BLE001 -- evidence upload must not mask the error
            LOG.warning("best-effort stdout/stderr upload failed", exc_info=True)

    _write_completion(
        run_id=run_id,
        status=status,
        exit_code=exit_code,
        output_uris=output_uris,
        stdout_uri=stdout_uri,
        stderr_uri=stderr_uri,
        model_variant=model_variant,
        canopy_height_uri=canopy_height_uri,
        started_at=started_at,
        error=error_msg,
    )
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
