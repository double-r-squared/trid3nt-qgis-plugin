"""A render whose frames hold no field is a gap, not a deliverable.

The packet judge reads the render report the animation renderer writes: a figure
whose every frame was masked to nothing, or whose still is cut from a frame that
was, shows a legend over an empty domain. It is refused with its reason, because
a present file and a present legend are exactly what let it pass before.
"""

from __future__ import annotations

import functools
import importlib.util
import sys
from pathlib import Path

import pytest

DEV = Path(__file__).resolve().parents[2] / "dev"
SCRIPT = DEV / "packet" / "assemble_proof_packet.py"

if not DEV.is_dir():
    pytest.skip("dev/ is absent: the dev tools are not on the remote",
                allow_module_level=True)

#: The two report entries a reach eutrophication run wrote, captured verbatim off
#: its packet: one animation of a field that ran wet the whole record, and one of
#: the oxygen the renderer masked to nothing in every frame but the transient.
#: The oxygen entry is the render that PASSED and is the reason the guard exists.
BIOMASS = {"frames": 180, "animated": True, "variable": "PHYTO BIOMASS",
           "peak_frame": 179, "peak_value": 2.0913689136505127,
           "dry_frames": [], "vmin": 0.0, "vmax": 2.09222}
OXYGEN = {"frames": 180, "animated": True, "variable": "DISSOLVED O2",
          "peak_frame": 179, "peak_value": float("nan"),
          "dry_frames": [i for i in range(180) if i != 2],
          "vmin": 0.0, "vmax": 664.6969}


@functools.lru_cache(maxsize=1)
def _packet_module():
    """The assembler, imported by path - ``dev/packet/`` is not a package."""
    spec = importlib.util.spec_from_file_location("assemble_proof_packet", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("assemble_proof_packet", module)
    spec.loader.exec_module(module)
    return module


def test_a_render_that_held_water_is_not_a_gap():
    assert _packet_module().all_dry(BIOMASS) is None


def test_a_still_cut_from_a_dry_frame_is_refused_with_its_variable():
    reason = _packet_module().all_dry(OXYGEN)
    assert reason is not None
    assert reason.startswith("DISSOLVED O2:") and "dry" in reason
    assert "179 of 180" in reason, "the reason says how much of the record is dry"


def test_a_record_dry_end_to_end_says_every_frame():
    entry = OXYGEN | {"dry_frames": list(range(180))}
    assert _packet_module().all_dry(entry) == (
        "DISSOLVED O2: every frame dry - no finite value inside the mask")


def test_a_report_with_nothing_measured_makes_no_claim():
    """No frames is no reading, and a gap nobody measured is not a finding."""
    assert _packet_module().all_dry({"frames": 0, "variable": "X"}) is None
