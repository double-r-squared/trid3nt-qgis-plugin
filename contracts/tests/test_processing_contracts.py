"""The session processing pair and the code approval card round-trip, refuse a
body that does not match its kind, and sit on the right side of the wire."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from trid3nt_contracts import ws
from trid3nt_contracts.common import new_ulid
from trid3nt_contracts.processing_contracts import (
    PROCESSING_AGENT_TO_CLIENT_PAYLOADS,
    PROCESSING_CLIENT_TO_AGENT_PAYLOADS,
    CodeExecRequestPayload,
    ProcessingRequestPayload,
    ProcessingResponsePayload,
)


def test_code_exec_request_round_trips_verbatim() -> None:
    p = CodeExecRequestPayload(
        code_exec_id=new_ulid(), python_code="result = 1\n", rationale="one"
    )
    back = CodeExecRequestPayload.model_validate(json.loads(p.model_dump_json()))
    assert back == p
    assert back.python_code == "result = 1\n"
    assert back.envelope_type == "code-exec-request"


def test_code_exec_request_refuses_empty_code() -> None:
    with pytest.raises(ValidationError):
        CodeExecRequestPayload(code_exec_id=new_ulid(), python_code="")


def test_algorithm_request_names_its_algorithm() -> None:
    p = ProcessingRequestPayload(
        request_id=new_ulid(), kind="algorithm", algorithm="native:slope",
        params={"INPUT": "DEM"},
    )
    back = ProcessingRequestPayload.model_validate(json.loads(p.model_dump_json()))
    assert back == p
    with pytest.raises(ValidationError):
        ProcessingRequestPayload(request_id=new_ulid(), kind="algorithm")


def test_code_request_carries_its_code() -> None:
    p = ProcessingRequestPayload(request_id=new_ulid(), kind="code", code="result = 2")
    assert p.algorithm is None and p.params == {}
    with pytest.raises(ValidationError):
        ProcessingRequestPayload(request_id=new_ulid(), kind="code")


def test_response_round_trips_and_status_is_closed() -> None:
    p = ProcessingResponsePayload(
        request_id=new_ulid(), status="ok", result={"layer_name": "Slope"}
    )
    back = ProcessingResponsePayload.model_validate(json.loads(p.model_dump_json()))
    assert back == p
    with pytest.raises(ValidationError):
        ProcessingResponsePayload(request_id=new_ulid(), status="blocked")  # type: ignore[arg-type]


def test_wire_sides() -> None:
    assert set(PROCESSING_AGENT_TO_CLIENT_PAYLOADS) == {
        "code-exec-request", "processing-request"}
    assert set(PROCESSING_CLIENT_TO_AGENT_PAYLOADS) == {"processing-response"}
    assert ws.AGENT_TO_CLIENT_PAYLOADS["processing-request"] is ProcessingRequestPayload
    assert ws.CLIENT_TO_AGENT_PAYLOADS["processing-response"] is ProcessingResponsePayload
    assert "code-exec-result" not in ws.ALL_PAYLOADS
    for model in (CodeExecRequestPayload, ProcessingRequestPayload, ProcessingResponsePayload):
        assert model.model_json_schema()
