"""Offline unit tests for the sediment-plume engine template.

No solver / no network: the need rows it declares, the stages the workflow
owns on its behalf, the params it no longer restates, and the keyword
opinions it moved into the deck. Live end-to-end is the canary, whose packet
is the evidence.
"""

from __future__ import annotations

import pytest

from trid3nt_server.workflows.runtime.plan import DataRef
from trid3nt_server.workflows.runtime import Ref
from trid3nt_server.workflows.runtime.levers import LEVER_NAMES
from trid3nt_server.workflows.telemac.workflow import stated
from trid3nt_server.workflows.telemac.templates.sediment_plume import (
    declarations, sediment_plume as template,
)

_TOOL = "telemac_sediment_plume"
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


def test_the_domain_is_one_need_row_asked_at_the_release_point():
    """The domain names the CLASS it needs and the point it is asked at; which
    fetcher answers - a reach walked downstream, a waterbody at a point - is
    the match's, ranked in the coverage order."""
    domain = _rows()["domain"]
    assert domain.role == "domain" and domain.data_class == "hydrography"
    assert domain.coercion["near"] == Ref("release")
    assert domain.span_km == 6.0
    assert domain.fills_from_user


def test_the_bed_is_one_need_row_the_match_composes():
    """One row, one class: the measurement where it measured, the terrain
    under the rest, composed by the match's own bed rule - never a producer or
    a merge stated on the template."""
    bed = _rows()["bed"]
    assert bed.role == "bed" and bed.data_class == "bathymetry"
    assert bed.producer is None
    assert [row.name for row in _workflow().data if row.role == "bed"] == ["bed"]


def test_the_discharge_is_a_need_row_the_match_ranks_against_the_domain():
    """The flow that carries the pulse is ONE measured value the run opens on,
    not a layer: a NEED row, absent-is-context, and the user's own number wins
    over any record."""
    discharge = _rows()["discharge"]
    assert (discharge.role == "discharge"
            and discharge.data_class == "discharge series")
    assert discharge.is_context
    assert "National Water Model" in discharge.context_sentence
    assert discharge.producer is None


def test_an_unplaced_release_sits_along_the_domains_own_centerline():
    """The release step is handed the domain, so an unplaced point has a line to
    sit its fraction along and a supplied one is snapped onto the same line."""
    source = [s for s in _workflow().plan.declared() if s.label == "source"][0]
    assert source.kwargs["domain"] == DataRef("domain")
    assert source.kwargs["mesh"] == Ref("mesh")
    assert source.kwargs["fraction"].name == "spill_fraction"


def test_the_workflow_owns_every_stage_this_template_does_not_differ_on():
    """No domain steps, no mesh recipe, no settle, no file names: what the
    template states is the open-channel hydraulics and where the sediment
    enters."""
    workflow = _workflow()
    assert [step.label for step in workflow.plan.steps] == [
        "stated", "mesh", "mesh_files", "channel", "source", "settled", "sheet", "solve",
        "outputs"]
    assert not hasattr(template, "MESH")


def test_the_mesh_is_built_over_the_slots_at_the_runtimes_own_lever():
    """The extent is the domain row, the bed is the matched row, and the runs
    ride on the domain's own producer since there is no runs row any more."""
    recipe = [s for s in _workflow().plan.steps if s.label == "mesh"][0].kwargs["mesh"]
    assert recipe["extent"] == DataRef("domain")
    # A placeholder refuses ``==`` by design, so the read is named by its name.
    assert recipe["resolution_m"].name == "mesh_resolution_m"
    ops = {op["op"]: op["kwargs"] for op in recipe["ops"]}
    assert ops["set_bed"] == {"source": DataRef("bed")}
    assert ops["set_boundary_roles"] == {"runs": DataRef("domain")}
    assert "runs" not in _rows()


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
    the discharge ROW's, so it is no param of this one."""
    declared = {prm.name for prm in _workflow().params}
    assert "discharge_m3s" not in declared
    assert {"release", "spill_fraction", "spill_duration_s"} <= declared
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
    dictionary reads in kg/m3 and the question is asked in mg/L. The class is
    30 um fine silt, the fraction this question is asked of - the one that
    travels as a plume at the currents a release reach carries."""
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


def test_every_slot_a_user_can_fill_reaches_the_wire():
    """A supplied polygon supersedes the matched reach and a supplied raster
    supersedes the matched bed, so both slots are arguments though both name a
    class."""
    assert {row.name for row in _workflow().data if row.fills_from_user} == {
        "domain", "bed", "discharge", "level"}


def test_every_published_read_carries_its_caption():
    """CAPTIONS covers what the module PLACES, plus the sentence for the one
    DATA row that states one, keyed by the row's own name."""
    assert "T2" in template.CAPTIONS
    assert set(template.CAPTIONS) == {"T2", "discharge"}


def test_the_package_holds_the_template_its_declarations_and_its_corpus():
    """A template package carries no function of its own: the mass relation the
    deposited fraction is held against lives in the shared helpers."""
    from pathlib import Path

    package = Path(template.__file__).parent
    assert sorted(p.name for p in package.glob("*.py")) == [
        "__init__.py", "declarations.py", "sediment_plume.py"]
    assert (package / "corpus.yaml").is_file()
