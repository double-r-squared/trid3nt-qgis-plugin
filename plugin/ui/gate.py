"""Gate logic -- PURE PYTHON (no PyQGIS / PyQt imports).

Everything about the dock's gate cards that is NOT a widget, so each gate's
behaviour is unit-testable without QGIS. A gate the server PAUSES a turn on
always has an answer that closes it: unanswerable means a hung turn."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

__all__ = [
    "CodeExecRequest",
    "CodeExecResult",
    "CredentialRequest",
    "GateDecision",
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
    "parse_code_exec_result",
    "parse_credential_request",
    "estimate_cells",
    "estimate_eta_seconds",
    "estimate_frames",
    "parse_code_exec_request",
    "parse_payload_warning",
    "parse_region_choice",
    "parse_secrets_list",
    "parse_spatial_input_request",
    "parse_tool_candidates",
    "region_choice_summary",
    "resolve_code_exec_decision",
    "resolve_gate_decision",
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


#
# The sheet rides an OPTIONAL field of the payload warning, and its presence is
# what turns the gate card into an editable property grid. Edits ride back on
# the SAME confirmation envelope, and a submit-with-edits IS the approval: the
# server does not re-present the sheet.


@dataclass
class ParamRow:
    """One editable row of a resolved param sheet."""

    name: str
    value: object = None
    units: Optional[str] = None
    desc: str = ""
    door: str = "scenario"
    basis: str = "default_demo"
    # WHERE the value came from, as one word of the server's closed set. Empty
    # on a row that is not a filled slot, which is the row that gets no chip.
    origin: str = ""
    source_badge: str = ""
    bounds: Optional[tuple] = None
    user_lever: bool = False
    editable: bool = True
    advanced: bool = False
    group: str = ""
    note: Optional[str] = None

    @property
    def is_numeric(self) -> bool:
        """A bounded row is a MEASUREMENT, so its editor parses numbers.
        ``bool`` is excluded on purpose: it is an ``int`` in Python, and a flag
        is not a measurement."""
        return isinstance(self.value, (int, float)) and not isinstance(self.value, bool)

    @property
    def label(self) -> str:
        return f"{self.name} ({self.units})" if self.units else self.name

    def display(self) -> str:
        """The value as the editor shows it -- never ``None`` spelled out."""
        if self.value is None:
            return ""
        if isinstance(self.value, float):
            return f"{self.value:g}"
        if isinstance(self.value, (list, tuple)):
            return ", ".join(str(v) for v in self.value)
        return str(self.value)


@dataclass
class ParamSheetRequest:
    """Parsed ``param_sheet`` (defensive; raw kept)."""

    workflow: str
    title: str = ""
    rows: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    @property
    def basic(self) -> list:
        return [r for r in self.rows if not r.advanced]

    @property
    def advanced(self) -> list:
        return [r for r in self.rows if r.advanced]


def parse_param_sheet(payload: dict) -> Optional[ParamSheetRequest]:
    """Parse the ``param_sheet`` off a payload warning; None when absent OR
    malformed, so the card falls back to its plain provenance text -- a worse
    form, never a broken one."""
    if not isinstance(payload, dict):
        return None
    sheet = payload.get("param_sheet")
    if not isinstance(sheet, dict):
        return None
    raw_rows = sheet.get("rows")
    if not isinstance(raw_rows, list) or not raw_rows:
        return None
    rows = [_parse_param_row(r) for r in raw_rows if isinstance(r, dict)]
    rows = [r for r in rows if r is not None]
    if not rows:
        return None
    return ParamSheetRequest(
        workflow=str(sheet.get("workflow") or payload.get("tool_name") or "this run"),
        title=str(sheet.get("title") or ""),
        rows=rows,
        raw=sheet,
    )


def _parse_param_row(raw: dict) -> Optional[ParamRow]:
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        return None
    bounds = raw.get("bounds")
    pair = None
    if isinstance(bounds, (list, tuple)) and len(bounds) == 2:
        try:
            pair = (float(bounds[0]), float(bounds[1]))
        except (TypeError, ValueError):
            pair = None
    return ParamRow(
        name=name,
        value=raw.get("value"),
        units=raw.get("units") if isinstance(raw.get("units"), str) else None,
        desc=str(raw.get("desc") or ""),
        door=str(raw.get("door") or "scenario"),
        basis=str(raw.get("basis") or "default_demo"),
        origin=str(raw.get("origin") or ""),
        source_badge=str(raw.get("source_badge") or ""),
        bounds=pair,
        user_lever=bool(raw.get("user_lever")),
        editable=bool(raw.get("editable", True)),
        advanced=bool(raw.get("advanced")),
        group=str(raw.get("group") or ""),
        note=raw.get("note") if isinstance(raw.get("note"), str) else None,
    )


def resolve_param_sheet_edits(rows: list, edited: dict) -> dict:
    """The ``revised_args`` for a submitted sheet: only the rows that actually
    MOVED. An unparseable numeric edit is DROPPED, and an unchanged row is not
    an edit -- sending it would stamp user authorship on an untouched value."""
    by_name = {r.name: r for r in rows}
    revised = {}
    for name, text in (edited or {}).items():
        row = by_name.get(name)
        if row is None or not row.editable:
            continue
        text = "" if text is None else str(text).strip()
        if text == row.display().strip():
            continue
        if row.is_numeric or row.bounds is not None:
            try:
                revised[name] = float(text)
            except (TypeError, ValueError):
                continue
        elif text:
            revised[name] = text
    return revised


def param_sheet_summary(sheet: ParamSheetRequest, revised: dict) -> str:
    """The folded chip line for an ANSWERED form card."""
    if not revised:
        return f"Inputs approved as resolved ({len(sheet.rows)} rows)"
    return "Inputs approved with edits: " + ", ".join(sorted(revised))




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




@dataclass
class GateDecision:
    """What the card should send: ``decision`` + ``revised_args`` (or an
    honest refusal when the combination is not allowed by ``options``)."""

    decision: Optional[str]  # "proceed" | "cancel" | "narrow_scope" | None
    revised_args: Optional[dict]
    note: str = ""


def resolve_gate_decision(
    warning: PayloadWarning,
    cancel: bool = False,
    chosen_resolution_m: Optional[float] = None,
    interval_min: Optional[float] = None,
    duration_hr: Optional[float] = None,
) -> GateDecision:
    """Map the card's UI state to the confirmation envelope. Any override
    becomes ``narrow_scope`` under the EXACT param keys the envelope named."""
    # An unchanged sheet is a plain ``proceed``, EXCEPT on a hard-cap warning,
    # whose options omit proceed: that is refused with an honest note rather
    # than sent as a decision the agent would reject.
    if cancel:
        return GateDecision("cancel", None)

    revised: dict = {}
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


#
# The decision rides back on the ORDINARY payload-confirmation envelope, with
# its ``warning_id`` set to the request's ``code_exec_id``. The server
# fail-closes everything but ``proceed`` -- you do not "narrow" a code snippet
# -- so the card offers exactly Run and Deny, and ``revised_args`` is always
# None.


@dataclass
class CodeExecRequest:
    """Parsed ``code-exec-request`` payload (defensive; raw kept)."""

    code_exec_id: str
    python_code: str
    layer_refs: dict = field(default_factory=dict)
    rationale: str = ""
    raw: dict = field(default_factory=dict)


def parse_code_exec_request(payload: dict) -> Optional[CodeExecRequest]:
    """Parse a ``code-exec-request`` payload; None when it carries no id to
    confirm against, or NO CODE -- approving unseen code is exactly what this
    gate exists to prevent."""
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
    """Map the card's Run or Deny click to the confirmation envelope.
    ``revised_args`` is ALWAYS None: proceed and cancel forbid it, and
    ``narrow_scope`` is never offered here."""
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


#
# The reply is TWO envelopes in order, and the split is the point: the raw key
# rides ``secret-add`` ALONE, and the ``credential-provided`` retry signal that
# follows carries no key material. A ``signup_url`` is None when there is no
# self-serve signup, and is never a fabricated URL. Skip is
# ``provided=False`` with NO preceding secret-add, so the server re-raises the
# original typed error and the agent narrates it honestly.


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
    """Parse a ``credential-request`` payload; None without a ``request_id``
    to correlate the reply, or a ``provider_id`` to scope the key under -- a
    key in the wrong scope is one the paused tool can never re-resolve."""
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
    """The card's muted metadata lines. Every value is a structured envelope
    field, never re-derived from prose, and the raw key never appears."""
    lines = []
    if request.secret_key_name:
        lines.append(f"Key name: {request.secret_key_name}")
    if request.tool_name:
        lines.append(f"Waiting tool: {request.tool_name}")
    return lines


#
# Candidates arrive ranked best-first and MAY be empty on a retrieval degrade,
# in which case the card offers only free text and let-agent-decide. The reply
# is ONE envelope carrying exactly one of three shapes: a verbatim candidate
# pick, typed guidance, or neither. Unanswered, the SERVER's own fail-open
# window proceeds with the agent's top pick, which is what the third shape
# does instantly.


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
    """Parse a ``tool-candidates`` payload; None without a ``request_id``. A
    row with no usable ``tool_name`` is SKIPPED, and an empty surviving list is
    legal -- free text and let-agent-decide still answer the card."""
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
    """Normalize the card's state to ``(tool_name, free_text)``, exactly ONE
    of the three legal shapes. A picked candidate wins outright and any stray
    free text is dropped, so both are never sent together."""
    if isinstance(picked_tool, str) and picked_tool:
        return (picked_tool, None)
    if isinstance(free_text, str) and free_text.strip():
        return (None, free_text.strip())
    return (None, None)


def tool_choice_summary(tool_name: Optional[str], free_text: Optional[str]) -> str:
    """The folded chip line for an ANSWERED picker card. The unanswered
    timeout fold is a different card state and is not built here."""
    if tool_name:
        return f"picked {tool_name}"
    if free_text:
        return "sent guidance to the agent"
    return "agent decided"




def summary_lines(warning: PayloadWarning) -> list:
    """The card's body lines. Every number is a structured envelope field,
    never re-derived from prose."""
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
                # The "local" compute lane renders plain CPU wording, never a
                # cloud vCPU label; any other compute label keeps its own.
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


#
# The server snapped a vague geocode to the WHOLE state bbox, which is the
# honest already-resolved default, and offers a narrower pick. Candidates MAY
# be empty on a region-set build failure, and the card then offers only that
# default. On a region pick the server re-resolves the bbox from the id, which
# is authoritative over any bbox sent with it. A ``whole_state`` answer IS the
# decline path, so the card ALWAYS has a move that closes the gate.


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
    """Parse a ``region-choice-request``; None without a ``request_id``, since
    an unanswerable one leaves the turn paused. A malformed candidate is
    SKIPPED and an empty list is legal: the whole-state default still answers."""
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
    """Build the ``region-choice-provided`` wire dict. An id matching a
    candidate narrows; anything else keeps the whole state, which is the honest
    default AND the decline path, so the gate always closes."""
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


#
# The agent asks the user to pick a geometry: a point, a dragged bbox, or a
# drawn shape whose purpose is either an area or a line. This plugin answers
# the first two through the canvas point-emit and extent tools, and the third
# through the vertex-capture tool -- a polygon for an area, a polyline for a
# line. ``cancelled`` is the decline path and closes the gate.


@dataclass
class SpatialInputRequest:
    """Parsed ``spatial-input-request`` payload (defensive; raw kept)."""

    request_id: str
    mode: str  # "point" | "bbox" | "vector_draw"
    title: str = ""
    description: str = ""
    purpose: str = "aoi"
    raw: dict = field(default_factory=dict)

    @property
    def draw_kind(self) -> str:
        """``"polygon"`` / ``"polyline"`` for a drawable vector_draw, else ``""``."""
        if self.mode != "vector_draw":
            return ""
        return {"aoi": "polygon", "line": "polyline"}.get(self.purpose, "")


def parse_spatial_input_request(payload: dict) -> Optional[SpatialInputRequest]:
    """Parse a ``spatial-input-request``; None without a ``request_id``, which
    would leave the turn hung, or with an unknown ``mode``, which leaves the
    card no affordance to offer."""
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
        purpose=purpose if purpose in ("aoi", "line") else "aoi",
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


def resolve_spatial_input_features(request_id: str, draw_kind: str,
                                   vertices: list) -> dict:
    """Build the response for a DRAWN shape from ``[[lon, lat], ...]`` in draw
    order. A polygon's ring is CLOSED here, because every downstream reader
    wants a ring and no user should have to click a vertex twice."""
    pts = [[round(float(v[0]), 6), round(float(v[1]), 6)] for v in vertices]
    if draw_kind == "polygon":
        if len(pts) >= 3 and pts[0] != pts[-1]:
            pts = pts + [pts[0]]
        geometry = {"type": "Polygon", "coordinates": [pts]}
        role = "aoi"
    else:
        geometry = {"type": "LineString", "coordinates": pts}
        role = "line"
    return {
        "request_id": request_id,
        "geometry_type": "vector_draw",
        "coordinates": None,
        "features": {
            "type": "FeatureCollection",
            "features": [{"type": "Feature", "properties": {"role": role},
                          "geometry": geometry}],
        },
        "cancelled": False,
    }


def spatial_input_vertices_ready(draw_kind: str, vertices: list) -> bool:
    """Enough vertices to submit: 3 for a polygon ring, 2 for a polyline."""
    return len(vertices) >= (3 if draw_kind == "polygon" else 2)


def resolve_spatial_input_cancel(request_id: str) -> dict:
    """Build the response for a CANCEL: ``cancelled=True`` with every geometry
    field None. This is the decline path, and it CLOSES the paused gate rather
    than leaving the turn hung."""
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
    if request.mode == "vector_draw":
        drawn = ((wire.get("features") or {}).get("features") or [{}])[0]
        geom = drawn.get("geometry") or {}
        coords = geom.get("coordinates") or []
        ring = coords[0] if geom.get("type") == "Polygon" and coords else coords
        return f"drew a {request.draw_kind or 'shape'} ({len(ring)} vertices)"
    return "spatial input sent"


#
# ``code_exec_id`` joins the result back to the card that approved it. The
# status is the HONEST terminal outcome and is never dressed up. Fire-and-
# forget: no reply is expected or sent.


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


#
# Emitted when the secrets surface opens, and as the confirmation after an
# add or a revoke. The raw key value NEVER appears here -- only the
# ``vault_ref``-bearing records -- and a vault_ref is never logged.


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
