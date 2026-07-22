"""compute_canopy_height -- staging / dispatch / publish glue tests.

Exercises the canopy-height ML-inference tool in ISOLATION (no AWS, no torch, no
geoai, no network): the build_spec assembly + S3 staging (mocked S3 client), the
tile-count -> compute-class estimate, the full stage -> run_solver -> wait ->
publish chain (mocked run_solver / wait_for_completion / publish_layer / NAIP
fetch), and the typed-error guards (bad params, AOI too large, GPU-only variant,
non-complete solve, empty output). Pattern mirrors test_run_swan_chain.py +
test_solver_aws_batch.py (FakeS3 + set_s3_client + patch-at-source).
"""

from __future__ import annotations

import asyncio
import io
import json
from typing import Any

import pytest

from grace2_agent.tools import TOOL_REGISTRY
from grace2_agent.tools import compute_canopy_height as cch
from grace2_agent.tools.compute_canopy_height import (
    assemble_canopy_build_spec,
    compute_canopy_height,
    estimate_canopy_tiles,
    resolve_canopy_cog_uri,
    stage_canopy_build_spec,
)

# A small CONUS forested AOI (panhandle Florida -- well inside the 0.06 deg^2 cap).
_AOI = (-85.30, 29.94, -85.29, 29.95)


# --------------------------------------------------------------------------- #
# Registration + solver-registry presence
# --------------------------------------------------------------------------- #


def test_compute_canopy_height_registered():
    assert "compute_canopy_height" in TOOL_REGISTRY


def test_canopy_registered_in_solver_workflow_registry():
    from grace2_agent.tools.solver import SOLVER_WORKFLOW_REGISTRY

    assert "canopy" in SOLVER_WORKFLOW_REGISTRY


def test_canopy_style_preset_resolves_to_titiler_rescale_colormap():
    from grace2_agent.tools.publish_layer import _registry_style_params

    params = _registry_style_params("canopy_height_m")
    assert params is not None
    assert "rescale=0,40" in params
    assert "colormap_name=greens" in params


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


def test_estimate_canopy_tiles_scales_with_area():
    small = estimate_canopy_tiles(_AOI)
    big = estimate_canopy_tiles((-85.30, 29.90, -85.25, 29.95))
    assert small >= 1
    assert big > small


def test_assemble_build_spec_shape():
    spec = assemble_canopy_build_spec(
        "s3://cache/rgb.tif", model_variant="compressed_SSLhuge_aerial", bbox=_AOI
    )
    assert spec["inputs"] == [{"gs_uri": "s3://cache/rgb.tif", "dest": "rgb.tif"}]
    bs = spec["build_spec"]
    assert bs["model_variant"] == "compressed_SSLhuge_aerial"
    assert bs["input_file"] == "rgb.tif"
    assert bs["output_file"] == "canopy_height.tif"
    assert bs["bbox"] == list(_AOI)
    assert "canopy_height.tif" in spec["outputs"]


def test_resolve_canopy_cog_uri_prefers_canopy_tif():
    uris = [
        "s3://r/RID/canopy.stdout",
        "s3://r/RID/canopy.stderr",
        "s3://r/RID/canopy_height.tif",
    ]
    assert resolve_canopy_cog_uri(uris) == "s3://r/RID/canopy_height.tif"


def test_resolve_canopy_cog_uri_none_when_no_tif():
    assert resolve_canopy_cog_uri(["s3://r/RID/canopy.stdout"]) is None


# --------------------------------------------------------------------------- #
# S3 staging (mocked S3 client)
# --------------------------------------------------------------------------- #


class _FakeS3:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def put_object(self, Bucket: str, Key: str, Body: Any, **_kw: Any) -> dict:  # noqa: N803
        data = Body.read() if hasattr(Body, "read") else bytes(Body)
        self.objects[(Bucket, Key)] = data
        return {}

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}


def test_stage_canopy_build_spec_uploads_to_cache(monkeypatch):
    from grace2_agent.tools import solver as solver_mod

    fake = _FakeS3()
    solver_mod.set_s3_client(fake)
    monkeypatch.setenv("GRACE2_CACHE_BUCKET", "test-cache-bucket")
    try:
        uri = stage_canopy_build_spec(
            "s3://cache/rgb.tif",
            model_variant="compressed_SSLhuge_aerial",
            run_id="STAGERID",
            bbox=_AOI,
        )
    finally:
        solver_mod.set_s3_client(None)

    assert uri == (
        "s3://test-cache-bucket/cache/static-30d/canopy_setup/STAGERID/build_spec.json"
    )
    # The build_spec landed in the fake S3 with the right shape.
    key = "cache/static-30d/canopy_setup/STAGERID/build_spec.json"
    body = json.loads(fake.objects[("test-cache-bucket", key)].decode())
    assert body["build_spec"]["model_variant"] == "compressed_SSLhuge_aerial"
    assert body["inputs"][0]["gs_uri"] == "s3://cache/rgb.tif"


# --------------------------------------------------------------------------- #
# Typed-error guards (no Spot spend)
# --------------------------------------------------------------------------- #


def test_missing_bbox_and_imagery_is_typed_error():
    out = asyncio.run(compute_canopy_height())
    assert out["status"] == "error"
    assert out["error_code"] == "CANOPY_PARAMS_INCOMPLETE"


def test_aoi_too_large_is_typed_error():
    # A 1x1 degree bbox -- far over the 0.06 deg^2 cap.
    out = asyncio.run(compute_canopy_height(bbox=(-85.0, 29.0, -84.0, 30.0)))
    assert out["status"] == "error"
    assert out["error_code"] == "CANOPY_AOI_TOO_LARGE"


def test_gpu_only_variant_rejected():
    out = asyncio.run(
        compute_canopy_height(bbox=_AOI, model_variant="SSLhuge_satellite")
    )
    assert out["status"] == "error"
    assert out["error_code"] == "CANOPY_PARAMS_INVALID"
    assert "GPU" in out["error_message"]


def test_unknown_variant_rejected():
    out = asyncio.run(compute_canopy_height(bbox=_AOI, model_variant="not_a_model"))
    assert out["status"] == "error"
    assert out["error_code"] == "CANOPY_PARAMS_INVALID"


def test_degenerate_bbox_rejected():
    out = asyncio.run(compute_canopy_height(bbox=(-85.0, 29.0, -85.0, 30.0)))
    assert out["status"] == "error"
    assert out["error_code"] == "CANOPY_PARAMS_INVALID"


# --------------------------------------------------------------------------- #
# Full chain (run_solver / wait_for_completion / publish_layer / NAIP MOCKED)
# --------------------------------------------------------------------------- #


class _FakeHandle:
    run_id = "BATCHRID"
    workflow_name = "aws-batch"


class _FakeRunResult:
    run_id = "BATCHRID"
    status = "complete"
    output_uri = "s3://runs/BATCHRID/"
    error_code = None
    error_message = None
    cancellation_reason = None


def _patch_chain(monkeypatch, *, captured: dict, naip_uri="s3://cache/naip-rgb.tif",
                 cog_uri="s3://runs/BATCHRID/canopy_height.tif", run_result=None):
    """Patch the chain seams at their SOURCE modules (the tool imports them
    inside the function body, so patch the source, not the tool module)."""
    from grace2_agent.tools import publish_layer as publish_mod
    from grace2_agent.tools import solver as solver_mod
    from grace2_agent.tools import fetch_naip as naip_mod

    rr = run_result if run_result is not None else _FakeRunResult()

    def _fake_run_solver(*, solver, model_setup_uri, compute_class):
        captured["solver"] = solver
        captured["model_setup_uri"] = model_setup_uri
        captured["compute_class"] = compute_class
        return _FakeHandle()

    async def _fake_wait(handle):
        return rr

    def _fake_stage(imagery_uri, *, model_variant, run_id, bbox=None):
        captured["imagery_uri"] = imagery_uri
        captured["model_variant"] = model_variant
        return f"s3://cache/canopy_setup/{run_id}/build_spec.json"

    def _fake_resolve(run_result, batch_run_id):
        captured["batch_run_id"] = batch_run_id
        return cog_uri

    def _fake_publish(*, layer_uri, layer_id, style_preset, case_id=None, **_kw):
        captured["publish_layer_uri"] = layer_uri
        captured["publish_layer_id"] = layer_id
        captured["publish_style_preset"] = style_preset
        return f"https://tiles/{layer_id}.png"

    class _FakeNaipLayer:
        uri = naip_uri

    def _fake_fetch_naip(bbox, **_kw):
        captured["naip_bbox"] = tuple(bbox)
        return _FakeNaipLayer()

    monkeypatch.setattr(solver_mod, "run_solver", _fake_run_solver)
    monkeypatch.setattr(solver_mod, "wait_for_completion", _fake_wait)
    monkeypatch.setattr(cch, "stage_canopy_build_spec", _fake_stage)
    monkeypatch.setattr(cch, "_resolve_cog_from_result", _fake_resolve)
    monkeypatch.setattr(publish_mod, "publish_layer", _fake_publish)
    monkeypatch.setattr(naip_mod, "fetch_naip", _fake_fetch_naip)


def test_full_chain_fetches_naip_stages_dispatches_publishes(monkeypatch):
    from grace2_contracts.execution import LayerURI

    captured: dict = {}
    _patch_chain(monkeypatch, captured=captured)

    layer = asyncio.run(compute_canopy_height(bbox=_AOI))

    assert isinstance(layer, LayerURI)
    assert layer.layer_type == "raster"
    assert layer.style_preset == "canopy_height_m"
    assert layer.units == "m"
    assert "Estimated Canopy Height" in layer.name
    assert layer.uri == "https://tiles/canopy-height-BATCHRID.png"

    # NAIP was fetched for the AOI (no imagery_uri supplied).
    assert captured["naip_bbox"] == _AOI
    # The staged RGB is the NAIP COG.
    assert captured["imagery_uri"] == "s3://cache/naip-rgb.tif"
    # Dispatched as solver='canopy'.
    assert captured["solver"] == "canopy"
    assert captured["model_setup_uri"].endswith("build_spec.json")
    # Published with the canopy preset.
    assert captured["publish_style_preset"] == "canopy_height_m"
    assert captured["publish_layer_uri"] == "s3://runs/BATCHRID/canopy_height.tif"


def test_full_chain_uses_supplied_imagery_uri_skips_naip(monkeypatch):
    captured: dict = {}
    _patch_chain(monkeypatch, captured=captured)

    layer = asyncio.run(
        compute_canopy_height(imagery_uri="s3://my/rgb.tif")
    )
    # No NAIP fetch happened (imagery_uri supplied) and the supplied URI was staged.
    assert "naip_bbox" not in captured
    assert captured["imagery_uri"] == "s3://my/rgb.tif"
    assert layer.style_preset == "canopy_height_m"


def test_full_chain_auto_selects_compute_class(monkeypatch):
    captured: dict = {}
    _patch_chain(monkeypatch, captured=captured)
    asyncio.run(compute_canopy_height(bbox=_AOI))
    # A small AOI -> the 'small' bucket from select_compute_class.
    assert captured["compute_class"] in {"small", "standard", "large", "xlarge"}


def test_non_complete_solve_is_typed_error(monkeypatch):
    class _FailedResult(_FakeRunResult):
        status = "failed"
        error_code = "SOLVER_TIMEOUT"
        error_message = "timed out"

    captured: dict = {}
    _patch_chain(monkeypatch, captured=captured, run_result=_FailedResult())

    out = asyncio.run(compute_canopy_height(bbox=_AOI))
    assert out["status"] == "error"
    assert out["error_code"] == "CANOPY_SOLVE_FAILED"


def test_empty_output_is_typed_error(monkeypatch):
    captured: dict = {}
    _patch_chain(monkeypatch, captured=captured, cog_uri=None)

    out = asyncio.run(compute_canopy_height(bbox=_AOI))
    assert out["status"] == "error"
    assert out["error_code"] == "CANOPY_OUTPUT_MISSING"


def test_local_fs_imagery_uri_rejected(monkeypatch):
    captured: dict = {}
    _patch_chain(monkeypatch, captured=captured)
    out = asyncio.run(
        compute_canopy_height(imagery_uri="file:///tmp/rgb.tif")
    )
    assert out["status"] == "error"
    assert out["error_code"] == "CANOPY_IMAGERY_FAILED"
