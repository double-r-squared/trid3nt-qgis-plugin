"""Unit tests for the geocode PRECISION option's two pure decisions.

Covered: a state-snap answer recognised by what the fetcher states about it
rather than by its coordinates, a normal answer not mistaken for one, the
locality tail of every compound form, and a query with no tail."""

from __future__ import annotations

import pytest

from trid3nt_server.tools.fetchers.socioeconomic.geocode_location.precision import (
    PRECISION_LOCALITY,
    is_state_snap,
    locality_tail,
)


def test_the_option_names_the_one_precision_a_caller_can_ask_for() -> None:
    assert PRECISION_LOCALITY == "locality"


@pytest.mark.parametrize("answer", [
    {"source": "state-bbox-fallback", "latitude": 44.0, "longitude": -120.5},
    {"source": "nominatim", "fallback_reason": "query too vague"},
])
def test_a_state_snap_is_read_off_what_the_fetcher_states(answer: dict) -> None:
    assert is_state_snap(answer) is True


@pytest.mark.parametrize("answer", [
    {"source": "nominatim", "latitude": 45.5, "longitude": -122.6},
    {},
    None,
    "Portland",
])
def test_a_normal_answer_is_not_a_state_snap(answer: object) -> None:
    assert is_state_snap(answer) is False


@pytest.mark.parametrize("query,tail", [
    ("the Eel River near Scotia", "Scotia"),
    ("Willamette River at Portland, OR", "Portland, OR"),
    ("the channel by Port Huron", "Port Huron"),
    ("a reach outside Eugene", "Eugene"),
    ("the river in Multnomah County", "Multnomah County"),
])
def test_the_locality_tail_of_a_compound_name(query: str, tail: str) -> None:
    assert locality_tail(query) == tail


@pytest.mark.parametrize("query", ["Portland", "Oregon", "", "near"])
def test_a_query_with_no_locality_tail(query: str) -> None:
    assert locality_tail(query) is None
