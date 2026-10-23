"""What a filled GeoClaw sheet writes, and what refuses before the solve.

Offline: Clawpack 5.14.0 (Part A's wheel in the daemon venv) authors every
.data line through its own writer; the staged check parses each file back.
Pinned: a MANDATORY keyword nothing stated refuses at the write naming it
(an unknown keyword already refuses at fill - leg 2's
``test_geoclaw_modules.py``), a STRUCT row lands as the writer's own list or
the tools' grid object, claw.data and geoclaw.data parse back equal, and the
chile2010 setrun values stated on one sheet write the staged set the worker
launches: bare staged file names where the writers abspath them, no host
path in any file.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from trid3nt_server.workflows.geoclaw.authoring.rundata import serialize
from trid3nt_server.workflows.geoclaw.authoring.staged_check import (
    check_staged_run,
)
from trid3nt_server.workflows.geoclaw.errors import GeoclawError
from trid3nt_server.workflows.geoclaw.modules import GEO
from trid3nt_server.workflows.runtime.sheet import Sheet, fill

#: chile2010's setrun as its own rundata leaves it: every attribute the
#: example assigns, final value, paths already the staged file names the
#: product states (leg 3 writes file names only; staging is legs 5-6).
#: The two None-valued rows are dropped by the fill (None states nothing)
#: and the engine reads the same default there either way.
_CHILE = {'AMR_AMR_LEVELS_MAX': 3,
 'AMR_AUX_TYPE': ['center', 'capacity', 'yleft'],
 'AMR_CLUSTERING_CUTOFF': 0.7,
 'AMR_DPRINT': False,
 'AMR_EDEBUG': False,
 'AMR_EPRINT': False,
 'AMR_FLAG2REFINE': True,
 'AMR_FLAG_RICHARDSON': False,
 'AMR_FLAG_RICHARDSON_TOL': -1.0,
 'AMR_GPRINT': False,
 'AMR_MAX1D': 60,
 'AMR_NPRINT': False,
 'AMR_PPRINT': False,
 'AMR_REFINEMENT_RATIOS_T': [2, 6],
 'AMR_REFINEMENT_RATIOS_X': [2, 6],
 'AMR_REFINEMENT_RATIOS_Y': [2, 6],
 'AMR_REGRID_BUFFER_WIDTH': 2,
 'AMR_REGRID_INTERVAL': 3,
 'AMR_RPRINT': False,
 'AMR_SPRINT': False,
 'AMR_TPRINT': True,
 'AMR_UPRINT': False,
 'AMR_VERBOSITY_REGRID': 0,
 'CLAW_BC_LOWER': [1, 1],
 'CLAW_BC_UPPER': [1, 1],
 'CLAW_CAPA_INDEX': 2,
 'CLAW_CFL_DESIRED': 0.75,
 'CLAW_CFL_MAX': 1.0,
 'CLAW_CHECKPT_INTERVAL': 1000,
 'CLAW_CHECKPT_STYLE': -4,
 'CLAW_CHECKPT_TIMES': [1000.0],
 'CLAW_DIMENSIONAL_SPLIT': 0,
 'CLAW_DT_INITIAL': 0.2,
 'CLAW_DT_MAX': 1e+99,
 'CLAW_DT_VARIABLE': True,
 'CLAW_LIMITER': [4, 4, 4],
 'CLAW_LOWER': [-120.0, -60.0],
 'CLAW_NUM_AUX': 3,
 'CLAW_NUM_CELLS': [30, 30],
 'CLAW_NUM_DIM': 2,
 'CLAW_NUM_EQN': 3,
 'CLAW_NUM_GHOST': 2,
 'CLAW_NUM_OUTPUT_TIMES': 18,
 'CLAW_NUM_WAVES': 3,
 'CLAW_ORDER': 2,
 'CLAW_OUTPUT_AUX_COMPONENTS': 'none',
 'CLAW_OUTPUT_AUX_ONLYONCE': False,
 'CLAW_OUTPUT_FORMAT': 1,
 'CLAW_OUTPUT_Q_COMPONENTS': 'all',
 'CLAW_OUTPUT_STEP_INTERVAL': None,
 'CLAW_OUTPUT_STYLE': 1,
 'CLAW_OUTPUT_T0': True,
 'CLAW_OUTPUT_TIMES': [],
 'CLAW_RESTART': False,
 'CLAW_RESTART_FILE': 'fort.chk00096',
 'CLAW_SOURCE_SPLIT': 1,
 'CLAW_STEPS_MAX': 5000,
 'CLAW_T0': 0.0,
 'CLAW_TFINAL': 32400.0,
 'CLAW_TOTAL_STEPS': None,
 'CLAW_TRANSVERSE_WAVES': 2,
 'CLAW_UPPER': [-60.0, 0.0],
 'CLAW_USE_FWAVES': True,
 'CLAW_VERBOSITY': 1,
 'DTOPO_DTOPOFILES': [[3, 'dtopo_usgs100227.tt3']],
 'DTOPO_DT_MAX_DTOPO': 0.2,
 'FLAGREGION_FLAGREGIONS': [['region1', 3, 3, 0.0, 10000.0, 1, [-77.0, -67.0, -40.0, -30.0]],
                           ['region2', 3, 3, 8000.0, 26000.0, 1, [-90.0, -80.0, -30.0, -15.0]]],
 'GAUGE_GAUGES': [[32412, -86.392, -17.975, 0.0, 10000000000.0]],
 'GEO_COORDINATE_SYSTEM': 2,
 'GEO_CORIOLIS_FORCING': False,
 'GEO_DRY_TOLERANCE': 0.001,
 'GEO_EARTH_RADIUS': 6367500.0,
 'GEO_FRICTION_DEPTH': 1000000.0,
 'GEO_FRICTION_FORCING': True,
 'GEO_GRAVITY': 9.81,
 'GEO_MANNING_COEFFICIENT': [0.025],
 'GEO_SEA_LEVEL': 0.0,
 'QINIT_QINITFILES': [],
 'QINIT_QINIT_TYPE': 0,
 'REFINEMENT_VARIABLE_DT_REFINEMENT_RATIOS': True,
 'REFINEMENT_WAVE_TOLERANCE': 0.1,
 'TOPO_TOPOFILES': [[2, 'etopo10min120W60W60S0S.asc']]}

_TOPO = "etopo10min120W60W60S0S.asc"
_DTOPO = "dtopo_usgs100227.tt3"


def _staged(rundir: Path) -> None:
    """The file-path keywords' staged files - only their names reach a .data line."""
    rundir.mkdir(parents=True, exist_ok=True)
    (rundir / _TOPO).write_bytes(b"staged topography\n")
    (rundir / _DTOPO).write_bytes(b"staged dtopography\n")


def _sheet(**over):
    return fill(GEO, **{**_CHILE, **over})


def _without(*names):
    return fill(GEO, **{k: v for k, v in _CHILE.items() if k not in names})


def _run(tmp_path: Path, sheet=None, staged=True) -> dict:
    rundir = tmp_path / "run"
    if staged:
        _staged(rundir)
    return serialize(sheet or _sheet(), rundir)


def test_a_mandatory_keyword_nothing_stated_refuses_naming_it(tmp_path):
    with pytest.raises(GeoclawError) as caught:
        _run(tmp_path, _without("CLAW_TFINAL"))

    assert caught.value.error_code == "GEOCLAW_KEYWORD_UNSTATED"
    assert "CLAW_TFINAL" in str(caught.value)


def test_an_identifier_no_data_attribute_answers_refuses_naming_it(tmp_path):
    from trid3nt_server.workflows.runtime.sheet import Provenance, Filled
    from trid3nt_contracts.common import Origin

    slot = GEO.MODULE_INPUT["CLAW_TFINAL"]
    sheet = Sheet(body=GEO, filled={"CLAW_TFINALL": Filled(
        slot=slot, value=32400.0, provenance=Provenance(Origin.USER))},
        files={}, run={})

    with pytest.raises(GeoclawError) as caught:
        serialize(sheet, tmp_path / "run")

    assert caught.value.error_code == "GEOCLAW_KEYWORD_UNKNOWN"
    assert "CLAW_TFINALL" in str(caught.value)


def test_a_struct_row_lands_as_the_writers_list(tmp_path):
    rundir = tmp_path / "run"
    _staged(rundir)
    written = _run(tmp_path)["written"]

    assert "claw.data" in written and len(written) == 16
    regions = (rundir / "flagregions.data").read_text()
    assert "'region1'" in regions and "8000.0" in regions
    assert "32412" in (rundir / "gauges.data").read_text()


def test_old_style_regions_refuse_by_name_pointing_at_flagregion(tmp_path):
    sheet = _sheet(REGION_REGIONS=[[3, 3, 0.0, 10000.0, -77, -67, -40, -30]])
    with pytest.raises(GeoclawError) as caught:
        _run(tmp_path, sheet)

    assert caught.value.error_code == "GEOCLAW_REGION_REGIONS_UNSUPPORTED"
    assert "FLAGREGION_FLAGREGIONS" in str(caught.value)


def test_the_written_claw_and_geoclaw_files_parse_back_equal(tmp_path):
    rundir = tmp_path / "run"
    _staged(rundir)
    sheet = _sheet()
    written = serialize(sheet, rundir)["written"]

    read = check_staged_run(rundir, sheet=sheet, written=written)

    assert "CLAW_TFINAL" in read["checked"]
    assert "GEO_GRAVITY" in read["checked"]
    assert len(read["checked"]) == 80
    assert read["files"] == sorted(written)


def test_the_chile_staged_set_carries_no_host_path(tmp_path):
    rundir = tmp_path / "run"
    _staged(rundir)
    written = _run(tmp_path)["written"]
    texts = {name: (rundir / name).read_text() for name in written}

    assert all("/home" not in text for text in texts.values())
    assert f"'{_TOPO}'" in texts["topo.data"]
    assert f"'{_DTOPO}'" in texts["dtopo.data"]


def test_a_value_that_never_reached_the_file_refuses_naming_the_keyword(tmp_path):
    rundir = tmp_path / "run"
    _staged(rundir)
    written = _run(tmp_path)["written"]
    claw = rundir / "claw.data"
    claw.write_text(re.sub(r"^\S+\s+=: tfinal\s*$", "123.0 =: tfinal",
                           claw.read_text(), flags=re.M))

    sheet = _sheet()
    with pytest.raises(GeoclawError) as caught:
        check_staged_run(rundir, sheet=sheet, written=written)

    assert caught.value.error_code == "GEOCLAW_STAGED_MISMATCH"
    assert "CLAW_TFINAL" in str(caught.value)


def test_a_file_the_record_names_nobody_staged_refuses_naming_the_keyword(tmp_path):
    rundir = tmp_path / "run"
    _staged(rundir)
    written = _run(tmp_path)["written"]
    (rundir / _TOPO).unlink()

    sheet = _sheet()
    with pytest.raises(GeoclawError) as caught:
        check_staged_run(rundir, sheet=sheet, written=written)

    assert caught.value.error_code == "GEOCLAW_STAGED_FILE_MISSING"
    assert "TOPO_TOPOFILES" in str(caught.value)


def test_a_written_file_that_is_gone_refuses_by_name(tmp_path):
    rundir = tmp_path / "run"
    _staged(rundir)
    written = _run(tmp_path)["written"]
    (rundir / "amr.data").unlink()

    sheet = _sheet()
    with pytest.raises(GeoclawError) as caught:
        check_staged_run(rundir, sheet=sheet, written=written)

    assert caught.value.error_code == "GEOCLAW_STAGED_FILE_MISSING"
    assert "amr.data" in str(caught.value)


def _outputs_sheet():
    return fill(GEO, CLAW_NUM_OUTPUT_TIMES=2, CLAW_TFINAL=3600.0,
                FGOUT_FGOUT_GRIDS=[[1, 1, 5, 0.0, 3600.0, 2, "ascii",
                                    -115.0, -70.0, -55.0, -10.0, 200, 250]])


def test_an_fgout_grid_record_writes_and_reads_back(tmp_path):
    rundir = tmp_path / "run"
    sheet = _outputs_sheet()
    written = serialize(sheet, rundir)["written"]

    text = (rundir / "fgout_grids.data").read_text()
    assert "1                           # fgno" in text
    assert "FGOUT_FGOUT_GRIDS" in check_staged_run(
        rundir, sheet=sheet, written=written)["checked"]


def test_a_flagregion_record_writes_as_the_tools_object(tmp_path):
    rundir = tmp_path / "run"
    sheet = fill(GEO, CLAW_NUM_OUTPUT_TIMES=2, CLAW_TFINAL=3600.0,
                 FLAGREGION_FLAGREGIONS=[["coast", 2, 3, 0.0, 10000.0, 1,
                                          [-80.0, -70.0, -35.0, -30.0]]])
    written = serialize(sheet, rundir)["written"]

    text = (rundir / "flagregions.data").read_text()
    assert "'coast'" in text
    assert "FLAGREGION_FLAGREGIONS" in check_staged_run(
        rundir, sheet=sheet, written=written)["checked"]


def test_an_fgmax_record_whose_geometry_the_writes_never_take_refuses(tmp_path):
    """The dictionary's record shape carries no x1..nx,ny the writer reads."""
    sheet = fill(GEO, CLAW_NUM_OUTPUT_TIMES=2, CLAW_TFINAL=3600.0,
                 FGMAX_FGMAX_GRIDS=[[1, 10.0, 1.0e10, 60.0, 3, 1.0e-2, 0, 2,
                                     [[-100.0, -30.0], [-90.0, -20.0]]]])

    with pytest.raises(GeoclawError) as caught:
        serialize(sheet, tmp_path / "run")

    assert caught.value.error_code == "GEOCLAW_AUTHORING_WRITE"


_FGMAX_GRID = [[1, 10.0, 1.0e10, 60.0, 3, 1.0e-2, 0, 2,
                {"x1": -119.91666667, "y1": -59.91666667, "x2": -60.08333333, "y2": -0.08333333,
                 "nx": 360, "ny": 360}]]


def test_an_fgmax_grid_states_its_geometry_by_the_tools_names_and_reads_back(tmp_path):
    rundir = tmp_path / "run"
    sheet = fill(GEO, CLAW_NUM_OUTPUT_TIMES=2, CLAW_TFINAL=3600.0, FGMAX_NUM_FGMAX_VAL=2,
                 FGMAX_FGMAX_GRIDS=_FGMAX_GRID)
    written = serialize(sheet, rundir)["written"]

    text = (rundir / "fgmax_grids.data").read_text()
    assert "360  360" in text and "# x1, y1" in text and "# point_style" in text
    assert "FGMAX_FGMAX_GRIDS" in check_staged_run(
        rundir, sheet=sheet, written=written)["checked"]


def test_an_fgmax_geometry_that_did_not_reach_the_file_refuses_by_name(tmp_path):
    rundir = tmp_path / "run"
    sheet = fill(GEO, CLAW_NUM_OUTPUT_TIMES=2, CLAW_TFINAL=3600.0, FGMAX_FGMAX_GRIDS=_FGMAX_GRID)
    written = serialize(sheet, rundir)["written"]
    path = rundir / "fgmax_grids.data"
    path.write_text(path.read_text().replace("360  360", "180  360"))

    with pytest.raises(GeoclawError) as caught:
        check_staged_run(rundir, sheet=sheet, written=written)

    assert caught.value.error_code == "GEOCLAW_STAGED_MISMATCH"
    assert "FGMAX_FGMAX_GRIDS" in str(caught.value)
