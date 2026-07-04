"""Unit tests for the job-0293b S3 port (sprint-14-aws).

Covers every helper patched in this job, driving the s3:// branches with a
fake reader bound over the shared ``tools/cache.py::read_object_bytes_s3``
seam (boto3-shaped; no network, no moto) + temp files:

1. run_pelicun_damage_assessment._download_uri_to_local — s3 staging to a
   temp file, the job-0253 last-two-segment path-mangle retry mirrored for
   s3, typed PelicunRuntimeError wrap, and the scheme-aware WMS reverse-map
   (s3 under GRACE2_STORAGE_BACKEND=s3; gs byte-identical by default).
2. run_pelicun_damage_assessment._fetch_pelicun_damage_bytes — end-to-end
   shaped: s3 hazard + asset URIs staged locally, was_remote flags proven by
   the finally-block unlink.
3. postprocess_pelicun._download_uri_to_local + the async tool's was_remote
   unlink for an s3 damage layer.
4. clip_raster_to_bbox._get_source_crs / clip_raster_to_polygon._get_source_crs
   — /vsis3/ header-read branch (mirrors /vsigs/ style).
5. clip_vector_to_polygon._resolve_layer_to_local_path — (path, is_temp)
   tuple shape for s3 + typed ClipVectorError wrap.
6. extract_landcover_class._open_source — /vsis3/ path for s3 URIs.
7. analytical_qa._download_uri_bytes/_materialize_uri — bytes + tmpdir
   materialization for s3 + typed AnalyticalQAError wrap.
8. chart_tools._download_uri_bytes/_materialize_uri — same pair with
   ChartToolError(retryable=True).

gs:// behaviour is asserted unchanged where the port touched adjacent code
(the WMS reverse-map default-scheme test drives the gs branch with a fake
storage client).
"""

from __future__ import annotations

import asyncio
import os
import tempfile

import pytest


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------


class _FakeRasterioDataset:
    """Context-manager dataset exposing only ``.crs``."""

    def __init__(self, crs: str) -> None:
        self.crs = crs

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _bind_fake_reader(monkeypatch, payload: bytes = b"S3-BYTES"):
    """Bind a fake over the shared cache.read_object_bytes_s3 seam.

    Every per-tool s3 branch imports the reader lazily from
    ``grace2_agent.tools.cache`` at call time, so one setattr covers all
    modules. Returns the list of URIs requested.
    """
    from grace2_agent.tools import cache as cache_mod

    calls: list[str] = []

    def fake_reader(uri: str) -> bytes:
        calls.append(uri)
        return payload

    monkeypatch.setattr(cache_mod, "read_object_bytes_s3", fake_reader)
    return calls


def _bind_failing_reader(monkeypatch, exc: Exception):
    from grace2_agent.tools import cache as cache_mod

    def fake_reader(uri: str) -> bytes:
        raise exc

    monkeypatch.setattr(cache_mod, "read_object_bytes_s3", fake_reader)


# ---------------------------------------------------------------------------
# 1. run_pelicun_damage_assessment._download_uri_to_local
# ---------------------------------------------------------------------------


def test_pelicun_download_s3_stages_to_temp_file(monkeypatch):
    from grace2_agent.tools.run_pelicun_damage_assessment import (
        _download_uri_to_local,
    )

    calls = _bind_fake_reader(monkeypatch, b"TIFF-PAYLOAD")
    path = _download_uri_to_local("s3://bkt/run123/flood_depth_peak.tif", ".tif")
    try:
        assert calls == ["s3://bkt/run123/flood_depth_peak.tif"]
        assert path.endswith(".tif")
        assert path != "s3://bkt/run123/flood_depth_peak.tif"
        with open(path, "rb") as f:
            assert f.read() == b"TIFF-PAYLOAD"
    finally:
        os.unlink(path)


def test_pelicun_download_s3_mangle_repair_retries_last_two_segments(monkeypatch):
    """LLM path-mangle guard (job-0253) mirrored for s3:// URIs."""
    from grace2_agent.tools import cache as cache_mod
    from grace2_agent.tools.run_pelicun_damage_assessment import (
        _download_uri_to_local,
    )

    calls: list[str] = []

    def fake_reader(uri: str) -> bytes:
        calls.append(uri)
        if uri == "s3://bkt/runs/run123/flood_depth_peak.tif":
            raise RuntimeError("NoSuchKey")
        assert uri == "s3://bkt/run123/flood_depth_peak.tif"
        return b"REPAIRED"

    monkeypatch.setattr(cache_mod, "read_object_bytes_s3", fake_reader)
    path = _download_uri_to_local(
        "s3://bkt/runs/run123/flood_depth_peak.tif", ".tif"
    )
    try:
        assert calls == [
            "s3://bkt/runs/run123/flood_depth_peak.tif",
            "s3://bkt/run123/flood_depth_peak.tif",
        ]
        with open(path, "rb") as f:
            assert f.read() == b"REPAIRED"
    finally:
        os.unlink(path)


def test_pelicun_download_s3_failure_raises_typed_error(monkeypatch):
    from grace2_agent.tools.run_pelicun_damage_assessment import (
        PelicunRuntimeError,
        _download_uri_to_local,
    )

    _bind_failing_reader(monkeypatch, RuntimeError("AccessDenied"))
    with pytest.raises(PelicunRuntimeError, match="S3 download failed"):
        # Short key (<= 2 segments) so the mangle-repair retry cannot fire.
        _download_uri_to_local("s3://bkt/x.tif", ".tif")


_WMS_URI = (
    "https://qgis.example.com/ows?SERVICE=WMS&REQUEST=GetMap"
    "&LAYERS=flood-depth-peak-RUN123&FORMAT=image/png"
)


def test_pelicun_wms_reverse_map_uses_s3_scheme_on_aws(monkeypatch):
    from grace2_agent.tools.run_pelicun_damage_assessment import (
        _download_uri_to_local,
    )

    monkeypatch.setenv("GRACE2_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("GRACE2_RUNS_BUCKET", "test-runs")
    calls = _bind_fake_reader(monkeypatch, b"COG")
    path = _download_uri_to_local(_WMS_URI, ".tif")
    try:
        assert calls == ["s3://test-runs/RUN123/flood_depth_peak.tif"]
        with open(path, "rb") as f:
            assert f.read() == b"COG"
    finally:
        os.unlink(path)


# NOTE: the former ``test_pelicun_wms_reverse_map_legacy_gcs_scheme`` was
# deleted in the GCP decommission — the reverse-map is now S3-only (see
# ``test_pelicun_wms_reverse_map_uses_s3_scheme_on_aws`` above).


# ---------------------------------------------------------------------------
# 2. Pelicun end-to-end shaped: s3 URIs through _fetch_pelicun_damage_bytes
# ---------------------------------------------------------------------------


def test_pelicun_fetch_stages_s3_inputs_and_unlinks_after(monkeypatch):
    from grace2_agent.tools import run_pelicun_damage_assessment as mod

    _bind_fake_reader(monkeypatch, b"PAYLOAD")

    seen: dict[str, object] = {}

    def fake_assess(
        *,
        hazard_raster_path,
        assets_path,
        component_types_filter,
        realization_count,
        hazard_uri_for_seed,
    ):
        # Both inputs were staged to real local temp files (not URIs).
        for p in (hazard_raster_path, assets_path):
            assert not p.startswith("s3://")
            with open(p, "rb") as f:
                assert f.read() == b"PAYLOAD"
        seen["hazard_local"] = hazard_raster_path
        seen["assets_local"] = assets_path
        assert hazard_uri_for_seed == "s3://bkt/run1/flood_depth_peak.tif"
        return "GDF-SENTINEL"

    monkeypatch.setattr(mod, "_assess_assets", fake_assess)
    monkeypatch.setattr(mod, "_gdf_to_fgb_bytes", lambda gdf: b"FGB")

    out = mod._fetch_pelicun_damage_bytes(
        hazard_raster_uri="s3://bkt/run1/flood_depth_peak.tif",
        assets_uri="s3://bkt/run1/assets.fgb",
        fragility_set="hazus_flood_v6",
        component_types=None,
        realization_count=100,
    )
    assert out == b"FGB"
    # was_remote=True for s3 → the finally block unlinked both staged files.
    assert not os.path.exists(seen["hazard_local"])  # type: ignore[arg-type]
    assert not os.path.exists(seen["assets_local"])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 3. postprocess_pelicun
# ---------------------------------------------------------------------------


def test_postprocess_pelicun_download_s3_stages_and_wraps_errors(monkeypatch):
    from grace2_agent.tools.postprocess_pelicun import (
        PelicunPostprocessIOError,
        _download_uri_to_local,
    )

    calls = _bind_fake_reader(monkeypatch, b"FGB-PAYLOAD")
    path = _download_uri_to_local("s3://bkt/damage.fgb", ".fgb")
    try:
        assert calls == ["s3://bkt/damage.fgb"]
        assert path.endswith(".fgb")
        with open(path, "rb") as f:
            assert f.read() == b"FGB-PAYLOAD"
    finally:
        os.unlink(path)

    _bind_failing_reader(monkeypatch, RuntimeError("AccessDenied"))
    with pytest.raises(PelicunPostprocessIOError, match="S3 download failed"):
        _download_uri_to_local("s3://bkt/damage.fgb", ".fgb")


def test_postprocess_pelicun_tool_unlinks_s3_staged_file(monkeypatch):
    import geopandas as gpd

    from grace2_agent.tools import postprocess_pelicun as mod

    _bind_fake_reader(monkeypatch, b"FGB-PAYLOAD")

    staged: dict[str, str] = {}

    def fake_read_file(path, *args, **kwargs):
        staged["path"] = path
        with open(path, "rb") as f:
            assert f.read() == b"FGB-PAYLOAD"
        return "GDF-SENTINEL"

    monkeypatch.setattr(gpd, "read_file", fake_read_file)

    class _FakeEnvelope:
        def model_dump(self, mode="json"):
            return {"ok": True}

    def fake_aggregate(gdf, *, damage_layer_uri, flood_layer_uri):
        assert gdf == "GDF-SENTINEL"
        assert damage_layer_uri == "s3://bkt/damage.fgb"
        return _FakeEnvelope()

    monkeypatch.setattr(mod, "_aggregate_gdf", fake_aggregate)

    out = asyncio.run(
        mod.postprocess_pelicun(damage_layer_uri="s3://bkt/damage.fgb")
    )
    assert out == {"ok": True}
    # was_remote=True for s3 → the staged temp file was unlinked.
    assert not os.path.exists(staged["path"])


# ---------------------------------------------------------------------------
# 4. CRS detection — /vsis3/ branches
# ---------------------------------------------------------------------------


# NOTE: the former ``test_clip_raster_get_source_crs_gs_branch_unchanged`` was
# deleted in the GCP decommission — ``_get_source_crs`` no longer has a gs://
# /vsigs/ branch (S3 stage-then-open only; see the s3 test below).


# ---------------------------------------------------------------------------
# 5. clip_vector_to_polygon
# ---------------------------------------------------------------------------


def test_clip_vector_resolve_s3_returns_temp_path_tuple(monkeypatch):
    from grace2_agent.tools.clip_vector_to_polygon import (
        _resolve_layer_to_local_path,
    )

    calls = _bind_fake_reader(monkeypatch, b"FGB-BYTES")
    path, is_temp = _resolve_layer_to_local_path(
        "s3://bkt/occurrences.fgb",
        None,
        ".fgb",
        not_found_code="UNKNOWN_VECTOR_URI",
    )
    try:
        assert is_temp is True
        assert calls == ["s3://bkt/occurrences.fgb"]
        assert path.endswith(".fgb")
        with open(path, "rb") as f:
            assert f.read() == b"FGB-BYTES"
    finally:
        os.unlink(path)


def test_clip_vector_resolve_s3_failure_raises_typed_error(monkeypatch):
    from grace2_agent.tools.clip_vector_to_polygon import (
        ClipVectorError,
        _resolve_layer_to_local_path,
    )

    _bind_failing_reader(monkeypatch, RuntimeError("AccessDenied"))
    with pytest.raises(ClipVectorError, match="S3 download failed") as ei:
        _resolve_layer_to_local_path(
            "s3://bkt/occurrences.fgb",
            None,
            ".fgb",
            not_found_code="UNKNOWN_VECTOR_URI",
        )
    assert ei.value.error_code == "DOWNLOAD_FAILED"


# ---------------------------------------------------------------------------
# 6. extract_landcover_class
# ---------------------------------------------------------------------------


# NOTE: the former ``test_extract_landcover_open_source_gs_branch_unchanged``
# was deleted in the GCP decommission — ``_open_source`` no longer has a gs://
# /vsigs/ branch (S3 stage-then-open only; see the s3 test below).


# ---------------------------------------------------------------------------
# 7. analytical_qa
# ---------------------------------------------------------------------------


def test_analytical_qa_download_and_materialize_s3(monkeypatch):
    from grace2_agent.tools.analytical_qa import (
        AnalyticalQAError,
        _download_uri_bytes,
        _materialize_uri,
    )

    calls = _bind_fake_reader(monkeypatch, b"LAYER-BYTES")
    assert _download_uri_bytes("s3://bkt/layer.fgb", None) == b"LAYER-BYTES"
    assert calls == ["s3://bkt/layer.fgb"]

    with tempfile.TemporaryDirectory() as tmpdir:
        local = _materialize_uri("s3://bkt/layer.fgb", tmpdir, "value", None)
        assert local.startswith(tmpdir)
        assert local.endswith("value_layer.fgb")
        with open(local, "rb") as f:
            assert f.read() == b"LAYER-BYTES"

    _bind_failing_reader(monkeypatch, RuntimeError("AccessDenied"))
    with pytest.raises(AnalyticalQAError, match="S3 download failed") as ei:
        _download_uri_bytes("s3://bkt/layer.fgb", None)
    assert ei.value.error_code == "DOWNLOAD_FAILED"


def test_analytical_qa_materialize_local_path_passthrough_unchanged():
    from grace2_agent.tools.analytical_qa import _materialize_uri

    with tempfile.TemporaryDirectory() as tmpdir:
        assert _materialize_uri("/tmp/x.fgb", tmpdir, "v", None) == "/tmp/x.fgb"


# ---------------------------------------------------------------------------
# 8. chart_tools
# ---------------------------------------------------------------------------


def test_chart_tools_download_and_materialize_s3(monkeypatch):
    from grace2_agent.tools.chart_tools import (
        ChartToolError,
        _download_uri_bytes,
        _materialize_uri,
    )

    calls = _bind_fake_reader(monkeypatch, b"CHART-BYTES")
    assert _download_uri_bytes("s3://bkt/damage.fgb", None) == b"CHART-BYTES"
    assert calls == ["s3://bkt/damage.fgb"]

    with tempfile.TemporaryDirectory() as tmpdir:
        local = _materialize_uri("s3://bkt/damage.fgb", tmpdir, "hist", None)
        assert local.startswith(tmpdir)
        assert local.endswith("hist_damage.fgb")
        with open(local, "rb") as f:
            assert f.read() == b"CHART-BYTES"

    _bind_failing_reader(monkeypatch, RuntimeError("AccessDenied"))
    with pytest.raises(ChartToolError, match="S3 download failed") as ei:
        _download_uri_bytes("s3://bkt/damage.fgb", None)
    assert ei.value.error_code == "DOWNLOAD_FAILED"
    assert ei.value.retryable is True

def _tiny_tif_bytes(crs="EPSG:32613"):
    import numpy as np
    import rasterio
    from rasterio.io import MemoryFile
    from rasterio.transform import from_origin
    with MemoryFile() as mf:
        with mf.open(driver="GTiff", height=4, width=4, count=1, dtype="uint8",
                     crs=crs, transform=from_origin(0, 4, 1, 1)) as ds:
            ds.write(np.zeros((1, 4, 4), dtype="uint8"))
        return mf.read()


def test_clip_raster_to_bbox_get_source_crs_s3_stages_via_boto3(monkeypatch):
    # job-0293c: /vsis3/ creds don't resolve on the EC2 role in this env —
    # the s3 branch must stage bytes via the shared boto3 reader.
    from grace2_agent.tools import cache as cache_mod
    from grace2_agent.tools.clip_raster_to_bbox import _get_source_crs

    calls: list[str] = []
    data = _tiny_tif_bytes()

    def fake_read(uri):
        calls.append(uri)
        return data

    monkeypatch.setattr(cache_mod, "read_object_bytes_s3", fake_read)
    crs = _get_source_crs("s3://bkt/dem/boulder.tif")
    assert str(crs) == "EPSG:32613"
    assert calls == ["s3://bkt/dem/boulder.tif"]


def test_clip_raster_to_polygon_get_source_crs_s3_stages_via_boto3(monkeypatch):
    from grace2_agent.tools import cache as cache_mod
    from grace2_agent.tools.clip_raster_to_polygon import _get_source_crs

    calls: list[str] = []
    data = _tiny_tif_bytes()
    monkeypatch.setattr(cache_mod, "read_object_bytes_s3", lambda u: (calls.append(u), data)[1])
    crs = _get_source_crs("s3://bkt/dem/x.tif")
    assert str(crs) == "EPSG:32613"
    assert calls == ["s3://bkt/dem/x.tif"]


def test_extract_landcover_open_source_s3_stages_via_boto3(monkeypatch):
    import os
    from grace2_agent.tools import cache as cache_mod
    from grace2_agent.tools import extract_landcover_class as elc

    calls: list[str] = []
    data = _tiny_tif_bytes()
    monkeypatch.setattr(cache_mod, "read_object_bytes_s3", lambda u: (calls.append(u), data)[1])
    with elc._open_source("s3://bkt/nlcd/x.tif") as src:
        assert str(src.crs) == "EPSG:32613"
    assert calls == ["s3://bkt/nlcd/x.tif"]
