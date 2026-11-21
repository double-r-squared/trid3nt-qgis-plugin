"""A data row stated 'none' is a stated absence: nothing is fetched for it and the run reads it as nothing measured.

The river templates derive their outflow where no level is read, so 'none' on `level` is how a run declares that."""

from __future__ import annotations

import asyncio

import pytest

from trid3nt.tools import TOOL_REGISTRY
from trid3nt.workflows.runtime import fill as fill_module
from trid3nt.workflows.runtime.fill import Fill, fill

_RING = [[-122.6769, 45.5153], [-122.6615, 45.5153], [-122.6615, 45.5198], [-122.6769, 45.5198]]
_STATED = {"domain": _RING, "bed": 4.0, "release": [-122.669784, 45.518485], "discharge": 56.6,
           "event_time": "2026-11-10T18:00:00Z"}


@pytest.fixture(autouse=True)
def _auto_input_mode(user_settings):
    user_settings(AUTO=True)


@pytest.fixture
def fetched(monkeypatch):
    calls: list[str] = []
    real = fill_module._produce

    async def _produce(env, row):
        # The level the match would fetch stands in as a number, so the suite asks no service.
        calls.append(row.name)
        return 0.0 if row.name == "level" else await real(env, row)

    monkeypatch.setattr(fill_module, "_produce", _produce)
    return calls


def _dye(values):
    return asyncio.run(fill(Fill(workflow=TOOL_REGISTRY["telemac_dye_release"].fn.workflow), values))


@pytest.mark.parametrize("word", ["none", " None "])
def test_a_row_stated_none_is_accepted_empty_and_never_produced(fetched, word):
    state = _dye({**_STATED, "level": word})
    assert state.inputs["level"].state == "accepted" and state.inputs["level"].value is None
    assert state.env.run["level"] is None
    assert "level" not in fetched


def test_a_row_left_unstated_is_still_produced(fetched):
    _dye(_STATED)
    assert "level" in fetched
