"""The engine's typed failures, each carrying the code the envelope renders.

A failure is named for what the run could not do - acquire, settle, stage,
solve, read - never for the question that asked; every raiser states its own
code, and the two reach refusals carry a fixed message because nothing about
them varies from one run to the next."""

from __future__ import annotations

from trid3nt_server.workflows.runtime import DeclarativeError

__all__ = ["ReachMeshUncovered", "ReachUnmapped", "TelemacError",
           "TelemacInputInvalid"]


class TelemacError(DeclarativeError):
    """A TELEMAC run could not be acquired, settled, staged, solved or read."""

    error_code = "TELEMAC_FAILED"


class TelemacInputInvalid(TelemacError):
    """An input the run cannot model: no AOI, or a malformed knob."""

    error_code = "TELEMAC_INPUT_INVALID"


class ReachUnmapped(TelemacError):
    """No mapped water polygon covers this reach, so it has NO DOMAIN. TERMINAL."""

    error_code = "REACH_WATER_UNMAPPED"

    def __init__(self) -> None:
        super().__init__(
            "No mapped water polygon covers this reach, so there is no domain to "
            "mesh. NHDArea maps water surfaces wide enough to have two banks; a "
            "narrow creek is a flowline only, and a flowline is a centreline "
            "rather than a shape. Draw or supply the reach polygon, name a case "
            "layer that holds it, or pick a reach with mapped water coverage. A "
            "stream this narrow may also be below the range where a 2D depth-"
            "averaged solve is the useful answer at all.")


class ReachMeshUncovered(TelemacError):
    """The accepted mesh holds NO part of the reach, so the solve has no reach.

    The only coverage outcome that stops a run: above zero it is journalled."""

    error_code = "REACH_MESH_UNCOVERED"

    def __init__(self) -> None:
        super().__init__(
            "The accepted mesh covers none of the reach centreline, so there is "
            "nothing of this river in the domain the solve would run over. Re-run "
            "at a finer mesh_resolution_m, declare a sizing function that resolves "
            "the channel, or supply your own mesh of the reach.")
