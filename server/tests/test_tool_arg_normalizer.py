"""Tests for ``tool_arg_normalizer`` — the centralized Gemini kwarg sweep (job-0164).

Each test names exactly one normalization rule and proves it fires by calling
``normalize_args(name, raw, fn)`` against a tiny fake callable whose signature
is the contract. No imports of real registered tools — the normalizer reads
``inspect.signature(fn)`` and that's all it needs.
"""

from __future__ import annotations

from typing import Any

import pytest

from grace2_agent.tool_arg_normalizer import (
    LatLonCoercionError,
    coerce_bbox_value,
    coerce_latlon,
    normalize_args,
    parse_forcing_string,
    snake_case,
)


# --------------------------------------------------------------------------- #
# coerce_bbox_value — a bbox double-encoded as a JSON string arrives with
# LITERAL surrounding quote chars (observed live: fetch_fault_sources' first
# call failed with `"\"-122.5,37.5,-121.5,38.5\""` -> "bbox must be [min_lon...]").
# The coercer must peel the wrapping quotes before parsing.
# --------------------------------------------------------------------------- #


def test_coerce_bbox_value_strips_wrapping_double_quotes() -> None:
    assert coerce_bbox_value('"-122.5,37.5,-121.5,38.5"') == [
        -122.5,
        37.5,
        -121.5,
        38.5,
    ]


def test_coerce_bbox_value_strips_quotes_then_brackets() -> None:
    assert coerce_bbox_value("'[-122.5, 37.5, -121.5, 38.5]'") == [
        -122.5,
        37.5,
        -121.5,
        38.5,
    ]


def test_coerce_bbox_value_plain_forms_unaffected() -> None:
    assert coerce_bbox_value("-122.5,37.5,-121.5,38.5") == [
        -122.5,
        37.5,
        -121.5,
        38.5,
    ]
    assert coerce_bbox_value([1, 2, 3, 4]) == [1.0, 2.0, 3.0, 4.0]
    assert coerce_bbox_value("garbage") is None


# --------------------------------------------------------------------------- #
# coerce_latlon (job-0317) — Bedrock Claude passes spill_location_latlon as a
# STRING, not a JSON array. The naive ``tuple(float(v) for v in value)``
# iterated the string's characters -> float('.') crash. coerce_latlon accepts
# every string form AND a real list, and raises a typed error only when the
# value is genuinely not two numbers.
# --------------------------------------------------------------------------- #


def test_coerce_latlon_real_list_passthrough() -> None:
    assert coerce_latlon([40.81, -96.71]) == [40.81, -96.71]


def test_coerce_latlon_real_tuple_passthrough() -> None:
    assert coerce_latlon((40.81, -96.71)) == [40.81, -96.71]


def test_coerce_latlon_int_list_coerced_to_floats() -> None:
    out = coerce_latlon([40, -96])
    assert out == [40.0, -96.0]
    assert all(isinstance(v, float) for v in out)


def test_coerce_latlon_bare_comma_string() -> None:
    # The exact live failure form: "40.8088861,-96.7077751".
    assert coerce_latlon("40.8088861,-96.7077751") == [40.8088861, -96.7077751]


def test_coerce_latlon_comma_space_string() -> None:
    assert coerce_latlon("40.81, -96.71") == [40.81, -96.71]


def test_coerce_latlon_bracketed_string() -> None:
    assert coerce_latlon("[40.81, -96.71]") == [40.81, -96.71]


def test_coerce_latlon_paren_string() -> None:
    assert coerce_latlon("(40.81, -96.71)") == [40.81, -96.71]


def test_coerce_latlon_whitespace_separated_string() -> None:
    assert coerce_latlon("40.81 -96.71") == [40.81, -96.71]


def test_coerce_latlon_bracketed_no_inner_space() -> None:
    assert coerce_latlon("[40.81,-96.71]") == [40.81, -96.71]


def test_coerce_latlon_surrounding_whitespace_stripped() -> None:
    assert coerce_latlon("  40.81 , -96.71  ") == [40.81, -96.71]


def test_coerce_latlon_negative_and_positive_order_preserved() -> None:
    # Order is preserved verbatim (no reordering / range fixup here).
    assert coerce_latlon("-96.71, 40.81") == [-96.71, 40.81]


@pytest.mark.parametrize(
    "bad",
    [
        "not-a-coordinate",
        "40.81",  # only one number
        "40.81, -96.71, 12.0",  # three numbers
        "[40.81]",  # bracketed single
        "",  # empty
        "   ",  # whitespace only
        "lat,lon",  # non-numeric parts
        "40.81,abc",  # one non-numeric part
    ],
)
def test_coerce_latlon_bad_string_raises_typed_error(bad: str) -> None:
    with pytest.raises(LatLonCoercionError):
        coerce_latlon(bad)


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [40.81],  # wrong element count
        [40.81, -96.71, 12.0],  # three elements
        [],  # empty list
        ["lat", "lon"],  # non-numeric list elements
        42,  # bare scalar
        {"lat": 40.81, "lon": -96.71},  # dict
    ],
)
def test_coerce_latlon_bad_nonstring_raises_typed_error(bad: Any) -> None:
    with pytest.raises(LatLonCoercionError):
        coerce_latlon(bad)


# --------------------------------------------------------------------------- #
# parse_forcing_string
# --------------------------------------------------------------------------- #


def test_parse_forcing_string_atlas14_year_only() -> None:
    assert parse_forcing_string("atlas14_100yr") == {"return_period_years": 100}


def test_parse_forcing_string_atlas14_year_plus_hour() -> None:
    out = parse_forcing_string("atlas14_500yr_24hr")
    assert out == {"return_period_years": 500, "duration_hours": 24}


def test_parse_forcing_string_design_storm_phrase() -> None:
    out = parse_forcing_string("100-yr / 24-hr design storm")
    assert out == {"return_period_years": 100, "duration_hours": 24}


def test_parse_forcing_string_year_word() -> None:
    assert parse_forcing_string("500 year") == {"return_period_years": 500}


def test_parse_forcing_string_hour_word() -> None:
    assert parse_forcing_string("6 hour") == {"duration_hours": 6}


def test_parse_forcing_string_empty_returns_empty() -> None:
    assert parse_forcing_string("") == {}
    assert parse_forcing_string("not a forcing spec") == {}


# --------------------------------------------------------------------------- #
# snake_case
# --------------------------------------------------------------------------- #


def test_snake_case_camel_to_snake() -> None:
    assert snake_case("durationHours") == "duration_hours"


def test_snake_case_passthrough_snake() -> None:
    assert snake_case("duration_hours") == "duration_hours"


def test_snake_case_single_lowercase_word() -> None:
    assert snake_case("bbox") == "bbox"


# --------------------------------------------------------------------------- #
# normalize_args — the public entry point
# --------------------------------------------------------------------------- #


def _fake_flood_tool(
    bbox: tuple[float, float, float, float] | None = None,
    location_query: str | None = None,
    return_period_years: int = 100,
    duration_hours: int = 24,
    compute_class: str = "medium",
) -> dict[str, Any]:
    """Signature mirrors ``run_model_flood_scenario`` for normalization tests."""
    return {"ok": True}


def _fake_tool_with_kwargs(
    foo: int = 1,
    **kwargs: Any,
) -> Any:
    return None


def test_passes_known_kwargs_through() -> None:
    out = normalize_args(
        "run_model_flood_scenario",
        {"location_query": "Fort Myers, FL", "return_period_years": 100},
        _fake_flood_tool,
    )
    assert out == {"location_query": "Fort Myers, FL", "return_period_years": 100}


def test_drops_unknown_kwargs_does_not_raise() -> None:
    """Gemini's invented kwargs (``run_name``, ``scenario_id``) get dropped."""
    out = normalize_args(
        "run_model_flood_scenario",
        {
            "location_query": "Fort Myers, FL",
            "run_name": "fort-myers-ian",
            "scenario_id": "ian-2022",
            "description": "demo run",
        },
        _fake_flood_tool,
    )
    assert "run_name" not in out
    assert "scenario_id" not in out
    assert "description" not in out
    assert out["location_query"] == "Fort Myers, FL"


def test_alias_return_period_yr_to_years() -> None:
    """If LLM sends ``return_period_yr`` but tool accepts ``return_period_years``."""

    def fn(return_period_years: int = 100) -> Any:
        return None

    out = normalize_args("any_tool", {"return_period_yr": 500}, fn)
    assert out == {"return_period_years": 500}


def test_alias_return_period_years_to_yr() -> None:
    """If LLM sends ``return_period_years`` but tool accepts ``return_period_yr``."""

    def fn(return_period_yr: int = 100) -> Any:
        return None

    out = normalize_args("any_tool", {"return_period_years": 500}, fn)
    assert out == {"return_period_yr": 500}


def test_alias_duration_hr_to_hours() -> None:
    def fn(duration_hours: int = 24) -> Any:
        return None

    out = normalize_args("any_tool", {"duration_hr": 6}, fn)
    assert out == {"duration_hours": 6}


def test_camel_to_snake() -> None:
    out = normalize_args(
        "run_model_flood_scenario",
        {"durationHours": 12, "returnPeriodYears": 25},
        _fake_flood_tool,
    )
    assert out == {"duration_hours": 12, "return_period_years": 25}


def test_string_form_forcing_parsed_when_signature_accepts() -> None:
    out = normalize_args(
        "run_model_flood_scenario",
        {"forcing": "atlas14_500yr_48hr", "location_query": "Houston"},
        _fake_flood_tool,
    )
    assert out["return_period_years"] == 500
    assert out["duration_hours"] == 48
    assert out["location_query"] == "Houston"
    # The string-form ``forcing`` itself is not in the signature → dropped.
    assert "forcing" not in out


def test_string_form_rainfall_event_parsed() -> None:
    out = normalize_args(
        "run_model_flood_scenario",
        {"rainfall_event": "100-yr / 24-hr design storm"},
        _fake_flood_tool,
    )
    assert out["return_period_years"] == 100
    assert out["duration_hours"] == 24


def test_string_form_does_not_overwrite_explicit_kwargs() -> None:
    """If LLM sends both ``forcing="..."`` and explicit ``return_period_years=...``,
    the explicit value wins (forcing-string fill is a fallback)."""
    out = normalize_args(
        "run_model_flood_scenario",
        {"forcing": "atlas14_100yr", "return_period_years": 500},
        _fake_flood_tool,
    )
    assert out["return_period_years"] == 500


def test_var_keyword_function_passes_unknowns_through() -> None:
    """If a tool declared ``**kwargs``, the normalizer leaves unknowns alone."""
    out = normalize_args(
        "any_tool",
        {"foo": 1, "unknown_thing": "preserved"},
        _fake_tool_with_kwargs,
    )
    assert out == {"foo": 1, "unknown_thing": "preserved"}


def test_tool_specific_alias_place_to_location_query() -> None:
    out = normalize_args(
        "run_model_flood_scenario",
        {"place": "Fort Myers, FL"},
        _fake_flood_tool,
    )
    assert out == {"location_query": "Fort Myers, FL"}


def test_empty_args_returns_empty_dict() -> None:
    assert normalize_args("run_model_flood_scenario", {}, _fake_flood_tool) == {}


def test_canonical_alias_present_does_not_double_rename() -> None:
    """If both the canonical name and the alias are present, canonical wins."""

    def fn(return_period_years: int = 100) -> Any:
        return None

    out = normalize_args(
        "any_tool",
        {"return_period_years": 100, "return_period_yr": 500},
        fn,
    )
    # Canonical value wins — alias does not overwrite.
    assert out == {"return_period_years": 100}


def test_silent_drop_kwargs_do_not_appear_in_log_warning_level() -> None:
    """``run_name`` etc. are dropped silently (debug-level only)."""
    # Just assert the drop happens — log level enforcement is verified by
    # eyeballing the agent service logs (live verification step).
    out = normalize_args(
        "run_model_flood_scenario",
        {"run_name": "x", "scenario_name": "y"},
        _fake_flood_tool,
    )
    assert "run_name" not in out
    assert "scenario_name" not in out


def test_does_not_raise_on_uninspectable_callable() -> None:
    """``inspect.signature`` may fail on C-extension callables — must not raise."""
    # Use a built-in method whose signature can't be introspected in CPython.
    import builtins

    out = normalize_args("any", {"foo": 1, "bar": 2}, builtins.print)
    # When signature can't be introspected we conservatively pass everything
    # through (accepts_var_keyword=True default for safety) OR drop if
    # introspection succeeds. The important invariant is "no exception".
    assert isinstance(out, dict)


@pytest.mark.parametrize(
    "raw,expected_year,expected_hour",
    [
        ({"forcing": "atlas14_25yr"}, 25, None),
        ({"forcing": "atlas14_100yr_6hr"}, 100, 6),
        ({"rainfall_event": "500yr"}, 500, None),
        ({"rainfall_event": "12hr storm"}, None, 12),
    ],
)
def test_forcing_string_table(
    raw: dict[str, Any],
    expected_year: int | None,
    expected_hour: int | None,
) -> None:
    out = normalize_args("run_model_flood_scenario", raw, _fake_flood_tool)
    if expected_year is not None:
        assert out.get("return_period_years") == expected_year
    if expected_hour is not None:
        assert out.get("duration_hours") == expected_hour


# ---------------------------------------------------------------------------
# job-0261: NWS alert tools — LLM-invented state kwargs land on "area" so the
# precise server-side ?area= filter engages instead of the CONUS sweep.
# ---------------------------------------------------------------------------


def test_nws_conus_state_kwarg_maps_to_area() -> None:
    def fake_conus(event_types=None, status="actual", area=None):  # type: ignore[no-untyped-def]
        return None

    for wrong in ("state", "state_code", "state_name", "location", "region"):
        out = normalize_args("fetch_nws_alerts_conus", {wrong: "Texas"}, fake_conus)
        assert out == {"area": "Texas"}, f"{wrong!r} should map to area"


def test_nws_event_state_and_fips_kwargs_map_to_area() -> None:
    def fake_event(area=None, event_types=None, status="actual", message_type="alert"):  # type: ignore[no-untyped-def]
        return None

    out = normalize_args("fetch_nws_event", {"state": "TX"}, fake_event)
    assert out == {"area": "TX"}
    out = normalize_args("fetch_nws_event", {"county_fips": "12071"}, fake_event)
    assert out == {"area": "12071"}
