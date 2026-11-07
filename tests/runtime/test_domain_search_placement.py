"""A domain search with no domain is placed around the run's own stated point.

Offline: the fill, the match and the probe run as a template run runs them; only the fetcher call is stubbed to record what
every source was asked. With the spill point stated and no domain every source takes a box that holds the point and reaches
the row's own span; with neither, the domain is never guessed and refuses as before."""

from __future__ import annotations

import asyncio

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.workflows.runtime import fill
from trid3nt_server.workflows.runtime.errors import StepFailedError

_RELEASE = (-122.65, 45.53)


@pytest.fixture
def asked(monkeypatch):
    import trid3nt_server.main as _main
    from trid3nt_server.inputs.bound_domain import bind_domain, reset_domain

    _main._import_tools_registry()
    calls = []
    token = bind_domain(None)

    async def run(runner, kwargs, label):
        calls.append((runner, dict(kwargs)))
        raise RuntimeError("held nothing")

    monkeypatch.setattr(fill, "_call_runner", run)
    yield calls
    reset_domain(token)


def _domain_search(release):
    workflow = TOOL_REGISTRY["telemac_dye_release"].fn.workflow
    env = fill._Env(data={row.name: row for row in workflow.inputs}, run={"release": release})
    with pytest.raises(StepFailedError) as caught:
        asyncio.run(fill.read(env, "domain"))
    return caught.value


def test_with_the_spill_point_stated_and_no_domain_every_box_holds_the_point_and_reaches_the_span(asked):
    _domain_search(_RELEASE)
    boxes = [kw["bbox"] for _runner, kw in asked if "bbox" in kw]
    assert boxes, "no source was asked over a box"
    lon, lat = _RELEASE
    for west, south, east, north in boxes:
        assert west < lon < east and south < lat < north
        # the template's own reach (6 km) either side of the point, in degrees of latitude
        assert north - lat == pytest.approx(6.0 * 1000.0 / 111_320.0, rel=0.01)
    assert not [runner for runner, kw in asked if "bbox" not in kw and "seed_point" not in kw]


def test_a_search_placed_nowhere_is_a_search_without_a_box(asked, monkeypatch):
    """Fail proof: with the placement removed no source is asked over a box."""
    monkeypatch.setattr(fill, "_about", lambda lon, lat, span: None)
    _domain_search(_RELEASE)
    assert not [kw for _runner, kw in asked if "bbox" in kw]


def test_with_neither_a_point_nor_a_domain_the_domain_is_never_guessed(asked):
    refusal = _domain_search(None)
    assert refusal.error_code == "DATA_NEED_UNPLACED"
    assert asked == []


def test_a_row_with_no_span_asks_the_ground_the_point_stands_on():
    from trid3nt_server.tools.search.match import AROUND_PLACE_DEG

    west, south, east, north = fill._about(-100.0, 0.0, None)
    assert north - south == pytest.approx(2 * AROUND_PLACE_DEG)
    assert east - west == pytest.approx(2 * AROUND_PLACE_DEG)
