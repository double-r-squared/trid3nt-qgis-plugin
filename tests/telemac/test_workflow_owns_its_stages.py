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
    DataRef,
    ParamRef,
    PlanValidationError,
    Ref,
    tool,
)
from trid3nt_server.workflows.runtime.levers import (
    LEVER_NAMES,
    lever,
)
from trid3nt_server.workflows.telemac.modules import T2D
from trid3nt_server.workflows.telemac.workflow import TelemacWorkflow

_METADATA = AtomicToolMetadata(
    name="telemac_water_temperature", ttl_class="live-no-cache",
    source_class="workflow_dispatch", cacheable=False, engine="telemac",
    tier="template")


class STEERING(T2D):
    """The deck: a body of water under a week of weather."""

    GEOMETRY_FILE = "domain.slf"
    BOUNDARY_CONDITIONS_FILE = "domain.cli"
    RESULTS_FILE = "r2d_domain.slf"
    TITLE = Ref("settled.title")
    # The clock is a KEYWORD the module carries, so the deck states it and the
    # settle reads it off the deck rather than off a param beside it.
    DURATION = 604800.0


class PARAMS:
    mesh_resolution_m = lever("mesh_resolution_m", default=25.0)


class DATA:
    domain = Data.need("hydrography", at=Ref("seed"))
    bed = Data.need("bathymetry")


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
        bed = Data.need("bathymetry")

    with pytest.raises(PlanValidationError, match="declares no domain"):
        _workflow(data=NO_DOMAIN)


def test_a_template_with_no_bed_refuses_at_import():
    class NO_BED:
        domain = Data.need("hydrography")

    with pytest.raises(PlanValidationError, match="declares no bed"):
        _workflow(data=NO_BED)
