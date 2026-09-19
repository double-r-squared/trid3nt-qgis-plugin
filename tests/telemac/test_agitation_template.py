"""The harbour template's world is SLOTS: a domain, a bed and a structure.

Nothing here is about the wave. What is pinned is how the water it solves over
is stated - outlined, or cut out of a box with the mapped coastline - that the
bed it prefers can be superseded by what a user surveyed, and that the one piece
of geometry work it needs is the shared structure ingestion rather than a
function of its own."""

from __future__ import annotations

import pytest

from trid3nt_server.workflows.runtime.data import BED, DOMAIN, EXTENT


def _door():
    from trid3nt_server.tools import TOOL_REGISTRY

    return TOOL_REGISTRY["artemis_harbor_agitation"].fn.workflow.plan_decl


def _rows():
    from trid3nt_server.tools import TOOL_REGISTRY

    return {row.name: row
            for row in TOOL_REGISTRY["artemis_harbor_agitation"].fn.workflow.data}


def test_the_water_is_cut_out_of_a_box_with_the_mapped_coastline():
    """Nothing fetches a harbour basin as a polygon, so the one producer this
    question prefers is the CUT: a coastline over the box the user drew, and the
    water it leaves. A basin the user outlines supersedes it."""
    rows = _rows()
    domain = rows["domain"]
    assert (domain.role, domain.geometry) == (DOMAIN, "polygon")
    assert domain.fills_from_user
    assert domain.producer.runner == "derive_water_polygon"
    assert repr(domain.producer.kwargs["coastline"]) == "DataRef('coast')"
    assert domain.producer.kwargs["extent"].path == "box.bbox"
    assert rows["coast"].producer.runner == "fetch_osm_coastline"


def test_the_box_is_an_extent_slot_the_canvas_offers_a_rectangle_for():
    """Giving the domain a producer takes away its own draw gate, so the window
    the cut is made in is the slot with the canvas ask: one EXTENT row, read as
    four numbers by whatever takes a box."""
    from trid3nt_server.inputs.slots import DRAW_PURPOSES

    rows = _rows()
    box = rows["box"]
    assert (box.role, box.geometry, box.producer) == (EXTENT, "rectangle", None)
    assert DRAW_PURPOSES[EXTENT][0] == "rectangle"
    assert rows["coast"].producer.kwargs["bbox"].path == "box.bbox"


def test_the_bed_is_the_surveyed_sea_floor_merged_over_the_terrain():
    """A bed is ONE row: the surveyed sea floor where it sounded and the terrain
    everywhere else, composed by the merge derive before the slot - a cut water
    polygon follows the coastline to the metre and a delivered tile ends on its
    own grid, so the two disagree by a node at the rim. A survey the user holds
    is the same slot, so the row is on the wire beside its producer."""
    rows = _rows()
    bed = rows["bed"]
    assert bed.role == BED and bed.fills_from_user
    assert bed.producer.runner == "derive_merge_rasters"
    assert repr(bed.producer.kwargs["primary"]) == "DataRef('seafloor')"
    assert repr(bed.producer.kwargs["fallback"]) == "DataRef('terrain')"
    assert rows["seafloor"].producer.runner == "fetch_topobathy"
    assert rows["seafloor"].producer.kwargs["target_crs"] == "EPSG:4326"
    assert rows["terrain"].producer.runner == "fetch_dem"


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
    footprint = next(step for step in _door().domain if step.label == "footprint")
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


@pytest.mark.parametrize("name", ["box", "domain", "bed", "structure"])
def test_every_slot_reaches_the_wire(name: str):
    """A slot the caller cannot name is a slot only a fetcher can fill."""
    import inspect

    from trid3nt_server.tools import TOOL_REGISTRY

    fn = TOOL_REGISTRY["artemis_harbor_agitation"].fn
    assert name in inspect.signature(fn).parameters
    assert f"{name}: " in (fn.__doc__ or "")


def test_the_transect_follows_the_direction_the_run_states():
    """The transect is read along the wave, so it is placed on the KEYWORD and
    not on the number the deck was authored at: the read resolves through the
    filled sheet, which is where a stated direction lands."""
    from trid3nt_server.workflows.runtime import Ref

    row = _rows()["transect"]
    assert row.producer.kwargs["bearing_deg"] == Ref(
        "sheet.DIRECTION_OF_WAVE_PROPAGATION")


def test_the_settle_reads_the_wave_the_run_is_solved_at():
    """The boundary file is stamped at the run's own period and direction, so
    the step reads the resolved floor rather than the body's own numbers."""
    from trid3nt_server.workflows.runtime import Ref
    from trid3nt_server.workflows.telemac.templates.agitation import agitation
    from trid3nt_server.workflows.telemac.workflow import stated

    settle = _door().settle
    assert settle.kwargs["wave_period_s"] == Ref("stated.WAVE_PERIOD")
    assert settle.kwargs["wave_direction_deg"] == Ref(
        "stated.DIRECTION_OF_WAVE_PROPAGATION")
    floor = stated(steering=agitation.STEERING,
                   keywords={"DIRECTION OF WAVE PROPAGATION": 160.0})
    assert floor["DIRECTION_OF_WAVE_PROPAGATION"] == 160.0
    assert floor["WAVE_PERIOD"] == agitation.STEERING.ASSERTED["WAVE_PERIOD"]
