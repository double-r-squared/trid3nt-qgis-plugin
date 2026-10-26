"""The GeoClaw grid recipe on the mesh front.

Offline. Pinned: chile2010's grid stated as a recipe yields the grid keywords its
own setrun states and writes the same .data set byte for byte; the mesh gate's
card names the finest cell and the cell count per level; a topofile that does not
cover the level-1 box refuses naming both grids, under both header registrations;
a flagregion above the levels the ratios give and a grid keyword stated beside the
recipe refuse by name.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

from trid3nt_server.inputs.gate import pending
from trid3nt_server.render import pipeline_emitter as pe
from trid3nt_server.tools.mesh import gate as mesh_gate
from trid3nt_server.tools.mesh.meshers import MeshToolError
from trid3nt_server.tools.mesh.session import MeshSession
from trid3nt_server.tools.mesh.tool import mesh_op, tool
from trid3nt_server.workflows.geoclaw.authoring import (
    check_topofile,
    grid_keywords,
)
from trid3nt_server.workflows.geoclaw.authoring.rundata import serialize
from trid3nt_server.workflows.geoclaw.errors import GeoclawError
from trid3nt_server.workflows.geoclaw.modules import GEO
from trid3nt_server.workflows.runtime.sheet import fill

_AUTHORING = Path(__file__).with_name("test_geoclaw_authoring.py")
_spec = importlib.util.spec_from_file_location("geoclaw_authoring_sheet", _AUTHORING)
_sheet_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_sheet_module)

#: chile2010's level-1 box, and the metre edge of its 2 degree cell along a meridian.
_CHILE_BOX = (-120.0, -60.0, -60.0, 0.0)
_CHILE_EDGE_M = 2.0 * 111_320.0
_COAST = dict(name="coast", minlevel=2, maxlevel=3, t1=0.0, t2=10000.0,
              extent=(-77.0, -40.0, -67.0, -30.0))
_GRID_KEYS = ("CLAW_LOWER", "CLAW_UPPER", "CLAW_NUM_CELLS", "AMR_AMR_LEVELS_MAX",
              "AMR_REFINEMENT_RATIOS_X", "AMR_REFINEMENT_RATIOS_Y",
              "AMR_REFINEMENT_RATIOS_T")


def _recipe(*ops, box=_CHILE_BOX, edge=_CHILE_EDGE_M, ratios=(2, 6)):
    return tool.build_mesh(mesher="claw_grid", extent=box, resolution_m=edge,
                           build={"refinement_ratios": list(ratios)},
                           ops=list(ops))


_CHILE_REGIONS = (
    mesh_op("flagregion", name="region1", minlevel=3, maxlevel=3, t1=0.0, t2=10000.0,
            extent=(-77.0, -40.0, -67.0, -30.0)),
    mesh_op("flagregion", name="region2", minlevel=3, maxlevel=3, t1=8000.0, t2=26000.0,
            extent=(-90.0, -30.0, -80.0, -15.0)))


def _accepted(tmp_path, *ops, **over):
    return MeshSession(_recipe(*ops, **over), workdir=tmp_path).accept()


def _stage(rundir: Path) -> None:
    rundir.mkdir(parents=True, exist_ok=True)
    (rundir / _sheet_module._TOPO).write_bytes(b"staged topography\n")
    (rundir / _sheet_module._DTOPO).write_bytes(b"staged dtopography\n")


def test_chile_grid_as_a_recipe_states_the_setrun_grid_keywords(tmp_path):
    keywords = grid_keywords(_accepted(tmp_path, *_CHILE_REGIONS))

    for name in (*_GRID_KEYS, "FLAGREGION_FLAGREGIONS"):
        assert keywords[name] == _sheet_module._CHILE[name], name


def test_chile_with_the_recipes_grid_writes_the_same_data_files(tmp_path):
    grid = grid_keywords(_accepted(tmp_path, *_CHILE_REGIONS))
    own = {k: v for k, v in _sheet_module._CHILE.items()
           if k not in (*_GRID_KEYS, "FLAGREGION_FLAGREGIONS")}
    original, recipe = tmp_path / "original", tmp_path / "recipe"
    _stage(original)
    _stage(recipe)

    wrote = serialize(fill(GEO, **_sheet_module._CHILE), original)["written"]
    serialize(fill(GEO, **own, **grid), recipe)

    assert len(wrote) == 16
    assert all((original / name).read_bytes() == (recipe / name).read_bytes()
               for name in wrote)


def test_old_style_regions_stated_beside_a_recipe_refuse_pointing_at_flagregion(tmp_path):
    with pytest.raises(GeoclawError) as caught:
        grid_keywords(_accepted(tmp_path), {"REGION_REGIONS"})

    assert caught.value.error_code == "GEOCLAW_REGION_REGIONS_UNSUPPORTED"
    assert "FLAGREGION_FLAGREGIONS" in str(caught.value)


def test_a_flagregion_op_lands_as_the_flagregion_record(tmp_path):
    art = _accepted(tmp_path, mesh_op("flagregion", **_COAST))

    record = grid_keywords(art)["FLAGREGION_FLAGREGIONS"]

    assert record == [["coast", 2, 3, 0.0, 10000.0, 1,
                       [-77.0, -67.0, -40.0, -30.0]]]
    wrote = tmp_path / "run"
    serialize(fill(GEO, CLAW_NUM_OUTPUT_TIMES=2, CLAW_TFINAL=3600.0,
                   FLAGREGION_FLAGREGIONS=record), wrote)
    assert "'coast'" in (wrote / "flagregions.data").read_text()


@pytest.fixture(autouse=True)
def _no_mesh_at_the_gate(monkeypatch):
    monkeypatch.setattr(mesh_gate, "_AT_GATE", None)


class _Emitter:
    session_id = "sess-claw-grid"

    def __init__(self):
        self.sent = []

    async def send_envelope(self, message_type, payload):
        self.sent.append(payload)

    async def add_loaded_layer(self, layer):
        pass

    async def emit_map_command(self, name, payload):
        pass


async def _proceed():
    for _ in range(1000):
        fresh = [fut for _wid, (_s, fut) in pending._PENDING_CONFIRMATIONS.items()
                 if not fut.done()]
        if fresh:
            from trid3nt_contracts.payload_warning import (
                PayloadConfirmationEnvelopePayload,
            )

            wid = next(iter(pending._PENDING_CONFIRMATIONS))
            fresh[0].set_result(PayloadConfirmationEnvelopePayload(
                warning_id=wid, decision="proceed", revised_args=None))
            return
        await asyncio.sleep(0.005)
    raise AssertionError("no gate card appeared")


@pytest.mark.asyncio
async def test_the_gate_card_names_the_finest_cell_and_the_cells_per_level(
        tmp_path, monkeypatch):
    fake = _Emitter()
    monkeypatch.setattr(pe, "current_emitter", lambda: fake)
    session = MeshSession(_recipe(mesh_op("flagregion", **_COAST)), workdir=tmp_path)
    driver = asyncio.create_task(_proceed())

    await mesh_gate.gate_mesh_build(session, tool_name="geoclaw_probe",
                                    input_mode="user_gated")
    await driver

    envelope = fake.sent[0]
    rows = {row.name: row.value for row in envelope.param_sheet.rows}
    assert "finest cell 18553.3 m meridian" in envelope.recommendation
    assert rows["finest_cell"] == (
        "18553.3 m meridian x 16067.7 m parallel = 600.00 arc-seconds")
    assert rows["grid"].startswith("level-1 grid 30 x 30 cells")
    assert rows["level[2]"] == "100 cells forced, up to 100 allowed"
    assert rows["level[3]"] == "0 cells forced, up to 3600 allowed"
    assert rows["wall_estimate"].startswith("about ") and "per simulated hour" in rows["wall_estimate"]


def _copalis_ops():
    ops = [mesh_op("flagregion", name="a", minlevel=1, maxlevel=2, t1=0.0, t2=1e9, extent=(-128.5, 45.0, -123.5, 49.0)),
           mesh_op("flagregion", name="b", minlevel=4, maxlevel=4, t1=0.0, t2=10.0, extent=(-126.6, 45.5, -124.0, 48.5)),
           mesh_op("flagregion", name="c", minlevel=3, maxlevel=4, t1=10.0, t2=1e9, extent=(-126.6, 46.27, -124.0, 47.68)),
           mesh_op("flagregion", name="d", minlevel=7, maxlevel=7, t1=0.0, t2=1e9, extent=(-124.195, 47.11, -124.155, 47.145))]
    return ops


def test_the_wall_estimate_reproduces_the_measured_copalis_runs(tmp_path):
    ops = _copalis_ops()
    mesh = MeshSession(_recipe(*ops, box=(-128.5, 45.0, -123.5, 49.0), edge=4.0 / 60.0 * 111_320.0,
                               ratios=(2, 5, 2, 2, 2, 3)), workdir=tmp_path).mesh
    minutes = int(dict(mesh.meta["card_rows"])["wall_estimate"].split()[1])
    # the full run took about 50 minutes for its simulated hour (3600 s)
    assert 47 <= minutes <= 53


# GeoClaw's reach rule (topo_module.f90: the overlap of the topo NODE extent
# with the domain must be within 1e-2 of the domain's area, else "topo arrays do
# not cover domain"). A corner header's first node is half a cell in from xll; a
# centre header's first node IS xll. Domain (-12,12)^2; cell 0.5; the 49-node
# centre header at xll -12.25 is the engine's own measured stop (overlap area
# 564.0625 of 576).
_BOX = (-12.0, -12.0, 12.0, 12.0)


def _header(registration, ncols, xll=-12.25):
    return {"ncols": ncols, "nrows": ncols, "cellsize": 0.5, "xll": xll,
            "yll": xll, "registration": registration}


def _box_art(tmp_path):
    return _accepted(tmp_path, box=_BOX, edge=0.5 * 111_320.0, ratios=(2,))


def test_a_centre_header_short_of_the_box_refuses_naming_both_grids(tmp_path):
    with pytest.raises(GeoclawError) as caught:
        check_topofile(_box_art(tmp_path), _header("centre", 49))

    assert caught.value.error_code == "GEOCLAW_TOPO_COVERAGE"
    text = str(caught.value)
    assert "564.062" in text and "x -12.25..11.75" in text
    assert "x -12..12" in text and "(-12.0, -12.0, 11.75, 11.75)" in text


def test_the_same_numbers_under_a_corner_header_cover_the_box(tmp_path):
    check_topofile(_box_art(tmp_path), _header("corner", 49))
    check_topofile(_box_art(tmp_path), _header("centre", 50))


def test_a_corner_header_short_of_the_box_refuses(tmp_path):
    with pytest.raises(GeoclawError) as caught:
        check_topofile(_box_art(tmp_path), _header("corner", 48))

    assert caught.value.error_code == "GEOCLAW_TOPO_COVERAGE"
    assert "corner registration" in str(caught.value)


def test_a_flagregion_above_the_levels_refuses_by_name(tmp_path):
    over = dict(_COAST, maxlevel=4)

    with pytest.raises(MeshToolError) as caught:
        _accepted(tmp_path, mesh_op("flagregion", **over))

    assert caught.value.error_code == "MESH_FLAGREGION_LEVELS"
    assert "'coast'" in str(caught.value) and "maxlevel 4" in str(caught.value)


def test_a_grid_keyword_stated_beside_the_recipe_refuses_by_name(tmp_path):
    with pytest.raises(GeoclawError) as caught:
        grid_keywords(_accepted(tmp_path), stated=["CLAW_NUM_CELLS", "CLAW_TFINAL"])

    assert caught.value.error_code == "GEOCLAW_GRID_KEYWORD_STATED"
    assert "CLAW_NUM_CELLS" in str(caught.value)
    assert "CLAW_TFINAL" not in str(caught.value)


def test_a_recipe_without_ratios_refuses_rather_than_defaulting(tmp_path):
    with pytest.raises(MeshToolError) as caught:
        MeshSession(tool.build_mesh(mesher="claw_grid", extent=_CHILE_BOX,
                                    resolution_m=_CHILE_EDGE_M),
                    workdir=tmp_path).accept()

    assert caught.value.error_code == "MESH_RATIOS_UNSTATED"


def _source_region(**over):
    return mesh_op("flagregion", **{**_COAST, "t2": 10.0, **over})


def test_a_region_that_follows_the_rupture_ends_with_it_and_never_before_its_own_window(tmp_path):
    art = _accepted(tmp_path, _source_region(with_rupture=True))

    late = grid_keywords(art, rupture_end=115.4)["FLAGREGION_FLAGREGIONS"][0][4]
    early = grid_keywords(art, rupture_end=0.0)["FLAGREGION_FLAGREGIONS"][0][4]
    none = grid_keywords(art)["FLAGREGION_FLAGREGIONS"][0][4]

    assert (late, early, none) == (115.4, 10.0, 10.0)


def test_a_region_that_states_its_window_without_following_keeps_it(tmp_path):
    art = _accepted(tmp_path, _source_region())

    assert grid_keywords(art, rupture_end=115.4)["FLAGREGION_FLAGREGIONS"][0][4] == 10.0


_DIAGONAL = dict(ixy=1, method=1, s=[-110.0, -70.0], lower=[-50.0, -20.0], upper=[-30.0, 0.0], filename="RuledRectangle_Diagonal.data")


def _ruled_op(**over):
    return mesh_op("flagregion", name="diag", minlevel=2, maxlevel=3, t1=0.0, t2=1e9, ruled={**_DIAGONAL, **over})


def test_a_ruled_flagregion_lands_writes_with_clawpacks_file_and_reads_back(tmp_path):
    from clawpack.amrclaw.region_tools import RuledRectangle

    from trid3nt_server.workflows.geoclaw.authoring.staged_check import check_staged_run

    record = grid_keywords(_accepted(tmp_path, _ruled_op()))["FLAGREGION_FLAGREGIONS"]
    assert record == [["diag", 2, 3, 0.0, 1e9, 2, {"ixy": 1, "method": 1, "s": [-110.0, -70.0], "lower": [-50.0, -20.0],
                                                   "upper": [-30.0, 0.0], "filename": "RuledRectangle_Diagonal.data"}]]
    rundir = tmp_path / "run"
    sheet = fill(GEO, CLAW_NUM_OUTPUT_TIMES=2, CLAW_TFINAL=3600.0, FLAGREGION_FLAGREGIONS=record)
    written = serialize(sheet, rundir)["written"]

    assert "'RuledRectangle_Diagonal.data' =: spatial_region_file" in (rundir / "flagregions.data").read_text()
    assert RuledRectangle(str(rundir / "RuledRectangle_Diagonal.data")).upper.tolist() == [-30.0, 0.0]
    assert "FLAGREGION_FLAGREGIONS" in check_staged_run(rundir, sheet=sheet, written=written)["checked"]


def test_a_ruled_flagregion_is_counted_by_its_polygon_not_its_bounding_box(tmp_path):
    ruled = _accepted(tmp_path, _ruled_op())
    boxed = _accepted(tmp_path, mesh_op("flagregion", name="diag", minlevel=2, maxlevel=3, t1=0.0, t2=1e9,
                                        extent=(-110.0, -50.0, -70.0, 0.0)))
    got = ruled.probes["claw_grid"]["cells_per_level"][1]["allowed"]
    bound = boxed.probes["claw_grid"]["cells_per_level"][1]["allowed"]

    assert 0 < got < bound
    assert got == pytest.approx(bound * 0.4, rel=0.05)


@pytest.mark.parametrize("over", [{"s": [-70.0, -110.0]}, {"lower": [-30.0, 1.0]}, {"ixy": 3}, {"method": 2}, {"filename": "a/b"}])
def test_a_wrong_ruled_rectangle_refuses_by_name(tmp_path, over):
    with pytest.raises(MeshToolError) as caught:
        _accepted(tmp_path, _ruled_op(**over))

    assert caught.value.error_code == "MESH_FLAGREGION_RULED"


def test_a_flagregion_states_exactly_one_of_extent_and_ruled(tmp_path):
    with pytest.raises(MeshToolError) as caught:
        _accepted(tmp_path, mesh_op("flagregion", **_COAST, ruled=_DIAGONAL))

    assert caught.value.error_code == "MESH_FLAGREGION_EXTENT"
