"""A SLOT THAT DECLARES ROWS BESIDE ITS OWN, and the runtime that produces them.

Offline. A place on a flowline reaches the domain slot as a LINE, which is not
something the equations can be solved over: the slot declares the water surface
it needs beside the line and the runtime produces that through the match, under
the slot, the way the rows a merge op names are produced. Declaring a need is
not fetching - the slot states it and the match fills it - so what is proved
here is that the need is stated, that the produced row reaches the ingestion
under its own name, and that a kind declaring none asks for none.
"""

from __future__ import annotations

import dataclasses

import pytest

from trid3nt_server.inputs.point import Point
from trid3nt_server.inputs.slots import needs_of
from trid3nt_server.workflows.runtime import Data, fill, resolve_params


def _row(**need):
    return dataclasses.replace(Data.need("hydrography", **need), name="domain")


async def _env():
    return fill._Env(params=await resolve_params((), {}), data={},
                            results={})


def test_only_the_domain_declares_rows_beside_its_own() -> None:
    assert needs_of("domain", "flowline") == (("banks", "water surface",
                                               "polygon"),)
    assert needs_of("bed", "flowline") == ()
    assert needs_of("domain", "waterbody") == ()


@pytest.mark.asyncio
async def test_a_closed_kind_declares_nothing_and_asks_for_nothing() -> None:
    env = await _env()
    row = dataclasses.replace(_row(), coercion={"near": Point(
        -123.2, 45.5, "Hagg Lake", "reservoir")})
    assert await fill._beside(env, row) == {}


@pytest.mark.asyncio
async def test_a_place_on_a_flowline_has_its_water_surface_produced_for_it(
        monkeypatch) -> None:
    """The row is matched for the word it declared and produced under the slot,
    and it reaches the ingestion under the name the slot reads it back by."""
    asked: dict[str, str] = {}

    async def _probe(env, decl, data_class, label, **kw):
        asked[decl.name] = decl.observes
        return (None, f"{label}-layer")

    monkeypatch.setattr(fill, "_probe", _probe)
    env = await _env()
    row = dataclasses.replace(_row(span_km=3.0), coercion={"near": Point(
        -122.67, 45.52, "Willamette River", "river")})
    assert await fill._beside(env, row) == {
        "banks": "domain_banks-layer"}
    assert asked == {"domain_banks": "water surface"}


@pytest.mark.asyncio
async def test_a_declared_row_nothing_measured_refuses_by_name(monkeypatch) -> None:
    async def _probe(env, decl, data_class, label, **kw):
        return (None, None)

    monkeypatch.setattr(fill, "_probe", _probe)
    env = await _env()
    row = dataclasses.replace(_row(), coercion={"near": Point(
        -122.67, 45.52, "Willamette River", "river")})
    with pytest.raises(Exception, match="water surface"):
        await fill._beside(env, row)
