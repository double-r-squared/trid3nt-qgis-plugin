"""The engine's typed failures, each carrying the code the envelope renders.

A failure is named for what the run could not do - acquire, settle, stage,
solve, read - never for the question that asked; every raiser states its own
code, and the two reach refusals carry a fixed message because nothing about
them varies from one run to the next."""

from __future__ import annotations

from trid3nt_server.workflows.runtime import DeclarativeError

__all__ = ["TelemacError"]


class TelemacError(DeclarativeError):
    """A TELEMAC run could not be acquired, settled, staged, solved or read."""

    error_code = "TELEMAC_FAILED"
