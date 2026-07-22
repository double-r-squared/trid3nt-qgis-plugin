# Worker-side wiring test: the regular-grid SFINCS (sfincs) worker's
# run_raster_postprocess + completion.json publish_manifest_uri emission.
#
# The regular-grid path reads its bbox off the NetCDF 1D x/y coords (no spec
# bbox), writes overview-bearing COGs into scratch for the *.tif sweep, builds
# the publish manifest, and fires the empty-field honesty gate.

from __future__ import annotations

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

from services.workers.sfincs import entrypoint as ep  # noqa: E402


def _write_regular_map(path: Path, *, nx=20, ny=16, n_time=3, flooded=True) -> None:
    x = np.linspace(500000.0, 500000.0 + nx * 50.0, nx).astype("float64")
    y = np.linspace(3300000.0, 3300000.0 + ny * 50.0, ny).astype("float64")
    hmax = np.full((ny, nx), 2.0 if flooded else 0.0, dtype="float32")
    zb = np.full((ny, nx), -1.0, dtype="float32")
    zs = np.stack(
        [np.full((ny, nx), 0.5 + 1.0 * t / (n_time - 1), dtype="float32")
         for t in range(n_time)]
    )
    ds = xr.Dataset(
        {
            "hmax": (("n", "m"), hmax),
            "zb": (("n", "m"), zb),
            "zs": (("time", "n", "m"), zs),
            "crs": ((), np.int32(32616)),
        },
        coords={
            "x": ("m", x), "y": ("n", y),
            "time": ("time", np.arange(n_time, dtype="float64")),
        },
    )
    ds.to_netcdf(path, engine="scipy")


def test_run_raster_postprocess_regular_grid(tmp_path: Path):
    _write_regular_map(tmp_path / "sfincs_map.nc")
    manifest, status_override, error_code = ep.run_raster_postprocess("RID", tmp_path)
    assert status_override is None and error_code is None
    assert manifest is not None
    assert manifest["engine"] == "sfincs"
    assert manifest["status"] == "ok"
    assert (tmp_path / "flood_depth_peak.tif").exists()
    assert manifest["frame_count"] == 3
    with rasterio.open(tmp_path / "flood_depth_peak.tif") as src:
        assert str(src.crs) == "EPSG:32616"
        assert len(src.overviews(1)) >= 1


def test_run_raster_postprocess_regular_honesty_gate(tmp_path: Path):
    _write_regular_map(tmp_path / "sfincs_map.nc", flooded=False)
    manifest, status_override, error_code = ep.run_raster_postprocess("RID", tmp_path)
    assert status_override == "error"
    assert error_code == "RUN_OUTPUT_EMPTY"
    assert not (tmp_path / "flood_depth_peak.tif").exists()


def test_run_raster_postprocess_no_local_nc_is_skip(tmp_path: Path):
    manifest, status_override, error_code = ep.run_raster_postprocess("RID", tmp_path)
    assert manifest is None and status_override is None and error_code is None


def test_write_completion_carries_publish_manifest_uri(tmp_path: Path, monkeypatch):
    captured: dict = {}

    class _FakeS3:
        def put_object(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(ep, "_s3_client", lambda: _FakeS3())
    monkeypatch.setenv("GRACE2_OBJECT_STORE", "s3")
    monkeypatch.setattr(ep, "RUNS_BUCKET", "runs-b")
    ep._write_completion(
        run_id="RID", status="ok", exit_code=0,
        output_uris=["s3://runs-b/RID/x.tif"], stdout_uri=None, stderr_uri=None,
        started_at="t", error=None,
        publish_manifest_uri="s3://runs-b/RID/publish_manifest.json",
    )
    import json

    body = json.loads(captured["Body"].decode("utf-8"))
    assert body["publish_manifest_uri"] == "s3://runs-b/RID/publish_manifest.json"
    for k in ("run_id", "status", "exit_code", "output_uris", "started_at",
              "finished_at", "error"):
        assert k in body
