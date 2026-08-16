"""HEC-RAS worker entrypoint unit tests (mesh wave M3).

Flat-import pattern (M2 lesson: workers tests do NOT collect from repo
root -- run FROM the worker dir):

    cd workers/hecras && python -m pytest test_entrypoint.py

Binary-free: the engine legs need the baked HEC-RAS binaries + a 4 MB deck, so
these tests exercise the pure helpers + the honest-failure surface + the metric
extractor against a synthetic minimal Results HDF (no engine invocation).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import h5py
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import entrypoint  # noqa: E402


def _write_min_results_hdf(path: Path, *, with_results: bool = True) -> None:
    """A minimal plan HDF mimicking the post-unsteady structure the extractor reads."""
    with h5py.File(path, "w") as f:
        if not with_results:
            f.create_group("Geometry")
            return
        va = f.create_group("Results/Unsteady/Summary/Volume Accounting")
        va.attrs["Error Percent"] = np.float64(0.005835)
        va.attrs["Error"] = np.float64(-2.157)
        va.attrs["Vol accounting in"] = np.bytes_(b"Acre Feet")
        va.attrs["Total Boundary Flux of Water In"] = np.float64(36674.18)
        base = "Results/Unsteady/Output/Output Blocks/Base Output/Summary Output"
        area = f.create_group(f"{base}/2D Flow Areas/2D Interior Area")
        mw = np.array([[900.0, 951.9, 1e31], [0.0, 940.0, 930.0]], dtype=np.float32)
        area.create_dataset("Maximum Water Surface", data=mw)
        xs = f.create_group(f"{base}/Cross Sections")
        xs.create_dataset("Maximum Water Surface", data=np.array([[955.0, 948.0]], dtype=np.float32))


def test_finite_masks_fill_values():
    a = np.array([1.0, 1e31, -1e31, 5.0])
    out = entrypoint._finite(a)
    assert np.isnan(out[1]) and np.isnan(out[2])
    assert out[0] == 1.0 and out[3] == 5.0


def test_extract_metrics_reads_volume_accounting_and_wse(tmp_path):
    hdf = tmp_path / "plan.hdf"
    _write_min_results_hdf(hdf)
    m = entrypoint._extract_metrics(hdf)
    assert m["volume_accounting"]["Error Percent"] == pytest.approx(0.005835)
    assert m["volume_accounting"]["Vol accounting in"] == "Acre Feet"
    twod = m["max_water_surface_2d"]["2D Interior Area"]
    # the 1e31 fill must be masked out of the max
    assert twod["max_ft"] == pytest.approx(951.9, abs=1e-2)
    assert twod["cells"] == 3
    assert m["max_water_surface_1d"]["max_ft"] == pytest.approx(955.0)


def test_extract_metrics_raises_without_results(tmp_path):
    hdf = tmp_path / "noresults.hdf"
    _write_min_results_hdf(hdf, with_results=False)
    with pytest.raises(entrypoint.HecrasError, match="no /Results"):
        entrypoint._extract_metrics(hdf)


def test_run_raises_on_missing_manifest(tmp_path):
    with pytest.raises(entrypoint.HecrasError, match="no manifest.json"):
        entrypoint.run(tmp_path)


def test_run_raises_on_missing_plan_hdf(tmp_path):
    (tmp_path / "manifest.json").write_text(
        json.dumps({"plan_hdf": "Absent.p04.tmp.hdf", "geom_suffix": "x04"})
    )
    with pytest.raises(entrypoint.HecrasError, match="not found"):
        entrypoint.run(tmp_path)


def test_run_rejects_unknown_manifest_field(tmp_path):
    """ADR 0158: an unknown manifest.json field errors loudly instead of
    silently keeping the deck's baked default (the ADR 0148 lesson)."""
    (tmp_path / "manifest.json").write_text(
        json.dumps({
            "plan_hdf": "Absent.p04.tmp.hdf", "geom_suffix": "x04",
            "typo_field_name": 1.0,
        })
    )
    with pytest.raises(entrypoint.HecrasError, match="typo_field_name"):
        entrypoint.run(tmp_path)


def test_seam_envelope_fields_are_accepted():
    """ADR 0188: the generic run_solver-seam envelope (run_id/inputs/outputs/
    hecras_args) rides the same manifest.json and must NOT be rejected as
    unknown -- the M3-gate fresh-deck path (hecras_flood_2d) stages via it."""
    for field in ("run_id", "inputs", "outputs", "hecras_args"):
        assert field in entrypoint._KNOWN_MANIFEST_FIELDS, field
    # a full M3 manifest with the seam envelope passes the field check
    entrypoint._reject_unknown_manifest_fields({
        "run_id": "01ABC", "plan_hdf": "Fresh2D.p04.tmp.hdf", "geom_suffix": "x04",
        "run_geompre": True, "inputs": [{"gs_uri": "s3://x", "dest": "a"}],
        "hecras_args": [], "outputs": ["Fresh2D.p04.tmp.hdf"],
    })


def test_main_returns_nonzero_on_failure(tmp_path):
    # No manifest -> run() raises -> main() surfaces a non-zero exit (honest).
    assert entrypoint.main([str(tmp_path)]) == 1
