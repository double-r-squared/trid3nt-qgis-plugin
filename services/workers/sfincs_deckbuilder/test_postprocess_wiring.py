# Worker-side wiring test: the SFINCS quadtree (sfincs_deckbuilder) worker's
# run_raster_postprocess + completion.json publish_manifest_uri emission.
#
# Proves the entrypoint wiring (NOT just the shared substrate): a synthetic LOCAL
# sfincs_map.nc in the deck dir -> overview-bearing COGs written into the deck dir
# (so the *.tif sweep ships them) + a publish manifest dict + the empty-field
# honesty gate. completion.json carries the explicit publish_manifest_uri pointer.
#
# rasterio/xarray/scipy ship in the agent test venv; the test skips cleanly
# where a dep is absent.

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

rasterio = pytest.importorskip("rasterio")
xr = pytest.importorskip("xarray")
pytest.importorskip("scipy")
import numpy as np  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from services.workers.sfincs_deckbuilder import entrypoint as ep  # noqa: E402


def _write_quadtree_map(path: Path, *, n_faces=64, n_time=3, flooded=True) -> None:
    rng = np.random.default_rng(0)
    fx = (665000.0 + rng.uniform(0, 25000, n_faces)).astype("float64")
    fy = (3310000.0 + rng.uniform(0, 20000, n_faces)).astype("float64")
    hmax = (1.0 + rng.uniform(0, 2, n_faces)).astype("float32")
    if not flooded:
        hmax[:] = 0.0
    zb = np.full((n_faces,), -1.0, dtype="float32")
    zs = np.stack(
        [np.full((n_faces,), 0.5 + 1.0 * t / (n_time - 1), dtype="float32")
         for t in range(n_time)]
    )
    ds = xr.Dataset(
        {
            "hmax": (("nmesh2d_face",), hmax),
            "zb": (("nmesh2d_face",), zb),
            "zs": (("time", "nmesh2d_face"), zs),
            "mesh2d_face_x": (("nmesh2d_face",), fx),
            "mesh2d_face_y": (("nmesh2d_face",), fy),
            "crs": ((), np.int32(32616)),
        },
        coords={"time": ("time", np.arange(n_time, dtype="float64"))},
    )
    ds.to_netcdf(path, engine="scipy")


_SPEC = {"aoi": {"bbox": [-85.3, 29.9, -85.0, 30.1]}}


def test_spec_bbox_4326_reads_aoi():
    assert ep._spec_bbox_4326(_SPEC) == (-85.3, 29.9, -85.0, 30.1)
    assert ep._spec_bbox_4326({}) is None
    assert ep._spec_bbox_4326({"aoi": {}}) is None


def test_run_raster_postprocess_builds_manifest_and_cogs(tmp_path: Path):
    _write_quadtree_map(tmp_path / "sfincs_map.nc")
    manifest, status_override, error_code, rels = ep.run_raster_postprocess(
        "RID", tmp_path, _SPEC
    )
    assert status_override is None and error_code is None
    assert manifest is not None
    assert manifest["schema_version"] == 1
    assert manifest["engine"] == "sfincs_quadtree"
    assert manifest["status"] == "ok"
    # peak + 3 frames written into the deck dir for the *.tif sweep.
    assert (tmp_path / "flood_depth_peak.tif").exists()
    assert (tmp_path / "flood_depth_frame_03.tif").exists()
    assert "flood_depth_peak.tif" in rels
    peak = manifest["layers"][0]
    assert peak["role"] == "primary"
    assert peak["style_preset"] == "continuous_flood_depth"
    assert peak["has_overviews"] is True
    assert peak["cog_uri"].startswith("s3://") or peak["cog_uri"].startswith("gs://")
    assert "/tiles/" not in peak["cog_uri"]  # bare key, not a tile URL


def test_run_raster_postprocess_honesty_gate(tmp_path: Path):
    _write_quadtree_map(tmp_path / "sfincs_map.nc", flooded=False)
    manifest, status_override, error_code, rels = ep.run_raster_postprocess(
        "RID", tmp_path, _SPEC
    )
    assert status_override == "error"
    assert error_code == "RUN_OUTPUT_EMPTY"
    assert manifest["status"] == "error"
    assert manifest["layers"] == []
    assert not (tmp_path / "flood_depth_peak.tif").exists()


def test_run_raster_postprocess_no_local_nc_is_skip(tmp_path: Path):
    # No sfincs_map.nc (failed solve) -> graceful skip, legacy fallback path.
    manifest, status_override, error_code, rels = ep.run_raster_postprocess(
        "RID", tmp_path, _SPEC
    )
    assert manifest is None and status_override is None and error_code is None


def test_write_completion_carries_publish_manifest_uri(tmp_path: Path, monkeypatch):
    captured: dict = {}

    def _fake_put_json(payload, uri):
        captured["payload"] = payload
        captured["uri"] = uri
        return uri

    monkeypatch.setattr(ep, "_put_json", _fake_put_json)
    monkeypatch.setenv("TRID3NT_OBJECT_STORE", "s3")
    monkeypatch.setattr(ep, "RUNS_BUCKET", "runs-b")
    ep._write_completion(
        run_id="RID", status="ok", exit_code=0, output_uris=["s3://runs-b/RID/x.tif"],
        stdout_uri=None, stderr_uri=None, deck_provenance=None, started_at="t",
        error=None, publish_manifest_uri="s3://runs-b/RID/publish_manifest.json",
    )
    assert captured["payload"]["publish_manifest_uri"] == (
        "s3://runs-b/RID/publish_manifest.json"
    )
    # completion.json keeps every key wait_for_completion reads.
    for k in ("run_id", "status", "exit_code", "output_uris",
              "sfincs_stdout_uri", "sfincs_stderr_uri", "started_at",
              "finished_at", "error"):
        assert k in captured["payload"]
