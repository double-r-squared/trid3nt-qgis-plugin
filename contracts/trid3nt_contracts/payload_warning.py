"""The payload-warning gate: a warning envelope and its confirmation.

A dispatch whose ESTIMATED payload exceeds the warning threshold pauses until a
confirmation carrying the same ``warning_id`` returns - no confirmation, no
dispatch. Past the hard cap the warning still goes out but ``proceed`` is
removed from the options, so the only ways forward are cancel or narrow.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import Field, field_validator, model_validator

from .common import GraceModel, InputBasis, SyntheticInput, ULIDStr

__all__ = [
    "PayloadWarningOption",
    "PayloadWarningEnvelopePayload",
    "PayloadConfirmationDecision",
    "PayloadConfirmationEnvelopePayload",
    "GranularitySuggestion",
    "ParamDoor",
    "ParamSheet",
    "ParamSheetRow",
    "TimeScaleSuggestion",
    "WARNING_THRESHOLD_MB_DEFAULT",
    "HARD_CAP_MB_DEFAULT",
]


#: Default warning threshold in megabytes, env-overridable per deployment. A
#: module constant rather than a contract field, so a call site with no env
#: still has a sensible default.
WARNING_THRESHOLD_MB_DEFAULT: float = 25.0

#: Default hard cap in megabytes, env-overridable per deployment. Above it
#: ``proceed`` is removed from the options entirely.
HARD_CAP_MB_DEFAULT: float = 250.0


#: The three actions a payload-warning gate can return.
#:
#: - ``proceed`` - dispatch with the originally proposed args. Removed from
#:   the options when the estimate exceeds the hard cap.
#: - ``cancel`` - abort. A typed failure surfaces and no consequence runs.
#: - ``narrow_scope`` - dispatch with the revised args the confirmation
#:   carries back.
PayloadWarningOption = Literal["proceed", "cancel", "narrow_scope"]


class GranularitySuggestion(GraceModel):
    """A pre-run GRANULARITY suggestion, optional on a payload warning.
    It makes resolution a USER LEVER rather than a silent auto-coarsen, showing
    the cost of resolution before the run. No number here is a price."""

    # The run this suggestion is for. A FETCH resolution choice uses the same
    # ladder as a solver mesh, so both are members here.
    engine: Literal["swmm", "sfincs", "dem", "topobathy", "landcover", "telemac"]
    #: The args key the chosen rung is written back under, VERBATIM.
    resolution_param: Literal[
        "target_resolution_m", "grid_resolution_m", "resolution_m",
        "mesh_resolution_m",
    ]
    #: The recommended cell size, and the default-selected rung.
    suggested_resolution_m: float = Field(gt=0.0)
    #: The ascending ladder of selectable cell sizes.
    resolution_choices: list[float] = Field(default_factory=list)
    #: Projected cells and wall-clock at the SUGGESTED resolution.
    estimated_active_cells: int = Field(ge=0)
    estimated_solve_seconds: float = Field(ge=0.0)
    vcpus: int = Field(gt=0)
    #: A FREE string, not a Literal, so a fetch gate can label its own tier
    #: without a contract change.
    compute_class: str = Field(min_length=1)
    #: The element cap honoured - the ceiling above which a suggestion coarsens.
    cell_cap: int = Field(gt=0)
    #: True when the suggestion is COARSER than what was asked for, because the
    #: request would have exceeded the cap. The honesty signal on this card.
    coarsened: bool
    reason: str = Field(max_length=512)
    #: An optional instance label for the readout. A capability descriptor, not
    #: a price.
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
        """Every rung must be a positive cell size - a zero or negative one
        renders an option nothing can select."""
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
        """A cell count is never negative."""
        if value < 0:
            raise ValueError(
                f"estimated_active_cells must be >= 0; got {value!r}"
            )
        return value

    @field_validator("estimated_solve_seconds")
    @classmethod
    def _validate_solve_seconds(cls, value: float) -> float:
        """A wall-clock estimate is never negative."""
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
    """A pre-run TIME-SCALE suggestion, optional on a payload warning.
    Cadence and window together fix the FRAME COUNT: too many balloon the
    payload, too few hide the motion. Absent when the cadence is fixed."""

    # A card carrying BOTH rows sends both overrides in ONE ``revised_args``,
    # so reviewing space and time is a single interaction.

    #: The args key a cadence edit is written back under, VERBATIM.
    cadence_param: Literal["output_interval_min"] = "output_interval_min"
    #: The recommended minutes per frame, and the default-prefilled value.
    suggested_interval_min: float = Field(gt=0.0)
    #: An OPTIONAL quick-pick ladder. The card also offers a free numeric edit,
    #: so a value off the ladder is allowed; empty means free-edit only.
    interval_choices: list[float] = Field(default_factory=list)
    #: The args key a window edit is written back under, VERBATIM.
    duration_param: Literal["duration_hr"] = "duration_hr"
    suggested_duration_hr: float = Field(gt=0.0)
    #: Projected frames at the suggested cadence and window. A client
    #: recomputes it live as the user edits, clamped to ``max_frames``.
    estimated_frame_count: int = Field(ge=1)
    #: The frame cap the recompute clamps to, so a readout never advertises an
    #: unbounded count.
    max_frames: int = Field(gt=0)
    #: The PHYSICAL floor on cadence: the deck re-floors here, so a finer edit
    #: cannot produce more frames than the deck emits.
    min_interval_min: float = Field(default=1.0, gt=0.0)
    #: Whether the run animates at a fine stride or a coarse one, so the card
    #: labels its cadence honestly.
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


#: Which resolution door a declared param came through, in the resolver's order.
#: The form card ranks its rows by this and folds ``constant`` under "advanced".
ParamDoor = Literal["user", "question", "derived", "scenario", "constant", "gate"]


class ParamSheetRow(GraceModel):
    """One row of the resolved param sheet a form card renders.
    Richer than a provenance line: an EDIT SURFACE needs the declaration too -
    what the value means, what it may become, and how loudly to warn."""

    #: The declared param name - the key an edit rides back under.
    name: str = Field(min_length=1)
    #: The resolved value. ``None`` means the row resolved to nothing: an
    #: optional param, or one still waiting on a gate.
    value: float | int | str | bool | list[Any] | None = None
    units: str | None = None
    #: The param's one-line declaration; the row LABEL.
    desc: str = Field(default="", max_length=512)
    door: ParamDoor
    basis: InputBasis
    #: WHERE the value came from, as one word of a closed set. A card renders it
    #: as a chip so a reader can override with confidence; nothing branches on
    #: it. Empty on a row that is not a filled slot.
    origin: Literal["template", "user", "model", "producer", "derived",
                    "calibrated"] | None = None
    #: The short phrase shown beside the value. RENDERED, never re-derived by a
    #: client from the basis and door.
    source_badge: str = Field(default="", max_length=200)
    #: The declared range. A card clamps its editor to it and the server
    #: re-clamps on submit: the form is an edit surface, not a way around the
    #: declaration.
    bounds: tuple[float, float] | None = None
    #: The declaration marks this derived or constant value as one the user is
    #: EXPECTED to override.
    user_lever: bool = False
    #: Whether an editor is offered. Editing a derived row is WARNED through
    #: the badge, not locked.
    editable: bool = True
    #: Render under the advanced fold: inspectable, but not the question.
    advanced: bool = False
    #: The heading the advanced fold sorts this row under. Empty on a sheet
    #: that groups nothing.
    group: str = Field(default="", max_length=120)
    #: The resolution note - a clamp, a derivation, a conflict.
    note: str | None = None

    @model_validator(mode="after")
    def _validate_bounds(self) -> "ParamSheetRow":
        """Inverted bounds would render an editor no value can satisfy."""
        if self.bounds is not None and self.bounds[0] > self.bounds[1]:
            raise ValueError(
                f"bounds {self.bounds!r} are inverted for row {self.name!r}"
            )
        return self


class ParamSheet(GraceModel):
    """The resolved sheet a step reviewing its own inputs presents.
    Rows arrive in RENDER order, owned by whoever owns the doors. A
    submit-with-edits IS the approval - the whole sheet was visible."""

    workflow: str = Field(min_length=1)
    title: str = Field(default="", max_length=200)
    rows: list[ParamSheetRow] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_unique_names(self) -> "ParamSheet":
        """Two rows for one param would render two editors writing one key."""
        names = [row.name for row in self.rows]
        if len(names) != len(set(names)):
            raise ValueError(f"param sheet rows must be unique; got {names!r}")
        return self


class PayloadWarningEnvelopePayload(GraceModel):
    """``tool-payload-warning``: the gate a heavy dispatch pauses on.
    Both numbers travel - the estimate AND the threshold it crossed - so why the
    gate fired is visible rather than narrated. No number here is a price."""

    MESSAGE_TYPE: ClassVar[str] = "tool-payload-warning"

    envelope_type: Literal["tool-payload-warning"] = "tool-payload-warning"
    #: Identifies the gate; the confirmation carries it back so the right
    #: paused dispatch is resumed.
    warning_id: ULIDStr
    tool_name: str = Field(min_length=1)
    #: The args intended for dispatch, sanitized, so the user can verify what
    #: is about to be fetched.
    tool_args: dict[str, Any] = Field(default_factory=dict)
    #: The projected size and the threshold it crossed. Both travel, so WHY the
    #: gate fired is visible without re-deriving it.
    estimated_mb: float = Field(ge=0.0)
    threshold_mb: float = Field(ge=0.0)
    recommendation: str = Field(max_length=512)
    #: Optional drafted narrowing, so a narrow can be one click. Permissive, so
    #: a smaller extent, fewer steps and fewer features all fit; it is
    #: round-tripped through the target signature before dispatch.
    alternative_args: dict[str, Any] | None = None
    #: A non-empty subset of the three actions. Past the hard cap ``proceed``
    #: is omitted HERE, so a client cannot offer it at all.
    options: list[PayloadWarningOption] = Field(
        default_factory=lambda: ["proceed", "cancel", "narrow_scope"],
        min_length=1,
        max_length=3,
    )
    #: Gate validity in seconds from the envelope stamp. On expiry the gate
    #: becomes a typed timeout failure, never a silent proceed.
    ttl_seconds: int = Field(default=300, ge=1)
    #: OPTIONAL run-settings rows. Present together, they render ONE combined
    #: card whose overrides ride back in a single ``revised_args``.
    granularity: GranularitySuggestion | None = None
    time_scale: TimeScaleSuggestion | None = None
    #: OPTIONAL resolved input-provenance table. Present, the envelope is an
    #: input REVIEW card: one row per resolved physical input, so what was
    #: fetched, interpreted or defaulted is reviewed BEFORE the run. On a
    #: proceed the run stamps exactly these entries into its result, so what was
    #: approved IS what ran.
    synthetic_inputs: list[SyntheticInput] | None = None
    #: OPTIONAL resolved param SHEET - the self-reviewing step's card. Present,
    #: it renders an editable grid instead of the plain provenance table, and a
    #: submit-with-edits is the approval.
    param_sheet: ParamSheet | None = None

    @model_validator(mode="after")
    def _validate_options_unique(self) -> "PayloadWarningEnvelopePayload":
        """A duplicated option would render two identical buttons."""
        if len(self.options) != len(set(self.options)):
            raise ValueError(
                f"options must be unique; got {self.options!r}"
            )
        return self


#: The user's selection, from the ``options`` the originating warning
#: advertised. A ``proceed`` the warning did not advertise is REFUSED on
#: receipt - the hard cap cannot be talked past by the reply.
PayloadConfirmationDecision = Literal["proceed", "cancel", "narrow_scope"]


class PayloadConfirmationEnvelopePayload(GraceModel):
    """``tool-payload-confirmation``: the reply that authorizes a paused gate.
    ``warning_id`` selects the paused dispatch; the decision then proceeds with
    the original or the revised args, or surfaces a cancellation.
    """

    MESSAGE_TYPE: ClassVar[str] = "tool-payload-confirmation"

    envelope_type: Literal["tool-payload-confirmation"] = "tool-payload-confirmation"
    warning_id: ULIDStr
    decision: PayloadConfirmationDecision
    #: The args to dispatch with, on a narrow. Permissive, so it can echo the
    #: warning's own draft or a user-edited variant; it is validated against
    #: the target signature before dispatch.
    revised_args: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _validate_decision_consistency(self) -> "PayloadConfirmationEnvelopePayload":
        """A narrow REQUIRES revised args - otherwise there is nothing to
        dispatch with - and every other decision FORBIDS them, so a lingering
        set is caught here rather than at dispatch."""
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
