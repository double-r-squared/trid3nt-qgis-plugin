"""Offline unit tests for the sediment-plume engine template under the domain wave.

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
from trid3nt_server.workflows.telemac.workflow import stated
from trid3nt_server.workflows.telemac.templates.sediment_plume import (
    declarations, sediment_plume as template,
)

_TOOL = "telemac_sediment_plume"
_MASS_RESOLVE = ("trid3nt_server.workflows.telemac.helpers.released_mass."
                 "released_mass_kg")


def _workflow():
    return getattr(template, _TOOL).workflow


def _rows():
    return {row.name: row for row in _workflow().data}


def test_registered_on_the_model_surface():
    """The plume front is LIVE, and it is a template of the telemac engine."""
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


def test_the_survey_surface_names_the_depth_it_interpolates():
    """An eHydro row carries more than one number, so the field the surface is
    gridded from is named rather than guessed at."""
    surveyed = _rows()["surveyed_bed"]
    assert surveyed.producer.runner == "derive_survey_surface"
    assert surveyed.producer.kwargs["value_field"] == "depth_below_datum_m"
    assert surveyed.producer.kwargs["resolution_m"].name == "mesh_resolution_m"


def test_an_unsurveyed_domain_continues_and_says_so():
    """No federal navigation project is no published survey, and the sheet says
    which side was missing rather than refusing the run."""
    rows = _rows()
    assert rows["survey"].producer.runner == "fetch_ehydro_surveys"
    for name in ("survey", "carrier"):
        assert rows[name].is_context, name
        assert rows[name].context_sentence


def test_the_carrier_flow_is_one_reading_rather_than_the_grid_it_came_off():
    """The step that opens the channel takes an ingested reading, so which
    segment reports the flow is ranked against the domain's own interior point
    and a record nobody chose from is refused by name."""
    carrier = _rows()["carrier"]
    assert carrier.role == DISCHARGE
    assert carrier.producer.runner == "fetch_noaa_nwm_streamflow"
    assert carrier.coercion["near"] == Ref("domain.centroid")
    assert carrier.coercion["field"] == "streamflow_cms"


def test_an_unplaced_release_sits_along_the_domains_own_centerline():
    """The release step is handed the domain, so an unplaced point has a line to
    sit its fraction along and a supplied one is snapped onto the same line."""
    source = [s for s in _workflow().plan.declared() if s.label == "source"][0]
    assert source.kwargs["domain"] == Ref("domain")
    assert source.kwargs["mesh"] == Ref("mesh")
    assert source.kwargs["fraction"].name == "spill_fraction"


def test_the_workflow_owns_every_stage_this_template_does_not_differ_on():
    """No domain steps, no mesh recipe, no settle, no file names: what the
    template states is the open-channel hydraulics and where the sediment
    enters."""
    workflow = _workflow()
    assert [step.label for step in workflow.plan.steps] == [
        "stated", "mesh", "channel", "source", "settled", "sheet", "solve",
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
    """What a settling-plume question asks: where the sediment went in and how
    concentrated. The flow that carries it is the inflow run's value, which is
    the carrier ROW's, so it is no param of this one."""
    declared = {prm.name for prm in _workflow().params}
    assert "discharge_m3s" not in declared
    assert {"release", "spill_fraction", "spill_duration_s",
            "injected_mass_kg"} <= declared
    # source strength and concentration are the deck's own fixed keyword
    # values now, not a lever this question states.
    assert not declared & {"source_q_m3s", "sediment_concentration_mgl"}


def test_no_param_restates_a_keyword_the_module_already_carries():
    """The clock, the class, the transport and the two forcings are keywords the
    dictionary describes, so the deck states each with its reason and the user
    overrides it by the keyword's own name."""
    declared = {prm.name for prm in _workflow().params}
    assert not declared & {"sim_duration_s", "grain_size_um", "wind_speed_mps",
                           "wind_direction_deg", "rainfall_mm_per_day"}


def test_the_deck_states_the_window_the_settle_is_built_off():
    """A stage settled at one clock under a deck written at another describes a
    different run, so the seconds are the deck's ONE statement."""
    assert template.STEERING.ASSERTED["DURATION"] == 3600.0
    settled = [s for s in _workflow().plan.declared() if s.label == "settled"][0]
    assert settled.kwargs["duration_s"] == Ref("stated.DURATION")
    assert stated(steering=template.STEERING, keywords={})["DURATION"] == 3600.0


def test_the_settling_class_is_keywords_on_gaias_own_body():
    """ONE class, the suspension formula and the advection scheme are GAIA's own
    keywords; what the composite carries is the source concentration the
    dictionary reads in kg/m3 and the question is asked in mg/L."""
    body = template.STEERING.ASSERTED["coupling"][0]["slots"]
    assert body["CLASSES_SEDIMENT_DIAMETERS"] == [3.0e-5]
    assert body["SUSPENSION_TRANSPORT_FORMULA_FOR_ALL_SANDS"] == 3
    assert body["SCHEME_FOR_ADVECTION_OF_SUSPENDED_SEDIMENTS"] == [1]
    assert body["MASS_BALANCE"] is True
    assert (body["suspension"]["concentration_mgl"]
            == declarations.SEDIMENT_CONCENTRATION_MGL)


def test_the_deck_states_the_four_source_keywords_by_name():
    """One element per source, in the engine's own positional order: where it
    enters, and how much of the fixed strength/concentration discharges."""
    asserted = template.STEERING.ASSERTED
    assert asserted["ABSCISSAE_OF_SOURCES"] == [Ref("source.at.0")]
    assert asserted["ORDINATES_OF_SOURCES"] == [Ref("source.at.1")]
    assert asserted["WATER_DISCHARGE_OF_SOURCES"] == [declarations.SOURCE_Q_M3S]
    assert asserted["VALUES_OF_THE_TRACERS_AT_THE_SOURCES"] == [
        declarations.SEDIMENT_CONCENTRATION_MGL]


def test_the_sources_composite_writes_the_sources_file():
    """``sources`` carries the window and horizon, and reads the discharge and
    tracer values off the sheet by the keywords stated above - it writes no
    number of its own."""
    asserted = template.STEERING.ASSERTED["sources"]
    assert asserted["window_s"].name == "spill_duration_s"
    assert asserted["until_s"] == Ref("settled.until_s")
    assert asserted["q"] == Ref("WATER_DISCHARGE_OF_SOURCES")
    assert asserted["tracers"] == Ref("VALUES_OF_THE_TRACERS_AT_THE_SOURCES")


def test_the_deleted_release_params_are_refused():
    """``source_q_m3s`` and ``sediment_concentration_mgl`` fed Release and
    nothing else; neither is declared, so neither is on the generated
    signature, on the sheet, or resolvable by name any more."""
    import inspect

    signature = inspect.signature(getattr(template, _TOOL))
    declared = {prm.name for prm in _workflow().params}
    for gone in ("source_q_m3s", "sediment_concentration_mgl"):
        assert gone not in signature.parameters, gone
        assert gone not in declared, gone
        with pytest.raises(AttributeError):
            getattr(template.PARAMS, gone)


def test_a_calm_dry_deck_states_no_wind_and_no_rain_at_all():
    """A zero speed and an absent rate write NOTHING: the settling this question
    reads is the current's, and a caller who wants either sets the keyword."""
    from trid3nt_server.workflows.telemac.modules import T2D

    for name in ("wind", "rain"):
        slots, _files = T2D.COMPOSITES[name].expand(
            template.STEERING.ASSERTED[name])
        assert slots == {}, name


def test_the_injected_mass_is_derived_from_the_pulse_the_sheet_states():
    """The deposited fraction is held against what the pulse PUT IN, and that
    number is the shared release-mass relation rather than a local one."""
    from trid3nt_server.workflows.runtime import ParamRef

    injected = [p for p in _workflow().params if p.name == "injected_mass_kg"][0]
    assert injected.door == "derived"
    # The relation lives in helpers, and what this question hands it is DECLARED
    # rather than wrapped in a function of the template package's own.
    assert injected.resolve == _MASS_RESOLVE
    bound = injected.resolve_kwargs
    assert bound["discharge_m3s"] == declarations.SOURCE_Q_M3S
    assert bound["concentration_mgl"] == declarations.SEDIMENT_CONCENTRATION_MGL
    assert isinstance(bound["duration_s"], ParamRef)
    assert bound["duration_s"].name == "spill_duration_s"
    assert template.ANSWER["deposit_fraction"].op == "over"


def test_the_derived_mass_resolves_to_the_shared_release_relation():
    """A DERIVED param is a declaration until the resolver can call what it
    names: the declared bindings reach the shared relation at the deck's own two
    fixed numbers and the window the question asks for."""
    import asyncio

    from trid3nt_server.workflows.runtime import resolve_params

    sheet = asyncio.run(resolve_params(_workflow().params, {}))
    assert sheet.value_of("injected_mass_kg") == 240.0


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


@pytest.mark.parametrize("name", ["T2"])
def test_every_published_read_carries_its_caption(name):
    """CAPTIONS covers the reads this template PLACES and nothing else: what the
    modules write is published under the engine's own names."""
    assert name in template.CAPTIONS
    assert set(template.CAPTIONS) == {
        p.variable or p.kind for p in template.OUTPUTS}


def test_the_package_holds_the_template_its_declarations_and_its_corpus():
    """A template package carries no function of its own: the mass relation the
    deposited fraction is held against lives in the shared helpers."""
    from pathlib import Path

    package = Path(template.__file__).parent
    assert sorted(p.name for p in package.glob("*.py")) == [
        "__init__.py", "declarations.py", "sediment_plume.py"]
    assert (package / "corpus.yaml").is_file()
