"""The payload-warning gate: a warning envelope and its confirmation.

A dispatch whose ESTIMATED payload exceeds the warning threshold pauses until a
confirmation carrying the same ``warning_id`` returns - no confirmation, no
dispatch. Past the hard cap the warning still goes out but ``proceed`` is
removed from the options, so the only ways forward are cancel or narrow.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import Field, field_validator, model_validator

from .common import ContractModel, InputBasis, SyntheticInput, ULIDStr
from .coverage import SourceChoice

__all__ = [
    "PayloadWarningOption",
    "PayloadWarningEnvelopePayload",
    "PayloadConfirmationDecision",
    "PayloadConfirmationEnvelopePayload",
    "GranularitySuggestion",
    "ParamDoor",
    "ParamOption",
    "ParamSheet",
    "ParamSheetRow",
    "TimeScaleSuggestion",
    "WARNING_THRESHOLD_MB_DEFAULT",
    "HARD_CAP_MB_DEFAULT",
]


#: Default warning threshold in megabytes, env-overridable per deployment.
WARNING_THRESHOLD_MB_DEFAULT: float = 25.0

#: Default hard cap in megabytes, env-overridable; above it ``proceed`` leaves the options.
HARD_CAP_MB_DEFAULT: float = 250.0


#: ``proceed`` is removed from the options above the hard cap; ``narrow_scope`` dispatches with the
#: revised args the confirmation carries back.
PayloadWarningOption = Literal["proceed", "cancel", "narrow_scope"]


class GranularitySuggestion(ContractModel):
    """A pre-run GRANULARITY suggestion, optional on a payload warning.
    It makes resolution a USER LEVER rather than a silent auto-coarsen, showing
    the cost of resolution before the run. No number here is a price."""

    # A fetch resolution choice uses the same ladder as a solver mesh.
    engine: Literal["swmm", "sfincs", "dem", "topobathy", "landcover", "telemac"]
    #: The args key the chosen rung is written back under, VERBATIM.
    resolution_param: Literal[
        "target_resolution_m", "grid_resolution_m", "resolution_m",
        "mesh_resolution_m",
    ]
    suggested_resolution_m: float = Field(gt=0.0)
    resolution_choices: list[float] = Field(default_factory=list)
    estimated_active_cells: int = Field(ge=0)
    estimated_solve_seconds: float = Field(ge=0.0)
    #: A count, never a size name.
    vcpus: int = Field(gt=0)
    cell_cap: int = Field(gt=0)
    #: True when coarser than asked because the request would have exceeded the cap.
    coarsened: bool
    reason: str = Field(max_length=512)
    #: A capability descriptor, not a price.
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
        """A solve runs on at least one core."""
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


class TimeScaleSuggestion(ContractModel):
    """A pre-run TIME-SCALE suggestion, optional on a payload warning.
    Cadence and window together fix the FRAME COUNT: too many balloon the
    payload, too few hide the motion. Absent when the cadence is fixed.

    The cadence itself is READ-ONLY here: a run writes its frames on its own
    module's printout-period keyword, which a caller overrides by that keyword's
    name, so this block carries no args key for it."""

    # A card carrying both rows sends both overrides in one ``revised_args``.

    suggested_interval_min: float = Field(gt=0.0)
    #: Optional quick-pick ladder; the card also offers a free numeric edit, so empty means free-edit only.
    interval_choices: list[float] = Field(default_factory=list)
    #: The args key a window edit is written back under, VERBATIM.
    duration_param: Literal["duration_hr"] = "duration_hr"
    suggested_duration_hr: float = Field(gt=0.0)
    #: Projected frames; a client recomputes it live, clamped to ``max_frames``.
    estimated_frame_count: int = Field(ge=1)
    max_frames: int = Field(gt=0)
    #: Physical floor on cadence; a finer edit cannot produce more frames than the deck emits.
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


#: Which resolution door a declared param came through, in the resolver's order; the form card folds ``constant`` under advanced.
ParamDoor = Literal["user", "question", "derived", "scenario", "constant", "gate"]


class ParamOption(ContractModel):
    """One value an input would ACCEPT: what a pick sends back, and what the
    dropdown shows. A case layer is sent by its id and shown by its name."""

    value: str | int | float | bool
    label: str = Field(min_length=1, max_length=200)


class ParamSheetRow(ContractModel):
    """One row of the resolved param sheet a form card renders.
    Richer than a provenance line: an EDIT SURFACE needs the declaration too -
    what the value means, what it may become, and how loudly to warn."""

    name: str = Field(min_length=1)
    #: None means the row resolved to nothing: an optional param or one waiting on a gate.
    value: float | int | str | bool | list[Any] | None = None
    units: str | None = None
    desc: str = Field(default="", max_length=512)
    door: ParamDoor
    basis: InputBasis
    #: Where the value came from, one word of a closed set; nothing branches on it.
    origin: Literal["template", "user", "model", "producer", "derived",
                    "calibrated"] | None = None
    #: Rendered, never re-derived by a client from the basis and door.
    source_badge: str = Field(default="", max_length=200)
    #: The declared range; a card clamps to it and the server re-clamps on submit.
    bounds: tuple[float, float] | None = None
    user_lever: bool = False
    #: Editing a derived row is warned through the badge, not locked.
    editable: bool = True
    advanced: bool = False
    group: str = Field(default="", max_length=120)
    note: str | None = None
    #: The ranked list behind a data slot the match filled; None on every other row.
    choices: SourceChoice | None = None
    #: Exactly the values this input accepts, as the dropdown lists them; None on a typed row.
    options: list[ParamOption] | None = None

    @model_validator(mode="after")
    def _validate_bounds(self) -> "ParamSheetRow":
        """Inverted bounds would render an editor no value can satisfy."""
        if self.bounds is not None and self.bounds[0] > self.bounds[1]:
            raise ValueError(
                f"bounds {self.bounds!r} are inverted for row {self.name!r}"
            )
        return self


class ParamSheet(ContractModel):
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


class PayloadWarningEnvelopePayload(ContractModel):
    """``tool-payload-warning``: the gate a heavy dispatch pauses on.
    Both numbers travel - the estimate AND the threshold it crossed - so why the
    gate fired is visible rather than narrated. No number here is a price."""

    MESSAGE_TYPE: ClassVar[str] = "tool-payload-warning"

    envelope_type: Literal["tool-payload-warning"] = "tool-payload-warning"
    #: The confirmation carries it back so the right paused dispatch resumes.
    warning_id: ULIDStr
    tool_name: str = Field(min_length=1)
    tool_args: dict[str, Any] = Field(default_factory=dict)
    #: The projected size and the threshold it crossed, so why the gate fired is visible.
    estimated_mb: float = Field(ge=0.0)
    threshold_mb: float = Field(ge=0.0)
    recommendation: str = Field(max_length=512)
    #: Drafted narrowing; permissive, and round-tripped through the target signature before dispatch.
    alternative_args: dict[str, Any] | None = None
    #: Past the hard cap ``proceed`` is omitted here, so a client cannot offer it.
    options: list[PayloadWarningOption] = Field(
        default_factory=lambda: ["proceed", "cancel", "narrow_scope"],
        min_length=1,
        max_length=3,
    )
    #: Gate validity in seconds from the envelope stamp; expiry is a typed timeout, never a silent proceed.
    ttl_seconds: int = Field(default=300, ge=1)
    #: Present together, they render one combined card whose overrides ride back in a single ``revised_args``.
    granularity: GranularitySuggestion | None = None
    time_scale: TimeScaleSuggestion | None = None
    #: Present, the envelope is an input review card; on proceed the run stamps exactly these entries into its result.
    synthetic_inputs: list[SyntheticInput] | None = None
    #: Present, renders an editable grid instead of the provenance table; a submit-with-edits is the approval.
    param_sheet: ParamSheet | None = None

    @model_validator(mode="after")
    def _validate_options_unique(self) -> "PayloadWarningEnvelopePayload":
        """A duplicated option would render two identical buttons."""
        if len(self.options) != len(set(self.options)):
            raise ValueError(
                f"options must be unique; got {self.options!r}"
            )
        return self


#: A ``proceed`` the warning did not advertise is refused on receipt.
PayloadConfirmationDecision = Literal["proceed", "cancel", "narrow_scope"]


class PayloadConfirmationEnvelopePayload(ContractModel):
    """``tool-payload-confirmation``: the reply that authorizes a paused gate.
    ``warning_id`` selects the paused dispatch; the decision then proceeds with
    the original or the revised args, or surfaces a cancellation.
    """

    MESSAGE_TYPE: ClassVar[str] = "tool-payload-confirmation"

    envelope_type: Literal["tool-payload-confirmation"] = "tool-payload-confirmation"
    warning_id: ULIDStr
    decision: PayloadConfirmationDecision
    #: Permissive; validated against the target signature before dispatch.
    revised_args: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _validate_decision_consistency(self) -> "PayloadConfirmationEnvelopePayload":
        """A narrow requires revised args; every other decision forbids them."""
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
