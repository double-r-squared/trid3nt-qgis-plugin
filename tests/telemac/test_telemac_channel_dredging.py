"""The maintenance-dredge template as VALUES: the slots it stands on and the
plan the workflow builds from them.

Offline: nothing is fetched, meshed or solved. What is proved is the
DECLARATION - one domain row, one composed bed row, the reading the inflow
opens on, the two areas the template names no source for, the runtime levers it
no longer restates, and the two stages it still owns because this question
measures them on top of the domain.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from trid3nt_server.workflows.telemac.workflow import stated
from trid3nt_server.workflows.runtime.levers import LEVER_NAMES
from trid3nt_server.workflows.telemac.templates.channel_dredging import (
    channel_dredging,
)

_WORKFLOW = channel_dredging.telemac_channel_dredging.workflow
_STEERING = channel_dredging.STEERING
#: The package the corpus sits in, read off the module so a rename moves both.
_PACKAGE = Path(channel_dredging.__file__).parent

#: What the domain wave took off every template: the keyword twins the module's
#: dictionary already describes, the domain twins the slots replaced, and the
#: two levers the runtime declares once.
_DISSOLVED = {"location", "bbox", "river_geometry_uri", "reach_length_km",
              "friction_coefficient", "friction_law", "output_interval_min",
              "sim_duration_s", "time_origin", "grain_size_um",
              "bed_thickness_m", "bedload_formula", "morphological_factor"}


def _rows() -> dict:
    return {row.name: row for row in _WORKFLOW.data}


def _recipe():
    from trid3nt_server.mesh.tool import recipe_from_plan_value

    return recipe_from_plan_value(_steps()["mesh"].kwargs["mesh"])


def test_the_domain_is_one_need_row_asked_at_the_seed_point():
    """The domain names the CLASS it needs and the point it is asked at; which
    fetcher answers - a reach walked downstream, a waterbody at a point - is the
    match's, ranked in the coverage order."""
    domain = _rows()["domain"]
    assert domain.role == "domain" and domain.data_class == "hydrography"
    assert domain.coercion["near"] == "seed_point"
    assert domain.span_km == channel_dredging._REACH_LENGTH_KM


def test_the_bed_is_one_need_row_the_stricter_class_the_match_composes():
    """The dredge asks the STRICTER class - the maintained prism, not the water
    around it - and the match's own bed rule composes it: the measurement where
    it measured, the terrain under the rest, never a producer or a merge stated
    on the template."""
    bed = _rows()["bed"]
    assert bed.role == "bed" and bed.data_class == "channel survey"
    assert bed.producer is None
    assert [row.name for row in _WORKFLOW.data if row.role == "bed"] == ["bed"]


def test_the_flow_the_channel_is_dredged_under_is_a_need_row_not_a_record():
    """The inflow carries ONE number: a NEED row, absent-is-context, and the
    user's own number wins over any record."""
    discharge = _rows()["discharge"]
    assert discharge.role == "discharge" and discharge.data_class == "discharge series"
    assert discharge.is_context
    assert "National Water Model" in discharge.context_sentence
    assert discharge.producer is None


def test_the_bed_the_dredger_cuts_is_stated_as_the_module_s_own_keywords():
    """The class, the stock and the morphological factor are keywords GAIA
    carries, so the deck states them and a user overrides each by its own name;
    the composite carries only the gradation and the dredge. The transport law
    the deck wants IS the dictionary's own, so the deck states nothing."""
    slots = _STEERING.ASSERTED["coupling"][0]["slots"]
    assert slots["CLASSES_SEDIMENT_DIAMETERS"] == [2.0e-4]
    assert slots["LAYERS_INITIAL_THICKNESS"] == [channel_dredging._BED_STOCK_M]
    assert "BED_LOAD_TRANSPORT_FORMULA_FOR_ALL_SANDS" not in slots
    assert slots["MORPHOLOGICAL_FACTOR"] == 10.0
    assert slots["MASS_BALANCE"] is True
    assert slots["bed"] == {"gradation": None, "presets": None}


def test_the_runtime_levers_are_seated_and_no_baseline_param_is_restated():
    declared = [prm.name for prm in _WORKFLOW.params]
    # Every lever is on the sheet; the ones this question does not state for
    # itself are seated at the end, in the order the runtime declares them, and
    # one it differs on keeps its own place and its own opinion of the value.
    from trid3nt_server.workflows.runtime import param_rows

    own = [prm.name for prm in param_rows(channel_dredging.PARAMS)]
    assert set(LEVER_NAMES) <= set(declared)
    seated = [name for name in LEVER_NAMES if name not in own]
    assert declared[-len(seated):] == seated
    assert _DISSOLVED.isdisjoint(declared)


def test_every_slot_a_user_can_fill_reaches_the_wire():
    """A fairway the port supplies supersedes the matched reach, a surveyed
    raster supersedes the matched bed and a stated flow supersedes the reading,
    so all name a class or a role though none names a producer; the two areas
    are supplied and the line names none either."""
    assert {row.name for row in _WORKFLOW.data if row.fills_from_user} == {
        "domain", "line", "bed", "discharge", "level", "dredge_area",
        "dump_area"}


def test_the_two_areas_name_no_source_and_take_polygons():
    rows = _rows()
    for name in ("dredge_area", "dump_area"):
        assert rows[name].producer is None and rows[name].geometry == "polygon"
        assert not rows[name].is_optional


def test_a_dredge_releases_nothing_so_it_prescribes_no_tracer():
    assert _STEERING.ASSERTED["boundaries"]["tracers"] == []


def test_the_two_measured_rows_carry_their_noun_on_the_journal():
    """CAPTIONS carries the sentence for the two DATA rows the run journal opens
    on, keyed by the row's own name."""
    assert channel_dredging.CAPTIONS == {"discharge": "a streamflow",
                                         "level": "a water level"}


def test_the_corpus_key_is_the_registered_name():
    corpus = yaml.safe_load((_PACKAGE / "corpus.yaml").read_text(encoding="utf-8"))
    assert list(corpus) == [_WORKFLOW.name]
    assert all(phrase == phrase.strip() and phrase.isascii()
               for phrase in corpus[_WORKFLOW.name])
