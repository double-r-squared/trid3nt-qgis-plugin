"""The TELEMAC style rows: how each published quantity is drawn.

A TELEMAC field is a dataset group on the mesh the run solved on, so each row is
a mesh row; what the quantity contributes to it is its ramp, its units, its
legend title and where the legend is ranged from, and a template declares the
row beside the read it publishes."""

from __future__ import annotations

__all__ = [
    "TELEMAC_DYE_STYLE",
    "TELEMAC_SEDIMENT_CONCENTRATION_STYLE",
    "TELEMAC_BED_EVOLUTION_STYLE",
    "TELEMAC_DO_STYLE",
    "TELEMAC_AGITATION_STYLE",
    "TELEMAC3D_STRATIFICATION_STYLE",
    "TELEMAC_MAX_DEPTH_STYLE",
]

# Nothing here is a preset NAME, so nothing here can drift from one: two fields
# differ because their PARAMETERS differ, which is what a reader sees on the
# canvas. ``center`` ranges a diverging ramp symmetrically about a value,
# ``floor`` pins the legend's bottom, and ``range`` caps its top at a percentile
# of the field (``p99.5``); all three are how the producer MEASURES the range
# while it holds the values, and what the layer carries is the range itself.

#: A TELEMAC-3D plane of a strictly positive field - temperature C, salinity
#: psu - on a sequential ramp; the caption names the variable.
TELEMAC3D_STRATIFICATION_STYLE: dict = {"kind": "mesh", "ramp": "viridis"}

#: The ARTEMIS agitation coefficient Kd = Hs/H0 - a dimensionless amplification
#: ratio, not a wave height. The legend is capped at the 99.5th percentile: the
#: standing wave against the open boundary sets the field's maximum, and a ramp
#: run to it paints the harbour interior one colour.
TELEMAC_AGITATION_STYLE: dict = {"kind": "mesh", "range": "p99.5"}

#: The dye-concentration field.
TELEMAC_DYE_STYLE: dict = {
    "kind": "mesh", "ramp": "reds", "units": "mg/L",
    "label": "Dye concentration"}

#: The GAIA SUSPENDED-SEDIMENT concentration field - a grain load, on its own
#: ramp so it never reads as the dissolved dye field a sediment run publishes
#: beside it.
TELEMAC_SEDIMENT_CONCENTRATION_STYLE: dict = {
    "kind": "mesh", "ramp": "oranges", "units": "mg/L",
    "label": "Suspended sediment concentration"}

#: The GAIA bed-evolution field, in the metres the module writes: deposition
#: positive, erosion negative, so the ramp diverges about zero and the legend is
#: ranged symmetrically about that centre.
TELEMAC_BED_EVOLUTION_STYLE: dict = {
    "kind": "mesh", "ramp": "rdbu", "units": "m", "label": "Bed evolution",
    "center": 0.0}

#: The MAX WATER DEPTH field: depth above ground, no datum, always positive,
#: on the wet-blue ramp an inundation field is read on.
TELEMAC_MAX_DEPTH_STYLE: dict = {
    "kind": "mesh", "ramp": "ylgnbu", "units": "m",
    "label": "Max water depth"}

#: The DISSOLVED-OXYGEN field from a WAQTEL O2 run. rdylbu, NOT reversed: low
#: oxygen reads red and high reads blue, which is the direction a deficit is
#: read in. The legend floors at zero so a standard in the low single digits
#: stays on the ramp beside a river that never fell below six.
TELEMAC_DO_STYLE: dict = {
    "kind": "mesh", "ramp": "rdylbu", "units": "mg/L",
    "label": "Dissolved oxygen", "floor": 0}
