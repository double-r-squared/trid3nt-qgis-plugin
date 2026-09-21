"""Engine template ``tomawac_wave_driven_currents`` - the current the waves drive.

TELEMAC-2D solving the water, COUPLED to TOMAWAC solving the wave field over the
same mesh: where the waves break, the gradient of their radiation stress enters
the momentum equation as a force, and what comes out of it is the longshore
current that moves sand along a beach and sets a swimmer down the coast. The
wave edge is forced at a sea state a buoy MEASURED, and the water stands at the
tide a gauge reported - which is what decides where the breaking, and therefore
the forcing, happens.
"""

from __future__ import annotations

import sys

from trid3nt_contracts.tool_registry import AtomicToolMetadata, ResolutionSpec

from trid3nt_server.inputs import point_arg
from trid3nt_server.inputs.instant import event_time
from trid3nt_server.workflows.runtime import (
    Data,
    Ref,
    register_workflow,
)
from trid3nt_server.workflows.mesh.tool import mesh_op, tool
from trid3nt_server.workflows.telemac.modules import T2D, WAC, field, mesh, series
from trid3nt_server.workflows.telemac.modules.telemac2d import Boundaries, Wind
from trid3nt_server.workflows.telemac.modules.tomawac import RESULT_FILENAME
from trid3nt_server.workflows.telemac.templates.wave_driven_currents.declarations import (
    ACCEPTS,
    DOC,
    PARAMS,
    PARAMS as P,
)
from trid3nt_server.workflows.telemac.workflow import Placed, TelemacWorkflow

__all__ = ["ANSWER", "CAPTIONS", "DATA", "MESH", "OUTPUTS", "PARAMS", "STEERING",
           "tomawac_wave_driven_currents"]


#: What the run directory holds the run's files under - the host deck's own
#: GEOMETRY / BOUNDARY CONDITIONS / RESULTS statements. Same-mesh coupling, so
#: the wave deck is handed these two rather than a pair of its own.
_GEOMETRY = "coast.slf"
_BOUNDARY = "coast.cli"
_RESULT = "t2d_coast.slf"
_STEERING_FILE = "t2d_wave_driven.cas"

#: THE CLOCK, stated once and read by both decks. An hour is long enough for the
#: wave field to cross the domain and for the current it forces to spin up to
#: the speed the forcing holds it at.
_DURATION_S = 3600.0
#: The step the WATER is solved at. Stated rather than taken off the settle,
#: because the wave deck marches the same clock and the two have to agree: a
#: coupled run whose modules step apart is two runs on one mesh. One second is
#: the CFL step at the finest edge this deck declares.
_TIME_STEP_S = 1.0
#: HOW OFTEN the wave field is recomputed, in host steps. A measured sea state
#: is steady over the hour, so what changes between recomputations is the depth
#: and the current the waves refract through - a minute of those, not a second.
_COUPLING_PERIOD = 60
#: The wave deck's own clock, which is the host's seen through that period.
_WAVE_TIME_STEP_S = _TIME_STEP_S * _COUPLING_PERIOD
_WAVE_STEPS = int(_DURATION_S / _WAVE_TIME_STEP_S)
#: HOW OFTEN each result is written, in that deck's own solver steps: sixty
#: frames of the hour on both, so the two files carry the same instants and the
#: current and the wave that drove it are read on one clock.
_HOST_FRAMES = int(_DURATION_S / _TIME_STEP_S) // _WAVE_STEPS

#: The roughness the shoreface is solved at, and the law it is read under:
#: Nikuradse, whose coefficient is a grain roughness in METRES, which is how a
#: sandy surf zone is described. A user who knows the bed sets the keyword by
#: its own name.
_FRICTION_LAW = 5
_FRICTION_COEFFICIENT = 0.05

#: WHERE the current is read over time: the point the ask gave, settled onto a
#: node of the accepted mesh. The fraction is never reached - the point is a
#: required param, because a coast has no centerline a station could sit along.
_STATION = Placed("station", point=PARAMS.station, label="Current station")


class DATA:
    """What the run consumes from the world: the water the coast leaves inside
    the window, the bed the waves break over, the sea state arriving across the
    open edge and the tide the whole of it stands on."""

    #: The window the coastal question is asked in. Drawn on the canvas, so a
    #: caller who supplies the water's outline below is never asked to draw one.
    extent = Data.supplied(geometry="rectangle")
    #: THE DOMAIN, asked for as the LAND-WATER EDGE: the class it is rather than
    #: the source it comes from, and the feature of that class this question
    #: reads. A line is not a domain, so the slot cuts the window above with it
    #: and the water that leaves is what both modules are solved over.
    domain = Data.need("hydrography", of="coastline", geometry="polyline")
    #: ONE bed: the class it is defined over rather than the source it comes
    #: from. Where the depth falls is where the waves break, and where they
    #: break is where the current is driven, so this surface decides the answer
    #: twice over.
    bed = Data.need("bathymetry")
    #: THE SEA STATE the wave deck's open edge is forced at, as the CLASS it is:
    #: one record of a height, a period and a direction, measured by whatever
    #: buoy reports them near the point the question names. A swell arriving
    #: square to the beach drives no current along it, so the direction in this
    #: record is what the whole question turns on.
    wave = Data.need("wave series", at=Ref("seed"))
    #: THE TIDE, as the SERIES the record serves rather than one reading of it.
    #: The seaward rim is a prescribed ELEVATION - that is what an ocean boundary
    #: section writes into the boundary file - so this is what the open edge
    #: holds and what the depths under the breaking are counted down from, and
    #: it MOVES: the host writes the window as the boundary's own column and
    #: lumps to a single number only where the record reported one moment.
    level = Data.need("water level series")
    #: The domain as a MESH, when the caller has one already. Unfilled, MESH
    #: below is what both modules are solved on.
    mesh = Data.supplied(geometry="mesh").optional()


class STEERING(T2D):
    """The host deck: the water, and the wave field it feels as a force."""

    TITLE = Ref("settled.title")
    GEOMETRY_FILE = _GEOMETRY
    BOUNDARY_CONDITIONS_FILE = _BOUNDARY
    RESULTS_FILE = _RESULT

    TIME_STEP = _TIME_STEP_S
    DURATION = _DURATION_S
    GRAPHIC_PRINTOUT_PERIOD = _HOST_FRAMES
    LISTING_PRINTOUT_PERIOD = _HOST_FRAMES

    # HOW THE WATER OPENS is the settle's, not this deck's: flat at the tide
    # somebody measured. A horizontal surface is what a coastal window opens at,
    # and the nodes above it are the beach the waves run up.
    INITIAL_CONDITIONS = Ref("settled.opening")
    INITIAL_DEPTH = Ref("settled.depth_m")
    INITIAL_ELEVATION = Ref("settled.level_m")

    LAW_OF_BOTTOM_FRICTION = _FRICTION_LAW
    FRICTION_COEFFICIENT = _FRICTION_COEFFICIENT

    # The advection of momentum and depth, and the SUPG the domain is stable
    # under.
    TYPE_OF_ADVECTION = [1, 5]
    SUPG_OPTION = [0, 0]
    MASS_LUMPING_ON_H = 1.0
    CONTINUITY_CORRECTION = True
    SOLVER = 1
    SOLVER_ACCURACY = 1.0e-6
    MAXIMUM_NUMBER_OF_ITERATIONS_FOR_SOLVER = 500
    IMPLICITATION_FOR_DEPTH = 0.6
    IMPLICITATION_FOR_VELOCITY = 0.6

    # A SURF ZONE DRIES AND WETS every wave: the swash runs up the beach and
    # back off it, and without this the solver meets a negative depth there and
    # the run either stops or reports water where there is sand.
    TREATMENT_OF_NEGATIVE_DEPTHS = 1

    # The engine accounts for its own water volume and prints one flux per
    # liquid boundary, which is the only honest check that the tide prescribed
    # at the seaward rim reached it.
    MASS_BALANCE = True

    #: NO tracer: this question is about the water's own momentum, so every
    #: liquid boundary carries the measured tide and nothing else. The walk is
    #: the mesh's own.
    boundaries = Boundaries(measured=Ref("settled"), tracers=[])

    #: CALM: the question is what the WAVES drive, so this deck states no
    #: surface stress of its own - a current with a wind in it would be two
    #: answers added together and reported as one.
    wind = Wind(speed_mps=0.0, from_deg=0.0)

    #: THE WAVE FIELD, solved on this mesh and handed back as a momentum source.
    #: TOMAWAC appends no row to this deck's results - it writes its own file -
    #: so WAVE DRIVEN CURRENTS is what the coupling arms on this host, and
    #: without it the wave field would be solved and thrown away.
    coupling = [WAC.wave(
        geometry=_GEOMETRY, boundary=_BOUNDARY,
        # The wave deck marches the host's clock, seen once per coupling period.
        TIME_STEP=_WAVE_TIME_STEP_S,
        NUMBER_OF_TIME_STEP=_WAVE_STEPS,
        PERIOD_FOR_GRAPHIC_PRINTOUTS=1,
        PERIOD_FOR_LISTING_PRINTOUTS=1,
        # THE SPECTRAL GRID. Twenty-four directions is a sector every fifteen
        # degrees, which resolves a swell refracting round into the shore-normal
        # it drives no current at; twenty-five frequencies from 0.04 Hz at the
        # dictionary's own ratio span a 25 s swell down to a 2.5 s wind chop.
        NUMBER_OF_DIRECTIONS=24,
        NUMBER_OF_FREQUENCIES=25,
        MINIMAL_FREQUENCY=0.04,
        # The domain opens EMPTY and fills from the boundary: the sea state that
        # arrives is the whole of the forcing.
        TYPE_OF_INITIAL_DIRECTIONAL_SPECTRUM=0,
        # THE OPEN EDGE, as the keywords the dictionary spells it in: a JONSWAP
        # shape at the buoy's own height, one over its period, and the bearing
        # the waves run toward, which the wave slot's ingestion turns from the
        # one the record publishes.
        TYPE_OF_BOUNDARY_DIRECTIONAL_SPECTRUM=6,
        BOUNDARY_SIGNIFICANT_WAVE_HEIGHT=Ref("wave.height_m"),
        BOUNDARY_PEAK_FREQUENCY=Ref("wave.peak_frequency_hz"),
        BOUNDARY_MAIN_DIRECTION_1=Ref("wave.direction_deg"),
        # BREAKING IS THE FORCING. The longshore current is the gradient of the
        # radiation stress the breaking leaves behind, so a deck without this
        # hands its host a wave field that never loses energy and therefore
        # drives nothing at all.
        DEPTH_INDUCED_BREAKING_DISSIPATION=1,
        BOTTOM_FRICTION_DISSIPATION=1)]

    #: HOW OFTEN the host calls it, in host steps.
    COUPLING_PERIOD_FOR_TOMAWAC = _COUPLING_PERIOD


#: THE MESH RECIPE, frozen at declaration and building nothing at import. The
#: domain polygon's own edge IS the shoreline the sizing function measures. Both
#: modules are solved on it: the host reads the rim as its prescribed tide and
#: the wave deck reads the same rim as the edge the spectrum enters across.
MESH = tool.build_mesh(
    mesher="om2d",
    kind="unstructured_tri",
    extent=DATA.domain,
    resolution_m=P.mesh_resolution_m,
    ops=[
        mesh_op("feature_sizing_function"),
        # THE RIM IS THE ASK'S TO SIZE. No sizing function the library has
        # measures the domain's own outline, so an undeclared rim comes back an
        # order of magnitude past the size word and the band behind it
        # triangulates into slivers.
        mesh_op("set_rim_size"),
        mesh_op("enforce_mesh_gradation"),
        mesh_op("delete_boundary_faces"),
        mesh_op("delete_faces_connected_to_one_face"),
        mesh_op("make_mesh_boundaries_traversable"),
        mesh_op("fix_mesh", delete_unused=True),
        mesh_op("set_bed", source=DATA.bed),
        # EVERY stretch the library reads as ocean at this depth OPENS. The code
        # quad an open section is written under prescribes a water LEVEL and
        # leaves the velocity free, which is exactly the tidal edge a coastal
        # window wants and exactly the edge a spectrum is imposed across.
        mesh_op("identify_ocean_boundary_sections",
                depth_threshold=P.open_depth_threshold_m),
    ],
)


#: What this question PLACES: the current over time at the point the ask gave,
#: and the wave height at the same point off the coupled module's own file.
#: Everything else the two modules wrote - the velocity components, the depth,
#: the wave periods and directions, the breaking band, the forces - is published
#: because their tables row it, not because this template asked.
OUTPUTS = [
    series("M", at=_STATION).chart(),
    series("HM0", at=_STATION, module="tomawac").chart(),
]
CAPTIONS = {"M": "current speed", "U": "current along x", "V": "current along y",
            "HM0": "significant wave height", "BETA": "breaking rate",
            "wave": "a sea state",
            "level": "the tide the open edge holds, over the run's window"}

#: The run's ANSWER, as the numbers a reader has to be able to check: how fast
#: the water is moving at the station when the window closes, the pair that says
#: WHICH WAY it is running, the wave that is driving it, and the fastest water
#: anywhere in the run. A bearing is not among them because neither module
#: writes one for a current - the two components are what the engine solved.
ANSWER = {
    "longshore_current_speed_mps": series("M", at=_STATION).measure("last"),
    "current_along_x_mps": series("U", at=_STATION).measure("last"),
    "current_along_y_mps": series("V", at=_STATION).measure("last"),
    "hs_at_station_m": series("HM0", at=_STATION,
                              module="tomawac").measure("last"),
    "peak_current_speed_mps": series("M").measure("max"),
    # THE FORCING ITSELF, as a magnitude: the coupled module publishes its
    # breaking rate as a negative quantity, so the hardest-working node is the
    # field's MINIMUM negated. A current with no breaking behind it is a run
    # that drove nothing, which is the one reading this answer separates.
    "breaking_rate_peak_per_s": (field("BETA", t=-1, module="tomawac")
                                 .measure("min").over(-1.0)),
    "mesh_size_m": mesh().measure("size_m"),
}


#: DECLARED mesh_resolution_m range. The floor is NOT the mesh builder's: this
#: deck states its own time step, and a step stable at a 20 m edge diverges at
#: a finer one, so twenty metres is where this question's stated clock stops
#: being honest.
_RES_SPEC = ResolutionSpec(
    param="mesh_resolution_m",
    unit="m",
    min_value=20.0,
    native_hint="edge sized from the water's own width; the bed's own cell is "
                "the matched source's, stated on its row",
    constraint_source="solver",
    rationale=(
        "explicit target edge length; 20 m is the finest edge the TIME STEP "
        "this deck states for BOTH its modules stays stable at, and a long "
        "window is further coarsened under the mesh node budget "
        "(self-labeled). The current is driven across the surf zone, so a "
        "coarse edge averages the breaking band away and with it the gradient "
        "that forces the current"
    ),
)

_METADATA = AtomicToolMetadata(
    name="tomawac_wave_driven_currents",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    engine="telemac",
    tier="template",
    resolution_specs=(_RES_SPEC,),
)


#: WHAT the mesh is built over, and the DATA slot a caller may hand a built
#: mesh in instead of the recipe. Filled, that mesh is adopted whole.
MESH_ON = "domain"
SUPPLIED_MESH = DATA.mesh

#: WHAT THE RUN HAS TO WRITE: the host's file and the wave module's own beside
#: it. The wave the current is read against lives in the second one, so a run
#: that published only the host's would answer half the question.
RESULTS = (_RESULT, RESULT_FILENAME)

#: What the run directory calls the deck, and where the staged files live.
STEERING_FILE = _STEERING_FILE
PREFIX = "t2d"

#: The title the card carries when the run is held for review.
REVIEW_TITLE = "Review the sea state, the tide and the water they drive"


tomawac_wave_driven_currents = register_workflow(
    TelemacWorkflow, _METADATA, sys.modules[__name__],
    provenance=(("mesh_resolution_m", "mesh_resolution_note"),),
    # The longshore current lives INSIDE the surf zone, which is a band a few
    # elements wide; a coarse element averages the breaking across it and the
    # peak speed lands low.
    sensitivity=(("peak_current_speed_mps", "peak"),),
    coerce=(
        point_arg("seed", tool="tomawac_wave_driven_currents",
                  prompt="Click offshore, where the incoming sea state is "
                         "measured",
                  code="TELEMAC_PARAMS_INVALID"),
        point_arg("station", tool="tomawac_wave_driven_currents",
                  prompt="Click on the water where the current should be read",
                  code="TELEMAC_PARAMS_INVALID"),
        event_time(),
    ),
)
