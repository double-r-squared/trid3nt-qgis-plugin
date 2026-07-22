"""Python-sandbox code-exec envelopes (sprint-13 Stage 2 / job-0233).

The conversational data-analysis layer (memory ``project_conversational_data
_analysis_layer``) lets the agent run **user-confirmed ad-hoc Python** over
layers already on the map — "compute the 95th-percentile depth", "cross-tabulate
damage by land-cover class" — in the egress-denied Cloud Run Job sandbox
(``infra/python-sandbox/``, job-0232). The agent's ``code_exec_request`` atomic
tool (``grace2_agent.tools.code_exec_tool``) drives two wire messages, both
**agent -> client** (Appendix A.4 amendment):

1. ``code-exec-request`` (:class:`CodeExecRequestPayload`) — emitted BEFORE the
   sandbox runs. The client renders a confirm card showing the exact Python the
   agent wants to execute, the layers it will receive, and the agent's rationale.
   **Running arbitrary code is a consequential action** (FR-AS-8 / Invariant 9
   spirit), so this is a HARD confirm gate: the agent does not dispatch until the
   user approves. The user's decision rides back on the EXISTING
   ``tool-payload-confirmation`` envelope (``payload_warning.py``) — the
   ``code_exec_id`` is carried as that envelope's ``warning_id`` so the server's
   ``pending_payload_warnings`` future seam matches the reply to the paused
   dispatch with zero new client-control plumbing. ``decision="proceed"`` runs
   it; ``decision="cancel"`` aborts honestly.

2. ``code-exec-result`` (:class:`CodeExecResultPayload`) — emitted AFTER the
   sandbox returns. Carries the run status, bounded stdout/stderr tails, the
   converted ``result`` descriptor (the sandbox's ``convert_result`` output —
   scalar / dataframe / chart / json / too_large), an honest ``truncated`` flag,
   and the wallclock duration. The client renders this inline in the chat
   alongside the tool card.

Determinism boundary (Invariant 1 / Decision H / FR-AS-7)
---------------------------------------------------------
Any NUMBER the agent narrates from a code-exec run is the structured ``result``
descriptor computed by the deterministic sandbox, fed back to Gemini as the
``function_response`` — never free-text the model invents. This envelope is the
visual surface of that same structured result. No cost field anywhere
(Invariant 9): there is no dollar / quota figure on either message.

Confirmation reuse — why no new ``*-response`` message
------------------------------------------------------
The three interaction styles (``request_spatial_input`` /
``request_disambiguation`` / ``request_clarification``) each have their own
request+response pair. Code-exec is a binary approve/deny of a consequential
action, which is *exactly* the shape the payload-warning gate already implements
(``tool-payload-warning`` -> ``tool-payload-confirmation`` with a
``proceed``/``cancel`` decision + a ``warning_id`` correlation key). Reusing that
confirmation reply — rather than minting a fourth ``*-response`` message — keeps
the confirm-gate plumbing single-sourced (one ``pending_payload_warnings`` future
map, one inbound handler) per the job-0233 kickoff's "reuse that seam" directive.
The ONLY new wire shapes are the two agent->client envelopes here.

Registration (manifest job-0233 scope, chart-emission precedent)
----------------------------------------------------------------
Both messages are agent -> client (Appendix A.4). Following the ``secrets`` /
``payload_warning`` / ``chart-emission`` precedent, this module exports the
per-module routing fragment :data:`SANDBOX_AGENT_TO_CLIENT_PAYLOADS`; ``ws.py``
(Appendix A, schema-owned) splats it into ``AGENT_TO_CLIENT_PAYLOADS`` /
``ALL_PAYLOADS`` so the wire envelope is decode-routable like every other message.
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


#: Terminal status of a sandbox run (mirrors the executor envelope's ``status``).
#:
#: - ``ok``      — user code ran to completion and produced a result.
#: - ``error``   — user code raised (the stderr tail carries the traceback).
#: - ``timeout`` — user code exceeded the wallclock cap (SIGALRM or outer kill).
#: - ``blocked`` — the in-process net guard blocked a non-allowlisted egress.
CodeExecStatus = Literal["ok", "error", "timeout", "blocked"]


class CodeExecRequestPayload(GraceModel):
    """``code-exec-request`` (Appendix A.4 amendment, job-0233, sprint-13).

    Agent -> client, emitted BEFORE the sandbox runs. The client renders a
    confirm card; the user approves/denies via a ``tool-payload-confirmation``
    whose ``warning_id`` equals this payload's ``code_exec_id`` (the agent
    matches the reply to the paused dispatch via that key).

    Fields:

    - ``envelope_type`` — discriminator, literal ``"code-exec-request"``.
    - ``code_exec_id`` — ULID for this code-exec dispatch. Doubles as the
      confirmation correlation key (the ``tool-payload-confirmation.warning_id``
      the client echoes back) AND the join key to the matching
      ``code-exec-result``. The client keys the confirm card + result card on it.
    - ``python_code`` — the EXACT Python the agent wants to run, verbatim. Shown
      to the user so they confirm what they're approving (never a paraphrase).
      Non-empty. Capped at 64 KiB — a code-exec snippet is small; a megabyte of
      "code" is a red flag the contract rejects at the boundary.
    - ``layer_refs`` — ``{var_name: layer_uri}`` the sandbox will pre-open as
      rasterio/geopandas handles (or hand back as a URI string). Shown so the
      user sees which of their layers the code can touch. Default empty.

      ADDITIVE multi-frame extension (sandbox-staging): a value may ALSO be a
      LIST of URIs — an ordered set of animation-frame COGs (e.g. the per-step
      flood-depth or GLM lightning frames for ``list_run_frames``). When the
      value is a list the sandbox pre-opens it as an ORDERED LIST of handles
      (``layer_handles[var] = [rasterio.open(p) for p in paths]``) so a snippet
      can iterate frames (a gaussian glow over a flash sequence, a first/peak/
      last panel). The single-string form is unchanged and byte-identical; this
      is the substrate that makes per-frame visualizations just snippets, not
      per-viz tools. The agent pre-fetches every URI (single or list) to a local
      file and rewrites the refs to LOCAL paths before the jailed (network-denied)
      executor opens them.
    - ``rationale`` — optional one-line, human-readable reason the agent is
      running this code ("computing the 95th-percentile flood depth over the
      city polygon"). Capped at 512 chars to keep it a caption, not a narrative.

    Invariant 9 (No cost theater): no cost / quota field. The consequential
    action here is *running code*, gated by user confirmation, not a billed run.
    """

    MESSAGE_TYPE: ClassVar[str] = "code-exec-request"

    envelope_type: Literal["code-exec-request"] = "code-exec-request"
    code_exec_id: ULIDStr
    python_code: str = Field(min_length=1, max_length=64 * 1024)
    #: ``{var: uri}`` OR ``{var: [uri, ...]}`` (ADDITIVE multi-frame extension).
    #: A string value pre-opens a single handle (byte-identical legacy behaviour);
    #: a list value pre-opens an ordered list of frame handles. The Union keeps
    #: the single-string wire shape unchanged while admitting an ordered frame set.
    layer_refs: dict[str, str | list[str]] = Field(default_factory=dict)
    rationale: str | None = Field(default=None, max_length=512)


class CodeExecResultPayload(GraceModel):
    """``code-exec-result`` (Appendix A.4 amendment, job-0233, sprint-13).

    Agent -> client, emitted AFTER the sandbox returns. The client renders the
    run outcome inline in the chat (status pill + stdout/stderr tails + the
    result descriptor — a scalar, a dataframe preview, a chart, or a too-large
    marker).

    Fields:

    - ``envelope_type`` — discriminator, literal ``"code-exec-result"``.
    - ``code_exec_id`` — matches the originating ``code-exec-request`` (the
      client joins the result card to the request card on this key).
    - ``status`` — one of ``ok`` / ``error`` / ``timeout`` / ``blocked``
      (:data:`CodeExecStatus`). The HONEST terminal outcome — a ``blocked`` /
      ``timeout`` run is never dressed up as ``ok``.
    - ``stdout_tail`` — bounded tail of the run's stdout (the executor + host
      runner already truncate; this field is the user-visible capture). Capped
      at 16 KiB on the wire.
    - ``stderr_tail`` — bounded tail of stderr (carries the traceback on
      ``error``, the ``SandboxNetworkBlocked`` message on ``blocked``). Capped at
      16 KiB.
    - ``result`` — the sandbox's converted ``result`` descriptor (the
      ``convert_result`` output: ``{"kind": ...}``), or None when the run
      produced no ``result`` variable / errored before assigning one. Opaque
      dict — the client branches on ``result["kind"]``.
    - ``truncated`` — True when EITHER the result descriptor was size-bounded
      (executor FINDING-1 cap) OR the stdout/stderr was truncated. The single
      honest "you are not seeing the whole thing" signal for the card.
    - ``duration_s`` — wallclock seconds the run took (float). Not a cost — a
      latency, surfaced so the user knows a 60s run hit the cap.

    Invariant 1 (Determinism boundary): the agent narrates numbers from
    ``result``, fed back as the function_response; this envelope is the visual
    twin of that structured payload. Invariant 9: ``duration_s`` is a latency,
    not a dollar figure — no cost field anywhere.
    """

    MESSAGE_TYPE: ClassVar[str] = "code-exec-result"

    envelope_type: Literal["code-exec-result"] = "code-exec-result"
    code_exec_id: ULIDStr
    status: CodeExecStatus
    stdout_tail: str = Field(default="", max_length=16 * 1024)
    stderr_tail: str = Field(default="", max_length=16 * 1024)
    result: dict[str, Any] | None = None
    truncated: bool = False
    duration_s: float = Field(default=0.0, ge=0.0)


# --------------------------------------------------------------------------- #
# Routing registry fragment (sibling wires into ws.ALL_PAYLOADS — see ws.py)
# --------------------------------------------------------------------------- #
#
# Both ``code-exec-request`` and ``code-exec-result`` are agent -> client
# (Appendix A.4). Following the ``secrets`` / ``payload_warning`` /
# ``chart-emission`` precedent, this module exposes the typed routing fragment;
# ``ws.py`` (Appendix A, schema-owned) splats it into ``AGENT_TO_CLIENT_PAYLOADS``
# so the decoder can route the wire envelopes. The confirmation REPLY rides the
# existing ``tool-payload-confirmation`` (client -> agent) message — no new
# client->agent shape is added here.

SANDBOX_AGENT_TO_CLIENT_PAYLOADS: dict[str, type[GraceModel]] = {
    CodeExecRequestPayload.MESSAGE_TYPE: CodeExecRequestPayload,
    CodeExecResultPayload.MESSAGE_TYPE: CodeExecResultPayload,
}
