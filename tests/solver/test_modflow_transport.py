"""The transport leg: a gwf deck that couples a gwt body, the two-model write over one staged DISV, and the solved fixture's concentration read.

Offline over the strip the kickoff pins (p01: 1 row x 101 cells, 10 m, top 0, bottom -1, as the spike
builds it): the Transport composite's expansion and its refusals at the fill, serialize writing both
name files over the mesh's own DISV with one IMS per model, and the committed fixture's concentration
publishing as its temporal UGRID group. The strip's display is the mesher's triangles; the walk solves
its quad cells, so ``mesh_nodes`` is monkeypatched to hand them - the scaffolding of ``staged_beside``.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import netCDF4
import pytest
from pyproj import Transformer

from trid3nt_contracts.execution import LayerURI

from trid3nt_server.inputs.run_notes import bind_notes, drain_notes
from trid3nt_server.render.formats import Mesh, Published
from trid3nt_server.tools.mesh.artifact import MeshArtifact
from trid3nt_server.workflows.modflow.authoring import mesh_files as MF
from trid3nt_server.workflows.modflow.authoring import simulation as SIM
from trid3nt_server.workflows.modflow.authoring.staged_check import check_staged_run
from trid3nt_server.workflows.modflow.authoring.simulation import serialize
from trid3nt_server.workflows.modflow.errors import ModflowError
from trid3nt_server.workflows.modflow.modules import GWF, GWT
from trid3nt_server.workflows.modflow.modules.outputs import Solved
from trid3nt_server.workflows.runtime.module import SlotRefused
from trid3nt_server.workflows.runtime.sheet import fill

#: p01, as the spike's reference grid states it: one row of 101 cells, 10 m each, top 0 bottom -1.
_NCOL = 101
_K11 = 1.0
_PRSITY = 0.25
_V = 0.24
_H1 = _V * _PRSITY * 1000.0
_C0 = 1.0
_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "modflow" / "strip_transport"
_TO_LONLAT = Transformer.from_crs(32617, 4326, always_xy=True).transform


def _strip(tmp_path: Path) -> Path:
    """The strip's display: two triangles per cell, the mesher's own shape."""
    rows = ["MESH2D"]
    cell = 0
    for c in range(_NCOL):
        a = c + 1
        cell += 1
        rows.append(f"E3T {cell} {a} {a + 1} {_NCOL + 2 + c}")
        cell += 1
        rows.append(f"E3T {cell} {_NCOL + 2 + c} {_NCOL + 2 + c + 1} {a + 1}")
    node = 0
    for y in (0.0, 1.0):
        for x in (i * 10.0 for i in range(_NCOL + 1)):
            node += 1
            rows.append(f"ND {node} {x} {y} 0.0")
    path = tmp_path / "strip.2dm"
    path.write_text("\n".join(rows) + "\n")
    return path


def _quads() -> tuple:
    """The cells the walk stages: one quad per strip cell, joined by the display's splits."""
    xs = [i * 10.0 for i in range(_NCOL + 1)]
    points = [[x, y] for y in (0.0, 1.0) for x in xs]
    cells = [[c, _NCOL + 1 + c, _NCOL + 1 + c + 1, c + 1] for c in range(_NCOL)]
    return (points, cells, None, None)


def _inside() -> list[float]:
    """A lon/lat that lands on the first cell of the strip."""
    lon, lat = _TO_LONLAT(5.0, 0.5)
    return [lon, lat]


def _sheet(transport: dict | None = None, **over):
    """The p01 flow through this leg's fill, with a coupled transport body if stated."""
    stated = {
        "NAM_SAVE_FLOWS": True,
        "DISV_NLAY": 1, "DISV_TOP": 0.0, "DISV_BOTM": [[-1.0] * _NCOL],
        "DISV_LENGTH_UNITS": "meters",
        "NPF_K": _K11, "NPF_K33": _K11, "NPF_ICELLTYPE": 1,
        "NPF_SAVE_SPECIFIC_DISCHARGE": True,
        "IC_STRT": [[_H1] + [0.0] * (_NCOL - 1)],
        "TDIS_NPER": 1, "TDIS_TIME_UNITS": "days",
        "TDIS_PERIODDATA": [[2000.0, 100, 1.0]],
        "OC_HEAD_FILERECORD": "gwf.hds", "OC_BUDGET_FILERECORD": "gwf.cbc",
        "OC_SAVERECORD": [("HEAD", "LAST"), ("BUDGET", "LAST")],
        "CHD_MAXBOUND": 2,
        "CHD_STRESS_PERIOD_DATA": {0: [((0, 0), _H1), ((0, _NCOL - 1), 0.0)]},
        "IMS_OUTER_DVCLOSE": 1.0e-6, "IMS_OUTER_MAXIMUM": 100,
        "IMS_INNER_MAXIMUM": 300, "IMS_INNER_DVCLOSE": 1.0e-6,
        "IMS_RCLOSERECORD": [1.0e-6, "default"],
        "IMS_LINEAR_ACCELERATION": "CG"}
    if transport is not None:
        stated["Transport"] = transport
    stated.update(over)
    return fill(GWF, **{k: v for k, v in stated.items() if v is not None})


def _transport(**over) -> dict:
    """The gwt body's stated slots: the transport of p01, CNC at the strip's first cell."""
    slots = {
        "NAM_SAVE_FLOWS": True,
        "IC_STRT": [[_C0] + [0.0] * (_NCOL - 1)],
        "MST_POROSITY": _PRSITY, "ADV_SCHEME": "upstream",
        "OC_CONCENTRATION_FILERECORD": "gwt.ucn",
        "OC_BUDGET_FILERECORD": "gwt.cbc",
        "OC_SAVERECORD": [("CONCENTRATION", "LAST"), ("BUDGET", "LAST")],
        "IMS_OUTER_DVCLOSE": 1.0e-6, "IMS_OUTER_MAXIMUM": 100,
        "IMS_INNER_MAXIMUM": 300, "IMS_INNER_DVCLOSE": 1.0e-6,
        "IMS_RCLOSERECORD": [1.0e-6, "default"],
        "IMS_LINEAR_ACCELERATION": "BICGSTAB",
        "CNC_STRESS_PERIOD_DATA": {
            "geometry": {"type": "Point", "coordinates": _inside()},
            "layer": 1, "concentration": _C0}}
    slots.update(over)
    return {"slots": {k: v for k, v in slots.items() if v is not None}}


@pytest.fixture
def strip(monkeypatch, tmp_path):
    """The strip mesh handed the authoring, its quad cells monkeypatched over the display."""
    display = _strip(tmp_path)
    art = MeshArtifact(
        mesh_id="S01", name="strip", mode="om2d", display_uri=str(display),
        crs_authid="EPSG:32617", has_bathymetry=False, node_count=2 * (_NCOL + 1),
        element_count=2 * _NCOL, bbox=(0.0, 0.0, 10.0 * _NCOL, 1.0), utm_epsg=32617)
    mesh = {"artifact": art, "display_uri": str(display)}
    quads = _quads()
    monkeypatch.setattr(SIM, "mesh_nodes", lambda _mesh: quads)
    monkeypatch.setattr(MF, "mesh_nodes", lambda _mesh: quads)
    return mesh


@pytest.fixture
def staged_beside(monkeypatch, tmp_path):
    """The DISV's upload replaced by a copy beside the run, as the authoring suite does."""
    out = tmp_path / "staged"
    out.mkdir()

    def _copy(_mesh_id, local):
        dest = out / local.name
        shutil.copy(local, dest)
        return str(dest)

    monkeypatch.setattr(MF, "_stage", _copy)
    return out


# ---- the fill: what Transport composes, what it refuses ----------------------


def test_the_transport_composite_expands_to_a_coupled_gwt_body():
    sheet = _sheet(_transport())

    assert len(sheet.coupled) == 1
    body = sheet.coupled[0]
    assert body["module"] == "gwt" and body["given"] == []
    assert body["slots"]["MST_POROSITY"] == _PRSITY
    assert body["slots"]["ADV_SCHEME"] == "upstream"


def test_a_stress_statement_is_the_factory_s_shape_inside_the_body():
    sheet = _sheet(_transport())

    cnc = sheet.coupled[0]["slots"]["CNC_STRESS_PERIOD_DATA"]
    assert cnc["stress"] == "CNC" and cnc["composite"] == "ConstantConcentration"
    assert cnc["values"] == {"concentration": _C0} and cnc["layer"] == 1
    assert cnc["geometry"]["coordinates"] == _inside()


def test_an_unstated_given_composes_nothing():
    sheet = _sheet({"given": "recharge", "slots": {"MST_POROSITY": _PRSITY}})

    assert sheet.coupled == ()


def test_a_gwt_keyword_the_dictionary_lacks_refuses_by_name():
    with pytest.raises(SlotRefused) as caught:
        _sheet({"slots": {"MST_POROSITYY": _PRSITY}})

    assert "MST_POROSITYY" in str(caught.value)
    assert "gwt" in str(caught.value)


@pytest.mark.parametrize("identifier", ["TDIS_NPER", "DISV_NLAY"])
def test_the_transport_body_may_not_state_the_simulation_s_own(identifier):
    with pytest.raises(SlotRefused) as caught:
        _sheet({"slots": {identifier: 1}})

    assert "simulation's own" in str(caught.value)
    assert identifier in str(caught.value)


# ---- the write: two models over one staged DISV -----------------------------


def test_the_strip_serializes_two_models_over_one_staged_disv(
        strip, staged_beside, tmp_path):
    sheet = _sheet(_transport())
    out = MF._mesh_files(sheet, strip)
    rundir = tmp_path / "run"
    rundir.mkdir(exist_ok=True)
    shutil.copy(out["disv"], rundir / "mesh.disv")
    staged_bytes = (rundir / "mesh.disv").read_bytes()

    written = serialize(sheet, rundir, strip)["written"]

    assert (rundir / "mesh.disv").read_bytes() == staged_bytes
    assert {"gwf.nam", "gwt.nam", "mfsim.nam", "gwf.ims", "gwt.ims"} <= set(written)
    assert (rundir / "gwf.gwfgwt").exists()
    both = " ".join((rundir / "mfsim.nam").read_text().split())
    assert "GWF6-GWT6 gwf.gwfgwt gwf gwt" in both
    assert "mesh.disv" in (rundir / "gwf.nam").read_text()
    assert "mesh.disv" in (rundir / "gwt.nam").read_text()

    read = check_staged_run(
        rundir, steering="mfsim.nam",
        inputs=[{"gs_uri": out["disv"], "dest": "mesh.disv"}],
        written_by_the_engine=written, duration_s=2000.0 * 86400.0, cores=1)
    assert read["digested"] == ["mesh.disv"]


@pytest.mark.parametrize("identifier", ["IC_STRT", "MST_POROSITY"])
def test_each_transport_value_left_unstated_refuses_by_name(
        identifier, strip, staged_beside, tmp_path):
    slots = _transport()["slots"]
    slots.pop(identifier)
    with pytest.raises(ModflowError) as caught:
        serialize(_sheet({"slots": slots}), tmp_path / "run", strip)

    assert caught.value.error_code == "MODFLOW_KEYWORD_UNSTATED"
    assert identifier in str(caught.value)


def test_cnc_at_a_point_off_the_mesh_refuses(strip, staged_beside, tmp_path):
    from trid3nt_server.inputs.point import PointNotViableError

    transport = _transport()
    transport["slots"]["CNC_STRESS_PERIOD_DATA"] = {
        "geometry": {"type": "Point", "coordinates": [0.0, 0.0]},
        "layer": 1, "concentration": _C0}
    with pytest.raises(PointNotViableError) as caught:
        serialize(_sheet(transport), tmp_path / "run", strip)

    assert caught.value.error_code == "POINT_NOT_VIABLE"
    assert "ConstantConcentration" in str(caught.value)


# ---- the read: the fixture's concentration as its temporal group ------------


def _fixture_sheet():
    """The sheet the committed fixture solved: this strip's flow, no flow stress."""
    return fill(GWF, **{
        "DISV_NLAY": 1, "DISV_TOP": 0.0, "DISV_BOTM": [[-1.0] * 3],
        "DISV_LENGTH_UNITS": "meters",
        "NPF_K": _K11, "NPF_ICELLTYPE": 0, "IC_STRT": [[0.0] * 3],
        "TDIS_NPER": 1, "TDIS_TIME_UNITS": "days",
        "TDIS_PERIODDATA": [[10.0, 2, 1.0]],
        "OC_HEAD_FILERECORD": "gwf.hds", "OC_BUDGET_FILERECORD": "gwf.cbc",
        "OC_SAVERECORD": [("HEAD", "LAST"), ("BUDGET", "LAST")],
        "Transport": {"slots": {
            "IC_STRT": [[_C0] + [0.0] * 2],
            "MST_POROSITY": _PRSITY, "ADV_SCHEME": "upstream",
            "OC_CONCENTRATION_FILERECORD": "gwt.ucn",
            "OC_BUDGET_FILERECORD": "gwt.cbc",
            "OC_SAVERECORD": [("CONCENTRATION", "LAST"), ("BUDGET", "LAST")],
            "IMS_OUTER_DVCLOSE": 1.0e-6, "IMS_OUTER_MAXIMUM": 100,
            "IMS_INNER_MAXIMUM": 300, "IMS_INNER_DVCLOSE": 1.0e-6,
            "IMS_RCLOSERECORD": [1.0e-6, "default"],
            "IMS_LINEAR_ACCELERATION": "BICGSTAB",
            "CNC_STRESS_PERIOD_DATA": {
                "geometry": {"type": "Point", "coordinates": _TO_LONLAT(5.0, 0.5)},
                "layer": 1, "concentration": _C0}}}})


def _fixture_display(path: Path) -> Path:
    """The fixture strip's display: its 3 cells as the triangles the mesher wrote."""
    rows = ["MESH2D"]
    cell = 0
    for c in range(3):
        a = c + 1
        cell += 1
        rows.append(f"E3T {cell} {a} {a + 1} {5 + c}")
        cell += 1
        rows.append(f"E3T {cell} {5 + c} {6 + c} {a + 1}")
    node = 0
    for y in (0.0, 1.0):
        for x in (0.0, 10.0, 20.0, 30.0):
            node += 1
            rows.append(f"ND {node} {x} {y} 0.0")
    path.write_text("\n".join(rows) + "\n")
    return path


@pytest.fixture
def publish(monkeypatch, tmp_path, fake_s3):
    """Publish the fixture run -> (record, items, notes)."""
    def download(_run_id, basename):
        dest = tmp_path / f"dl_{basename}"
        shutil.copy(_FIXTURE / basename, dest)
        return str(dest)

    monkeypatch.setattr("trid3nt_server.workflows.solver.solver.download_result", download)
    from trid3nt_server.workflows.modflow.modules import outputs as out_mod
    quads = ([(x, y) for y in (0.0, 1.0) for x in (0.0, 10.0, 20.0, 30.0)],
             [[c, 4 + c, 5 + c, c + 1] for c in range(3)], None, None)
    monkeypatch.setattr(out_mod, "mesh_nodes", lambda _mesh: quads)
    handed = []

    async def _publish(*, run_id, engine, name, items):
        assert engine == "modflow"
        handed.extend(items)
        return Published(layers=tuple(
            LayerURI(layer_id=f"L{rank}", name=item.caption, layer_type="mesh",
                     uri=f"s3://runs/{run_id}/{item.product.file}")
            for rank, item in enumerate(items) if isinstance(item.product, Mesh)))

    monkeypatch.setattr("trid3nt_server.workflows.runtime.primitives.publish", _publish)

    def run(outputs=(), anchors=(), captions=None, **over):
        display = _fixture_display(tmp_path / "mesh.2dm")
        art = MeshArtifact(
            mesh_id="F01", name="strip", mode="om2d", display_uri=str(display),
            crs_authid="EPSG:32617", has_bathymetry=False, node_count=8,
            element_count=6, bbox=(0.0, 0.0, 30.0, 1.0), utm_epsg=32617)
        handle = {"run_id": "R1", "utm_epsg": 32617, "module": "gwf",
                  "name": "strip", "reference_time": "2024-06-01T00:00:00Z",
                  "vertical_frame": "NAVD88",
                  "mesh": {"artifact": art, "display_uri": str(display)}, **over}
        token = bind_notes()
        try:
            from trid3nt_server.workflows.modflow import workflow as door
            record = asyncio.run(door.publish_outputs(
                run=handle, sheet=_fixture_sheet(), outputs=list(outputs),
                captions=dict(captions or {}), params={}, anchors=list(anchors)))
        finally:
            notes = drain_notes(token)
        return record, list(handed), notes

    return run


def test_the_fixture_s_concentration_publishes_as_its_temporal_group(publish, fake_s3):
    from trid3nt_server.store import objects as storage

    record, items, _notes = publish()

    groups = sorted(item.product.group for item in items if isinstance(item.product, Mesh))
    assert "concentration_layer_1" in groups
    concentration = [item for item in items
                     if isinstance(item.product, Mesh)
                     and item.product.group == "concentration_layer_1"]
    assert len(concentration) == 1
    assert concentration[0].product.frames == 1
    # One file per group: the concentration rides its own UGRID file beside the head's.
    files = [item.product.file for item in items if isinstance(item.product, Mesh)]
    assert files.count(concentration[0].product.file) == 1
    assert record.uri.endswith("modflow_head_layer_1.nc")
    body = fake_s3.get_object(
        Bucket=storage.runs_bucket(),
        Key=f"R1/{concentration[0].product.file}")["Body"].read()
    ds = netCDF4.Dataset("inmemory.nc", memory=body)
    assert "concentration_layer_1" in ds.variables
    variable = ds.variables["concentration_layer_1"]
    assert variable.shape == (1, 3)
    assert variable[0, :].tolist() == [_C0, 0.0, 0.0]


def test_the_fixture_s_concentration_read_directly_is_its_saved_frames(
        monkeypatch, tmp_path, fake_s3):
    def download(_run_id, basename):
        dest = tmp_path / f"dl_{basename}"
        shutil.copy(_FIXTURE / basename, dest)
        return str(dest)

    monkeypatch.setattr("trid3nt_server.workflows.solver.solver.download_result", download)
    from trid3nt_server.workflows.modflow.modules import outputs as out_mod
    quads = ([(x, y) for y in (0.0, 1.0) for x in (0.0, 10.0, 20.0, 30.0)],
             [[c, 4 + c, 5 + c, c + 1] for c in range(3)], None, None)
    monkeypatch.setattr(out_mod, "mesh_nodes", lambda _mesh: quads)
    display = _fixture_display(tmp_path / "mesh.2dm")
    art = MeshArtifact(
        mesh_id="F01", name="strip", mode="om2d", display_uri=str(display),
        crs_authid="EPSG:32617", has_bathymetry=False, node_count=8,
        element_count=6, bbox=(0.0, 0.0, 30.0, 1.0), utm_epsg=32617)
    run = {"run_id": "R1", "utm_epsg": 32617, "sheet": _fixture_sheet(),
           "reference_time": "2024-06-01T00:00:00Z", "vertical_frame": "NAVD88",
           "mesh": {"artifact": art, "display_uri": str(display)}}
    gwt_body = _fixture_sheet().coupled[0]
    from trid3nt_contracts.common import Origin

    from trid3nt_server.workflows.runtime.sheet import Filled, Provenance
    wrapper = GWT
    solved = Solved(run, wrapper, filled={
        name: Filled(slot=wrapper.slot(name),
                     value=wrapper.slot(name).check(value),
                     provenance=Provenance(Origin.DERIVED, "the coupled deck"))
        for name, value in gwt_body["slots"].items()})
    from trid3nt_server.workflows.runtime import primitives as P

    read = GWT.READS["field"](P.field("CONCENTRATION", t="every"), solved)

    assert read.frames == 1
    assert read.group == "concentration_layer_1"
    assert read.values.shape == (1, 3)
    assert read.values[0].tolist() == [_C0, 0.0, 0.0]


# ---- a measured concentration against the fixture's, one site and a set ------


def _sample(site_id, x, value, units="ug/L"):
    from trid3nt_server.inputs.observation import Observation

    lon, lat = _TO_LONLAT(x, 0.5)
    return Observation(value=value, units=units, site_id=site_id, lon=lon, lat=lat,
                       sampled="2024-06-11T00:00:00Z",
                       asked="2024-06-11T00:00:00+00:00")


@pytest.fixture
def gwt_solved(monkeypatch, tmp_path, fake_s3):
    """The fixture's transport body opened for reading -> a builder over the handle's own statements."""
    def download(_run_id, basename):
        dest = tmp_path / f"dl_{basename}"
        shutil.copy(_FIXTURE / basename, dest)
        return str(dest)

    monkeypatch.setattr("trid3nt_server.workflows.solver.solver.download_result", download)
    from trid3nt_server.workflows.modflow import workflow as door
    from trid3nt_server.workflows.modflow.modules import outputs as out_mod
    quads = ([(x, y) for y in (0.0, 1.0) for x in (0.0, 10.0, 20.0, 30.0)],
             [[c, 4 + c, 5 + c, c + 1] for c in range(3)], None, None)
    monkeypatch.setattr(out_mod, "mesh_nodes", lambda _mesh: quads)

    def build(**over):
        display = _fixture_display(tmp_path / "mesh.2dm")
        art = MeshArtifact(
            mesh_id="F01", name="strip", mode="om2d", display_uri=str(display),
            crs_authid="EPSG:32617", has_bathymetry=False, node_count=8,
            element_count=6, bbox=(0.0, 0.0, 30.0, 1.0), utm_epsg=32617)
        run = {"run_id": "R1", "utm_epsg": 32617, "sheet": _fixture_sheet(),
               "reference_time": "2024-06-01T00:00:00Z", "vertical_frame": "NAVD88",
               "mesh": {"artifact": art, "display_uri": str(display)}, **over}
        sheet = run["sheet"]
        from trid3nt_server.workflows.modflow.authoring.simulation import coupled_sheet

        return Solved({**run}, GWT, filled=coupled_sheet(sheet, sheet.coupled[0]).filled)

    return build


def test_a_measured_concentration_is_checked_in_the_unit_the_run_states(gwt_solved):
    from trid3nt_server.workflows.runtime import primitives as P

    read = GWT.READS["series"](P.series("CONCENTRATION", at=_sample("W-0", 5.0, 900.0)),
                               gwt_solved(published_units={"CONCENTRATION": "mg/L"}))
    assert read.units == "mg/L"
    assert read.measures["observed"] == pytest.approx(0.9)
    assert read.measures["simulated"] == pytest.approx(_C0)
    assert read.measures["units"] == "mg/L"


def test_a_concentration_the_run_states_no_unit_for_refuses_naming_it(gwt_solved):
    from trid3nt_server.workflows.runtime import primitives as P

    with pytest.raises(ModflowError) as caught:
        GWT.READS["series"](P.series("CONCENTRATION", at=_sample("W-0", 5.0, 900.0)),
                            gwt_solved())
    assert caught.value.error_code == "MODFLOW_UNITS_UNSTATED"
    assert "concentration_units" in str(caught.value)


def test_a_reading_no_conversion_reaches_the_run_s_unit_refuses(gwt_solved):
    from trid3nt_server.workflows.runtime import primitives as P

    with pytest.raises(ModflowError) as caught:
        GWT.READS["series"](P.series("CONCENTRATION",
                                     at=_sample("W-0", 5.0, 3.0, units="ft")),
                            gwt_solved(published_units={"CONCENTRATION": "mg/L"}))
    assert caught.value.error_code == "MODFLOW_OBSERVATION_UNCOMPARABLE"


def test_a_set_of_sites_publishes_one_comparison_and_drops_the_one_off_the_mesh(publish):
    from trid3nt_server.render.formats import Chart
    from trid3nt_server.workflows.runtime import primitives as P

    wells = (_sample("W-0", 5.0, 900.0), _sample("W-1", 15.0, 20.0),
             _sample("FAR-OUT", 500.0, 5.0))
    _record, items, notes = publish(
        outputs=[P.series("CONCENTRATION", module="gwt").chart()],
        anchors=[{"at": wells, "along": None, "within": None}],
        captions={"CONCENTRATION": "boron"},
        published_units={"CONCENTRATION": "mg/L"})

    charts = [item for item in items if isinstance(item.product, Chart)]
    assert len(charts) == 1
    points, agree = charts[0].product.payload["vega_lite_spec"]["layer"]
    assert [(row["site"], row["observed"], row["simulated"])
            for row in points["data"]["values"]] == [("W-0", 0.9, _C0), ("W-1", 0.02, 0.0)]
    assert points["encoding"]["x"]["title"] == "Measured (mg/L)"
    assert agree["mark"]["type"] == "line"
    # The well the mesh does not hold is never moved onto it: it is left out and said.
    assert any("FAR-OUT" in note and "outside the mesh" in note for note in notes)
