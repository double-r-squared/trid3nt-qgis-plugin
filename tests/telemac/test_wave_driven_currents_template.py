"""The wave-driven-current template: the world it stands on, and the two decks
it hands TELEMAC-2D and TOMAWAC.

Offline: nothing here starts a solve. What is pinned is the sea state reaching
the WAVE deck's boundary keywords, the coupling arming the host's own switch,
the two decks marching one clock, and the answers being the two modules' own
rows rather than a template's arithmetic.
"""

from __future__ import annotations

import pytest

from trid3nt_server.inputs.wave import Wave
from trid3nt_server.workflows.runtime import Ref
from trid3nt_server.workflows.runtime.data import BED, DOMAIN, EXTENT, LEVEL, WAVE
from trid3nt_server.workflows.telemac.modules import T2D, WAC, fill
from trid3nt_server.workflows.telemac.modules.module import SlotRefused
from trid3nt_server.workflows.telemac.authoring.boundaries import (
    LIQUID_BOUNDARIES_FILENAME)
from trid3nt_server.workflows.telemac.modules.tomawac import RESULT_FILENAME
from trid3nt_server.workflows.telemac.templates.wave_driven_currents import (
    wave_driven_currents as template)
from trid3nt_server.workflows.telemac.workflow import run_bodies

_TOOL = "tomawac_wave_driven_currents"
_HOUR_S = 3600.0
_STATION = {"at": [500000.0, 4500000.0], "lon": -71.51, "lat": 41.36,
            "name": "Current station"}


def _workflow():
    from trid3nt_server.tools import TOOL_REGISTRY

    return TOOL_REGISTRY[_TOOL].fn.workflow


def _rows():
    return {row.name: row for row in _workflow().data}


def _sheet(**floor):
    """The host body filled with what its refs resolve to, coupling and all."""
    return fill(template.STEERING, **floor, produced={
        "settled": {"title": "COAST DOMAIN",
                    "liquid_boundary_order": ["open"],
                    "liquid_boundary_prescribes": ["elevation"],
                    "opening": "CONSTANT ELEVATION", "depth_m": None,
                    "level_m": 0.4, "outflow_stage_m": 0.4},
        "station": _STATION,
        "wave": Wave(height_m=1.6, peak_frequency_hz=0.125,
                     direction_deg=40.0, peak_period_s=8.0,
                     from_direction_deg=220.0),
        "level": 0.4})


def test_the_world_is_the_water_a_coastline_leaves_inside_the_window():
    """The same world the standalone wave question stands on: a window, the
    coast that cuts it, the bed both modules are solved over, the sea state and
    the tide. The level is NOT optional here - the seaward rim is a prescribed
    elevation, so a run nobody measured a tide for has nothing to hold it at."""
    rows = _rows()
    assert rows["extent"].role == EXTENT and rows["extent"].supplied
    assert rows["domain"].role == DOMAIN
    assert rows["domain"].data_class == "hydrography"
    assert rows["domain"].kind == "coastline"
    assert rows["bed"].role == BED and rows["bed"].data_class == "bathymetry"
    assert rows["wave"].role == WAVE and rows["wave"].data_class == "wave series"
    assert rows["wave"].coercion["near"] == Ref("seed")
    assert rows["level"].role == LEVEL and not rows["level"].is_optional


def test_the_sea_state_forces_the_wave_deck_and_never_the_host():
    """A boundary spectrum is TOMAWAC's statement. The host has no keyword for
    it, so the three numbers ride on the coupled body and a floor that means to
    move one names the body it belongs to."""
    (body,) = _sheet().coupled
    assert body["module"] == "tomawac"
    stated = dict(body["slots"])
    assert stated["BOUNDARY_SIGNIFICANT_WAVE_HEIGHT"] == 1.6
    assert stated["BOUNDARY_PEAK_FREQUENCY"] == 0.125
    assert stated["BOUNDARY_MAIN_DIRECTION_1"] == 40.0
    assert stated["TYPE_OF_BOUNDARY_DIRECTIONAL_SPECTRUM"] == 6
    with pytest.raises(SlotRefused):
        T2D.identify("BOUNDARY SIGNIFICANT WAVE HEIGHT")


def test_the_coupling_arms_the_hosts_wave_driven_currents():
    """TOMAWAC appends no row to its host: the wave forces reach it as a
    momentum source, which the engine adds only under this switch. A coupled
    deck that left it off would solve the wave field and throw it away."""
    stated = dict(_sheet().resolved())
    assert stated["WAVE DRIVEN CURRENTS"] is True
    assert stated["COUPLING WITH"] == "TOMAWAC"
    assert stated["TOMAWAC STEERING FILE"]
    assert [b.MODULE for b in run_bodies(template.STEERING)] == [
        "telemac2d", "tomawac"]


def test_both_decks_march_one_clock():
    """A coupled run whose modules step apart is two runs on one mesh. The host
    names a window and the wave deck names a step and a count, which is how each
    module spells a run length - and the three numbers are one hour."""
    stated = dict(_sheet().resolved())
    assert stated["DURATION"] == _HOUR_S
    host_step = float(stated["TIME STEP"])
    period = int(stated["COUPLING PERIOD FOR TOMAWAC"])
    (body,) = _sheet().coupled
    wave = dict(body["slots"])
    assert wave["TIME_STEP"] == host_step * period
    assert wave["TIME_STEP"] * wave["NUMBER_OF_TIME_STEP"] == _HOUR_S


def test_the_wave_deck_is_solved_on_the_hosts_own_mesh():
    """Same-mesh coupling: the wave field is solved over the water the host
    solves over, so the geometry and the boundary walk are the host's own and
    the wave module writes only its own result beside them."""
    (body,) = _sheet().coupled
    stated = dict(body["slots"])
    assert stated["GEOMETRY_FILE"] == template.STEERING.GEOMETRY_FILE
    assert stated["BOUNDARY_CONDITIONS_FILE"] == \
        template.STEERING.BOUNDARY_CONDITIONS_FILE
    assert stated["ED_RESULTS_FILE"] == RESULT_FILENAME
    assert template.RESULTS == (template.STEERING.RESULTS_FILE, RESULT_FILENAME)


def test_the_breaking_is_on_because_the_breaking_is_the_forcing():
    """The longshore current is the gradient of the radiation stress breaking
    leaves behind: a wave field that never loses energy drives nothing."""
    stated = dict(_sheet().coupled[0]["slots"])
    assert stated["DEPTH_INDUCED_BREAKING_DISSIPATION"] == 1
    assert stated["BOTTOM_FRICTION_DISSIPATION"] == 1
    assert stated["TYPE_OF_INITIAL_DIRECTIONAL_SPECTRUM"] == 0


def test_the_seaward_rim_is_opened_so_the_tide_and_the_spectrum_both_enter():
    """One rim, two roles: the code quad an ocean section is written under
    prescribes a level and leaves the velocity free, which is the host's tidal
    edge and the wave deck's spectral edge at once."""
    named = {op.fn: op for op in template.MESH.ops}
    assert "set_bed" in named
    # THE DECK STATES THE DEPTH the rim is opened at, never the library: its own
    # default is deeper than every node of a coastal window, and a walled rim
    # admits neither the tide nor the spectrum.
    stated = named["identify_ocean_boundary_sections"].kwargs["depth_threshold"]
    assert stated.name == "open_depth_threshold_m"
    param = next(p for p in _workflow().params
                 if p.name == "open_depth_threshold_m")
    assert (param.default, param.door) == (-12.0, "scenario")


def test_the_answers_are_the_two_modules_own_rows():
    """Every number a reader is handed is a measure of one variable an engine
    wrote. No module writes a BEARING for a current, so which way it runs is
    answered by the pair of components the engine solved."""
    named = {name: (measure.primitive.variable, measure.primitive.module)
             for name, measure in template.ANSWER.items()}
    assert named["longshore_current_speed_mps"] == ("M", None)
    assert named["current_along_x_mps"] == ("U", None)
    assert named["current_along_y_mps"] == ("V", None)
    assert named["hs_at_station_m"] == ("HM0", "tomawac")
    assert named["peak_current_speed_mps"] == ("M", None)
    assert named["breaking_rate_peak_per_s"] == ("BETA", "tomawac")
    assert "HM0" in WAC.MODULE_OUTPUT


def test_every_charted_variable_is_captioned():
    """A chart titled by a token nobody named is a picture a reader cannot read."""
    assert [p.variable for p in template.OUTPUTS] == ["M", "HM0"]
    assert all((p.variable or p.kind) in template.CAPTIONS
               for p in template.OUTPUTS)


def test_the_granularity_floor_is_the_step_this_deck_states():
    """The deck states its own TIME STEP for both modules, so the finest edge it
    may be asked for is the finest that step is stable at - not the mesh
    builder's own floor."""
    resolution = next(p for p in _workflow().params
                      if p.name == "mesh_resolution_m")
    assert resolution.default == 40.0
    assert resolution.bounds[0] == 20.0


def test_the_breaking_answer_reads_the_magnitude_of_a_negative_field():
    """TOMAWAC publishes its breaking rate as a NEGATIVE quantity - energy
    leaving the spectrum - so the hardest-working node is the field's minimum,
    and read as a maximum this answer would report the untouched water offshore.
    The synthetic field is a shore band against still water."""
    measure = template.ANSWER["breaking_rate_peak_per_s"]
    assert measure.stat == "min"
    band = [-0.205, -0.118, -0.004, 0.0, 0.0]
    assert measure.answer(min(band), measure.against) == pytest.approx(0.205)


def test_the_tide_reaches_the_rim_as_the_series_the_record_served():
    """The level slot reads a water level SERIES, and the host writes the window
    the record reported as the boundary's own column: a rim held at one number
    is a sea standing still for the whole run, which a wave-driven current is
    measured against."""
    from trid3nt_server.workflows.runtime.temporal import Series

    tide = Series([0.0, 1800.0, 3600.0], [0.25, 0.40, 0.52], units="m")
    rows = _rows()
    assert rows["level"].data_class == "water level series"
    sheet = fill(template.STEERING, produced={
        "settled": {"title": "COAST DOMAIN",
                    "liquid_boundary_order": ["open"],
                    "liquid_boundary_prescribes": ["elevation"],
                    "opening": "CONSTANT ELEVATION", "depth_m": None,
                    "level_m": 0.25, "outflow_stage_m": 0.25,
                    "outflow_stage_series": tide, "time_step_s": 1.0,
                    "start_time_s": 0.0, "until_s": _HOUR_S},
        "station": _STATION,
        "wave": Wave(height_m=1.6, peak_frequency_hz=0.125,
                     direction_deg=40.0, peak_period_s=8.0,
                     from_direction_deg=220.0),
        "level": tide})
    stated = dict(sheet.resolved())
    assert stated["LIQUID BOUNDARIES FILE"] == LIQUID_BOUNDARIES_FILENAME
    written = sheet.files[LIQUID_BOUNDARIES_FILENAME]
    assert written.splitlines()[1] == "T SL(1)"


def test_the_captions_say_the_level_row_carries_the_tide_over_the_window():
    """A caption that called it one elevation would describe a different run."""
    assert "tide" in template.CAPTIONS["level"]
    assert "window" in template.CAPTIONS["level"]
