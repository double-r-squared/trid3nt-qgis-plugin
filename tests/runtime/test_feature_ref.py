"""WHAT OF ITS CLASS a row asks for, where the feature is the CALLER'S.

Offline. One question can be asked of two kinds of water - a stretch of river
and a closed body - and which one it is is the caller's to say, so the need row
states ``of=`` as a read of the param that settles it. The read is bound at run
time the way the point the row is asked at is, through the same binder and the
same walk: there is no second mechanism, and a row that states a plain word
still states a plain word.
"""

from __future__ import annotations

import dataclasses

import pytest

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
    assert await interpreter._asked_of(env, _row(of=Ref("body"))) == asked


@pytest.mark.asyncio
async def test_the_need_the_match_filters_on_carries_the_bound_word() -> None:
    env = await _env(body="waterbody")
    need = await interpreter._need(env, _row(of=Ref("body")), "hydrography",
                                   "domain")
    assert need.of == "waterbody"


@pytest.mark.asyncio
async def test_a_row_that_asks_for_nothing_of_its_class_asks_for_nothing() -> None:
    env = await _env()
    assert await interpreter._asked_of(env, _row()) == ""
