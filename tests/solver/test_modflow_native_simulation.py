from __future__ import annotations

import zipfile

import flopy
import pytest

from trid3nt.workflows.modflow.authoring import native
from trid3nt.workflows.modflow.errors import ModflowError


def test_native_simulation_retains_structured_grid_values_and_external_files(tmp_path):
    source = tmp_path / "source"
    (source / "properties").mkdir(parents=True)
    sim = flopy.mf6.MFSimulation(sim_ws=source, verbosity_level=0)
    flopy.mf6.ModflowTdis(sim, nper=1, perioddata=[(10.0, 2, 1.0)])
    flow = flopy.mf6.ModflowGwf(sim, modelname="flow")
    flopy.mf6.ModflowGwfdis(flow, nlay=1, nrow=1, ncol=2,
                            delr=20, delc=30, top=8, botm=1)
    flopy.mf6.ModflowGwfic(flow, strt=6)
    flopy.mf6.ModflowGwfnpf(flow, k={"filename": "properties/k.dat", "data": [[4, 7]]})
    flopy.mf6.ModflowIms(sim)
    sim.write_simulation()

    model = native.load(source / "mfsim.nam")
    values = model.values
    assert values["flow"]["flow.dis"]["DIS_NCOL"] == 2
    assert values["flow"]["flow.npf"]["NPF_K"] == [[[4.0, 7.0]]]
    assert "DIS_IDOMAIN" not in values["flow"]["flow.dis"]
    assert values[""]["sim.tdis"]["TDIS_PERIODDATA"] == [[10.0, 2, 1.0]]

    target = tmp_path / "copy"
    model.copy_to(target)
    assert (target / "properties" / "k.dat").read_bytes() == (source / "properties" / "k.dat").read_bytes()
    assert native.load(target / "mfsim.nam").values == values
    assert len(native.mapping_grid(target)["cells"]) == 2
    edited = native.override(native.load(target / "mfsim.nam"), {"NPF_K": 9.0}, {})
    assert edited.values["flow"]["flow.npf"]["NPF_K"] == [[[9.0, 9.0]]]
    assert edited.values["flow"]["flow.dis"] == values["flow"]["flow.dis"]
    assert native.load(source / "mfsim.nam").values == values


def test_native_package_cannot_reference_a_file_outside_its_archive(tmp_path):
    archive = tmp_path / "import.zip"
    with zipfile.ZipFile(archive, "w") as packed:
        packed.writestr("mfsim.nam", "BEGIN TIMING\nTDIS6 ../outside.tdis\nEND TIMING\n")
    with pytest.raises(ModflowError) as error:
        native.unpack(archive, tmp_path / "unpacked")
    assert error.value.error_code == "MODFLOW_NATIVE_INPUT_OUTSIDE"


def test_from_zero_authors_the_stated_grid_clock_and_flow_packages(tmp_path):
    stated = {
        "TDIS_NPER": 1, "TDIS_PERIODDATA": [(10.0, 2, 1.0)],
        "IMS_OUTER_DVCLOSE": 1e-6, "IMS_OUTER_MAXIMUM": 100,
        "IMS_INNER_DVCLOSE": 1e-6, "IMS_INNER_MAXIMUM": 100,
        "IMS_RCLOSERECORD": [(1e-6, "strict")], "IMS_LINEAR_ACCELERATION": "BICGSTAB",
        "DIS_NLAY": 1, "DIS_NROW": 1, "DIS_NCOL": 2,
        "DIS_DELR": 20, "DIS_DELC": 30,
        "DIS_TOP": 8, "DIS_BOTM": 1, "IC_STRT": 6,
        "NPF_ICELLTYPE": 0, "NPF_K": [[4, 7]],
    }
    simulation = native.author(tmp_path, stated, {})
    assert simulation.values["gwf"]["gwf.npf"]["NPF_K"] == [[[4.0, 7.0]]]
    assert simulation.values[""]["sim.tdis"]["TDIS_PERIODDATA"] == [[10.0, 2, 1.0]]
