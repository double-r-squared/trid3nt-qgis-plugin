"""Seat a whole declared sheet off a supplied mapping, the way a fill seats each row."""

from __future__ import annotations

from typing import Any, Mapping

from trid3nt_server.workflows.runtime.params import (
    ResolvedParams,
    param_rows,
    refuse_duplicate_params,
)
from trid3nt_server.workflows.runtime.resolver import seat_param


#: The inputs a TELEMAC template carries without restating them, in the order its
#: sheet reads them: the mesh ask's edge, the engine's moment, the bed's frame.
SEATED = ("mesh_resolution_m", "event_time", "vertical_frame")


async def resolve_params(declared: Any, supplied: Mapping[str, Any]) -> ResolvedParams:
    declared = param_rows(declared)
    refuse_duplicate_params(declared)
    return ResolvedParams({param.name: seat_param(param, supplied.get(param.name))
                           for param in declared})
