"""The vector writer keeps every field a source returned under its own name, adds what the fetch
produced, and refuses a produced name the record already carries; a row restates only what differs
from its source's field map.

Written on recorded USGS and Water Quality Portal answers (tests/fixtures/fetcher_fields/README.md)."""

from __future__ import annotations

import csv
import json
import os
import tempfile
from pathlib import Path

import pyogrio
import pytest

from trid3nt_contracts.source_spec import SourceSpec
from trid3nt.tools.fetchers._router.errors import RouterUpstreamError
from trid3nt.tools.fetchers._router.executors.vector_fgb import features_to_fgb_bytes
from trid3nt.tools.fetchers._router.spec import compose_specs_from_tree

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "fetcher_fields"
_SPECS = compose_specs_from_tree()


@pytest.fixture
def spec() -> SourceSpec:
    # A source whose ingest states no column map and no pinned header: the writer alone decides the columns.
    return _SPECS["fetch_usgs_groundwater_levels"].model_copy(update={"ingest": {}})


def _written(features, spec):
    data = features_to_fgb_bytes(features, spec, {})
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as handle:
        handle.write(data)
        path = handle.name
    try:
        return pyogrio.read_dataframe(path)
    finally:
        os.unlink(path)


def test_every_field_of_a_recorded_answer_is_written_under_its_own_name(spec):
    doc = json.loads((_FIXTURES / "usgs_field_measurements_des_moines.json").read_text())
    frame = _written(doc["features"], spec)
    published = set(doc["features"][0]["properties"])
    assert len(published) == 21 and published <= set(frame.columns)
    assert len(frame) == 185
    # A list is written as JSON text, the source's own items inside.
    assert json.loads(frame["qualifier"].iloc[0]) == ["Static"]
    # The spatial index orders the rows, so the record is compared as a whole.
    assert sorted(frame["observing_procedure"]) == sorted(
        f["properties"]["observing_procedure"] for f in doc["features"])


def test_a_wide_record_with_slashes_in_its_names_round_trips(spec):
    names = next(csv.reader((_FIXTURES / "wqp_result_header.csv").open()))
    assert len(names) == 81 and "DataQuality/PrecisionValue" in names
    # The header is recorded; one row carrying each name's own position as text exercises the names.
    record = {name: str(i) for i, name in enumerate(names)}
    frame = _written([{"type": "Feature", "geometry": {"type": "Point", "coordinates": [-95.9, 41.3]},
                       "properties": record}], spec)
    assert set(names) <= set(frame.columns)
    assert frame["DataQuality/PrecisionValue"].iloc[0] == record["DataQuality/PrecisionValue"]


def test_a_nested_object_is_flattened_to_the_dotted_names_a_member_reads(spec):
    frame = _written([{"type": "Feature", "geometry": {"type": "Point", "coordinates": [0.0, 0.0]},
                       "properties": {"status": {"observed": {"primary": 3.2}}, "id": "a"}}], spec)
    assert frame["status.observed.primary"].iloc[0] == pytest.approx(3.2)


def test_what_the_fetch_produced_is_written_beside_the_record(spec):
    frame = _written([{"type": "Feature", "geometry": {"type": "Point", "coordinates": [0.0, 0.0]},
                       "properties": {"value": "1.5"}, "produced": {"distance_km": 2.0}}], spec)
    assert frame["value"].iloc[0] == "1.5" and frame["distance_km"].iloc[0] == 2.0


def test_a_produced_name_the_source_already_uses_refuses_naming_it(spec):
    with pytest.raises(RouterUpstreamError) as caught:
        features_to_fgb_bytes([{"type": "Feature", "geometry": {"type": "Point", "coordinates": [0.0, 0.0]},
                                "properties": {"value": "1.5"}, "produced": {"value": 0.46}}], spec, {})
    assert caught.value.error_code.endswith("_FIELD_COLLISION")
    assert "value" in str(caught.value) and not caught.value.retryable


def test_a_row_reads_by_its_sources_map_and_restates_only_what_differs():
    stated = _SPECS["fetch_usgs_nwis_gauges"].model_dump(exclude_none=True)
    stated["fields"] = {"time": {"field": "datetime"}, "value": {"field": "discharge_cfs", "unit": "ft3/s"}}
    stage = next(row for row in stated["coverage"] if row["data_class"] == "water level series")
    stage["fields"] = {"value": {"field": "gage_height_ft", "unit": "ft"}}
    spec = SourceSpec.model_validate(stated)
    rows = {row.data_class: row for row in spec.coverage}
    assert rows["water level series"].fields["value"].field == "gage_height_ft"
    assert rows["water level series"].fields["time"].field == "datetime"
    assert rows["discharge series"].fields["value"].field == "discharge_cfs"
