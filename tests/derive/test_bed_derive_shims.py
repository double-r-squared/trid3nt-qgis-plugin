"""The bed derives are SHIMS: the rules they name are the bed slot's own.

Declared plan steps still call the tool, so the registration stands; what it
calls is ``inputs.bed.survey_surface``. Nothing here re-tests the rule - it is
tested at the seam that owns it."""

from __future__ import annotations

import inspect

from trid3nt_server.inputs.bed import SURVEY_DERIVE
from trid3nt_server.tools import TOOL_REGISTRY


def test_the_survey_grid_is_registered_and_calls_the_bed_seam():
    entry = TOOL_REGISTRY[SURVEY_DERIVE]
    assert entry.fn.__name__ == "derive_survey_surface"
    assert entry.metadata.cacheable is False
    assert "survey_surface(" in inspect.getsource(entry.fn)
