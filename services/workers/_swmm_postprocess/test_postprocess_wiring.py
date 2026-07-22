# Worker-side wiring test: the SWMM postprocess module.
#
# Covers the happy path (SWMM .out node depths -> EPSG:4326 COG + manifest)
# and the honesty gate (no .out file -> error; dry solve -> error). The binary
# pyswmm Output reader (_read_swmm_out_depths) is monkeypatched so no real
# .out binary is needed; COG rasterization runs for real.

from __future__ import annotations

import sys
from pathlib import Path

import pytest

rasterio = pytest.importorskip("rasterio")

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from services.workers._swmm_postprocess import postprocess as pp  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# 8x8 mesh, 50-m cells, EPSG:32617.  affine = [x_off, x_pix, x_rot, y_off, y_rot, y_pix]
# from_origin(500000, 4400400, 50, 50) -> x_off=500000, x_pix=50, y_off=4400400, y_pix=-50
_NROWS, _NCOLS = 8, 8
_RES_M = 50.0
_CRS = "EPSG:32617"
_TRANSFORM = [500000.0, 50.0, 0.0, 4400400.0, 0.0, -50.0]
_BBOX = [-87.5, 29.5, -85.5, 31.0]

_POSTPROCESS_SPEC = {
    "grid_shape": [_NROWS, _NCOLS],
    "resolution_m": _RES_M,
    "crs": _CRS,
    "transform": _TRANSFORM,
    "bbox": _BBOX,
}


def _fake_timesteps(*, flooded: bool) -> list[dict[str, float]]:
    """Return one timestep dict with node depths scattered on the grid."""
    ts: dict[str, float] = {}
    if flooded:
        # Set depth > NODATA_DEPTH_M for a patch of nodes
        for i in range(3, 5):
            for j in range(3, 6):
                ts[f"S_{i}_{j}"] = 1.5
    return [ts]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_run_swmm_postprocess_ok(tmp_path: Path, monkeypatch):
    # Stub .out file so the glob finds it.
    (tmp_path / "mesh.out").write_bytes(b"stub")
    monkeypatch.setattr(
        pp, "_read_swmm_out_depths",
        lambda _p, _gs: _fake_timesteps(flooded=True),
    )

    result = pp.run_swmm_postprocess(
        "RID", tmp_path, _POSTPROCESS_SPEC, lambda rel: f"s3://runs-b/RID/{rel}"
    )
    assert result.status == "ok", result.error_message
    assert result.error_code is None
    manifest = result.manifest
    assert manifest is not None
    assert manifest["engine"] == "swmm"
    assert manifest["status"] == "ok"
    assert len(manifest["layers"]) >= 1
    layer = manifest["layers"][0]
    assert layer["role"] == "primary"
    assert layer["style_preset"] == pp.SWMM_DEPTH_STYLE_PRESET
    m = layer["metrics"]
    assert m["max_depth_m"] == pytest.approx(1.5, rel=0.1)
    assert m["flooded_area_km2"] >= 0.0
    cog_name = Path(layer["cog_uri"]).name
    cog = tmp_path / cog_name
    assert cog.exists(), f"COG {cog} not produced"
    with rasterio.open(cog) as src:
        assert str(src.crs) == "EPSG:4326"


def test_run_swmm_postprocess_dry_honesty_gate(tmp_path: Path, monkeypatch):
    (tmp_path / "mesh.out").write_bytes(b"stub")
    monkeypatch.setattr(
        pp, "_read_swmm_out_depths",
        lambda _p, _gs: _fake_timesteps(flooded=False),
    )

    result = pp.run_swmm_postprocess(
        "RID", tmp_path, _POSTPROCESS_SPEC, lambda rel: f"s3://runs-b/RID/{rel}"
    )
    assert result.status == "error"
    assert result.error_code is not None
    assert result.manifest is None


def test_run_swmm_postprocess_no_out_file(tmp_path: Path):
    result = pp.run_swmm_postprocess(
        "RID", tmp_path, _POSTPROCESS_SPEC, lambda rel: f"s3://runs-b/RID/{rel}"
    )
    assert result.status == "error"
    assert result.error_code == "SWMM_OUTPUT_EMPTY"
    assert result.manifest is None


def test_run_swmm_postprocess_missing_spec_keys(tmp_path: Path, monkeypatch):
    (tmp_path / "mesh.out").write_bytes(b"stub")
    monkeypatch.setattr(
        pp, "_read_swmm_out_depths",
        lambda _p, _gs: _fake_timesteps(flooded=True),
    )
    # Omit "crs" from spec -> should return error
    spec = {k: v for k, v in _POSTPROCESS_SPEC.items() if k != "crs"}
    result = pp.run_swmm_postprocess(
        "RID", tmp_path, spec, lambda rel: f"s3://runs-b/RID/{rel}"
    )
    assert result.status == "error"
    assert result.error_code == "SWMM_OUTPUT_EMPTY"
