# Shared raster-postprocess unit tests (worker-side postprocess-offload spike).
#
# Cover the GPL-free NetCDF -> COG substrate with SYNTHETIC NetCDFs (no Batch, no
# S3, no cht): regular-grid + quadtree extraction, the parallel + serial frame
# encode, the empty-field honesty gate, band_stats precompute, and the manifest
# round-trip. rasterio/xarray/scipy ship in the agent venv
# (venvs/agent), so these run there; they skip cleanly where a dep is absent.

from __future__ import annotations

import sys
from pathlib import Path

import pytest

rasterio = pytest.importorskip("rasterio")
xr = pytest.importorskip("xarray")
pytest.importorskip("scipy")
import numpy as np  # noqa: E402

# Make the repo root importable so ``services.workers._raster_postprocess`` works
# from the agent venv (the conftest at services/workers does this for the worker
# tree; this test is collected under the package dir).
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from services.workers._raster_postprocess import (  # noqa: E402
    band_stats as _band_stats,
)
from services.workers._raster_postprocess import cog as _cog  # noqa: E402
from services.workers._raster_postprocess import manifest as _manifest  # noqa: E402
from services.workers._raster_postprocess import postprocess as _pp  # noqa: E402
from services.workers._raster_postprocess import sfincs_reader as _reader  # noqa: E402


# --------------------------------------------------------------------------- #
# Synthetic NetCDF builders (scipy backend — NetCDF3, the only one in the venv).
# --------------------------------------------------------------------------- #


def _write_regular_nc(
    path: Path,
    *,
    nx: int = 16,
    ny: int = 12,
    n_time: int = 0,
    depth: float = 2.0,
    epsg: int = 32616,
    flooded: bool = True,
) -> None:
    """A regular-grid sfincs_map.nc: hmax (peak) + optional zs(time)/zb frames."""
    x = np.linspace(500000.0, 500000.0 + nx * 50.0, nx).astype("float64")
    y = np.linspace(3300000.0, 3300000.0 + ny * 50.0, ny).astype("float64")
    hmax = np.full((ny, nx), depth if flooded else 0.0, dtype="float32")
    data = {
        "hmax": (("n", "m"), hmax),
        "crs": ((), np.int32(epsg)),
    }
    coords = {"x": ("m", x), "y": ("n", y)}
    if n_time > 1:
        zb = np.full((ny, nx), -1.0, dtype="float32")  # bed 1 m below datum
        # water-level rising from 0.5 -> 1.5 over time (depth = zs - zb).
        zs = np.stack(
            [np.full((ny, nx), 0.5 + 1.0 * t / (n_time - 1), dtype="float32")
             for t in range(n_time)]
        )
        data["zb"] = (("n", "m"), zb)
        data["zs"] = (("time", "n", "m"), zs)
        coords["time"] = ("time", np.arange(n_time, dtype="float64"))
    ds = xr.Dataset(data, coords=coords)
    ds.to_netcdf(path, engine="scipy")


def _write_quadtree_nc(
    path: Path,
    *,
    n_faces: int = 64,
    n_time: int = 0,
    epsg: int = 32616,
    wave: bool = False,
    flooded: bool = True,
) -> None:
    """A face-indexed (quadtree) sfincs_map.nc: hmax (or hm0) on nmesh2d_face."""
    rng = np.random.default_rng(0)
    # Face centres in UTM 16N near the (-85.3..-85.0, 29.9..30.1) test bbox so a
    # reprojected bbox-bounded grid actually overlaps the mesh.
    fx = (665000.0 + rng.uniform(0, 25000, n_faces)).astype("float64")
    fy = (3310000.0 + rng.uniform(0, 20000, n_faces)).astype("float64")
    val = (1.0 + rng.uniform(0, 2, n_faces)).astype("float32")
    if not flooded:
        val[:] = 0.0
    field_name = "hm0" if wave else "hmax"
    data = {
        field_name: (("nmesh2d_face",), val),
        "mesh2d_face_x": (("nmesh2d_face",), fx),
        "mesh2d_face_y": (("nmesh2d_face",), fy),
        "crs": ((), np.int32(epsg)),
    }
    coords: dict = {}
    if n_time > 1:
        ts = np.stack(
            [(val * (0.4 + 0.6 * t / (n_time - 1))).astype("float32")
             for t in range(n_time)]
        )  # (time, face)
        data[field_name] = (("time", "nmesh2d_face"), ts)
        coords["time"] = ("time", np.arange(n_time, dtype="float64"))
        if not wave:
            # depth path needs zs(time)+zb too; emit them on faces.
            zb = np.full((n_faces,), -1.0, dtype="float32")
            zs = np.stack(
                [np.full((n_faces,), 0.5 + 1.0 * t / (n_time - 1), dtype="float32")
                 for t in range(n_time)]
            )
            data["zb"] = (("nmesh2d_face",), zb)
            data["zs"] = (("time", "nmesh2d_face"), zs)
    ds = xr.Dataset(data, coords=coords)
    ds.to_netcdf(path, engine="scipy")


def _runs_uri_for(run_id: str):
    return lambda rel: f"s3://test-runs/{run_id}/{rel}"


# --------------------------------------------------------------------------- #
# cog.py — CRS + readers + finalize
# --------------------------------------------------------------------------- #


def test_read_crs_from_scalar_variable(tmp_path: Path):
    nc = tmp_path / "m.nc"
    _write_regular_nc(nc, epsg=32616)
    ds = xr.open_dataset(nc)
    try:
        assert _cog.read_crs_from_dataset(ds) == "EPSG:32616"
        assert _cog.is_quadtree_output(ds) is False
    finally:
        ds.close()


def test_is_quadtree_and_face_coords(tmp_path: Path):
    nc = tmp_path / "q.nc"
    _write_quadtree_nc(nc, n_faces=32)
    ds = xr.open_dataset(nc)
    try:
        assert _cog.is_quadtree_output(ds) is True
        fx, fy = _cog.read_face_coords(ds)
        assert fx.shape == (32,) and fy.shape == (32,)
    finally:
        ds.close()


def test_finalize_cog_crs_roundtrip_and_overviews(tmp_path: Path):
    arr = np.full((600, 600), 1.5, dtype="float32")
    transform = rasterio.transform.from_bounds(500000, 3300000, 530000, 3330000, 600, 600)
    out = tmp_path / "peak.tif"
    _cog.finalize_cog(arr, crs="EPSG:32616", transform=transform, out_path=out)
    with rasterio.open(out) as src:
        assert str(src.crs) == "EPSG:32616"
        # overview-bearing (the agent's _ensure_raster_has_overviews becomes a no-op)
        assert len(src.overviews(1)) >= 1


def test_finalize_cog_upsamples_tiny_raster_for_overviews(tmp_path: Path):
    arr = np.full((8, 8), 2.0, dtype="float32")  # below _COG_MIN_DIM_PX
    transform = rasterio.transform.from_bounds(500000, 3300000, 500400, 3300400, 8, 8)
    out = tmp_path / "tiny.tif"
    _cog.finalize_cog(arr, crs="EPSG:32616", transform=transform, out_path=out)
    with rasterio.open(out) as src:
        assert min(src.height, src.width) >= _cog._COG_MIN_DIM_PX
        assert len(src.overviews(1)) >= 1
        # bounds preserved by the upsample (same geographic extent).
        assert src.bounds.left == pytest.approx(500000, abs=1.0)


def test_finalize_cog_rejects_crs_bounds_mismatch(tmp_path: Path):
    # projected CRS but geographic-magnitude bounds -> CRS_TAG_MISMATCH.
    arr = np.full((520, 520), 1.0, dtype="float32")
    transform = rasterio.transform.from_bounds(-85.3, 29.9, -85.1, 30.1, 520, 520)
    out = tmp_path / "bad.tif"
    with pytest.raises(_cog.CogError) as ei:
        _cog.finalize_cog(arr, crs="EPSG:32616", transform=transform, out_path=out)
    assert ei.value.error_code == "CRS_TAG_MISMATCH"


def test_write_field_cog_quadtree_metrics(tmp_path: Path):
    rng = np.random.default_rng(1)
    fx = 500000.0 + rng.uniform(0, 500, 50)
    fy = 3300000.0 + rng.uniform(0, 500, 50)
    vals = np.full(50, 2.0, dtype="float32")
    out = tmp_path / "qf.tif"
    metrics = _cog.write_field_cog(
        out_path=out, crs="EPSG:32616", face_values=vals, face_x=fx, face_y=fy,
        bbox=None, resolution_m=20.0,
    )
    assert out.exists()
    assert metrics["max_depth_m"] == pytest.approx(2.0, abs=1e-4)
    assert metrics["flooded_cell_count"] > 0
    assert metrics["crs"] == "EPSG:32616"


# --------------------------------------------------------------------------- #
# sfincs_reader.py — frame select + extraction
# --------------------------------------------------------------------------- #


def test_select_frame_time_indices_caps(monkeypatch):
    monkeypatch.setattr(_reader, "MAX_FLOOD_FRAMES", 5)
    assert _reader.select_frame_time_indices(3) == [0, 1, 2]
    idx = _reader.select_frame_time_indices(100)
    assert len(idx) <= 5
    assert idx[0] == 0 and idx[-1] == 99  # endpoints kept


def test_extract_depth_regular_peak_only(tmp_path: Path):
    nc = tmp_path / "r.nc"
    _write_regular_nc(nc, n_time=0)
    res = _reader.extract_depth(nc)
    assert res.is_quadtree is False
    assert len(res.frames) == 1  # peak only (no time dim)
    assert res.frames[0].role == "primary"
    assert res.frames[0].name == "Peak flood depth"


def test_extract_depth_regular_with_frames(tmp_path: Path):
    nc = tmp_path / "rt.nc"
    _write_regular_nc(nc, n_time=4)
    res = _reader.extract_depth(nc)
    assert len(res.frames) == 5  # peak + 4 frames
    assert [f.name for f in res.frames[1:]] == [
        "Flood depth step 1", "Flood depth step 2",
        "Flood depth step 3", "Flood depth step 4",
    ]
    assert res.frames[1].dest_filename == "flood_depth_frame_01.tif"


def test_extract_waves_none_when_no_wave_field(tmp_path: Path):
    nc = tmp_path / "nowave.nc"
    _write_regular_nc(nc)  # hmax only, no hm0
    assert _reader.extract_waves(nc) is None


def test_extract_waves_quadtree(tmp_path: Path):
    nc = tmp_path / "w.nc"
    _write_quadtree_nc(nc, n_faces=48, n_time=3, wave=True)
    res = _reader.extract_waves(nc)
    assert res is not None
    assert res.is_quadtree is True
    assert res.frames[0].name == "Peak wave height"
    assert res.frames[0].style_preset == _reader.WAVE_HEIGHT_STYLE_PRESET
    assert any(f.name.startswith("Wave height step") for f in res.frames[1:])


# --------------------------------------------------------------------------- #
# band_stats.py
# --------------------------------------------------------------------------- #


def test_band_stats_continuous(tmp_path: Path):
    out = tmp_path / "c.tif"
    arr = np.linspace(0.0, 4.0, 600 * 600).reshape(600, 600).astype("float32")
    transform = rasterio.transform.from_bounds(500000, 3300000, 530000, 3330000, 600, 600)
    _cog.finalize_cog(arr, crs="EPSG:32616", transform=transform, out_path=out)
    stats = _band_stats.compute_band_stats(out)
    assert stats["is_rgba"] is False
    assert stats["is_categorical"] is False
    assert stats["p2"] is not None and stats["p98"] is not None
    assert stats["p2"] < stats["p98"]


def test_band_stats_all_nan_returns_none(tmp_path: Path):
    out = tmp_path / "nan.tif"
    arr = np.full((520, 520), np.nan, dtype="float32")
    transform = rasterio.transform.from_bounds(500000, 3300000, 530000, 3330000, 520, 520)
    # finalize masks <threshold to NaN anyway; write directly via write_field_cog.
    _cog.finalize_cog(arr, crs="EPSG:32616", transform=transform, out_path=out)
    stats = _band_stats.compute_band_stats(out)
    assert stats["p2"] is None and stats["p98"] is None


# --------------------------------------------------------------------------- #
# manifest.py — round trip + schema gate
# --------------------------------------------------------------------------- #


def test_manifest_round_trip():
    layer = _manifest.build_layer_entry(
        layer_id_stem="flood-depth-peak", name="Peak flood depth", role="primary",
        style_preset="continuous_flood_depth", units="meters",
        cog_uri="s3://runs/RID/flood_depth_peak.tif", frame_no=None,
        bbox=[-85.3, 29.9, -85.1, 30.1],
        band_stats={"is_categorical": False, "is_rgba": False, "p2": 0.05, "p98": 2.3},
        metrics={"max_depth_m": 2.4, "flooded_cell_count": 100},
    )
    m = _manifest.build_manifest(
        engine="sfincs_quadtree", run_id="RID", status="ok", frame_count=0,
        metrics={"max_depth_m": 2.4}, layers=[layer],
    )
    text = _manifest.manifest_to_json(m)
    back = _manifest.parse_manifest_json(text)
    assert back["schema_version"] == _manifest.MANIFEST_SCHEMA_VERSION
    assert back["layers"][0]["cog_uri"].endswith("flood_depth_peak.tif")
    assert back["layers"][0]["style_preset"] == "continuous_flood_depth"
    # cog_uri is a BARE s3 key, NOT a tile URL.
    assert back["layers"][0]["cog_uri"].startswith("s3://")
    assert "/tiles/" not in back["layers"][0]["cog_uri"]


def test_manifest_rejects_unknown_schema_version():
    bad = '{"schema_version": 999, "layers": []}'
    with pytest.raises(ValueError, match="unknown publish_manifest schema_version"):
        _manifest.parse_manifest_json(bad)


def test_manifest_rejects_missing_schema_version():
    with pytest.raises(ValueError, match="missing schema_version"):
        _manifest.parse_manifest_json('{"layers": []}')


# --------------------------------------------------------------------------- #
# postprocess.py — full orchestration (serial + parallel), honesty gate
# --------------------------------------------------------------------------- #


def test_run_postprocess_depth_regular_serial(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(_pp, "POSTPROCESS_WORKERS", 1)  # serial encode
    nc = tmp_path / "sfincs_map.nc"
    _write_regular_nc(nc, n_time=4)
    res = _pp.run_postprocess(
        nc, run_id="RID", deck_dir=tmp_path, runs_uri_for=_runs_uri_for("RID"),
        kind="depth",
    )
    assert res.status == "ok"
    # peak + 4 frames all written into the deck dir (entrypoint sweep uploads them).
    assert (tmp_path / "flood_depth_peak.tif").exists()
    assert (tmp_path / "flood_depth_frame_04.tif").exists()
    m = res.manifest
    assert m["frame_count"] == 4
    assert len(m["layers"]) == 5
    peak = m["layers"][0]
    assert peak["role"] == "primary"
    assert peak["has_overviews"] is True
    assert peak["band_stats"]["p2"] is not None
    assert peak["bbox"] is not None and len(peak["bbox"]) == 4
    assert peak["cog_uri"] == "s3://test-runs/RID/flood_depth_peak.tif"
    assert "metrics" in peak and peak["metrics"]["flooded_cell_count"] > 0
    # frames carry frame_no + the web grouping token.
    assert m["layers"][1]["frame_no"] == 1
    assert m["layers"][1]["name"] == "Flood depth step 1"


def test_run_postprocess_depth_quadtree_parallel(tmp_path: Path):
    # Default POSTPROCESS_WORKERS (>1 on a multicore box) exercises the ProcessPool.
    nc = tmp_path / "sfincs_map.nc"
    _write_quadtree_nc(nc, n_faces=80, n_time=3)
    res = _pp.run_postprocess(
        nc, run_id="QID", deck_dir=tmp_path, runs_uri_for=_runs_uri_for("QID"),
        kind="depth", bbox=(-85.3, 29.9, -85.0, 30.1), resolution_m=25.0,
    )
    assert res.status == "ok"
    assert (tmp_path / "flood_depth_peak.tif").exists()
    assert res.manifest["frame_count"] == 3
    # quadtree COGs carry the projected CRS, overviews, and a 4326 bbox.
    with rasterio.open(tmp_path / "flood_depth_peak.tif") as src:
        assert str(src.crs) == "EPSG:32616"
        assert len(src.overviews(1)) >= 1


def test_run_postprocess_honesty_gate_empty_depth(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(_pp, "POSTPROCESS_WORKERS", 1)
    nc = tmp_path / "sfincs_map.nc"
    _write_regular_nc(nc, n_time=0, flooded=False, depth=0.0)
    res = _pp.run_postprocess(
        nc, run_id="EID", deck_dir=tmp_path, runs_uri_for=_runs_uri_for("EID"),
        kind="depth",
    )
    assert res.status == "error"
    assert res.error_code == "RUN_OUTPUT_EMPTY"
    assert res.manifest["status"] == "error"
    assert res.manifest["layers"] == []
    # The empty COG is cleaned up (never lands in the runs bucket).
    assert not (tmp_path / "flood_depth_peak.tif").exists()


def test_run_postprocess_waves_no_field_is_ok_empty(tmp_path: Path):
    nc = tmp_path / "sfincs_map.nc"
    _write_regular_nc(nc)  # depth only, no hm0
    res = _pp.run_postprocess(
        nc, run_id="WID", deck_dir=tmp_path, runs_uri_for=_runs_uri_for("WID"),
        kind="waves",
    )
    # Not a SnapWave run -> honest OK manifest, no layers (depth pass owns gate).
    assert res.status == "ok"
    assert res.manifest["layers"] == []
    assert res.cog_paths == []


def test_run_postprocess_waves_quadtree(tmp_path: Path):
    nc = tmp_path / "sfincs_map.nc"
    _write_quadtree_nc(nc, n_faces=64, n_time=3, wave=True)
    res = _pp.run_postprocess(
        nc, run_id="WQ", deck_dir=tmp_path, runs_uri_for=_runs_uri_for("WQ"),
        kind="waves", bbox=(-85.3, 29.9, -85.0, 30.1),
    )
    assert res.status == "ok"
    assert (tmp_path / "wave_height_peak.tif").exists()
    peak = res.manifest["layers"][0]
    assert peak["name"] == "Peak wave height"
    assert peak["style_preset"] == "continuous_wave_height"
