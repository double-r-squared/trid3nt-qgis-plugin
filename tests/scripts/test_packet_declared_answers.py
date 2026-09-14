"""A declared answer that is not a number: what the delivery does with a sentence.

A measure answers with prose in two cases, and they are opposite verdicts: a read
that came back EMPTY states why and is a gap in the delivery, while a measure held
to a value nobody supplied states that nobody asked and is not. A chart is judged
the same way: against what the template PLACED, never against its absence.
"""

from __future__ import annotations

import functools
import importlib.util
import sys
from pathlib import Path

import pytest

from trid3nt_server.workflows.telemac.modules.outputs import NOT_ASKED, NOT_READ

DEV = Path(__file__).resolve().parents[2] / "dev"
SCRIPT = DEV / "packet" / "assemble_proof_packet.py"

if not DEV.is_dir():
    pytest.skip("dev/ is absent: the dev tools are not on the remote",
                allow_module_level=True)

#: A registered template whose ANSWER names more than one field, so the gap list
#: is read against a real declaration rather than a stub nobody ships.
TEMPLATE = "telemac_river_scour"


@functools.lru_cache(maxsize=1)
def _packet_module():
    """The assembler, imported by path - ``dev/packet/`` is not a package."""
    spec = importlib.util.spec_from_file_location("assemble_proof_packet", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("assemble_proof_packet", module)
    spec.loader.exec_module(module)
    return module


def _fields() -> tuple[str, ...]:
    import trid3nt_server.tools  # noqa: F401 - the import IS the registration
    from trid3nt_server.tools import TOOL_REGISTRY

    return tuple(TOOL_REGISTRY[TEMPLATE].fn.workflow.answer_fields)


def _metrics(**stated: object) -> dict:
    """A sheet the template answered in full, with the stated keys replaced: a
    field the metrics never carry is a null of its own and would mask the case."""
    return {key: 1.0 for key in _fields()} | stated


def test_an_empty_read_states_its_reason_and_the_delivery_refuses_it():
    packet = _packet_module()
    key = _fields()[0]
    reason = "D50 is not among the variables the result carries."
    gaps = packet.unanswered(TEMPLATE, _metrics(**{key: f"{NOT_READ}{reason}"}))
    assert len(gaps) == 1 and gaps[0].startswith(f"answer {key}:")
    assert reason in gaps[0] and "came back empty" in gaps[0]


def test_a_measure_nobody_asked_passes_and_a_null_one_does_not():
    packet = _packet_module()
    key, other = _fields()[0], _fields()[1]
    asked = f"{NOT_ASKED}d50_standard_m was not supplied"
    assert packet.unanswered(TEMPLATE, _metrics(**{key: asked})) == []
    gaps = packet.unanswered(TEMPLATE, _metrics(**{key: None, other: asked}))
    assert len(gaps) == 1 and "answered null" in gaps[0]
    assert asked in gaps[0], "the sentence the run did state rides on the gap line"


def test_a_chart_is_a_gap_only_where_the_template_places_one():
    """A chart is a read a template gives a place. A template that places none
    owes none, so a run that persisted no spec is complete; one that places a
    chart and persisted no spec is short a deliverable."""
    packet = _packet_module()
    assert packet.unplaced_chart("telemac_river_dredging", {}) == []
    gaps = packet.unplaced_chart(TEMPLATE, {})
    assert len(gaps) == 1 and gaps[0].startswith("chart:")
    assert packet.unplaced_chart(TEMPLATE, {"chart_spec": {"marker": {}}}) == []
