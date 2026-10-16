"""A template's values read their schema off the module input JSON its STEERING drives.

The registered workflow hydrates its body at import, so both the wire and the
template's own class carry the schema the JSON states.
"""

from __future__ import annotations

import json

from trid3nt_server.workflows.telemac.modules.module import module_input_dir
from trid3nt_server.workflows.telemac.templates.dye_release.dye_release import (
    INPUTS,
    telemac_dye_release,
)


def _workflow():
    return telemac_dye_release.workflow


def test_the_registered_workflow_hydrates_its_values():
    spill = _workflow().input("spill_fraction")
    assert (spill.default, spill.bounds, spill.units) == (0.25, (0.05, 0.9), None)
    assert spill.desc.startswith("Along-domain release position")


def test_the_template_body_itself_carries_the_schema_after_registration():
    assert INPUTS.spill_fraction.desc.startswith("Along-domain release position")
    assert INPUTS.release.type.__name__ == "Point"


def test_the_schema_source_is_the_steering_modules_dictionary():
    directory, module = _workflow().input_schema()
    assert (module, directory) == ("telemac2d", module_input_dir())
    doc = json.loads((directory / f"{module}.json").read_text())
    section = doc["inputs"]["telemac_dye_release"]
    assert "spill_fraction" in section and "release" in section
