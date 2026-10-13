"""WHAT OF ITS CLASS a row asks for, and WHAT KIND the domain is solved over.

Offline. A row that reads a record asks for the variable it observes. The
DOMAIN asks for a kind instead: the word a template states where the seed
cannot imply it - the land draining through a pour point, the water inside a
drawn box - and otherwise the kind of the place its SEED stands on, which is a
fact of the place rather than of the question asked there. A stated kind wins,
and no other row has a kind to state at all.
"""

from __future__ import annotations

import dataclasses

import pytest

from trid3nt_server.inputs.point import Point
from trid3nt_server.workflows.runtime import fill
from trid3nt_server.inputs.input import Input
from seated import resolve_params
from trid3nt_server.workflows.runtime.errors import DeclarationError
from trid3nt_server.inputs.slots import DOMAIN, OBSERVE, RAIN, WEATHER


def _row(**need):
    return dataclasses.replace(Input(DOMAIN).need("hydrography", **need), name="domain")


async def _env():
    return fill._Env(data={})


def test_a_stated_variable_is_what_a_record_row_asks_for() -> None:
    row = dataclasses.replace(Input(OBSERVE).need("water quality sample",
                                        of="TEMPERATURE"), name="observe")
    assert fill._asked_of(row, None) == "TEMPERATURE"


def test_a_domain_that_states_no_kind_and_stands_on_nothing_asks_for_nothing() -> None:
    """A seed that said nothing about itself leaves the row asking for nothing,
    and every row the class has is ranked."""
    assert fill._asked_of(_row(), None) == ""


def test_a_domain_states_no_kind_and_the_seed_says_which_water() -> None:
    seed = Point(-123.2, 45.5, "Hagg Lake", "reservoir")
    assert fill._asked_of(_row(), seed) == "waterbody"


def test_the_kind_a_template_states_is_not_overruled_by_the_place() -> None:
    seed = Point(-123.2, 45.5, "Hagg Lake", "reservoir")
    assert fill._asked_of(_row(kind="basin"), seed) == "basin"


def test_only_the_domain_has_a_kind_to_state() -> None:
    """A kind is the water the equations are solved over, so a row that is not
    the domain refuses one at the line that wrote it."""
    with pytest.raises(DeclarationError, match="is not a domain"):
        class DATA:
            rivers = Input().need("hydrography", kind="flowline")


@pytest.mark.asyncio
async def test_the_need_the_match_filters_on_carries_the_place_s_kind() -> None:
    env = await _env()
    row = dataclasses.replace(_row(), coercion={"near": Point(-123.2, 45.5,
                                                             "Gales Creek",
                                                             "stream")})
    need = await fill._need(env, row, "hydrography", "domain")
    assert need.of == "flowline"


@pytest.mark.asyncio
async def test_the_need_carries_the_kind_the_template_stated() -> None:
    env = await _env()
    need = await fill._need(env, _row(kind="basin"), "hydrography",
                                   "domain")
    assert need.of == "basin"


@pytest.mark.asyncio
@pytest.mark.parametrize("slot, data_class, reads", [
    (WEATHER, "weather forcing", {"fetch_asos_metar", "fetch_raws_weather"}),
    (RAIN, "precipitation series", {"fetch_aorc_precip"}),
])
async def test_a_slot_asks_only_the_sources_its_ingestion_reads(
        slot, data_class, reads) -> None:
    """A gridded product serving the class is excluded before it is fetched: the
    station reader takes points and the hyetograph reader a record."""
    import trid3nt_server.tools  # noqa: F401 - registers the fetcher rows
    from trid3nt_server.tools.search.match import match, sources_with_coverage

    row = dataclasses.replace(Input(slot).need(data_class), name="row")
    need = dataclasses.replace(
        await fill._need(await _env(), row, data_class, "row"),
        lon=-122.72, lat=45.57, opens="2024-01-10T00:00:00Z",
        until="2024-01-11T00:00:00Z")
    ranked = match(need, sources_with_coverage())
    assert {option.fetcher for option in ranked.rows
            if not option.excluded} == reads
    assert any("this slot reads its class as" in option.excluded
               for option in ranked.rows)
