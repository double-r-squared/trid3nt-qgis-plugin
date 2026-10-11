"""The stages the WORKFLOW builds from a template module's declared slots.

Offline: nothing is built and nothing is solved. What is proved is the PLAN a
template gets when it states only what differs from the base.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.workflows.runtime import (
    Data,
    DeclarationError,
    tool,
)
from trid3nt_server.workflows.runtime.levers import (
    LEVER_NAMES,
    lever,
)
from trid3nt_server.workflows.telemac.modules import T2D
from trid3nt_server.workflows.telemac.workflow import TelemacWorkflow
from trid3nt_server.inputs.slots import BED, DOMAIN

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


class PARAMS:
    mesh_resolution_m = lever("mesh_resolution_m", default=25.0)


class DATA:
    domain = Data(DOMAIN).need("hydrography", at="seed")
    bed = Data(BED).need("bathymetry")


def _template(data: type = DATA, params: type = PARAMS) -> SimpleNamespace:
    """A template module, as the workflow reads one: its own names and nothing
    around them."""
    return SimpleNamespace(STEERING=STEERING, PARAMS=params, DATA=data)


def _workflow(data: type = DATA, params: type = PARAMS) -> TelemacWorkflow:
    return TelemacWorkflow(metadata=_METADATA, params=params,
                           template=_template(data, params), data=data,
                           levers=TelemacWorkflow.levers())


def _mesh(workflow: TelemacWorkflow):
    return _step(workflow, "mesh")


def test_the_runtime_levers_are_seated_so_the_template_states_none_of_them():
    workflow = _workflow()
    # The template's own row for a lever keeps its place; every lever it does
    # not state is seated after it, in the order the runtime declares them.
    assert [prm.name for prm in workflow.params] == [
        "mesh_resolution_m", *(n for n in LEVER_NAMES if n != "mesh_resolution_m")]


def test_the_domain_and_the_bed_reach_the_wire_as_the_slots_they_are():
    """What the user hands in supersedes the producer the template preferred, so
    both slots are arguments even though both name a source."""
    supplied = workflow_wire(_workflow())
    assert {"domain", "bed"} <= supplied


def workflow_wire(workflow: TelemacWorkflow) -> set[str]:
    return {decl.name for decl in workflow.data if decl.fills_from_user}


def test_a_template_with_no_domain_refuses_at_import():
    class NO_DOMAIN:
        bed = Data(BED).need("bathymetry")

    with pytest.raises(DeclarationError, match="declares 0 domain rows"):
        _workflow(data=NO_DOMAIN)


def test_a_template_with_no_bed_refuses_at_import():
    class NO_BED:
        domain = Data(DOMAIN).need("hydrography")

    with pytest.raises(DeclarationError, match="declares 0 bed rows"):
        _workflow(data=NO_BED)


def _built(kind: str):
    from trid3nt_server.tools.mesh.artifact import MeshArtifact

    return MeshArtifact(
        mesh_id="01MINE", name="ring", mode="om2d",
        display_uri="s3://m/01MINE/mesh.2dm", crs_authid="EPSG:32610",
        has_bathymetry=True, node_count=4, element_count=2,
        bbox=(-122.68, 45.51, -122.66, 45.52), engine_files={},
        provenance={"recipe": {"mesher": "om2d", "kind": kind}})


def _filled_with(monkeypatch, mesh):
    import asyncio

    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.workflows.runtime.fill import Fill, fill

    workflow = TOOL_REGISTRY["telemac_dye_release"].fn.workflow
    # Only what the call states is filled: the rows the template names go out to sources.
    monkeypatch.setattr(workflow, "named_rows", lambda: ())
    ring = [[-122.6769, 45.5153], [-122.6615, 45.5153],
            [-122.6615, 45.5198], [-122.6769, 45.5198]]
    return asyncio.run(fill(Fill(workflow=workflow),
                            {"domain": ring, "mesh": mesh}))


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


def test_two_level_rows_are_legal_and_two_bed_rows_refuse():
    from trid3nt_server.inputs.slots import LEVEL

    class TWO_LEVELS:
        domain = Data(DOMAIN).need("hydrography", at="seed")
        bed = Data(BED).need("bathymetry")
        upstream = Data(LEVEL).need("water level series").optional()
        downstream = Data(LEVEL).need("water level series").optional()

    assert _workflow(data=TWO_LEVELS)._slots()[LEVEL] == "upstream"

    class TWO_BEDS:
        domain = Data(DOMAIN).need("hydrography", at="seed")
        bed = Data(BED).need("bathymetry")
        survey = Data(BED).need("channel survey")

    with pytest.raises(DeclarationError, match="declares 2 bed rows"):
        _workflow(data=TWO_BEDS)
