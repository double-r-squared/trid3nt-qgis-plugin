"""The GeoClaw template's pieces: the SIFT unit-source row, the SiftFault, FixedGrids and InitialSurface composites, the staging and the declaration.

Offline. The unit-source rows are an excerpt of NOAA PMEL's own table; every raster is SYNTHETIC and exists to verify
the plumbing only. Pinned: the SIFT row reads the named rows and refuses an unnamed or unknown one; SiftFault writes the
dtopo and grid Clawpack's own SiftFault writes for the ASCE_SIFT_Region2 slips; FixedGrids puts the fgmax and fgout
grids on the same finest-level nodes with frame 0 at t = 0; InitialSurface writes the one qinit file in the engine's
order; the template declares what its composites read; the staged case is the one the worker reads.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from trid3nt_server.tools.fetchers._router.registration import get_spec
from trid3nt_server.tools.fetchers.hazard.fetch_sift_unit_sources import hooks as sift_hooks
from trid3nt_server.tools.mesh.session import MeshSession
from trid3nt_server.tools.mesh.tool import tool
from trid3nt_server.workflows.geoclaw.authoring import grid_keywords
from trid3nt_server.workflows.geoclaw.authoring.dtopo import dtopo_axes, sift_subfaults
from trid3nt_server.workflows.geoclaw.authoring.rundata import serialize
from trid3nt_server.workflows.geoclaw.authoring.staged_check import check_staged_run
from trid3nt_server.workflows.geoclaw.errors import GeoclawError
from trid3nt_server.workflows.geoclaw.modules import GEO
from trid3nt_server.workflows.geoclaw.templates.tsunami_inundation import tsunami_inundation as template
from trid3nt_server.workflows.runtime.sheet import fill

_AUTHORING = Path(__file__).with_name("test_geoclaw_authoring.py")
_spec = importlib.util.spec_from_file_location("geoclaw_authoring_sheet", _AUTHORING)
_chile = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_chile)

_TABLE = Path(__file__).parents[1] / "fixtures" / "geoclaw" / "info_sz_excerpt.dat"

#: ASCE_SIFT_Region2: LeVeque's tutorial page "Make ASCE_SIFT_Region2 dtopo file".
_SLIPS = template._ASCE_SIFT_REGION2


def _record() -> dict:
    spec = get_spec("fetch_sift_unit_sources")
    return sift_hooks.record(spec, {"names": list(_SLIPS)}, [_TABLE.read_bytes()])


def test_the_row_reads_the_named_unit_sources_of_the_table():
    record = _record()

    assert sorted(record["unit_sources"]) == sorted(_SLIPS)
    assert record["unit_sources"]["acsza57"] == {
        "longitude": 234.9041, "latitude": 48.25964, "slip_m": 1.0, "strike": 341.0, "dip": 11.0,
        "depth_km": 12.82, "length_km": 100.0, "width_km": 50.0, "rake": 90.0}


def test_the_row_refuses_a_name_the_table_lacks_and_no_name_at_all():
    spec = get_spec("fetch_sift_unit_sources")

    with pytest.raises(Exception, match="no unit source named 'acsza9999'"):
        sift_hooks.record(spec, {"names": ["acsza9999"]}, [_TABLE.read_bytes()])
    with pytest.raises(Exception, match="states no unit source"):
        sift_hooks.build_request(spec, {"names": []})


def test_siftfault_states_the_subfaults_and_grid_clawpack_lays_for_the_same_slips():
    from clawpack.geoclaw import dtopotools

    theirs = dtopotools.SiftFault(dict(_SLIPS))
    for sub in theirs.subfaults:
        sub.longitude -= 360.0
    ours = sift_subfaults(_record()["unit_sources"], _SLIPS)

    assert len(ours) == len(theirs.subfaults) == 8
    for mine, sub in zip(ours, theirs.subfaults):
        for field in ("longitude", "latitude", "depth", "strike", "dip", "rake", "slip", "length", "width"):
            assert mine[field] == pytest.approx(getattr(sub, field), rel=1e-12), field
        assert mine["coordinate_specification"] == sub.coordinate_specification
    x, y = theirs.create_dtopo_xy(dx=10.0 / 3600.0, buffer_size=1)
    assert dtopo_axes(ours, 10.0 / 3600.0, 1) == ([float(x[0]), float(x[-1]), 1801],
                                                   [float(y[0]), float(y[-1]), 2161])


def _grid_recipe(tmp_path, *ops, box=(-128.5, 45.0, -123.5, 49.0), ratios=(2, 5, 2, 2, 2, 3)):
    edge = 4.0 / 60.0 * 111_320.0
    return MeshSession(tool.build_mesh(mesher="claw_grid", extent=box, resolution_m=edge,
                                       build={"refinement_ratios": list(ratios)}, ops=list(ops)),
                       workdir=tmp_path).accept()


_GRIDS = {"extent": SimpleNamespace(bbox=(-124.195, 47.11, -124.155, 47.145)), "interval": 120.0,
          "tstart_max": 0.0, "dt_check": 10.0, "arrival_tol": 0.2, "interp_method": 0}


def _fixed(tmp_path, **over):
    art = _grid_recipe(tmp_path)
    run = {"mesh": {"artifact": art}, "CLAW_TFINAL": 3600.0}
    return GEO.COMPOSITES["FixedGrids"].apply({**_GRIDS, **over}, run)[0]


def test_fixedgrids_puts_fgmax_and_fgout_on_the_finest_cells_of_the_tutorials_coast(tmp_path):
    slots = _fixed(tmp_path)
    fgmax, fgout = slots["FGMAX_FGMAX_GRIDS"][0], slots["FGOUT_FGOUT_GRIDS"][0]
    points = fgmax[8]

    # the tutorial's own fgout grid: 144 x 126 cells of 1 arcsecond over [-124.195, -124.155] x [47.11, 47.145]
    assert (fgout[11], fgout[12]) == (144, 126) == (points["nx"], points["ny"])
    assert fgout[7:11] == pytest.approx([-124.195, -124.155, 47.11, 47.145], abs=1e-9)
    assert points["x1"] == pytest.approx(fgout[7] + 0.5 / 3600.0, abs=1e-9)
    assert points["x2"] == pytest.approx(fgout[8] - 0.5 / 3600.0, abs=1e-9)
    assert fgmax[4] == 7 and fgmax[1] == 0.0
    # frame 0 is the initial sea: the first fgout frame is at t = 0, a frame every 120 s to CLAW_TFINAL
    assert (fgout[2], fgout[3], fgout[4]) == (31, 0.0, 3600.0)


def test_fixedgrids_grows_a_coast_outward_to_whole_finest_cells(tmp_path):
    slots = _fixed(tmp_path, extent=SimpleNamespace(bbox=(-124.1951, 47.1101, -124.1549, 47.1449)))
    fgout = slots["FGOUT_FGOUT_GRIDS"][0]

    assert fgout[7] <= -124.1951 and fgout[8] >= -124.1549 and fgout[9] <= 47.1101 and fgout[10] >= 47.1449
    assert (fgout[8] - fgout[7]) * 3600.0 == pytest.approx(fgout[11], abs=1e-6)


def test_fixedgrids_beside_the_grid_keywords_or_without_a_mesh_refuses(tmp_path):
    art = _grid_recipe(tmp_path)
    with pytest.raises(GeoclawError) as stated:
        GEO.COMPOSITES["FixedGrids"].apply(
            _GRIDS, {"mesh": {"artifact": art}, "CLAW_TFINAL": 3600.0, "FGOUT_FGOUT_GRIDS": []})
    with pytest.raises(GeoclawError) as unmeshed:
        GEO.COMPOSITES["FixedGrids"].apply(_GRIDS, {"CLAW_TFINAL": 3600.0})

    assert stated.value.error_code == "GEOCLAW_GRIDS_STATED"
    assert unmeshed.value.error_code == "GEOCLAW_GRIDS_UNSTATED"


def _surface(path: Path) -> str:
    """SYNTHETIC: a 4 x 3 raster whose value is its own column."""
    with rasterio.open(path, "w", driver="GTiff", height=3, width=4, count=1, dtype="float32",
                       crs="EPSG:4326", nodata=-9999.0, transform=from_origin(-1.0, 1.0, 0.5, 0.5)) as dst:
        dst.write(np.tile(np.arange(4, dtype="float32"), (3, 1)) + np.arange(3, 0, -1, dtype="float32")[:, None] * 10, 1)
    return str(path)


_NO_FILES = ("TOPO_TOPOFILES", "DTOPO_DTOPOFILES", "QINIT_QINIT_TYPE", "QINIT_QINITFILES")


def _sheet_with(tmp_path, **over):
    run = {"vertical_frame": "NAVD88", "initial_surface": _surface(tmp_path / "s.tif")}
    base = {k: v for k, v in _chile._CHILE.items() if k not in _NO_FILES}
    return fill(GEO, produced=run, **base, InitialSurface={"surface": "initial_surface"}, **over)


def test_initialsurface_writes_the_one_qinit_file_in_the_engines_order(tmp_path):
    rundir = tmp_path / "run"
    sheet = _sheet_with(tmp_path)
    written = serialize(sheet, rundir)
    lines = (rundir / "qinit_01.xyz").read_text().splitlines()

    assert sheet.filled["QINIT_QINIT_TYPE"].value == 4
    assert sheet.filled["QINIT_QINITFILES"].value == [["qinit_01.xyz"]]
    assert len(lines) == 12
    first, second, fifth = (line.split() for line in (lines[0], lines[1], lines[4]))
    # north-west corner first, x increasing along a row, then the next row south
    assert (float(first[0]), float(first[1]), float(first[2])) == (-0.75, 0.75, 30.0)
    assert float(second[0]) == -0.25 and float(second[1]) == 0.75
    assert float(fifth[1]) == 0.25 and float(fifth[2]) == 20.0
    assert "qinit_01.xyz" in (rundir / "qinit.data").read_text()
    assert "QINIT_QINITFILES" in check_staged_run(rundir, sheet=sheet, written=written["written"])["checked"]


def test_an_unstated_surface_states_nothing_and_a_qinit_keyword_beside_it_refuses(tmp_path):
    base = {k: v for k, v in _chile._CHILE.items() if k not in _NO_FILES}
    sheet = fill(GEO, produced={"vertical_frame": "NAVD88"}, **base, InitialSurface={"surface": "initial_surface"})

    assert "QINIT_QINITFILES" not in sheet.filled and not sheet.files
    with pytest.raises(GeoclawError) as caught:
        _sheet_with(tmp_path, QINIT_QINIT_TYPE=4)
    assert caught.value.error_code == "GEOCLAW_SURFACE_QINIT_STATED"


def test_siftfault_with_no_slip_writes_no_dtopo_and_an_unstated_value_refuses_by_name():
    nothing = {"unit_sources": None, "slips": {}, "spacing": 0.003, "buffer": 1.0, "times": [0.0]}
    base = {k: v for k, v in _chile._CHILE.items() if k not in ("DTOPO_DTOPOFILES",)}

    assert fill(GEO, **base, SiftFault=nothing).filled.get("DTOPO_DTOPOFILES") is None
    with pytest.raises(GeoclawError) as caught:
        fill(GEO, **base, SiftFault={**nothing, "slips": {"acsza57": 1.0}})
    assert caught.value.error_code == "GEOCLAW_SIFT_UNSTATED" and "unit_sources" in str(caught.value)


def test_a_flagregion_label_is_a_word_and_an_extent_word_names_an_input():
    from trid3nt_server.tools.mesh.recipe import input_names

    assert sorted(input_names(template.MESH)) == [
        "approach", "domain", "extent", "extent", "level1_cell_m", "source"]


def test_the_template_declares_every_input_its_statements_read():
    from trid3nt_server.tools import TOOL_REGISTRY

    assert "geoclaw_tsunami_inundation" in TOOL_REGISTRY
    steering = template.STEERING
    declared = {name for name in vars(template.INPUTS) if not name.startswith("_")}
    read = set(steering.named()) - {"deep_bed_offset", "sift_unit_sources"}

    assert read <= declared, read - declared
    assert steering.ASSERTED["GEO_COORDINATE_SYSTEM"] == 2
    assert "GEO_SEA_LEVEL" not in steering.ASSERTED


def test_the_staged_case_is_the_one_section_the_worker_reads(tmp_path):
    from trid3nt_server.workflows.geoclaw import workflow

    captured = {}

    async def fake_stage_run(rundir, run_tag, **kwargs):
        captured.update(kwargs)
        return {"case": kwargs["case"], "manifest_uri": "s3://x"}

    base = {k: v for k, v in _chile._CHILE.items() if k not in ("GAUGE_GAUGES",)}
    sheet = fill(GEO, **base, FGMAX_FGMAX_GRIDS=[[1, 0.0, 1e10, 10.0, 3, 0.2, 0, 2, {"x1": 0.0, "y1": 0.0, "x2": 1.0, "y2": 1.0, "nx": 3, "ny": 3}]],
                 FGOUT_FGOUT_GRIDS=[[1, 1, 5, 0.0, 600.0, 2, "binary32", 0.0, 1.0, 0.0, 1.0, 3, 3]], GAUGE_GAUGES=[[7, 0.5, 0.5, 0.0, 1e10]])
    results = workflow._results(sheet)

    assert results == ["fgmax0001.txt", "gauge00007.txt", "fgout0001.b0005"]
    from trid3nt_server.workflows.solver import staging

    original = staging.stage_run
    staging.stage_run = fake_stage_run
    try:
        async def go():
            workflow.serialize = lambda sheet, rundir: {"written": []}
            return await workflow.stage_sheet(sheet=sheet, run_tag="tagtagtag", prefix="geoclaw", name="x")
        original_serialize = workflow.serialize
        try:
            staged = asyncio.run(go())
        finally:
            workflow.serialize = original_serialize
    finally:
        staging.stage_run = original

    assert captured["case"] == {"results": results} and staged["name"] == "x"
    assert json.dumps(captured["case"])


def test_the_workflow_answers_for_its_own_fetch_rows_and_the_modules_keywords():
    from trid3nt_server.tools import TOOL_REGISTRY

    workflow = TOOL_REGISTRY["geoclaw_tsunami_inundation"].fn.workflow

    assert workflow.input("sea_level_offset").is_row and workflow.input("sea_level_offset").producer.runner == "fetch_vertical_datum_offset"
    assert workflow.input("sift_unit_sources_fetch").producer.runner == "fetch_sift_unit_sources"
    assert workflow.input("CLAW_TFINAL").name == "CLAW_TFINAL"
    with pytest.raises(Exception):
        workflow.input("no_such_thing")


def test_a_run_stating_no_moment_counts_its_seconds_from_the_solve_start_and_says_so(monkeypatch):
    from trid3nt_server.inputs.run_notes import bind_notes, drain_notes
    from trid3nt_server.workflows.geoclaw import workflow as gw

    async def _box(*_args, **_kwargs):
        return {"run_id": "RUN1", "started_at": "2026-10-12T08:00:00+00:00"}

    monkeypatch.setattr(gw, "run_sheet_", _box)
    sheet = SimpleNamespace(module="geoclaw")

    def solved(**stated):
        token = bind_notes()
        handle = asyncio.run(gw.run_sheet(sheet=sheet, staged={}, mesh={}, name="x", **stated))
        return handle["reference_time"], drain_notes(token)

    assert solved(moment="2000-01-01T00:00:00Z") == ("2000-01-01T00:00:00Z", [])
    stamped, notes = solved()
    assert stamped == "2026-10-12T08:00:00+00:00" and len(notes) == 1 and "defaulted" in notes[0]
