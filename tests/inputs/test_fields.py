"""The one reader of fetched records: members through a source's map, the raw record untouched, refusals by name.

Read on recorded USGS answers (tests/fixtures/fetcher_fields/README.md)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trid3nt_contracts.coverage import Coverage
from trid3nt_server.inputs.fields import FieldError, Measured, records

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "fetcher_fields"

#: The accuracy table the design read from TM 1-A1, stated over the field-measurement record's own procedure.
_GWPD = "Cunningham and Schalk 2011, USGS TM 1-A1: GWPD 1 p.5 (steel tape 0.01 ft), GWPD 4 p.34 (electric tape +/-0.01 ft)"
_FIELD_MEASUREMENTS = {
    "time": {"field": "time"},
    "value": {"field": "value", "unit": {"field": "unit_of_measure"}},
    "accuracy": {"field": "observing_procedure", "cite": _GWPD, "table": {
        "GW level, steel tape": {"absolute": 0.01, "unit": "ft"},
        "GW level, electric tape": {"absolute": 0.01, "unit": "ft"},
        "GW level, calib electric tape": {"absolute": 0.01, "unit": "ft"}}},
}
_TM_3A7 = ("Sauer and Turnipseed 2010, USGS TM 3-A7, Stage-Accuracy Requirements p.3: 0.01 ft or "
           "0.2 percent of the effective stage, whichever is greater")


def _fields(**stated):
    row = Coverage.model_validate({"data_class": "groundwater level", "kind": "measured",
                                   "extent": {"kind": "service"}, "window": {"series": True},
                                   "fields": stated})
    return row.fields


@pytest.fixture(scope="module")
def wells():
    doc = json.loads((_FIXTURES / "usgs_field_measurements_des_moines.json").read_text())
    return records(doc, _fields(**_FIELD_MEASUREMENTS), source="fetch_usgs_groundwater_levels",
                   site_fields=("monitoring_location_id",))


def test_members_read_through_the_map_and_raw_keeps_every_field(wells):
    assert len(wells) == 185
    first = wells[0]
    assert first.value == Measured(428.0, "ft")
    assert first.time == "1998-02-09T15:40:00+00:00"
    assert len(first.raw) == 21 and first.raw["qualifier"] == ["Static"]


def test_accuracy_reads_off_the_cited_table_where_the_procedure_has_a_row(wells):
    read, refused = [], []
    for rec in wells:
        try:
            read.append(rec.accuracy)
        except FieldError as exc:
            refused.append(exc)
    assert len(read) == 66 and set(read) == {Measured(0.01, "ft")}
    assert len(refused) == 119
    assert {exc.error_code for exc in refused} == {"FIELD_UNTABLED"}
    assert "fetch_usgs_groundwater_levels at USGS-" in str(refused[0])


def test_a_member_the_source_does_not_map_refuses_naming_source_and_member(wells):
    with pytest.raises(FieldError) as caught:
        wells[0].above
    assert caught.value.error_code == "FIELD_UNMAPPED"
    assert "fetch_usgs_groundwater_levels" in str(caught.value) and "'above'" in str(caught.value)


def test_a_mapped_field_empty_on_the_record_refuses():
    rec = records({"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": None,
         "properties": {"value": "", "unit_of_measure": "ft"}}]},
        _fields(value={"field": "value", "unit": {"field": "unit_of_measure"}}),
        source="fetch_usgs_groundwater_levels")[0]
    with pytest.raises(FieldError) as caught:
        rec.value
    assert caught.value.error_code == "FIELD_EMPTY"


def test_a_rule_is_the_larger_of_its_bound_and_its_fraction_of_the_value():
    lines = [line.split("\t") for line in (_FIXTURES / "nwis_iv_06610670.rdb").read_text().splitlines()
             if line and not line.startswith("#")]
    header, readings = lines[0], lines[2:]
    rows = [dict(zip(header, line)) for line in readings]
    stage = records({"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": None, "properties": row} for row in rows]},
        _fields(value={"field": "271977_00065", "unit": "ft"},
                accuracy={"absolute": 0.01, "relative": 0.002, "unit": "ft", "cite": _TM_3A7}),
        source="fetch_usgs_nwis_gauges", site_fields=("site_no",))
    first = stage[0]
    assert first.value == Measured(11.26, "ft")
    assert first.accuracy.value == pytest.approx(0.002 * 11.26)
    assert first.accuracy.unit == "ft"


def test_a_constant_reads_the_same_on_every_record_and_a_flattened_name_reads_as_written():
    rec = records({"status.observed.primary": 3.2, "unit": "m"},
                  _fields(value={"field": "status.observed.primary", "unit": "m"},
                          accuracy={"constant": 0.02, "unit": "m", "cite": "a published spec"}),
                  source="a_source")[0]
    assert rec.value == Measured(3.2, "m") and rec.accuracy == Measured(0.02, "m")
    nested = records({"status": {"observed": {"primary": 4.5}}},
                     _fields(value={"field": "status.observed.primary", "unit": "m"}),
                     source="a_source")[0]
    assert nested.value == Measured(4.5, "m")
