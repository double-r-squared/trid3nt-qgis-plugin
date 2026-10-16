"""The MODFLOW workflow: the template it registers, and the publish of a solved run through the runtime's one path.

Offline over the synthetic verification fixture (tests/fixtures/modflow/lattice_solved): the publish
hands the module table of every output the output control saved, one temporal layer per layer, beside the
template's reads; an unsaved output is journalled, the run's record is the layer-1 head group's file.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

import pytest

from trid3nt_contracts.execution import LayerURI

from trid3nt_server.inputs.run_notes import bind_notes, drain_notes
from trid3nt_server.render.formats import Chart, Mesh, Published
from trid3nt_server.tools.mesh.artifact import MeshArtifact
from trid3nt_server.workflows.modflow import workflow as door
from trid3nt_server.workflows.modflow.errors import ModflowError
from trid3nt_server.workflows.modflow.modules import GWF
from trid3nt_server.workflows.runtime.primitives import mass_balance, unanchored
from trid3nt_server.workflows.runtime.sheet import fill

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "modflow" / "lattice_solved"


def _lattice(path: Path) -> Path:
    rows = ["MESH2D"]
    cell = 0
    for r in range(2):
        for c in range(2):
            a = r * 3 + c + 1
            for tri in ((a, a + 1, a + 4), (a, a + 4, a + 3)):
                cell += 1
                rows.append(f"E3T {cell} {tri[0]} {tri[1]} {tri[2]} 1")
    node = 0
    for y in (0.0, 10.0, 20.0):
        for x in (0.0, 10.0, 20.0):
            node += 1
            rows.append(f"ND {node} {x} {y} -2.0")
    path.write_text("\n".join(rows) + "\n")
    return path


def _sheet(**over):
    stated = {
        "DISV_NLAY": 2, "DISV_BOTM": [[-10.0] * 8, [-20.0] * 8],
        "DISV_LENGTH_UNITS": "meters", "NPF_K": 1.0e-4, "NPF_ICELLTYPE": 0,
        "IC_STRT": 0.0, "NPF_SAVE_SATURATION": True,
        "NPF_SAVE_SPECIFIC_DISCHARGE": True, "TDIS_NPER": 2,
        "TDIS_TIME_UNITS": "days",
        "TDIS_PERIODDATA": [[5.0, 2, 1.0], [5.0, 1, 1.0]],
        "OC_HEAD_FILERECORD": "gwf.hds", "OC_BUDGET_FILERECORD": "gwf.cbc"}
    stated.update(over)
    return fill(GWF, **{k: v for k, v in stated.items() if v is not None})


@pytest.fixture
def publish(monkeypatch, tmp_path, fake_s3):
    """Publish the fixture run with the sheet ``over`` states -> (record, items, notes)."""
    def download(run_id, basename):
        dest = tmp_path / f"dl_{basename}"
        shutil.copy(_FIXTURE / basename, dest)
        return str(dest)

    monkeypatch.setattr("trid3nt_server.workflows.solver.solver.download_result", download)
    handed: list[Any] = []

    async def _publish(*, run_id, engine, name, items):
        assert engine == "modflow"
        handed.extend(items)
        return Published(layers=tuple(
            LayerURI(layer_id=f"L{rank}", name=item.caption, layer_type="mesh",
                     uri=f"s3://runs/{run_id}/{item.product.file}")
            for rank, item in enumerate(items) if isinstance(item.product, Mesh)))

    monkeypatch.setattr("trid3nt_server.workflows.runtime.primitives.publish", _publish)

    def run(outputs=(mass_balance().chart(),), captions=None, anchors=(), **over):
        display = _lattice(tmp_path / "mesh.2dm")
        art = MeshArtifact(
            mesh_id="M01", name="lattice", mode="om2d", display_uri=str(display),
            crs_authid="EPSG:32617", has_bathymetry=True, node_count=9,
            element_count=8, bbox=(0.0, 0.0, 20.0, 20.0), utm_epsg=32617)
        handle = {"run_id": "R1", "utm_epsg": 32617, "module": "gwf",
                  "name": "lattice", "reference_time": "2024-06-01T00:00:00Z",
                  "vertical_frame": "NAVD88",
                  "mesh": {"artifact": art, "display_uri": str(display)}}
        token = bind_notes()
        try:
            record = asyncio.run(door.publish_outputs(
                run=handle, sheet=_sheet(**over),
                outputs=[unanchored(p) for p in outputs],
                captions=captions or {"mass_balance": "water budget"}, params={},
                anchors=anchors))
        finally:
            notes = drain_notes(token)
        return record, list(handed), notes

    return run


def test_every_saved_output_is_one_temporal_layer_per_layer_beside_the_budget(publish):
    record, items, _notes = publish()

    groups = sorted(item.product.group for item in items if isinstance(item.product, Mesh))
    assert groups == sorted(f"{name}_layer_{layer}" for layer in (1, 2)
                            for name in ("head", "saturation", "specific_discharge"))
    assert all(item.product.frames == 3 for item in items if isinstance(item.product, Mesh))
    charts = [item for item in items if isinstance(item.product, Chart)]
    assert [item.caption for item in charts] == ["water budget"]
    assert record.uri.endswith("/R1/modflow_head_layer_1.nc")
    assert record.layer_type == "mesh" and record.crs_authid == "EPSG:32617"


def test_an_output_the_run_did_not_save_is_journalled_never_refused(publish):
    _record, items, notes = publish(NPF_SAVE_SATURATION=None)

    groups = {item.product.group for item in items if isinstance(item.product, Mesh)}
    assert not any(group.startswith("saturation") for group in groups)
    assert {"head_layer_1", "specific_discharge_layer_2"} <= groups
    assert any("saturation" in note and "NPF_SAVE_SATURATION" in note for note in notes)


def test_a_run_that_saved_no_head_has_no_record_to_return(publish):
    with pytest.raises(ModflowError) as caught:
        publish(OC_HEAD_FILERECORD=None)
    assert caught.value.error_code == "MODFLOW_OUTPUT_UNSAVED"


def test_the_template_s_well_check_is_said_on_the_run_s_record(publish):
    from pyproj import Transformer

    from trid3nt_server.inputs.observation import Observation
    from trid3nt_server.tools import TOOL_REGISTRY

    template = TOOL_REGISTRY["modflow_water_table"].fn.workflow.template
    lon, lat = Transformer.from_crs(32617, 4326, always_xy=True).transform(18.0, 12.0)
    well = Observation(value=10.0, units="ft", site_id="USGS-1", lon=lon, lat=lat,
                       quantity="water_table_depth", sampled="2024-06-11T00:00:00Z")
    _record, _items, notes = publish(
        outputs=template.OUTPUTS, captions=template.CAPTIONS,
        anchors=[{"at": well if p.at == "observe" else p.at, "along": None,
                  "within": None} for p in template.OUTPUTS],
        DISV_NLAY=1, DISV_BOTM=[[-10.0] * 8], NPF_SAVE_SATURATION=None,
        NPF_SAVE_SPECIFIC_DISCHARGE=None)
    said = next(note for note in notes if note.startswith("the water table at the observed well"))
    assert "site = USGS-1" in said and "observed = 3.048" in said
    assert "quantity = water_table_depth" in said and "simulated_minus_observed = " in said


def test_the_template_registers_over_the_modflow_workflow():
    from trid3nt_server.tools import TOOL_REGISTRY

    entry = TOOL_REGISTRY["modflow_water_table"]
    workflow = entry.fn.workflow
    assert isinstance(workflow, door.ModflowWorkflow)
    assert entry.metadata.engine == "modflow" and workflow.moment == "event_time"
    by_name = {row.name: row for row in workflow.inputs}
    assert by_name["drain_conductance"].required
    assert by_name["drain_conductance"].units == "m2/day"
    assert by_name["well"].stated_only and by_name["well_rate"].stated_only
    assert {"domain", "bed", "saturated_thickness", "transmissivity", "water_table_depth",
            "recharge", "streams", "observe"} <= set(workflow.named_rows())
    assert "conductivity" not in by_name
    # Each aquifer row is filled by the source publishing its quantity; the well is read as a point.
    assert {name: (by_name[name].data_class, by_name[name].observes) for name in
            ("saturated_thickness", "transmissivity", "water_table_depth", "recharge")} == {
        "saturated_thickness": ("aquifer property", "aquifer_saturated_thickness"),
        "transmissivity": ("aquifer property", "aquifer_transmissivity"),
        "water_table_depth": ("groundwater level", "water_table_depth"),
        "recharge": ("aquifer property", "groundwater_recharge")}
    assert (by_name["observe"].data_class, by_name["observe"].geometry) == (
        "groundwater level", "point")
    assert workflow.unnamed() == ()


def test_every_row_the_sheet_reads_is_in_the_mapping_after_the_fill(monkeypatch):
    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.tools.mesh.recipe import input_names
    from trid3nt_server.workflows.runtime import fill as fill_mod
    from trid3nt_server.workflows.runtime.fill import Fill

    async def _produce(env, decl):
        return decl.name

    monkeypatch.setattr(fill_mod, "_produce", _produce)
    workflow = TOOL_REGISTRY["modflow_water_table"].fn.workflow
    state = asyncio.run(fill_mod.fill(Fill(workflow=workflow), {
        "drain_conductance": 100.0, "input_mode": "auto"}))
    assert state.ready, state.refusal()
    read = set(workflow.steering.named()) | set(input_names(workflow.template.MESH))
    rows = {row.name for row in workflow.inputs if row.is_row and row.name != "mesh"}
    assert read & rows and not sorted(name for name in read & rows
                                      if name not in state.env.run)
