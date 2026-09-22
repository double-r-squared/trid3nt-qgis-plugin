"""WHICH KIND OF WATER a question is solved over.

Hydrography is one class and many things - a coastline, a basin, a waterbody, a
flowline, the water surface a flowline runs between - and the KIND is what keeps
a pond at the same seed off a river question's slot. The kind is a fact of the
PLACE, read off the seed, so eleven questions state nothing at all; a question
whose seed cannot imply it - the land draining through a pour point, the water
inside a box drawn over land and water - states its own. Asserted once here
rather than fourteen times in fourteen files.
"""

from __future__ import annotations

import pytest

#: The questions whose domain is the water their SEED stands on.
SEEDED_QUESTIONS = (
    "telemac_dye_release",
    "telemac_do_sag",
    "telemac_bed_scour",
    "telemac_oil_spill",
    "telemac_micropollutant_release",
    "telemac_sediment_plume",
    "telemac_eutrophication",
    "telemac_channel_dredging",
    "telemac_water_temperature",
    "telemac_ice_cover",
    "telemac3d_stratified_flow",
)

#: The questions whose seed cannot imply the kind, and the kind each states.
STATED_QUESTIONS = {
    "telemac_rain_on_grid": "basin",
    "tomawac_nearshore_waves": "coastline",
    "artemis_harbor_agitation": "coastline",
    "tomawac_wave_driven_currents": "coastline",
}


def _picked(of: str) -> str:
    """The source the match picks for a hydrography domain of this kind."""
    from trid3nt_server.tools.search.match import (
        Need, match, sources_with_coverage)

    return match(Need(slot="domain", data_class="hydrography",
                      lon=-123.21, lat=45.49, of=of),
                 sources_with_coverage()).picked


def _domain(tool: str):
    from trid3nt_server.tools import TOOL_REGISTRY

    rows = {row.name: row for row in TOOL_REGISTRY[tool].fn.workflow.data}
    return rows["domain"]


@pytest.mark.parametrize("tool", SEEDED_QUESTIONS)
def test_a_question_whose_seed_says_the_kind_states_none(tool):
    domain = _domain(tool)
    assert domain.data_class == "hydrography"
    assert (domain.kind, domain.observes) == ("", "")


@pytest.mark.parametrize("tool,kind", sorted(STATED_QUESTIONS.items()))
def test_a_question_whose_seed_cannot_imply_the_kind_states_it(tool, kind):
    domain = _domain(tool)
    assert (domain.data_class, domain.kind) == ("hydrography", kind)


def test_a_place_on_a_flowline_takes_the_line_and_not_the_pond_beside_it():
    """The match is what enforces it: with the place's kind on the need, the
    only source left on the list is the one that publishes a flowline."""
    from trid3nt_server.tools.search.match import (
        Need, match, sources_with_coverage)

    choice = match(Need(slot="domain", data_class="hydrography",
                        lon=-122.67, lat=45.52, of="flowline"),
                   sources_with_coverage())
    assert choice.picked == "fetch_nhdplus_nldi_navigate"
    assert [row.fetcher for row in choice.rows if not row.excluded] == [
        "fetch_nhdplus_nldi_navigate"]


def test_each_kind_picks_the_source_that_publishes_it():
    assert _picked("flowline") == "fetch_nhdplus_nldi_navigate"
    assert _picked("water surface") == "fetch_nhd_water_surface"
    assert _picked("waterbody") == "fetch_nhd_waterbody_at_point"
    assert _picked("basin") == "fetch_watershed"
