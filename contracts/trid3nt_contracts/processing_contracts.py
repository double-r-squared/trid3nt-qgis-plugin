"""The session processing pair and the code approval card.

A registered tool that runs in the user's QGIS session is a ``processing-request`` the plugin runs and answers with
``processing-response``. A code request is preceded by the ``code-exec-request`` card, and the approval rides back on
the payload-confirmation keyed by ``code_exec_id``.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import Field, model_validator

from .common import ContractModel, ULIDStr

__all__ = [
    "CodeExecRequestPayload",
    "ProcessingKind",
    "ProcessingRequestPayload",
    "ProcessingResponsePayload",
    "PROCESSING_AGENT_TO_CLIENT_PAYLOADS",
    "PROCESSING_CLIENT_TO_AGENT_PAYLOADS",
]

#: A snippet is small; a megabyte of "code" is refused at the boundary.
_CODE_CAP = 64 * 1024

#: What the session runs: a Processing algorithm over named canvas layers, or a Python snippet.
ProcessingKind = Literal["algorithm", "code"]


class CodeExecRequestPayload(ContractModel):
    """``code-exec-request``, emitted BEFORE a code request reaches the session.
    Approval returns on a payload-confirmation whose ``warning_id`` equals
    ``code_exec_id``; anything but ``proceed`` fails closed."""

    MESSAGE_TYPE: ClassVar[str] = "code-exec-request"

    envelope_type: Literal["code-exec-request"] = "code-exec-request"
    code_exec_id: ULIDStr
    #: The exact code to be run, verbatim - never a paraphrase, because this is what the user approves.
    python_code: str = Field(min_length=1, max_length=_CODE_CAP)
    rationale: str | None = Field(default=None, max_length=512)


class ProcessingRequestPayload(ContractModel):
    """``processing-request``: one thing for the session to run, agent -> client.
    An ``algorithm`` request names a Processing algorithm id and its parameters
    (canvas layers by name); a ``code`` request carries the approved snippet."""

    MESSAGE_TYPE: ClassVar[str] = "processing-request"

    request_id: ULIDStr
    kind: ProcessingKind
    algorithm: str | None = Field(default=None, min_length=1, max_length=200)
    #: Processing parameters; a layer is named by its canvas name.
    params: dict[str, Any] = Field(default_factory=dict)
    code: str | None = Field(default=None, min_length=1, max_length=_CODE_CAP)
    #: The approval card a code request rode in on, so the session joins the outcome to it.
    code_exec_id: ULIDStr | None = None

    @model_validator(mode="after")
    def _kind_carries_its_body(self) -> "ProcessingRequestPayload":
        if self.kind == "algorithm" and not self.algorithm:
            raise ValueError("an algorithm request names its algorithm")
        if self.kind == "code" and not self.code:
            raise ValueError("a code request carries its code")
        return self


class ProcessingResponsePayload(ContractModel):
    """``processing-response``: what the session produced, client -> agent.
    ``status`` is the honest terminal outcome; ``result`` is the layer summary
    of an algorithm's output or the JSON-coerced ``result`` of a snippet."""

    MESSAGE_TYPE: ClassVar[str] = "processing-response"

    request_id: ULIDStr
    status: Literal["ok", "error"]
    result: dict[str, Any] | None = None
    error: str | None = Field(default=None, max_length=16 * 1024)
    stdout: str = Field(default="", max_length=16 * 1024)


PROCESSING_AGENT_TO_CLIENT_PAYLOADS: dict[str, type[ContractModel]] = {
    CodeExecRequestPayload.MESSAGE_TYPE: CodeExecRequestPayload,
    ProcessingRequestPayload.MESSAGE_TYPE: ProcessingRequestPayload,
}

PROCESSING_CLIENT_TO_AGENT_PAYLOADS: dict[str, type[ContractModel]] = {
    ProcessingResponsePayload.MESSAGE_TYPE: ProcessingResponsePayload,
}
