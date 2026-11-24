"""The SDK's own help: the same descriptions an LLM reads, without leaving Python."""

from __future__ import annotations

import pytest

from trid3nt import case
from trid3nt.inputs.user_input import WireArgsError
from trid3nt.tools.search.describe_keywords.describe_keywords import (
    describe_keywords,
)


def test_model_help_lists_an_input_with_its_origin(tmp_path):
    m = case(tmp_path / "sdk-help-list").telemac_dye_release(
        release=[-122.669784, 45.518485], spill_duration_s=300.0)
    shown = m.help()
    assert "release = [-122.669784, 45.518485]" in shown
    assert "[user]" in shown


def test_model_help_by_name_returns_the_describe_keywords_text(tmp_path):
    m = case(tmp_path / "sdk-help-input").calibrate()
    expected = describe_keywords(template="calibrate", keyword="parameters")["entry"]["help"]
    assert m.help("parameters") == expected


def test_model_help_names_an_engine_keyword(tmp_path):
    m = case(tmp_path / "sdk-help-keyword").telemac_dye_release()
    expected = describe_keywords(module="telemac2d", keyword="FRICTION COEFFICIENT")["entry"]["help"]
    assert m.help("FRICTION COEFFICIENT") == expected


def test_model_help_refuses_an_unknown_name_with_the_nearest(tmp_path):
    m = case(tmp_path / "sdk-help-unknown").telemac_dye_release()
    with pytest.raises(WireArgsError) as refused:
        m.help("friction_coefficent")
    assert "FRICTION_COEFFICIENT" in str(refused.value)


def test_case_help_lists_templates_and_bare_entries(tmp_path):
    shown = case(tmp_path / "sdk-help-case").help()
    assert "telemac_dye_release" in shown
    assert "telemac2d" in shown
