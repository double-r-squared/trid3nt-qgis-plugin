"""Offline checks for the Results-typed plan-HDF skeleton builder.

Pure h5py -- no engine image. Asserts the Muncie-diff transplant produces a
``File Type="HEC-RAS Results"`` wrapper carrying the seeded fixture's real
geometry (the recognition-flip half of the shared HEC-RAS blocker; the residual
``.xNN`` authoring is engine-gated and out of an offline test's reach).
"""
from __future__ import annotations

from pathlib import Path

import h5py
import pytest

from build_plan_hdf_skeleton import build_skeleton

_FIX = Path(__file__).resolve().parent
_BEAVER = _FIX / "beaver_creek_steady" / "BEAVCREK.g01.hdf"
_CONN = _FIX / "baldeagle_connection" / "BaldEagleDamBrk.g01.hdf"


def _file_type(path: Path) -> str:
    with h5py.File(path, "r") as f:
        v = f["/"].attrs["File Type"]
    return v.decode() if isinstance(v, bytes) else str(v)


def test_seeded_geometry_hdf_is_geometry_typed():
    # The premise: the seeded fixtures ship a geometry-only HDF (the io.x-fallback
    # input), which is exactly what the skeleton wrapper must upgrade.
    assert _file_type(_BEAVER) == "HEC-RAS Geometry"


def test_steady_skeleton_is_results_typed_with_1d_geometry(tmp_path):
    out = tmp_path / "BEAVCREK.p01.tmp.hdf"
    prov = build_skeleton(_BEAVER, out, geometry_filename="BEAVCREK.g01",
                          flow_filename="BEAVCREK.f01")
    assert _file_type(out) == "HEC-RAS Results"
    with h5py.File(out, "r") as f:
        assert set(f.keys()) >= {"Plan Data", "Event Conditions", "Geometry"}
        assert "Cross Sections" in f["Geometry"]
    assert "Cross Sections" in prov["geometry_children"]


def test_connection_skeleton_carries_connection_structures(tmp_path):
    out = tmp_path / "BaldEagleDamBrk.p01.tmp.hdf"
    prov = build_skeleton(_CONN, out, geometry_filename="BaldEagleDamBrk.g01")
    assert _file_type(out) == "HEC-RAS Results"
    assert prov["structure_types"] == ["Connection"]
    assert "BaldEagleCr" in prov["flow_areas"]
    with h5py.File(out, "r") as f:
        assert "Storage Areas" in f["Geometry"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
