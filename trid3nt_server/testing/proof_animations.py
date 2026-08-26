"""WHICH field a template's delivered animation paints - declared, never inferred.

A time-stepped solve writes half a dozen variables and only one of them is the
ANSWER. Coastal is the case that proves it: ``res_coastal.slf`` carries WATER
DEPTH and FREE SURFACE, and depth over a tidal bay is bathymetry-dominated - the
deep channel stays deep, the shallows stay shallow, and a six-hour surge moves the
picture almost not at all. The surge lives in the free surface. But FREE SURFACE
on a DRY node is the bed elevation (TELEMAC sets it that way), so painting it
unmasked scales the whole field by the highest hill in the domain and the tide
reads as a flat wash. The masked pair is the answer: FREE SURFACE where WATER
DEPTH > 0.02 m, which is exactly the ``WET_TOL`` discriminant the coastal worker's
own ``peak_wl_max_m`` / ``final_wl_max_m`` scalars are computed on.

That choice used to live in ``--var`` / ``--mask-var`` flags a person typed, which
means it lived in whoever remembered to type them. It lives HERE now, beside the
canary declarations, for the same reason the canaries' locations and windows do: a
delivered proof that came off a remembered command line is not repeatable, and a
mechanical re-render that fell back to a default variable delivered the wrong
picture with every check passing.

THERE IS NO DEFAULT. A time-stepped template with no entry in
:data:`PROOF_ANIMATIONS` REFUSES to render an animation - the packet assembler
reports the absent declaration as a named gap. Guessing a variable is how the
regression happened; refusing is the fix.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["PROOF_ANIMATIONS", "ProofAnimation"]

#: The coastal worker's own wet-node discriminant
#: (``workers/telemac/telemac_coastal_build.py``): a node counts as wet at
#: 0.02 m of depth. The animation masks on the SAME number its scalars do, so the
#: picture and ``peak_wl_max_m`` cannot disagree about which nodes hold water.
WET_TOL_M = 0.02


@dataclass(frozen=True)
class ProofAnimation:
    """One template's animation declaration: the field, the mask, the still, why.

    ``variable`` and ``mask_var`` are TOKENS matched against the SELAFIN's own
    padded 32-char names, so a token that stops matching refuses loudly rather
    than animating a neighbouring variable.

    ``still`` is ``peak`` for a field that BUILDS toward its answer (a rising
    tide, an arriving plume) and ``final`` for one that DECAYS toward it (a
    settling sag, a cooling column), where the peak frame is the initial
    condition and shows the reader nothing the run did.

    ``exempt_reason`` is the other half of the declaration: a solver with no
    simulation clock states WHY it owes no animation, so the exemption is a
    physics fact in the packet rather than a hole nobody noticed.
    """

    variable: str | None = None
    units: str = ""
    #: The published quantity this field is, so the animation resolves the SAME
    #: style-contract row - ramp, range and legend sentence - as the published
    #: raster of that quantity.
    quantity: str | None = None
    mask_var: str | None = None
    mask_threshold: float = 0.0
    still: str = "peak"
    #: Which horizontal plane of a 3D PRISM result to paint.
    plane: str = "surface"
    reason: str = ""
    exempt_reason: str | None = None


#: Keyed by TOOL, because a tool name is what an evidence JSON records.
PROOF_ANIMATIONS: dict[str, ProofAnimation] = {
    # quantity is deliberately UNSET. The style contract has no water-surface-
    # elevation row, and borrowing ``flood_depth``'s would put the published
    # inundation raster's label and ramp on a field that is not that quantity -
    # a quiet mislabel in exchange for a prettier colour. The neutral ramp,
    # scaled p2-p98 over the run, is the honest resolution until the contract
    # gains a water_level row.
    "coastal_tidal_surge": ProofAnimation(
        variable="FREE SURFACE", units="m", quantity=None,
        mask_var="WATER DEPTH", mask_threshold=WET_TOL_M, still="peak",
        reason="depth is bathymetry-dominated; the surge lives in the free "
               "surface. Masked to wet nodes because TELEMAC sets FREE SURFACE = "
               "BOTTOM on dry land, so an unmasked field is scaled by the highest "
               "hill in the domain and the tide reads as a flat wash."),
    # A dry-start watershed is the opposite case: the published answer IS max
    # water depth (overland sheet flow), and free surface over a hillslope is
    # terrain. The wet mask keeps unrained hillside out of the colour scale.
    "telemac_rain_on_grid": ProofAnimation(
        variable="WATER DEPTH", units="m", quantity="flood_depth",
        mask_var="WATER DEPTH", mask_threshold=WET_TOL_M, still="peak",
        reason="the question is overland sheet flow on a dry-start catchment, so "
               "the answer is the depth itself; free surface on a hillslope is "
               "terrain."),
    "telemac_river_dye": ProofAnimation(
        variable="DYE", units="mg/L", quantity="dye_concentration",
        mask_var="WATER DEPTH", mask_threshold=WET_TOL_M, still="peak",
        reason="the plume IS the tracer concentration; it BUILDS as the dye "
               "arrives, so the peak frame is the answer."),
    # The contract has no dissolved-oxygen row either, so no quantity is claimed.
    "telemac_do_sag": ProofAnimation(
        variable="DISSOLVED O2", units="mgO2/l", quantity=None,
        mask_var="WATER DEPTH", mask_threshold=WET_TOL_M, still="final",
        reason="the sag is the oxygen deficit downstream of the outfall. It "
               "DECAYS toward its answer, so the still is the final frame - the "
               "peak frame is the un-depleted initial condition."),
    "tomawac_wave_field": ProofAnimation(
        variable="WAVE HEIGHT HM0", units="m", quantity="wave_height",
        mask_var="WATER DEPTH", mask_threshold=WET_TOL_M, still="peak",
        reason="the significant wave height is the sea state the run reports; it "
               "GROWS with fetch and duration, so the peak frame is the answer."),
    # A 3D prism result carries no WATER DEPTH to mask on - every node of the
    # column is in the water by construction.
    "telemac3d_stratified_flow": ProofAnimation(
        variable="TEMPERATURE", units="degC", quantity="temperature",
        still="final", plane="surface",
        reason="the thermocline question is answered by the temperature field, "
               "and the surface plane is the half that the wind (or its absence) "
               "acts on. It SETTLES toward its answer, so the still is final."),
    "artemis_harbor_agitation": ProofAnimation(
        exempt_reason="ARTEMIS is the phase-resolving elliptic mild-slope "
                      "(Berkhoff) solver: it solves a boundary-value problem for "
                      "a single monochromatic sea state and returns ONE field, "
                      "the steady agitation coefficient Kd. The deck has no "
                      "simulation clock at all, which is why the run records no "
                      "ntimestep - there is no time evolution to animate, and "
                      "the single field IS the whole answer.",
        variable="WAVE HEIGHT", units="m", still="peak",
        reason="the steady wave height field, rendered as the run's one still."),
}
