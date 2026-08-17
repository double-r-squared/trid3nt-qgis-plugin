"""Offline gates for the 2D-BC-line Event-Conditions author (OI-FT1).

No engine, no vendored data: build a tiny in-memory geometry (a 2D area + one BC
line) via the production geometry writer, author the EC group, and assert the
decoded schema -- shapes, dtypes, attribute set, and that the face keying derives
from the geometry External Faces clipped to [0, Length].
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

_HERE = Path(__file__).resolve().parent
_HECRAS2025 = _HERE.parents[2]
for p in (str(_HERE), str(_HECRAS2025)):
    if p not in sys.path:
        sys.path.insert(0, p)

h5py = pytest.importorskip("h5py")

from carve_muncie import load_muncie, carve  # noqa: E402
from hecras_geometry_writer import (  # noqa: E402
    write_2d_flow_area, write_boundary_condition_lines,
    BoundaryConditionLine, perimeter_face_run, PropertyTableOptions, AREA_GROUP,
)
from hecras_event_conditions import (  # noqa: E402
    write_flow_hydrograph_2d_bc, write_normal_depth_2d_bc,
    strip_1d_reach_bcs, finalize_event_conditions, derive_bc_faces,
    BC_ROOT, EC_ROOT,
)

AREA = "2D Interior Area"
BC_ROOT_FH = f"{BC_ROOT}/Flow Hydrographs"
BC_ROOT_ND = f"{BC_ROOT}/Normal Depths"


@pytest.fixture(scope="module")
def geom(tmp_path_factory):
    """A tiny fresh carve with one Inflow BC line, written to a plan HDF."""
    m = load_muncie()
    c = m.cell_center[:m.nc_real]
    keep = (c[:, 0] < 408600.0) & (c[:, 1] > 1803025.0)
    r = carve(m, keep)
    path = tmp_path_factory.mktemp("ec") / "plan.hdf"
    with h5py.File(path, "w") as f:
        f.attrs["Projection"] = ""
        write_2d_flow_area(f, AREA, r.mesh, r.tables, PropertyTableOptions(),
                           projection_wkt="")
        run = perimeter_face_run(
            r.mesh, min_elevation=r.tables.face_min_elevation, n_faces=14)
        write_boundary_condition_lines(
            f, [BoundaryConditionLine(name="Inflow", sa_2d=AREA, face_indices=run)],
            r.mesh)
    return path


def test_flow_hydrograph_schema(geom):
    t = np.linspace(0.0, 1.0, 25)
    q = np.linspace(200.0, 2000.0, 25)
    with h5py.File(geom, "r+") as f:
        info = write_flow_hydrograph_2d_bc(
            f, AREA, "Inflow", t, q,
            start_date="01Jan1900 2400", end_date="02Jan1900 2400", interval="Days")
        d = f[f"{BC_ROOT_FH}/2D: {AREA} BCLine: Inflow"]
        assert d.shape == (25, 2) and d.dtype == np.float32
        assert np.allclose(d[:, 0], t, atol=1e-4)
        assert np.allclose(d[:, 1], q, atol=1e-3)
        # attribute set + dtypes
        assert d.attrs["2D Flow Area"].decode() == AREA
        assert d.attrs["BC Line"].decode() == "Inflow"
        assert d.attrs["Data Type"].decode() == "INST-VAL"
        assert d.attrs["Check TW Stage"].decode() == "False"
        assert np.dtype(d.attrs["Face Indexes"].dtype) == np.int32
        assert np.dtype(d.attrs["Face Fraction"].dtype) == np.float32
        assert int(d.attrs["Node Index"]) == 1
        k = d.attrs["Face Indexes"].size
        assert d.attrs["Face Point Indexes"].size == k + 1
        assert d.attrs["Face Fraction"].size == k
    assert info["faces"] >= 1


def test_face_keying_derives_from_geometry(geom):
    """The EC Face Indexes/Fraction reproduce the geometry External Faces clip."""
    with h5py.File(geom, "r+") as f:
        fi, fp, frac, length = derive_bc_faces(f, "Inflow")
        ef = f["Geometry/Boundary Condition Lines/External Faces"][()]
        geo_faces = [int(r["Face Index"]) for r in ef if int(r["BC Line ID"]) == 0]
        # our writer lays the polyline exactly on face endpoints: all faces kept,
        # every fraction == 1.0, and order preserved.
        assert list(fi) == geo_faces
        assert np.allclose(frac, 1.0)
        assert fp.size == fi.size + 1


def test_normal_depth_and_finalize(geom):
    with h5py.File(geom, "r+") as f:
        write_normal_depth_2d_bc(f, AREA, "Inflow", slope=0.0015)
        nd = f[f"{BC_ROOT_ND}/2D: {AREA} BCLine: Inflow"]
        assert nd.shape == (1,) and nd.dtype == np.float32
        assert abs(float(nd[0]) - 0.0015) < 1e-6
        assert nd.attrs["BC Line WS"].decode() == "Multiple"
        finalize_event_conditions(f)
        assert f[EC_ROOT].attrs["Completed Successfully"].decode() == "True"
        assert f["Event Conditions/Unsteady/Initial Conditions"].attrs[
            "Startup Mode"].decode() == "Computed"


def test_strip_1d_reach_bcs(geom):
    with h5py.File(geom, "r+") as f:
        grp = f.require_group(BC_ROOT_FH)
        grp.create_dataset("River: White  Reach: Muncie  RS: 15696.24",
                           data=np.zeros((3, 2), np.float32))
        n = strip_1d_reach_bcs(f)
        assert n >= 1
        assert "River: White  Reach: Muncie  RS: 15696.24" not in f[BC_ROOT_FH]
        # the authored 2D entry survives
        assert f"2D: {AREA} BCLine: Inflow" in f[BC_ROOT_FH]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
