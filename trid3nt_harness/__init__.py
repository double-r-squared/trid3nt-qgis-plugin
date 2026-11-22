"""The control plane: the LLM adapters and turn guards, tool retrieval and the gate machine. It imports trid3nt only.

Importing it wires what it adds into the SDK: the gate every input review and draw asks, the fill's walk
down a ranked list for an input a run leaves unstated, strictness (a coverage shortfall no step resolves refuses),
and the tools only the model calls (search, find, fetch, the spatial-input ask) on the one registry."""

from trid3nt.inputs.coverage import register_strict as _register_strict
from trid3nt.inputs.review import register_gate as _register_gate
from trid3nt.workflows.runtime.fill import register_rank_pick as _register_rank_pick
from trid3nt_harness.inputs.gate.draw_input import gate_draw_input as _gate_draw_input
from trid3nt_harness.inputs.gate.input_review import gate_input_review as _gate_input_review

_register_gate(review=_gate_input_review, draw=_gate_draw_input)


def _by_rank(_choice: object) -> bool:
    # An input the run leaves unstated takes the match's top survivor, and the next where that one held nothing.
    return True


_register_rank_pick(_by_rank)
_register_strict(True)

from trid3nt_harness.tools.search.search_tools import search_tools  # noqa: E402,F401
from trid3nt_harness.tools.search.find_sources import find_sources  # noqa: E402,F401
from trid3nt_harness.tools.search.fetch import fetch  # noqa: E402,F401
from trid3nt_harness.inputs.gate import spatial_input_tool  # noqa: E402,F401
