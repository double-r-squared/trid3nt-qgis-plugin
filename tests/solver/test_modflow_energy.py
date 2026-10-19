"""The energy leg: a gwf deck that couples a gwe body beside a gwt body, the three-model write over one staged DISV, and the solved fixture's temperature read.

Offline over the p01 strip (1 row x 101 cells, 10 m): the Energy composite's expansion and its refusals at
the fill, serialize writing every name file over the mesh's own DISV with one IMS per model and one exchange
per coupled body, the exchange table refusing an unknown pair, and the committed fixture's temperature
publishing as its temporal UGRID group. ``mesh_nodes`` is monkeypatched to hand the strip's quad cells.
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
from trid3nt_server.workflows.modflow.authoring.simulation import serialize
from trid3nt_server.workflows.modflow.authoring.stress import place
from trid3nt_server.workflows.modflow.errors import ModflowError
from trid3nt_server.workflows.modflow.modules import GWE, GWF
from trid3nt_server.workflows.modflow.modules.outputs import Solved
from trid3nt_server.workflows.runtime.module import SlotRefused
from trid3nt_server.workflows.runtime.sheet import fill

_NCOL = 101
_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "modflow" / "strip_energy"
_TO_LONLAT = Transformer.from_crs(32617, 4326, always_xy=True).transform
_IMS = {"IMS_OUTER_DVCLOSE": 1.0e-6, "IMS_OUTER_MAXIMUM": 100,
        "IMS_INNER_MAXIMUM": 300, "IMS_INNER_DVCLOSE": 1.0e-6,
        "IMS_RCLOSERECORD": [1.0e-6, "default"]}


def _strip(tmp_path: Path) -> Path:
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
    xs = [i * 10.0 for i in range(_NCOL + 1)]
    points = [[x, y] for y in (0.0, 1.0) for x in xs]
    cells = [[c, _NCOL + 1 + c, _NCOL + 1 + c + 1, c + 1] for c in range(_NCOL)]
    return (points, cells, None, None)


def _inside() -> list[float]:
    lon, lat = _TO_LONLAT(5.0, 0.5)
    return [lon, lat]


def _sheet(*bodies: dict, **over):
    """A strip's flow through this leg's fill with the coupled bodies stated by composite name."""
    stated = {
        "NAM_SAVE_FLOWS": True,
        "DISV_NLAY": 1, "DISV_TOP": 0.0, "DISV_BOTM": [[-1.0] * _NCOL],
        "DISV_LENGTH_UNITS": "meters", "NPF_K": 1.0, "NPF_ICELLTYPE": 0,
        "IC_STRT": [[0.0] * _NCOL],
        "TDIS_NPER": 1, "TDIS_TIME_UNITS": "days", "TDIS_PERIODDATA": [[10.0, 2, 1.0]],
        "OC_HEAD_FILERECORD": "gwf.hds", "OC_BUDGET_FILERECORD": "gwf.cbc",
        "OC_SAVERECORD": [("HEAD", "LAST"), ("BUDGET", "LAST")],
        "IMS_LINEAR_ACCELERATION": "CG", **_IMS}
    stated.update(dict(zip(("Transport", "Energy"), bodies)) if len(bodies) == 2
                  else {})
    stated.update(over)
    return fill(GWF, **{k: v for k, v in stated.items() if v is not None})


def _energy(**over) -> dict:
    """The gwe body's stated slots: CTP at the strip's first cell."""
    slots = {
        "NAM_SAVE_FLOWS": True, "IC_STRT": [[4.0] * _NCOL], "ADV_SCHEME": "upstream",
        "EST_POROSITY": 0.25, "EST_HEAT_CAPACITY_SOLID": 800.0, "EST_DENSITY_SOLID": 1200.0,
        "CND_KTW": 50000.0, "CND_KTS": 20000.0,
        "OC_TEMPERATURE_FILERECORD": "gwe.ucn", "OC_BUDGET_FILERECORD": "gwe.cbc",
        "OC_SAVERECORD": [("TEMPERATURE", "LAST"), ("BUDGET", "LAST")],
        "IMS_LINEAR_ACCELERATION": "BICGSTAB", **_IMS,
        "CTP_STRESS_PERIOD_DATA": {
            "geometry": {"type": "Point", "coordinates": _inside()},
            "layer": 1, "temperature": 20.0}}
    slots.update(over)
    return {"slots": {k: v for k, v in slots.items() if v is not None}}


def _transport() -> dict:
    return {"slots": {
        "NAM_SAVE_FLOWS": True, "IC_STRT": [[1.0] + [0.0] * (_NCOL - 1)],
        "MST_POROSITY": 0.25, "ADV_SCHEME": "upstream",
        "OC_CONCENTRATION_FILERECORD": "gwt.ucn", "OC_BUDGET_FILERECORD": "gwt.cbc",
        "OC_SAVERECORD": [("CONCENTRATION", "LAST"), ("BUDGET", "LAST")],
        "IMS_LINEAR_ACCELERATION": "BICGSTAB", **_IMS}}


def _energy_only(body: dict, **over):
    return fill(GWF, **{
        "NAM_SAVE_FLOWS": True,
        "DISV_NLAY": 1, "DISV_TOP": 0.0, "DISV_BOTM": [[-1.0] * _NCOL],
        "DISV_LENGTH_UNITS": "meters", "NPF_K": 1.0, "NPF_ICELLTYPE": 0,
        "IC_STRT": [[0.0] * _NCOL],
        "TDIS_NPER": 1, "TDIS_TIME_UNITS": "days", "TDIS_PERIODDATA": [[10.0, 2, 1.0]],
        "OC_HEAD_FILERECORD": "gwf.hds", "OC_BUDGET_FILERECORD": "gwf.cbc",
        "OC_SAVERECORD": [("HEAD", "LAST"), ("BUDGET", "LAST")],
        "IMS_LINEAR_ACCELERATION": "CG", **_IMS, "Energy": body, **over})


@pytest.fixture
def strip(monkeypatch, tmp_path):
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
    out = tmp_path / "staged"
    out.mkdir()

    def _copy(_mesh_id, local):
        dest = out / local.name
        shutil.copy(local, dest)
        return str(dest)

    monkeypatch.setattr(MF, "_stage", _copy)
    return out


# ---- the fill: what Energy composes, what it refuses -------------------------


def test_the_energy_composite_expands_to_a_coupled_gwe_body():
    sheet = _energy_only(_energy())

    assert len(sheet.coupled) == 1
    body = sheet.coupled[0]
    assert body["module"] == "gwe" and body["given"] == []
    assert body["slots"]["EST_POROSITY"] == 0.25
    ctp = body["slots"]["CTP_STRESS_PERIOD_DATA"]
    assert ctp["stress"] == "CTP" and ctp["composite"] == "ConstantTemperature"
    assert ctp["values"] == {"temperature": 20.0} and ctp["layer"] == 1


def test_an_energy_source_is_the_factory_s_shape_inside_the_body():
    sheet = _energy_only(_energy(ESL_STRESS_PERIOD_DATA={
        "geometry": {"type": "Point", "coordinates": _inside()}, "layer": 1,
        "rate": 5.0}))

    esl = sheet.coupled[0]["slots"]["ESL_STRESS_PERIOD_DATA"]
    assert esl["stress"] == "ESL" and esl["composite"] == "EnergySource"
    assert esl["values"] == {"rate": 5.0}


def test_an_unstated_given_composes_nothing():
    sheet = _energy_only({"given": "recharge", "slots": {"EST_POROSITY": 0.25}})

    assert sheet.coupled == ()


def test_a_gwe_keyword_the_dictionary_lacks_refuses_by_name():
    with pytest.raises(SlotRefused) as caught:
        _energy_only({"slots": {"EST_POROSITYY": 0.25}})

    assert "EST_POROSITYY" in str(caught.value)
    assert "gwe" in str(caught.value)


@pytest.mark.parametrize("identifier", ["TDIS_NPER", "DISV_NLAY"])
def test_the_energy_body_may_not_state_the_simulation_s_own(identifier):
    with pytest.raises(SlotRefused) as caught:
        _energy_only({"slots": {identifier: 1}})

    assert "simulation's own" in str(caught.value)
    assert identifier in str(caught.value)


def test_a_temperature_raster_refuses_for_want_of_a_unit_on_the_frame():
    statement = {"stress": "CTP", "composite": "ConstantTemperature", "layer": 1,
                 "geometry": {"type": "Point", "coordinates": _inside()},
                 "values": {"temperature": {"uri": "t.tif", "units": "degC"}},
                 "dimensions": {"temperature": "THETA"}}
    points, cells, _bed, _lonlat = _quads()

    with pytest.raises(ModflowError) as caught:
        place(statement, points=points, cells=cells, utm_epsg=32617, nlay=1,
              frame=("meters", "days"))

    assert caught.value.error_code == "MODFLOW_UNITS_UNSTATED"
    assert "THETA" in str(caught.value)


def test_the_exchange_table_refuses_a_pair_it_does_not_hold():
    with pytest.raises(ModflowError) as caught:
        SIM._exchange(None, None, carrier="gwt", coupled="gwe")

    assert caught.value.error_code == "MODFLOW_EXCHANGE_UNKNOWN"
    assert "gwt and gwe" in str(caught.value)


# ---- the write: three models over one staged DISV ---------------------------


def test_the_strip_serializes_both_coupled_bodies_over_one_staged_disv(
        strip, staged_beside, tmp_path):
    sheet = _sheet(_transport(), _energy())
    assert [body["module"] for body in sheet.coupled] == ["gwt", "gwe"]
    out = MF._mesh_files(sheet, strip)
    rundir = tmp_path / "run"
    rundir.mkdir(exist_ok=True)
    shutil.copy(out["disv"], rundir / "mesh.disv")
    staged_bytes = (rundir / "mesh.disv").read_bytes()

    written = serialize(sheet, rundir, strip)["written"]

    assert (rundir / "mesh.disv").read_bytes() == staged_bytes
    assert {"gwf.nam", "gwt.nam", "gwe.nam", "mfsim.nam", "gwf.ims", "gwt.ims",
            "gwe.ims", "gwf.gwfgwt", "gwf.gwfgwe", "gwe.ctp"} <= set(written)
    both = " ".join((rundir / "mfsim.nam").read_text().split())
    assert "GWF6-GWT6 gwf.gwfgwt gwf gwt" in both
    assert "GWF6-GWE6 gwf.gwfgwe gwf gwe" in both
    for name in ("gwf", "gwt", "gwe"):
        assert "mesh.disv" in (rundir / f"{name}.nam").read_text()


@pytest.mark.parametrize("identifier", ["IC_STRT", "EST_POROSITY", "EST_DENSITY_SOLID"])
def test_each_energy_value_left_unstated_refuses_by_name(
        identifier, strip, staged_beside, tmp_path):
    slots = _energy()["slots"]
    slots.pop(identifier)
    with pytest.raises(ModflowError) as caught:
        serialize(_energy_only({"slots": slots}), tmp_path / "run", strip)

    assert caught.value.error_code == "MODFLOW_KEYWORD_UNSTATED"
    assert identifier in str(caught.value)


def test_ctp_at_a_point_off_the_mesh_refuses(strip, staged_beside, tmp_path):
    from trid3nt_server.inputs.point import PointNotViableError

    body = _energy()
    body["slots"]["CTP_STRESS_PERIOD_DATA"] = {
        "geometry": {"type": "Point", "coordinates": [0.0, 0.0]},
        "layer": 1, "temperature": 20.0}
    with pytest.raises(PointNotViableError) as caught:
        serialize(_energy_only(body), tmp_path / "run", strip)

    assert caught.value.error_code == "POINT_NOT_VIABLE"
    assert "ConstantTemperature" in str(caught.value)


def test_buy_and_vsc_are_the_carrier_s_own_keywords(strip, staged_beside, tmp_path):
    sheet = _sheet(_transport(), _energy(), **{
        "BUY_NRHOSPECIES": 1, "BUY_PACKAGEDATA": [(0, 0.7, 0.0, "gwt", "concentration")],
        "VSC_NVISCSPECIES": 1, "VSC_VISCREF": 8.904e-4,
        "VSC_PACKAGEDATA": [(0, 0.0, 20.0, "gwe", "TEMPERATURE")]})
    rundir = tmp_path / "run"
    rundir.mkdir()

    written = serialize(sheet, rundir, strip)["written"]

    assert {"gwf.buy", "gwf.vsc"} <= set(written)
    assert "gwt" in (rundir / "gwf.buy").read_text()
    assert "gwe" in (rundir / "gwf.vsc").read_text()


# ---- the read: the fixture's temperature as its temporal group --------------


def _fixture_sheet():
    """The sheet the committed fixture solved: a 3-cell strip's flow, a gwe body with CTP at its first cell."""
    return fill(GWF, **{
        "DISV_NLAY": 1, "DISV_TOP": 0.0, "DISV_BOTM": [[-1.0] * 3],
        "DISV_LENGTH_UNITS": "meters", "NPF_K": 1.0, "NPF_ICELLTYPE": 0,
        "IC_STRT": [[0.0] * 3], "TDIS_NPER": 1, "TDIS_TIME_UNITS": "days",
        "TDIS_PERIODDATA": [[10.0, 2, 1.0]],
        "OC_HEAD_FILERECORD": "gwf.hds", "OC_BUDGET_FILERECORD": "gwf.cbc",
        "OC_SAVERECORD": [("HEAD", "LAST"), ("BUDGET", "LAST")],
        "Energy": {"slots": {
            "IC_STRT": [[4.0] * 3], "ADV_SCHEME": "upstream",
            "OC_TEMPERATURE_FILERECORD": "gwe.ucn",
            "OC_SAVERECORD": [("TEMPERATURE", "LAST"), ("BUDGET", "LAST")]}}})


def _fixture_display(path: Path) -> Path:
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


def _stage_fixture(monkeypatch, tmp_path):
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
    return {"run_id": "R1", "utm_epsg": 32617, "module": "gwf", "name": "strip",
            "reference_time": "2024-06-01T00:00:00Z", "vertical_frame": "NAVD88",
            "mesh": {"artifact": art, "display_uri": str(display)}}


def test_the_fixture_s_temperature_publishes_as_its_temporal_group(
        monkeypatch, tmp_path, fake_s3):
    from trid3nt_server.store import objects as storage
    from trid3nt_server.workflows.modflow import workflow as door

    handle = _stage_fixture(monkeypatch, tmp_path)
    handed = []

    async def _publish(*, run_id, engine, name, items):
        handed.extend(items)
        return Published(layers=tuple(
            LayerURI(layer_id=f"L{rank}", name=item.caption, layer_type="mesh",
                     uri=f"s3://runs/{run_id}/{item.product.file}")
            for rank, item in enumerate(items) if isinstance(item.product, Mesh)))

    monkeypatch.setattr("trid3nt_server.workflows.runtime.primitives.publish", _publish)
    token = bind_notes()
    try:
        asyncio.run(door.publish_outputs(
            run=handle, sheet=_fixture_sheet(), outputs=[], captions={}, params={}))
    finally:
        drain_notes(token)

    found = [item for item in handed if isinstance(item.product, Mesh)
             and item.product.group == "temperature_layer_1"]
    assert len(found) == 1 and found[0].product.frames == 1
    body = fake_s3.get_object(
        Bucket=storage.runs_bucket(), Key=f"R1/{found[0].product.file}")["Body"].read()
    ds = netCDF4.Dataset("inmemory.nc", memory=body)
    variable = ds.variables["temperature_layer_1"]
    assert variable.shape == (1, 3)
    assert variable[0, 0] == pytest.approx(20.0)
    assert 4.0 <= variable[0, 1] < 20.0


def test_the_fixture_s_temperature_read_directly_is_its_saved_frames(
        monkeypatch, tmp_path, fake_s3):
    from trid3nt_contracts.common import Origin

    from trid3nt_server.workflows.runtime import primitives as P
    from trid3nt_server.workflows.runtime.sheet import Filled, Provenance

    handle = _stage_fixture(monkeypatch, tmp_path)
    run = {**handle, "sheet": _fixture_sheet()}
    body = _fixture_sheet().coupled[0]
    solved = Solved(run, GWE, filled={
        name: Filled(slot=GWE.slot(name), value=GWE.slot(name).check(value),
                     provenance=Provenance(Origin.DERIVED, "the coupled deck"))
        for name, value in body["slots"].items()})

    read = GWE.READS["field"](P.field("TEMPERATURE", t="every"), solved)

    assert read.frames == 1 and read.group == "temperature_layer_1"
    assert read.values.shape == (1, 3)
    assert read.values[0, 0] == pytest.approx(20.0)
