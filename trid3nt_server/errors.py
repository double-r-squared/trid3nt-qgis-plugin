"""The base of the typed-error tree: a failure carrying the code the emitter renders.

It lives at the package root because BOTH the input layer and the declarative
library raise over it, and the input layer sits below the library - an input
module that reached up into ``workflows`` would close an import cycle, since
importing any workflows module registers the TELEMAC solver and pulls the whole
tool registry back through the templates.
"""

from __future__ import annotations

__all__ = ["DeclarativeError"]


class DeclarativeError(RuntimeError):
    """Base for every declarative-library failure; carries a typed ``error_code``."""

    error_code = "DECLARATIVE_ERROR"

    def __init__(self, message: str, *, error_code: str | None = None) -> None:
        super().__init__(message)
        if error_code:
            self.error_code = error_code
