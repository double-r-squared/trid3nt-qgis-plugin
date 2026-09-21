"""The harbour template's world is SLOTS: a domain, a bed and a structure.

Nothing here is about the wave. What is pinned is how the water it solves over
is stated - outlined, or cut out of a box with the mapped coastline - that the
bed it prefers can be superseded by what a user surveyed, and that the one piece
of geometry work it needs is the shared structure ingestion rather than a
function of its own."""

from __future__ import annotations

import pytest

from trid3nt_server.workflows.runtime.data import BED, DOMAIN, EXTENT


def _declared():
    """The template module the registration was handed, by its own names."""
    from trid3nt_server.tools import TOOL_REGISTRY

    return TOOL_REGISTRY["artemis_harbor_agitation"].fn.workflow.template


def _step(label):
    """One step of the plan the WORKFLOW built, by the name it was given."""
    from trid3nt_server.tools import TOOL_REGISTRY

    plan = TOOL_REGISTRY["artemis_harbor_agitation"].fn.workflow.plan
    return next(step for step in plan.declared() if step.label == label)


def _rows():
    from trid3nt_server.tools import TOOL_REGISTRY

    return {row.name: row
            for row in TOOL_REGISTRY["artemis_harbor_agitation"].fn.workflow.data}


def test_the_domain_is_asked_for_as_the_land_water_edge():
    """Nothing fetches a harbour basin as a polygon, so this question asks for
    the EDGE - the hydrography feature a coastline is, read as a line - and the
    slot cuts the window with it. The row names no producer and no module path:
    a template states a need, and the cut is the slot's ingestion."""
    rows = _rows()
    domain = rows["domain"]
    assert domain.role == DOMAIN
    assert domain.fills_from_user
    assert domain.producer is None
    assert (domain.data_class, domain.observes, domain.geometry) == (
        "hydrography", "coastline", "polyline")


def test_the_coastline_is_the_only_hydrography_source_this_row_can_take():
    """The feature the row asks for is what selects: a waterbody, a reach and a
    traced basin are the same class and none of them is a land-water edge."""
    from trid3nt_server.tools.search.match import (
        Need, match, sources_with_coverage)

    domain = _rows()["domain"]
    choice = match(Need(slot="domain", data_class=domain.data_class,
                        lon=-71.36, lat=41.36, of=domain.observes,
                        geometry=domain.geometry), sources_with_coverage())
    assert choice.picked == "fetch_osm_features"
    assert [row.fetcher for row in choice.rows if not row.excluded] == [
        "fetch_osm_features"]


def test_the_extent_is_a_slot_the_canvas_offers_a_rectangle_for():
    """The window the cut is made in is the slot with the canvas ask: one EXTENT
    row, read as four numbers by whatever takes a box, and the domain slot is
    told it rather than the deck stating a box twice."""
    from trid3nt_server.inputs.slots import SLOTS

    rows = _rows()
    extent = rows["extent"]
    assert (extent.role, extent.geometry, extent.producer) == (EXTENT, "rectangle",
                                                                None)
    assert SLOTS[EXTENT].draw[0] == "rectangle"


def test_the_bed_is_the_class_it_is_defined_over():
    """A bed is ONE row that states the CLASS it needs - the measurement where
    something sounded it, the terrain everywhere else - rather than naming the
    fetcher that composes it; the match, not the template, decides which source
    reaches this domain. A survey the user holds is the same slot."""
    rows = _rows()
    bed = rows["bed"]
    assert bed.role == BED and bed.fills_from_user
    assert bed.data_class == "bathymetry"
    assert bed.producer is None


def test_the_mesh_is_cut_from_the_domain_polygon_and_not_from_a_box():
    """The domain polygon's own edge IS the shoreline every sizing function
    measures, so the recipe hands the mesher that polygon; a box is not a domain
    and the mesher refuses one by name."""
    from trid3nt_server.workflows.telemac.templates.agitation.agitation import MESH

    assert repr(MESH.extent) == "DataRef('domain')"
    set_bed = next(op for op in MESH.ops if op.fn == "set_bed")
    assert repr(set_bed.kwargs["source"]) == "DataRef('bed')"


def test_the_footprint_is_the_shared_structure_ingestion():
    """A centreline bounds no area, and widening one is not this template's
    work: the ingestion every structure passes through is what the step names."""
    footprint = _step("footprint")
    assert footprint.runner == "trid3nt_server.inputs.structure.structure"
    assert repr(footprint.kwargs["value"]) == "DataRef('structure')"


def test_the_template_declares_no_domain_twin():
    """The domain, its bed and its resolution are the slots' - a param of the
    same name beside them would be two statements of one thing."""
    from trid3nt_server.tools import TOOL_REGISTRY

    params = {p.name for p in
              TOOL_REGISTRY["artemis_harbor_agitation"].fn.workflow.params}
    assert params.isdisjoint({"location", "bbox", "river_geometry_uri",
                              "reach_length_km"})


@pytest.mark.parametrize("name", ["extent", "domain", "bed", "structure"])
def test_every_slot_reaches_the_wire(name: str):
    """A slot the caller cannot name is a slot only a fetcher can fill."""
    import inspect

    from trid3nt_server.tools import TOOL_REGISTRY

    fn = TOOL_REGISTRY["artemis_harbor_agitation"].fn
    assert name in inspect.signature(fn).parameters
    assert f"{name}: " in (fn.__doc__ or "")


def test_the_transect_is_measured_off_the_structure_the_question_asks_about():
    """The line a read runs ACROSS a structure along is no row: the workflow
    measures it off the structure, along the wave the run is solved at, so it
    resolves through the floor the keyword lands on and names no tool."""
    from trid3nt_server.workflows.runtime import Ref

    assert "transect" not in _rows()
    step = _step("transect")
    assert step.runner == "trid3nt_server.inputs.structure.transect"
    assert step.kwargs["bearing_deg"] == Ref(
        "stated.DIRECTION_OF_WAVE_PROPAGATION")
    assert step.kwargs["convention"] == "trig"


def test_the_settle_reads_the_wave_the_run_is_solved_at():
    """The boundary file is stamped at the run's own period and direction, so
    the step reads the resolved floor rather than the body's own numbers."""
    from trid3nt_server.workflows.runtime import Ref
    from trid3nt_server.workflows.telemac.templates.agitation import agitation
    from trid3nt_server.workflows.telemac.workflow import stated

    settle = _step("settled")
    assert settle.kwargs["wave_period_s"] == Ref("stated.WAVE_PERIOD")
    assert settle.kwargs["wave_direction_deg"] == Ref(
        "stated.DIRECTION_OF_WAVE_PROPAGATION")
    floor = stated(steering=agitation.STEERING,
                   keywords={"DIRECTION OF WAVE PROPAGATION": 160.0})
    assert floor["DIRECTION_OF_WAVE_PROPAGATION"] == 160.0
    assert floor["WAVE_PERIOD"] == agitation.STEERING.ASSERTED["WAVE_PERIOD"]
