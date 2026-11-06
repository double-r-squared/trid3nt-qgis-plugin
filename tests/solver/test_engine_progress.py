"""Each engine's declared progress parser, pinned on lines captured from the solve containers of the zero-fetch canaries."""
from __future__ import annotations

from pathlib import Path

from trid3nt_server.workflows.geoclaw import progress as geoclaw
from trid3nt_server.workflows.modflow import progress as modflow
from trid3nt_server.workflows.telemac import progress as telemac

_SAMPLES = Path(__file__).parents[1] / "fixtures" / "progress"


def _fold(module, sheet, name):
    parts: dict[str, str] = {}
    for line in (_SAMPLES / name).read_text(encoding="utf-8", errors="replace").splitlines():
        parts.update(module.step_of(line, sheet) or {})
    return ", ".join(parts.values())


def _filled(**values):
    return {"filled": {name: {"value": value} for name, value in values.items()}}


def test_telemac_states_the_iteration_and_the_time_against_the_decks_duration():
    sheet = _filled(DURATION=1800.0)
    assert _fold(telemac, sheet, "telemac.stdout") == "step 2449, t = 1800.02 s of 1800 s"


def test_telemac_states_a_step_total_only_when_the_deck_states_one_under_a_fixed_step():
    line = " ITERATION     1500    TIME: 18 MN  22.5000 S   (     1102.5000 S)"
    assert telemac.step_of(line, _filled(NUMBER_OF_TIME_STEPS=2449)) == {"step": "step 1500 of 2449", "t": "t = 1102.5 s"}
    variable = _filled(NUMBER_OF_TIME_STEPS=2449, VARIABLE_TIME_STEP=True)
    assert telemac.step_of(line, variable)["step"] == "step 1500"
    assert telemac.step_of(" ITERATION        0    TIME:   0.0000 S", {}) == {"step": "step 0", "t": "t = 0 s"}
    assert telemac.step_of(" GRACJG (BIEF) :        2 ITERATIONS, ABSOLUTE PRECISION:   0.5E-06", {}) is None


def test_geoclaw_states_the_time_reached_and_each_frame_against_the_decks_totals():
    sheet = _filled(CLAW_TFINAL=54.0, FGOUT_FGOUT_GRIDS=[[1, 1, 19, 0.0, 54.0]])
    assert _fold(geoclaw, sheet, "geoclaw.stdout") == "t = 54 s of 54 s, frame 19 of 19"


def test_geoclaw_names_the_grid_when_the_deck_writes_several():
    sheet = _filled(FGOUT_FGOUT_GRIDS=[[1, 1, 5], [2, 1, 9]])
    line = "Writing fgout grid #   2  frame    4 at time =    0.90000000E+01"
    assert geoclaw.step_of(line, sheet) == {"frame2": "grid 2 frame 4 of 9"}


def test_modflow_states_the_stress_period_and_step_against_the_decks_tdis():
    sheet = _filled(TDIS_NPER=2, TDIS_PERIODDATA=[[1.0, 4, 1.0], [2.0, 6, 1.0]])
    line = "    Solving:  Stress period:     2    Time step:     3"
    assert modflow.step_of(line, sheet) == {"step": "stress period 2 of 2, step 3 of 6"}
    adapted = {"filled": {**sheet["filled"], "TDIS_ATS_FILERECORD": {"value": [["ats.dat"]]}}}
    assert modflow.step_of(line, adapted) == {"step": "stress period 2 of 2, step 3"}
    assert modflow.step_of(line, {}) == {"step": "stress period 2, step 3"}
    assert _fold(modflow, _filled(TDIS_NPER=1, TDIS_PERIODDATA=[[1.0, 1, 1.0]]), "modflow.stdout") == "stress period 1 of 1, step 1 of 1"
