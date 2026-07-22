# Worker-side wiring test: the GeoClaw postprocess module.
#
# Covers the happy path (fort.q AMR frame -> EPSG:4326 COG + manifest) and
# the honesty gate (no frames -> error; dry patch -> error). Fort.q frames are
# written as ASCII text so no real clawpack binary is needed.

from __future__ import annotations

import sys
from pathlib import Path

import pytest

rasterio = pytest.importorskip("rasterio")

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from services.workers._geoclaw_postprocess import postprocess as pp  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BBOX = (-87.5, 29.5, -85.5, 31.0)  # 2x1.5 deg Gulf test AOI

_BUILD_SPEC = {
    "bbox": list(_BBOX),
    "scenario": "dam_break",
    "mask_ocean": False,
}


def _fort_q_frame(*, h_value: float, mx: int = 6, my: int = 5) -> str:
    """Write a minimal fort.q ASCII frame with a single AMR patch.

    Each line in the data section is one cell (h hu hv eta).  The parser in
    postprocess.py takes parts[0] as h and reads one value per line.
    """
    lines = [
        "1      grid_number",
        "1      AMR_level",
        f"{mx}      mx",
        f"{my}      my",
        f"{_BBOX[0]:.4f}      xlow",
        f"{_BBOX[1]:.4f}      ylow",
        f"{(_BBOX[2] - _BBOX[0]) / mx:.6f}      dx",
        f"{(_BBOX[3] - _BBOX[1]) / my:.6f}      dy",
    ]
    # mx * my data lines
    for _ in range(mx * my):
        lines.append(f"{h_value:.6f} 0.000000 0.000000 {h_value:.6f}")
    return "\n".join(lines) + "\n"


def _write_frame(scratch: Path, *, frame_no: int = 1, h_value: float = 2.5) -> None:
    name = f"fort.q{frame_no:04d}"
    (scratch / name).write_text(_fort_q_frame(h_value=h_value), encoding="utf-8")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_run_geoclaw_postprocess_ok(tmp_path: Path):
    _write_frame(tmp_path, h_value=2.5)
    result = pp.run_geoclaw_postprocess(
        "RID", tmp_path, _BUILD_SPEC, lambda rel: f"s3://runs-b/RID/{rel}"
    )
    assert result.status == "ok", result.error_message
    assert result.error_code is None
    manifest = result.manifest
    assert manifest is not None
    assert manifest["engine"] == "geoclaw"
    assert manifest["status"] == "ok"
    assert len(manifest["layers"]) >= 1
    layer = manifest["layers"][0]
    cog_name = Path(layer["cog_uri"]).name
    cog = tmp_path / cog_name
    assert cog.exists(), f"COG {cog} not produced"
    with rasterio.open(cog) as src:
        assert str(src.crs) == "EPSG:4326"
    m = layer["metrics"]
    assert m["max_depth_m"] == pytest.approx(2.5, rel=0.2)
    assert m["flooded_area_km2"] >= 0.0


def test_run_geoclaw_postprocess_dry_honesty_gate(tmp_path: Path):
    _write_frame(tmp_path, h_value=0.0)  # no water
    result = pp.run_geoclaw_postprocess(
        "RID", tmp_path, _BUILD_SPEC, lambda rel: f"s3://runs-b/RID/{rel}"
    )
    assert result.status == "error"
    assert result.error_code is not None
    assert result.manifest is None


def test_run_geoclaw_postprocess_no_frames(tmp_path: Path):
    result = pp.run_geoclaw_postprocess(
        "RID", tmp_path, _BUILD_SPEC, lambda rel: f"s3://runs-b/RID/{rel}"
    )
    assert result.status == "error"
    assert result.error_code == "GEOCLAW_OUTPUT_EMPTY"
    assert result.manifest is None


def test_run_geoclaw_postprocess_missing_bbox(tmp_path: Path):
    _write_frame(tmp_path, h_value=2.5)
    result = pp.run_geoclaw_postprocess(
        "RID", tmp_path, {"scenario": "dam_break"}, lambda rel: f"s3://runs-b/RID/{rel}"
    )
    assert result.status == "error"
    assert result.error_code == "GEOCLAW_OUTPUT_EMPTY"
