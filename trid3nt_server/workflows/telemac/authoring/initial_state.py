"""THE INITIAL STATE a run starts from: every node wet for a fresh run, or the
last instant and wet/dry field of the restart record a continued run carries on
from, read off that record by the one reader of the format."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..errors import TelemacError

__all__ = ["PREVIOUS_DEST", "initial_state_of"]

#: What a continued run's PREVIOUS COMPUTATION FILE is called in the run
#: directory. The engine reads a file, not a URI, so the previous run's restart
#: record is staged under one name and the steering file names that.
PREVIOUS_DEST = "previous.slf"


#: The engine's perfect-restart record, under the name a body that keeps one
#: writes it.
_RESTART = "restart_domain.slf"


#: How a restart record names its depth, and the depth a node has to hold at
#: that instant to count as water a source can be released into.
_DEPTH_VARIABLE = ("WATER DEPTH", "HAUTEUR D'EAU", "HAUTEUR D EAU")


_WET_DEPTH_M = 0.01


def _continuation_state(uri: str) -> dict[str, Any]:
    """The restart record's own last instant and depth field - read off the file.

    A continued run is the same declared scenario over an extended horizon."""
    import tempfile

    import numpy as np

    from trid3nt_server.workflows.solver.solver import _download_object
    from trid3nt_server.workflows.telemac.modules.outputs import read_selafin

    with tempfile.TemporaryDirectory(prefix="telemac-continue-") as tmp:
        path = Path(tmp) / PREVIOUS_DEST
        _download_object(str(uri), path)
        record = read_selafin(path)
    if len(record["times"]) == 0:
        raise TelemacError(
            f"{uri} holds no time record, so there is no state to continue from "
            "and no instant to continue the scenario at. Point continue_from at "
            f"a completed run's {_RESTART}.",
            error_code="TELEMAC_CONTINUATION_UNREADABLE")
    depth = next((record["data"][name] for name in record["varnames"]
                  if name.strip().upper() in _DEPTH_VARIABLE), None)
    if depth is None:
        raise TelemacError(
            f"{uri} carries no water depth among {record['varnames']}, so the "
            "state this run would start from cannot say where it is wet.",
            error_code="TELEMAC_CONTINUATION_UNREADABLE")
    # Only the file can say where the continued leg stopped: the engine writes the
    # restart at its own last time step, which is not the graphic period, not the
    # asked duration, and not anything the server can compute from the ask. The
    # depth at that instant is the initial state, which decides where a release
    # can land.
    start_s = float(record["times"][-1])
    wet = np.asarray(depth[-1], dtype=float) > _WET_DEPTH_M
    return {
        "start_s": start_s, "wet": wet,
        "note": (f"the restart record this run continues from, at t={start_s:.0f} s: "
                 f"{int(wet.sum())} of {record['npoin']} nodes clear the "
                 f"{_WET_DEPTH_M} m wet floor"),
    }


def initial_state_of(continue_from: str | None, node_count: int) -> dict[str, Any]:
    """WHAT THE RUN STARTS FROM: a fresh reach opens at the derived normal depth
    laid bed-parallel, a positive depth at every node; a CONTINUED one opens at
    the restart record's own wet/dry field and the instant it stands at."""
    if continue_from:
        return _continuation_state(str(continue_from))
    return {"start_s": None, "wet": [True] * node_count,
            "note": "the deck's own constant initial depth, the derived normal "
                    "depth laid bed-parallel over every node of the accepted mesh"}
