"""Offline unit tests for the oil-spill engine template under the domain wave.

No solver / no network: the three engine-neutral slots it declares, the bed it
composes out of a survey and a terrain surface, the stages the workflow owns on
its behalf, the params it no longer restates, and the keyword opinions it moved
into the deck. Live end-to-end is the canary, whose packet is the evidence.
"""

from __future__ import annotations

import pytest

from trid3nt_server.workflows.runtime import Ref
from trid3nt_server.workflows.runtime.data import BED, DISCHARGE, DOMAIN
from trid3nt_server.workflows.runtime.levers import LEVER_NAMES
from trid3nt_server.workflows.telemac.templates.oil_spill import (
    oil_spill as template,
)

_TOOL = "telemac_oil_spill"


def _workflow():
    return getattr(template, _TOOL).workflow


def _rows():
    return {row.name: row for row in _workflow().data}


def test_registered_on_the_model_surface():
    """The oil front is LIVE, and it is a template of the telemac engine."""
    import trid3nt_server.main as _main

    _main._import_tools_registry()
    from trid3nt_server.tools import TOOL_REGISTRY

    assert _TOOL in TOOL_REGISTRY
    assert getattr(template, _TOOL).parked is None

    md = _workflow().metadata
    assert md.engine == "telemac"
    assert md.tier == "template"
    assert {r.param for r in (md.resolution_specs or ())} == {"mesh_resolution_m"}


def test_the_domain_is_one_slot_the_reach_producer_only_prefers():
    """A river question names the reach fetcher, and a supplied polygon still
    wins: the row carries a role, so it reaches the wire as an argument."""
    domain = _rows()["domain"]
    assert domain.role == DOMAIN
    assert domain.geometry == "polygon"
    assert domain.producer.runner == "fetch_river_reach"
    # The seed is the release POINT; a place name is geocoded before the call.
    assert domain.producer.kwargs["seed_point"] == [Ref("release.lon"),
                                                    Ref("release.lat")]
    assert domain.fills_from_user


def test_the_bed_is_one_row_the_merge_derive_made_of_two_rows():
    """The survey where it measured, the terrain everywhere else - ONE bed row,
    composed in the DATA body by the merge derive, never a second bed row and
    never a fallback on set_bed."""
    rows = _rows()
    assert [name for name, row in rows.items() if row.role == BED] == ["bed"]
    bed = rows["bed"]
    assert bed.producer.runner == "derive_merge_rasters"
    assert bed.producer.kwargs["primary"].path == "surveyed_bed"
    assert bed.producer.kwargs["fallback"].path == "terrain"
    assert rows["surveyed_bed"].producer.kwargs["points"].path == "survey"
    assert rows["terrain"].producer.runner == "fetch_dem"


def test_an_unsurveyed_domain_continues_and_says_so():
    """No federal navigation project is no published survey, and the sheet says
    which side was missing rather than refusing the run."""
    rows = _rows()
    assert rows["survey"].producer.runner == "fetch_ehydro_surveys"
    for name in ("survey", "carrier"):
        assert rows[name].is_context, name
        assert rows[name].context_sentence


def test_the_carrier_is_one_reading_and_never_the_grid_it_came_from():
    """The step that opens the channel takes an ingested Observation: which site
    reports the flow and how old the sample is are the slot's to decide."""
    carrier = _rows()["carrier"]
    assert carrier.role == DISCHARGE
    assert carrier.producer.runner == "fetch_noaa_nwm_streamflow"
    assert carrier.coercion["near"] == Ref("domain.centroid")
    assert carrier.coercion["field"] == "streamflow_cms"


def test_the_release_is_settled_against_the_domain_it_may_be_unplaced_in():
    """An unplaced release sits its fraction along the domain's own centerline
    companion, and a supplied point is snapped onto that same line."""
    kwargs = [s for s in _workflow().plan.steps if s.label == "source"][0].kwargs
    assert kwargs["domain"] == Ref("domain")
    assert kwargs["fraction"].name == "spill_fraction"


def test_the_workflow_owns_every_stage_this_template_does_not_differ_on():
    """No domain steps, no mesh recipe, no settle, no file names: what the
    template states is the open-channel hydraulics and where the oil enters."""
    workflow = _workflow()
    assert [step.label for step in workflow.plan.steps] == [
        "mesh", "channel", "source", "settled", "sheet", "solve", "outputs"]
    assert not hasattr(template, "MESH")


def test_the_mesh_is_built_over_the_slots_at_the_runtimes_own_lever():
    """The extent is the domain row, the bed is the merged row, and the runs
    come off the runs slot the domain producer's two end transects fill."""
    from trid3nt_server.workflows.runtime.plan import DataRef

    recipe = [s for s in _workflow().plan.steps if s.label == "mesh"][0].kwargs["mesh"]
    assert recipe["extent"] == DataRef("domain")
    # A placeholder refuses ``==`` by design, so the read is named by its name.
    assert recipe["resolution_m"].name == "mesh_resolution_m"
    ops = {op["op"]: op["kwargs"] for op in recipe["ops"]}
    assert ops["set_bed"] == {"source": DataRef("bed")}
    assert ops["set_boundary_roles"] == {"runs": DataRef("runs")}


def test_the_baseline_params_are_the_runtimes_and_are_not_restated():
    """A template declares no domain twin, no keyword twin and no lever of its
    own; the three levers are seated on it because the workflow owns its stages."""
    declared = [prm.name for prm in _workflow().params]
    for gone in ("location", "bbox", "river_geometry_uri", "reach_length_km",
                 "friction_law", "friction_coefficient", "output_interval_min",
                 "evaporation_mm_per_day", "rainfall_gridmet_window"):
        assert gone not in declared, gone
    assert set(LEVER_NAMES) <= set(declared)
    # Every lever is on the sheet; the ones this question does not state for
    # itself are seated at the end, in the order the runtime declares them, and
    # one it differs on keeps its own place and its own opinion of the value.
    own = [prm.name for prm in template.PARAMS.__dict__.values()
           if getattr(prm, "name", None)]
    assert declared[:len(own)] == own
    seated = [name for name in LEVER_NAMES if name not in own]
    assert declared[len(own):] == seated


def test_the_question_keeps_only_its_own_inputs():
    """What an oil question asks: where the oil went in, which oil, how much
    dissolved and how the slick is drawn. The flow that carries it is the inflow
    run's value, which is the carrier ROW's, so it is no param of this one."""
    declared = {prm.name for prm in _workflow().params}
    assert "discharge_m3s" not in declared
    assert {"release", "spill_fraction", "spill_duration_s",
            "source_q_m3s", "oil_concentration_mgl", "oil_type", "n_drogues",
            "drogues_period_s", "oil_release_step", "wind_speed_mps",
            "wind_direction_deg", "rainfall_mm_per_day",
            "sim_duration_s"} <= declared


def test_the_roughness_is_the_decks_own_opinion_stated_as_keywords():
    """The stage is derived as a normal depth AT this roughness, so the number
    the deck is written at and the number it was derived at are one number."""
    asserted = template.STEERING.ASSERTED
    assert asserted["LAW_OF_BOTTOM_FRICTION"] == 3
    assert asserted["FRICTION_COEFFICIENT"] == 33.0
    assert [step.name for step in _workflow().plan.steps
            if step.name == "channel"] == ["channel"]


def test_the_slots_reach_the_wire_as_arguments():
    """A drawn harbour, a supplied polygon and a surveyed bed all arrive the same
    way: the roles are on the generated signature."""
    import inspect

    signature = inspect.signature(getattr(template, _TOOL))
    assert "domain" in signature.parameters
    assert "bed" in signature.parameters
    assert "location" not in signature.parameters


@pytest.mark.parametrize("name", ["T1", "drogues"])
def test_every_published_read_carries_its_caption(name):
    """CAPTIONS covers the reads this template PLACES and nothing else: what the
    module writes is published under the engine's own names."""
    assert name in template.CAPTIONS
    assert set(template.CAPTIONS) == {
        p.variable or p.kind for p in template.OUTPUTS}
