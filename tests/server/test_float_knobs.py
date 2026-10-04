"""Every float knob reads through one reader: a zero, negative or malformed wait
takes its default, never an unbounded or zero wait; the ambiguity margin alone
takes zero, which turns the ask off."""
from __future__ import annotations

import pytest

from trid3nt_server.inputs.gate.confirm import _code_exec_approval_timeout_s
from trid3nt_server.server.config import _ambiguity_margin_threshold, _tool_choice_timeout_s
from trid3nt_server.server.processing import _timeout_s

WAITS = [
    ("TRID3NT_TOOL_CHOICE_TIMEOUT_S", _tool_choice_timeout_s, 45.0),
    ("TRID3NT_CODE_EXEC_APPROVAL_TIMEOUT_S", _code_exec_approval_timeout_s, 180.0),
    ("TRID3NT_SESSION_PROCESSING_TIMEOUT_S", _timeout_s, 600.0),
]


@pytest.mark.parametrize("name,read,default", WAITS)
@pytest.mark.parametrize("raw", ["0", "-5", "soon"])
def test_a_wait_never_reads_zero_negative_or_garbage(monkeypatch, name, read, default, raw):
    monkeypatch.setenv(name, raw)
    assert read() == default


@pytest.mark.parametrize("name,read,default", WAITS)
def test_a_wait_reads_a_positive_value_live(monkeypatch, name, read, default):
    monkeypatch.setenv(name, "7.5")
    assert read() == 7.5
    monkeypatch.delenv(name)
    assert read() == default


def test_ambiguity_margin_zero_turns_the_ask_off(monkeypatch):
    monkeypatch.setenv("TRID3NT_AMBIGUITY_MARGIN", "0")
    assert _ambiguity_margin_threshold() == 0.0
    monkeypatch.setenv("TRID3NT_AMBIGUITY_MARGIN", "-1")
    assert _ambiguity_margin_threshold() == 0.0
    monkeypatch.setenv("TRID3NT_AMBIGUITY_MARGIN", "wide")
    assert _ambiguity_margin_threshold() == 0.01
