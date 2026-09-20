"""WHICH FEATURE of hydrography a reach question stands on.

Hydrography is one class and many things - a coastline, a basin divide, a
waterbody, a mapped reach - and the ten questions below model a STRETCH OF
RIVER. The feature the row asks for is what keeps a pond at the same seed off
the slot, so it is asserted once here for every one of them rather than ten
times in ten files.
"""

from __future__ import annotations

import pytest

#: The questions whose domain is a reach, by the tool the registry holds.
REACH_QUESTIONS = (
    "telemac_dye_release",
    "telemac_do_sag",
    "telemac_bed_scour",
    "telemac_oil_spill",
    "telemac_micropollutant_release",
    "telemac_sediment_plume",
    "telemac_water_temperature",
    "telemac_ice_cover",
    "telemac_eutrophication",
    "telemac_channel_dredging",
)


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
