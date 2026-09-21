"""WHAT OF ITS CLASS a row asks for: the caller's read, else the PLACE's own kind.

Offline. One question can be asked of two kinds of water - a stretch of river
and a closed body - and which one it is is the caller's to say, so the need row
states ``of=`` as a read of the param that settles it. The read is bound at run
time the way the point the row is asked at is, through the same binder and the
same walk: there is no second mechanism, and a row that states a plain word
still states a plain word. A DOMAIN that states no word at all is answered for
the water its SEED stands on, which is a fact of the place rather than of the
question asked there.
"""

from __future__ import annotations

import dataclasses

import pytest

from trid3nt_server.inputs.point import Point
from trid3nt_server.workflows.runtime import (
    Data, Param, Ref, doors, interpreter, resolve_params)


def _row(**need):
    return dataclasses.replace(Data.need("hydrography", **need), name="domain")


def _body(default: str = "reach") -> Param:
    return Param(name="body", door=doors.QUESTION, optional=True,
                 default=default, desc="reach | waterbody")


async def _env(**values):
    params = await resolve_params((_body(),), dict(values))
    return interpreter._Env(params=params, data={}, results={})


def test_a_stated_word_is_the_word() -> None:
    assert _row(of="reach").observes == "reach"


def test_a_ref_is_kept_as_the_read_it_is() -> None:
    """Not stringified at declaration: a description of a read that became a
    word would ask every run for a feature called ``Ref('body')``."""
    assert _row(of=Ref("body")).observes == Ref("body")


def test_the_walk_sees_the_read_the_row_declares() -> None:
    """The one walk the validator, the binder and the derivations share."""
    assert list(interpreter._refs(_row(of=Ref("body")).observes)) == [Ref("body")]


@pytest.mark.asyncio
@pytest.mark.parametrize("stated,asked", [({}, "reach"),
                                          ({"body": "waterbody"}, "waterbody")])
async def test_the_feature_asked_for_is_the_one_the_caller_settled(
        stated, asked) -> None:
    env = await _env(**stated)
    assert await interpreter._asked_of(env, _row(of=Ref("body")), None) == asked


@pytest.mark.asyncio
async def test_the_need_the_match_filters_on_carries_the_bound_word() -> None:
    env = await _env(body="waterbody")
    need = await interpreter._need(env, _row(of=Ref("body")), "hydrography",
                                   "domain")
    assert need.of == "waterbody"


@pytest.mark.asyncio
async def test_a_row_that_asks_for_nothing_of_its_class_asks_for_nothing() -> None:
    """A seed that said nothing about itself leaves the row asking for nothing,
    and every row the class has is ranked."""
    env = await _env()
    assert await interpreter._asked_of(env, _row(), None) == ""


@pytest.mark.asyncio
async def test_a_domain_states_no_feature_and_the_seed_says_which_water() -> None:
    """The kind is the PLACE's: a question that names no feature is answered
    for the water its seed stands on."""
    env = await _env()
    seed = Point(-123.2, 45.5, "Hagg Lake", "reservoir")
    assert await interpreter._asked_of(env, _row(), seed) == "waterbody"


@pytest.mark.asyncio
async def test_a_word_the_question_states_is_not_overruled_by_the_place() -> None:
    env = await _env()
    seed = Point(-123.2, 45.5, "Hagg Lake", "reservoir")
    assert await interpreter._asked_of(env, _row(of="coastline"), seed) \
        == "coastline"


@pytest.mark.asyncio
async def test_the_need_carries_the_place_s_kind() -> None:
    env = await _env()
    row = dataclasses.replace(_row(), coercion={"near": Point(-123.2, 45.5,
                                                             "Gales Creek",
                                                             "stream")})
    need = await interpreter._need(env, row, "hydrography", "domain")
    assert need.of == "flowline"
