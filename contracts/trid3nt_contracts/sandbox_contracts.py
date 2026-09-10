"""The two code-exec envelopes: the confirm request and the run result.

Both are agent -> client. Running arbitrary code is a consequential action, so
the request is a HARD confirm gate; the decision rides back on the EXISTING
payload-confirmation message, keyed by ``code_exec_id`` as its ``warning_id``,
rather than minting a fourth request/response pair.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import Field

from .common import GraceModel, ULIDStr

__all__ = [
    "CodeExecStatus",
    "CodeExecRequestPayload",
    "CodeExecResultPayload",
    "SANDBOX_AGENT_TO_CLIENT_PAYLOADS",
]


#: Terminal status of a sandbox run, mirroring the executor's own.
#:
#: - ``ok``      - the code ran to completion and produced a result.
#: - ``error``   - the code raised; the stderr tail carries the traceback.
#: - ``timeout`` - the code exceeded the wallclock cap.
#: - ``blocked`` - the net guard blocked a non-allowlisted egress.
CodeExecStatus = Literal["ok", "error", "timeout", "blocked"]


class CodeExecRequestPayload(GraceModel):
    """``code-exec-request``, emitted BEFORE the sandbox runs.
    The client renders a confirm card and the user approves or denies on a
    payload-confirmation whose ``warning_id`` equals ``code_exec_id``. No cost
    or quota field: the consequential act is running code, not a billed run.
    """

    MESSAGE_TYPE: ClassVar[str] = "code-exec-request"

    envelope_type: Literal["code-exec-request"] = "code-exec-request"
    #: Doubles as the confirmation correlation key and the join key to the
    #: matching ``code-exec-result``.
    code_exec_id: ULIDStr
    #: The EXACT code to be run, verbatim - never a paraphrase, because this is
    #: what the user is approving. Capped at 64 KiB: a snippet is small, and a
    #: megabyte of "code" is refused at the boundary rather than run.
    python_code: str = Field(min_length=1, max_length=64 * 1024)
    #: ``{var: uri}`` OR ``{var: [uri, ...]}`` - the layers the code may touch,
    #: shown to the user for that reason. A string pre-opens ONE handle; a list
    #: pre-opens an ORDERED list of frame handles, so a snippet can iterate an
    #: animation. Every URI is fetched to a local path and the refs rewritten
    #: before the network-denied executor opens them.
    layer_refs: dict[str, str | list[str]] = Field(default_factory=dict)
    #: One-line reason the code is being run. Capped to keep it a caption.
    rationale: str | None = Field(default=None, max_length=512)


class CodeExecResultPayload(GraceModel):
    """``code-exec-result``, emitted AFTER the sandbox returns.
    ``status`` is the HONEST terminal outcome - a blocked or timed-out run is
    never dressed up as ``ok`` - and every number narrated from a run comes from
    the structured ``result``, never from free text a model invents.
    """

    MESSAGE_TYPE: ClassVar[str] = "code-exec-result"

    envelope_type: Literal["code-exec-result"] = "code-exec-result"
    #: Matches the originating ``code-exec-request``.
    code_exec_id: ULIDStr
    status: CodeExecStatus
    stdout_tail: str = Field(default="", max_length=16 * 1024)
    stderr_tail: str = Field(default="", max_length=16 * 1024)
    #: The converted result descriptor, ``{"kind": ...}``; a consumer branches
    #: on ``kind``. ``None`` when the run assigned no result or errored first.
    result: dict[str, Any] | None = None
    #: The ONE honest "you are not seeing all of it" signal: True when the
    #: result descriptor was size-bounded OR a stream tail was cut.
    truncated: bool = False
    #: Wallclock seconds. A latency, not a cost - it says a run hit the cap.
    duration_s: float = Field(default=0.0, ge=0.0)


# --------------------------------------------------------------------------- #
# Routing registry fragment
# --------------------------------------------------------------------------- #

SANDBOX_AGENT_TO_CLIENT_PAYLOADS: dict[str, type[GraceModel]] = {
    CodeExecRequestPayload.MESSAGE_TYPE: CodeExecRequestPayload,
    CodeExecResultPayload.MESSAGE_TYPE: CodeExecResultPayload,
}
