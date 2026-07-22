"""Validation + round-trip tests for the python-sandbox code-exec contracts
(sprint-13 Stage 2, conversational data-analysis layer, job-0233).

Covers:
- ``CodeExecRequestPayload`` round-trip + bounds (python_code min/max length,
  rationale cap, default empty layer_refs).
- ``CodeExecResultPayload`` round-trip + the status enum + the field caps +
  ``duration_s >= 0``.
- both envelopes are wired into the ws.py agent->client routing registry, and
  NO new client->agent shape is added (the confirm reply rides
  tool-payload-confirmation).
- no cost field anywhere (Invariant 9).
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from grace2_contracts import (
    CodeExecRequestPayload,
    CodeExecResultPayload,
)
from grace2_contracts.common import new_ulid
from grace2_contracts.sandbox_contracts import SANDBOX_AGENT_TO_CLIENT_PAYLOADS
from grace2_contracts import ws


def test_request_payload_round_trip() -> None:
    p = CodeExecRequestPayload(
        code_exec_id=new_ulid(),
        python_code="result = dem.read(1).mean()",
        layer_refs={"dem": "gs://bucket/dem.tif"},
        rationale="mean elevation",
    )
    back = CodeExecRequestPayload.model_validate(json.loads(p.model_dump_json()))
    assert back == p
    assert back.envelope_type == "code-exec-request"


def test_request_payload_defaults() -> None:
    p = CodeExecRequestPayload(code_exec_id=new_ulid(), python_code="result = 1")
    assert p.layer_refs == {}
    assert p.rationale is None


def test_request_payload_accepts_list_valued_layer_refs() -> None:
    """sandbox-staging: a layer_refs value may be an ORDERED LIST of frame URIs
    (the multi-frame extension) alongside the legacy single-string form. Both
    shapes coexist in one mapping and round-trip byte-identically."""
    p = CodeExecRequestPayload(
        code_exec_id=new_ulid(),
        python_code="result = len(frames)",
        layer_refs={
            "peak": "s3://b/runs/peak.tif",  # single (legacy)
            "frames": ["s3://b/runs/f0.tif", "s3://b/runs/f1.tif"],  # list (new)
        },
    )
    assert p.layer_refs["peak"] == "s3://b/runs/peak.tif"
    assert p.layer_refs["frames"] == ["s3://b/runs/f0.tif", "s3://b/runs/f1.tif"]
    # JSON round-trip preserves both shapes exactly.
    back = CodeExecRequestPayload.model_validate(json.loads(p.model_dump_json()))
    assert back == p
    assert isinstance(back.layer_refs["frames"], list)
    assert isinstance(back.layer_refs["peak"], str)


def test_request_payload_single_string_layer_refs_byte_identical() -> None:
    """The legacy single-URI string form is UNCHANGED (regression guard)."""
    p = CodeExecRequestPayload(
        code_exec_id=new_ulid(),
        python_code="result = dem.read(1).mean()",
        layer_refs={"dem": "s3://bucket/dem.tif"},
    )
    assert p.layer_refs == {"dem": "s3://bucket/dem.tif"}
    back = CodeExecRequestPayload.model_validate(json.loads(p.model_dump_json()))
    assert back.layer_refs == {"dem": "s3://bucket/dem.tif"}


def test_request_payload_rejects_empty_code() -> None:
    with pytest.raises(ValidationError):
        CodeExecRequestPayload(code_exec_id=new_ulid(), python_code="")


def test_request_payload_rejects_oversized_code() -> None:
    with pytest.raises(ValidationError):
        CodeExecRequestPayload(
            code_exec_id=new_ulid(), python_code="x" * (64 * 1024 + 1)
        )


def test_request_payload_rejects_long_rationale() -> None:
    with pytest.raises(ValidationError):
        CodeExecRequestPayload(
            code_exec_id=new_ulid(), python_code="result=1", rationale="x" * 513
        )


def test_result_payload_round_trip() -> None:
    p = CodeExecResultPayload(
        code_exec_id=new_ulid(),
        status="ok",
        stdout_tail="done\n",
        stderr_tail="",
        result={"kind": "json", "value": 42},
        truncated=False,
        duration_s=0.5,
    )
    back = CodeExecResultPayload.model_validate(json.loads(p.model_dump_json()))
    assert back == p
    assert back.envelope_type == "code-exec-result"


@pytest.mark.parametrize("status", ["ok", "error", "timeout", "blocked"])
def test_result_payload_accepts_each_status(status: str) -> None:
    p = CodeExecResultPayload(code_exec_id=new_ulid(), status=status)  # type: ignore[arg-type]
    assert p.status == status


def test_result_payload_rejects_bad_status() -> None:
    with pytest.raises(ValidationError):
        CodeExecResultPayload(code_exec_id=new_ulid(), status="weird")  # type: ignore[arg-type]


def test_result_payload_rejects_negative_duration() -> None:
    with pytest.raises(ValidationError):
        CodeExecResultPayload(
            code_exec_id=new_ulid(), status="ok", duration_s=-1.0
        )


def test_both_envelopes_wired_into_ws_routing() -> None:
    assert "code-exec-request" in SANDBOX_AGENT_TO_CLIENT_PAYLOADS
    assert "code-exec-result" in SANDBOX_AGENT_TO_CLIENT_PAYLOADS
    # ws.py splatted them into the agent->client + ALL routing dicts.
    assert ws.AGENT_TO_CLIENT_PAYLOADS["code-exec-request"] is CodeExecRequestPayload
    assert ws.AGENT_TO_CLIENT_PAYLOADS["code-exec-result"] is CodeExecResultPayload
    assert "code-exec-request" in ws.ALL_PAYLOADS
    assert "code-exec-result" in ws.ALL_PAYLOADS
    # No new client->agent shape — the confirm reply rides tool-payload-confirmation.
    assert "code-exec-request" not in ws.CLIENT_TO_AGENT_PAYLOADS
    assert "code-exec-result" not in ws.CLIENT_TO_AGENT_PAYLOADS


def test_no_cost_field_anywhere() -> None:
    for model in (CodeExecRequestPayload, CodeExecResultPayload):
        for field in model.model_fields:
            low = field.lower()
            assert "cost" not in low
            assert "price" not in low
            assert "dollar" not in low
