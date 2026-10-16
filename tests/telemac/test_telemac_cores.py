"""How many cores a solve runs on is the module's own PARALLEL PROCESSORS keyword.

Covered: the dictionary's value standing as the default, a count past this box
refusing by name rather than being cut down, no `cores` argument on any
template, the same number reaching the case the worker's launcher partitions
on, and the DECK'S OWN numerics answering the count - a direct solver is
factorised whole and runs serial whatever was stated.
"""

from __future__ import annotations

import pytest

from trid3nt_server.workflows.telemac.authoring.staged_check import BOX_CORES
from trid3nt_server.workflows.solver.staging import case_section
from trid3nt_server.workflows.telemac.modules import T2D
from trid3nt_server.workflows.telemac.modules.artemis import ART
from trid3nt_server.workflows.runtime.module import SlotRefused
from trid3nt_server.workflows.telemac.modules.module import PROCESSORS
from trid3nt_server.workflows.runtime.sheet import fill
from trid3nt_server.workflows.telemac.modules.sheet import (
    SOLVER,
    solve_cores,
)
from seated import value_inputs

#: A partition every box this runs on can seat, so the assertions are about the
#: keyword and not about the machine the suite happens to run on.
PARTITIONED = min(2, BOX_CORES)


def test_a_run_that_states_no_count_leaves_the_modules_own_keyword_standing():
    """The dictionary's own PARALLEL PROCESSORS value is the default, and it is
    a scalar computation - so nothing is written and the solve runs on one."""
    assert T2D.MODULE_INPUT[PROCESSORS].engine_default == 0
    assert PROCESSORS not in fill(T2D).filled
    assert solve_cores(T2D, {}) == 1


def test_a_count_past_this_box_refuses_by_name():
    """Refused, never clamped: a partition cut down to fit is one the caller
    was never told about - on the sheet, and on the call before any stage."""
    from trid3nt_server.tools import TOOL_REGISTRY
    import trid3nt_server.workflows.telemac.templates  # noqa: F401

    with pytest.raises(SlotRefused) as raised:
        fill(T2D, **{PROCESSORS: BOX_CORES + 1})
    assert "PARALLEL PROCESSORS" in str(raised.value)
    assert str(BOX_CORES) in str(raised.value)
    workflow = TOOL_REGISTRY["telemac_dye_release"].fn.workflow
    with pytest.raises(SlotRefused):
        workflow.input("PARALLEL PROCESSORS").accept(BOX_CORES + 1)


def test_no_template_takes_a_cores_argument():
    """The count is stated once, under the engine's own name for it."""
    from trid3nt_server.tools import TOOL_REGISTRY
    import trid3nt_server.workflows.telemac.templates  # noqa: F401

    templates = [tool.fn.workflow for tool in TOOL_REGISTRY.values()
                 if getattr(getattr(tool.fn, "workflow", None), "engine", "") == "telemac"]
    assert templates
    for workflow in templates:
        assert "cores" not in {prm.name for prm in value_inputs(workflow)}
        assert "PARALLEL PROCESSORS" in workflow.sheet_doc()


def test_a_coupled_run_states_the_count_once_and_it_is_the_carriers():
    """The launcher partitions a run once, so the bare keyword on a coupled
    run is the carrier's and asks for no body to be named."""
    from trid3nt_server.tools import TOOL_REGISTRY
    import trid3nt_server.workflows.telemac.templates  # noqa: F401
    from trid3nt_server.workflows.telemac.workflow import run_bodies

    workflow = TOOL_REGISTRY["telemac_dye_release"].fn.workflow
    assert len(run_bodies(workflow.steering)) > 1
    held = workflow.input("PARALLEL PROCESSORS")
    assert held.body is workflow.steering
    assert (held.name, held.accept(PARTITIONED)) == (PROCESSORS, PARTITIONED)


def test_the_stated_count_is_the_count_the_solve_runs_on():
    sheet = fill(T2D, **{PROCESSORS: PARTITIONED})
    assert sheet.filled[PROCESSORS].value == PARTITIONED
    assert sheet.filled[PROCESSORS].provenance.origin.value == "user"
    assert solve_cores(T2D, sheet.filled) == PARTITIONED


def test_the_case_carries_the_same_count_the_steering_file_states():
    case = case_section(module="telemac2d", steering="t2d.cas",
                        results=["r.slf"], server_facts={}, cores=4)
    assert case["cores"] == 4
    assert "cores" not in case_section(
        module="telemac2d", steering="t2d.cas", results=["r.slf"],
        server_facts={}, cores=1)


def test_the_direct_solver_runs_serial_whatever_was_asked_for():
    """ARTEMIS's dictionary defaults SOLVER to the direct one, which factorises
    the whole system rather than partitioning it. The deck states no solver, so
    the default IS what the engine will do, and the run is serial."""
    sheet = fill(ART, **{PROCESSORS: max(2, BOX_CORES)})
    assert ART.MODULE_INPUT[SOLVER].engine_default == 8
    assert PROCESSORS not in sheet.filled
    assert solve_cores(ART, sheet.filled) == 1


def test_the_run_says_why_the_cores_it_asked_for_are_not_used():
    """The caption is the run's, not a log line: a reader who asked for every
    core on the box and got one reads WHY on the record."""
    from trid3nt_server.inputs.run_notes import bind_notes, drain_notes

    token = bind_notes()
    try:
        fill(ART, **{PROCESSORS: max(2, BOX_CORES)})
        notes = drain_notes(token)
    except BaseException:
        drain_notes(token)
        raise
    assert any("direct solver" in note and "serial" in note for note in notes)


def test_a_deck_that_states_the_parallel_direct_solver_keeps_its_cores():
    """The model never chooses numerics, so SOLVER 9 - the parallel direct
    solver - is a deck author's statement, and a deck that makes it is
    partitioned like any other."""
    sheet = fill(ART, **{PROCESSORS: PARTITIONED, SOLVER: 9})
    assert solve_cores(ART, sheet.filled) == PARTITIONED


def test_an_iterative_deck_is_partitioned_on_the_count_it_asked_for():
    """The rule is the SOLVER's, not the module's: a deck stating an iterative
    solver is partitioned whatever the dictionary's own default was."""
    sheet = fill(ART, **{PROCESSORS: PARTITIONED, SOLVER: 1})
    assert solve_cores(ART, sheet.filled) == PARTITIONED


def test_the_launcher_is_handed_the_number_the_deck_resolved_to():
    """The engine and the launcher read ONE count: a deck the engine solves
    serially must not be launched under mpirun on every core the box has."""
    serial = fill(ART, **{PROCESSORS: max(2, BOX_CORES)})
    case = case_section(module="artemis", steering="art.cas",
                        results=["r.slf"], server_facts={},
                        cores=solve_cores(ART, serial.filled))
    assert "cores" not in case
