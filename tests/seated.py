"""Answer a whole declared body off a supplied mapping, the way a fill answers each value."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Mapping

from trid3nt_server.inputs.input import declared as body_of
from trid3nt_server.errors import GateRefusedError
from trid3nt_server.workflows.runtime.fill import REJECTED, Fill, Verdict, _answer


#: The inputs a TELEMAC template carries without restating them, in the order its
#: sheet reads them: the mesh ask's edge, the engine's moment, the bed's frame.
SEATED = ("mesh_resolution_m", "event_time", "vertical_frame")


async def resolve_params(declared: Any, supplied: Mapping[str, Any]) -> dict[str, Verdict]:
    """The verdict of every value input, by name; a refused one raises its own refusal."""
    rows = [row for row in body_of(declared) if not row.is_row]
    state = Fill(workflow=SimpleNamespace(inputs=tuple(rows)))
    for row in rows:
        await _answer(state, row.name, row, supplied.get(row.name))
        if getattr(state.inputs.get(row.name), "state", "") == REJECTED:
            raise GateRefusedError(state.inputs[row.name].reason)
    return state.inputs


def values_of(answered: Mapping[str, Verdict], declared: Any) -> dict[str, Any]:
    """What a run's mapping holds for every value input: the value taken, or none."""
    return {row.name: getattr(answered.get(row.name), "value", None)
            for row in body_of(declared) if not row.is_row}


def value_inputs(body: Any) -> tuple[Any, ...]:
    """The value inputs of a workflow's one body, or of a body itself, in body order."""
    return tuple(row for row in body_of(getattr(body, "inputs", body))
                 if not row.is_row)


def row_inputs(body: Any) -> tuple[Any, ...]:
    """The rows of a workflow's one body, or of a body itself, in body order."""
    return tuple(row for row in body_of(getattr(body, "inputs", body)) if row.is_row)
