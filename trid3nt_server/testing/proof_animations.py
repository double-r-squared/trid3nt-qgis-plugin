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

That choice lives HERE, beside the canary declarations, and never on a command
line, for the same reason the canaries' locations and windows do: a delivered
proof that came off a remembered flag is not repeatable, and a mechanical
re-render that falls back to a default variable delivers the wrong picture with
every check passing.

THERE IS NO DEFAULT. A time-stepped template with no entry in
:data:`PROOF_ANIMATIONS` REFUSES to render an animation - the packet assembler
reports the absent declaration as a named gap. Guessing a variable is how the
regression happened; refusing is the fix.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["PACKET_NOTES", "PROOF_ANIMATIONS", "ProofAnimation",
           "animations_for", "packet_notes", "suffixed"]

#: The wet-node discriminant every TELEMAC worker applies: a node counts as wet
#: at 0.02 m of depth. The animation masks on the SAME number its scalars do, so
#: the picture and the run scalars cannot disagree about which nodes hold water.
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

    ``name`` exists because ONE run can owe more than one animation. A coastal
    solve answers two different questions off the same SELAFIN - how the water
    surface moves, and where it went onto land - and they are not two renderings
    of one picture: they take different variables, different masks and different
    scales. A template declares each, the packet requires all of them, and the
    name lands in the filename so a reader is never guessing which is which.

    ``dry_land_only`` is the INUNDATION gate: keep only nodes that were DRY at
    t=0 (bed above the run's initial water line) so permanently submerged bay
    floor is nodata rather than colour. It is the same discriminant
    ``flooded_land_km2`` counts on, and without it the bathymetry dominates the
    scale and "inundation" paints the sea.
    """

    name: str = "default"
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
    dry_land_only: bool = False
    #: A field the SELAFIN does not store, built from the components it does.
    #: ``("VELOCITY U", "VELOCITY V")`` is the vector magnitude; the solver writes
    #: components and the QUESTION is the speed.
    derived: tuple[str, ...] = ()
    #: Draw the vector FIELD over the colour field, off the same two components
    #: ``derived`` builds the magnitude from. ``"streamlines"`` traces the flow
    #: DIRECTION as a continuous line, one arrowhead per trace; ``"quiver"``
    #: draws one arrow per grid cell instead - a discrete read that a coarse
    #: grid or a single frozen frame carries more calmly than a trace does.
    #: (``"particles"`` - discrete advected markers rather than either - is a
    #: FUTURE value; nothing here reads it yet.)
    #:
    #: CONSTRAINT, not history: this styling is the reference for QGIS-native
    #: mesh vector rendering when mesh-layer publishing lands. The dock ships
    #: arrows, streamlines and traces natively, and the declared vocabulary here
    #: maps onto that set - streamlines over a magnitude ramp at a declared
    #: density, quiver at a declared arrow size on a declared grid - or the
    #: proof sheet and the product will be showing the same run two different
    #: ways.
    vectors: str | None = None
    #: The vector style the PEAK/FINAL STILL draws, when it differs from the
    #: moving GIF's own ``vectors``. ``None`` means "same as the animation". A
    #: moving field reads as a traced flow; one frozen frame reads calmer as
    #: discrete arrows on a coarser, further-decimated grid, because a still has
    #: no motion to imply and a dense trace frozen in place is noise a moving
    #: GIF does not carry.
    still_vectors: str | None = None
    #: Streamline seeding density, matplotlib's own ``density`` argument. 1.4
    #: keeps a catchment-scale network legible: enough traces to see the
    #: drainage concentrate, few enough that they do not merge into a wash.
    vector_density: float = 1.4
    #: The regular grid the components are interpolated onto before tracing, per
    #: axis. Streamlines need a REGULAR field and the solve is on a triangular
    #: mesh, so this is a declared DECIMATION - 200 across the wider axis, which
    #: resolves a 40 m mesh over a 30 km2 catchment without paying for a trace
    #: through every element. A ``"quiver"`` still reads a further-decimated cut
    #: of this same grid - fewer, larger-spaced arrows for a calmer static read.
    vector_grid_n: int = 200
    #: matplotlib's own arrow-prominence scale for whichever primitive
    #: ``vectors``/``still_vectors`` draws (streamplot's ``arrowsize``, or an
    #: equivalent multiplier on quiver's head dimensions): 1.0 is that
    #: primitive's OWN default. 0.7 is the family's baseline; a dense trace at
    #: high seeding density needs it turned DOWN further, because a same-size
    #: arrowhead on every one of several hundred traces is what reads as
    #: clutter, not the traces themselves.
    arrow_size: float = 0.7
    #: The ``(min, max)`` line width a streamline trace's LOCAL magnitude tapers
    #: between - matplotlib's own per-point ``linewidth`` array on
    #: ``streamplot``, scaled off the SAME ``(vmin, vmax)`` the colour ramp
    #: reads, so width and colour agree about which point on the trace is fast.
    #: The taper carries speed; the arrowhead is secondary.
    vector_lw: tuple[float, float] = (0.35, 1.1)
    #: Overrides the preset's ramp TRANSFORM (``linear`` | ``log`` | ``sqrt``).
    #: A field spanning orders of magnitude - millimetre sheet flow against
    #: centimetre channel accumulation - has no linear ramp that shows both: the
    #: whole grid just darkens together and the picture cannot tell solver
    #: dynamics from a brightness ramp. The legend states the transform.
    transform: str | None = None
    reason: str = ""
    exempt_reason: str | None = None


#: Keyed by TOOL, because a tool name is what an evidence JSON records; the value
#: is a TUPLE, because one run can owe more than one animation. A second entry is
#: a template DECISION - somebody decided this run answers two questions - never
#: a default the assembler invents.
PROOF_ANIMATIONS: dict[str, tuple[ProofAnimation, ...]] = {
    # A dry-start watershed's published answer IS max water depth (overland sheet
    # flow); free surface over a hillslope is terrain.
    #
    # AND THE THRESHOLD IS NOT WET_TOL. 0.02 m is the depth at which flooded land
    # counts as flooded, and overland sheet flow on a hillslope is an order of
    # magnitude thinner than that: this catchment's
    # whole field peaks at 0.0273 m, so masking at 0.02 keeps a 7 mm sliver and
    # throws the answer away. Exact zero is the only honest gate here: paint
    # every cell carrying water, leave the never-wetted hillside to the basemap.
    "telemac_rain_on_grid": (
        ProofAnimation(
            name="inundation_depth",
            variable="WATER DEPTH", units="m", quantity="flood_depth",
            mask_var="WATER DEPTH", mask_threshold=0.0, still="peak",
            transform="log",
            reason="the question is overland sheet flow on a dry-start "
                   "catchment, so the answer is the depth itself; free surface "
                   "on a hillslope is terrain. Masked at depth > 0 rather than "
                   "at the coastal WET_TOL, which is thicker than the entire "
                   "sheet-flow field. LOG ramp because uniform design-storm "
                   "forcing over a millimetre-scale field on a linear scale "
                   "renders as the whole grid darkening together - the drainage "
                   "network only separates from the hillslope when millimetre "
                   "sheet flow and centimetre channel accumulation stop sharing "
                   "one linear ramp."),
        # The depth field says how much water is standing; it does not say the
        # water is GOING anywhere. Speed is the field that shows hillslope-to-
        # channel concentration - the visual counterpart of the outlet
        # hydrograph the run already charts. The solver writes components, so
        # the magnitude is derived.
        ProofAnimation(
            name="flow_dynamics",
            variable="VELOCITY MAGNITUDE", units="m/s", quantity="flow_velocity",
            derived=("VELOCITY U", "VELOCITY V"),
            vectors="streamlines", still_vectors="quiver",
            vector_density=1.4, vector_grid_n=200,
            # A same-size arrowhead on every one of several hundred moving
            # traces reads as clutter rather than as several hundred traces, so
            # the arrowhead is turned down to a THIRD of the family baseline and
            # the trace's own width - tapered by the LOCAL magnitude between the
            # declared bounds - carries speed instead. The still switches
            # primitive entirely: a frozen quiver arrow per cell reads calmer
            # than a frozen trace, which a moving GIF does not need to worry
            # about because it is never frozen.
            arrow_size=0.23, vector_lw=(0.3, 1.3),
            mask_var="WATER DEPTH", mask_threshold=0.0, still="peak",
            reason="speed is what shows the water MOVING - runoff concentrating "
                   "off the hillslopes into the drainage network - which a depth "
                   "field cannot show. Linear, because a speed field spans one "
                   "order of magnitude rather than three. Streamlines over the "
                   "magnitude ramp so the frame carries DIRECTION as well as "
                   "speed: a scalar speed field says the water is fast without "
                   "saying where it is going, with the trace's width - not its "
                   "arrowhead - carrying how fast."),
    ),
    "telemac_river_dye": (
        ProofAnimation(
            variable="DYE", units="mg/L", quantity="dye_concentration",
            mask_var="WATER DEPTH", mask_threshold=WET_TOL_M, still="peak",
            reason="the plume IS the tracer concentration; it BUILDS as the dye "
                   "arrives, so the peak frame is the answer."),
    ),
    "telemac_do_sag": (
        ProofAnimation(
            variable="DISSOLVED O2", units="mgO2/l", quantity="dissolved_oxygen",
            mask_var="WATER DEPTH", mask_threshold=WET_TOL_M, still="final",
            reason="the sag is the oxygen deficit downstream of the outfall. It "
                   "DECAYS toward its answer, so the still is the final frame - "
                   "the peak frame is the un-depleted initial condition."),
    ),
    # A 3D prism result carries no WATER DEPTH to mask on - every node of the
    # column is in the water by construction.
    "telemac3d_stratified_flow": (
        ProofAnimation(
            variable="TEMPERATURE", units="degC", quantity="temperature",
            still="final", plane="surface",
            reason="the thermocline question is answered by the temperature "
                   "field, and the surface plane is the half that the wind (or "
                   "its absence) acts on. It SETTLES toward its answer, so the "
                   "still is final."),
    ),
    "artemis_harbor_agitation": (
        ProofAnimation(
            exempt_reason="ARTEMIS is the phase-resolving elliptic mild-slope "
                          "(Berkhoff) solver: it solves a boundary-value problem "
                          "for a single monochromatic sea state and returns ONE "
                          "field, the steady agitation coefficient Kd. The run "
                          "has no simulation clock at all, which is why its "
                          "result carries ONE record - there is no time "
                          "evolution to animate, and the single field IS the "
                          "whole answer.",
            variable="WAVE HEIGHT", units="m", still="peak",
            reason="the steady wave height field, rendered as the run's one "
                   "still."),
    ),
}


def animations_for(tool: str) -> tuple[ProofAnimation, ...]:
    """Every animation the tool declares, in declaration order. Empty when none.

    Empty is a REFUSAL upstream, never a default: the packet assembler reports an
    undeclared time-stepped template as a named gap rather than animating
    whatever variable a renderer would have reached for first.
    """
    return PROOF_ANIMATIONS.get(tool, ())


def suffixed(animation: ProofAnimation, declared: int) -> str:
    """The filename infix for one animation: ``_<name>`` only when there are many.

    A template declaring ONE animation keeps the bare ``_animation.gif`` /
    ``_peak_frame.png`` names, because those filenames are cited by name in ADRs
    and evidence JSONs and renaming every existing proof for a coastal-only
    feature is churn, not consistency.
    """
    return f"_{animation.name}" if declared > 1 else ""


#: Standing caveats a packet must CARRY, keyed by ``(tool, variant)``. A proof
#: that is a thin test of what it shows has to say so on the packet rather than
#: in somebody's memory of the review - a reader handed a folder cannot tell a
#: deliberately small canary from the flagship it stands in for.
PACKET_NOTES: dict[tuple[str, str], tuple[str, ...]] = {
    ("telemac_rain_on_grid", "coarse"): (
        "SCREENING RUN, not a calibrated study. The storm is the NOAA Atlas 14 "
        "10-year / 24-hour depth at the pour point spread as a CONSTANT rate - a "
        "real design depth, but a rectangular hyetograph, so the crest arrives "
        "when the rain stops rather than when the basin's own intensity peak "
        "passes. The catchment is UNGAUGED here: no observed hydrograph has been "
        "paired against this outlet, so the peak and the runoff coefficient are "
        "the model's answer under the declared curve numbers, not a verified "
        "one. The peak DEPTH is a single-node maximum that a terrain pit can set "
        "on its own - read it beside the p99, which is the sheet.",
    ),
    # THE COARSE LANE'S OWN CAVEAT, per template. A coarse packet that passes
    # every mechanical check still shows a run sized to prove the plumbing, and
    # each of these limits is already written into the run's own declaration -
    # so the packet carries it rather than leaving a reader to find it there.
    ("telemac_do_sag", "coarse"): (
        "PLUMBING SMOKE, and nothing more. A DO sag is a TRAVEL-TIME answer - it "
        "needs k1 times travel time of order one to exist at all - and 600 s over "
        "half a kilometre is four orders short of that. This run's sag numbers "
        "are NOT a physics answer and must not be read as one; what it proves is "
        "that geocode -> flowline -> section -> mesh -> WAQTEL run -> solve -> "
        "products still runs end to end. The physics lives in the refined "
        "declaration (4 km / 48 h).",
    ),
    ("telemac_river_dye", "coarse"): (
        "PLUMBING PIN, not a concentration study. A concentration peak lives "
        "inside one element, so the coarse mesh measures dye_cmax_mgl LOW - the "
        "template declares that scalar peak-class for exactly this reason. The "
        "plume's REACH is a front location and travels with the front, so it is "
        "the scalar this run can be read on. The refined declaration (10 m) is "
        "what carries a concentration answer.",
    ),
    ("artemis_harbor_agitation", "coarse"): (
        "UNDER-RESOLVED BY DESIGN. An 8 s swell in ten metres of water is about a "
        "78 m wave, and 30 m node spacing resolves it on roughly two nodes - "
        "enough to prove the diffraction plumbing, not enough to trust a fringe "
        "pattern or a peak Kd. The refined declaration (20 m, the builder's own "
        "floor) is what gets delivered. The peak-frame still paints WAVE HEIGHT, "
        "which this run publishes no raster of, so its legend is read off the "
        "frames alone rather than shared with a panel.",
    ),
    ("artemis_harbor_resonance_idealized", "coarse"): (
        "AN ANALYTIC BASIN, NOT A PLACE. The domain is the idealized harbour "
        "geometry; the bbox only puts the basin's label on the map, and no "
        "bathymetry of Marquette enters this run. Its answer is the pair of "
        "responses - at the basin's own resonant period and off it - which is "
        "what a regression moves.",
    ),
    ("telemac3d_stratified_flow", "coarse"): (
        "3000 m HORIZONTAL over a one-hour window. The question is whether a calm "
        "column KEEPS its thermocline, and the surviving top-to-bottom difference "
        "is the number this run answers with; the horizontal field at this "
        "spacing resolves no lake circulation. The refined declaration (1000 m at "
        "the same 13 planes) is the one the horizontal is read from.",
    ),
}


def packet_notes(tool: str, variant: str) -> tuple[str, ...]:
    """The standing caveats this template+variant packet must carry."""
    return PACKET_NOTES.get((tool, variant), ())
