"""event_time accepts 'none': an ask with no moment states it, and no source is asked for one.

'none' still satisfies the required list; a need left unfilled under it refuses naming the need,
never asking for a time; 'now', a date and an omitted moment keep their behaviour.
"""

from __future__ import annotations

import asyncio

import pytest

from trid3nt_server.inputs.instant import event_time, instant
from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.workflows.runtime.fill import Fill, fill

_RING = [[-122.6769, 45.5153], [-122.6615, 45.5153], [-122.6615, 45.5198], [-122.6769, 45.5198]]
_STATED = {"domain": _RING, "bed": 4.0, "release": [-122.669784, 45.518485],
           "discharge": 56.6, "level": 0.0}


@pytest.fixture(autouse=True)
def _auto_input_mode(user_settings):
    user_settings(AUTO=True)


def _dye(values: dict):
    workflow = TOOL_REGISTRY["telemac_dye_release"].fn.workflow
    return asyncio.run(fill(Fill(workflow=workflow), values))


def _refused(state) -> dict:
    """Every refused input by name; the AUTO review verdict is the session's, not an input's."""
    return {name: verdict for name, verdict in state.inputs.items()
            if name != "AUTO" and verdict.state in ("rejected", "missing")}


def test_the_coercion_passes_none_through_and_still_parses_the_rest():
    coerce = event_time()
    assert coerce({"event_time": " None "}) == {"event_time": "none"}
    assert coerce({"event_time": "2026-03-04"})["event_time"].startswith("2026-03-04")
    assert coerce({"event_time": "now"})["event_time"] is not None
    assert coerce({})["event_time"] is None
    with pytest.raises(Exception):
        instant("none")


def test_the_description_front_loads_the_three_forms():
    from trid3nt_server.model.adapters.adapter import _tool_schema

    schema = _tool_schema(TOOL_REGISTRY["telemac_dye_release"].fn)
    assert "event_time" in schema["required"]
    head = schema["properties"]["event_time"]["description"][:120]
    assert "'now'" in head and "ISO" in head and "'none'" in head


def test_none_satisfies_the_required_moment_and_holds_no_instant():
    state = _dye({**_STATED, "event_time": "none"})
    assert state.inputs["event_time"].state == "accepted"
    assert state.inputs["event_time"].value is None
    assert not _refused(state)


def test_none_with_every_observation_stated_fills_without_a_refusal():
    assert not _refused(_dye({**_STATED, "event_time": "none"}))


def test_an_omitted_moment_is_still_refused_naming_it():
    state = _dye(_STATED)
    assert set(_refused(state)) == {"event_time"}


def test_none_with_a_need_unstated_refuses_naming_the_need_not_a_time():
    stated = {key: value for key, value in _STATED.items() if key != "discharge"}
    refused = _refused(_dye({**stated, "event_time": "none"}))
    assert set(refused) == {"discharge"}
    message = refused["discharge"].reason
    assert "as a number or a layer" in message
    assert "State the moment" not in message and "event_time" not in message


@pytest.mark.parametrize("moment", ["now", "2026-03-04"])
def test_now_and_a_date_are_unchanged(moment):
    state = _dye({**_STATED, "event_time": moment})
    assert state.inputs["event_time"].value is not None
    assert state.inputs["event_time"].value.startswith(
        "2026-03-04" if moment != "now" else "20")
