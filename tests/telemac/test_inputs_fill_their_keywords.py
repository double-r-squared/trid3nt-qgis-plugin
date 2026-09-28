"""An input of the run fills the keywords it determines itself: no template
line routes it, and a value the template states wins."""

from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace

from trid3nt_server.workflows.telemac.modules import T2D, WAC
from trid3nt_server.workflows.telemac.modules.sheet import fill


class _Release(T2D):
    TITLE = "a release"


class _Stated(T2D):
    ABSCISSAE_OF_SOURCES = [5.0]


def test_a_settled_release_fills_the_source_coordinates_with_no_template_line():
    sheet = fill(_Release, produced={"source": {"at": [101.5, 202.5]}})
    assert "ABSCISSAE_OF_SOURCES" not in _Release.ASSERTED
    assert sheet.filled["ABSCISSAE_OF_SOURCES"].value == [101.5]
    assert sheet.filled["ORDINATES_OF_SOURCES"].value == [202.5]
    assert str(sheet.filled["ORDINATES_OF_SOURCES"].provenance) == "derived: source"


def test_a_value_the_template_states_wins_over_the_input_that_fills_it():
    sheet = fill(_Stated, produced={"source": {"at": [101.5, 202.5]}})
    assert sheet.filled["ABSCISSAE_OF_SOURCES"].value == [5.0]
    assert sheet.filled["ORDINATES_OF_SOURCES"].value == [202.5]


def test_an_absent_input_fills_nothing():
    sheet = fill(_Release, produced={"source": None, "observe": None})
    assert "ABSCISSAE_OF_SOURCES" not in sheet.filled
    assert "INITIAL_VALUES_OF_TRACERS" not in sheet.filled


def test_a_measured_sample_fills_the_opening_tracer():
    sheet = fill(_Release, produced={"observe": SimpleNamespace(value=11.5)})
    assert sheet.filled["INITIAL_VALUES_OF_TRACERS"].value == [11.5]


def test_the_fetched_level_and_sea_state_fill_a_wave_deck():
    wave = SimpleNamespace(height_m=1.25, peak_frequency_hz=0.1,
                           direction_deg=270.0)
    sheet = fill(WAC, produced={"wave": wave,
                                "level": SimpleNamespace(value=0.4)})
    assert sheet.filled["BOUNDARY_SIGNIFICANT_WAVE_HEIGHT"].value == 1.25
    assert sheet.filled["BOUNDARY_PEAK_FREQUENCY"].value == 0.1
    assert sheet.filled["BOUNDARY_MAIN_DIRECTION_1"].value == 270.0
    assert sheet.filled["INITIAL_STILL_WATER_LEVEL"].value == 0.4


def test_a_coupled_wave_body_takes_the_sea_state_and_not_the_level():
    from trid3nt_server.workflows.telemac.modules.sheet import _coupled_filled

    body = WAC.wave(geometry="g.slf", boundary="b.cli",
                    BOUNDARY_PEAK_FREQUENCY=0.2)
    wave = SimpleNamespace(height_m=1.25, peak_frequency_hz=0.1,
                           direction_deg=270.0)
    slots = _coupled_filled(body, {"wave": wave,
                                   "level": SimpleNamespace(value=0.4)})["slots"]
    assert slots["BOUNDARY_SIGNIFICANT_WAVE_HEIGHT"] == 1.25
    assert slots["BOUNDARY_PEAK_FREQUENCY"] == 0.2
    assert "INITIAL_STILL_WATER_LEVEL" not in slots


def _template_bodies():
    import importlib
    import inspect
    import pkgutil

    from trid3nt_server.workflows.telemac import templates

    for info in pkgutil.walk_packages(templates.__path__, templates.__name__ + "."):
        module = importlib.import_module(info.name)
        for name, body in vars(module).items():
            if (inspect.isclass(body) and body.__module__ == module.__name__
                    and isinstance(getattr(body, "ASSERTED", None), Mapping)):
                yield f"{info.name}.{name}", body


def test_no_template_line_routes_a_keyword_an_input_fills():
    from trid3nt_server.workflows.runtime import Ref
    from trid3nt_server.workflows.runtime.reads import declared_reads

    fills = {name for wrapper in (T2D, WAC)
             for keywords in wrapper.FILLED_BY.values() for name in keywords}
    bodies = dict(_template_bodies())
    assert len(bodies) >= 8
    routed = []
    for where, body in bodies.items():
        stated = dict(body.ASSERTED)
        for coupled in body.ASSERTED.get("coupling") or ():
            if isinstance(coupled, Mapping):
                stated |= coupled.get("slots", {})
        routed += [f"{where}.{name}" for name in sorted(fills & set(stated))
                   if any(declared_reads(stated[name], Ref))]
    assert routed == []


def test_the_run_reads_the_input_a_coupled_body_fills_itself_from():
    from trid3nt_server.tools import TOOL_REGISTRY

    workflow = TOOL_REGISTRY["tomawac_wave_driven_currents"].fn.workflow
    assert "wave" in workflow._reads()
