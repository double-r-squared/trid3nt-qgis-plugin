"""The base of the typed-error tree: a failure carrying the code the emitter renders.

It lives at the package root because the input layer sits below the library and both raise over it;
importing any workflows module registers the TELEMAC solver and would close an import cycle.
"""

from __future__ import annotations

from typing import Mapping

__all__ = ["ACCURACY_NOT_CITED", "ACCURACY_UNIT_START", "PARAMETERS_AT_BOUNDS", "parameters_at_bounds_warning",
           "unit_start_warning", "READINGS_UNTABLED", "CLOUD_COVER_FROM_ASOS", "DeclarationError", "DeclarativeError", "GateRefusedError",
           "INPUT_NOT_USED", "accuracy_not_cited_warning", "cloud_cover_from_asos_warning", "unused_input_warning",
           "untabled_readings_note"]

#: The warning code for a handed input the engine's chosen option does not read; a warning is a run note that opens with its code.
INPUT_NOT_USED = "INPUT_NOT_USED"


def unused_input_warning(*, slot: str, option: str) -> str:
    """The run note for input ``slot`` handed and not used by the choice stated under keyword ``option``.

    Names only the slot and the keyword identifier, never a value a source or a person supplied."""
    return (f"{INPUT_NOT_USED}: the {slot} input was handed and is not used, because the option stated under {option} "
            f"does not read it. Change {option} to an option that reads it, or drop the {slot} input.")


#: The warning code for cloud cover read from the ASOS sky-cover report because the satellite source held none.
CLOUD_COVER_FROM_ASOS = "CLOUD_COVER_FROM_ASOS"


def cloud_cover_from_asos_warning() -> str:
    """The run note for cloud cover read from ASOS sky-cover categories in place of the GOES cloud mask.

    States the swap and the ceilometer's limit; names no station, value or source text."""
    return (f"{CLOUD_COVER_FROM_ASOS}: cloud cover is read from the ASOS sky-cover report, not from GOES, which held "
            "nothing for this point and window. The ASOS ceilometer reports no cloud above 12,000 ft, so its clear "
            "sky can hide high cloud, and each value is the midpoint of a reported percent-of-sky range. Use a "
            "window and point GOES covers, or state the cloud cover.")


#: The warning code for readings whose code the source's table holds no row for: they give no value, the record stands.
READINGS_UNTABLED = "READINGS_UNTABLED"


def untabled_readings_note(counts: dict[str, int]) -> str:
    """The run note counting, by member, the readings a record's table holds no row for; it states no code the record carried."""
    held = ", ".join(f"{name} {count}" for name, count in sorted(counts.items()))
    return (f"{READINGS_UNTABLED}: readings whose code the source's table holds no row for give no value at their "
            f"hour, and the record is read without them (by member: {held}).")


#: The warning code for calibration targets whose records cite no measurement accuracy.
ACCURACY_NOT_CITED = "ACCURACY_NOT_CITED"


def accuracy_not_cited_warning(*, count: int) -> str:
    """The run note for ``count`` targets weighed without a cited accuracy, in their own group.

    Names only the count and the group, never a site, value or source text."""
    return (f"{ACCURACY_NOT_CITED}: {count} targets carry no cited measurement accuracy, so they are weighed in their "
            "own group, not_cited, starting at the widest accuracy their source cites elsewhere (a floor), or at 1 in "
            "the run's unit where the source cites none; the widening after the fit sets their sigma. State sigma for "
            "not_cited, or use a source that cites its accuracy.")


#: The warning code for a group starting at sigma 1 in the run's unit beside groups weighed otherwise.
ACCURACY_UNIT_START = "ACCURACY_UNIT_START"


def unit_start_warning(*, count: int) -> str:
    """The run note for ``count`` targets started at sigma 1 in the run's unit in a study whose other targets are
    weighed by a cited accuracy, a floor or a stated sigma; names only the count."""
    return (f"{ACCURACY_UNIT_START}: {count} targets start at sigma 1 in the run's unit, and beside targets weighed "
            "otherwise that start sets their relative weight in the fit. State sigma for not_cited, or use a source "
            "that cites its accuracy.")


#: The warning code for calibrated parameters left at a bound, a symptom of a structural error or of too many parameters.
PARAMETERS_AT_BOUNDS = "PARAMETERS_AT_BOUNDS"


def parameters_at_bounds_warning(*, groups: Mapping[str, Mapping[str, int]]) -> str:
    """The run note for the calibrated parameters at a bound, per parameter group: its identifier and counts only."""
    counts = "; ".join(f"{group} {held['lower']} at the lower and {held['upper']} at the upper of {held['of']}"
                       for group, held in groups.items())
    return (f"{PARAMETERS_AT_BOUNDS}: the calibrated parameters stand at a bound ({counts}). A parameter held at its "
            "bound may be fitting something the model does not represent, or the study may tune more than its "
            "observations inform: check the model, tune fewer parameters, or widen the bound with a source.")


class DeclarativeError(RuntimeError):
    """Base for every declarative-library failure; carries a typed ``error_code``."""

    error_code = "DECLARATIVE_ERROR"

    def __init__(self, message: str, *, error_code: str | None = None) -> None:
        super().__init__(message)
        if error_code:
            self.error_code = error_code


class DeclarationError(DeclarativeError):
    error_code = "DECLARATION_INVALID"


class GateRefusedError(DeclarativeError):
    error_code = "GATE_INPUT_REQUIRED"
