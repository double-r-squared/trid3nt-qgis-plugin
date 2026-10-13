"""Offline guard: a TELEMAC coercion never falsifies a value's provenance.

A coercion's return merges into the supplied sheet, so anything it emits for an
argument nobody sent resolves through the USER door and is stamped as a user
choice. The rule pinned here is ABSENT in, nothing out. The resolution spine runs
offline - no solver and no network."""

from __future__ import annotations

import asyncio

import pytest
from seated import value_inputs

#: REGISTERED TELEMAC templates carrying the engine's ``event_time``, and the bare
#: natural prompt each one is asked. The parked declarations are absent: a tool
#: off the model surface has no invocation for a provenance row to describe.
TELEMAC_TEMPLATES: tuple[tuple[str, str], ...] = (
    ("telemac_dye_release", "the Wabash River near Lafayette, Indiana"),
    ("telemac_do_sag", "the Wabash River near Lafayette, Indiana"),
    ("telemac3d_stratified_flow", "Lake Mead"),
    ("artemis_harbor_agitation", "Marquette Harbor, Michigan"),
)


def _resolve_bare(tool_name: str, location: str):
    """The door-1 sheet a BARE ``{"location": ...}`` invocation resolves to."""
    from trid3nt_server.tools import TOOL_REGISTRY
    from seated import resolve_params

    workflow = TOOL_REGISTRY[tool_name].fn.workflow
    supplied, err = asyncio.run(_normalized(workflow, {}))
    assert err is None, f"{tool_name} refused a bare location: {err}"
    return workflow, asyncio.run(resolve_params(value_inputs(workflow), supplied))


@pytest.mark.parametrize(("tool_name", "location"), TELEMAC_TEMPLATES)
def test_an_unsupplied_moment_states_none_of_its_own(tool_name: str,
                                                     location: str) -> None:
    """Nobody sent a moment, so the engine's coercion seats none."""
    workflow, sheet = _resolve_bare(tool_name, location)
    assert workflow.input("event_time").is_optional
    assert "event_time" not in sheet, f"{tool_name} invented a moment"


@pytest.mark.parametrize(("tool_name", "location"), TELEMAC_TEMPLATES)
def test_an_unsupplied_moment_s_provenance_row_is_not_user(tool_name: str,
                                                          location: str) -> None:
    """The same abstention on the row the LAYER and the input-review gate read."""
    from trid3nt_server.workflows.runtime import provenance_entries

    workflow, sheet = _resolve_bare(tool_name, location)
    entry = next(e for e in provenance_entries(sheet, value_inputs(workflow))
                 if e.param == "event_time")
    assert entry.basis == "derived"
    assert "most recent cycle" in entry.note


def test_a_supplied_moment_still_reads_as_the_users() -> None:
    """The abstention is about ABSENCE only: a sent moment is still the user's."""
    from trid3nt_server.tools import TOOL_REGISTRY
    from seated import resolve_params

    workflow = TOOL_REGISTRY["telemac_dye_release"].fn.workflow
    supplied, err = asyncio.run(_normalized(workflow, 
        {"event_time": "2026-03-04"}))
    assert err is None, err
    row = asyncio.run(resolve_params(value_inputs(workflow), supplied))["event_time"]
    assert (row.value[:10], row.origin) == ("2026-03-04", "user")


async def _normalized(workflow, args):
    """The accepted params of one fill, and the refusal where one was rejected;
    the rows the fill produces are stood in for, so the params alone answer."""
    from unittest.mock import patch

    from trid3nt_server.workflows.runtime import fill as fill_mod
    from trid3nt_server.workflows.runtime.fill import ACCEPTED, REJECTED, Fill, fill

    async def _held(env, decl):
        return decl.name

    with patch.object(fill_mod, "_produce", _held):
        state = await fill(Fill(workflow=workflow), {"input_mode": "auto", **args})
    declared = {prm.name for prm in value_inputs(workflow)}
    refused = [v.reason for v in state.inputs.values() if v.state == REJECTED]
    return ({n: v.value for n, v in state.inputs.items()
             if v.state == ACCEPTED and n in declared},
            " ".join(refused) or None)
