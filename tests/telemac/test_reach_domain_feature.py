"""WHICH FEATURE of hydrography a question stands on.

Hydrography is one class and many things - a coastline, a basin divide, a
waterbody, a mapped reach - and the feature the row asks for is what keeps a
pond at the same seed off a reach question's slot, and a reach off a lake
question's. Eight questions model a STRETCH OF RIVER and say so; one models a
CLOSED BODY and says so; two are asked of either and read the caller's own
word. Asserted once here rather than eleven times in eleven files.
"""

from __future__ import annotations

import pytest

#: The questions whose domain is a reach whatever they are asked of, by the tool
#: the registry holds.
REACH_QUESTIONS = (
    "telemac_dye_release",
    "telemac_do_sag",
    "telemac_bed_scour",
    "telemac_oil_spill",
    "telemac_micropollutant_release",
    "telemac_sediment_plume",
    "telemac_eutrophication",
    "telemac_channel_dredging",
)

#: The questions asked of EITHER kind of water, whose feature is the caller's.
EITHER_QUESTIONS = ("telemac_water_temperature", "telemac_ice_cover")


def _picked(of: str) -> str:
    """The source the match picks for a hydrography domain asked for ``of``."""
    from trid3nt_server.tools.search.match import (
        Need, match, sources_with_coverage)

    return match(Need(slot="domain", data_class="hydrography",
                      lon=-123.21, lat=45.49, of=of),
                 sources_with_coverage()).picked


def _domain(tool: str):
    from trid3nt_server.tools import TOOL_REGISTRY

    rows = {row.name: row for row in TOOL_REGISTRY[tool].fn.workflow.data}
    return rows["domain"]


@pytest.mark.parametrize("tool", REACH_QUESTIONS)
def test_a_reach_question_asks_hydrography_for_the_reach(tool):
    domain = _domain(tool)
    assert domain.data_class == "hydrography"
    assert domain.observes == "reach"


@pytest.mark.parametrize("tool", REACH_QUESTIONS)
def test_a_pond_at_the_seed_never_outranks_the_reach(tool):
    """The match is what enforces it: with the feature named, the only source
    left on the list is the one that publishes a reach."""
    from trid3nt_server.tools.search.match import (
        Need, match, sources_with_coverage)

    domain = _domain(tool)
    choice = match(Need(slot="domain", data_class=domain.data_class,
                        lon=-122.67, lat=45.52, of=domain.observes),
                   sources_with_coverage())
    assert choice.picked == "fetch_river_reach"
    assert [row.fetcher for row in choice.rows if not row.excluded] == [
        "fetch_river_reach"]


def test_a_closed_body_question_asks_hydrography_for_the_waterbody():
    """Stratification is a question about a LAKE: the reach mapped at the same
    seed is not the water it is asked of."""
    domain = _domain("telemac3d_stratified_flow")
    assert (domain.data_class, domain.observes) == ("hydrography", "waterbody")
    assert _picked("waterbody") == "fetch_nhd_waterbody_at_point"


@pytest.mark.parametrize("tool", EITHER_QUESTIONS)
def test_a_question_asked_of_either_water_reads_the_caller_s_word(tool):
    """One question, two kinds of water: the row states the feature as a read of
    the param that settles it rather than fixing one of them."""
    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.workflows.runtime import Ref

    assert _domain(tool).observes == Ref("body")
    body = {row.name: row
            for row in TOOL_REGISTRY[tool].fn.workflow.params}["body"]
    assert body.default == "reach"
    assert "waterbody" in body.desc


def test_each_word_picks_the_source_that_publishes_it():
    assert _picked("reach") == "fetch_river_reach"
    assert _picked("waterbody") == "fetch_nhd_waterbody_at_point"
