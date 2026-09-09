"""``code_exec_request`` - the LLM-facing entry to the network-denied code-exec box.
The tool body REFUSES without ``confirmed=True``: the dispatch layer gates on user
approval and only then re-dispatches with the flag, so the gate cannot be bypassed
from the model's side. A direct programmatic caller passing ``confirmed=True`` is
the one documented bypass; there is no hidden auto-approve."""

from __future__ import annotations

import logging
from typing import Any

from trid3nt_contracts import new_ulid
from trid3nt_contracts.sandbox_contracts import CodeExecResultPayload
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.sandbox import box

__all__ = [
    "code_exec_request",
    "CodeExecConfirmationRequired",
    "is_code_exec_result",
    "CODE_EXEC_RESULT_KEY",
    "build_code_exec_result_payload",
    "summarize_code_exec_for_llm",
]

logger = logging.getLogger("trid3nt_server.tools.meta.code_exec_tool.code_exec_tool")

#: The key under which a tool result carries the FULL ``CodeExecResultPayload``
#: for the dispatch layer to detect and emit as a ``code-exec-result`` envelope.
#: It is stripped from the function_response, so the model never sees the payload.
CODE_EXEC_RESULT_KEY = "_code_exec_result"

#: Char cap on the stdout/stderr tails fed back to the model. The wire envelope's
#: own caps are larger; a short tail is all the model needs to narrate.
_LLM_TAIL_CHARS = 2000


class CodeExecConfirmationRequired(RuntimeError):
    """Raised when ``code_exec_request`` is invoked without ``confirmed=True`` -
    the fail-closed guard. ``retryable=False``: no retry gets past a missing user
    approval, the gate has to run."""

    error_code: str = "CODE_EXEC_CONFIRMATION_REQUIRED"
    retryable: bool = False

    def __init__(self, code_exec_id: str | None = None) -> None:
        super().__init__(
            "code_exec_request requires user confirmation before running: the "
            "server emits a code-exec-request card and awaits approval, then "
            "re-dispatches with confirmed=True. This call had confirmed=False"
            + (f" (code_exec_id={code_exec_id})" if code_exec_id else "")
            + "."
        )
        self.code_exec_id = code_exec_id


# --------------------------------------------------------------------------- #
# Result shaping
# --------------------------------------------------------------------------- #


def build_code_exec_result_payload(
    code_exec_id: str, envelope: dict[str, Any]
) -> CodeExecResultPayload:
    """Map a sandbox executor envelope -> a validated
    :class:`CodeExecResultPayload`. The single honest ``truncated`` flag is the
    UNION of the stdout, stderr and result-descriptor truncation flags."""
    status = envelope.get("status", "error")
    if status not in ("ok", "error", "timeout", "blocked"):
        status = "error"

    result_desc = envelope.get("result")
    result_truncated = bool(
        isinstance(result_desc, dict)
        and (result_desc.get("truncated") or result_desc.get("kind") == "too_large")
    )
    truncated = bool(
        envelope.get("stdout_truncated")
        or envelope.get("stderr_truncated")
        or result_truncated
    )

    # Pull the wallclock duration if the runner reported it; fall back to the cap
    # on a timeout (the run consumed the whole budget) else 0.0.
    duration = envelope.get("duration_s")
    if duration is None:
        if status == "timeout":
            duration = float(envelope.get("wallclock_cap_seconds", 0) or 0)
        else:
            duration = 0.0

    # Tail-bound stdout/stderr to the wire field caps (16 KiB) keeping the TAIL
    # (most-recent output / the traceback foot is the useful part).
    stdout = _tail(envelope.get("stdout", "") or "", 16 * 1024)
    stderr = _tail(envelope.get("stderr", "") or "", 16 * 1024)
    # The harness puts the error message in ``error``; fold it into the stderr
    # tail if stderr is empty so the card never shows a bare status with no why.
    err_msg = envelope.get("error")
    if status != "ok" and not stderr and err_msg:
        stderr = _tail(str(err_msg), 16 * 1024)

    return CodeExecResultPayload(
        code_exec_id=code_exec_id,
        status=status,  # type: ignore[arg-type]
        stdout_tail=stdout,
        stderr_tail=stderr,
        result=result_desc if isinstance(result_desc, dict) else None,
        truncated=truncated,
        duration_s=float(duration),
    )


def _tail(text: str, cap: int) -> str:
    """Keep the LAST ``cap`` chars of ``text`` with a leading truncation marker."""
    if len(text) <= cap:
        return text
    keep = cap - 40
    return f"...[{len(text) - keep} chars truncated]...\n" + text[-keep:]


def summarize_code_exec_for_llm(payload: CodeExecResultPayload) -> dict[str, Any]:
    """The COMPACT function_response the model sees, never the full payload. It
    deliberately omits the wire payload's larger stdout/stderr fields: every number
    the model narrates comes from the structured ``result`` descriptor, not logs."""
    return {
        "status": payload.status,
        "result": payload.result,
        "stdout_tail": payload.stdout_tail[-_LLM_TAIL_CHARS:],
        "stderr_tail": payload.stderr_tail[-_LLM_TAIL_CHARS:]
        if payload.status != "ok"
        else "",
        "truncated": payload.truncated,
        "duration_s": payload.duration_s,
        "code_exec_id": payload.code_exec_id,
    }


def is_code_exec_result(result: Any) -> bool:
    """True when a tool result carries a code-exec-result payload to emit: the
    :data:`CODE_EXEC_RESULT_KEY` field holding a dict whose ``envelope_type`` is
    ``"code-exec-result"``."""
    if not isinstance(result, dict):
        return False
    payload = result.get(CODE_EXEC_RESULT_KEY)
    return (
        isinstance(payload, dict)
        and payload.get("envelope_type") == "code-exec-result"
    )


# --------------------------------------------------------------------------- #
# The atomic tool
# --------------------------------------------------------------------------- #


@register_tool(
    AtomicToolMetadata(
        name="code_exec_request",
        ttl_class="live-no-cache",
        cacheable=False,
    ),
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
def code_exec_request(
    python_code: str,
    layer_refs: dict[str, str] | None = None,
    rationale: str | None = None,
    *,
    confirmed: bool = False,
    code_exec_id: str | None = None,
) -> dict[str, Any]:
    """Run user-confirmed ad-hoc Python over on-map layers in a sealed sandbox.

    ROUTING: a quantitative follow-up about an on-map layer no other tool answers -
    a custom aggregation, percentile, cross-tabulation, derived field or figure over
    its real pixels or features. NOT for fetching data, running an
    engine, or a standard chart. Assign the answer to `result` (scalar, dict,
    DataFrame or Figure); the user approves the exact code first.

    NO NETWORK, NO guessable paths: `rasterio.open("s3://...")`, urllib, requests and
    boto3 all fail inside. List every layer the snippet reads in `layer_refs`
    ({var_name: layer_uri}); each is pre-fetched and injected ALREADY
    OPEN under exactly that key - raster as a rasterio dataset, vector as a
    GeoDataFrame - plus `<name>_uri` and `layers`. A failed open leaves the raw
    string and a reason in `result["layer_errors"]`.

    Returns {status, result, stdout_tail, truncated, duration_s}. Narrate a non-ok
    status honestly; never claim a result the run did not produce.
    """
    # MANDATORY confirm gate, fail-closed: a call without confirmed=True never
    # reaches the sandbox.
    if not confirmed:
        raise CodeExecConfirmationRequired(code_exec_id)

    # The id is minted with the request card and passed back on re-dispatch so the
    # request and result cards correlate. A direct caller may arrive confirmed with
    # no id; mint one so the result payload is still well-formed.
    cx_id = code_exec_id or new_ulid()

    logger.info(
        "code_exec_request dispatch code_exec_id=%s code_len=%d n_layers=%d",
        cx_id,
        len(python_code or ""),
        len(layer_refs or {}),
    )

    envelope = box.submit_sandbox_job(python_code, layer_refs or {})

    payload = build_code_exec_result_payload(cx_id, envelope)
    summary = summarize_code_exec_for_llm(payload)
    # Attach the FULL wire payload for the dispatch layer to emit.
    summary[CODE_EXEC_RESULT_KEY] = payload.model_dump(mode="json")
    return summary
