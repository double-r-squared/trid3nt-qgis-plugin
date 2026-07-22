# Worker-side wiring test: the SWAN postprocess module.
#
# Covers the happy path (swan_out.mat -> EPSG:4326 COG + manifest) and the
# honesty gate (no mat file -> error; zero Hs -> error). The mat binary reader
# (_read_mat_fields) is monkeypatched so no real scipy mat file is needed;
# all other logic (rasterisation + COG write) runs for real.

from __future__ import annotations

import sys
from pathlib import Path

import pytest

rasterio = pytest.importorskip("rasterio")
import numpy as np  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from services.workers._swan_postprocess import postprocess as pp  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BBOX = (-87.5, 29.5, -85.5, 31.0)  # 2x1.5 deg Gulf test AOI

_BUILD_SPEC = {
    "bbox": list(_BBOX),
    "n_frames": 1,
    "resolution_m": 500.0,
}


def _fake_hs(*, flooded: bool, shape: tuple[int, int] = (20, 24)) -> np.ndarray:
    arr = np.zeros(shape, dtype="float64")
    if flooded:
        arr[8:12, 8:16] = 2.5  # some wave height above zero
    return arr


def _fake_mat_fields(*, flooded: bool) -> dict:
    hs = _fake_hs(flooded=flooded)
    tp = np.full_like(hs, 8.0 if flooded else 0.0)
    dir_ = np.full_like(hs, 180.0 if flooded else 0.0)
    return {"hs": [hs], "tp": [tp], "dir": [dir_]}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_run_swan_postprocess_ok(tmp_path: Path, monkeypatch):
    # Create a stub mat file so _discover_mat finds it.
    (tmp_path / "swan_out.mat").write_bytes(b"stub")
    monkeypatch.setattr(pp, "_read_mat_fields", lambda _p: _fake_mat_fields(flooded=True))

    result = pp.run_swan_postprocess(
        "RID", tmp_path, _BUILD_SPEC, lambda rel: f"s3://runs-b/RID/{rel}"
    )
    assert result.status == "ok", result.error_message
    assert result.error_code is None
    manifest = result.manifest
    assert manifest is not None
    assert manifest["engine"] == "swan"
    assert manifest["status"] == "ok"
    assert len(manifest["layers"]) >= 1
    layer = manifest["layers"][0]
    assert "hs" in layer["cog_uri"].lower() or "wave" in layer["cog_uri"].lower()
    assert layer["style_preset"] == pp.SWAN_WAVE_HEIGHT_STYLE_PRESET
    m = layer["metrics"]
    assert m["max_hs_m"] == pytest.approx(2.5, rel=0.1)
    assert m["wave_area_km2"] >= 0.0
    cog_name = Path(layer["cog_uri"]).name
    cog = tmp_path / cog_name
    assert cog.exists(), f"expected COG {cog} not found; scratch: {list(tmp_path.iterdir())}"
    with rasterio.open(cog) as src:
        assert str(src.crs) == "EPSG:4326"


def test_run_swan_postprocess_zero_hs_honesty_gate(tmp_path: Path, monkeypatch):
    (tmp_path / "swan_out.mat").write_bytes(b"stub")
    monkeypatch.setattr(pp, "_read_mat_fields", lambda _p: _fake_mat_fields(flooded=False))

    result = pp.run_swan_postprocess(
        "RID", tmp_path, _BUILD_SPEC, lambda rel: f"s3://runs-b/RID/{rel}"
    )
    assert result.status == "error"
    assert result.error_code is not None
    assert result.manifest is None


def test_run_swan_postprocess_no_mat(tmp_path: Path):
    result = pp.run_swan_postprocess(
        "RID", tmp_path, _BUILD_SPEC, lambda rel: f"s3://runs-b/RID/{rel}"
    )
    assert result.status == "error"
    assert result.error_code == "SWAN_OUTPUT_EMPTY"
    assert result.manifest is None
