"""The nearshore-wave template: the world it stands on, and the deck it hands
TOMAWAC.

Offline: nothing here starts a solve. What is pinned is the sea state reaching
the boundary keywords as three separate numbers, the run length the module's own
spelling gives it, the result file it is read off, the open edge the spectrum
enters across, and the answers being the module's own rows rather than a
template's arithmetic.
"""

from __future__ import annotations

import pytest

from trid3nt_server.workflows.runtime import Ref
from trid3nt_server.workflows.runtime.data import BED, DOMAIN, EXTENT, LEVEL, WAVE
from trid3nt_server.workflows.runtime.plan import declared_reads
from trid3nt_server.workflows.telemac.modules import WAC, fill
from trid3nt_server.workflows.telemac.modules.tomawac import RESULT_FILENAME

_TOOL = "tomawac_nearshore_waves"
#: The hour the deck marches, as the two keywords the module spells it in.
_HOUR_S = 3600.0


def _workflow():
    from trid3nt_server.tools import TOOL_REGISTRY

    return TOOL_REGISTRY[_TOOL].fn.workflow


def _rows():
    return {row.name: row for row in _workflow().data}


def _step(label):
    return next(step for step in _workflow().plan.declared()
                if step.label == label)


def test_the_world_is_the_water_a_coastline_leaves_inside_the_window():
    """A wave run solves over water, and nothing maps a stretch of open coast as
    a polygon: the question asks for the EDGE and the slot cuts the drawn window
    with it, exactly as the harbour question does."""
    rows = _rows()
    assert rows["extent"].role == EXTENT
    assert rows["extent"].geometry == "rectangle"
    domain = rows["domain"]
    assert (domain.role, domain.data_class, domain.observes, domain.geometry) \
        == (DOMAIN, "hydrography", "coastline", "polyline")
    assert domain.producer is None
    assert rows["bed"].role == BED
    assert rows["bed"].data_class == "bathymetry"


def test_the_sea_state_is_a_row_on_the_reserved_wave_slot():
    """The boundary is forced at something somebody MEASURED, so it is a DATA row
    of the class a buoy network publishes, ranked at the point the question names
    - not a number the template states."""
    wave = _rows()["wave"]
    assert wave.role == WAVE
    assert wave.data_class == "wave series"
    assert wave.producer is None
    assert wave.coercion["near"] == Ref("seed")


def test_the_buoys_are_what_answers_the_sea_state_row():
    """The class the row states has to reach a source that measures it here; a
    row no source serves is a question that cannot be asked."""
    from trid3nt_server.tools.search.match import (
        Need, match, sources_with_coverage)

    wave = _rows()["wave"]
    choice = match(Need(slot="wave", data_class=wave.data_class,
                        lon=-75.75, lat=36.18,
                        opens="2026-02-10T12:00:00Z",
                        until="2026-02-10T13:00:00Z"), sources_with_coverage())
    assert choice.picked == "fetch_ndbc_buoys"


def test_the_boundary_keywords_read_the_slot_by_name():
    """Three separate keywords, each reading the value the wave slot's ingestion
    made for it: a height as measured, a FREQUENCY off the period, and the
    bearing the waves run TOWARD. The deck states no conversion of its own."""
    from trid3nt_server.workflows.telemac.templates.nearshore_waves import (
        nearshore_waves as template)

    deck = template.STEERING
    assert deck.ASSERTED["BOUNDARY_SIGNIFICANT_WAVE_HEIGHT"] == Ref("wave.height_m")
    assert deck.ASSERTED["BOUNDARY_PEAK_FREQUENCY"] == Ref("wave.peak_frequency_hz")
    assert deck.ASSERTED["BOUNDARY_MAIN_DIRECTION_1"] == Ref("wave.direction_deg")
    # The water the depths are counted down from is the tide slot's, as one
    # value: TOMAWAC carries no free surface of its own.
    assert _rows()["level"].role == LEVEL
    assert deck.ASSERTED["INITIAL_STILL_WATER_LEVEL"] == Ref("level.value")


def test_the_run_length_is_the_product_the_module_spells_it_as():
    """TOMAWAC names no DURATION, so the settle and the window a matched record
    is filtered on both read the step and the count this deck states."""
    assert _workflow().run_window_s({}) == _HOUR_S
    assert _step("settled").kwargs["duration_s"] == [Ref("stated.TIME_STEP"),
                                                     Ref("stated.NUMBER_OF_TIME_STEP")]
    # The floor still moves it: a caller who states a longer run moves the
    # window every series source is matched against.
    assert _workflow().run_window_s({"NUMBER OF TIME STEP": 720}) == 2 * _HOUR_S


def test_the_run_is_read_off_the_file_the_module_spells_its_results_in():
    """The deck names its result under 2D RESULTS FILE, and nothing restates the
    name: the solve declares the same file the primitives open."""
    assert _step("solve").kwargs["results"] == [RESULT_FILENAME]
    assert WAC.RESULT_FILE == RESULT_FILENAME


def test_the_seaward_edge_is_opened_so_the_spectrum_has_somewhere_to_enter():
    """A wave run whose whole rim is wall is a domain no sea state can reach, so
    the recipe designates every ocean stretch of the outline open."""
    from trid3nt_server.workflows.telemac.templates.nearshore_waves import (
        nearshore_waves as template)

    ops = [op.fn for op in template.MESH.ops]
    assert "identify_ocean_boundary_sections" in ops
    assert "set_bed" in ops
    assert template.MESH_ON == "domain"


def test_the_deck_states_the_physics_the_question_is_about():
    """Depth-induced breaking is what takes a shoaling wave down; without it the
    run reports a height that keeps growing into water too shallow to hold it,
    and LECDON writes no breaking row at all."""
    sheet = fill(WAC, **{name: value for name, value in
                         _deck().ASSERTED.items()
                         if not any(declared_reads(value, Ref))})
    stated = dict(sheet.resolved())
    assert stated["DEPTH-INDUCED BREAKING DISSIPATION"] == 1
    assert stated["BOTTOM FRICTION DISSIPATION"] == 1
    assert stated["TYPE OF BOUNDARY DIRECTIONAL SPECTRUM"] == 6
    # The domain opens EMPTY and fills from the boundary: a spectrum laid over
    # the whole of it at t = 0 is wave energy nobody measured.
    assert stated["TYPE OF INITIAL DIRECTIONAL SPECTRUM"] == 0
    assert not sheet.required()


def test_the_answers_are_the_module_s_own_rows():
    """Every number a reader is handed is a measure of one variable TOMAWAC
    wrote, so nothing here is a template's arithmetic over the engine's."""
    from trid3nt_server.workflows.telemac.templates.nearshore_waves import (
        nearshore_waves as template)

    named = {name: measure.primitive.variable
             for name, measure in template.ANSWER.items()}
    assert named["hs_max_m"] == "HM0"
    assert named["peak_period_at_station_s"] == "TPD"
    assert named["direction_at_station_deg"] == "DMOY"
    assert named["breaking_rate_max_per_s"] == "BETA"
    assert named["breaker_dissipation_max_m2s"] == "DBR"
    for token in ("HM0", "TPD", "DMOY", "BETA", "DBR"):
        assert token in WAC.MODULE_OUTPUT
    assert [p.variable or p.kind
            for p in template.OUTPUTS] == ["HM0", "TPD", "DMOY", "spectrum"]


def test_every_charted_variable_is_captioned():
    """A chart titled by a token nobody named is a picture a reader cannot read."""
    from trid3nt_server.workflows.telemac.templates.nearshore_waves import (
        nearshore_waves as template)

    assert all((p.variable or p.kind) in template.CAPTIONS
               for p in template.OUTPUTS)


def test_the_spectrum_is_recorded_where_the_station_settled():
    """The three charted numbers are statistics OF a spectrum, so the deck names
    the station as a printout point and publishes the spectrum itself. The
    keyword takes the abscissae apart from the ordinates, and a settled point is
    one value with an order - so each is read off the pair by position."""
    from trid3nt_server.workflows.telemac.templates.nearshore_waves import (
        nearshore_waves as template)

    asserted = template.STEERING.ASSERTED
    assert asserted["ABSCISSAE_OF_SPECTRUM_PRINTOUT_POINTS"] == [
        Ref("station.at.0")]
    assert asserted["ORDINATES_OF_SPECTRUM_PRINTOUT_POINTS"] == [
        Ref("station.at.1")]
    # The spectra are read off the file the deck named, never off the wave field.
    assert asserted["PUNCTUAL_RESULTS_FILE"] != asserted["ED_RESULTS_FILE"]
    read = next(p for p in template.OUTPUTS if p.kind == "spectrum")
    assert read.at is template._STATION and read.publish == "chart"


def _deck():
    from trid3nt_server.workflows.telemac.templates.nearshore_waves import (
        nearshore_waves as template)

    return template.STEERING


@pytest.mark.parametrize("named", ["seed", "station", "mesh_resolution_m",
                                   "event_time", "compute_class"])
def test_the_question_declares_the_levers_a_caller_reaches_it_through(named):
    assert named in {param.name for param in _workflow().params}
