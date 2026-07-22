"""CI guard: every engine OUTPUT_QUANTITIES style_preset must resolve.

engine-coverage-levers STEP 3. A ``OutputQuantitySpec.style_preset`` that is NOT
registered in ``publish_layer._TITILER_STYLE_REGISTRY`` silently falls through to
a band-stats percentile rescale -> a physically-WRONG colormap (e.g. a diverging
seepage field rendered with a sequential ramp, hiding the sign). This guard fails
the build the moment a spec references an unregistered preset, so a new published
quantity cannot ship with a mis-resolving (or absent) colormap.

The check is the EXACT-KEY leg of the resolver (``_TITILER_STYLE_REGISTRY``):
every published quantity must pin a deterministic colormap, never rely on the
substring/family fallback or the percentile default.
"""

from __future__ import annotations

import pytest

from grace2_contracts.output_quantities import OUTPUT_QUANTITIES
from grace2_agent.tools.publish_layer import _TITILER_STYLE_REGISTRY


def _all_specs():
    for engine, specs in OUTPUT_QUANTITIES.items():
        for spec in specs:
            yield engine, spec


def test_every_output_quantity_style_preset_is_registered() -> None:
    """Every engine OUTPUT_QUANTITIES spec pins a REGISTERED style_preset."""
    missing: list[str] = []
    for engine, spec in _all_specs():
        if spec.style_preset not in _TITILER_STYLE_REGISTRY:
            missing.append(f"{engine}.{spec.quantity_id} -> {spec.style_preset!r}")
    assert not missing, (
        "OutputQuantitySpec.style_preset(s) NOT in publish_layer."
        "_TITILER_STYLE_REGISTRY (would render a physically-wrong colormap):\n  "
        + "\n  ".join(missing)
    )


def test_registry_entries_are_well_formed() -> None:
    """Each referenced registry entry is a (rescale "lo,hi", colormap) pair."""
    referenced = {spec.style_preset for _engine, spec in _all_specs()}
    for preset in referenced:
        rescale, cmap = _TITILER_STYLE_REGISTRY[preset]
        lo, hi = rescale.split(",")
        assert float(hi) > float(lo), f"{preset}: rescale hi<=lo ({rescale})"
        assert cmap, f"{preset}: empty colormap"


def test_step3_engines_have_new_quantities() -> None:
    """The four STEP-3 engines carry at least one default_on (new) quantity."""
    for engine in ("modflow", "landlab", "openquake", "swmm"):
        specs = OUTPUT_QUANTITIES[engine]
        assert specs, f"{engine} OUTPUT_QUANTITIES is empty"
        assert any(s.default_on for s in specs), (
            f"{engine} has no default_on (new) published quantity"
        )
