"""The engine's typed failures, each carrying the code the envelope renders.

A failure is named for what the run could not do - stage, solve, read - never
for the question that asked; every raiser states its own code."""

from __future__ import annotations

from trid3nt_server.workflows.runtime import DeclarativeError

__all__ = ["ModflowError"]


class ModflowError(DeclarativeError):
    """A MODFLOW run could not be staged, solved or read."""

    error_code = "MODFLOW_FAILED"
