"""``describe_keywords``: the read over the module catalogs.

The keyword surface is larger than any docstring budget, so what is checked is
that a question in WORDS reaches the keyword that answers it, that the answer
carries the dictionary's own help, choices and default, and that it decides
nothing. ASCII only."""

from __future__ import annotations

import pytest

from trid3nt_server.workflows.telemac.modules.describe import (
    DescribeKeywordsError,
    describe_keywords,
)


def _keywords(answer) -> dict:
    return {row["keyword"]: row
            for section in answer["matches"] for row in section["keywords"]}


def test_a_question_in_words_reaches_the_keyword_that_answers_it():
    for query, wanted in (
        ("what governs the bottom friction", "LAW OF BOTTOM FRICTION"),
        ("graphic printout period", "GRAPHIC PRINTOUT PERIOD"),
        ("turbulence model", "TURBULENCE MODEL"),
        ("tidal flats", "TIDAL FLATS"),
        ("how long does the simulation run", "DURATION"),
    ):
        found = _keywords(describe_keywords(module="telemac2d", query=query))
        assert wanted in found, f"{query!r} did not surface {wanted!r}: {list(found)}"


def test_a_match_carries_the_dictionary_s_own_help_choices_and_default():
    row = _keywords(describe_keywords(
        module="telemac2d", query="law of bottom friction"))["LAW OF BOTTOM FRICTION"]
    assert "friction" in row["help"].lower()
    assert row["choices"]["4"] == "MANNING"
    assert row["type"] == "INTEGER"
    # The dictionary answers this keyword for nobody, and the row says so rather
    # than rendering an engine default that does not exist.
    assert row["engine_default"] is None and row["open"] is True


def test_a_defaulted_keyword_states_the_value_the_engine_will_use():
    row = _keywords(describe_keywords(
        module="telemac2d", query="maximum number of friction domains"))[
            "MAXIMUM NUMBER OF FRICTION DOMAINS"]
    assert row["engine_default"] == 10
    assert "open" not in row


def test_the_same_question_is_answered_the_same_way_twice():
    first = describe_keywords(module="telemac2d", query="advection scheme")
    assert first == describe_keywords(module="telemac2d", query="advection scheme")


def test_an_empty_query_returns_the_section_index_and_not_the_whole_dictionary():
    answer = describe_keywords(module="telemac2d")
    assert answer["keyword_count"] == 376
    assert sum(row["keywords"] for row in answer["sections"]) == 376
    assert "matches" not in answer
    assert "telemac3d" in answer["modules"]


def test_every_exposed_module_answers_a_question_of_its_own():
    for module, query, wanted in (
        ("telemac3d", "how many vertical levels", "NUMBER OF HORIZONTAL LEVELS"),
        ("artemis", "wave period", "WAVE PERIOD"),
        ("waqtel", "water temperature", "WATER TEMPERATURE"),
        ("gaia", "hindered settling", "HINDERED SETTLING"),
        ("tomawac", "wind generation", "WIND GENERATION"),
    ):
        found = _keywords(describe_keywords(module=module, query=query))
        assert wanted in found, f"{module}/{query!r}: {list(found)}"


def test_the_limit_is_the_caller_s_and_is_clamped():
    assert sum(len(s["keywords"]) for s in describe_keywords(
        module="telemac2d", query="friction", limit=3)["matches"]) == 3
    assert sum(len(s["keywords"]) for s in describe_keywords(
        module="telemac2d", query="friction", limit=999)["matches"]) <= 50


def test_a_module_with_no_catalog_refuses_naming_the_ones_there_are():
    with pytest.raises(DescribeKeywordsError) as caught:
        describe_keywords(module="sisyphe", query="friction")
    assert caught.value.error_code == "UNKNOWN_MODULE"
    assert "telemac2d" in str(caught.value)


def test_the_tool_is_registered_read_only_and_reaches_the_retrieval_pool():
    import trid3nt_server.main as main

    main._import_tools_registry()
    from trid3nt_server.tools import TOOL_REGISTRY

    entry = TOOL_REGISTRY["describe_keywords"]
    assert entry.metadata.read_only_hint is True
    assert entry.metadata.destructive_hint is False
    assert getattr(entry.metadata, "tier", "general") != "internal"


def test_every_corpus_phrasing_surfaces_the_tool_model_free():
    """The new-tool law: the phrasings a practitioner would use reach it through
    the hashed, model-free retrieval path, not only through a dense index."""
    import yaml

    import trid3nt_server.main as main
    from trid3nt_server.workflows.telemac.modules.describe import __file__ as here
    from trid3nt_server.tools.search.tool_retrieval import retrieve_visible_tools

    main._import_tools_registry()
    from pathlib import Path

    corpus = yaml.safe_load(
        (Path(here).parent / "corpus.yaml").read_text())["describe_keywords"]
    missed = [q for q in corpus
              if "describe_keywords" not in retrieve_visible_tools(q, None, 8)]
    assert not missed, f"corpus phrasings that do not surface the tool: {missed}"
