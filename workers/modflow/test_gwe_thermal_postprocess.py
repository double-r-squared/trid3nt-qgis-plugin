"""Offline worker-side postprocess test for the GWE gwe_thermal runner.

Exercises ``run_gwe_thermal_postprocess`` (the temperature-COG twin of
``run_plume_postprocess``) end-to-end against a REAL mf6 6.7.0 solve of the
gwt_adapter gwe_thermal deck (no monkeypatch of the reader -- the .ucn fixture is
a genuine GWE TEMPERATURE output):

  injection_plume: the runner writes a georeferenced EPSG:4326 temperature-excess
    COG placed over the deck AOI, with a positive peak excess and NO recovery
    series (there are no extract periods).
  ates: the runner additionally emits the per-cycle recovery-efficiency SERIES
    (chart data), one bounded value per cycle.

Skipped when the mf6 binary is absent (mirrors test_gwt_adapter_gwe_thermal).

Run:
    TRID3NT_MF6_BIN=$PWD/bin/mf6 venvs/agent/bin/python -m pytest \
        workers/modflow/test_gwe_thermal_postprocess.py -q
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

rasterio = pytest.importorskip("rasterio")
import numpy as np  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gwt_adapter import build_modflow_deck  # noqa: E402
from workers._modflow_postprocess import postprocess as pp  # noqa: E402

# St. Paul MN (natural place, no bbox) -- the ATES/geothermal setting the proofs use.
LAT0, LON0 = 44.95, -93.09
BASE = dict(
    spill_location_latlon=(LAT0, LON0),
    contaminant="temperature",
    release_rate_kg_s=0.0,
    aquifer_k_ms=1.0e-4,
    porosity=0.20,
)

MF6_BIN = os.environ.get("TRID3NT_MF6_BIN") or str(_REPO_ROOT / "bin" / "mf6")


def _mf6_available() -> bool:
    try:
        subprocess.run([MF6_BIN, "--version"], capture_output=True, check=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def _run(ws: Path, **deck_kwargs):
    dk = build_modflow_deck(workdir=ws, archetype="gwe_thermal", **BASE, **deck_kwargs)
    r = subprocess.run([MF6_BIN], cwd=ws, capture_output=True, text=True)
    assert r.returncode == 0, f"mf6 rc={r.returncode}\n{r.stdout[-2000:]}"
    return dk


pytestmark = pytest.mark.skipif(not _mf6_available(), reason="mf6 binary not available")


def test_gwe_thermal_injection_plume_cog_georef_and_values(tmp_path):
    ws = tmp_path / "plume"
    dk = _run(
        ws,
        gwe_mode="injection_plume",
        duration_days=120.0,
        injection_temperature_c=40.0,
        injection_rate_m3_day=400.0,
    )
    res = pp.run_gwe_thermal_postprocess(
        "RIDP", ws, dk.model_crs, lambda rel: f"s3://runs-b/RIDP/{rel}"
    )
    assert res.status == "ok", res.error_message
    # Metrics: positive heating, injection_plume mode, no recovery series.
    assert res.metrics["peak_excess_temperature_c"] > 0.0
    assert res.metrics["gwe_mode"] == "injection_plume"
    assert "recovery_efficiency_series" not in res.metrics
    assert res.metrics["ambient_temperature_c"] == pytest.approx(10.0, abs=1e-6)

    # A georeferenced EPSG:4326 COG landed in the deck dir, placed over St. Paul.
    assert res.cog_paths and res.cog_paths[0].exists()
    with rasterio.open(res.cog_paths[0]) as ds:
        assert ds.crs.to_epsg() == 4326
        b = ds.bounds
        assert b.left < LON0 < b.right, (b.left, LON0, b.right)
        assert b.bottom < LAT0 < b.top, (b.bottom, LAT0, b.top)
        arr = ds.read(1, masked=True)
        finite = np.asarray(arr.compressed(), dtype="float64")
        finite = finite[np.isfinite(finite)]
        # The rendered field is the temperature EXCESS above ambient (degC), so the
        # warm plume cells are strictly positive.
        assert finite.size > 0 and float(np.max(finite)) > 0.0


def test_gwe_thermal_ates_recovery_series_shape(tmp_path):
    n_cycles = 2
    ws = tmp_path / "ates"
    dk = _run(
        ws,
        gwe_mode="ates",
        duration_days=360.0 * n_cycles,
        n_cycles=n_cycles,
        injection_temperature_c=50.0,
        injection_rate_m3_day=300.0,
    )
    res = pp.run_gwe_thermal_postprocess(
        "RIDA", ws, dk.model_crs, lambda rel: f"s3://runs-b/RIDA/{rel}"
    )
    assert res.status == "ok", res.error_message
    assert res.metrics["gwe_mode"] == "ates"
    series = res.metrics.get("recovery_efficiency_series")
    assert isinstance(series, list) and len(series) == n_cycles, series
    for e in series:
        assert 0.0 < e < 1.0, f"recovery efficiency out of (0,1): {e}"
    # The headline scalar mirrors the last cycle.
    assert res.metrics["recovery_efficiency"] == pytest.approx(series[-1])
    # The temperature COG still lands for ATES (the charged footprint).
    assert res.cog_paths and res.cog_paths[0].exists()


def test_gwe_thermal_missing_ucn_is_typed_error(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    res = pp.run_gwe_thermal_postprocess(
        "RIDX", empty, "EPSG:32615", lambda rel: f"s3://runs-b/RIDX/{rel}"
    )
    assert res.status == "error"
    assert res.error_code == "MODFLOW_THERMAL_OUTPUT_MISSING"
