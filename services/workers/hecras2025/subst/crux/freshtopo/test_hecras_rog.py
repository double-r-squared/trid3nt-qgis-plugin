"""Offline gates for the HEC-RAS 2D RAIN-ON-GRID authoring (ADR 0199).

No engine: build the infiltration + meteorology structures and compose a full RoG
pure-2D deck from a carved Muncie sub-rectangle, then assert the plan-HDF trees the
production Linux engine reads for rain-on-grid:

  - Geometry ``.../<area>/Infiltration``  (SCS Curve Number loss layer), structured
    byte-for-byte like the shipped Bald Eagle Creek reference (Curve Number /
    Abstraction Ratio / Minimum Infiltration Rate / Cell+Face Classifications /
    Properties), sized 1:1 with the geometry cell/face counts;
  - ``Event Conditions/Meteorology/Precipitation`` (Constant-mode uniform storm),
    with the exact 6.x attrs (Enabled/Mode/Constant Value/Constant Units) + the
    Meteorology/Attributes index row;
  - a single normal-depth Outlet BC (no inflow hydrograph -- rain replaces inflow);
  - the plan simulation window patched to the storm duration.

The end-to-end SOLVE (does the engine wet the domain from rain and abstract via the
CN layer) is a LIVE smoke through ``trid3nt-local/hecras:latest`` -- these gates
prove the authored structure matches the decoded layout before any live run.
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
from hecras_deck2d import compose_pure2d_deck, AREA_NAME, MUNCIE_PLAN  # noqa: E402
from hecras_event_conditions import BC_ROOT  # noqa: E402
from hecras_infiltration import (  # noqa: E402
    amc_convert_cn, amc_to_int, build_infiltration_layer,
    HecrasInfiltrationError,
)
from hecras_meteorology import (  # noqa: E402
    constant_precipitation_ascii, inject_precipitation_ascii,
    design_storm_units_and_rate, HecrasMeteorologyError, PRECIP_PATH, MET_ROOT,
)


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


# --- infiltration unit gates ------------------------------------------------- #

def test_amc_conversion_monotone_and_identity():
    # AMC II is identity; dry lowers CN, wet raises it (NRCS NEH-630).
    assert amc_convert_cn(80.0, 2) == pytest.approx(80.0)
    cn1 = amc_convert_cn(80.0, 1)
    cn3 = amc_convert_cn(80.0, 3)
    assert cn1 < 80.0 < cn3
    assert amc_to_int("dry") == 1 and amc_to_int("III") == 3 and amc_to_int(2) == 2


def test_build_infiltration_layer_uniform_sizes_and_amc():
    layer = build_infiltration_layer(100, curve_number=85.0, amc="wet", ia_ratio=0.2)
    assert layer.curve_number.shape == (100,)
    assert layer.abstraction_ratio.shape == (100,)
    assert layer.min_infiltration_rate.shape == (100,)
    assert float(layer.curve_number[0]) == pytest.approx(amc_convert_cn(85.0, 3), rel=1e-5)
    assert np.all(layer.abstraction_ratio == np.float32(0.2))


def test_build_infiltration_layer_rejects_double_source():
    with pytest.raises(HecrasInfiltrationError):
        build_infiltration_layer(10, curve_number=80.0, per_cell_cn2=np.full(10, 80.0))
    with pytest.raises(HecrasInfiltrationError):
        build_infiltration_layer(10)  # neither given


def test_build_infiltration_layer_distributed_size_mismatch():
    with pytest.raises(HecrasInfiltrationError):
        build_infiltration_layer(10, per_cell_cn2=np.full(9, 80.0))


# --- meteorology unit gates -------------------------------------------------- #

def test_design_storm_passthrough_metric():
    rate, units = design_storm_units_and_rate(25.0)
    assert rate == 25.0 and units == "mm/hr"


def test_constant_precip_ascii_block():
    txt = constant_precipitation_ascii(25.0, "mm/hr")
    assert "Precipitation Mode=Enable" in txt
    assert "Met BC=Precipitation|Mode=Constant" in txt
    assert "Met BC=Precipitation|Constant Value=25" in txt
    assert "Met BC=Precipitation|Constant Units=mm/hr" in txt


def test_inject_precip_ascii_idempotent():
    base = "Some Header=1\nComputation Interval=2MIN\n"
    once = inject_precipitation_ascii(base, 25.0)
    twice = inject_precipitation_ascii(once, 30.0)
    # re-injection replaces, never duplicates the switch
    assert twice.count("Precipitation Mode=Enable") == 1
    assert "Met BC=Precipitation|Constant Value=30" in twice
    assert "Met BC=Precipitation|Constant Value=25" not in twice


def test_meteorology_units_validation():
    with pytest.raises(HecrasMeteorologyError):
        constant_precipitation_ascii(25.0, "cm/hr")
    with pytest.raises(HecrasMeteorologyError):
        design_storm_units_and_rate(-1.0)


# --- composed RoG deck: the plan-HDF trees the engine reads ------------------ #

def test_rog_compose_authors_precip_infiltration_and_outlet(tmp_path, carved, projection):
    info = compose_pure2d_deck(
        tmp_path / "rog", carved.mesh, carved.tables,
        projection_wkt=projection,
        design_storm_mm_per_hr=25.0, storm_duration_hr=6.0,
        curve_number=80.0, amc_condition="normal")
    assert info["rain_on_grid"] is True
    assert info["design_storm_mm_per_hr"] == 25.0
    assert info["storm_total_mm"] == pytest.approx(150.0)
    assert info["cn_min"] == pytest.approx(80.0, rel=1e-4)  # AMC II identity

    with h5py.File(info["paths"].plan, "r") as f:
        # (1) Meteorology/Precipitation uniform-gridded group the engine reads
        assert PRECIP_PATH in f
        pg = f[PRECIP_PATH]
        assert int(pg.attrs["Enabled"]) == 1
        mode = pg.attrs["Mode"]
        assert (mode.decode() if isinstance(mode, bytes) else str(mode)) == "Gridded"
        # the 1x1 cumulative Values series the compute engine abstracts rain from
        vals = f[f"{PRECIP_PATH}/Values"]
        assert vals.shape[1] == 1                       # single uniform cell
        assert float(np.asarray(vals)[-1, 0]) == pytest.approx(150.0, rel=1e-4)  # 25*6 mm
        assert int(vals.attrs["Raster Cols"]) == 1 and int(vals.attrs["Raster Rows"]) == 1
        # the HEC DDMonYYYY-format Timestamp the compute engine parses (live-decoded)
        ts = f[f"{PRECIP_PATH}/Timestamp"]
        assert bytes(np.asarray(ts)[0]).rstrip(b"\x00").decode() == "02Jan1900 00:00:00"
        # Meteorology/Attributes index row names the Precipitation group
        att = f[f"{MET_ROOT}/Attributes"][()]
        assert any(bytes(r["Variable"]).rstrip(b"\x00") == b"Precipitation" for r in att)

        # (2) Infiltration SCS-CN layer sized 1:1 with the geometry cells/faces
        area = f[f"Geometry/2D Flow Areas/{AREA_NAME}"]
        nc = int(area["Cells Center Manning's n"].shape[0])
        nf = int(area["Faces Cell Indexes"].shape[0])
        inf = area["Infiltration"]
        assert inf["Curve Number"].shape == (nc,)
        assert inf["Abstraction Ratio"].shape == (nc,)
        assert inf["Minimum Infiltration Rate"].shape == (nc,)
        assert inf["Cell Center Classifications"].shape == (nc,)
        assert inf["Face Center Classifications"].shape == (nf,)
        assert inf["Properties"].dtype.names == ("Name", "Value")
        assert bytes(inf["Properties"][0]["Name"]).rstrip(b"\x00") \
            == b"SCS Initial Loss Reset Time"
        # infiltration attrs mirrored on the 2D-area group (the geometry cross-ref)
        assert "Infiltration Layername" in area.attrs

        # (3) a single normal-depth Outlet BC, NO inflow flow hydrograph
        bc_attrs = f["Geometry/Boundary Condition Lines/Attributes"][()]
        names = {r["Name"].decode(errors="replace").strip() for r in bc_attrs}
        assert names == {"Outlet"}
        nd = f[f"{BC_ROOT}/Normal Depths"]
        assert any("Outlet" in k for k in nd.keys())
        assert f"{BC_ROOT}/Flow Hydrographs" not in f or \
            len(f[f"{BC_ROOT}/Flow Hydrographs"].keys()) == 0

        # (4) the simulation window patched to the storm duration (6 h)
        info_grp = f["Plan Data/Plan Information"]
        end = info_grp.attrs["Simulation End Time"]
        assert (end.decode() if isinstance(end, bytes) else str(end)) == "02Jan1900 06:00:00"

    # (5) the .bNN persists the ASCII precipitation switch
    bnn = info["paths"].bnn.read_text()
    assert "Precipitation Mode=Enable" in bnn
    assert "Met BC=Precipitation|Constant Value=25" in bnn


def test_rog_amc_knob_changes_effective_cn(tmp_path, carved, projection):
    dry = compose_pure2d_deck(
        tmp_path / "dry", carved.mesh, carved.tables, projection_wkt=projection,
        design_storm_mm_per_hr=25.0, storm_duration_hr=6.0,
        curve_number=80.0, amc_condition="dry")
    wet = compose_pure2d_deck(
        tmp_path / "wet", carved.mesh, carved.tables, projection_wkt=projection,
        design_storm_mm_per_hr=25.0, storm_duration_hr=6.0,
        curve_number=80.0, amc_condition="wet")
    # dry AMC I lowers the effective CN (less runoff); wet AMC III raises it.
    assert dry["cn_min"] < 80.0 < wet["cn_min"]
