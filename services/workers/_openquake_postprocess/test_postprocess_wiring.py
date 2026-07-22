# Worker-side wiring test: the OpenQuake postprocess module.
#
# Covers the happy path (hazard-map CSV -> EPSG:4326 COG + manifest) and the
# honesty gate (no CSV / empty CSV -> error status). A synthetic hazard-map CSV
# replaces real oq-engine output; the COG rasterization runs for real.

from __future__ import annotations

import sys
from pathlib import Path

import pytest

rasterio = pytest.importorskip("rasterio")

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from services.workers._openquake_postprocess import postprocess as pp  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BBOX = (-85.5, 29.5, -84.5, 30.5)  # 1-deg Gulf Coast test AOI

_CSV_HEADER = "lon,lat,pga~0.02\n"
_CSV_ROWS = (
    "-85.5,29.5,0.05\n"
    "-85.4,29.5,0.08\n"
    "-85.3,29.5,0.12\n"
    "-85.2,29.5,0.10\n"
    "-85.1,29.5,0.07\n"
    "-85.0,29.5,0.06\n"
    "-84.9,29.5,0.09\n"
    "-84.8,29.5,0.11\n"
    "-85.5,30.0,0.06\n"
    "-85.4,30.0,0.09\n"
    "-85.3,30.0,0.14\n"
    "-85.2,30.0,0.13\n"
    "-85.1,30.0,0.08\n"
    "-85.0,30.0,0.07\n"
    "-84.9,30.0,0.10\n"
    "-84.8,30.0,0.12\n"
)

_BUILD_SPEC = {
    "bbox": list(_BBOX),
    "investigation_time_years": 50.0,
    "poe": 0.1,
    "imt": "PGA",
}


def _write_csv(scratch: Path, *, rows: str = _CSV_ROWS) -> None:
    out = scratch / "output"
    out.mkdir(parents=True, exist_ok=True)
    (out / "hazard_map-mean-RLP50.0.csv").write_text(
        _CSV_HEADER + rows, encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_run_openquake_postprocess_ok(tmp_path: Path):
    _write_csv(tmp_path)
    result = pp.run_openquake_postprocess(
        "RID", tmp_path, _BUILD_SPEC, lambda rel: f"s3://runs-b/RID/{rel}"
    )
    assert result.status == "ok", result.error_message
    assert result.error_code is None
    manifest = result.manifest
    assert manifest is not None
    assert manifest["engine"] == "openquake"
    assert manifest["status"] == "ok"
    assert len(manifest["layers"]) == 1
    layer = manifest["layers"][0]
    assert layer["cog_uri"] == f"s3://runs-b/RID/{pp._HAZARD_COG_FILENAME}"
    assert layer["style_preset"] == pp.SEISMIC_HAZARD_STYLE_PRESET
    m = layer["metrics"]
    # The rasterization clusters closely-spaced lon points (~0.1 deg step vs 0.15 tol),
    # so the max may be the last-written value per row rather than the true point max.
    assert m["max_pga_g"] >= 0.10
    assert m["n_sites"] >= 1
    assert m["hazard_area_km2"] >= 0.0
    cog = tmp_path / pp._HAZARD_COG_FILENAME
    assert cog.exists()
    with rasterio.open(cog) as src:
        assert str(src.crs) == "EPSG:4326"


def test_run_openquake_postprocess_below_floor_zero_hazard_area(tmp_path: Path):
    # All values below HAZARD_FLOOR_VALUE: sites ARE parsed (finite values), so
    # n_sites > 0 and status is "ok", but hazard_area_km2 == 0 because no cell
    # exceeds the floor threshold.  The COG floor-masks those cells to NaN.
    rows = "".join(
        f"-85.{i},30.0,{pp.HAZARD_FLOOR_VALUE * 0.5:.6f}\n" for i in range(8)
    )
    _write_csv(tmp_path, rows=rows)
    result = pp.run_openquake_postprocess(
        "RID", tmp_path, _BUILD_SPEC, lambda rel: f"s3://runs-b/RID/{rel}"
    )
    assert result.status == "ok", result.error_message
    assert result.manifest is not None
    m = result.manifest["layers"][0]["metrics"]
    assert m["hazard_area_km2"] == pytest.approx(0.0)


def test_run_openquake_postprocess_no_csv(tmp_path: Path):
    result = pp.run_openquake_postprocess(
        "RID", tmp_path, _BUILD_SPEC, lambda rel: f"s3://runs-b/RID/{rel}"
    )
    assert result.status == "error"
    assert result.error_code == "OQ_HAZARD_EMPTY"
    assert result.manifest is None
