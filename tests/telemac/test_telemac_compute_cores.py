"""The compute lever's ONE consequence: how many cores a solve runs on.

Covered: the core count per sizing class stated once in the runtime, the
keyword the engine reads it under, a serial class writing nothing, the same
number reaching the case the worker's launcher partitions on, a class outside
the ladder refusing by name, and the card sentence a module that spells no
processor keyword carries.
"""

from __future__ import annotations

import pytest

from trid3nt_server.workflows.runtime.levers import COMPUTE_CORES, cores_for
from trid3nt_server.workflows.solver.compute_class import (
    COMPUTE_CLASS_ALIAS,
    ComputeClassUnknown,
)
from trid3nt_server.workflows.telemac.authoring.assembler import case_section
from trid3nt_server.workflows.telemac.modules import T2D
from trid3nt_server.workflows.telemac.modules.sheet import PROCESSORS, fill


def test_every_rung_the_dispatcher_serves_is_sized_in_cores():
    """One table, so the rungs a caller is offered and the rungs a solve is
    partitioned on cannot drift apart."""
    assert set(COMPUTE_CLASS_ALIAS.values()) == set(COMPUTE_CORES)


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
