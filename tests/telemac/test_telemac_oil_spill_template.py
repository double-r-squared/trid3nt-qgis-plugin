"""Offline unit tests for the oil-spill engine template under the domain wave.

No solver / no network: the three engine-neutral slots it declares, the bed it
composes out of a survey and a terrain surface, the stages the workflow owns on
its behalf, the params it no longer restates, and the keyword opinions it moved
into the deck. Live end-to-end is the canary, whose packet is the evidence.
"""

from __future__ import annotations

import pytest

from trid3nt_server.workflows.runtime.plan import DataRef
from trid3nt_server.workflows.runtime import Ref
from trid3nt_server.workflows.runtime.data import BED, DISCHARGE, DOMAIN
from trid3nt_server.workflows.runtime.levers import LEVER_NAMES
from trid3nt_server.workflows.telemac.modules import T2D
from trid3nt_server.workflows.telemac.workflow import stated
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


def test_the_domain_is_the_class_it_needs_and_a_supplied_polygon_still_wins():
    """A river question needs a water body; which source cuts one here is the
    match's. The row carries a role, so it reaches the wire as an argument."""
    domain = _rows()["domain"]
    assert domain.role == DOMAIN
    assert domain.producer is None
    assert domain.data_class == "hydrography"
    # The seed is the release POINT; a place name is geocoded before the call.
    assert domain.coercion["near"] == Ref("release")
    assert domain.fills_from_user


def test_the_bed_is_one_row_stating_the_class_it_needs():
    """ONE bed row, stating bathymetry - the measurement where it measured, the
    terrain under the rest, which is the RUNTIME's rule and not a question's."""
    rows = _rows()
    assert [name for name, row in rows.items() if row.role == BED] == ["bed"]
    bed = rows["bed"]
    assert bed.producer is None
    assert bed.data_class == "bathymetry"


def test_the_level_is_matched_and_its_absence_is_legal():
    """A reach with no reported water level has no uniform-flow depth to derive,
    so the slot states its class and stays optional."""
    level = _rows()["level"]
    assert level.producer is None
    assert level.data_class == "water level series"
    assert level.is_optional


def test_the_discharge_is_one_reading_and_never_the_grid_it_came_from():
    """The step that opens the channel takes an ingested Observation: which site
    reports the flow and how old the sample is are the slot's to decide."""
    discharge = _rows()["discharge"]
    assert discharge.role == DISCHARGE
    assert discharge.producer is None
    assert discharge.data_class == "discharge series"
    assert discharge.coercion["near"] is None
    assert "to_units" not in discharge.coercion


def test_the_release_is_settled_against_the_domain_it_may_be_unplaced_in():
    """An unplaced release sits its fraction along the domain's own centerline
    companion, and a supplied point is snapped onto that same line."""
    kwargs = [s for s in _workflow().plan.steps if s.label == "source"][0].kwargs
    assert kwargs["domain"] == DataRef("domain")
    assert kwargs["fraction"].name == "spill_fraction"


def test_the_workflow_owns_every_stage_this_template_does_not_differ_on():
    """No domain steps, no mesh recipe, no settle, no file names: what the
    template states is the open-channel hydraulics and where the oil enters."""
    workflow = _workflow()
    assert [step.label for step in workflow.plan.steps] == [
        "stated", "mesh", "mesh_files", "channel", "source", "settled", "sheet", "solve",
        "outputs"]
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
    assert ops["set_boundary_roles"] == {"runs": DataRef("domain")}


def test_the_baseline_params_are_the_runtimes_and_are_not_restated():
    """A template declares no domain twin, no keyword twin and no lever of its
    own; the three levers are seated on it because the workflow owns its stages."""
    declared = [prm.name for prm in _workflow().params]
    for gone in ("location", "bbox", "river_geometry_uri", "reach_length_km",
                 "friction_law", "friction_coefficient", "output_interval_min",
                 "evaporation_mm_per_day", "rainfall_gridmet_window",
                 "sim_duration_s", "n_drogues", "drogues_period_s",
                 "wind_speed_mps", "wind_direction_deg", "rainfall_mm_per_day"):
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
    run's value, which is the discharge ROW's, so it is no param of this one."""
    declared = {prm.name for prm in _workflow().params}
    assert "discharge_m3s" not in declared
    assert {"release", "spill_fraction", "spill_duration_s", "oil_type",
            "oil_release_step"} <= declared
    # the source strength and its dissolved concentration are the deck's own
    # fixed keyword values now, not a lever this question states.
    assert not declared & {"source_q_m3s", "oil_concentration_mgl"}


def test_the_source_is_stated_as_the_four_source_keywords_by_name():
    """No ``releases`` composite: the deck states WHERE, HOW MUCH and AT WHAT
    CONCENTRATION as the engine's own keywords, one element per source."""
    asserted = template.STEERING.ASSERTED
    assert asserted["ABSCISSAE_OF_SOURCES"] == [Ref("source.at.0")]
    assert asserted["ORDINATES_OF_SOURCES"] == [Ref("source.at.1")]
    assert asserted["WATER_DISCHARGE_OF_SOURCES"] == [8.0]
    assert asserted["VALUES_OF_THE_TRACERS_AT_THE_SOURCES"] == [100.0]
    assert "releases" not in asserted


def test_the_sources_composite_writes_only_the_sources_file():
    """``Sources(window_s=, until_s=)`` reads the discharge and the tracer value
    off the sheet by the keywords stated above, and writes the SOURCES FILE."""
    from trid3nt_server.workflows.telemac.modules.telemac2d import SOURCES_FILENAME

    slots, files = T2D.COMPOSITES["sources"].expand(
        {"window_s": 300.0, "until_s": 3600.0,
         "q": [8.0], "tracers": [100.0]})
    assert slots == {"SOURCES_FILE": SOURCES_FILENAME}
    assert list(files) == [SOURCES_FILENAME]


def test_the_spill_window_is_the_questions_own_input_and_stays_a_param():
    asserted = template.STEERING.ASSERTED
    assert asserted["sources"]["window_s"].name == "spill_duration_s"
    assert asserted["sources"]["until_s"].path == "settled.until_s"


def test_the_ex_release_params_are_gone_from_the_declared_wire():
    """The discharge and its dissolved concentration were only ever a twin
    inside the release composite; they leave with it, off both the declared
    params and the wire the model is offered - a caller naming the old name
    gets no such slot."""
    import inspect

    from trid3nt_server.tools import TOOL_REGISTRY

    declared = {p.name for p in _workflow().params}
    wire = set(inspect.signature(TOOL_REGISTRY[_TOOL].fn).parameters)
    gone = {"source_q_m3s", "oil_concentration_mgl"}
    assert not (declared & gone) and not (wire & gone)


def test_the_roughness_is_the_decks_own_opinion_stated_as_keywords():
    """The stage is derived as a normal depth AT this roughness, so the number
    the deck is written at and the number it was derived at are one number."""
    asserted = template.STEERING.ASSERTED
    assert asserted["LAW_OF_BOTTOM_FRICTION"] == 3
    assert asserted["FRICTION_COEFFICIENT"] == 33.0
    assert [step.name for step in _workflow().plan.steps
            if step.name == "channel"] == ["channel"]


def test_the_clock_and_the_track_are_the_modules_own_keywords():
    """The window, the float count and how often their positions are written
    are keywords telemac2d carries, so the deck states them by their own names
    and the settle is built off the same statement."""
    asserted = template.STEERING.ASSERTED
    assert asserted["DURATION"] == 3600.0
    assert asserted["MAXIMUM_NUMBER_OF_DROGUES"] == 100
    # In SOLVER STEPS, like GRAPHIC PRINTOUT PERIOD beside it - no seconds and
    # no division by the settled step.
    assert asserted["PRINTOUT_PERIOD_FOR_DROGUES"] == 60
    settled = [s for s in _workflow().plan.steps if s.label == "settled"][0]
    assert settled.kwargs["duration_s"] == Ref("stated.DURATION")
    assert stated(steering=template.STEERING, keywords={})["DURATION"] == 3600.0


def test_a_calm_dry_deck_writes_no_wind_and_no_rain_at_all():
    """This question asks what the CURRENT does with the slick: a zero speed and
    an absent rate state nothing, so the keywords are the user's to set by
    name rather than a zero the deck put in front of them."""
    slots, _ = template.STEERING.COMPOSITES["wind"].expand(
        template.STEERING.wind)
    assert slots == {}
    slots, _ = template.STEERING.COMPOSITES["rain"].expand(
        template.STEERING.rain)
    assert slots == {}


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
    """CAPTIONS covers every read this template PLACES; the rows keyed beside
    them are the sentences the journal opens a measured slot with."""
    assert name in template.CAPTIONS
    assert {p.variable or p.kind for p in template.OUTPUTS} <= set(
        template.CAPTIONS)
