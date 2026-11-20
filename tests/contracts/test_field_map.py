"""The field map a source states: four member forms, a cite on every number, a unit on every numeric member."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from trid3nt_contracts.coverage import Coverage, Member, Rule

_TM_3A7 = "Sauer and Turnipseed 2010, USGS TM 3-A7, Stage-Accuracy Requirements p.3"


def _row(**fields):
    return Coverage.model_validate({
        "data_class": "water level series", "kind": "measured",
        "extent": {"kind": "service"}, "window": {"series": True},
        "fields": fields})


def test_each_of_the_four_forms_is_one_member():
    assert Member(field="value", unit="ft").field == "value"
    assert Member(field="code", table={"A": "x"}, cite="a table").table == {"A": "x"}
    assert Member(constant=0.02, unit="m", cite="a spec").constant == 0.02
    ruled = Member(absolute=0.01, relative=0.002, unit="ft", cite=_TM_3A7).rule
    assert ruled == Rule(absolute=0.01, relative=0.002, unit="ft")


@pytest.mark.parametrize("stated", [
    {"constant": 0.02, "unit": "m"},
    {"absolute": 0.01, "unit": "ft"},
    {"field": "procedure", "table": {"steel tape": 0.01}, "unit": "ft"},
])
def test_a_number_nobody_can_trace_refuses(stated):
    with pytest.raises(ValidationError, match="cite"):
        Member(**stated)


def test_a_plain_field_carries_no_cite():
    with pytest.raises(ValidationError, match="no cite"):
        Member(field="value", unit="ft", cite="the source")


@pytest.mark.parametrize("stated", [
    {},
    {"field": "value", "constant": 1.0, "unit": "m", "cite": "c"},
    {"constant": 1.0, "absolute": 0.1, "unit": "m", "cite": "c"},
    {"table": {"A": 1.0}, "unit": "m", "cite": "c"},
])
def test_a_member_is_exactly_one_form(stated):
    with pytest.raises(ValidationError):
        Member(**stated)


def test_a_rule_states_its_own_unit():
    with pytest.raises(ValidationError, match="own unit"):
        Member(absolute=0.01, unit={"field": "unit_of_measure"}, cite=_TM_3A7)


def test_a_numeric_member_states_its_unit_and_time_needs_none():
    assert _row(time={"field": "time"}).fields["time"].field == "time"
    assert _row(value={"field": "value", "unit": {"field": "unit_of_measure"}})
    with pytest.raises(ValidationError, match="states no unit"):
        _row(value={"field": "value"})


def test_a_table_of_cited_rules_carries_its_units_in_each_row():
    row = _row(accuracy={"field": "procedure", "cite": "GWPD",
                         "table": {"steel tape": {"absolute": 0.01, "unit": "ft"}}})
    assert row.fields["accuracy"].table["steel tape"] == Rule(absolute=0.01, unit="ft")


def test_an_unknown_member_name_refuses():
    with pytest.raises(ValidationError):
        _row(valeu={"field": "value", "unit": "ft"})
