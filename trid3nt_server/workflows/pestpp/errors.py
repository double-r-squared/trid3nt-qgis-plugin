"""The engine's typed failures, each carrying the code the envelope renders.

A failure is named for what the run could not do - stage, solve, read - never
for the question that asked; every raiser states its own code."""

from __future__ import annotations

from trid3nt_server.workflows.runtime import DeclarativeError

__all__ = ["PestppError"]


class PestppError(DeclarativeError):
    """A PEST++ study could not be staged, run or read."""

    error_code = "PESTPP_FAILED"
