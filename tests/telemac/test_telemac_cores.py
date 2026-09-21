"""The cores lever's ONE consequence: how many cores a solve runs on.

Covered: the module's own processors keyword standing as the default, a count
past this box refusing by name rather than being cut down, the keyword the
engine reads a stated count under, the same number reaching the case the
worker's launcher partitions on, the card sentence a module that spells no
processor keyword carries, and the DECK'S OWN numerics answering the lever -
a direct solver is factorised whole and runs serial whatever was asked.
"""

from __future__ import annotations

import pytest

from trid3nt_server.workflows.runtime.errors import CoresUnavailable
from trid3nt_server.workflows.runtime.levers import BOX_CORES, cores_asked
from trid3nt_server.workflows.telemac.authoring.assembler import case_section
from trid3nt_server.workflows.telemac.modules import T2D
from trid3nt_server.workflows.telemac.modules.artemis import ART
from trid3nt_server.workflows.telemac.modules.sheet import (
    PROCESSORS,
    SOLVER,
    fill,
    solve_cores,
)

#: A partition every box this runs on can seat, so the assertions are about the
#: lever and not about the machine the suite happens to run on.
PARTITIONED = min(2, BOX_CORES)


def test_a_run_that_states_no_count_leaves_the_modules_own_keyword_standing():
    """The dictionary's own PARALLEL PROCESSORS value is the default, and it is
    a scalar computation - so nothing is written and the solve runs on one."""
    assert T2D.MODULE_INPUT[PROCESSORS].engine_default == 0
    assert PROCESSORS not in fill(T2D).filled
    assert solve_cores(T2D, {}, None) == 1


def test_a_count_past_this_box_refuses_by_name():
    """Refused, never clamped: a partition cut down to fit is one the caller
    was never told about."""
    with pytest.raises(CoresUnavailable) as raised:
        cores_asked(BOX_CORES + 1)
    assert "cores" in str(raised.value) and str(BOX_CORES) in str(raised.value)


def test_a_count_that_is_not_a_whole_number_of_cores_refuses():
    with pytest.raises(CoresUnavailable):
        cores_asked("large")
    with pytest.raises(CoresUnavailable):
        cores_asked(0)


def test_the_stated_count_states_the_keyword_the_engine_reads_it_under():
    sheet = fill(T2D, params={"cores": PARTITIONED})
    if PARTITIONED < 2:
        assert PROCESSORS not in sheet.filled
        return
    assert sheet.filled[PROCESSORS].value == PARTITIONED
    assert sheet.filled[PROCESSORS].provenance.origin.value == "producer"


def test_the_deck_a_run_edited_by_keyword_keeps_the_number_it_stated():
    sheet = fill(T2D, params={"cores": PARTITIONED}, **{PROCESSORS: 3})
    assert sheet.filled[PROCESSORS].value == 3


def test_the_case_carries_the_same_count_the_steering_file_states():
    case = case_section(module="telemac2d", steering="t2d.cas",
                        results=["r.slf"], server_facts={}, cores=4)
    assert case["cores"] == 4
    assert "cores" not in case_section(
        module="telemac2d", steering="t2d.cas", results=["r.slf"],
        server_facts={}, cores=1)


def test_a_module_that_spells_no_processor_keyword_says_so_on_the_card():
    from trid3nt_server.workflows.telemac.workflow import _serial_rows

    class _Serial(type(T2D)):
        MODULE = "serial_probe"
        MODULE_INPUT = {}
        PRINTOUTS = ""

    class _Sheet:
        body = _Serial
        coupled = ()
        filled = {}
        tracers = ()

        def stated(self):
            return {}

    row, = _serial_rows(_Sheet())
    assert row.name == "serial_probe.cores"
    assert row.value == "serial: this engine runs on one core"
    assert not row.editable


def test_the_direct_solver_runs_serial_whatever_was_asked_for():
    """ARTEMIS's dictionary defaults SOLVER to the direct one, which factorises
    the whole system rather than partitioning it. The deck states no solver, so
    the default IS what the engine will do, and the run is serial."""
    sheet = fill(ART, params={"cores": BOX_CORES})
    assert ART.MODULE_INPUT[SOLVER].engine_default == 8
    assert PROCESSORS not in sheet.filled
    assert solve_cores(ART, sheet.filled, BOX_CORES) == 1


def test_the_run_says_why_the_cores_it_asked_for_are_not_used():
    """The caption is the run's, not a log line: a reader who asked for every
    core on the box and got one reads WHY on the record."""
    from trid3nt_server.workflows.runtime.journal import bind_notes, drain_notes

    token = bind_notes()
    try:
        fill(ART, params={"cores": BOX_CORES})
        notes = drain_notes(token)
    except BaseException:
        drain_notes(token)
        raise
    assert any("direct solver" in note and "serial" in note for note in notes)


def test_a_deck_that_states_the_parallel_direct_solver_keeps_its_cores():
    """The model never chooses numerics, so SOLVER 9 - the parallel direct
    solver - is a deck author's statement, and a deck that makes it is
    partitioned like any other."""
    sheet = fill(ART, params={"cores": PARTITIONED}, **{SOLVER: 9})
    assert solve_cores(ART, sheet.filled, PARTITIONED) == PARTITIONED


def test_an_iterative_deck_is_partitioned_on_the_count_it_asked_for():
    """The rule is the SOLVER's, not the module's: a deck stating an iterative
    solver is partitioned whatever the dictionary's own default was."""
    sheet = fill(ART, params={"cores": PARTITIONED}, **{SOLVER: 1})
    assert solve_cores(ART, sheet.filled, PARTITIONED) == PARTITIONED


def test_the_launcher_is_handed_the_number_the_deck_resolved_to():
    """The engine and the launcher read ONE count: a deck the engine solves
    serially must not be launched under mpirun on every core the box has."""
    serial = fill(ART, params={"cores": BOX_CORES})
    case = case_section(module="artemis", steering="art.cas",
                        results=["r.slf"], server_facts={},
                        cores=solve_cores(ART, serial.filled, BOX_CORES))
    assert "cores" not in case
