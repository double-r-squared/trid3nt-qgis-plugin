"""The mobile-bed template as VALUES: the slots it stands on and the plan the
workflow builds from them.

Offline: nothing is fetched, meshed or solved. What is proved is the
DECLARATION - one domain row, one composed bed row, the runtime levers it no
longer restates, the keyword opinions it states as keywords, and the two stages
it still owns because open-channel hydraulics and a release point are measured
against the accepted mesh.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from trid3nt_server.workflows.telemac.workflow import stated
from trid3nt_server.workflows.runtime.levers import LEVER_NAMES
from trid3nt_server.workflows.telemac.modules import T2D
from trid3nt_server.workflows.telemac.templates.bed_scour import (
    bed_scour,
)

_MODULE = bed_scour
#: The registered tool, found by what it CARRIES rather than by its name, so the
#: question-class rename moves the module and this test moves with it.
_WORKFLOW = next(value.workflow for value in vars(_MODULE).values()
                 if getattr(value, "workflow", None) is not None)
#: The package the corpus sits in, read off the module so a rename moves both.
_PACKAGE = Path(_MODULE.__file__).parent

#: What the domain and twins waves took off this template: the keyword twins the
#: module's dictionary already describes, the domain twins the slots replaced,
#: and the levers the runtime declares once.
_DISSOLVED = {"location", "bbox", "river_geometry_uri", "reach_length_km",
              "friction_coefficient", "friction_law", "output_interval_min",
              "evaporation_mm_per_day", "rainfall_gridmet_window",
              "sim_duration_s", "wind_speed_mps", "wind_direction_deg",
              "rainfall_mm_per_day", "grain_size_um", "bed_thickness_m",
              "bedload_formula", "morphological_factor",
              "source_q_m3s", "tracer_concentration_mgl"}


def _rows() -> dict:
    return {row.name: row for row in _WORKFLOW.data}


def _recipe():
    from trid3nt_server.mesh.tool import recipe_from_plan_value

    return recipe_from_plan_value(_step("mesh").kwargs["mesh"])


def test_the_domain_is_one_need_row_asked_at_the_release_point():
    """The domain names the CLASS it needs and the point it is asked at; which
    fetcher answers - a reach walked downstream, a waterbody at a point - is the
    match's, ranked in the coverage order."""
    domain = _rows()["domain"]
    assert domain.role == "domain" and domain.data_class == "hydrography"
    assert domain.coercion["near"] == "release"
    assert domain.span_km == 6.0


def test_the_bed_is_one_need_row_the_match_composes():
    """One row, one class: the measurement where it measured, the terrain under
    the rest, composed by the match's own bed rule - never a producer or a merge
    stated on the template."""
    bed = _rows()["bed"]
    assert bed.role == "bed" and bed.data_class == "bathymetry"
    assert bed.producer is None
    assert [row.name for row in _WORKFLOW.data if row.role == "bed"] == ["bed"]


def test_the_discharge_is_a_need_row_the_match_ranks_against_the_domain():
    """The flow that moves the bed is ONE measured value the run opens on, not a
    layer: a NEED row, absent-is-context, and the user's own number wins over
    any record."""
    discharge = _rows()["discharge"]
    assert discharge.role == "discharge" and discharge.data_class == "discharge series"
    assert discharge.is_context
    assert "National Water Model" in discharge.context_sentence
    assert discharge.producer is None


def test_the_deck_names_its_own_files_and_the_door_does_not_restate_them():
    """The run directory's names ARE the deck's own statements, so the settle
    step above reads them off the body rather than being told them twice."""
    asserted = _MODULE.STEERING.ASSERTED
    assert asserted["GEOMETRY_FILE"] == "domain.slf"
    assert asserted["BOUNDARY_CONDITIONS_FILE"] == "domain.cli"
    assert asserted["RESULTS_FILE"] == "r2d_domain.slf"


def test_the_runtime_levers_are_seated_and_no_baseline_param_is_restated():
    declared = [prm.name for prm in _WORKFLOW.params]
    # Every lever is on the sheet; the ones this question does not state for
    # itself are seated at the end, in the order the runtime declares them, and
    # one it differs on keeps its own place and its own opinion of the value.
    from trid3nt_server.workflows.runtime import param_rows

    own = [prm.name for prm in param_rows(_MODULE.PARAMS)]
    assert set(LEVER_NAMES) <= set(declared)
    seated = [name for name in LEVER_NAMES if name not in own]
    assert declared[-len(seated):] == seated
    assert _DISSOLVED.isdisjoint(declared)


def test_the_bed_the_deck_states_is_gaias_own_keywords_on_the_coupled_body():
    """The class diameter, the erodible stock, the hiding factor and the
    morphological factor are GAIA's keywords, stated by name on the body; only
    the GRADATION the dictionary lacks arrives as a value. The transport law
    the deck wants IS the dictionary's own, so the deck states nothing."""
    slots = _MODULE.STEERING.ASSERTED["coupling"][0]["slots"]
    assert slots["CLASSES_SEDIMENT_DIAMETERS"] == [1.0e-4]
    assert slots["LAYERS_INITIAL_THICKNESS"] == [5.0]
    assert "BED_LOAD_TRANSPORT_FORMULA_FOR_ALL_SANDS" not in slots
    assert slots["HIDING_FACTOR_FORMULA"] == 1
    assert slots["MORPHOLOGICAL_FACTOR"] == 10.0
    assert slots["bed"]["gradation"] == "sediment_gradation"


def test_the_marker_is_stated_as_the_four_source_keywords_by_name():
    """No ``releases`` composite: the deck states WHERE, HOW MUCH and AT WHAT
    CONCENTRATION as the engine's own keywords, one element per source."""
    asserted = _MODULE.STEERING.ASSERTED
    assert "ABSCISSAE_OF_SOURCES" not in asserted
    assert "ORDINATES_OF_SOURCES" not in asserted
    assert asserted["WATER_DISCHARGE_OF_SOURCES"] == [8.0]
    assert asserted["VALUES_OF_THE_TRACERS_AT_THE_SOURCES"] == [100.0]
    assert "releases" not in asserted


def test_the_sources_composite_writes_only_the_sources_file():
    """``Sources(window_s=)`` reads the discharge and the tracer value
    off the sheet by the keywords stated above, and writes the SOURCES FILE."""
    from trid3nt_server.workflows.telemac.modules.telemac2d import SOURCES_FILENAME

    slots, files = T2D.COMPOSITES["sources"].expand(
        {"window_s": 300.0, "settled": {"until_s": 3600.0},
         "q": [8.0], "tracers": [100.0]})
    assert slots == {"SOURCES_FILE": SOURCES_FILENAME}
    assert list(files) == [SOURCES_FILENAME]


def test_the_spill_window_is_the_questions_own_input_and_stays_a_param():
    asserted = _MODULE.STEERING.ASSERTED
    assert asserted["sources"]["window_s"] == "spill_duration_s"


def test_a_calm_dry_deck_writes_no_wind_and_no_rain_keyword():
    """This question asks what the carrier flow does to the bed. A zero speed and
    an absent rate each expand to nothing, so neither block reaches the deck and
    a user who wants one sets the keyword by its own name."""
    for name in ("wind", "rain"):
        slots, _files = T2D.COMPOSITES[name].expand(
            _MODULE.STEERING.ASSERTED[name])
        assert not slots, name


def test_the_boundary_values_read_the_measured_walk_and_the_open_channel_step():
    """WHICH list carries a value is the mesh's own walk; what the two liquid
    faces carry is the flow and the level the open-channel step measured - one
    read of the settle's own record, which carries the walk, the two numbers,
    the windows behind them and the clock the file is written on."""
    measured = _MODULE.STEERING.ASSERTED["boundaries"]["measured"]
    assert measured == "settled"
    assert not {"INITIAL_DEPTH", "INITIAL_ELEVATION"} & set(_MODULE.STEERING.ASSERTED)


def test_every_slot_a_user_can_fill_reaches_the_wire():
    """A drawn estuary supersedes the matched reach and a supplied raster
    supersedes the matched bed, so both slots are arguments though both name a
    class."""
    assert {row.name for row in _WORKFLOW.data if row.fills_from_user} == {
        "domain", "bed", "discharge", "level"}


def test_the_only_published_read_is_the_placed_one_and_it_has_its_caption():
    """What the run WRITES is the module's own table. This template publishes
    the marker's history and captions that; CAPTIONS also carries the sentence
    for the one DATA row that states one, keyed by the row's own name."""
    assert [primitive.variable for primitive in _MODULE.OUTPUTS] == ["T1"]
    assert set(_MODULE.CAPTIONS) == {"T1", "discharge"}


def test_the_corpus_key_is_the_registered_name():
    corpus = yaml.safe_load((_PACKAGE / "corpus.yaml").read_text(encoding="utf-8"))
    assert list(corpus) == [_WORKFLOW.name]
    assert all(phrase == phrase.strip() and phrase.isascii()
               for phrase in corpus[_WORKFLOW.name])


def test_the_routing_text_names_the_question_class_not_one_body_of_water():
    """A bed scours under an estuary as readily as under a river, so the first
    sentence of the routing text says the question, never the body."""
    routing = _MODULE.DOC["routing"]
    assert "reach" not in routing.split(".")[0]
    assert "supply `domain`" in routing


def test_the_routing_text_names_the_keywords_this_deck_has_opinions_about():
    """A user overrides an opinion by the keyword's own name, so the names have
    to be in the text the model routes on."""
    routing = _MODULE.DOC["routing"]
    for keyword in ("DURATION", "MORPHOLOGICAL FACTOR", "CLASSES SEDIMENT",
                    "LAYERS INITIAL THICKNESS", "BED-LOAD TRANSPORT FORMULA"):
        assert keyword in routing, keyword
