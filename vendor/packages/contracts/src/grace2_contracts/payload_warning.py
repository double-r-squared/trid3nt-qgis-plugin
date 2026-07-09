"""Tool payload warning envelopes (Appendix A amendment, job-0127, sprint-12-mega).

The chat payload-warning system gates large tool dispatches behind explicit
user confirmation. Before invoking a tool whose estimated response payload
exceeds the warning threshold (default 25 MB), the agent emits a
``tool-payload-warning`` envelope and pauses dispatch until the client
returns a ``tool-payload-confirmation`` envelope carrying the user's decision.

This pattern keeps three guarantees:

1. **Determinism boundary (Invariant 1).** The estimator output is a
   structured numeric field (``estimated_mb``), never narrated free text.
   The threshold is also a numeric field on the envelope (``threshold_mb``)
   so the client renders both numbers consistently without re-deriving them
   from the agent's prose.

2. **No cost theater (Invariant 9).** ``estimated_mb`` is a payload-size
   estimate, NOT a dollar / latency / quota figure. The recommendation is a
   short human-readable nudge ("Consider narrowing bbox to <region>"), not a
   pricing surface. ``alternative_args`` is the agent's tentative narrowed
   call signature — the user can accept it via ``decision="narrow_scope"``
   with ``revised_args`` echoed back.

3. **Confirmation before consequence (Invariant 9).** The warning envelope
   is the gate; the matching confirmation envelope is the consequence-
   authorizing response. Without a confirmation matching the same
   ``warning_id`` the agent does not dispatch.

Routing per Wave 1.5 ``AtomicToolMetadata.payload_mb_estimator_name``: the
agent's dispatcher resolves the named callable in the tool module's
namespace, calls ``estimate_payload_mb(**args)``, and gates on its return.
A tool that does not declare an estimator skips the gate.

Hard cap behaviour: when ``estimated_mb`` exceeds a hard threshold
(default 250 MB, configurable via env), the warning envelope is still
emitted but ``options`` is constrained — ``proceed`` is removed and the
user must pick ``cancel`` or ``narrow_scope`` (the agent enforces this
on receipt).

See memory: ``feedback_large_payload_chat_warning``. See
``packages/contracts/src/grace2_contracts/tool_registry.py`` for the
``AtomicToolMetadata.payload_mb_estimator_name`` field. See
``services/agent/src/grace2_agent/server.py`` for the dispatcher gate.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import Field, field_validator, model_validator

from .common import GraceModel, ULIDStr

__all__ = [
    "PayloadWarningOption",
    "PayloadWarningEnvelopePayload",
    "PayloadConfirmationDecision",
    "PayloadConfirmationEnvelopePayload",
    "GranularitySuggestion",
    "TimeScaleSuggestion",
    "WARNING_THRESHOLD_MB_DEFAULT",
    "HARD_CAP_MB_DEFAULT",
]


#: Default warning threshold in megabytes. Override per-deployment via the
#: ``GRACE2_PAYLOAD_WARNING_MB`` env var read by the agent. Kept as a module
#: constant (not a contract field) so call sites that don't have the env
#: have a sensible default.
WARNING_THRESHOLD_MB_DEFAULT: float = 25.0

#: Default hard-cap in megabytes. Override per-deployment via the
#: ``GRACE2_PAYLOAD_HARDCAP_MB`` env var read by the agent. Above this size
#: ``proceed`` is removed from ``options``; the user must pick ``cancel``
#: or ``narrow_scope``.
HARD_CAP_MB_DEFAULT: float = 250.0


#: The three actions a payload-warning gate can return.
#:
#: - ``proceed`` — dispatch the tool with the originally-proposed args.
#:   Removed from ``options`` when the estimate exceeds the hard cap.
#: - ``cancel`` — abort the dispatch; the agent surfaces a typed failure
#:   to the chat (no consequence executed).
#: - ``narrow_scope`` — re-dispatch with revised args (the client returns
#:   them via ``revised_args`` on the confirmation envelope).
PayloadWarningOption = Literal["proceed", "cancel", "narrow_scope"]


class GranularitySuggestion(GraceModel):
    """Pre-run mesh-granularity suggestion attached to a ``tool-payload-warning``
    (#154 granularity gate, sprint-16).

    The granularity gate makes mesh resolution a USER lever rather than a
    silent auto-coarsen (memory: ``feedback_user_controlled_granularity``).
    Before a heavy solver run (SWMM / SFINCS), the autoscaler emits a
    SUGGESTED resolution alongside the active-cell count, the estimated
    solve wall-clock, and the chosen compute class so the user can SEE the
    cost-of-resolution and override the rung before execution. The user's
    override reuses the existing ``tool-payload-confirmation`` path
    (``decision="narrow_scope"`` + ``revised_args`` carrying the chosen
    ``resolution_param`` value) — no new confirmation envelope.

    This model is an OPTIONAL enrichment on ``PayloadWarningEnvelopePayload``;
    a payload-warning without a granularity suggestion is unchanged.

    Fields:

    - ``engine`` — the run this suggestion is for. Solvers ``"swmm"`` /
      ``"sfincs"`` OR a FETCHER resolution choice ``"dem"`` / ``"topobathy"``
      / ``"landcover"`` (#154 gate widened, NATE 2026-06-26; landcover added
      to support state-scale NLCD auto-coarsening) -- the same ladder UI
      describes a fetch resolution, not only a solver mesh.
    - ``resolution_param`` — the args field the user overrides:
      ``"target_resolution_m"`` (SWMM overland cell size),
      ``"grid_resolution_m"`` (SFINCS grid), or ``"resolution_m"`` (a
      DEM/topobathy fetcher's cell size). The client writes the chosen rung
      back into ``revised_args`` under this exact key.
    - ``suggested_resolution_m`` — the autoscaler's recommended cell size in
      metres (> 0). The default-selected rung.
    - ``resolution_choices`` — the ascending ladder of selectable cell sizes
      in metres (the rungs the user can pick from). Each rung > 0.
    - ``estimated_active_cells`` — projected active-cell count at the
      suggested resolution (>= 0). Drives the "~46k cells" readout.
    - ``estimated_solve_seconds`` — projected solver wall-clock in seconds
      at the suggested resolution (>= 0). An inference, surfaced as "est ~70s".
    - ``vcpus`` — vCPU count of the chosen compute class (> 0).
    - ``compute_class`` — human/infra label for the compute tier
      (e.g. a Batch Spot instance type "c7i.2xlarge"). FREE str (not a
      Literal), so a fetch gate sets ``compute_class="fetch"`` with no
      contract change (NATE 2026-06-26). A fetch suggestion passes the
      analogue values ``vcpus=1``, ``estimated_solve_seconds=0.0``,
      ``coarsened=False``, ``spot_label=None`` — all accepted by the
      existing bounds (no field made Optional, no Literal added here).
    - ``cell_cap`` — the element-cap the autoscaler honoured (> 0); the
      ceiling above which the suggestion coarsens.
    - ``coarsened`` — True when the suggested resolution is COARSER than the
      user's originally-requested resolution because the request would have
      exceeded ``cell_cap``. Surfaces the "we coarsened" honesty signal.
    - ``reason`` — short human-readable rationale (e.g. "Requested 10 m would
      exceed the 2M-cell cap; suggesting 30 m").
    - ``spot_label`` — optional Spot-pricing/instance label for the readout
      (e.g. "c7i.2xlarge Spot"); None when not on a Spot tier.

    Invariant 1 (Determinism boundary): every number the chat narrates here is
    a structured field, never inferred from prose.
    Invariant 9 (No cost theater): cells / seconds / vCPUs / instance label are
    capacity + capability descriptors, NOT dollar figures. No dollar field.
    """

    # NATE 2026-06-26: widened engine + resolution_param so the #154 gate can
    # also describe a FETCHER resolution choice (dem / topobathy / landcover
    # fetch), not just the SWMM/SFINCS solver mesh. compute_class stays a free
    # str, so a fetch gate sets compute_class="fetch" with no Literal change.
    # "landcover" added to support state-scale NLCD auto-coarsening.
    engine: Literal["swmm", "sfincs", "dem", "topobathy", "landcover"]
    resolution_param: Literal[
        "target_resolution_m", "grid_resolution_m", "resolution_m"
    ]
    suggested_resolution_m: float = Field(gt=0.0)
    resolution_choices: list[float] = Field(default_factory=list)
    estimated_active_cells: int = Field(ge=0)
    estimated_solve_seconds: float = Field(ge=0.0)
    vcpus: int = Field(gt=0)
    compute_class: str = Field(min_length=1)
    cell_cap: int = Field(gt=0)
    coarsened: bool
    reason: str = Field(max_length=512)
    spot_label: str | None = None

    @field_validator("suggested_resolution_m")
    @classmethod
    def _validate_suggested_resolution(cls, value: float) -> float:
        """Resolution must be a positive cell size (metres)."""
        if value <= 0.0:
            raise ValueError(
                f"suggested_resolution_m must be > 0; got {value!r}"
            )
        return value

    @field_validator("resolution_choices")
    @classmethod
    def _validate_resolution_choices(cls, value: list[float]) -> list[float]:
        """Every ladder rung must be a positive cell size (metres). Negative or
        zero rungs are non-sensical and would render an unselectable option."""
        for rung in value:
            if rung <= 0.0:
                raise ValueError(
                    f"resolution_choices rungs must be > 0; got {rung!r} "
                    f"in {value!r}"
                )
        return value

    @field_validator("estimated_active_cells")
    @classmethod
    def _validate_active_cells(cls, value: int) -> int:
        """A negative cell count is non-sensical."""
        if value < 0:
            raise ValueError(
                f"estimated_active_cells must be >= 0; got {value!r}"
            )
        return value

    @field_validator("estimated_solve_seconds")
    @classmethod
    def _validate_solve_seconds(cls, value: float) -> float:
        """A negative wall-clock estimate is non-sensical."""
        if value < 0.0:
            raise ValueError(
                f"estimated_solve_seconds must be >= 0; got {value!r}"
            )
        return value

    @field_validator("vcpus")
    @classmethod
    def _validate_vcpus(cls, value: int) -> int:
        """A compute tier must have at least one vCPU."""
        if value <= 0:
            raise ValueError(f"vcpus must be > 0; got {value!r}")
        return value

    @field_validator("cell_cap")
    @classmethod
    def _validate_cell_cap(cls, value: int) -> int:
        """The element-cap must be a positive ceiling."""
        if value <= 0:
            raise ValueError(f"cell_cap must be > 0; got {value!r}")
        return value


class TimeScaleSuggestion(GraceModel):
    """Pre-run TIME-SCALE (animation cadence + window) suggestion attached to a
    ``tool-payload-warning`` (combined run-settings gate, sprint-16).

    The sibling of :class:`GranularitySuggestion`: where granularity makes the
    SPATIAL resolution a user lever, this makes the TEMPORAL resolution one. A
    coastal/wave SFINCS run animates its rising water at a FINE minute-scale
    stride (so it reads as water rolling in, not a slowly-filling bathtub —
    memory: the "looks like rain" fix); the pluvial path animates hourly. The
    cadence (minutes per frame) and the simulation window (duration in hours)
    together determine the animation FRAME COUNT — too many frames balloons the
    payload, too few hides the wave motion. This model surfaces the agent's
    SUGGESTED cadence + window + the resulting frame-count estimate so the user
    can SEE "~N frames every M min" and override the cadence / window before the
    solve.

    The override reuses the EXISTING ``tool-payload-confirmation`` path
    (``decision="narrow_scope"`` + ``revised_args`` carrying the chosen
    ``cadence_param`` value and/or ``duration_param`` value) — no new envelope.
    The combined run-settings card sends the resolution override AND the
    time-scale override in the SAME ``revised_args`` dict (ONE interaction).

    This model is an OPTIONAL enrichment on ``PayloadWarningEnvelopePayload``; a
    payload-warning without a time-scale suggestion is unchanged. The pluvial
    flood path emits NO time-scale row (hourly cadence is fixed) so the gate
    falls back to the granularity-only card.

    Fields:

    - ``cadence_param`` — the solver-args field the user overrides for cadence:
      ``"output_interval_min"`` (minutes between animation frames). The client
      writes the chosen value back into ``revised_args`` under this exact key.
    - ``suggested_interval_min`` — the agent's recommended minutes-per-frame
      (> 0). The default-prefilled cadence.
    - ``interval_choices`` — an OPTIONAL ascending ladder of suggested cadences
      (minutes) for quick-pick chips; the card ALSO exposes a free numeric edit
      so a value off the ladder is allowed. May be empty (free-edit only).
    - ``duration_param`` — the solver-args field for the simulation window:
      ``"duration_hr"``. The client writes the chosen window back under this key.
    - ``suggested_duration_hr`` — the agent's simulation window in hours (> 0).
      The default-prefilled, editable duration.
    - ``estimated_frame_count`` — projected animation frames at the suggested
      cadence + window (>= 1). Drives the "~N frames" readout; the client
      LIVE-recomputes it as the user edits (``duration_hr*60 / interval``,
      clamped to ``[1, max_frames]``).
    - ``max_frames`` — the postprocess frame cap (> 0); the ceiling the
      recompute clamps to (so the readout never advertises an unbounded count).
    - ``min_interval_min`` — physical floor on the cadence (minutes, > 0); the
      deck re-floors at this, so a finer edit cannot yield more frames than the
      deck emits. The client floors the editable interval at this value.
    - ``is_coastal`` — True for a coastal/wave run (fine minute-scale stride),
      False for pluvial (hourly). Surfaced so the card labels the cadence honestly.
    - ``reason`` — short human-readable rationale (e.g. "Coastal surge: 5-min
      frames over a 6 h window animate the wave roll-in").

    Invariant 1 (Determinism boundary): every number the chat narrates here is a
    structured field, never inferred from prose.
    Invariant 9 (No cost theater): frames / minutes / hours are capacity +
    capability descriptors, NOT dollar figures. No dollar field.
    """

    cadence_param: Literal["output_interval_min"] = "output_interval_min"
    suggested_interval_min: float = Field(gt=0.0)
    interval_choices: list[float] = Field(default_factory=list)
    duration_param: Literal["duration_hr"] = "duration_hr"
    suggested_duration_hr: float = Field(gt=0.0)
    estimated_frame_count: int = Field(ge=1)
    max_frames: int = Field(gt=0)
    min_interval_min: float = Field(default=1.0, gt=0.0)
    is_coastal: bool = True
    reason: str = Field(default="", max_length=512)

    @field_validator("suggested_interval_min")
    @classmethod
    def _validate_suggested_interval(cls, value: float) -> float:
        """The cadence must be a positive minutes-per-frame."""
        if value <= 0.0:
            raise ValueError(
                f"suggested_interval_min must be > 0; got {value!r}"
            )
        return value

    @field_validator("interval_choices")
    @classmethod
    def _validate_interval_choices(cls, value: list[float]) -> list[float]:
        """Every cadence rung must be a positive minutes value."""
        for rung in value:
            if rung <= 0.0:
                raise ValueError(
                    f"interval_choices rungs must be > 0; got {rung!r} in {value!r}"
                )
        return value

    @field_validator("suggested_duration_hr")
    @classmethod
    def _validate_suggested_duration(cls, value: float) -> float:
        """The simulation window must be a positive number of hours."""
        if value <= 0.0:
            raise ValueError(
                f"suggested_duration_hr must be > 0; got {value!r}"
            )
        return value

    @field_validator("estimated_frame_count")
    @classmethod
    def _validate_frame_count(cls, value: int) -> int:
        """A non-empty animation needs at least one frame."""
        if value < 1:
            raise ValueError(
                f"estimated_frame_count must be >= 1; got {value!r}"
            )
        return value

    @field_validator("max_frames")
    @classmethod
    def _validate_max_frames(cls, value: int) -> int:
        """The frame cap must be a positive ceiling."""
        if value <= 0:
            raise ValueError(f"max_frames must be > 0; got {value!r}")
        return value


class PayloadWarningEnvelopePayload(GraceModel):
    """``tool-payload-warning`` (Appendix A amendment, job-0127).

    Agent emits this when a registered estimator's projected payload
    exceeds the warning threshold. The client renders an inline chat card
    showing the tool name, the projected MB, the threshold, the agent's
    short recommendation, and (optionally) the agent's suggested narrowing
    args. The user picks one of the actions in ``options``.

    Fields:

    - ``envelope_type`` — discriminator, literal ``"tool-payload-warning"``.
    - ``warning_id`` — ULID identifying the gate; the response carries it
      back so the agent can match the confirmation to the right paused
      coroutine.
    - ``tool_name`` — atomic-tool function name (Python identifier). The
      client renders this to the user.
    - ``tool_args`` — the args the agent intended to dispatch (sanitized,
      JSON-serializable). The client shows a summary so the user can
      verify what's about to be fetched.
    - ``estimated_mb`` — the estimator's projected payload size in
      megabytes. Float; the estimator may return a fractional value.
    - ``threshold_mb`` — the threshold the estimate exceeded. The client
      surfaces both numbers (estimate + threshold) so the user understands
      WHY the gate fired.
    - ``recommendation`` — short human-readable suggestion (e.g.
      "Consider narrowing bbox to a single county" or "Filter to fewer
      bands"). Capped at 512 chars.
    - ``alternative_args`` — optional agent-drafted narrowed args. When
      present, the client can offer a one-click "narrow scope" using these
      exact args (no second prompt needed). Permissive ``dict`` shape so
      tool-specific narrowing strategies (smaller bbox / fewer time steps
      / fewer features) all fit. The agent service round-trips this
      through the target tool's signature before dispatch.
    - ``options`` — non-empty subset of {``"proceed"``, ``"cancel"``,
      ``"narrow_scope"``}. When the estimate exceeds the hard cap, the
      agent omits ``"proceed"`` here so the client cannot offer it.
    - ``ttl_seconds`` — gate validity (seconds since envelope ``ts``); on
      expiry the gate becomes a typed failure (``CONFIRMATION_TIMEOUT``
      from A.6). Default 300s — payload-warning gates are read-decisions,
      so they get the same TTL as a confirmation-request.
    - ``granularity`` — OPTIONAL pre-run mesh-granularity suggestion
      (#154 granularity gate). When present, the client renders the
      resolution ladder + estimated cells / solve time / compute class and
      lets the user override the rung before the heavy solver run. The
      override rides back on the existing ``tool-payload-confirmation``
      (``decision="narrow_scope"`` + ``revised_args``). None on ordinary
      payload-warnings — fully back-compatible.
    - ``time_scale`` — OPTIONAL pre-run time-scale (animation cadence +
      window) suggestion (combined run-settings gate). When present ALONGSIDE
      ``granularity``, the client renders ONE combined "Run settings" card
      letting the user review + override BOTH the spatial resolution AND the
      temporal cadence/window before the heavy solver run, sending both
      overrides in a SINGLE ``revised_args`` dict (ONE interaction). None for
      the pluvial path (hourly cadence is fixed) — the card falls back to the
      granularity-only resolution gate. Fully back-compatible.

    Invariant 1 (Determinism boundary): every number the chat narrates
    here is a structured field, never inferred from prose.
    Invariant 9 (No cost theater): ``estimated_mb`` is a payload-size
    estimate, not a dollar / latency / quota figure. No cost field anywhere.
    """

    MESSAGE_TYPE: ClassVar[str] = "tool-payload-warning"

    envelope_type: Literal["tool-payload-warning"] = "tool-payload-warning"
    warning_id: ULIDStr
    tool_name: str = Field(min_length=1)
    tool_args: dict[str, Any] = Field(default_factory=dict)
    estimated_mb: float = Field(ge=0.0)
    threshold_mb: float = Field(ge=0.0)
    recommendation: str = Field(max_length=512)
    alternative_args: dict[str, Any] | None = None
    options: list[PayloadWarningOption] = Field(
        default_factory=lambda: ["proceed", "cancel", "narrow_scope"],
        min_length=1,
        max_length=3,
    )
    ttl_seconds: int = Field(default=300, ge=1)
    granularity: GranularitySuggestion | None = None
    time_scale: TimeScaleSuggestion | None = None

    @model_validator(mode="after")
    def _validate_options_unique(self) -> "PayloadWarningEnvelopePayload":
        """Options must be unique — duplicates would render duplicate buttons."""
        if len(self.options) != len(set(self.options)):
            raise ValueError(
                f"options must be unique; got {self.options!r}"
            )
        return self


#: The user's selection from a ``tool-payload-warning`` modal.
#:
#: Matches the ``options`` set on the originating warning envelope. The
#: agent's gate handler enforces that ``proceed`` is rejected when the
#: original warning did not advertise it (hard-cap path).
PayloadConfirmationDecision = Literal["proceed", "cancel", "narrow_scope"]


class PayloadConfirmationEnvelopePayload(GraceModel):
    """``tool-payload-confirmation`` (Appendix A amendment, job-0127).

    Client returns this in response to a ``tool-payload-warning``. The
    agent matches ``warning_id`` against the paused dispatch coroutine and
    either proceeds with the original / revised args or surfaces a
    cancellation error to the chat.

    Fields:

    - ``envelope_type`` — discriminator, literal ``"tool-payload-confirmation"``.
    - ``warning_id`` — matches the originating ``tool-payload-warning``.
    - ``decision`` — one of ``"proceed"`` / ``"cancel"`` / ``"narrow_scope"``.
    - ``revised_args`` — populated only when ``decision == "narrow_scope"``;
      carries the args the agent should dispatch with. Permissive ``dict``
      shape so the client can echo back the warning's
      ``alternative_args`` OR a user-edited variant. The agent service
      validates against the target tool signature before dispatch.

    Cross-shape rule (``_validate_decision_consistency``):

    - ``decision == "narrow_scope"`` ⇒ ``revised_args`` must be a non-None
      dict (may be empty). Otherwise the agent has nothing to dispatch with.
    - ``decision != "narrow_scope"`` ⇒ ``revised_args`` must be None. A
      lingering revised_args on a proceed/cancel response is a client bug
      we want to catch at the contract boundary, not at dispatch time.
    """

    MESSAGE_TYPE: ClassVar[str] = "tool-payload-confirmation"

    envelope_type: Literal["tool-payload-confirmation"] = "tool-payload-confirmation"
    warning_id: ULIDStr
    decision: PayloadConfirmationDecision
    revised_args: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _validate_decision_consistency(self) -> "PayloadConfirmationEnvelopePayload":
        """Enforce the decision/revised_args cross-field rule."""
        if self.decision == "narrow_scope":
            if self.revised_args is None:
                raise ValueError(
                    "decision='narrow_scope' requires revised_args (dict); "
                    "got None."
                )
        else:
            if self.revised_args is not None:
                raise ValueError(
                    f"decision={self.decision!r} forbids revised_args; "
                    f"got {self.revised_args!r}."
                )
        return self
