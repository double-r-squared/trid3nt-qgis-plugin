"""The base of the typed-error tree: a failure carrying the code the emitter renders.

It lives at the package root because the input layer sits below the library and both raise over it;
importing any workflows module registers the TELEMAC solver and would close an import cycle.
"""

from __future__ import annotations

__all__ = ["DeclarationError", "DeclarativeError", "GateRefusedError", "INPUT_NOT_USED", "unused_input_warning"]

#: The warning code for a handed input the engine's chosen option does not read; a warning is a run note that opens with its code.
INPUT_NOT_USED = "INPUT_NOT_USED"


def unused_input_warning(*, slot: str, option: str) -> str:
    """The run note for input ``slot`` handed and not used by the choice stated under keyword ``option``.

    Names only the slot and the keyword identifier, never a value a source or a person supplied."""
    return (f"{INPUT_NOT_USED}: the {slot} input was handed and is not used, because the option stated under {option} "
            f"does not read it. Change {option} to an option that reads it, or drop the {slot} input.")


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
