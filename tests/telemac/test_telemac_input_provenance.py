"""Offline guard: a TELEMAC coercion never falsifies a value's provenance.

A coercion's return merges into the supplied sheet, so anything it emits for an
argument nobody sent resolves through the USER door and is stamped as a user
choice. The rule pinned here is ABSENT in, nothing out. The resolution spine runs
offline - no solver and no network."""

from __future__ import annotations

import asyncio

import pytest

#: Every REGISTERED TELEMAC template that declares ``cores``, and the bare
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
    from trid3nt_server.workflows.runtime.resolver import resolve_params

    workflow = TOOL_REGISTRY[tool_name].fn.workflow
    supplied, err = asyncio.run(workflow._normalize({"location": location}))
    assert err is None, f"{tool_name} refused a bare location: {err}"
    return workflow, asyncio.run(resolve_params(workflow.params, supplied))


@pytest.mark.parametrize(("tool_name", "location"), TELEMAC_TEMPLATES)
def test_unsupplied_cores_states_no_count_of_its_own(tool_name: str,
                                                     location: str) -> None:
    """Nobody sent a count, so the row seats none: the module's own processors
    keyword is the only default there is."""
    _, sheet = _resolve_bare(tool_name, location)
    row = sheet.row("cores")
    assert row is not None, f"{tool_name} declares no cores"
    assert row.value is None, f"{tool_name} invented a core count"
    assert "supplied on this invocation" not in row.note


@pytest.mark.parametrize(("tool_name", "location"), TELEMAC_TEMPLATES)
def test_unsupplied_cores_provenance_row_is_not_user(tool_name: str,
                                                     location: str) -> None:
    """The same abstention on the row the LAYER and the input-review gate read."""
    from trid3nt_server.workflows.runtime import provenance_entries

    workflow, sheet = _resolve_bare(tool_name, location)
    entry = next(e for e in provenance_entries(sheet, workflow.params)
                 if e.param == "cores")
    assert entry.basis == "derived"
    assert "dictionary" in entry.note


def test_a_supplied_count_still_reads_as_the_users() -> None:
    """The abstention is about ABSENCE only: a sent count is still door=user."""
    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.workflows.runtime.resolver import resolve_params

    workflow = TOOL_REGISTRY["telemac_dye_release"].fn.workflow
    supplied, err = asyncio.run(workflow._normalize(
        {"location": "the Wabash River", "cores": 2}))
    assert err is None, err
    row = asyncio.run(resolve_params(workflow.params, supplied)).row("cores")
    assert (row.value, row.door, row.basis) == (2, "user", "user")
