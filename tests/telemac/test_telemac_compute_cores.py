"""The compute lever's ONE consequence: how many cores a solve runs on.

Covered: the core count per sizing class stated once in the runtime, the
keyword the engine reads it under, a serial class writing nothing, the same
number reaching the case the worker's launcher partitions on, a class outside
the ladder refusing by name, the card sentence a module that spells no
processor keyword carries, and the DECK'S OWN numerics answering the lever -
a direct solver is factorised whole and runs serial whatever was asked.
"""

from __future__ import annotations

from typing import get_args

import pytest

from trid3nt_contracts.execution import ComputeClass
from trid3nt_server.workflows.runtime.levers import COMPUTE_CORES, cores_for
from trid3nt_server.workflows.solver.compute_class import (
    COMPUTE_CLASS_ALIAS,
    ComputeClassUnknown,
)
from trid3nt_server.workflows.telemac.authoring.assembler import case_section
from trid3nt_server.workflows.telemac.modules import T2D
from trid3nt_server.workflows.telemac.modules.artemis import ART
from trid3nt_server.workflows.telemac.modules.sheet import (
    PROCESSORS,
    SOLVER,
    fill,
    solve_cores,
)


def test_every_rung_the_dispatcher_serves_is_sized_in_cores():
    """One table, so the rungs a caller is offered, the rungs a solve is
    partitioned on, and the rungs the dispatch handle accepts cannot drift
    apart."""
    assert set(COMPUTE_CLASS_ALIAS.values()) == set(COMPUTE_CORES)
    assert set(COMPUTE_CLASS_ALIAS.values()) == set(get_args(ComputeClass))


def test_a_class_outside_the_ladder_refuses_by_name():
    with pytest.raises(ComputeClassUnknown):
        cores_for("enormous")


def test_the_sizing_class_states_the_keyword_the_engine_reads_it_under():
    sheet = fill(T2D, params={"compute_class": "large"})
    assert sheet.filled[PROCESSORS].value == cores_for("large") == 4
    assert sheet.filled[PROCESSORS].provenance.origin.value == "producer"


def test_a_serial_class_leaves_the_engine_on_its_own_default():
    """The keyword's own default is one machine with no parallel library, so a
    one-core run states nothing."""
    assert PROCESSORS not in fill(T2D, params={"compute_class": "small"}).filled
    assert PROCESSORS not in fill(T2D).filled


def test_the_deck_a_run_edited_by_keyword_keeps_the_number_it_stated():
    sheet = fill(T2D, params={"compute_class": "large"}, **{PROCESSORS: 3})
    assert sheet.filled[PROCESSORS].value == 3


def test_the_case_carries_the_same_count_the_steering_file_states():
    case = case_section(module="telemac2d", steering="t2d.cas",
                        results=["r.slf"], server_facts={},
                        cores=cores_for("large"))
    assert case["cores"] == 4
    assert "cores" not in case_section(
        module="telemac2d", steering="t2d.cas", results=["r.slf"],
        server_facts={}, cores=cores_for("small"))


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


def test_the_direct_solver_runs_serial_whatever_the_sizing_class_asks_for():
    """ARTEMIS's dictionary defaults SOLVER to the direct one, which factorises
    the whole system rather than partitioning it. The deck states no solver, so
    the default IS what the engine will do, and the run is serial."""
    sheet = fill(ART, params={"compute_class": "xlarge"})
    assert ART.MODULE_INPUT[SOLVER].engine_default == 8
    assert cores_for("xlarge") == 8
    assert PROCESSORS not in sheet.filled
    assert solve_cores(ART, sheet.filled, "xlarge") == 1


def test_the_run_says_why_the_cores_the_class_asked_for_are_not_used():
    """The caption is the run's, not a log line: a reader who asked for eight
    cores and got one reads WHY on the record."""
    from trid3nt_server.workflows.runtime.journal import bind_notes, drain_notes

    token = bind_notes()
    try:
        fill(ART, params={"compute_class": "xlarge"})
        notes = drain_notes(token)
    except BaseException:
        drain_notes(token)
        raise
    assert any("direct solver" in note and "serial" in note for note in notes)


def test_a_deck_that_states_the_parallel_direct_solver_keeps_its_cores():
    """The model never chooses numerics, so SOLVER 9 - the parallel direct
    solver - is a deck author's statement, and a deck that makes it is
    partitioned like any other."""
    sheet = fill(ART, params={"compute_class": "large"}, **{SOLVER: 9})
    assert sheet.filled[PROCESSORS].value == cores_for("large") == 4
    assert solve_cores(ART, sheet.filled, "large") == 4


def test_an_iterative_deck_is_partitioned_on_the_class_it_asked_for():
    """The rule is the SOLVER's, not the module's: a deck stating an iterative
    solver is partitioned whatever the dictionary's own default was."""
    sheet = fill(ART, params={"compute_class": "large"}, **{SOLVER: 1})
    assert sheet.filled[PROCESSORS].value == 4


def test_the_launcher_is_handed_the_number_the_deck_resolved_to():
    """The engine and the launcher read ONE count: a deck the engine solves
    serially must not be launched under mpirun on eight ranks."""
    serial = fill(ART, params={"compute_class": "xlarge"})
    case = case_section(module="artemis", steering="art.cas",
                        results=["r.slf"], server_facts={},
                        cores=solve_cores(ART, serial.filled, "xlarge"))
    assert "cores" not in case
