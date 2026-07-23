"""``code_exec_request`` — user-confirmed Python sandbox atomic tool (job-0233).

This is the LLM-facing entry point to the egress-denied Python sandbox
(``infra/python-sandbox/``, job-0232). It lets the agent run **ad-hoc Python over
layers already on the map** — "compute the 95th-percentile flood depth over the
city polygon", "cross-tabulate damage by land-cover class" — when no existing
atomic tool fits, then narrate the structured result (Decision H / Invariant 1).

The mandatory user-confirm gate (reused, not reinvented)
--------------------------------------------------------
Running arbitrary code is a consequential action. The user MUST approve the exact
Python before it runs. The gate is implemented at the server dispatch layer
(``server.py`` ``_gate_on_code_exec``), which:

1. emits a ``code-exec-request`` envelope (the confirm card — the verbatim code,
   the layer refs, the agent's rationale), and
2. blocks on the EXISTING ``pending_payload_warnings`` future seam (the same
   plumbing the payload-warning gate uses) until the client returns a
   ``tool-payload-confirmation`` whose ``warning_id`` equals the ``code_exec_id``.

On approval the server injects ``confirmed=True`` (+ the ``code_exec_id`` it
already minted + emitted) into this tool's params; on ``cancel`` / timeout it
raises a typed error and this tool body never runs. So the gate cannot be
bypassed from the LLM side — the LLM calls ``code_exec_request(python_code=...)``
WITHOUT ``confirmed``, and only the server's post-approval re-dispatch carries
``confirmed=True``. A direct programmatic caller (tests, a future trusted
composer) may pass ``confirmed=True`` explicitly — that is the single documented
bypass, and it is honest: there is no hidden auto-approve.

The flow once confirmed
-----------------------
``confirmed=True`` -> dispatch via ``sandbox_runner`` (local-subprocess in dev /
the Cloud Run Job in prod) -> shape a :class:`CodeExecResultPayload` -> return a
dict carrying BOTH a compact function_response summary (for Gemini narration —
status + the result descriptor + bounded stdout tail, NEVER the full payload) AND
the full result payload under ``_code_exec_result`` so ``server.py`` emits the
``code-exec-result`` envelope (the chart-emission detect-and-emit precedent).

Determinism boundary (Invariant 1 / Decision H / FR-AS-7)
---------------------------------------------------------
Every number the agent narrates from a sandbox run is the structured ``result``
descriptor the deterministic sandbox computed, fed back as the function_response —
never free-text. No cost field anywhere (Invariant 9): the only quantitative
fields are ``duration_s`` (a latency) and ``truncated`` (an honesty flag).

Caching: ``ttl_class="live-no-cache"`` (FR-DC-6 uncacheable-by-construction —
each run is a fresh interactive computation), so ``cacheable=False`` and
``source_class`` is omitted (the FR-DC-6 cross-field rule).
"""

from __future__ import annotations

import logging
from typing import Any

from trid3nt_contracts import new_ulid
from trid3nt_contracts.sandbox_contracts import CodeExecResultPayload
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server import sandbox_runner

__all__ = [
    "code_exec_request",
    "CodeExecConfirmationRequired",
    "is_code_exec_result",
    "CODE_EXEC_RESULT_KEY",
    "build_code_exec_result_payload",
    "summarize_code_exec_for_llm",
]

logger = logging.getLogger("trid3nt_server.tools.meta.code_exec_tool")

#: The key under which the tool result dict carries the FULL
#: ``CodeExecResultPayload`` (JSON dict) for ``server.py`` to detect + emit the
#: ``code-exec-result`` envelope. Stripped from the function_response by
#: ``adapter.summarize_tool_result`` so Gemini never sees the full payload.
CODE_EXEC_RESULT_KEY = "_code_exec_result"

#: Char cap on the stdout/stderr tails fed back to Gemini in the
#: function_response summary (the wire envelope's own caps are larger; the LLM
#: only needs a short tail to narrate).
_LLM_TAIL_CHARS = 2000


class CodeExecConfirmationRequired(RuntimeError):
    """Raised when ``code_exec_request`` is invoked without ``confirmed=True``.

    This is the fail-closed guard (Invariant 9 spirit): the tool body refuses to
    dispatch a sandbox run that the user has not approved. In normal operation
    the server's ``_gate_on_code_exec`` obtains approval and re-dispatches with
    ``confirmed=True``, so the LLM never sees this error on the happy path — it
    surfaces only if the gate is somehow bypassed (a coding error) or a direct
    caller forgets the flag.

    ``error_code`` / ``retryable`` follow the FR-AS-11 typed-exception
    convention. ``retryable=False``: the LLM cannot retry its way past a missing
    user approval; the gate must run.
    """

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
    """Map a sandbox executor envelope -> a validated :class:`CodeExecResultPayload`.

    ``envelope`` is the dict ``run_sandbox_local`` / the container emits:
    ``{stdout, stderr, result, status, error, stdout_truncated,
    stderr_truncated, wallclock_cap_seconds, ...}``. We map it onto the wire
    payload, deriving the single honest ``truncated`` flag from the union of the
    stdout/stderr truncation flags AND the result descriptor's own ``truncated``
    marker (executor FINDING-1 cap)."""
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
        or envelope.get("envelope_truncated")
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
    """Build the COMPACT function_response Gemini sees (never the full payload).

    Carries the status, the structured ``result`` descriptor (the numbers the
    LLM narrates — Decision H), a short stdout tail, the ``truncated`` honesty
    flag, and the duration. Deliberately omits the wire payload's larger
    stdout/stderr fields and the envelope plumbing — the LLM narrates from
    ``result``, not from raw logs."""
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
    """True when a tool result carries a code-exec-result payload to emit.

    The key signal is the :data:`CODE_EXEC_RESULT_KEY` field holding a
    ``code-exec-result``-shaped dict (``envelope_type == "code-exec-result"``).
    ``server.py`` uses this to fire the ``code-exec-result`` WS envelope in
    addition to the standard function_response (chart-emission precedent)."""
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
    """Run user-confirmed ad-hoc Python over on-map layers in a secure sandbox.

    Use this when: the user asks a quantitative follow-up about a layer
    already on the map that no existing tool answers directly -- a custom
    aggregation, percentile, cross-tabulation, derived field, or
    multi-panel figure -- computed from the layer's actual pixels/features.
    Write a snippet that assigns the answer to ``result`` (scalar, dict,
    DataFrame, or matplotlib Figure); the user sees the exact code and
    must approve it first. Do NOT use for: fetching new data
    (``fetch_*``), running a hazard model (``run_model_*``), a standard
    chart (``generate_histogram``/``generate_damage_distribution``), or
    anything a purpose-built tool already does -- this is the escape
    hatch for ad-hoc computation only.

    DATA ACCESS (the sandbox has NO network and NO guessable file paths):
    you cannot ``rasterio.open("s3://...")`` / ``urllib`` / ``requests`` /
    ``boto3`` from inside it. To use a layer, list its URI in
    ``layer_refs``; the sandbox pre-fetches it off-loop and injects it as
    a variable named EXACTLY the ``layer_refs`` key -- already open
    (raster -> open rasterio dataset, vector -> geopandas GeoDataFrame).
    For key ``"peak"`` you get variable ``peak`` (use ``peak.read(1)``
    directly, never ``rasterio.open(peak)``). Also injected: ``<name>_uri``
    (staged local path) and ``layers`` (name -> handle). Use simple
    identifier keys (letters/digits/underscore). A failed open leaves the
    key as the raw string with the reason in ``result["layer_errors"]``.

    Example::

        layer_refs = {"peak": "s3://.../peak.tif", "f20": "s3://.../frame_20.tif"}
        # peak is ALREADY an open rasterio dataset
        arr = peak.read(1)

    Args:
        python_code: Python to run; assign the answer to ``result``.
            ``numpy``/``pandas``/``rasterio``/``geopandas``/``matplotlib``
            importable.
        layer_refs: ``{var_name: layer_uri}`` for every layer/COG the
            snippet reads -- required since the sandbox has no network.
            Omit only for pure-compute snippets.
        rationale: optional one-line reason shown on the confirm card.

    Returns:
        ``{status, result, stdout_tail, truncated, duration_s, ...}``.
        On non-``ok`` status, narrate the honest reason (``timeout``/
        ``blocked``/``error``) -- never claim a result it didn't produce.
    """
    # MANDATORY confirm gate (fail-closed). The server obtains user approval and
    # re-dispatches with confirmed=True; a call without it never runs the sandbox.
    if not confirmed:
        raise CodeExecConfirmationRequired(code_exec_id)

    # The server mints + emits the code_exec_id with the request card and passes
    # it through on re-dispatch so the request/result cards correlate. If we were
    # somehow called confirmed=True without one (direct programmatic caller), mint
    # a fresh id so the result payload is still well-formed.
    cx_id = code_exec_id or new_ulid()

    logger.info(
        "code_exec_request dispatch code_exec_id=%s code_len=%d n_layers=%d",
        cx_id,
        len(python_code or ""),
        len(layer_refs or {}),
    )

    # Dispatch through the sandbox runner. In local mode this returns a finished
    # envelope dict synchronously; in cloud mode it returns a pending handle whose
    # result envelope is read back from Cloud Logging (job-0265 — the executor
    # prints a marker-prefixed envelope to stdout -> Cloud Logging, read under the
    # agent's identity). A genuine readback failure surfaces a typed error which
    # we convert to an honest error envelope (never a fabricated result).
    dispatch = sandbox_runner.submit_sandbox_job(python_code, layer_refs or {})

    if isinstance(dispatch, sandbox_runner.SandboxExecutionHandle):
        # Cloud dispatch: the executor printed its result envelope to stdout,
        # which Cloud Run ships to Cloud Logging. read_sandbox_result (job-0265)
        # polls Cloud Logging for the marker line and returns the parsed envelope.
        # On a genuine readback failure (envelope not ingested in time, or the
        # logging client can't be built) it raises a typed error — we convert
        # that to an HONEST error envelope (never a fabricated result) so the
        # agent narrates the limitation truthfully (Invariant 1 / Decision H).
        try:
            envelope = sandbox_runner.read_sandbox_result(dispatch)
        except (
            sandbox_runner.SandboxResultNotFound,
            sandbox_runner.SandboxCloudModeUnavailable,
        ) as exc:
            envelope = {
                "status": "error",
                "error": str(exc),
                "stdout": "",
                "stderr": str(exc),
                "result": {"kind": "none", "value": None},
                "stdout_truncated": False,
                "stderr_truncated": False,
            }
    else:
        envelope = dispatch

    payload = build_code_exec_result_payload(cx_id, envelope)
    summary = summarize_code_exec_for_llm(payload)
    # Attach the FULL wire payload for server.py to emit as code-exec-result.
    summary[CODE_EXEC_RESULT_KEY] = payload.model_dump(mode="json")
    return summary
