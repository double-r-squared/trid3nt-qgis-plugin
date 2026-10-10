"""The initial state a run starts from: every node wet for a fresh run, or the last
instant and wet/dry field of the restart record a continued run carries on from.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..errors import TelemacError

__all__ = ["PREVIOUS_DEST", "initial_state_of"]

# The engine reads a file, not a URI: the previous run's restart record is staged under this name.
PREVIOUS_DEST = "previous.slf"


# The engine's perfect-restart record, under the name a body that keeps one writes it.
_RESTART = "restart_domain.slf"


# How a restart record names its depth, and the depth that counts as water a source can be released into.
_DEPTH_VARIABLE = ("WATER DEPTH", "HAUTEUR D'EAU", "HAUTEUR D EAU")


_WET_DEPTH_M = 0.01


def _continuation_state(uri: str, node_count: int) -> dict[str, Any]:
    """The restart record's last instant and depth field, read off the file.

    Taken only where its node count is this run's mesh's.
    """
    import tempfile

    import numpy as np

    from trid3nt_server.workflows.solver.solver import _download_object
    from trid3nt_server.workflows.telemac.modules.outputs import read_selafin

    with tempfile.TemporaryDirectory(prefix="telemac-continue-") as tmp:
        path = Path(tmp) / PREVIOUS_DEST
        _download_object(str(uri), path)
        record = read_selafin(path)
    nodes = int(record.get("npoin2") or record["npoin"])
    if nodes != node_count:
        raise TelemacError(
            f"the previous computation {uri} holds {nodes} nodes and this run's "
            f"mesh {node_count}: a state carries on only on the mesh it was "
            "computed on.", error_code="TELEMAC_CONTINUATION_REFUSED")
    if len(record["times"]) == 0:
        raise TelemacError(
            f"{uri} holds no time record, so there is no state to continue from "
            "and no instant to continue the scenario at. Fill the previous "
            f"computation with a completed run's {_RESTART}.",
            error_code="TELEMAC_CONTINUATION_UNREADABLE")
    depth = next((record["data"][name] for name in record["varnames"]
                  if name.strip().upper() in _DEPTH_VARIABLE), None)
    if depth is None:
        raise TelemacError(
            f"{uri} carries no water depth among {record['varnames']}, so the "
            "state this run would start from cannot say where it is wet.",
            error_code="TELEMAC_CONTINUATION_UNREADABLE")
    # Only the file knows where the continued leg stopped: the restart is written at the engine's own last step; that instant's depth decides where a release can land.
    start_s = float(record["times"][-1])
    wet = np.asarray(depth[-1], dtype=float) > _WET_DEPTH_M
    return {
        "start_s": start_s, "wet": wet,
        "note": (f"the restart record this run continues from, at t={start_s:.0f} s: "
                 f"{int(wet.sum())} of {record['npoin']} nodes clear the "
                 f"{_WET_DEPTH_M} m wet floor"),
    }


def initial_state_of(continue_from: str | None, node_count: int) -> dict[str, Any]:
    """What the run starts from: a fresh reach opens at the derived normal depth laid
    bed-parallel; a continued one at the restart record's wet/dry field and instant.
    """
    if continue_from:
        return _continuation_state(str(continue_from), node_count)
    return {"start_s": None, "wet": [True] * node_count,
            "note": "the template's own constant initial depth, the derived normal "
                    "depth laid bed-parallel over every node of the accepted mesh"}
