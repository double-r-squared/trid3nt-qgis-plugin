"""The session processing pair and the code approval card.

A registered tool that runs in the user's QGIS session is a request on the plugin
wire: the agent emits ``processing-request``, the plugin runs it in the session
and answers ``processing-response``. A code request is preceded by the
``code-exec-request`` card; running code is a consequential action, and the
approval rides back on the payload-confirmation keyed by ``code_exec_id``.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import Field, model_validator

from .common import GraceModel, ULIDStr

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

#: What the session is asked to run: a Processing algorithm over named canvas
#: layers, or a Python snippet in the session's interpreter.
ProcessingKind = Literal["algorithm", "code"]


class CodeExecRequestPayload(GraceModel):
    """``code-exec-request``, emitted BEFORE a code request reaches the session.
    Approval returns on a payload-confirmation whose ``warning_id`` equals
    ``code_exec_id``; anything but ``proceed`` fails closed."""

    MESSAGE_TYPE: ClassVar[str] = "code-exec-request"

    envelope_type: Literal["code-exec-request"] = "code-exec-request"
    #: The confirmation correlation key.
    code_exec_id: ULIDStr
    #: The EXACT code to be run, verbatim - never a paraphrase, because this is
    #: what the user is approving.
    python_code: str = Field(min_length=1, max_length=_CODE_CAP)
    #: One-line reason the code is being run. Capped to keep it a caption.
    rationale: str | None = Field(default=None, max_length=512)


class ProcessingRequestPayload(GraceModel):
    """``processing-request``: one thing for the session to run, agent -> client.
    An ``algorithm`` request names a Processing algorithm id and its parameters
    (canvas layers by name); a ``code`` request carries the approved snippet."""

    MESSAGE_TYPE: ClassVar[str] = "processing-request"

    request_id: ULIDStr
    kind: ProcessingKind
    #: The Processing algorithm id, ``provider:name``.
    algorithm: str | None = Field(default=None, min_length=1, max_length=200)
    #: The algorithm's parameters, as the Processing framework takes them; a
    #: layer is named by its canvas name.
    params: dict[str, Any] = Field(default_factory=dict)
    #: The snippet, verbatim as approved.
    code: str | None = Field(default=None, min_length=1, max_length=_CODE_CAP)
    #: The approval card a code request rode in on, so the session joins the
    #: outcome to that card.
    code_exec_id: ULIDStr | None = None

    @model_validator(mode="after")
    def _kind_carries_its_body(self) -> "ProcessingRequestPayload":
        if self.kind == "algorithm" and not self.algorithm:
            raise ValueError("an algorithm request names its algorithm")
        if self.kind == "code" and not self.code:
            raise ValueError("a code request carries its code")
        return self


class ProcessingResponsePayload(GraceModel):
    """``processing-response``: what the session produced, client -> agent.
    ``status`` is the honest terminal outcome; ``result`` is the layer summary
    of an algorithm's output or the JSON-coerced ``result`` of a snippet."""

    MESSAGE_TYPE: ClassVar[str] = "processing-response"

    request_id: ULIDStr
    status: Literal["ok", "error"]
    result: dict[str, Any] | None = None
    #: The error text of a failed run, the traceback foot included.
    error: str | None = Field(default=None, max_length=16 * 1024)
    #: What the snippet printed, tail-bounded.
    stdout: str = Field(default="", max_length=16 * 1024)


PROCESSING_AGENT_TO_CLIENT_PAYLOADS: dict[str, type[GraceModel]] = {
    CodeExecRequestPayload.MESSAGE_TYPE: CodeExecRequestPayload,
    ProcessingRequestPayload.MESSAGE_TYPE: ProcessingRequestPayload,
}

PROCESSING_CLIENT_TO_AGENT_PAYLOADS: dict[str, type[GraceModel]] = {
    ProcessingResponsePayload.MESSAGE_TYPE: ProcessingResponsePayload,
}
