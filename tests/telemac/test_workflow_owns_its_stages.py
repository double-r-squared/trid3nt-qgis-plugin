"""The stages the WORKFLOW builds from a template module's declared slots.

Offline: nothing is built and nothing is solved. What is proved is the PLAN a
template gets when it states only what differs from the base.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.inputs.input import Input
from trid3nt_server.workflows.runtime import DeclarationError, tool
from seated import SEATED
from trid3nt_server.tools.mesh.recipe import RESOLUTION
from trid3nt_server.workflows.telemac.modules import T2D
from trid3nt_server.workflows.telemac.workflow import TelemacWorkflow
from trid3nt_server.inputs.slots import BED, DOMAIN
from seated import value_inputs, row_inputs

_METADATA = AtomicToolMetadata(
    name="telemac_water_temperature", ttl_class="live-no-cache",
    source_class="workflow_dispatch", cacheable=False, engine="telemac",
    tier="template")


class STEERING(T2D):
    """The deck: a body of water under a week of weather."""

    GEOMETRY_FILE = "domain.slf"
    BOUNDARY_CONDITIONS_FILE = "domain.cli"
    RESULTS_FILE = "r2d_domain.slf"
    # The clock is a KEYWORD the module carries, so the deck states it and the
    # settle reads it off the deck rather than off a param beside it.
    DURATION = 604800.0


class DATA:
    domain = Input(DOMAIN).need("hydrography", at="seed")
    bed = Input(BED).need("bathymetry")


class INPUTS:
    mesh_resolution_m = replace(RESOLUTION, default=25.0)
    domain = Input(DOMAIN).need("hydrography", at="seed")
    bed = Input(BED).need("bathymetry")


def _template(inputs: type = INPUTS) -> SimpleNamespace:
    """A template module, as the workflow reads one: its own names and nothing
    around them."""
    return SimpleNamespace(STEERING=STEERING, INPUTS=inputs)


def _workflow(inputs: type = INPUTS) -> TelemacWorkflow:
    return TelemacWorkflow(metadata=_METADATA, inputs=inputs,
                           template=_template(inputs))


def _mesh(workflow: TelemacWorkflow):
    return _step(workflow, "mesh")


def test_the_engine_seats_its_inputs_so_the_template_states_none_of_them():
    workflow = _workflow()
    # The template's own row for the mesh edge keeps its place; every input it
    # does not state is seated after it, in the order the engine seats them.
    assert [prm.name for prm in value_inputs(workflow)] == [
        "mesh_resolution_m", *(n for n in SEATED if n != "mesh_resolution_m")]
    assert "cores" not in {prm.name for prm in value_inputs(workflow)}


def test_the_mesh_edge_is_seated_only_where_the_mesh_ask_names_it():
    """The edge is the mesh ask's input: an ask built at a stated number names
    no run input, so no row is seated for one."""
    named = _workflow(DATA)
    assert value_inputs(named)[0] is RESOLUTION
    fixed = SimpleNamespace(
        STEERING=STEERING, INPUTS=DATA,
        MESH=tool.build_mesh(mesher="om2d", kind="unstructured_tri",
                             extent="domain", resolution_m=40.0))
    workflow = TelemacWorkflow(metadata=_METADATA, inputs=DATA, template=fixed)
    assert [prm.name for prm in value_inputs(workflow)] == ["event_time",
                                                     "vertical_frame"]


def test_a_sizing_input_is_the_meshers_and_seated_only_where_the_ask_names_it():
    """The grade and the coarsest edge are declared once, by the mesher, with no
    default of their own: an ask that names one seats it REQUIRED, a template's
    restatement gives it the template's number, and an ask naming neither seats none."""
    from trid3nt_server.tools.mesh.recipe import GRADE, MAX_EDGE
    from trid3nt_server.tools.mesh.tool import mesh_op

    def asking(inputs: type) -> TelemacWorkflow:
        named = SimpleNamespace(
            STEERING=STEERING, INPUTS=inputs,
            MESH=tool.build_mesh(
                mesher="om2d", kind="unstructured_tri", extent="domain",
                resolution_m=40.0,
                ops=[mesh_op("distance_sizing_from_line_function",
                             line_file="domain", max_edge_length="mesh_max_edge_m"),
                     mesh_op("enforce_mesh_gradation", gradation="mesh_grade")]))
        return TelemacWorkflow(metadata=_METADATA, inputs=inputs, template=named)

    workflow = asking(DATA)
    assert workflow.input("mesh_grade") is GRADE and GRADE.required
    assert workflow.input("mesh_max_edge_m") is MAX_EDGE and MAX_EDGE.required

    class OPINION:
        mesh_grade = replace(GRADE, default=0.2)
        domain = Input(DOMAIN).need("hydrography", at="seed")
        bed = Input(BED).need("bathymetry")

    stated = asking(OPINION).input("mesh_grade")
    assert (stated.default, stated.bounds, stated.required) == (0.2, GRADE.bounds, False)
    assert not {"mesh_grade", "mesh_max_edge_m"} & {
        prm.name for prm in value_inputs(_workflow(DATA))}


def test_the_frame_is_the_beds_and_every_reader_reads_that_one():
    """The bed's type declares the frame; the match's need and the level read
    the value the run stated for it, and the bed's own default without one."""
    from trid3nt_server.workflows.runtime import fill

    frame, = BED.states
    assert (frame.name, frame.default) == ("vertical_frame", "NAVD88")
    assert _workflow().input("vertical_frame") is frame
    assert fill._frame(fill._Env(data={})) == "NAVD88"
    assert fill._frame(fill._Env(data={}, run={frame.name: "IGLD85"})) == "IGLD85"


def test_the_domain_and_the_bed_reach_the_wire_as_the_slots_they_are():
    """What the user hands in supersedes the producer the template preferred, so
    both slots are arguments even though both name a source."""
    supplied = workflow_wire(_workflow())
    assert {"domain", "bed"} <= supplied


def workflow_wire(workflow: TelemacWorkflow) -> set[str]:
    return {decl.name for decl in row_inputs(workflow) if decl.fills_from_user}


def test_a_template_with_no_domain_refuses_at_import():
    class NO_DOMAIN:
        bed = Input(BED).need("bathymetry")

    with pytest.raises(DeclarationError, match="declares 0 domain rows"):
        _workflow(NO_DOMAIN)


def test_a_template_with_no_bed_refuses_at_import():
    class NO_BED:
        domain = Input(DOMAIN).need("hydrography")

    with pytest.raises(DeclarationError, match="declares 0 bed rows"):
        _workflow(NO_BED)


def _built(kind: str, **fields):
    from trid3nt_server.tools.mesh.artifact import MeshArtifact

    return MeshArtifact(**{**dict(
        mesh_id="01MINE", name="ring", mode="om2d",
        display_uri="s3://m/01MINE/mesh.2dm", crs_authid="EPSG:32610",
        has_bathymetry=True, node_count=4, element_count=2,
        bbox=(-122.68, 45.51, -122.66, 45.52), engine_files={},
        provenance={"recipe": {"mesher": "om2d", "kind": kind}}), **fields})


_RING = [[-122.6769, 45.5153], [-122.6615, 45.5153],
         [-122.6615, 45.5198], [-122.6769, 45.5198]]


def _filled_with(monkeypatch, mesh, **stated):
    import asyncio

    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.workflows.runtime.fill import Fill, fill

    workflow = TOOL_REGISTRY["telemac_dye_release"].fn.workflow
    # Only what the call states is filled: the rows the template names go out to sources.
    monkeypatch.setattr(workflow, "named_rows", lambda held=(): ())
    return asyncio.run(fill(Fill(workflow=workflow),
                            {"domain": _RING, "mesh": mesh, **stated}))


def test_a_template_that_writes_no_mesh_row_fills_with_a_supplied_mesh(monkeypatch):
    """The engine seats the mesh row, so the mesh a caller hands in is the run's
    mesh before any stage builds one."""
    mine = _built("unstructured_tri")
    state = _filled_with(monkeypatch, mine)
    assert state.inputs["mesh"].state == "accepted"
    assert state.env.run["mesh"]["artifact"] is mine
    assert state.env.run["mesh"]["mesh_id"] == "01MINE"


def test_a_supplied_mesh_of_a_kind_the_engine_does_not_solve_on_is_refused(
        monkeypatch):
    state = _filled_with(monkeypatch, _built("structured_grid"))
    assert state.inputs["mesh"].state == "rejected"
    assert state.inputs["mesh"].code == "MESH_KIND_MISMATCH"


def test_a_supplied_mesh_nobody_painted_a_bed_onto_is_refused(monkeypatch):
    state = _filled_with(monkeypatch,
                         _built("unstructured_tri", has_bathymetry=False))
    assert state.inputs["mesh"].state == "rejected"
    assert state.inputs["mesh"].code == "MESH_BED_UNPAINTED"
    assert "Paint the bed onto the mesh" in state.inputs["mesh"].reason


def test_a_supplied_mesh_that_shares_no_ground_with_the_domain_is_refused(
        monkeypatch):
    state = _filled_with(monkeypatch, _built(
        "unstructured_tri", bbox=(-71.52, 41.37, -71.50, 41.39)))
    assert state.inputs["mesh"].state == "rejected"
    assert state.inputs["mesh"].code == "SUPPLIED_COVERAGE_MISMATCH"
    assert "share no ground" in state.inputs["mesh"].reason


def test_what_only_the_mesh_build_reads_is_named_when_a_mesh_is_supplied(
        monkeypatch):
    from trid3nt_server.inputs.run_notes import bind_notes, drain_notes

    state = _filled_with(monkeypatch, _built("unstructured_tri"),
                         mesh_resolution_m=15.0, spill_duration_s=600.0)
    workflow, token = state.workflow, bind_notes()
    workflow._say_what_no_build_read(
        state.env, workflow._mesh("domain", workflow._slots()))
    said = drain_notes(token)
    assert len(said) == 1 and "mesh_resolution_m went unused" in said[0]
    assert "spill_duration_s" not in said[0] and "domain" not in said[0]


def test_a_drawn_geometry_handed_to_a_row_is_the_rows_value(monkeypatch):
    """A mapping that names no layer is not a layer reference: the ring a
    canvas drew is taken whole, never dropped for a fetch."""
    import asyncio

    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.workflows.runtime.fill import Fill, fill

    workflow = TOOL_REGISTRY["telemac_dye_release"].fn.workflow
    monkeypatch.setattr(workflow, "named_rows", lambda held=(): ())
    drawn = {"type": "Polygon", "coordinates": [[*_RING, _RING[0]]]}
    state = asyncio.run(fill(Fill(workflow=workflow),
                             {"domain": drawn, "mesh": _built("unstructured_tri")}))
    assert state.env.supplied["domain"] is drawn
    assert state.inputs["domain"].state == "accepted"


def test_the_fill_holds_a_mesh_to_the_kind_the_engine_states(monkeypatch):
    state = _filled_with(monkeypatch, _built("structured_grid"))
    assert state.inputs["mesh"].state == "rejected"
    assert "solves on" in state.inputs["mesh"].reason
    assert _filled_with(monkeypatch, _built("unstructured_tri")).inputs["mesh"].state == "accepted"


def test_a_row_named_by_a_layer_handle_is_read_at_its_uri(monkeypatch, tmp_path):
    import asyncio
    import json
    from trid3nt_server.render.uri_registry import (
        activate_registry, deactivate_registry, get_uri_registry)
    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.workflows.runtime.fill import Fill, fill

    outline = tmp_path / "outline.geojson"
    outline.write_text(json.dumps({"type": "Polygon", "coordinates": [[*_RING, _RING[0]]]}))
    workflow = TOOL_REGISTRY["telemac_dye_release"].fn.workflow
    monkeypatch.setattr(workflow, "named_rows", lambda held=(): ())
    reg = get_uri_registry("sess-row-handle")
    reg.record("outline-1", uri=str(outline), tool_name="publish_layer")
    token = activate_registry(reg)
    try:
        state = asyncio.run(fill(Fill(workflow=workflow), {
            "domain": {"layer": "outline-1"}, "mesh": _built("unstructured_tri")}))
    finally:
        deactivate_registry(token)
    assert state.env.supplied["domain"] == str(outline)
    assert state.inputs["domain"].state == "accepted"


def test_two_level_rows_are_legal_and_two_bed_rows_refuse():
    from trid3nt_server.inputs.slots import LEVEL

    class TWO_LEVELS:
        domain = Input(DOMAIN).need("hydrography", at="seed")
        bed = Input(BED).need("bathymetry")
        upstream = Input(LEVEL).need("water level series")
        downstream = Input(LEVEL).need("water level series")

    assert _workflow(TWO_LEVELS)._slots()[LEVEL] == "upstream"

    class TWO_BEDS:
        domain = Input(DOMAIN).need("hydrography", at="seed")
        bed = Input(BED).need("bathymetry")
        survey = Input(BED).need("channel survey")

    with pytest.raises(DeclarationError, match="declares 2 bed rows"):
        _workflow(TWO_BEDS)


def test_a_composite_naming_the_mesh_stage_does_not_fill_the_seated_mesh_row():
    """Infiltration reads the mesh the stage builds; were the unstated seat
    filled for it, the launch would find a mesh key and build none."""
    from trid3nt_server.tools import TOOL_REGISTRY

    workflow = TOOL_REGISTRY["telemac_rain_on_grid"].fn.workflow
    assert "mesh" in workflow._named()
    assert "mesh" not in workflow.named_rows()


def test_the_moment_is_the_engines_input_and_no_template_seats_or_coerces_it():
    """The skeleton declares no moment; the engine seats the row and reads it
    through its own coercion, so a template with no coercion still pins one."""
    from trid3nt_server.workflows.runtime import Workflow

    bare = Workflow(metadata=_METADATA, inputs=DATA, template=_template())
    assert "event_time" not in {prm.name for prm in value_inputs(bare)}
    workflow = _workflow()
    assert "event_time" in {prm.name for prm in value_inputs(workflow)}
    pinned = workflow.coercions[-1]({"event_time": "2026-03-04"})
    assert str(pinned["event_time"]).startswith("2026-03-04T00:00:00")


def test_every_registered_template_carries_the_moment_and_the_beds_frame():
    """Agitation among them: it has a bed, so it has the bed's frame."""
    from trid3nt_server.tools import TOOL_REGISTRY
    import trid3nt_server.workflows.telemac.templates  # noqa: F401

    templates = [tool.fn.workflow for tool in TOOL_REGISTRY.values()
                 if hasattr(tool.fn, "workflow")]
    assert len(templates) == 17
    for workflow in templates:
        assert {"event_time", "vertical_frame"} <= {p.name for p in value_inputs(workflow)}


def _filled_by(workflow, values):
    import asyncio

    from trid3nt_server.workflows.runtime.fill import Fill, fill

    return asyncio.run(fill(Fill(workflow=workflow), values))


def test_a_previous_computation_a_measured_settle_cannot_open_on_is_refused_at_the_fill(
        monkeypatch):
    from trid3nt_server.tools import TOOL_REGISTRY

    workflow = TOOL_REGISTRY["telemac_dye_release"].fn.workflow
    monkeypatch.setattr(workflow, "_settled_by",
                        lambda: [SimpleNamespace(kind="harbour")])
    monkeypatch.setattr(workflow, "named_rows", lambda held=(): ())
    state = _filled_by(workflow, {
        "input_mode": "auto",
        "keywords": {"PREVIOUS COMPUTATION FILE": "s3://r/prev.slf"}})
    refused = state.inputs["PREVIOUS_COMPUTATION_FILE"]
    assert refused.state == "rejected"
    assert refused.code == "TELEMAC_CONTINUATION_REFUSED" and not state.ready


def test_a_deck_stating_no_cadence_is_refused_at_the_fill_and_stating_it_clears_the_refusal(
        monkeypatch):
    from types import MappingProxyType

    from trid3nt_server.tools import TOOL_REGISTRY

    workflow = TOOL_REGISTRY["telemac_dye_release"].fn.workflow
    body = workflow.steering
    monkeypatch.setattr(body, "ASSERTED", MappingProxyType(
        {k: v for k, v in body.ASSERTED.items() if k != body.CADENCE}))
    monkeypatch.setattr(workflow, "named_rows", lambda held=(): ())
    state = _filled_by(workflow, {"input_mode": "auto"})
    refused = state.inputs[body.MODULE]
    assert refused.state == "missing" and refused.code == "TELEMAC_SHEET_INCOMPLETE"
    assert "GRAPHIC PRINTOUT PERIOD" in refused.reason and not state.ready
    state = _filled_by(workflow, {
        "input_mode": "auto", "keywords": {"GRAPHIC PRINTOUT PERIOD": 100}})
    assert body.MODULE not in state.inputs


def test_the_bed_only_a_mesh_build_reads_is_not_produced_under_a_supplied_mesh():
    from trid3nt_server.tools import TOOL_REGISTRY

    workflow = TOOL_REGISTRY["telemac_dye_release"].fn.workflow
    assert "bed" in workflow.named_rows()
    assert "bed" not in workflow.named_rows({"mesh"})
    ice = TOOL_REGISTRY["telemac_ice_cover"].fn.workflow
    assert "observe" in ice.named_rows()
    assert {"weather", "level", "discharge"} <= set(ice.named_rows())
