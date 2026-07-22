"""Round-trip + invariant tests for Case persistence envelopes (FR-MP-6).

Every Case persistence type defined in ``grace2_contracts.case`` is exercised:
- A real instance is built, dumped via ``model_dump(mode="json")``, JSON-text
  round-tripped, parsed back, and re-dumped. Both passes must be byte-identical.
- ULID format validation refuses malformed ids.
- ISO-8601 datetime validation produces ``...Z`` suffixes.
- envelope_type Literal validation refuses wrong discriminator values.
- Invariant 9: no cost field anywhere (self-checked).
- Closed-enum boundaries (``CaseStatus`` / ``CaseCommand``) are enforced.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from grace2_contracts.case import (
    CaseChatMessage,
    CaseCommandEnvelopePayload,
    CaseListEnvelopePayload,
    CaseOpenEnvelopePayload,
    CaseSessionState,
    CaseSummary,
    PersistedSubStepRecord,
    ToolCardRecord,
)
from grace2_contracts.common import new_ulid


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _roundtrip(model: Any) -> dict[str, Any]:
    """Real JSON serialize -> text -> dict -> re-validate -> dump. Idempotent."""
    dumped_a = model.model_dump(mode="json")
    text_a = json.dumps(dumped_a, sort_keys=True)
    loaded = json.loads(text_a)
    rebuilt = type(model).model_validate(loaded)
    dumped_b = rebuilt.model_dump(mode="json")
    text_b = json.dumps(dumped_b, sort_keys=True)
    assert text_a == text_b, "JSON round-trip not idempotent"
    return dumped_a


def _fresh_case_summary() -> CaseSummary:
    return CaseSummary(
        case_id=new_ulid(),
        title="Hurricane Ian — Fort Myers",
        created_at="2026-06-05T12:00:00Z",
        updated_at="2026-06-05T12:30:00Z",
        bbox=(-82.5, 26.4, -81.7, 26.9),
        primary_hazard="flood",
        layer_summary=["run-01HX-flood-depth", "run-01HX-pop"],
        qgs_project_uri="gs://trid3nt/cases/01HX/01HX.qgs",
    )


# --------------------------------------------------------------------------- #
# CaseSummary
# --------------------------------------------------------------------------- #


def test_case_summary_roundtrip() -> None:
    summary = _fresh_case_summary()
    dumped = _roundtrip(summary)
    assert dumped["status"] == "active"  # default
    assert dumped["schema_version"] == "v1"
    assert dumped["created_at"].endswith("Z")
    assert dumped["updated_at"].endswith("Z")
    assert dumped["bbox"] == [-82.5, 26.4, -81.7, 26.9]


def test_case_summary_minimal_required_fields() -> None:
    """All optional fields default cleanly."""
    summary = CaseSummary(
        case_id=new_ulid(),
        title="untitled",
        created_at="2026-06-05T12:00:00Z",
        updated_at="2026-06-05T12:00:00Z",
    )
    dumped = _roundtrip(summary)
    assert dumped["bbox"] is None
    assert dumped["primary_hazard"] is None
    assert dumped["layer_summary"] == []
    assert dumped["qgs_project_uri"] is None
    assert dumped["status"] == "active"


def test_case_summary_rejects_malformed_ulid() -> None:
    with pytest.raises(ValidationError):
        CaseSummary(
            case_id="not-a-ulid",
            title="x",
            created_at="2026-06-05T12:00:00Z",
            updated_at="2026-06-05T12:00:00Z",
        )


def test_case_summary_rejects_invalid_status() -> None:
    """CaseStatus is a closed Literal."""
    with pytest.raises(ValidationError):
        CaseSummary(
            case_id=new_ulid(),
            title="x",
            created_at="2026-06-05T12:00:00Z",
            updated_at="2026-06-05T12:00:00Z",
            status="paused",  # type: ignore[arg-type]
        )


def test_case_summary_rejects_bad_bbox_ordering() -> None:
    """BBox validator inherited from common.py: minLon > maxLon must fail."""
    with pytest.raises(ValidationError):
        CaseSummary(
            case_id=new_ulid(),
            title="x",
            created_at="2026-06-05T12:00:00Z",
            updated_at="2026-06-05T12:00:00Z",
            bbox=(10.0, 10.0, 5.0, 20.0),
        )


def test_case_summary_no_cost_field_invariant_9() -> None:
    """Invariant 9: no cost field anywhere on Case envelopes."""
    summary = _fresh_case_summary()
    dumped = summary.model_dump(mode="json")
    forbidden = {"cost", "estimated_cost", "spend", "spent", "budget", "quota"}
    assert forbidden.isdisjoint(dumped.keys()), f"cost-like field leaked: {dumped.keys() & forbidden}"


def test_case_summary_extra_forbid() -> None:
    """GraceModel extra='forbid' — unknown fields fail validation, not silently dropped."""
    with pytest.raises(ValidationError):
        CaseSummary.model_validate({
            "case_id": new_ulid(),
            "title": "x",
            "created_at": "2026-06-05T12:00:00Z",
            "updated_at": "2026-06-05T12:00:00Z",
            "estimated_cost": 42.0,  # invariant 9 + extra=forbid
        })


# --------------------------------------------------------------------------- #
# CaseChatMessage
# --------------------------------------------------------------------------- #


def test_case_chat_message_roundtrip() -> None:
    msg = CaseChatMessage(
        message_id=new_ulid(),
        case_id=new_ulid(),
        role="agent",
        content="Generating flood depth for Fort Myers...",
        pipeline_id=new_ulid(),
        layer_emissions=["run-01HX-flood-depth"],
        map_command_emissions=[
            {
                "command": "load-layer",
                "args": {
                    "layer_id": "run-01HX-flood-depth",
                    "wms_url": "https://qgis.example.com/wms?MAP=01HX.qgs",
                    "style_preset": "flood_depth_blue",
                },
            },
            {
                "command": "zoom-to",
                "args": {"bbox": [-82.5, 26.4, -81.7, 26.9]},
            },
        ],
        created_at="2026-06-05T12:01:00Z",
    )
    dumped = _roundtrip(msg)
    assert dumped["role"] == "agent"
    assert len(dumped["map_command_emissions"]) == 2
    assert dumped["created_at"].endswith("Z")


def test_case_chat_message_minimal_user_turn() -> None:
    msg = CaseChatMessage(
        message_id=new_ulid(),
        case_id=new_ulid(),
        role="user",
        content="Model the flood",
        created_at="2026-06-05T12:00:00Z",
    )
    dumped = _roundtrip(msg)
    assert dumped["pipeline_id"] is None
    assert dumped["layer_emissions"] == []
    assert dumped["map_command_emissions"] == []


def test_case_chat_message_rejects_invalid_role() -> None:
    with pytest.raises(ValidationError):
        CaseChatMessage(
            message_id=new_ulid(),
            case_id=new_ulid(),
            role="assistant",  # type: ignore[arg-type]
            content="...",
            created_at="2026-06-05T12:00:00Z",
        )


# --------------------------------------------------------------------------- #
# CaseSessionState
# --------------------------------------------------------------------------- #


def test_case_session_state_roundtrip() -> None:
    case = _fresh_case_summary()
    state = CaseSessionState(
        case=case,
        chat_history=[
            CaseChatMessage(
                message_id=new_ulid(),
                case_id=case.case_id,
                role="user",
                content="Run Ian",
                created_at="2026-06-05T12:00:00Z",
            ),
            CaseChatMessage(
                message_id=new_ulid(),
                case_id=case.case_id,
                role="agent",
                content="Running...",
                pipeline_id=new_ulid(),
                created_at="2026-06-05T12:00:30Z",
            ),
        ],
        loaded_layers=[
            {
                "layer_id": "run-01HX-flood-depth",
                "name": "Flood depth",
                "layer_type": "raster",
                "uri": "gs://trid3nt/runs/01HX/depth.cog.tif",
                "style_preset": "flood_depth_blue",
                "visible": True,
                "role": "primary",
                "temporal": False,
            }
        ],
        pipeline_history=[],
        current_pipeline=None,
    )
    dumped = _roundtrip(state)
    assert dumped["case"]["case_id"] == case.case_id
    assert len(dumped["chat_history"]) == 2
    assert dumped["current_pipeline"] is None


def test_case_session_state_requires_case() -> None:
    with pytest.raises(ValidationError):
        CaseSessionState.model_validate({"chat_history": []})


# --------------------------------------------------------------------------- #
# CaseListEnvelopePayload
# --------------------------------------------------------------------------- #


def test_case_list_envelope_roundtrip_with_cases() -> None:
    payload = CaseListEnvelopePayload(
        cases=[_fresh_case_summary(), _fresh_case_summary()]
    )
    dumped = _roundtrip(payload)
    assert dumped["envelope_type"] == "case-list"
    assert len(dumped["cases"]) == 2


def test_case_list_envelope_empty_default() -> None:
    payload = CaseListEnvelopePayload()
    dumped = _roundtrip(payload)
    assert dumped["envelope_type"] == "case-list"
    assert dumped["cases"] == []


def test_case_list_envelope_rejects_wrong_envelope_type() -> None:
    """envelope_type Literal is locked — assigning a wrong value fails."""
    with pytest.raises(ValidationError):
        CaseListEnvelopePayload.model_validate({
            "envelope_type": "case-open",  # wrong discriminator
            "cases": [],
        })


def test_case_list_envelope_message_type_classvar() -> None:
    """The MESSAGE_TYPE ClassVar matches the envelope_type literal (A.1 discipline)."""
    assert CaseListEnvelopePayload.MESSAGE_TYPE == "case-list"


# --------------------------------------------------------------------------- #
# CaseOpenEnvelopePayload
# --------------------------------------------------------------------------- #


def test_case_open_envelope_roundtrip_with_state() -> None:
    case = _fresh_case_summary()
    state = CaseSessionState(case=case)
    payload = CaseOpenEnvelopePayload(session_state=state)
    dumped = _roundtrip(payload)
    assert dumped["envelope_type"] == "case-open"
    assert dumped["session_state"] is not None
    assert dumped["session_state"]["case"]["case_id"] == case.case_id


def test_case_open_envelope_null_session_state() -> None:
    """When the server can't rehydrate (e.g. Case deleted mid-select), state is None."""
    payload = CaseOpenEnvelopePayload()
    dumped = _roundtrip(payload)
    assert dumped["envelope_type"] == "case-open"
    assert dumped["session_state"] is None


def test_case_open_envelope_rejects_wrong_envelope_type() -> None:
    with pytest.raises(ValidationError):
        CaseOpenEnvelopePayload.model_validate({
            "envelope_type": "case-list",
            "session_state": None,
        })


def test_case_open_envelope_message_type_classvar() -> None:
    assert CaseOpenEnvelopePayload.MESSAGE_TYPE == "case-open"


# --------------------------------------------------------------------------- #
# CaseCommandEnvelopePayload
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "command,case_id,args",
    [
        ("create", None, {}),
        ("create", None, {"title": "Hurricane Ian — Fort Myers"}),
        ("select", "fixed", {}),
        ("rename", "fixed", {"title": "Renamed Case"}),
        ("archive", "fixed", {}),
        ("delete", "fixed", {}),
    ],
)
def test_case_command_envelope_roundtrip_every_command(
    command: str, case_id: str | None, args: dict
) -> None:
    cid = new_ulid() if case_id == "fixed" else None
    payload = CaseCommandEnvelopePayload(
        command=command,  # type: ignore[arg-type]
        case_id=cid,
        args=args,
    )
    dumped = _roundtrip(payload)
    assert dumped["envelope_type"] == "case-command"
    assert dumped["command"] == command
    assert dumped["case_id"] == cid
    assert dumped["args"] == args


def test_case_command_envelope_rejects_invalid_command() -> None:
    """CaseCommand is a closed Literal."""
    with pytest.raises(ValidationError):
        CaseCommandEnvelopePayload(
            command="duplicate",  # type: ignore[arg-type]
        )


def test_case_command_envelope_rejects_wrong_envelope_type() -> None:
    with pytest.raises(ValidationError):
        CaseCommandEnvelopePayload.model_validate({
            "envelope_type": "case-list",
            "command": "create",
        })


def test_case_command_envelope_no_cost_field_invariant_9() -> None:
    """Invariant 9: no cost field on the command envelope."""
    payload = CaseCommandEnvelopePayload(command="create")
    dumped = payload.model_dump(mode="json")
    forbidden = {"cost", "estimated_cost", "spend", "spent", "budget", "quota"}
    assert forbidden.isdisjoint(dumped.keys())


def test_case_command_envelope_no_cancellation_field_invariant_8() -> None:
    """Invariant 8: cancellation flows through A.3 cancel, not a case-command field."""
    payload = CaseCommandEnvelopePayload(command="create")
    dumped = payload.model_dump(mode="json")
    forbidden = {"cancel", "cancelled", "cancellation_reason"}
    assert forbidden.isdisjoint(dumped.keys())


def test_case_command_envelope_message_type_classvar() -> None:
    assert CaseCommandEnvelopePayload.MESSAGE_TYPE == "case-command"


# --------------------------------------------------------------------------- #
# Exports
# --------------------------------------------------------------------------- #


def test_module_exports_via_package_namespace() -> None:
    """Idempotent-append re-export from the package __init__ exposes case.*."""
    import grace2_contracts

    assert grace2_contracts.case is not None
    assert grace2_contracts.case.CaseSummary is CaseSummary
    assert grace2_contracts.case.CaseChatMessage is CaseChatMessage
    assert grace2_contracts.case.CaseSessionState is CaseSessionState
    assert grace2_contracts.case.CaseListEnvelopePayload is CaseListEnvelopePayload
    assert grace2_contracts.case.CaseOpenEnvelopePayload is CaseOpenEnvelopePayload
    assert grace2_contracts.case.CaseCommandEnvelopePayload is CaseCommandEnvelopePayload


# --------------------------------------------------------------------------- #
# ToolCardRecord + role="tool" (job-0267 — full-stream persistence)
# --------------------------------------------------------------------------- #


def _fresh_tool_card() -> ToolCardRecord:
    return ToolCardRecord(
        tool_name="fetch_3dep_dem",
        state="complete",
        started_at="2026-06-10T12:00:00Z",
        duration_ms=2340,
        label="fetch_3dep_dem",
    )


def test_tool_card_record_roundtrip() -> None:
    card = _fresh_tool_card()
    dumped = _roundtrip(card)
    assert dumped["tool_name"] == "fetch_3dep_dem"
    assert dumped["state"] == "complete"
    assert dumped["duration_ms"] == 2340
    assert dumped["started_at"].endswith("Z")


def test_tool_card_record_failed_state_roundtrip() -> None:
    card = ToolCardRecord(tool_name="run_solver", state="failed")
    dumped = _roundtrip(card)
    assert dumped["state"] == "failed"
    # Minimal record: timing + label are optional (wire-death fallback path).
    assert dumped["started_at"] is None
    assert dumped["duration_ms"] is None
    assert dumped["label"] is None


def test_tool_card_record_io_fields_default_none() -> None:
    """C1: the 7 persisted IO fields default to None on a minimal record so
    pre-C1 documents (no IO keys) validate + replay unchanged."""
    card = ToolCardRecord(tool_name="fetch_3dep_dem", state="complete")
    assert card.raw_args is None
    assert card.function_response is None
    assert card.is_error is None
    assert card.args_truncated is None
    assert card.response_truncated is None
    assert card.args_bytes is None
    assert card.response_bytes is None
    # A document with NO IO keys at all validates (backward-compatible).
    raw = {"schema_version": "v1", "tool_name": "x", "state": "complete"}
    rehydrated = ToolCardRecord.model_validate(raw)
    assert rehydrated.raw_args is None
    assert rehydrated.function_response is None


def test_tool_card_record_io_fields_roundtrip() -> None:
    """C1: the persisted IO fields ride the TYPED record (the integration path
    W2 reads off ``m.tool_card``) and round-trip with the SAME names as the live
    ``ToolIoPayload`` / web ``ToolCardRecord`` (web/src/contracts.ts:698-704)."""
    card = ToolCardRecord(
        tool_name="fetch_3dep_dem",
        state="complete",
        started_at="2026-06-10T12:00:00Z",
        duration_ms=2340,
        label="fetch_3dep_dem",
        raw_args='{\n  "bbox": [-82.0, 26.0, -81.0, 27.0]\n}',
        function_response='{\n  "status": "ok"\n}',
        is_error=False,
        args_truncated=False,
        response_truncated=True,
        args_bytes=42,
        response_bytes=900000,
    )
    dumped = _roundtrip(card)
    assert dumped["raw_args"].startswith("{")
    assert dumped["function_response"] == '{\n  "status": "ok"\n}'
    assert dumped["is_error"] is False
    assert dumped["args_truncated"] is False
    assert dumped["response_truncated"] is True
    assert dumped["args_bytes"] == 42
    assert dumped["response_bytes"] == 900000


def test_tool_card_record_field_set_matches_ts_contract() -> None:
    """C1: the Python record field set EQUALS the web ``ToolCardRecord``
    (web/src/contracts.ts:689-705) so the producer<->consumer contract holds."""
    expected = {
        "schema_version",
        "tool_name",
        "state",
        "started_at",
        "duration_ms",
        "label",
        # C1 IO fields (same names as ToolIoPayload):
        "raw_args",
        "function_response",
        "is_error",
        "args_truncated",
        "response_truncated",
        "args_bytes",
        "response_bytes",
        # task-168 nested sub-step persistence:
        "children",
    }
    assert set(ToolCardRecord.model_fields) == expected


# --------------------------------------------------------------------------- #
# PersistedSubStepRecord + ToolCardRecord.children (task-168 -- read-only
# nested sub-step persistence)
# --------------------------------------------------------------------------- #


def test_persisted_substep_record_field_set_matches_ts_contract() -> None:
    """task-168: the Python child record field set EQUALS the web
    ``PersistedSubStepRecord`` (web/src/contracts.ts) so the producer<->consumer
    contract holds for the nested-timeline replay."""
    expected = {
        "schema_version",
        "step_id",
        "parent_step_id",
        "name",
        "tool_name",
        "state",
        "duration_ms",
        "error_code",
        "error_message",
        # tool-io fields reused from ToolCardRecord / ToolIoPayload:
        "raw_args",
        "function_response",
        "is_error",
        "args_truncated",
        "response_truncated",
        "args_bytes",
        "response_bytes",
    }
    assert set(PersistedSubStepRecord.model_fields) == expected


def test_persisted_substep_record_roundtrip() -> None:
    """A complete + a failed child round-trip (the failed one keeps its
    error_code / error_message honesty-floor fields)."""
    ok = PersistedSubStepRecord(
        step_id=new_ulid(),
        parent_step_id=new_ulid(),
        name="fetch_topobathy",
        tool_name="fetch_topobathy",
        state="complete",
        duration_ms=1200,
    )
    failed = PersistedSubStepRecord(
        step_id=new_ulid(),
        parent_step_id=ok.parent_step_id,
        name="run_solver",
        tool_name="run_solver",
        state="failed",
        duration_ms=4500,
        error_code="SOLVER_TIMEOUT",
        error_message="solver exceeded wall-clock budget",
    )
    ok_d = _roundtrip(ok)
    failed_d = _roundtrip(failed)
    assert ok_d["tool_name"] == "fetch_topobathy"
    assert ok_d["state"] == "complete"
    assert ok_d["error_code"] is None
    assert failed_d["state"] == "failed"
    assert failed_d["error_code"] == "SOLVER_TIMEOUT"
    assert failed_d["error_message"] == "solver exceeded wall-clock budget"


def test_persisted_substep_record_rejects_unknown_state() -> None:
    """``state`` is the ``ToolCardState`` lifecycle enum (running/complete/
    failed/cancelled). ``pending`` and arbitrary strings are still rejected;
    children only ever carry the two terminal values at runtime, but the wider
    type is a harmless superset (shared with the parent ToolCardRecord)."""
    for bad in ("pending", "ok", "succeeded"):
        with pytest.raises(ValidationError):
            PersistedSubStepRecord(
                step_id=new_ulid(),
                tool_name="x",
                state=bad,  # type: ignore[arg-type]
            )


def test_tool_card_record_children_default_none() -> None:
    """task-168: ``children`` defaults to None on a minimal record so every
    pre-task-168 document (no ``children`` key) validates + replays unchanged."""
    card = ToolCardRecord(tool_name="fetch_3dep_dem", state="complete")
    assert card.children is None
    # A document literally MISSING the children key validates (additive contract)
    raw = {"schema_version": "v1", "tool_name": "x", "state": "complete"}
    rehydrated = ToolCardRecord.model_validate(raw)
    assert rehydrated.children is None


def test_tool_card_record_children_roundtrip_with_failed_child_and_io() -> None:
    """task-168: a parent card carrying ordered children (one OK, one FAILED
    with a tool-io drop-down) round-trips with the children intact + ordered."""
    parent = ToolCardRecord(
        tool_name="run_model_flood_scenario",
        state="complete",
        duration_ms=88000,
        label="Model flood scenario",
        children=[
            PersistedSubStepRecord(
                step_id=new_ulid(),
                parent_step_id=new_ulid(),
                name="fetch_topobathy",
                tool_name="fetch_topobathy",
                state="complete",
                duration_ms=1200,
                raw_args='{\n  "bbox": [-82.0, 26.0, -81.0, 27.0]\n}',
                function_response='{\n  "status": "ok"\n}',
                is_error=False,
            ),
            PersistedSubStepRecord(
                step_id=new_ulid(),
                parent_step_id=new_ulid(),
                name="run_solver",
                tool_name="run_solver",
                state="failed",
                duration_ms=4500,
                error_code="SOLVER_FAILED",
                error_message="docker container exited non-zero",
                is_error=True,
            ),
        ],
    )
    dumped = _roundtrip(parent)
    assert [c["tool_name"] for c in dumped["children"]] == [
        "fetch_topobathy",
        "run_solver",
    ]
    assert dumped["children"][0]["state"] == "complete"
    assert dumped["children"][0]["raw_args"].startswith("{")
    assert dumped["children"][1]["state"] == "failed"
    assert dumped["children"][1]["error_code"] == "SOLVER_FAILED"
    assert dumped["children"][1]["is_error"] is True

    # Re-validating the dumped dict reconstructs typed child records (the warm +
    # cold replay path), preserving order.
    rebuilt = ToolCardRecord.model_validate(dumped)
    assert rebuilt.children is not None
    assert all(isinstance(c, PersistedSubStepRecord) for c in rebuilt.children)
    assert [c.tool_name for c in rebuilt.children] == [
        "fetch_topobathy",
        "run_solver",
    ]


def test_tool_card_record_accepts_lifecycle_states() -> None:
    """Durable-card lifecycle: ``running`` (persisted at mint) + the terminal
    states (complete/failed/cancelled) are all valid persisted states so a SOLVE
    card walks running -> terminal in place across a reconnect/reopen."""
    for good in ("running", "complete", "failed", "cancelled"):
        card = ToolCardRecord(tool_name="sfincs:solve", state=good)  # type: ignore[arg-type]
        assert card.state == good


def test_tool_card_record_rejects_unknown_state() -> None:
    """``pending`` is live-wire-only (never persisted); arbitrary strings reject."""
    for bad in ("pending", "ok", "succeeded"):
        with pytest.raises(ValidationError):
            ToolCardRecord(tool_name="x", state=bad)  # type: ignore[arg-type]


def test_tool_card_record_rejects_negative_duration() -> None:
    with pytest.raises(ValidationError):
        ToolCardRecord(tool_name="x", state="complete", duration_ms=-1)


def test_case_chat_message_tool_role_roundtrip() -> None:
    """role="tool" rows carry the typed card + its JSON twin in content."""
    card = _fresh_tool_card()
    msg = CaseChatMessage(
        message_id=new_ulid(),
        case_id=new_ulid(),
        role="tool",
        content=card.model_dump_json(),
        pipeline_id=new_ulid(),
        tool_card=card,
        created_at="2026-06-10T12:00:02Z",
    )
    dumped = _roundtrip(msg)
    assert dumped["role"] == "tool"
    assert dumped["tool_card"]["tool_name"] == "fetch_3dep_dem"
    assert dumped["tool_card"]["state"] == "complete"
    assert dumped["tool_card"]["duration_ms"] == 2340
    # content is the JSON twin — parseable, same tool_name.
    assert json.loads(dumped["content"])["tool_name"] == "fetch_3dep_dem"


def test_case_chat_message_tool_card_default_none_backcompat() -> None:
    """Pre-job-0267 documents (no ``tool_card`` key at all) validate unchanged."""
    raw = {
        "schema_version": "v1",
        "message_id": new_ulid(),
        "case_id": new_ulid(),
        "role": "agent",
        "content": "Done — flood depth published.",
        "pipeline_id": None,
        "layer_emissions": [],
        "map_command_emissions": [],
        "created_at": "2026-06-05T12:01:00Z",
    }
    msg = CaseChatMessage.model_validate(raw)
    assert msg.tool_card is None


def test_case_chat_message_still_rejects_assistant_role() -> None:
    """The role enum gained "tool", not an open set — "assistant" stays out."""
    with pytest.raises(ValidationError):
        CaseChatMessage(
            message_id=new_ulid(),
            case_id=new_ulid(),
            role="assistant",  # type: ignore[arg-type]
            content="...",
            created_at="2026-06-10T12:00:00Z",
        )


def test_case_session_state_carries_interleaved_stream() -> None:
    """chat_history holds the FULL stream: user -> tool -> agent rows."""
    case = _fresh_case_summary()
    card = _fresh_tool_card()
    user_row = CaseChatMessage(
        message_id=new_ulid(),
        case_id=case.case_id,
        role="user",
        content="Fetch the DEM",
        created_at="2026-06-10T12:00:00Z",
    )
    tool_row = CaseChatMessage(
        message_id=new_ulid(),
        case_id=case.case_id,
        role="tool",
        content=card.model_dump_json(),
        tool_card=card,
        created_at="2026-06-10T12:00:02Z",
    )
    agent_row = CaseChatMessage(
        message_id=new_ulid(),
        case_id=case.case_id,
        role="agent",
        content="I fetched the DEM and added it to the map.",
        created_at="2026-06-10T12:00:05Z",
    )
    state = CaseSessionState(case=case, chat_history=[user_row, tool_row, agent_row])
    dumped = _roundtrip(state)
    assert [m["role"] for m in dumped["chat_history"]] == ["user", "tool", "agent"]


# --------------------------------------------------------------------------- #
# CaseManifest / CaseManifestLayer (#165 data-island thin manifest)
# --------------------------------------------------------------------------- #


def _fresh_manifest_layer() -> "CaseManifestLayer":
    from grace2_contracts.case import CaseManifestLayer

    return CaseManifestLayer(
        layer_id="01HX-flood-depth",
        name="Flood depth",
        layer_type="raster",
        style_preset="flood-depth",
        asset_url="https://cf.example/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png",
    )


def test_case_manifest_layer_roundtrip() -> None:
    from grace2_contracts.case import CaseManifestLayer

    layer = CaseManifestLayer(
        layer_id="01HX-buildings",
        name="Buildings (Fort Myers)",
        layer_type="vector",
        style_preset="buildings",
        asset_url="s3://grace2-runs/case-data/01HXCASE/01HX-buildings.geojson",
        bbox=(-82.0, 26.5, -81.8, 26.7),
        wms_url=None,
    )
    dumped = _roundtrip(layer)
    assert dumped["schema_version"] == "v1"
    assert dumped["layer_type"] == "vector"
    assert dumped["bbox"] == [-82.0, 26.5, -81.8, 26.7]
    assert dumped["wms_url"] is None
    assert dumped["asset_url"].endswith(".geojson")


def test_case_manifest_roundtrip() -> None:
    from grace2_contracts.case import CaseManifest

    manifest = CaseManifest(
        case_id=new_ulid(),
        updated_at="2026-06-21T12:00:00Z",
        title="Hurricane Ian — Fort Myers",
        bbox=(-82.5, 26.4, -81.7, 26.9),
        primary_hazard="flood",
        layers=[_fresh_manifest_layer()],
    )
    dumped = _roundtrip(manifest)
    assert dumped["schema_version"] == "v1"
    assert dumped["updated_at"].endswith("Z")
    assert dumped["bbox"] == [-82.5, 26.4, -81.7, 26.9]
    assert dumped["primary_hazard"] == "flood"
    assert len(dumped["layers"]) == 1
    assert dumped["layers"][0]["layer_id"] == "01HX-flood-depth"


def test_case_manifest_minimal_defaults() -> None:
    from grace2_contracts.case import CaseManifest

    manifest = CaseManifest(
        case_id=new_ulid(),
        updated_at="2026-06-21T12:00:00Z",
        title="untitled",
    )
    dumped = _roundtrip(manifest)
    assert dumped["bbox"] is None
    assert dumped["primary_hazard"] is None
    assert dumped["layers"] == []


def test_case_manifest_no_cost_field() -> None:
    """Invariant 9: no cost/spend/quota field anywhere on the manifest."""
    from grace2_contracts.case import CaseManifest, CaseManifestLayer

    text = json.dumps(
        CaseManifest(
            case_id=new_ulid(),
            updated_at="2026-06-21T12:00:00Z",
            title="x",
            layers=[_fresh_manifest_layer()],
        ).model_dump(mode="json")
    ).lower()
    for forbidden in ("cost", "spend", "quota", "price", "dollar"):
        assert forbidden not in text


def test_case_manifest_layer_rejects_extra_field() -> None:
    """GraceModel extra='forbid' — a stray storage key never leaks in."""
    from grace2_contracts.case import CaseManifestLayer

    with pytest.raises(ValidationError):
        CaseManifestLayer(
            layer_id="x",
            name="x",
            layer_type="raster",
            style_preset="x",
            asset_url="https://x/y",
            user_id="leak",  # type: ignore[call-arg]
        )


def test_case_manifest_layer_rejects_bad_layer_type() -> None:
    from grace2_contracts.case import CaseManifestLayer

    with pytest.raises(ValidationError):
        CaseManifestLayer(
            layer_id="x",
            name="x",
            layer_type="point-cloud",  # type: ignore[arg-type]
            style_preset="x",
            asset_url="https://x/y",
        )
