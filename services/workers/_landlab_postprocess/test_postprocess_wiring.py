# Worker-side wiring test: the Landlab postprocess module.
#
# Covers the happy path (probability-of-failure field -> EPSG:4326 COG +
# manifest) and the honesty gate (no source raster -> error status). Mirrors
# the pattern used by the MODFLOW / SFINCS wiring tests: a synthetic rasterio
# source file replaces the real solver output, and the internal _warp_to_4326
# runs for real so the produced COG is a genuine EPSG:4326 raster we can open.

from __future__ import annotations

import sys
from pathlib import Path

import pytest

rasterio = pytest.importorskip("rasterio")
import numpy as np  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from services.workers._landlab_postprocess import postprocess as pp  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_source_cog(path: Path, *, flooded: bool) -> None:
    """Write a minimal EPSG:32617 GTiff that mimics landlab_field.tif.

    ``flooded=True`` writes a patch with probability > UNSTABLE_PROBABILITY_THRESHOLD.
    ``flooded=False`` writes all-NaN so that the honesty gate (finite_count == 0)
    triggers after reprojection (all-zeros would be finite and NOT trigger it).
    """
    from rasterio.transform import from_origin  # noqa: PLC0415

    if flooded:
        data = np.full((16, 16), 0.0, dtype="float32")
        data[6:10, 6:10] = 0.90  # above UNSTABLE_PROBABILITY_THRESHOLD (0.75)
    else:
        data = np.full((16, 16), np.nan, dtype="float32")
    transform = from_origin(500000.0, 4400800.0, 50.0, 50.0)
    with rasterio.open(
        path, "w", driver="GTiff",
        height=16, width=16, count=1, dtype="float32",
        crs="EPSG:32617", transform=transform,
        nodata=float("nan"),
    ) as dst:
        dst.write(data, 1)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_run_landlab_postprocess_ok(tmp_path: Path):
    _write_source_cog(tmp_path / pp._FIELD_COG_FILENAME, flooded=True)
    result = pp.run_landlab_postprocess(
        "RID", tmp_path, "shallow_landslide", None,
        lambda rel: f"s3://runs-b/RID/{rel}",
    )
    assert result.status == "ok", result.error_message
    assert result.error_code is None
    manifest = result.manifest
    assert manifest is not None
    assert manifest["engine"] == "landlab"
    assert manifest["status"] == "ok"
    assert len(manifest["layers"]) >= 1
    layer = manifest["layers"][0]
    assert layer["cog_uri"] == f"s3://runs-b/RID/{pp._LANDSLIDE_COG_FILENAME}"
    assert layer["style_preset"] == pp.LANDSLIDE_STYLE_PRESET
    assert layer["metrics"]["unstable_area_fraction"] > 0.0
    cog = tmp_path / pp._LANDSLIDE_COG_FILENAME
    assert cog.exists()
    with rasterio.open(cog) as src:
        assert str(src.crs) == "EPSG:4326"


def test_run_landlab_postprocess_empty_honesty_gate(tmp_path: Path):
    # All zeros -> below unstable threshold -> error
    _write_source_cog(tmp_path / pp._FIELD_COG_FILENAME, flooded=False)
    result = pp.run_landlab_postprocess(
        "RID", tmp_path, "shallow_landslide", None,
        lambda rel: f"s3://runs-b/RID/{rel}",
    )
    assert result.status == "error"
    assert result.error_code is not None
    assert result.manifest is None


def test_run_landlab_postprocess_missing_field_tif(tmp_path: Path):
    result = pp.run_landlab_postprocess(
        "RID", tmp_path, "shallow_landslide", None,
        lambda rel: f"s3://runs-b/RID/{rel}",
    )
    assert result.status == "error"
    assert result.error_code == "LANDLAB_OUTPUT_EMPTY"
    assert result.manifest is None
