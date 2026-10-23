"""An optional need: unfilled where no source covers, an either-or with the row it stands in for.

Offline. The match's probe is a stub, so what is proved is the RULE the fill applies to a row declared
optional, and to a pair of rows where one stands in for the other.
"""

from __future__ import annotations

import asyncio
import dataclasses
from types import SimpleNamespace

import pytest

from trid3nt_server.errors import DeclarationError
from trid3nt_server.inputs.input import Input
from trid3nt_server.inputs.run_notes import bind_notes, drain_notes
from trid3nt_server.workflows.runtime import fill
from trid3nt_server.workflows.runtime.errors import StepFailedError

_NOTHING = SimpleNamespace(sentence="nothing measures aquifer property here")


def _rows():
    stand = Input().need("aquifer property", of="aquifer_hydraulic_conductivity").optional_need(instead_of="transmissivity")
    return {"conductivity_layers": dataclasses.replace(stand, name="conductivity_layers"),
            "transmissivity": dataclasses.replace(
                Input().need("aquifer property", of="aquifer_transmissivity"), name="transmissivity")}


@pytest.fixture
def probed(monkeypatch):
    """The probe answers by the row's `of`: the K pair where `covered` holds, transmissivity always; what it was asked is recorded."""
    asked: list[str] = []
    covered = {"yes": True}

    async def probe(env, decl, data_class, label, *, read=None):
        asked.append(decl.name)
        if decl.name == "conductivity_layers" and not covered["yes"]:
            return _NOTHING, None
        return _NOTHING, ["frame"] if decl.name == "conductivity_layers" else 123.0

    async def nowhere(env):
        return None

    async def beside(env, decl):
        return {}

    monkeypatch.setattr(fill, "_probe", probe)
    monkeypatch.setattr(fill, "_somewhere_to_ask", nowhere)
    monkeypatch.setattr(fill, "_beside", beside)
    return asked, covered


def _env(**kw):
    rows = _rows()
    return fill._Env(data=rows, **kw)


def test_an_optional_row_no_source_covers_is_unfilled_and_a_required_one_refuses(probed):
    asked, covered = probed
    covered["yes"] = False
    env = _env()
    assert asyncio.run(fill.read(env, "conductivity_layers")) is None
    required = dataclasses.replace(_rows()["conductivity_layers"], optional=False, instead_of="")
    with pytest.raises(StepFailedError) as refused:
        asyncio.run(fill._matched(_env(), required))
    assert refused.value.error_code == "DATA_NEED_UNMATCHED"


def test_where_the_stand_in_fills_the_row_it_stands_in_for_is_not_asked_for(probed):
    asked, _ = probed
    env = _env()
    token = bind_notes()
    assert asyncio.run(fill.read(env, "transmissivity")) is None
    said = drain_notes(token)
    assert asked == ["conductivity_layers"]
    assert any("transmissivity is not asked for" in line and "conductivity_layers" in line for line in said)
    assert asyncio.run(fill.read(env, "conductivity_layers")) == ["frame"]


def test_where_no_stand_in_covers_the_row_it_stands_in_for_is_asked_as_today(probed):
    asked, covered = probed
    covered["yes"] = False
    env = _env()
    assert asyncio.run(fill.read(env, "transmissivity")) == 123.0
    assert asked == ["conductivity_layers", "transmissivity"]


def test_a_stated_transmissivity_wins_and_the_stand_in_is_not_read(probed):
    asked, _ = probed
    env = _env()
    env.supplied["transmissivity"] = 55.0
    token = bind_notes()
    assert asyncio.run(fill.read(env, "conductivity_layers")) is None
    said = drain_notes(token)
    assert asyncio.run(fill.read(env, "transmissivity")) == 55.0
    assert asked == []
    assert any("conductivity_layers is not read" in line for line in said)


def test_a_picked_transmissivity_source_also_wins(probed):
    asked, _ = probed
    env = _env()
    env.picks["transmissivity"] = "fetch_aquifer_transmissivity"
    assert asyncio.run(fill.read(env, "conductivity_layers")) is None
    assert asked == []


def test_an_optional_row_states_a_need():
    with pytest.raises(DeclarationError):
        Input(name="x", optional=True)
