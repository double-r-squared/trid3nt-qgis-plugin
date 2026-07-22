"""Tests for ``trid3nt_server.tools.us_states`` (job-0261).

The state-name → NWS area-code mapping is the linchpin of the "weather
alerts for Texas must not spill into surrounding states" fix: both NWS
alert tools route free-form LLM location text through
``resolve_state_code`` to engage the precise server-side ``?area=`` filter.
"""

from __future__ import annotations

import pytest

from trid3nt_server.tools.us_states import (
    NWS_AREA_CODES,
    STATE_CODE_TO_NAME,
    STATE_NAME_TO_CODE,
    resolve_state_code,
    state_display_name,
)


# ---------------------------------------------------------------------------
# Mapping shape
# ---------------------------------------------------------------------------


def test_fifty_states_plus_dc_have_names() -> None:
    """50 states + DC must all be reachable by full name."""
    codes = set(STATE_NAME_TO_CODE.values())
    # 50 states + DC + 5 territories = 56 distinct codes.
    assert len(codes) == 56


def test_every_named_code_is_a_valid_nws_area_code() -> None:
    for name, code in STATE_NAME_TO_CODE.items():
        assert code in NWS_AREA_CODES, f"{name!r} maps to invalid code {code!r}"


def test_marine_zone_codes_present_but_unnamed() -> None:
    assert "GM" in NWS_AREA_CODES  # Gulf of Mexico marine zone
    assert "GM" not in STATE_CODE_TO_NAME


# ---------------------------------------------------------------------------
# resolve_state_code — the live-demo cases first
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        # The live-demo prompt was lowercase "texas".
        ("texas", "TX"),
        ("Texas", "TX"),
        ("TEXAS", "TX"),
        ("TX", "TX"),
        ("tx", "TX"),
        ("  Texas  ", "TX"),
        ("state of Texas", "TX"),
        ("the State of Texas", "TX"),
        # Multi-word states with messy whitespace.
        ("new  mexico", "NM"),
        ("New Mexico", "NM"),
        ("North   Carolina", "NC"),
        ("district of columbia", "DC"),
        ("Washington D.C.", "DC"),
        # Territories.
        ("Puerto Rico", "PR"),
        ("guam", "GU"),
        # Marine zone code passes through.
        ("GM", "GM"),
    ],
)
def test_resolve_state_code_accepts(text: str, expected: str) -> None:
    assert resolve_state_code(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "Houston",          # city, not a state
        "Lee County",       # county
        "12071",            # FIPS — fetch_nws_event handles these separately
        "Canada",           # not a US state
        "XX",               # not a valid code
        "Texa",             # typo — no fuzzy matching by design
        "Gulf of Mexico",   # marine zone by NAME is not supported
    ],
)
def test_resolve_state_code_rejects(text: str) -> None:
    assert resolve_state_code(text) is None


def test_resolve_state_code_non_string_returns_none() -> None:
    assert resolve_state_code(None) is None  # type: ignore[arg-type]
    assert resolve_state_code(42) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Display names
# ---------------------------------------------------------------------------


def test_state_display_name_roundtrip() -> None:
    assert state_display_name("TX") == "Texas"
    assert state_display_name("tx") == "Texas"
    assert state_display_name("DC") == "District of Columbia"


def test_state_display_name_marine_zone_echoes_code() -> None:
    assert state_display_name("GM") == "GM"
