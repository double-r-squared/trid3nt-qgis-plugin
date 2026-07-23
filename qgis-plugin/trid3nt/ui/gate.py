"""Payload-warning gate logic -- PURE PYTHON (no PyQGIS / PyQt imports).

Milestone 2 item 1: the agent's ``tool-payload-warning`` envelope renders as a
real Qt card in the dock, and the user's decision returns as a
``tool-payload-confirmation``. This module holds everything about the gate
that is NOT a widget, so the contract behaviour is unit-testable without QGIS.

Contract source of truth (mirrored EXACTLY, not paraphrased):

* ``contracts/src/trid3nt_contracts/payload_warning.py``
  - warning payload fields: ``warning_id``, ``tool_name``, ``tool_args``,
    ``estimated_mb``, ``threshold_mb``, ``recommendation``,
    ``alternative_args``, ``options`` (non-empty subset of
    {"proceed","cancel","narrow_scope"}; hard-cap warnings OMIT "proceed"),
    ``ttl_seconds``, optional ``granularity`` (#154 gate) and ``time_scale``.
  - confirmation payload fields: ``warning_id``, ``decision``,
    ``revised_args`` (REQUIRED dict for ``narrow_scope``; MUST be None for
    ``proceed`` / ``cancel`` -- the agent's validator rejects violations).

* ``ResolutionPickerCard.tsx`` (web client, separate repo) decision rules:
  - chosen rung == suggested  -> decision "proceed",      revised None
  - chosen rung != suggested  -> decision "narrow_scope", revised
                                 {granularity.resolution_param: chosen}
  - changed cadence/duration  -> merged into the SAME revised dict under
                                 time_scale.cadence_param / .duration_param
  - Cancel                    -> decision "cancel",       revised None
  and the client-side live estimates:
  - cells(chosen) ~= round(estimated_active_cells * (suggested/chosen)^2)
  - eta(chosen)   ~= estimated_solve_seconds * cells(chosen)/cells(suggested)
  - frames        ~= clamp(round(duration_hr*60 / max(interval, floor)),
                           1, max_frames)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlparse

__all__ = [
    "CodeExecRequest",
    "CodeExecResult",
    "CredentialRequest",
    "GateDecision",
    "ImpactSummary",
    "LessonAdded",
    "Mode2CandidateRequest",
    "PayloadWarning",
    "RegionCandidate",
    "RegionChoiceRequest",
    "SecretRow",
    "SpatialInputRequest",
    "ToolCandidate",
    "ToolCandidatesRequest",
    "code_exec_layer_lines",
    "code_exec_result_chip",
    "code_exec_result_lines",
    "credential_note_lines",
    "impact_summary_lines",
    "lesson_added_line",
    "mode2_decision_chip",
    "mode2_reason_lines",
    "parse_code_exec_result",
    "parse_credential_request",
    "estimate_cells",
    "estimate_eta_seconds",
    "estimate_frames",
    "parse_code_exec_request",
    "parse_impact_envelope",
    "parse_lesson_added",
    "parse_mode2_candidate",
    "parse_offer_catalog_addition",
    "parse_payload_warning",
    "parse_region_choice",
    "parse_secrets_list",
    "parse_spatial_input_request",
    "parse_tool_candidates",
    "region_choice_summary",
    "resolve_code_exec_decision",
    "resolve_gate_decision",
    "resolve_mode2_decision",
    "resolve_region_choice",
    "resolve_spatial_input_bbox",
    "resolve_spatial_input_cancel",
    "resolve_spatial_input_point",
    "resolve_tool_choice",
    "secrets_list_lines",
    "spatial_input_summary",
    "summary_lines",
    "tool_choice_summary",
]


@dataclass
class PayloadWarning:
    """Parsed ``tool-payload-warning`` payload (defensive; raw kept)."""

    warning_id: str
    tool_name: str
    estimated_mb: float
    threshold_mb: float
    recommendation: str
    options: list
    tool_args: dict = field(default_factory=dict)
    alternative_args: Optional[dict] = None
    granularity: Optional[dict] = None
    time_scale: Optional[dict] = None
    raw: dict = field(default_factory=dict)

    @property
    def can_proceed(self) -> bool:
        """False on a hard-cap warning (the agent omitted "proceed")."""
        return "proceed" in self.options

    @property
    def can_narrow(self) -> bool:
        return "narrow_scope" in self.options

    @property
    def resolution_choices(self) -> list:
        """The granularity ladder rungs (may be empty)."""
        if not self.granularity:
            return []
        rungs = self.granularity.get("resolution_choices") or []
        return [r for r in rungs if isinstance(r, (int, float)) and r > 0]

    @property
    def suggested_resolution_m(self) -> Optional[float]:
        if not self.granularity:
            return None
        value = self.granularity.get("suggested_resolution_m")
        return float(value) if isinstance(value, (int, float)) and value > 0 else None


def parse_payload_warning(payload: dict) -> Optional[PayloadWarning]:
    """Parse a raw ``tool-payload-warning`` payload dict; None when the
    envelope is unusable (no warning_id -- nothing to confirm against)."""
    if not isinstance(payload, dict):
        return None
    warning_id = payload.get("warning_id")
    if not isinstance(warning_id, str) or not warning_id:
        return None
    options = payload.get("options")
    if not isinstance(options, list) or not options:
        # Contract guarantees a non-empty subset; a malformed envelope gets
        # the full default so the user is never left without a button.
        options = ["proceed", "cancel", "narrow_scope"]
    granularity = payload.get("granularity")
    time_scale = payload.get("time_scale")
    alternative = payload.get("alternative_args")
    tool_args = payload.get("tool_args")

    def _num(key: str) -> float:
        value = payload.get(key)
        return float(value) if isinstance(value, (int, float)) else 0.0

    return PayloadWarning(
        warning_id=warning_id,
        tool_name=str(payload.get("tool_name") or "unknown tool"),
        estimated_mb=_num("estimated_mb"),
        threshold_mb=_num("threshold_mb"),
        recommendation=str(payload.get("recommendation") or ""),
        options=[o for o in options if o in ("proceed", "cancel", "narrow_scope")],
        tool_args=tool_args if isinstance(tool_args, dict) else {},
        alternative_args=alternative if isinstance(alternative, dict) else None,
        granularity=granularity if isinstance(granularity, dict) else None,
        time_scale=time_scale if isinstance(time_scale, dict) else None,
        raw=payload,
    )


# --------------------------------------------------------------------------- #
# Client-side live estimates (ResolutionPickerCard math, mirrored)
# --------------------------------------------------------------------------- #


def estimate_cells(granularity: dict, chosen_resolution_m: float) -> int:
    """Projected active cells at ``chosen_resolution_m`` (area-invariant
    scaling off the suggested rung's authoritative numbers)."""
    base = granularity.get("estimated_active_cells") or 0
    suggested = granularity.get("suggested_resolution_m") or 0
    if chosen_resolution_m <= 0 or suggested <= 0:
        return int(base)
    ratio = float(suggested) / float(chosen_resolution_m)
    return int(round(base * ratio * ratio))


def estimate_eta_seconds(granularity: dict, chosen_resolution_m: float) -> float:
    """Projected solve wall-clock at ``chosen_resolution_m`` (scales with the
    cell ratio)."""
    base_cells = granularity.get("estimated_active_cells") or 0
    base_eta = granularity.get("estimated_solve_seconds") or 0.0
    if base_cells <= 0:
        return float(base_eta)
    cells = estimate_cells(granularity, chosen_resolution_m)
    return float(base_eta) * (cells / float(base_cells))


def estimate_frames(time_scale: dict, interval_min: float, duration_hr: float) -> int:
    """Projected animation frames -- ``duration_hr*60 / interval`` floored at
    ``min_interval_min`` and clamped to ``[1, max_frames]``."""
    floor_min = time_scale.get("min_interval_min") or 1.0
    if floor_min <= 0:
        floor_min = 1.0
    interval = max(float(floor_min), interval_min if interval_min > 0 else float(floor_min))
    duration = duration_hr if duration_hr > 0 else float(time_scale.get("suggested_duration_hr") or 0)
    if interval <= 0 or duration <= 0:
        return 1
    raw = int(round(duration * 60.0 / interval))
    max_frames = time_scale.get("max_frames") or 0
    if max_frames > 0:
        raw = min(raw, int(max_frames))
    return max(1, raw)


# --------------------------------------------------------------------------- #
# Decision resolution (the Proceed / Cancel wiring)
# --------------------------------------------------------------------------- #


@dataclass
class GateDecision:
    """What the card should send: ``decision`` + ``revised_args`` (or an
    honest refusal when the combination is not allowed by ``options``)."""

    decision: Optional[str]  # "proceed" | "cancel" | "narrow_scope" | None
    revised_args: Optional[dict]
    note: str = ""


def release_point_required(warning: PayloadWarning) -> bool:
    """BK-6: the envelope's tool_args flag that this gate needs a user-picked
    release point before Continue is allowed."""
    return bool((warning.tool_args or {}).get("release_point_required"))


def release_point_bbox(warning: PayloadWarning) -> Optional[list]:
    """The previewed mesh bbox [min_lon, min_lat, max_lon, max_lat] the click
    must land inside (None when the envelope did not carry one)."""
    bb = (warning.tool_args or {}).get("mesh_bbox")
    return bb if isinstance(bb, list) and len(bb) == 4 else None


def resolve_gate_decision(
    warning: PayloadWarning,
    cancel: bool = False,
    chosen_resolution_m: Optional[float] = None,
    interval_min: Optional[float] = None,
    duration_hr: Optional[float] = None,
    release_point: Optional[tuple] = None,
) -> GateDecision:
    """Map the card's UI state to the confirmation envelope (web rules).

    - ``cancel=True`` -> ("cancel", None).
    - Nothing changed from the suggestions -> ("proceed", None); on a
      hard-cap warning (no "proceed" in options) this is REFUSED with an
      honest note instead of silently sending a decision the agent rejects.
    - Any override (rung / cadence / window) -> ("narrow_scope", revised)
      with the changed values under the EXACT param keys the envelope names
      (granularity.resolution_param / time_scale.cadence_param /
      time_scale.duration_param).
    """
    if cancel:
        return GateDecision("cancel", None)

    # BK-6: a release-point gate REFUSES to submit until a point is placed;
    # once placed, the point rides revised_args (so the decision is always
    # narrow_scope -- proceed cannot carry revised_args by contract).
    if release_point_required(warning) and release_point is None:
        return GateDecision(
            None, None,
            "Click the map inside the previewed mesh to place the release "
            "point first (click again to move it).",
        )

    revised: dict = {}
    if release_point_required(warning) and release_point is not None:
        revised["release_lon"] = round(float(release_point[0]), 6)
        revised["release_lat"] = round(float(release_point[1]), 6)
    g = warning.granularity
    if g and chosen_resolution_m is not None:
        suggested = warning.suggested_resolution_m
        param = g.get("resolution_param")
        if (
            isinstance(param, str)
            and param
            and suggested is not None
            and chosen_resolution_m > 0
            and chosen_resolution_m != suggested
        ):
            revised[param] = chosen_resolution_m
    ts = warning.time_scale
    if ts:
        cadence_param = ts.get("cadence_param") or "output_interval_min"
        duration_param = ts.get("duration_param") or "duration_hr"
        suggested_interval = ts.get("suggested_interval_min")
        suggested_duration = ts.get("suggested_duration_hr")
        if (
            interval_min is not None
            and interval_min > 0
            and interval_min != suggested_interval
        ):
            floor_min = ts.get("min_interval_min") or 1.0
            revised[cadence_param] = max(float(floor_min), interval_min)
        if (
            duration_hr is not None
            and duration_hr > 0
            and duration_hr != suggested_duration
        ):
            revised[duration_param] = duration_hr

    if revised:
        if not warning.can_narrow:
            return GateDecision(
                None,
                None,
                "This warning does not offer narrow_scope; only "
                + " / ".join(warning.options)
                + " are allowed.",
            )
        return GateDecision("narrow_scope", revised)

    if not warning.can_proceed:
        return GateDecision(
            None,
            None,
            "The estimate exceeds the hard cap -- proceeding unchanged is not "
            "offered. Pick a coarser resolution (narrow scope) or cancel.",
        )
    return GateDecision("proceed", None)


# --------------------------------------------------------------------------- #
# Code-exec approval gate (live-feedback 2026-07-21: the agent's
# ``code-exec-request`` confirm gate had ZERO plugin handling, so the agent
# blocked on its confirmation future forever -- "it just stopped")
# --------------------------------------------------------------------------- #
#
# Contract source of truth (mirrored EXACTLY, not paraphrased):
#
# * ``contracts/src/trid3nt_contracts/sandbox_contracts.py``
#   (``CodeExecRequestPayload``): fields ``code_exec_id`` (ULID; doubles as
#   the confirmation correlation key), ``python_code`` (the EXACT code,
#   verbatim, never a paraphrase), ``layer_refs`` (``{var: uri}`` OR
#   ``{var: [uri, ...]}`` for an ordered frame set), ``rationale`` (optional
#   one-line caption).
# * The decision rides back on the EXISTING ``tool-payload-confirmation``
#   envelope with ``warning_id == code_exec_id`` (the server's shared
#   ``pending_payload_warnings`` confirm seam -- ``server.py``
#   ``_gate_on_code_exec``). ``decision="proceed"`` runs the sandbox; the
#   server FAIL-CLOSES everything else (``cancel`` AND ``narrow_scope``
#   alike -- you don't "narrow" a code snippet), so the card only ever
#   offers Run (proceed) / Deny (cancel), and ``revised_args`` is always
#   None (contract cross-rule: proceed/cancel forbid revised_args).


@dataclass
class CodeExecRequest:
    """Parsed ``code-exec-request`` payload (defensive; raw kept)."""

    code_exec_id: str
    python_code: str
    layer_refs: dict = field(default_factory=dict)
    rationale: str = ""
    raw: dict = field(default_factory=dict)


def parse_code_exec_request(payload: dict) -> Optional[CodeExecRequest]:
    """Parse a raw ``code-exec-request`` payload dict; None when the envelope
    is unusable -- no ``code_exec_id`` (nothing to confirm against) or no
    ``python_code`` (approving unseen code is exactly what this hard confirm
    gate exists to prevent)."""
    if not isinstance(payload, dict):
        return None
    code_exec_id = payload.get("code_exec_id")
    if not isinstance(code_exec_id, str) or not code_exec_id:
        return None
    python_code = payload.get("python_code")
    if not isinstance(python_code, str) or not python_code.strip():
        return None
    layer_refs = payload.get("layer_refs")
    rationale = payload.get("rationale")
    return CodeExecRequest(
        code_exec_id=code_exec_id,
        python_code=python_code,
        layer_refs=layer_refs if isinstance(layer_refs, dict) else {},
        rationale=rationale if isinstance(rationale, str) else "",
        raw=payload,
    )


def resolve_code_exec_decision(approve: bool) -> GateDecision:
    """Map the card's Run / Deny click to the confirmation envelope:
    Run -> ("proceed", None), Deny -> ("cancel", None). ``revised_args`` is
    ALWAYS None -- the contract cross-rule forbids it on proceed/cancel, and
    ``narrow_scope`` is never offered (the server fail-closes it)."""
    return GateDecision("proceed" if approve else "cancel", None)


def code_exec_layer_lines(request: CodeExecRequest) -> list:
    """One honest line per layer the sandbox will receive ("var: uri"; a
    multi-frame LIST value reads "var: N frames") -- so the user sees which
    of their layers the code can touch before approving."""
    lines = []
    for var, ref in (request.layer_refs or {}).items():
        if isinstance(ref, list):
            lines.append(f"{var}: {len(ref)} frames")
        else:
            lines.append(f"{var}: {ref}")
    return lines


# --------------------------------------------------------------------------- #
# Credential-request key-entry card (LANE K, NATE directive 2026-07-22)
# --------------------------------------------------------------------------- #
#
# Contract source of truth (mirrored EXACTLY, not paraphrased):
# ``contracts/src/trid3nt_contracts/secrets.py``:
#
# * inbound ``credential-request`` (CredentialRequestEnvelopePayload):
#   ``request_id`` / ``provider_id`` / ``provider_label`` / ``signup_url``
#   (None = no self-serve signup; NEVER a fabricated URL) /
#   ``secret_key_name`` / ``message`` / ``tool_name``.
# * the reply is TWO envelopes, in order, per the contract's Decision F
#   split (raw key isolated to the secret-add transport):
#     1. ``secret-add``  {provider, case_id, key_value}  -- the ONLY envelope
#        that ever carries the raw key; the server vault-writes it (file
#        vault, 0600) and answers with a refreshed ``secrets-list``.
#     2. ``credential-provided``  {request_id, secret_id, provided} -- the
#        retry signal that resolves the agent's paused-tool future. Skip /
#        decline is ``provided=False`` with NO preceding secret-add (the
#        server then re-raises the original typed error and the agent
#        narrates honestly -- data-source fallback norm).
#   The client sends ``secret_id=None``: the field is Optional in the
#   contract and the server's resume path re-resolves the vault record
#   itself (``_resolve_active_secret_ref``); this synchronous client never
#   blocks the UI thread waiting for the secrets-list to learn the ULID.


@dataclass
class CredentialRequest:
    """Parsed ``credential-request`` payload (defensive; raw kept)."""

    request_id: str
    provider_id: str
    provider_label: str = ""
    secret_key_name: str = ""
    message: str = ""
    tool_name: str = ""
    signup_url: Optional[str] = None
    raw: dict = field(default_factory=dict)

    @property
    def display_label(self) -> str:
        """The human name for chips/titles -- the server's ``provider_label``
        verbatim (the client never hardcodes a provider->label table),
        falling back to the provider_id for a defensively-parsed envelope."""
        return self.provider_label or self.provider_id


def parse_credential_request(payload: dict) -> Optional[CredentialRequest]:
    """Parse a raw ``credential-request`` payload dict; None when the envelope
    is unusable -- no ``request_id`` (nothing to correlate the reply against)
    or no ``provider_id`` (nothing to scope the secret-add under; a key saved
    to the wrong scope is one the paused tool's retry can never re-resolve)."""
    if not isinstance(payload, dict):
        return None
    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        return None
    provider_id = payload.get("provider_id")
    if not isinstance(provider_id, str) or not provider_id:
        return None
    label = payload.get("provider_label")
    key_name = payload.get("secret_key_name")
    message = payload.get("message")
    tool_name = payload.get("tool_name")
    signup_url = payload.get("signup_url")
    return CredentialRequest(
        request_id=request_id,
        provider_id=provider_id,
        provider_label=label if isinstance(label, str) else "",
        secret_key_name=key_name if isinstance(key_name, str) else "",
        message=message if isinstance(message, str) else "",
        tool_name=tool_name if isinstance(tool_name, str) else "",
        signup_url=(
            signup_url if isinstance(signup_url, str) and signup_url else None
        ),
        raw=payload,
    )


def credential_note_lines(request: CredentialRequest) -> list:
    """The card's muted metadata lines -- every value is a structured envelope
    field, never re-derived from prose (Invariant 1). The raw key value never
    appears here (it does not exist yet; the field is client-side only)."""
    lines = []
    if request.secret_key_name:
        lines.append(f"Key name: {request.secret_key_name}")
    if request.tool_name:
        lines.append(f"Waiting tool: {request.tool_name}")
    return lines


# --------------------------------------------------------------------------- #
# Tool-selection picker card (ADR 0018 auto/ask modes -- Stage 3, 2026-07-22)
# --------------------------------------------------------------------------- #
#
# Contract source of truth (mirrored EXACTLY, not paraphrased):
# ``contracts/src/trid3nt_contracts/ws.py``:
#
# * inbound ``tool-candidates`` (ToolCandidatesPayload): ``request_id`` (the
#   correlation key the reply echoes), ``stage_label`` ("Data step" etc. --
#   the card title), ``candidates`` (ranked best-first ``{tool_name, summary,
#   score}`` rows; MAY be empty on retrieval degrade -- the card then offers
#   only free-text + let-agent-decide), ``reason`` ("ambiguity" = AUTO-mode
#   measured near-tie / "ask_mode" = the user asked to see every staged
#   selection), ``timeout_s`` (the SERVER's fail-open window -- unanswered,
#   the turn proceeds with the agent's own top pick).
# * the reply is ONE ``tool-choice`` envelope (ToolChoicePayload):
#   ``request_id`` echo + exactly one of three shapes -- ``tool_name`` set
#   (verbatim candidate pick), ``free_text`` set (typed guidance), both None
#   (let the agent decide -- same outcome as the timeout, but instant).


@dataclass
class ToolCandidate:
    """One ranked candidate row (defensive parse of the contract shape)."""

    tool_name: str
    summary: str = ""
    score: float = 0.0


@dataclass
class ToolCandidatesRequest:
    """Parsed ``tool-candidates`` payload (defensive; raw kept)."""

    request_id: str
    stage_label: str = ""
    candidates: list = field(default_factory=list)  # list[ToolCandidate]
    reason: str = ""
    timeout_s: float = 0.0
    raw: dict = field(default_factory=dict)

    @property
    def reason_note(self) -> str:
        """The honest one-liner under the title: WHY the agent is asking
        (never invented client-side -- keyed off the closed contract enum,
        with an empty fallback for a defensively-parsed envelope)."""
        if self.reason == "ambiguity":
            return "The top matches are nearly tied -- your pick avoids a wrong turn."
        if self.reason == "ask_mode":
            return "Ask mode: confirm which tool runs for this step."
        return ""


def parse_tool_candidates(payload: dict) -> Optional[ToolCandidatesRequest]:
    """Parse a raw ``tool-candidates`` payload dict; None when the envelope is
    unusable -- no ``request_id`` (nothing to correlate the reply against).
    Candidate rows missing a usable ``tool_name`` are SKIPPED, never a crash;
    an empty surviving list is legal (free-text + let-agent-decide still
    render)."""
    if not isinstance(payload, dict):
        return None
    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        return None
    rows = payload.get("candidates")
    candidates: list = []
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = row.get("tool_name")
            if not isinstance(name, str) or not name:
                continue
            summary = row.get("summary")
            score = row.get("score")
            candidates.append(
                ToolCandidate(
                    tool_name=name,
                    summary=summary if isinstance(summary, str) else "",
                    score=(
                        float(score)
                        if isinstance(score, (int, float))
                        and not isinstance(score, bool)
                        else 0.0
                    ),
                )
            )
    stage_label = payload.get("stage_label")
    reason = payload.get("reason")
    timeout_s = payload.get("timeout_s")
    return ToolCandidatesRequest(
        request_id=request_id,
        stage_label=stage_label if isinstance(stage_label, str) else "",
        candidates=candidates,
        reason=reason if isinstance(reason, str) else "",
        timeout_s=(
            float(timeout_s)
            if isinstance(timeout_s, (int, float)) and not isinstance(timeout_s, bool)
            else 0.0
        ),
        raw=payload,
    )


def resolve_tool_choice(
    picked_tool: Optional[str], free_text: Optional[str]
) -> tuple:
    """Normalize the card's UI state to the ``tool-choice`` wire shape
    ``(tool_name, free_text)`` -- exactly one of the contract's three shapes:

    - a picked candidate wins outright (the explicit pick is the stronger
      signal; any stray free text is dropped so both are never sent),
    - else non-whitespace free text rides alone (stripped),
    - else ``(None, None)`` -- the explicit let-agent-decide reply.
    """
    if isinstance(picked_tool, str) and picked_tool:
        return (picked_tool, None)
    if isinstance(free_text, str) and free_text.strip():
        return (None, free_text.strip())
    return (None, None)


def tool_choice_summary(tool_name: Optional[str], free_text: Optional[str]) -> str:
    """The folded chip line for an ANSWERED picker card ("picked
    spatial_query" / "agent decided" / the free-text variant). The
    unanswered-timeout fold ("agent proceeded") is a separate card state --
    see the card's ``mark_superseded``."""
    if tool_name:
        return f"picked {tool_name}"
    if free_text:
        return "sent guidance to the agent"
    return "agent decided"


# --------------------------------------------------------------------------- #
# Honest card text
# --------------------------------------------------------------------------- #


def summary_lines(warning: PayloadWarning) -> list:
    """The card's body lines -- every number is a structured envelope field,
    never re-derived from prose (Invariant 1)."""
    lines = [
        f"Tool: {warning.tool_name}",
        (
            f"Estimated response ~{warning.estimated_mb:g} MB "
            f"(warning threshold {warning.threshold_mb:g} MB)"
        ),
    ]
    if warning.recommendation:
        lines.append(warning.recommendation)
    if not warning.can_proceed:
        lines.append(
            "Hard cap exceeded: proceeding unchanged is not offered -- "
            "narrow the scope or cancel."
        )
    g = warning.granularity
    if g:
        suggested = warning.suggested_resolution_m
        if suggested is not None:
            cells = g.get("estimated_active_cells")
            eta = g.get("estimated_solve_seconds")
            compute = g.get("compute_class") or ""
            vcpus = g.get("vcpus")
            bits = [f"Suggested resolution {suggested:g} m"]
            if isinstance(cells, (int, float)):
                bits.append(f"~{int(cells):,} cells")
            if isinstance(eta, (int, float)):
                bits.append(f"est ~{eta:g}s")
            if compute:
                # Local-cloud fingerprint fix (NATE 2026-07-08): this plugin
                # is the LOCAL product -- the "local" compute lane renders
                # plain CPU wording ("local run"), never the cloud "vCPU"
                # label. Any other compute label (e.g. a remote-mode cloud
                # agent's "standard") keeps the prior wording unchanged.
                if compute == "local":
                    if isinstance(vcpus, (int, float)) and vcpus > 1:
                        label = f"local run ({int(vcpus)} CPU)"
                    else:
                        label = "local run"
                else:
                    label = compute if not vcpus else f"{compute} ({vcpus} vCPU)"
                bits.append(label)
            lines.append(", ".join(bits))
        reason = g.get("reason")
        if reason:
            lines.append(str(reason))
        if g.get("coarsened"):
            lines.append(
                "Note: the suggestion is COARSER than requested (cell cap)."
            )
    ts = warning.time_scale
    if ts:
        interval = ts.get("suggested_interval_min")
        duration = ts.get("suggested_duration_hr")
        frames = ts.get("estimated_frame_count")
        if isinstance(interval, (int, float)) and isinstance(duration, (int, float)):
            line = f"Animation: ~{interval:g} min/frame over {duration:g} h"
            if isinstance(frames, (int, float)):
                line += f" (~{int(frames)} frames)"
            lines.append(line)
        reason = ts.get("reason")
        if reason:
            lines.append(str(reason))
    return lines


# --------------------------------------------------------------------------- #
# Offer-to-add card (LANE P, mode2-candidate + offer-catalog-addition,
# 2026-07-22) -- SRS Sec F.1.2 Mode 2 bounded-growth-path.
# --------------------------------------------------------------------------- #
#
# Contract source of truth (mirrored EXACTLY, not paraphrased):
#
# * ``server/src/trid3nt_server/mode2_classifier.py``
#   (``Mode2CandidateEnvelope`` / ``Mode2Candidate``): the LIGHT, fire-and-
#   forget flag -- server.py emits it as a raw dict (NOT a pydantic
#   ``trid3nt_contracts`` model; that package was FROZEN for the classifier's
#   job), wire type ``mode2-candidate``, payload
#   ``{envelope_type: "mode2-candidate", candidate: {candidate_id, url,
#   domain, domain_tld, confidence, detected_patterns, title,
#   suggested_tool_kind, snippet}}``. Deliberately carries no
#   ``request_id``/``ttl_seconds`` -- the module docstring: "the client opens
#   a passive 'candidate detected' indicator; user opt-in to the full review
#   fires the heavier flow."
# * ``contracts/src/trid3nt_contracts/ws.py`` ``OfferCatalogAdditionPayload``
#   (agent -> client, wire type ``offer-catalog-addition``): the HEAVIER
#   review flow -- ``request_id`` / ``url`` / ``discovered_via`` /
#   ``probe_findings`` (``ProbeFindings``) / ``suggested_catalog_entry``
#   (``SuggestedCatalogEntry``) / ``ttl_seconds``. Not yet emitted by the live
#   server (sprint-08 forward-looking); this module parses it defensively so
#   the plugin is ready the day it is.
# * ``CatalogAdditionResponsePayload`` (client -> agent, wire type
#   ``catalog-addition-response``): ``request_id`` (ULIDStr, echoes the
#   offer's) / ``decision`` (``"accept"``|``"reject"``|None) /
#   ``edited_catalog_entry`` / ``reject_reason`` / ``cancelled``. This is the
#   ONLY reply shape either offer envelope has in the contract.
#
# Bridging light -> heavy (ws.py's own "the two envelopes coexist; the
# lighter one feeds the heavier one" note, sprint-08 comment above
# ``OfferCatalogAdditionPayload``): a light ``mode2-candidate`` has no reply
# contract of its own to invent, but its ``candidate_id`` IS a ULID
# (``new_ulid()`` in ``classify_for_mode2``) -- structurally the same shape
# as the reply envelope's ``request_id`` ULIDStr. This card's decision on a
# LIGHT candidate therefore rides the SAME ``catalog-addition-response``
# contract shape, with ``candidate_id`` standing in for ``request_id``
# (``edited_catalog_entry`` stays None -- a light candidate never carried a
# drafted entry to edit). A genuine HEAVY ``offer-catalog-addition`` answers
# on the identical shape using its own real ``request_id``.


#: Human-readable label per ``classify_for_mode2``'s deterministic pattern
#: name (``_PATTERN_ORDER`` in mode2_classifier.py) -- the "why it was
#: flagged" line the card renders, never re-derived/guessed client-side.
_MODE2_PATTERN_LABELS = {
    "json-ld": "JSON-LD structured data found on the page",
    "openapi-spec-link": "links to an OpenAPI/Swagger spec",
    "rest-endpoint-pattern": "REST API endpoint pattern detected",
    "data-download-link": "offers a downloadable data file (CSV/GeoJSON/...)",
    "tabular-data": "tabular data / dataset listing detected",
}


def _host_from_url(url: str) -> str:
    """Best-effort hostname for a URL; empty string on anything unparsable
    (never raises -- this only feeds display text)."""
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


@dataclass
class Mode2CandidateRequest:
    """Parsed offer-to-add request -- either the LIGHT ``mode2-candidate``
    fire-and-forget flag (``kind="light"``) or the HEAVY
    ``offer-catalog-addition`` review envelope (``kind="heavy"``). Both
    render on the same card; only the reply's ``request_id`` provenance
    differs (see module docstring above)."""

    kind: str  # "light" | "heavy"
    request_id: str  # candidate_id (light) or the real request_id (heavy)
    url: str
    domain: str = ""
    reasons: list = field(default_factory=list)  # honest "why flagged" lines
    title: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def display_host(self) -> str:
        """The host for the title/chip -- the server's ``domain`` verbatim
        when present, else derived from the URL (never invented)."""
        return self.domain or _host_from_url(self.url)


def parse_mode2_candidate(payload: dict) -> Optional[Mode2CandidateRequest]:
    """Parse a raw ``mode2-candidate`` envelope payload; None when the
    envelope is unusable -- no ``candidate_id`` (nothing to correlate a
    decision against) or no ``url`` (nothing to offer adding). Accepts
    either the full envelope shape (``{"candidate": {...}}``) or a bare
    candidate dict, defensively."""
    if not isinstance(payload, dict):
        return None
    candidate = payload.get("candidate")
    if not isinstance(candidate, dict):
        candidate = payload  # bare-candidate fallback
    candidate_id = candidate.get("candidate_id")
    if not isinstance(candidate_id, str) or not candidate_id:
        return None
    url = candidate.get("url")
    if not isinstance(url, str) or not url:
        return None
    domain = candidate.get("domain")
    patterns = candidate.get("detected_patterns")
    reasons = []
    if isinstance(patterns, list):
        reasons = [
            _MODE2_PATTERN_LABELS.get(p, str(p))
            for p in patterns
            if isinstance(p, str)
        ]
    confidence = candidate.get("confidence")
    if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
        reasons.append(f"classifier confidence {confidence:.2f}")
    title = candidate.get("title")
    return Mode2CandidateRequest(
        kind="light",
        request_id=candidate_id,
        url=url,
        domain=domain if isinstance(domain, str) else "",
        reasons=reasons,
        title=title if isinstance(title, str) else "",
        raw=payload,
    )


def parse_offer_catalog_addition(payload: dict) -> Optional[Mode2CandidateRequest]:
    """Parse a raw ``offer-catalog-addition`` envelope payload (contracts
    ``ws.OfferCatalogAdditionPayload``); None when the envelope is unusable
    -- no ``request_id`` (nothing to reply against) or no ``url``."""
    if not isinstance(payload, dict):
        return None
    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        return None
    url = payload.get("url")
    if not isinstance(url, str) or not url:
        return None
    reasons: list = []
    probe = payload.get("probe_findings")
    if isinstance(probe, dict):
        tls_org = probe.get("tls_cert_org")
        if isinstance(tls_org, str) and tls_org:
            reasons.append(f"TLS cert org: {tls_org}")
        tier = probe.get("access_tier_inferred")
        if isinstance(tier, int) and not isinstance(tier, bool):
            reasons.append(f"inferred access tier {tier}")
        if probe.get("stac_root_found"):
            reasons.append("STAC root found")
        if probe.get("ogc_capabilities_found"):
            reasons.append("OGC GetCapabilities found")
        license_observed = probe.get("license_observed")
        if isinstance(license_observed, str) and license_observed:
            reasons.append(f"license: {license_observed}")
    entry = payload.get("suggested_catalog_entry")
    title = ""
    if isinstance(entry, dict):
        name_val = entry.get("name")
        if isinstance(name_val, str):
            title = name_val
    return Mode2CandidateRequest(
        kind="heavy",
        request_id=request_id,
        url=url,
        domain=_host_from_url(url),
        reasons=reasons,
        title=title,
        raw=payload,
    )


def mode2_reason_lines(request: Mode2CandidateRequest) -> list:
    """The card's "why it was flagged" body lines -- every line is a
    structured signal off the envelope, never invented client-side."""
    return list(request.reasons)


def resolve_mode2_decision(request: Mode2CandidateRequest, add: bool) -> dict:
    """Build the ``catalog-addition-response`` wire dict for the card's
    decision (contract ``CatalogAdditionResponsePayload``). The LIGHT
    ``mode2-candidate`` flow has no reply contract of its own, so
    ``request.request_id`` (the candidate's own ULID, or the HEAVY offer's
    real ``request_id``) stands in for the reply's ``request_id`` -- see the
    module docstring's light -> heavy bridge. ``edited_catalog_entry`` is
    always None (no edit UI in this card; the agent writes the original
    suggestion, or nothing for a light candidate that never carried one)."""
    return {
        "request_id": request.request_id,
        "decision": "accept" if add else "reject",
        "edited_catalog_entry": None,
        "reject_reason": None,
        "cancelled": False,
    }


def mode2_decision_chip(request: Mode2CandidateRequest, add: bool) -> str:
    """The folded chip text -- the exact strings the offer-to-add card
    commits to once answered."""
    if add:
        return f"added {request.display_host} to the catalog"
    return "dismissed"


# --------------------------------------------------------------------------- #
# Region-choice picker (state-bbox-fallback narrowing) -- GATE-WAIT.
# --------------------------------------------------------------------------- #
#
# CRITICAL PAIR (server pauses the turn awaiting the reply; unhandled = a hung
# turn, the same bug class the code-exec gate was). The server snaps a
# vague/regional geocode ("south Florida") to the WHOLE state bbox (the honest
# already-resolved default) and OFFERS a narrower pick.
#
# Contract source of truth (mirrored EXACTLY, not paraphrased):
# ``contracts/src/trid3nt_contracts/region_choice.py``:
#
# * inbound ``region-choice-request`` (RegionChoiceRequestEnvelopePayload):
#   ``request_id`` (the correlation key the reply echoes) / ``state_name`` /
#   ``state_code`` / ``state_bbox`` (BBox = ``[min_lon, min_lat, max_lon,
#   max_lat]``; the whole-state default) / ``candidates`` (RegionCandidate
#   rows: ``region_id`` / ``name`` / ``bbox`` / ``admin_level``; MAY be empty
#   on a region-set build failure -- the card then offers only the whole-state
#   default) / ``default_action`` (always ``"use_whole_state"``) / ``message``
#   (the honest "snapped to the whole state, offering a narrower pick" prompt).
# * the reply is ONE ``region-choice-provided``
#   (RegionChoiceProvidedEnvelopePayload): ``request_id`` echo + ``choice``
#   (``"region"`` when narrowed / ``"whole_state"`` for the honest default) +
#   ``selected_region_id`` (the candidate's id when ``choice == "region"``,
#   else None -- the server re-resolves the bbox by this id, authoritative
#   over a client-sent bbox) + ``selected_bbox`` (the candidate's bbox echo,
#   a convenience/fallback; None for whole_state). A ``whole_state`` reply IS
#   the decline path (Invariant 8: cancellation is first-class -- it keeps the
#   already-correct default), so the card ALWAYS has an answer that closes the
#   gate honestly, never a dead-end pause.


@dataclass
class RegionCandidate:
    """One selectable sub-region (defensive parse of the contract shape)."""

    region_id: str
    name: str
    bbox: list  # [min_lon, min_lat, max_lon, max_lat]
    admin_level: str = "county"


@dataclass
class RegionChoiceRequest:
    """Parsed ``region-choice-request`` payload (defensive; raw kept)."""

    request_id: str
    state_name: str
    state_code: str
    state_bbox: Optional[list] = None
    candidates: list = field(default_factory=list)  # list[RegionCandidate]
    message: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def state_label(self) -> str:
        """The whole-state option's label -- the state name, with the code in
        parens when both are present (never invented -- both are envelope
        fields)."""
        if self.state_name and self.state_code:
            return f"{self.state_name} ({self.state_code})"
        return self.state_name or self.state_code or "the whole state"


def _coerce_bbox4(value) -> Optional[list]:
    """A candidate ``[min_lon, min_lat, max_lon, max_lat]`` -> a clean float
    4-list, or None. Never raises."""
    if (
        isinstance(value, (list, tuple))
        and len(value) == 4
        and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value)
    ):
        return [float(v) for v in value]
    return None


def parse_region_choice(payload: dict) -> Optional[RegionChoiceRequest]:
    """Parse a raw ``region-choice-request`` payload dict; None when the
    envelope is unusable -- no ``request_id`` (nothing to correlate the reply
    against; an unanswerable request would leave the server's turn paused,
    exactly the hung-turn bug this gate exists to close). Candidate rows
    missing a usable ``region_id``/``name``/``bbox`` are SKIPPED, never a
    crash; an empty surviving list is legal (the whole-state default still
    answers the gate)."""
    if not isinstance(payload, dict):
        return None
    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        return None
    rows = payload.get("candidates")
    candidates: list = []
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            region_id = row.get("region_id")
            name = row.get("name")
            bbox = _coerce_bbox4(row.get("bbox"))
            if (
                not isinstance(region_id, str)
                or not region_id
                or not isinstance(name, str)
                or not name
                or bbox is None
            ):
                continue
            admin_level = row.get("admin_level")
            candidates.append(
                RegionCandidate(
                    region_id=region_id,
                    name=name,
                    bbox=bbox,
                    admin_level=(
                        admin_level if isinstance(admin_level, str) and admin_level
                        else "county"
                    ),
                )
            )
    state_name = payload.get("state_name")
    state_code = payload.get("state_code")
    message = payload.get("message")
    return RegionChoiceRequest(
        request_id=request_id,
        state_name=state_name if isinstance(state_name, str) else "",
        state_code=state_code if isinstance(state_code, str) else "",
        state_bbox=_coerce_bbox4(payload.get("state_bbox")),
        candidates=candidates,
        message=message if isinstance(message, str) else "",
        raw=payload,
    )


def resolve_region_choice(
    request: RegionChoiceRequest, selected_region_id: Optional[str]
) -> dict:
    """Build the ``region-choice-provided`` wire dict for the card's decision
    (contract RegionChoiceProvidedEnvelopePayload).

    ``selected_region_id`` set + matching a candidate -> ``choice="region"``
    with the id + the candidate's echoed bbox (the server re-resolves by id,
    authoritative). ``None`` (or an unknown id -- the whole-state option) ->
    ``choice="whole_state"`` with both selection fields None: the honest
    already-resolved default, which is ALSO the decline path. Both keys are
    always sent (None-valued when unused) so the wire shape is the full
    contract surface (the ``credential-provided`` explicit-None convention)."""
    if isinstance(selected_region_id, str) and selected_region_id:
        for cand in request.candidates:
            if cand.region_id == selected_region_id:
                return {
                    "request_id": request.request_id,
                    "choice": "region",
                    "selected_region_id": cand.region_id,
                    "selected_bbox": list(cand.bbox),
                }
    return {
        "request_id": request.request_id,
        "choice": "whole_state",
        "selected_region_id": None,
        "selected_bbox": None,
    }


def region_choice_summary(
    request: RegionChoiceRequest, selected_region_id: Optional[str]
) -> str:
    """The folded chip line for an ANSWERED region-choice card."""
    if isinstance(selected_region_id, str) and selected_region_id:
        for cand in request.candidates:
            if cand.region_id == selected_region_id:
                return f"narrowed to {cand.name}"
    return f"kept the whole state ({request.state_label})"


# --------------------------------------------------------------------------- #
# Spatial-input picker (agent needs a point / bbox / drawn geometry) -- GATE-WAIT.
# --------------------------------------------------------------------------- #
#
# CRITICAL PAIR (server pauses the turn awaiting the reply; unhandled = a hung
# turn). The agent asks the user to pick a geometry on the map.
#
# Contract source of truth (mirrored EXACTLY, not paraphrased):
# ``contracts/src/trid3nt_contracts/ws.py``:
#
# * inbound ``spatial-input-request`` (SpatialInputRequestPayload):
#   ``request_id`` (the correlation key the reply echoes) / ``mode``
#   (``"point"`` = single click / ``"bbox"`` = drag rectangle / ``"vector_draw"``
#   = terra-draw FeatureCollection) / ``title`` / ``description`` / ``purpose``
#   (vector_draw only: ``"barrier"`` | ``"line"`` | ``"aoi"``) /
#   ``suggested_view`` / ``reference_layers`` / ``default_timeout_seconds``.
# * the reply is ONE ``spatial-input-response`` (SpatialInputResponsePayload):
#   ``request_id`` echo + ``geometry_type`` (``"point"`` / ``"bbox"`` /
#   ``"vector_draw"``) + ``coordinates`` (``[lon, lat]`` for point,
#   ``[minLon, minLat, maxLon, maxLat]`` for bbox) + ``features`` (the drawn
#   FeatureCollection for vector_draw) + ``cancelled`` (True = the decline path
#   -- every geometry field None). The QGIS plugin captures POINT (canvas
#   point-emit tool) and BBOX (canvas extent tool) picks -- the exact
#   probe/AOI click machinery -- and answers vector_draw HONESTLY (the
#   terra-draw barrier surface is a web affordance; the plugin cannot draw
#   tagged walls/flap-gates, so it offers Cancel, which sends ``cancelled=True``
#   and CLOSES the gate rather than hanging the turn).


@dataclass
class SpatialInputRequest:
    """Parsed ``spatial-input-request`` payload (defensive; raw kept)."""

    request_id: str
    mode: str  # "point" | "bbox" | "vector_draw"
    title: str = ""
    description: str = ""
    purpose: str = "barrier"
    raw: dict = field(default_factory=dict)

    @property
    def supported(self) -> bool:
        """True when the QGIS plugin can capture this mode's geometry (point /
        bbox via the canvas tools). ``vector_draw`` is a web terra-draw
        affordance the plugin cannot reproduce -- the card degrades honestly
        (Cancel closes the gate) rather than pretending to draw."""
        return self.mode in ("point", "bbox")


def parse_spatial_input_request(payload: dict) -> Optional[SpatialInputRequest]:
    """Parse a raw ``spatial-input-request`` payload dict; None when the
    envelope is unusable -- no ``request_id`` (nothing to correlate the reply
    against, leaving the server's turn hung) or an unknown ``mode`` (the card
    would not know which affordance to offer)."""
    if not isinstance(payload, dict):
        return None
    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        return None
    mode = payload.get("mode")
    if mode not in ("point", "bbox", "vector_draw"):
        return None
    title = payload.get("title")
    description = payload.get("description")
    purpose = payload.get("purpose")
    return SpatialInputRequest(
        request_id=request_id,
        mode=mode,
        title=title if isinstance(title, str) else "",
        description=description if isinstance(description, str) else "",
        purpose=purpose if purpose in ("barrier", "line", "aoi") else "barrier",
        raw=payload,
    )


def resolve_spatial_input_point(request_id: str, lon: float, lat: float) -> dict:
    """Build the ``spatial-input-response`` wire dict for a POINT pick
    (contract SpatialInputResponsePayload): ``coordinates=[lon, lat]``,
    ``features`` None. All keys present (the explicit-None convention)."""
    return {
        "request_id": request_id,
        "geometry_type": "point",
        "coordinates": [round(float(lon), 6), round(float(lat), 6)],
        "features": None,
        "cancelled": False,
    }


def resolve_spatial_input_bbox(request_id: str, bbox) -> dict:
    """Build the ``spatial-input-response`` wire dict for a BBOX pick
    (contract SpatialInputResponsePayload): ``coordinates=[minLon, minLat,
    maxLon, maxLat]``, ``features`` None."""
    return {
        "request_id": request_id,
        "geometry_type": "bbox",
        "coordinates": [round(float(v), 6) for v in bbox],
        "features": None,
        "cancelled": False,
    }


def resolve_spatial_input_cancel(request_id: str) -> dict:
    """Build the ``spatial-input-response`` wire dict for a CANCEL (contract
    SpatialInputResponsePayload): ``cancelled=True`` with every geometry field
    None. This is the decline path AND the honest degrade for the unsupported
    ``vector_draw`` mode -- either way it CLOSES the server's paused gate
    (never a hung turn)."""
    return {
        "request_id": request_id,
        "geometry_type": None,
        "coordinates": None,
        "features": None,
        "cancelled": True,
    }


def spatial_input_summary(request: SpatialInputRequest, wire: dict) -> str:
    """The folded chip line for an ANSWERED spatial-input card, keyed off the
    committed wire reply (never re-derived)."""
    if wire.get("cancelled"):
        return "spatial input cancelled"
    coords = wire.get("coordinates") or []
    if request.mode == "point" and len(coords) == 2:
        return f"picked point ({coords[1]:.5f}, {coords[0]:.5f})"
    if request.mode == "bbox" and len(coords) == 4:
        return (
            f"picked bbox [{coords[0]:.4f}, {coords[1]:.4f}, "
            f"{coords[2]:.4f}, {coords[3]:.4f}]"
        )
    return "spatial input sent"


# --------------------------------------------------------------------------- #
# code-exec-result -- the run outcome that follows an APPROVED code-exec-request.
# --------------------------------------------------------------------------- #
#
# Contract source of truth (mirrored EXACTLY, not paraphrased):
# ``contracts/src/trid3nt_contracts/sandbox_contracts.py``
# (``CodeExecResultPayload``): ``code_exec_id`` (joins the result to the
# originating ``code-exec-request`` card) / ``status`` (``ok`` / ``error`` /
# ``timeout`` / ``blocked`` -- the HONEST terminal outcome, never dressed up) /
# ``stdout_tail`` / ``stderr_tail`` (bounded tails) / ``result`` (the converted
# ``{"kind": ...}`` descriptor, or None) / ``truncated`` / ``duration_s``.
# Fire-and-forget by contract (no reply); the plugin updates the approved
# code-exec card's folded chip with the outcome.


@dataclass
class CodeExecResult:
    """Parsed ``code-exec-result`` payload (defensive; raw kept)."""

    code_exec_id: str
    status: str
    stdout_tail: str = ""
    stderr_tail: str = ""
    result: Optional[dict] = None
    truncated: bool = False
    duration_s: float = 0.0
    raw: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def parse_code_exec_result(payload: dict) -> Optional[CodeExecResult]:
    """Parse a raw ``code-exec-result`` payload dict; None when the envelope
    is unusable -- no ``code_exec_id`` (nothing to join to the request card)
    or no ``status`` (no honest outcome to show)."""
    if not isinstance(payload, dict):
        return None
    code_exec_id = payload.get("code_exec_id")
    if not isinstance(code_exec_id, str) or not code_exec_id:
        return None
    status = payload.get("status")
    if not isinstance(status, str) or not status:
        return None
    result = payload.get("result")
    duration = payload.get("duration_s")
    return CodeExecResult(
        code_exec_id=code_exec_id,
        status=status,
        stdout_tail=str(payload.get("stdout_tail") or ""),
        stderr_tail=str(payload.get("stderr_tail") or ""),
        result=result if isinstance(result, dict) else None,
        truncated=bool(payload.get("truncated")),
        duration_s=(
            float(duration)
            if isinstance(duration, (int, float)) and not isinstance(duration, bool)
            else 0.0
        ),
        raw=payload,
    )


def code_exec_result_chip(result: CodeExecResult) -> str:
    """The one-line state chip the approved code-exec card folds to once the
    run outcome lands -- the HONEST terminal status (a blocked/timeout run is
    never dressed up as ok), with the duration when non-trivial."""
    status_word = {
        "ok": "succeeded",
        "error": "errored",
        "timeout": "timed out",
        "blocked": "blocked",
    }.get(result.status, result.status)
    line = f"Code run: {status_word}"
    if result.duration_s > 0:
        line += f" ({result.duration_s:g}s)"
    if result.truncated:
        line += " -- output truncated"
    return line


def code_exec_result_lines(result: CodeExecResult) -> list:
    """The honest body lines for the code-exec result (the tails + a result
    descriptor summary) -- every value is a structured envelope field."""
    lines: list = []
    kind = (result.result or {}).get("kind") if result.result else None
    if isinstance(kind, str) and kind:
        lines.append(f"Result: {kind}")
    if result.stdout_tail.strip():
        lines.append("stdout: " + result.stdout_tail.strip())
    if result.stderr_tail.strip():
        lines.append("stderr: " + result.stderr_tail.strip())
    return lines


# --------------------------------------------------------------------------- #
# secrets-list -- the server's per-user/per-Case secret roster (settings state).
# --------------------------------------------------------------------------- #
#
# Contract source of truth: ``contracts/src/trid3nt_contracts/secrets.py``
# (``SecretsListEnvelopePayload`` -> list[``SecretRecord``]). Emitted in
# response to opening the secrets surface OR as the confirmation after a
# ``secret-add`` / ``secret-revoke``. The raw key value NEVER appears -- only
# the ``vault_ref``-bearing records. The plugin stores these for the
# settings/secrets state (minimal honest handling) and never logs a vault_ref.


@dataclass
class SecretRow:
    """One parsed ``SecretRecord`` (defensive; raw key never present)."""

    secret_id: str
    provider: str
    case_id: Optional[str] = None
    label: Optional[str] = None
    is_active: bool = True

    @property
    def display(self) -> str:
        return self.label or self.provider or self.secret_id


def parse_secrets_list(payload: dict) -> list:
    """Parse a raw ``secrets-list`` payload into ``SecretRow``s (defensive:
    a missing/non-list ``secrets`` field or a row without a usable
    ``secret_id``/``provider`` is skipped, never raised on)."""
    if not isinstance(payload, dict):
        return []
    rows = payload.get("secrets")
    out: list = []
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        secret_id = row.get("secret_id")
        provider = row.get("provider")
        if not isinstance(secret_id, str) or not secret_id:
            continue
        if not isinstance(provider, str) or not provider:
            continue
        label = row.get("label")
        case_id = row.get("case_id")
        out.append(
            SecretRow(
                secret_id=secret_id,
                provider=provider,
                case_id=case_id if isinstance(case_id, str) else None,
                label=label if isinstance(label, str) and label else None,
                is_active=bool(row.get("is_active", True)),
            )
        )
    return out


def secrets_list_lines(secrets: list) -> list:
    """The honest one-line-per-active-secret roster for the settings surface --
    provider + optional label, NEVER a vault_ref or key material."""
    lines: list = []
    for row in secrets:
        if not isinstance(row, SecretRow) or not row.is_active:
            continue
        scope = "this Case" if row.case_id else "all Cases"
        line = f"{row.display} ({row.provider}) -- {scope}"
        lines.append(line)
    return lines


# --------------------------------------------------------------------------- #
# impact-envelope -- Pelicun portfolio damage/loss aggregates (compact note).
# --------------------------------------------------------------------------- #
#
# Contract source of truth: ``contracts/src/trid3nt_contracts/impact_envelope.py``
# (``ImpactEnvelope``): ``n_structures_total`` (the key signal the server keys
# emission on) / ``n_structures_damaged`` / ``n_structures_destroyed`` /
# ``expected_loss_usd`` / ``loss_percentile_95_usd`` / ``impact_area_km2`` /
# population fields (may be None for MS_BUILDINGS inventory). Emitted IN
# ADDITION to the function_response; the plugin renders a compact summary note
# in chat (Invariant 1: every number is a structured aggregate, never prose).


@dataclass
class ImpactSummary:
    """Parsed ``impact-envelope`` payload (defensive; raw kept)."""

    n_structures_total: int
    n_structures_damaged: Optional[int] = None
    n_structures_destroyed: Optional[int] = None
    expected_loss_usd: Optional[float] = None
    loss_percentile_95_usd: Optional[float] = None
    impact_area_km2: Optional[float] = None
    raw: dict = field(default_factory=dict)


def _opt_int(value) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _opt_float(value) -> Optional[float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def parse_impact_envelope(payload: dict) -> Optional[ImpactSummary]:
    """Parse a raw ``impact-envelope`` payload dict; None when the envelope is
    unusable -- no top-level ``n_structures_total`` (the ImpactEnvelope key
    signal the server keys emission on)."""
    if not isinstance(payload, dict):
        return None
    total = _opt_int(payload.get("n_structures_total"))
    if total is None:
        return None
    return ImpactSummary(
        n_structures_total=total,
        n_structures_damaged=_opt_int(payload.get("n_structures_damaged")),
        n_structures_destroyed=_opt_int(payload.get("n_structures_destroyed")),
        expected_loss_usd=_opt_float(payload.get("expected_loss_usd")),
        loss_percentile_95_usd=_opt_float(payload.get("loss_percentile_95_usd")),
        impact_area_km2=_opt_float(payload.get("impact_area_km2")),
        raw=payload,
    )


def _fmt_usd(value: float) -> str:
    """Compact USD -- ``$1.2M`` / ``$340K`` / ``$1,250`` (a latency-free
    aggregate, not a cost estimate; Invariant 9 governs COST fields, this is a
    modeled loss)."""
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"${value / 1_000:.0f}K"
    return f"${value:,.0f}"


def impact_summary_lines(summary: ImpactSummary) -> list:
    """The compact in-chat summary note lines -- every number is a structured
    aggregate off the envelope (Invariant 1, never prose)."""
    lines = [f"Structures assessed: {summary.n_structures_total:,}"]
    dmg_bits = []
    if summary.n_structures_damaged is not None:
        dmg_bits.append(f"{summary.n_structures_damaged:,} damaged")
    if summary.n_structures_destroyed is not None:
        dmg_bits.append(f"{summary.n_structures_destroyed:,} destroyed")
    if dmg_bits:
        lines.append("  ".join(dmg_bits))
    if summary.expected_loss_usd is not None:
        loss = f"Expected loss: {_fmt_usd(summary.expected_loss_usd)}"
        if summary.loss_percentile_95_usd is not None:
            loss += f" (P95 {_fmt_usd(summary.loss_percentile_95_usd)})"
        lines.append(loss)
    if summary.impact_area_km2 is not None:
        lines.append(f"Impact area: {summary.impact_area_km2:g} km2")
    return lines


# --------------------------------------------------------------------------- #
# lesson-added -- the LESSONS LOOP ack (subtle status note).
# --------------------------------------------------------------------------- #
#
# Server source: ``server.py`` ``_handle_lesson_add`` emits a raw-JSON
# envelope (no ``trid3nt_contracts`` model yet -- the payload has no extra-key
# schema): ``{"envelope_type": "lesson-added", "lesson_id": ..., "lesson":
# <normalized text>}``. The plugin surfaces a subtle status note.


@dataclass
class LessonAdded:
    """Parsed ``lesson-added`` ack payload (defensive; raw kept)."""

    lesson_id: str = ""
    lesson: str = ""
    raw: dict = field(default_factory=dict)


def parse_lesson_added(payload: dict) -> Optional[LessonAdded]:
    """Parse a raw ``lesson-added`` payload dict; None when the envelope is
    unusable -- neither a ``lesson_id`` nor a ``lesson`` text is present
    (nothing to acknowledge)."""
    if not isinstance(payload, dict):
        return None
    lesson_id = payload.get("lesson_id")
    lesson = payload.get("lesson")
    lesson_id = lesson_id if isinstance(lesson_id, str) else ""
    lesson = lesson if isinstance(lesson, str) else ""
    if not lesson_id and not lesson.strip():
        return None
    return LessonAdded(lesson_id=lesson_id, lesson=lesson, raw=payload)


def lesson_added_line(added: LessonAdded) -> str:
    """The subtle status note the dock shows on a lesson ack."""
    if added.lesson.strip():
        snippet = added.lesson.strip()
        if len(snippet) > 120:
            snippet = snippet[:117] + "..."
        return f"Lesson saved: {snippet}"
    return "Lesson saved."
