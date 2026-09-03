"""Offline guard: a TELEMAC coercion never falsifies a value's provenance.

A coercion's return value merges into the DOOR-1 supplied sheet, so anything it
emits for an argument nobody sent resolves through the USER door and the run's
provenance stamps it ``basis=user`` / "supplied on this invocation". A coercion
that unconditionally returns its own fall-through therefore reports the
template's own labeled default as a user choice on every single run.

The rule these tests pin: ABSENT in, nothing out. No solver, no network - the
resolution spine (``_normalize`` -> ``resolve_params`` -> ``provenance_entries``)
runs offline.
"""

from __future__ import annotations

import asyncio

import pytest

#: Every REGISTERED TELEMAC template that declares ``compute_class``, and the bare
#: natural prompt each one is asked. The parked declarations are absent: a tool
#: off the model surface has no invocation for a provenance row to describe.
TELEMAC_TEMPLATES: tuple[tuple[str, str], ...] = (
    ("telemac_river_dye", "the Wabash River near Lafayette, Indiana"),
    ("telemac_do_sag", "the Wabash River near Lafayette, Indiana"),
    ("telemac3d_stratified_flow", "Lake Mead"),
    ("artemis_harbor_agitation", "Marquette Harbor, Michigan"),
)


def _resolve_bare(tool_name: str, location: str):
    """The door-1 sheet a BARE ``{"location": ...}`` invocation resolves to."""
    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.workflows.lib.resolver import resolve_params

    workflow = TOOL_REGISTRY[tool_name].fn.workflow
    supplied, err = workflow._normalize({"location": location})
    assert err is None, f"{tool_name} refused a bare location: {err}"
    return workflow, asyncio.run(resolve_params(workflow.params, supplied))


@pytest.mark.parametrize(("tool_name", "location"), TELEMAC_TEMPLATES)
def test_unsupplied_compute_class_is_a_labeled_default(tool_name: str,
                                                       location: str) -> None:
    """Nobody sent a rung, so the row says CONSTANT door / labeled default."""
    _, sheet = _resolve_bare(tool_name, location)
    row = sheet.row("compute_class")
    assert row is not None, f"{tool_name} declares no compute_class"
    assert (row.door, row.basis) == ("constant", "default_demo"), (
        f"{tool_name} stamps an unsupplied compute_class as "
        f"door={row.door} basis={row.basis}")
    assert "supplied on this invocation" not in row.note


@pytest.mark.parametrize(("tool_name", "location"), TELEMAC_TEMPLATES)
def test_unsupplied_compute_class_provenance_row_is_not_user(tool_name: str,
                                                             location: str) -> None:
    """The same abstention on the row the LAYER and the input-review gate read."""
    from trid3nt_server.workflows.lib import provenance_entries

    workflow, sheet = _resolve_bare(tool_name, location)
    entry = next(e for e in provenance_entries(sheet, workflow.params)
                 if e.param == "compute_class")
    assert entry.basis == "default_demo"
    assert entry.value == "medium"


def test_a_supplied_compute_class_still_reads_as_the_users() -> None:
    """The abstention is about ABSENCE only: a sent rung is still door=user."""
    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.workflows.lib.resolver import resolve_params

    workflow = TOOL_REGISTRY["telemac_river_dye"].fn.workflow
    supplied, err = workflow._normalize(
        {"location": "the Wabash River", "compute_class": "LARGE"})
    assert err is None, err
    row = asyncio.run(resolve_params(workflow.params, supplied)).row("compute_class")
    assert (row.value, row.door, row.basis) == ("large", "user", "user")


def test_an_unknown_rung_refuses_rather_than_substituting() -> None:
    """A rung the dispatcher cannot serve is a REFUSAL, not a quiet 'medium'.

    It used to log a warning and seat 'medium', so a caller who asked for
    'xlarge' got a medium solve with no provenance row saying so and nothing on
    any surface a reader looks at.
    """
    from trid3nt_server.workflows.telemac.helpers.errors import TelemacDyeScenarioError
    from trid3nt_server.workflows.telemac.solving.solve import compute_class

    coerce = compute_class()
    with pytest.raises(TelemacDyeScenarioError) as excinfo:
        coerce({"compute_class": "enormous"})
    assert excinfo.value.error_code == "TELEMAC_COMPUTE_CLASS_UNKNOWN"
    assert "enormous" in str(excinfo.value)
    assert coerce({"compute_class": "  Small "}) == {"compute_class": "small"}


@pytest.mark.parametrize("args", [{}, {"compute_class": None},
                                  {"compute_class": ""}, {"compute_class": "   "}])
def test_compute_class_abstains_when_absent(args: dict) -> None:
    """Absent, null, and blank are the same non-answer: emit nothing."""
    from trid3nt_server.workflows.telemac.solving.solve import compute_class

    assert compute_class()(args) == {}


def test_question_class_coercions_abstain_without_a_signal() -> None:
    """The mode coercions read the ask; with nothing to read they emit nothing.

    Each declares the same fall-through class as its template's declared default,
    so abstaining changes the row's PROVENANCE, never its value.
    """
    from trid3nt_server.workflows.telemac.agitation.agitation_mode import agitation_mode
    from trid3nt_server.workflows.telemac.helpers.substance import substance_class
    from trid3nt_server.workflows.telemac.stratified_flow.flow_mode import flow_mode

    bare = {"location": "Lake Michigan"}
    assert flow_mode()(bare) == {}
    assert agitation_mode()(bare) == {}
    assert substance_class()(bare) == {}


@pytest.mark.parametrize(("tool_name", "location", "param", "expected"), [
    ("telemac3d_stratified_flow", "Lake Mead", "flow_mode", "stratification"),
    ("artemis_harbor_agitation", "Marquette Harbor", "wave_mode", "diffraction"),
    ("telemac_river_dye", "the Wabash River", "substance", "dye"),
])
def test_abstention_keeps_the_value_and_corrects_the_basis(
        tool_name: str, location: str, param: str, expected: str) -> None:
    """Same resolved class as before the fix; a QUESTION door and a labeled basis."""
    _, sheet = _resolve_bare(tool_name, location)
    row = sheet.row(param)
    assert row.value == expected
    assert (row.door, row.basis) == ("question", "default_demo")
