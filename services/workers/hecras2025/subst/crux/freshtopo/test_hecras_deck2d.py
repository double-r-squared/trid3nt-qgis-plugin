"""Offline gates for the pure-2D DECK COMPOSER (``hecras_deck2d``).

No engine: carve a small Muncie sub-rectangle (the mesh SOURCE), compose the
complete pure-2D deck, and assert the four deck files + the plan-HDF structure the
production Linux engines read (the 2D flow area, the Inflow + DS Boundary Condition
Lines, the 2D-BC Event-Conditions forcing, the .xNN/.bNN). The end-to-end SOLVE
(this composer -> production 6.6 engines, 1906 wet / WSE 946.94 / the x1.5 delta)
is exercised live by ``build_chippewa_wetting_deck`` in ``trid3nt-local/hecras:latest``
(ADR 0138 / the flood2d landing acceptance (a)).
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
from hecras_deck2d import (  # noqa: E402
    compose_pure2d_deck, default_hydrograph, AREA_NAME, MUNCIE_PLAN,
)
from hecras_event_conditions import BC_ROOT, EC_ROOT  # noqa: E402


@pytest.fixture(scope="module")
def carved():
    m = load_muncie()
    c = m.cell_center[:m.nc_real]
    keep = (c[:, 0] < 408600.0) & (c[:, 1] > 1803025.0)
    return carve(m, keep)


@pytest.fixture(scope="module")
def projection():
    with h5py.File(MUNCIE_PLAN, "r") as f:
        p = f.attrs["Projection"]
    return p.decode() if isinstance(p, bytes) else p


def test_default_hydrograph_ramp_holds_at_peak():
    t, q = default_hydrograph(2000.0, base_cfs=200.0, n_ord=25)
    assert t.shape == q.shape == (25,)
    assert q[0] == pytest.approx(200.0)          # starts at base
    assert q[-1] == pytest.approx(2000.0)        # holds at peak
    assert np.all(np.diff(q) >= -1e-9)           # monotone non-decreasing ramp


def test_compose_writes_four_deck_files_and_structure(tmp_path, carved, projection):
    info = compose_pure2d_deck(
        tmp_path / "run", carved.mesh, carved.tables,
        projection_wkt=projection, target_peak_cfs=2000.0)
    paths = info["paths"]
    for p in (paths.plan, paths.xnn, paths.bnn):
        assert p.exists() and p.stat().st_size > 0, f"missing deck file {p}"
    # provenance reflects the carved mesh + the authored forcing
    assert info["cells_real"] == carved.n_real == 2068
    assert info["perimeter_pts"] == int(carved.mesh.perimeter.shape[0])
    assert info["bc_faces"] > 0 and info["bc_length_ft"] > 0
    assert info["ec_peak_cfs"] == pytest.approx(2000.0)
    assert info["ec_ordinates"] == 25


def test_compose_plan_hdf_has_area_bclines_and_event_conditions(tmp_path, carved, projection):
    info = compose_pure2d_deck(
        tmp_path / "run", carved.mesh, carved.tables,
        projection_wkt=projection, target_peak_cfs=2500.0)
    with h5py.File(info["paths"].plan, "r") as f:
        # the authored 2D flow area replaced the shipped one
        assert f"Geometry/2D Flow Areas/{AREA_NAME}" in f
        # both BC lines authored on the perimeter
        bc_attrs = f["Geometry/Boundary Condition Lines/Attributes"][()]
        names = {r["Name"].decode(errors="replace").strip() for r in bc_attrs}
        assert {"Inflow", "DS"} <= names
        # the 2D-BC Event-Conditions forcing (the wetting link)
        fh = f[f"{BC_ROOT}/Flow Hydrographs"]
        nd = f[f"{BC_ROOT}/Normal Depths"]
        assert any("Inflow" in k for k in fh.keys()), "no Inflow flow hydrograph"
        assert any("DS" in k for k in nd.keys()), "no DS normal-depth outlet"
        # the flow hydrograph peak is the authored one (invariant: never synthesized)
        key = next(k for k in fh.keys() if "Inflow" in k)
        assert float(np.asarray(fh[key])[:, 1].max()) == pytest.approx(2500.0, rel=1e-3)
        # EC finalized + 1D-reach coupling stripped (pure-2D deck)
        assert f[EC_ROOT].attrs["Completed Successfully"].decode() == "True"
        for grp in ("Structures", "Reference Lines"):
            assert grp not in f["Geometry"], f"combined-1D/2D group {grp} not stripped"


def test_compose_rejects_overlapping_inflow_and_ds_runs(tmp_path, carved, projection):
    # forcing the inflow onto the SAME south edge as DS must be refused (the outlet
    # cannot share the inlet faces -- the ADR 0138 drainage physics)
    with pytest.raises(ValueError, match="overlap"):
        compose_pure2d_deck(
            tmp_path / "run", carved.mesh, carved.tables,
            projection_wkt=projection, target_peak_cfs=2000.0,
            inflow_edge="s", ds_edge="s")


def test_compose_requires_a_forcing(tmp_path, carved, projection):
    with pytest.raises(ValueError, match="times/flows|target_peak_cfs"):
        compose_pure2d_deck(
            tmp_path / "run", carved.mesh, carved.tables,
            projection_wkt=projection)
