"""Atomic tool ``compute_canopy_height`` -- canopy-height ML-inference tool.

An ORDINARY agent tool (NOT a special "tier"): a compute-heavy ML-inference tool
that runs on the SAME CPU SPOT AWS Batch substrate the physics engines (SFINCS /
SWMM / OpenQuake / SWAN) use. It mirrors the OpenGeoAI "AI-using-AI" pattern --
the outer agent picks the AOI + model variant and emits a tool call; the inner
model (Meta's pretrained HighResCanopyHeight ViT+DPT) runs in the worker.

Flow (mirrors the seismic / SWAN stage -> run_solver -> wait -> publish chain;
see reports/design/spike_canopy_height_tool.md):

  1. Resolve / stage a sub-metre RGB COG for the AOI. A caller-supplied
     ``imagery_uri`` (an existing fetcher's COG handle, PREFERRED) wins; else we
     fetch NAIP (the CONUS sub-metre RGB source) via ``fetch_naip``. Either way
     the model input is an ``s3://`` COG the ephemeral Batch worker can download
     (the worker has NO access to the agent box FS -- same honesty guard
     ``_run_solver_aws_batch`` enforces on ``model_setup_uri``).
  2. Write a build_spec JSON ({imagery_uri, model_variant, output_glob}) to the
     cache bucket (same ``cache.storage_scheme()`` + ``solver._get_s3_client()``
     path the OpenQuake/SWMM/SWAN decks stage to).
  3. Dispatch through the generic ``run_solver('canopy', model_setup_uri=<build
     spec>, compute_class=select_compute_class(tiles))`` seam. "canopy" is
     registered in ``SOLVER_WORKFLOW_REGISTRY``; the per-solver Batch job-def
     resolves from ``TRID3NT_AWS_BATCH_JOB_DEF_CANOPY`` and stays INERT (honest
     typed error) until NATE flips that env after ``tofu apply`` registers the
     job-def -- exactly the SWMM/OpenQuake/SWAN posture.
  4. ``wait_for_completion`` polls the SAME ``completion.json`` schema the canopy
     worker writes; the worker uploads ``canopy_height.tif`` under the Batch
     run_id prefix.
  5. ``publish_layer`` the canopy COG with the NEW ``canopy_height_m`` greens-ramp
     preset and return a ``LayerURI`` (so the ``emit_tool_call``
     ``add_loaded_layer`` gate fires and the map paints the layer).

AOI CAP (load-bearing): a ViT-huge on CPU is minutes-to-hours, so the bbox is
capped (the granularity gate's spirit) and ``select_compute_class`` grabs a
bigger box for a denser AOI. A too-large bbox returns an honest typed error
BEFORE any Spot spend, telling the caller to narrow the AOI.

Truthfulness floor: a canopy-height raster is a MODEL ESTIMATE (Tolan et al. MAE
~2.5 m aerial), not a measurement -- the layer name + result text say "estimated"
and a non-complete Batch solve NEVER reads as success.

Determinism boundary (Invariant 1): the tool stages + dispatches + publishes; no
LLM call anywhere. FR-DC-6: ``cacheable=False`` + ``ttl_class="live-no-cache"`` +
``source_class="workflow_dispatch"`` -- the cache shim is NOT invoked (it spends
SPOT, like the other solver dispatchers).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
from typing import Any

from trid3nt_contracts import new_ulid
from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tool_arg_normalizer import coerce_bbox_value

logger = logging.getLogger("trid3nt_server.tools.processing.compute_canopy_height")

__all__ = ["compute_canopy_height", "CanopyHeightError", "CANOPY_SOLVER_NAME"]


#: The registry key + handle ``solver`` tag for the canopy worker (matches the
#: ``"canopy"`` entry in ``tools.simulation.solver.SOLVER_WORKFLOW_REGISTRY``).
CANOPY_SOLVER_NAME: str = "canopy"

#: The Meta HighResCanopyHeight variant pinned for v1 -- the CPU-friendly
#: quantized aerial-tuned model (749 MB, best for NAIP/aerial RGB). The
#: SSLhuge_satellite full model wants a GPU (v2). Exposed as an advanced
#: ``model_variant`` override but not auto-chosen (the spike's S5 cut).
DEFAULT_MODEL_VARIANT: str = "compressed_SSLhuge_aerial"

#: The model variants the worker (geoai's ``MODEL_VARIANTS``) knows. The full
#: ``SSLhuge_satellite`` is GPU-only -- rejected for v1 (no GPU CE).
_CPU_MODEL_VARIANTS: frozenset[str] = frozenset(
    {
        "compressed_SSLhuge",
        "compressed_SSLhuge_aerial",
        "compressed_SSLlarge",
    }
)
_GPU_ONLY_VARIANTS: frozenset[str] = frozenset({"SSLhuge_satellite"})

#: The worker writes the canopy-height COG under this fixed name in the run_id
#: prefix (the entrypoint's output; mirrored by ``services/workers/canopy``).
CANOPY_OUTPUT_NAME: str = "canopy_height.tif"

#: The worker's output globs (canopy COG + stdout/stderr for the honesty gate).
CANOPY_OUTPUT_GLOBS: list[str] = [
    CANOPY_OUTPUT_NAME,
    "canopy.stdout",
    "canopy.stderr",
]

#: bbox area cap (deg^2). CPU ViT-huge inference is minutes-to-hours; cap the v1
#: AOI to a neighborhood / small preserve so a single SPOT box finishes in a sane
#: window. ~0.06 deg^2 matches the fetch_naip guardrail (NAIP is the RGB source).
#: Env-overridable so the cap re-tunes from logged runtime without a code change.
def _max_bbox_deg2() -> float:
    raw = (os.environ.get("TRID3NT_CANOPY_MAX_BBOX_DEG2") or "").strip()
    try:
        v = float(raw)
        return v if v > 0 else 0.06
    except ValueError:
        return 0.06


#: NAIP native resolution (~1 m); used to estimate the 256-px tile count the
#: model will run (the ViT cost proxy fed to ``select_compute_class``).
_NAIP_RES_M: float = 1.0
_TILE_PX: int = 256
#: deg -> m at the equator (a coarse upper-bound; canopy AOIs are small so the
#: latitude foreshortening is a second-order effect for a tile-count estimate).
_DEG_TO_M: float = 111_320.0


class CanopyHeightError(RuntimeError):
    """Raised on any staging / dispatch / publish failure before a layer.

    Carries an open-set A.6 ``error_code`` so the agent emitter renders a typed
    error frame. Codes:

    - ``CANOPY_PARAMS_INVALID`` -- the bbox / variant could not be coerced.
    - ``CANOPY_AOI_TOO_LARGE`` -- the AOI exceeds the CPU-runtime cap.
    - ``CANOPY_IMAGERY_FAILED`` -- the RGB COG could not be staged/fetched.
    - ``CANOPY_STAGING_FAILED`` -- the build_spec upload failed.
    - ``CANOPY_SOLVE_FAILED`` -- the Batch solve did not complete.
    - ``CANOPY_OUTPUT_MISSING`` -- a 'complete' solve produced no canopy COG.
    - ``CANOPY_PUBLISH_FAILED`` -- the COG could not be published to the map.
    """

    error_code: str = "CANOPY_WORKFLOW_FAILED"

    def __init__(
        self,
        error_code: str,
        *,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message or error_code)
        self.error_code = error_code
        self.details: dict[str, Any] = dict(details or {})


# --------------------------------------------------------------------------- #
# Tile-count estimate -> compute-class (the ViT-on-CPU cost proxy).
# --------------------------------------------------------------------------- #
def estimate_canopy_tiles(
    bbox: tuple[float, float, float, float],
    *,
    res_m: float = _NAIP_RES_M,
) -> int:
    """Estimate the number of 256-px inference tiles the model will run.

    The ViT+DPT runs per 256x256 tile, so the tile count is the natural cost
    proxy for ``select_compute_class``. Pure arithmetic (no I/O) -- unit-testable.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    width_m = max(0.0, (max_lon - min_lon)) * _DEG_TO_M * math.cos(
        math.radians((min_lat + max_lat) / 2.0)
    )
    height_m = max(0.0, (max_lat - min_lat)) * _DEG_TO_M
    if res_m <= 0:
        res_m = _NAIP_RES_M
    nx = max(1, math.ceil((width_m / res_m) / _TILE_PX))
    ny = max(1, math.ceil((height_m / res_m) / _TILE_PX))
    return int(nx * ny)


# --------------------------------------------------------------------------- #
# build_spec assembly (PURE -- unit-tested in isolation).
# --------------------------------------------------------------------------- #
def assemble_canopy_build_spec(
    imagery_uri: str,
    *,
    model_variant: str,
    bbox: tuple[float, float, float, float] | None = None,
) -> dict[str, Any]:
    """Map the staged RGB COG + variant onto the build_spec the worker reads.

    The single source of truth for the worker-side input. The worker reads
    ``inputs[]`` (the RGB COG, downloaded as ``rgb.tif``), runs
    ``CanopyHeightEstimation(model=<variant>).predict(rgb.tif, canopy_height.tif)``,
    and uploads the COG named by ``CANOPY_OUTPUT_NAME``. Pure dict assembly.
    """
    spec: dict[str, Any] = {
        "inputs": [{"gs_uri": imagery_uri, "dest": "rgb.tif"}],
        "build_spec": {
            "model_variant": model_variant,
            "input_file": "rgb.tif",
            "output_file": CANOPY_OUTPUT_NAME,
        },
        "outputs": list(CANOPY_OUTPUT_GLOBS),
    }
    if bbox is not None:
        spec["build_spec"]["bbox"] = list(bbox)
    return spec


# --------------------------------------------------------------------------- #
# build_spec staging (S3) -- mirror of stage_openquake_build_spec / stage_swan.
# --------------------------------------------------------------------------- #
def stage_canopy_build_spec(
    imagery_uri: str,
    *,
    model_variant: str,
    run_id: str,
    bbox: tuple[float, float, float, float] | None = None,
) -> str:
    """Upload the canopy build_spec JSON to the cache bucket; return its s3:// URI.

    Mirrors ``stage_openquake_build_spec`` EXACTLY (no new client): the same
    ``cache.storage_scheme()`` scheme + the same ``solver._get_s3_client()`` boto3
    client + the same ``TRID3NT_CACHE_BUCKET`` staging bucket. The returned URI is
    fed STRAIGHT to ``run_solver('canopy', model_setup_uri=<this>)``.

    Raises ``CanopyHeightError('CANOPY_STAGING_FAILED')`` on upload failure (the
    Batch lane cannot dispatch without a reachable build_spec -- fail loudly).
    """
    from trid3nt_server.tools.cache import CACHE_BUCKET, storage_scheme
    from trid3nt_server.tools.simulation.solver import _get_s3_client

    scheme = storage_scheme()  # "s3" on AWS
    cache_bucket = os.environ.get("TRID3NT_CACHE_BUCKET") or CACHE_BUCKET
    prefix = f"cache/static-30d/canopy_setup/{run_id}/"
    spec_key = f"{prefix}build_spec.json"
    spec_uri = f"{scheme}://{cache_bucket}/{spec_key}"

    build_spec = assemble_canopy_build_spec(
        imagery_uri, model_variant=model_variant, bbox=bbox
    )
    try:
        s3 = _get_s3_client()
        s3.put_object(
            Bucket=cache_bucket,
            Key=spec_key,
            Body=json.dumps(build_spec, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
    except Exception as exc:  # noqa: BLE001
        raise CanopyHeightError(
            "CANOPY_STAGING_FAILED",
            message=f"failed to stage canopy build_spec to {spec_uri}: {exc}",
            details={"run_id": run_id, "build_spec_uri": spec_uri},
        ) from exc

    logger.info("stage_canopy_build_spec run_id=%s -> %s", run_id, spec_uri)
    return spec_uri


# --------------------------------------------------------------------------- #
# Canopy COG handle resolution from the worker's completion.json.
# --------------------------------------------------------------------------- #
def resolve_canopy_cog_uri(output_uris: list[str]) -> str | None:
    """Pick the canopy-height COG from the uploaded output URIs (pure helper).

    The worker writes exactly one ``canopy_height.tif`` alongside stdout/stderr.
    Prefer the ``canopy_height``-named TIFF, falling back to any ``.tif``, else
    None. Pure (string-only) so it unit-tests in isolation.
    """
    tifs = [u for u in output_uris if u.lower().endswith((".tif", ".tiff"))]
    for u in tifs:
        if "canopy" in u.rsplit("/", 1)[-1].lower():
            return u
    return tifs[0] if tifs else None


# --------------------------------------------------------------------------- #
# AtomicToolMetadata + registration.
# --------------------------------------------------------------------------- #
_METADATA = AtomicToolMetadata(
    name="compute_canopy_height",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)


@register_tool(
    _METADATA,
    # Annotations mirror the solver dispatchers (run_swan_waves / run_solver):
    # readOnlyHint=False (dispatches a Batch job that writes output COG artifacts),
    # openWorldHint=False (Batch worker + intra-cloud object store -- no public
    # external API from the agent), destructiveHint=False (writes go to a new
    # runs/ prefix), idempotentHint=False (each call mints a new run_id + COG keys).
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def compute_canopy_height(
    bbox: tuple[float, float, float, float] | list[float] | str | None = None,
    imagery_uri: str | None = None,
    model_variant: str = DEFAULT_MODEL_VARIANT,
    compute_class: str | None = None,
    case_id: str | None = None,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI | dict[str, Any]:
    """Estimate tree-canopy HEIGHT (metres) over an AOI from RGB imagery.

    Use this (not fetch_usfs_canopy_fuels, which is fuel data) when you want ML-inferred tree-canopy HEIGHT in metres from RGB imagery.

    Runs Meta's pretrained HighResCanopyHeight deep-learning model (a DINOv2 ViT
    backbone + DPT decoder, Apache-2.0) on sub-metre RGB aerial imagery and
    produces an ESTIMATED per-pixel canopy-top-height raster (metres), painted on
    the map with a greens height ramp. This is an "AI-using-AI" inference tool:
    the inference is heavy (a ViT on CPU is minutes-to-hours), so it runs on the
    SAME scale-to-zero CPU AWS Batch substrate the physics engines use -- it is an
    ordinary compute-heavy tool, NOT a special tier.

    Use this when:
        - The user wants tree / forest CANOPY HEIGHT over an area ("how tall are
          the trees here", "estimate canopy height for <small forested AOI>",
          "show a canopy-height map"); OR
        - A downstream needs a height raster to feed ``compute_zonal_statistics``
          (mean/max canopy height per polygon / FTW ag field).

    Do NOT use this for:
        - Vegetation greenness / health (use ``compute_ndvi``).
        - Land-cover CLASSES (use ``fetch_landcover``).
        - Building heights / a DSM-DTM difference (this is a TREE-canopy model).
        - Very large AOIs -- a CPU ViT is slow, so the bbox is capped; narrow it
          to a neighborhood / small preserve.

    Params:
        bbox: the AOI as ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326
            (lon-first). Required UNLESS ``imagery_uri`` is supplied. CONUS-only
            when relying on the NAIP RGB source.
        imagery_uri: OPTIONAL ``s3://`` URI of an existing sub-metre RGB COG (an
            imagery fetcher's output handle). PREFERRED when available -- skips
            the NAIP fetch. When absent, NAIP is fetched for ``bbox``.
        model_variant: ADVANCED. One of the CPU-runnable Meta variants
            (``compressed_SSLhuge_aerial`` [default, aerial/NAIP-tuned],
            ``compressed_SSLhuge``, ``compressed_SSLlarge``). The full
            ``SSLhuge_satellite`` is GPU-only and rejected for v1.
        compute_class: OPTIONAL FR-CE-3 compute class override. When unset it is
            auto-selected from the estimated tile count (more tiles -> a bigger
            CPU box).

    Returns:
        On success: a ``LayerURI`` (``layer_type="raster"``) -- the emitter
        appends it to ``session-state.loaded_layers`` and the map renders the
        canopy-height COG with the ``canopy_height_m`` greens ramp. The layer
        name reads "Estimated Canopy Height (m)" (truthfulness floor: it is a
        model ESTIMATE, MAE ~2.5 m, not a measurement).

        On failure: a dict ``{"status": "error", "error_code", "error_message"}``
        so the LLM narrates the failure honestly (no layer). A non-complete Batch
        solve or an empty output returns a typed error -- it NEVER reports a
        silently-empty layer as success (honesty floor).

    FR-DC-6: ``cacheable=False`` + ``ttl_class="live-no-cache"`` +
    ``source_class="workflow_dispatch"`` -- the cache shim is NOT invoked.
    """
    # --- 1. Validate variant + resolve the AOI / imagery handle -------------
    variant = (model_variant or DEFAULT_MODEL_VARIANT).strip()
    if variant in _GPU_ONLY_VARIANTS:
        return {
            "status": "error",
            "error_code": "CANOPY_PARAMS_INVALID",
            "error_message": (
                f"model_variant {variant!r} is GPU-only and not supported in v1 "
                f"(no GPU compute environment); use a CPU variant: "
                f"{sorted(_CPU_MODEL_VARIANTS)}."
            ),
        }
    if variant not in _CPU_MODEL_VARIANTS:
        return {
            "status": "error",
            "error_code": "CANOPY_PARAMS_INVALID",
            "error_message": (
                f"unknown model_variant {variant!r}; allowed CPU variants: "
                f"{sorted(_CPU_MODEL_VARIANTS)}."
            ),
        }

    coerced_bbox: tuple[float, float, float, float] | None = None
    if bbox is not None:
        coerced = coerce_bbox_value(bbox)
        if coerced is None:
            return {
                "status": "error",
                "error_code": "CANOPY_PARAMS_INVALID",
                "error_message": (
                    f"invalid bbox (expected 4 numbers "
                    f"min_lon,min_lat,max_lon,max_lat): {bbox!r}"
                ),
            }
        coerced_bbox = tuple(coerced)  # type: ignore[assignment]

    if imagery_uri is None and coerced_bbox is None:
        return {
            "status": "error",
            "error_code": "CANOPY_PARAMS_INCOMPLETE",
            "error_message": (
                "compute_canopy_height requires a bbox "
                "(min_lon, min_lat, max_lon, max_lat) OR an imagery_uri."
            ),
        }

    # --- 2. AOI cap (CPU-runtime guard, BEFORE any Spot spend) --------------
    if coerced_bbox is not None:
        min_lon, min_lat, max_lon, max_lat = coerced_bbox
        if not all(math.isfinite(v) for v in coerced_bbox):
            return {
                "status": "error",
                "error_code": "CANOPY_PARAMS_INVALID",
                "error_message": f"bbox contains non-finite values: {coerced_bbox!r}",
            }
        if min_lon >= max_lon or min_lat >= max_lat:
            return {
                "status": "error",
                "error_code": "CANOPY_PARAMS_INVALID",
                "error_message": (
                    f"bbox is degenerate (min must be < max on both axes): "
                    f"{coerced_bbox!r}"
                ),
            }
        area = (max_lon - min_lon) * (max_lat - min_lat)
        cap = _max_bbox_deg2()
        if area > cap:
            return {
                "status": "error",
                "error_code": "CANOPY_AOI_TOO_LARGE",
                "error_message": (
                    f"bbox area {area:.4f} deg^2 exceeds the {cap} deg^2 canopy "
                    "cap (a ViT canopy model on CPU is minutes-to-hours; narrow "
                    "the AOI to a neighborhood / small preserve, or split it)."
                ),
            }

    try:
        return await _run_canopy_chain(
            bbox=coerced_bbox,
            imagery_uri=imagery_uri,
            model_variant=variant,
            compute_class=compute_class,
            case_id=case_id,
        )
    except asyncio.CancelledError:
        raise
    except CanopyHeightError as exc:
        logger.warning("compute_canopy_height failed: %s (%s)", exc.error_code, exc)
        return {
            "status": "error",
            "error_code": exc.error_code,
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001 -- defensive catch-all
        logger.exception("compute_canopy_height unexpected failure")
        return {
            "status": "error",
            "error_code": "CANOPY_INTERNAL_ERROR",
            "error_message": str(exc),
        }


async def _run_canopy_chain(
    *,
    bbox: tuple[float, float, float, float] | None,
    imagery_uri: str | None,
    model_variant: str,
    compute_class: str | None,
    case_id: str | None,
) -> LayerURI:
    """The stage -> dispatch -> wait -> publish chain (cancellable; raises
    ``CanopyHeightError`` on any failure before a layer)."""
    from trid3nt_server.tools.publish_layer import publish_layer
    from trid3nt_server.tools.simulation.solver import run_solver, select_compute_class, wait_for_completion

    run_id = new_ulid()

    # --- Resolve the RGB COG handle (caller-supplied or NAIP fetch) ---------
    staged_imagery_uri = imagery_uri
    if staged_imagery_uri is None:
        assert bbox is not None  # guarded by the caller
        staged_imagery_uri = await _fetch_naip_rgb_uri(bbox)
    if not (
        staged_imagery_uri.startswith("s3://") or staged_imagery_uri.startswith("gs://")
    ):
        # The ephemeral Batch worker has no agent-box FS access -- a non-object-
        # store imagery handle cannot be read. Reject loudly BEFORE the Spot
        # submit (the same honesty guard _run_solver_aws_batch enforces).
        raise CanopyHeightError(
            "CANOPY_IMAGERY_FAILED",
            message=(
                f"imagery_uri must be an s3:// / gs:// COG the Batch worker can "
                f"download (the worker has no access to the agent box FS); got "
                f"{staged_imagery_uri!r}"
            ),
            details={"run_id": run_id},
        )

    # --- Stage the build_spec to S3 (sync boto3 off the loop) ---------------
    build_spec_uri = await asyncio.to_thread(
        stage_canopy_build_spec,
        staged_imagery_uri,
        model_variant=model_variant,
        run_id=run_id,
        bbox=bbox,
    )

    # --- Pick the compute class from the tile-count estimate ----------------
    chosen_class = compute_class
    if chosen_class is None:
        tiles = estimate_canopy_tiles(bbox) if bbox is not None else 0
        chosen_class = select_compute_class(tiles)

    # --- Dispatch through the generic run_solver / wait_for_completion seam --
    handle = run_solver(
        solver=CANOPY_SOLVER_NAME,
        model_setup_uri=build_spec_uri,
        compute_class=chosen_class,
    )
    run_result = await wait_for_completion(handle)

    # Honesty floor: a non-complete Batch result is a hard failure (no layer).
    if run_result.status != "complete":
        raise CanopyHeightError(
            "CANOPY_SOLVE_FAILED",
            message=(
                "canopy-height Batch solve did not complete "
                f"(status={run_result.status}, error_code={run_result.error_code}): "
                f"{run_result.error_message or run_result.cancellation_reason or ''}"
            ),
            details={"run_id": run_id, "output_uri": run_result.output_uri},
        )

    # --- Resolve the canopy COG handle from the worker's completion ---------
    batch_run_id = getattr(run_result, "run_id", None) or run_id
    cog_uri = await asyncio.to_thread(_resolve_cog_from_result, run_result, batch_run_id)
    if not cog_uri:
        raise CanopyHeightError(
            "CANOPY_OUTPUT_MISSING",
            message=(
                "canopy-height Batch solve completed but produced no "
                "canopy_height.tif (honesty floor: an empty output is not a "
                "successful layer)."
            ),
            details={"run_id": batch_run_id, "output_uri": run_result.output_uri},
        )

    # --- Publish the canopy COG with the greens height ramp -----------------
    layer_id = f"canopy-height-{batch_run_id}"
    try:
        wms_url = await asyncio.to_thread(
            publish_layer,
            layer_uri=cog_uri,
            layer_id=layer_id,
            style_preset="canopy_height_m",
            case_id=case_id,
        )
    except Exception as exc:  # noqa: BLE001 -- publish-failure path
        raise CanopyHeightError(
            "CANOPY_PUBLISH_FAILED",
            message=f"failed to publish the canopy-height COG: {exc}",
            details={"run_id": batch_run_id, "cog_uri": cog_uri},
        ) from exc

    logger.info(
        "compute_canopy_height complete run_id=%s layer_id=%s variant=%s uri=%s",
        batch_run_id,
        layer_id,
        model_variant,
        cog_uri,
    )
    return LayerURI(
        layer_id=layer_id,
        name="Estimated Canopy Height (m)",
        layer_type="raster",
        uri=wms_url,
        style_preset="canopy_height_m",
        role="primary",
        units="m",
        bbox=bbox,
    )


def _resolve_cog_from_result(run_result: Any, batch_run_id: str) -> str | None:
    """Resolve the canopy COG s3:// URI from the RunResult / completion.json.

    Prefers the completion's ``output_uris`` (read off S3 by run_id); falls back
    to composing the canonical ``<runs>/<run_id>/canopy_height.tif`` path. Sync
    (boto3) -- the caller runs it off the loop via ``asyncio.to_thread``.
    """
    from trid3nt_server.tools.cache import storage_scheme
    from trid3nt_server.tools.simulation.solver import _get_runs_bucket, _try_get_completion_s3  # type: ignore[attr-defined]

    runs_bucket = _get_runs_bucket()
    manifest = _try_get_completion_s3(runs_bucket, batch_run_id)
    if isinstance(manifest, dict):
        uris = [str(u) for u in (manifest.get("output_uris") or [])]
        hit = resolve_canopy_cog_uri(uris)
        if hit:
            return hit
    # Fallback: the canonical output path under the runs prefix.
    scheme = storage_scheme()
    return f"{scheme}://{runs_bucket}/{batch_run_id}/{CANOPY_OUTPUT_NAME}"


async def _fetch_naip_rgb_uri(bbox: tuple[float, float, float, float]) -> str:
    """Fetch a NAIP RGB COG for the AOI; return its s3:// URI.

    NAIP is the CONUS sub-metre RGB source (the data-source fallback norm:
    NAIP -> [future Maxar] -> honest typed error). Reuses the existing
    ``fetch_naip`` tool (its output is an s3:// cache COG handle). ``fetch_naip``
    is synchronous (a cached read-through) so it runs off the loop.
    """
    from trid3nt_server.tools.fetchers.imagery.fetch_naip import fetch_naip

    try:
        layer = await asyncio.to_thread(fetch_naip, bbox)
    except Exception as exc:  # noqa: BLE001
        raise CanopyHeightError(
            "CANOPY_IMAGERY_FAILED",
            message=(
                f"failed to fetch NAIP RGB imagery for the AOI (the canopy model "
                f"needs a sub-metre RGB COG; NAIP is CONUS-only): {exc}"
            ),
        ) from exc
    uri = getattr(layer, "uri", None)
    if not uri:
        raise CanopyHeightError(
            "CANOPY_IMAGERY_FAILED",
            message=(
                "NAIP fetch returned no COG URI for the AOI (no NAIP coverage? "
                "the canopy model needs sub-metre RGB -- narrow to a CONUS AOI)."
            ),
        )
    return str(uri)
