"""Tests for ``grace2_contracts.errors.ToolInputError`` (job-0114-schema).

Verifies:
- All three closed-enum codes round-trip cleanly.
- ``retryable`` is pinned to ``False`` (the type-system literal).
- Empty / missing message is rejected at construction.
- Unknown code is rejected.
- Extra fields are rejected (``extra='forbid'`` inheritance from GraceModel).
- JSON round-trip is idempotent.
- The convenience re-export from ``grace2_contracts.tool_registry`` returns
  the same class as the authoritative module.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from grace2_contracts import errors as errors_mod
from grace2_contracts.errors import (
    TOOL_INPUT_ERROR_CODES,
    ToolInputError,
)


# --- All three codes round-trip --- #


@pytest.mark.parametrize(
    "code",
    ["BBOX_REQUIRED", "INVALID_ARG", "BAD_FORMAT"],
)
def test_tool_input_error_accepts_all_three_codes(code: str) -> None:
    """Each member of the closed enum is a legal code."""
    err = ToolInputError(code=code, message=f"problem: {code}")  # type: ignore[arg-type]
    assert err.code == code
    assert err.message == f"problem: {code}"
    assert err.retryable is False


def test_tool_input_error_codes_tuple_matches_literal() -> None:
    """The tuple form covers the same three codes as the Literal."""
    assert TOOL_INPUT_ERROR_CODES == ("BBOX_REQUIRED", "INVALID_ARG", "BAD_FORMAT")


# --- retryable pinned False --- #


def test_tool_input_error_retryable_defaults_false() -> None:
    """Default retryable is False (input errors are never retryable)."""
    err = ToolInputError(code="BBOX_REQUIRED", message="bbox is required")
    assert err.retryable is False


def test_tool_input_error_rejects_retryable_true() -> None:
    """Type system pins retryable to Literal[False]; True is a validation error."""
    with pytest.raises(ValidationError):
        ToolInputError.model_validate(
            {
                "code": "BBOX_REQUIRED",
                "message": "bbox is required",
                "retryable": True,  # not Literal[False]
            }
        )


# --- Message + code validation --- #


def test_tool_input_error_rejects_empty_message() -> None:
    """The message must be non-empty (min_length=1)."""
    with pytest.raises(ValidationError):
        ToolInputError(code="BBOX_REQUIRED", message="")


def test_tool_input_error_rejects_unknown_code() -> None:
    """The code is a closed Literal — unknown values are rejected."""
    with pytest.raises(ValidationError):
        ToolInputError.model_validate(
            {
                "code": "NOT_A_REAL_CODE",
                "message": "something went wrong",
            }
        )


# --- extra=forbid inheritance --- #


def test_tool_input_error_forbids_extra_fields() -> None:
    """GraceModel sets ``extra='forbid'``; unknown fields are rejected."""
    with pytest.raises(ValidationError):
        ToolInputError.model_validate(
            {
                "code": "BBOX_REQUIRED",
                "message": "bbox is required",
                "retry_after_seconds": 30,  # not a real field
            }
        )


# --- Round-trip --- #


def test_tool_input_error_json_roundtrip_idempotent() -> None:
    """Round-trip through real JSON serialize/deserialize is idempotent."""
    err = ToolInputError(
        code="INVALID_ARG",
        message="max_records must be in [1, 100000]; got -3",
    )
    dumped_a = err.model_dump(mode="json")
    text_a = json.dumps(dumped_a, sort_keys=True)
    err_b = ToolInputError.model_validate(json.loads(text_a))
    dumped_b = err_b.model_dump(mode="json")
    text_b = json.dumps(dumped_b, sort_keys=True)
    assert text_a == text_b
    assert err_b.code == "INVALID_ARG"
    assert err_b.message == "max_records must be in [1, 100000]; got -3"
    assert err_b.retryable is False


def test_tool_input_error_wire_form_contains_all_three_fields() -> None:
    """The wire form (``model_dump(mode='json')``) is a 3-key dict."""
    err = ToolInputError(code="BAD_FORMAT", message="polygon self-intersects")
    dumped = err.model_dump(mode="json")
    assert set(dumped.keys()) == {"code", "message", "retryable"}
    assert dumped["code"] == "BAD_FORMAT"
    assert dumped["message"] == "polygon self-intersects"
    assert dumped["retryable"] is False


# --- Convenience re-export --- #


def test_tool_input_error_reexport_from_tool_registry_is_same_class() -> None:
    """``grace2_contracts.tool_registry`` re-exports the same class object.

    Tool authors who already import from ``tool_registry`` can pick up
    ``ToolInputError`` without a second import line, but both paths must
    point at the authoritative class.
    """
    from grace2_contracts.tool_registry import (
        TOOL_INPUT_ERROR_CODES as reexport_codes,
    )
    from grace2_contracts.tool_registry import (
        ToolInputError as reexport_cls,
    )

    assert reexport_cls is ToolInputError
    assert reexport_cls is errors_mod.ToolInputError
    assert reexport_codes == TOOL_INPUT_ERROR_CODES
