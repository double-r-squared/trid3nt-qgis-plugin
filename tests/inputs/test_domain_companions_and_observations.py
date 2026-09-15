"""What a domain producer measured BESIDE the polygon, and the value a run opens on.

A producer that walks a reach measures a centerline on the way; one that
delineates a catchment measures a snapped outlet. Those ride ON the domain
artifact under the producer's own names, so a question that needs one asks for
that one. The OBSERVATION slot is the same idea for a number: the record goes in,
one reading comes out, in the unit the keyword reads, with its age stated.
"""

from __future__ import annotations

import pytest

from trid3nt_server.inputs.boundary import OPEN_TYPES, RATING, RUN_TYPES, boundary_runs
from trid3nt_server.inputs.boundary import roles_from_runs
from trid3nt_server.inputs.domain import domain
from trid3nt_server.inputs.observation import (
    Observation,
    ObservationError,
    observation,
)
from trid3nt_server.inputs.slots import ingest_slot
from trid3nt_server.inputs.user_input import UserInputError
from trid3nt_server.workflows.runtime.data import Data, OBSERVATION, tool

_POLYGON = {"type": "Polygon", "coordinates": [[
    [-122.70, 45.50], [-122.60, 45.50], [-122.60, 45.56], [-122.70, 45.56],
    [-122.70, 45.50]]]}
_CENTERLINE = {"type": "LineString",
               "coordinates": [[-122.695, 45.53], [-122.605, 45.53]]}


def _reach(*extra):
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"part": "reach"},
         "geometry": _POLYGON},
        {"type": "Feature", "properties": {"part": "centerline"},
         "geometry": _CENTERLINE},
        *extra]}


def test_a_producers_companion_rides_on_the_domain_under_its_own_name():
    reach = domain(_reach())
    assert reach.centerline == _CENTERLINE
    assert reach.companions == {"centerline": _CENTERLINE}


def test_a_drawn_outline_carries_no_companion_and_says_so_by_absence():
    """A ref to a companion a domain has not got refuses at binding, which is
    what ``getattr`` returning nothing is for."""
    pond = domain(_POLYGON)
    assert pond.companions == {}
    with pytest.raises(AttributeError):
        pond.centerline


def test_a_run_row_is_a_boundary_run_and_never_a_companion():
    """The producer names its rows; the slot reads the runs as runs and keeps
    what is left as geometry the question can ask for."""
    reach = domain(_reach(
        {"type": "Feature", "properties": {"part": "outflow"},
         "geometry": {"type": "LineString",
                      "coordinates": [[-122.60, 45.50], [-122.60, 45.56]]}}))
    assert [run.type for run in reach.runs] == ["outflow"]
    assert set(reach.companions) == {"centerline"}


def test_the_domain_ranks_a_nearest_site_against_a_point_inside_itself():
    """A crescent bay's mean vertex sits on land, so the point is a
    representative one rather than an average."""
    lon, lat = domain(_POLYGON).centroid
    assert -122.70 < lon < -122.60 and 45.50 < lat < 45.56


def test_a_catchment_outlet_is_a_run_type_spelled_as_the_role_it_prescribes():
    """One vocabulary: the run type IS the engine role, so nothing translates."""
    assert RATING == "rating_curve"
    assert RATING in RUN_TYPES and RATING in OPEN_TYPES
    runs = boundary_runs({"type": "Feature", "properties": {"type": RATING},
                          "geometry": {"type": "LineString",
                                       "coordinates": [[0.0, 0.0], [0.0, 1.0]]}})
    assert roles_from_runs(runs) == {RATING: [runs[0].face]}


def test_a_run_type_nothing_prescribes_refuses_rather_than_reaching_the_mesh():
    with pytest.raises(UserInputError):
        boundary_runs([{"properties": {"type": "spillway"},
                        "geometry": {"type": "LineString",
                                     "coordinates": [[0.0, 0.0], [0.0, 1.0]]}}])


_SITES = {"type": "FeatureCollection", "features": [
    {"type": "Feature", "geometry": {"type": "Point",
                                     "coordinates": [-122.66, 45.53]},
     "properties": {"value": 50.0, "unit": "degF", "site_id": "NEAR",
                    "site_name": "Willamette at Portland",
                    "result_date": "2026-03-04"}},
    {"type": "Feature", "geometry": {"type": "Point",
                                     "coordinates": [-121.00, 44.00]},
     "properties": {"value": 9.0, "unit": "degC", "site_id": "FAR",
                    "result_date": "2026-03-05"}}]}


def test_the_nearest_site_the_unit_and_the_age_are_the_slots_own_ingestion():
    found = observation(_SITES, near=(-122.65, 45.53), to_units="degC",
                        measures="a water temperature")
    assert found.site_id == "NEAR"
    assert found.value == pytest.approx(10.0)
    assert found.units == "degC"
    assert found.sampled == "2026-03-04"
    assert found.distance_km is not None and found.distance_km < 5.0


def test_a_number_stated_on_the_call_stands_over_any_record():
    found = observation(11.5, to_units="degC")
    assert found == Observation(value=11.5, units="degC")


def test_nothing_reporting_it_near_this_place_refuses_typed():
    with pytest.raises(ObservationError):
        observation({"type": "FeatureCollection", "features": []},
                    measures="a water temperature", label="the sample record")


def test_the_observation_slot_reads_through_its_rows_own_coercion():
    """A row tells its slot where to rank from and which unit the keyword reads;
    the ingestion does the rest and nothing downstream branches on the source."""
    assert ingest_slot(OBSERVATION, _SITES, label="water_temperature",
                       near=(-122.65, 45.53), to_units="degC").value == \
        pytest.approx(10.0)


def test_the_row_declares_the_slot_and_what_the_model_reads_off_it():
    row = Data.observation(
        tool("fetch_usgs_water_quality", characteristic="temperature"),
        near=(-122.65, 45.53), units="degC",
        measures="the water temperature", opens="the water opens at")
    assert row.role == OBSERVATION
    assert row.coercion["to_units"] == "degC"
    assert row.coercion["field"] == "value"
    # A reading is a record to read it off, OR the number itself.
    assert row.wire_annotation == (str | float | None)
    assert "the number itself" in row.doc_line
    assert "degC" in row.doc_line
