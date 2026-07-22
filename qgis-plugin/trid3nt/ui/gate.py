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

__all__ = [
    "CodeExecRequest",
    "CredentialRequest",
    "GateDecision",
    "PayloadWarning",
    "code_exec_layer_lines",
    "credential_note_lines",
    "parse_credential_request",
    "estimate_cells",
    "estimate_eta_seconds",
    "estimate_frames",
    "parse_code_exec_request",
    "parse_payload_warning",
    "resolve_code_exec_decision",
    "resolve_gate_decision",
    "summary_lines",
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
