"""The shared scalar check: coercion, refusal, and the range a refusal never restates.

The range is read at ingestion, so the refusal names the value and points at the
schema instead of carrying the numbers with it.
"""

from __future__ import annotations

import pytest

from trid3nt_server.inputs.validate import scalar


def test_a_number_string_is_coerced_and_returned_as_a_float():
    assert scalar("a", "4", (0.0, 5.0)) == 4.0


def test_bool_is_not_a_number():
    with pytest.raises(ValueError, match="a=True is not a number"):
        scalar("a", True, (0.0, 5.0))


def test_a_non_numeric_value_is_refused_and_never_defaulted():
    with pytest.raises(ValueError, match="not a number in m"):
        scalar("a", "a while", (0.0, 5.0), units="m")


def test_the_refusal_names_the_value_and_never_the_range():
    with pytest.raises(ValueError) as refused:
        scalar("a", 99.0, (0.0, 5.0), units="m")
    assert str(refused.value) == (
        "a=99 m is outside the declared range. State a value inside it.")


def test_a_position_names_the_element_of_a_list_the_refusal_is_about():
    with pytest.raises(ValueError, match=r"T=9 is outside the declared range at position 3"):
        scalar("T", 9.0, (0.0, 1.0), position=2)


def test_the_caller_states_the_readers_own_next_step():
    with pytest.raises(ValueError, match="look it up"):
        scalar("T", 9.0, (0.0, 1.0), beyond="look it up")


def test_a_value_inside_the_range_passes_through():
    assert scalar("a", 2.5, (0.0, 5.0)) == 2.5
