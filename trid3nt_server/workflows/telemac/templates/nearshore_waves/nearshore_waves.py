"""Engine template ``tomawac_nearshore_waves`` - what the swell becomes inshore.

TOMAWAC is the phase-AVERAGING spectral model: it carries the directional
spectrum over the water rather than the wave itself, so the answer is the height,
the period and the direction the sea state has after shoaling, refracting and
breaking across the bed between the offshore edge and the shore. The edge is
forced at a sea state a buoy MEASURED - a height, a period and a bearing - and
the water stands at the tide a gauge reported.
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
from trid3nt_server.workflows.telemac.modules import (
    WAC,
    field,
    mesh,
    series,
    spectrum,
)
from trid3nt_server.workflows.telemac.modules.tomawac import RESULT_FILENAME
from trid3nt_server.workflows.telemac.templates.nearshore_waves.declarations import (
    ACCEPTS,
    DOC,
    PARAMS,
    PARAMS as P,
)
from trid3nt_server.workflows.telemac.workflow import Placed, TelemacWorkflow

__all__ = ["ANSWER", "CAPTIONS", "DATA", "MESH", "OUTPUTS", "PARAMS", "STEERING",
           "tomawac_nearshore_waves"]


#: What the run directory holds the run's files under - the deck's own GEOMETRY /
#: BOUNDARY CONDITIONS / 2D RESULTS statements.
_GEOMETRY = "coast.slf"
_BOUNDARY = "coast.cli"
_STEERING_FILE = "tom_nearshore.cas"
#: The PUNCTUAL file, which is not a field over the domain: it is the whole
#: directional spectrum at each point the deck names, written over the polar
#: frequency-direction grid.
_SPECTRA = "tom_nearshore.spe"

#: WHERE the waves are read over time: the point the ask gave, settled onto a
#: node of the accepted mesh, so every chart is a node the run solved on. The
#: fraction is never reached - the point is a required param, because a coast
#: has no centerline a station could sit a fraction along.
_STATION = Placed("station", point=PARAMS.station, label="Wave station")


class DATA:
    """What the run consumes from the world: the water the coast leaves inside
    the window, the bed it shoals over, the sea state arriving across the open
    edge and the tide the whole of it stands on."""

    #: The window the coastal question is asked in. Drawn on the canvas, so a
    #: caller who supplies the water's outline below is never asked to draw one.
    extent = Data.supplied(geometry="rectangle")
    #: THE DOMAIN, asked for as the LAND-WATER EDGE: the class it is rather than
    #: the source it comes from, and the feature of that class this question
    #: reads. A line is not a domain, so the slot cuts the window above with it
    #: and the water that leaves is what the waves are solved over.
    domain = Data.need("hydrography", kind="coastline", geometry="polyline")
    #: ONE bed: the class it is defined over rather than the source it comes
    #: from - the survey where something sounded it, the terrain everywhere
    #: else. The whole question is what the waves do as the depth falls, so this
    #: is the load-bearing surface of the run.
    bed = Data.need("bathymetry")
    #: THE SEA STATE the open edge is forced at, as the CLASS it is: one record
    #: of a height, a period and a direction, measured by whatever buoy reports
    #: them near the point the question names. Its ingestion is what turns those
    #: three columns into the boundary keywords - a peak FREQUENCY off the
    #: period, and the bearing the waves run TOWARD off the one they come from.
    wave = Data.need("wave series", at=Ref("seed"))
    #: THE LEVEL the whole domain stands at, as ONE value: the still water the
    #: depths are counted down from, so a run at low water breaks further out
    #: than the same swell at high water. An ELEVATION on the datum the bed is
    #: painted on, which the runtime's own frame lever settles.
    level = Data.need("water level series").optional()
    #: The domain as a MESH, when the caller has one already. Unfilled, MESH
    #: below is what the waves are solved on.
    mesh = Data.supplied(geometry="mesh").optional()


class STEERING(WAC):
    """The deck: a measured sea state in across the open edge, and what the bed
    leaves of it at the shore."""

    TITLE = Ref("settled.title")
    GEOMETRY_FILE = _GEOMETRY
    BOUNDARY_CONDITIONS_FILE = _BOUNDARY
    ED_RESULTS_FILE = RESULT_FILENAME

    # THE CLOCK. TOMAWAC names no window: it names the step and how many of
    # them, and their product is what the run covers. A measured sea state is
    # one hour's, so the run marches an hour - long enough for the spectrum to
    # cross the domain from the open edge and stand up, short enough that the
    # buoy's reading still describes the sea the whole time.
    TIME_STEP = 10.0
    NUMBER_OF_TIME_STEP = 360
    # HOW OFTEN the field is written, in SOLVER STEPS: a frame every ten steps
    # is 36 of the hour, which is an animation of the spectrum filling the
    # domain rather than one per step.
    PERIOD_FOR_GRAPHIC_PRINTOUTS = 10
    PERIOD_FOR_LISTING_PRINTOUTS = 60

    # THE SPECTRAL GRID the sea state is carried on. Twenty-four directions is
    # a sector every fifteen degrees, which resolves a swell refracting round
    # into a bay; twenty-five frequencies from 0.04 Hz at the dictionary's own
    # ratio reach 0.39 Hz, so the grid spans a 25 s swell down to a 2.5 s
    # wind chop and the peak of an ocean swell is inside it rather than on its
    # edge.
    NUMBER_OF_DIRECTIONS = 24
    NUMBER_OF_FREQUENCIES = 25
    MINIMAL_FREQUENCY = 0.04

    # The domain opens EMPTY and fills from the boundary: what this question is
    # about is the sea state that arrives, so a spectrum laid over the whole
    # domain at t = 0 would be wave energy nobody measured.
    TYPE_OF_INITIAL_DIRECTIONAL_SPECTRUM = 0

    #: THE OPEN EDGE, as the keywords the dictionary spells it in. A JONSWAP
    #: spectrum is the shape a developing sea and a swell are both described
    #: by, and the three numbers under it are the buoy's own - the height it
    #: measured, one over the period it measured, and the bearing the waves run
    #: toward, which the wave slot's ingestion turns from the one the record
    #: publishes.
    TYPE_OF_BOUNDARY_DIRECTIONAL_SPECTRUM = 6
    BOUNDARY_SIGNIFICANT_WAVE_HEIGHT = Ref("wave.height_m")
    BOUNDARY_PEAK_FREQUENCY = Ref("wave.peak_frequency_hz")
    BOUNDARY_MAIN_DIRECTION_1 = Ref("wave.direction_deg")

    #: THE SPECTRUM ITSELF at the station, which is the sea state the three
    #: charted numbers are summary statistics OF: where the energy sits in
    #: frequency, and how it is spread over direction. The engine takes the 2D
    #: node nearest each coordinate, so the pair is the settled station in the
    #: mesh's own metres - one value with an order, read by position.
    PUNCTUAL_RESULTS_FILE = _SPECTRA
    ABSCISSAE_OF_SPECTRUM_PRINTOUT_POINTS = [Ref("station.at.0")]
    ORDINATES_OF_SPECTRUM_PRINTOUT_POINTS = [Ref("station.at.1")]

    #: WHAT THE WATER STANDS AT. TOMAWAC carries no free surface of its own: the
    #: depth every node shoals over is this level less the bed, so the tide the
    #: gauge reported is what decides where the waves break.
    INITIAL_STILL_WATER_LEVEL = Ref("level.value")

    # THE NEARSHORE PHYSICS, which is the whole of the question. Battjes-Janssen
    # depth-induced breaking is what takes a shoaling wave down at the bar and
    # the beach, and without it the run reports a height that keeps growing into
    # water too shallow to hold it; the WAM4 bottom-friction term is the other
    # sink over a shelf this shallow. White capping and the triads stand at the
    # engine's own zero: a deck driven by a swell and no wind grows nothing for
    # white capping to dissipate.
    DEPTH_INDUCED_BREAKING_DISSIPATION = 1
    BOTTOM_FRICTION_DISSIPATION = 1


#: THE MESH RECIPE, frozen at declaration and building nothing at import. The
#: domain polygon's own edge IS the shoreline the sizing function measures, so
#: the recipe hands the mesher that polygon and nothing restates where the water
#: is. The rim is sized between the sizing and the gradation that grades it in,
#: because that rim is the seaward edge the spectrum is imposed across.
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
        # triangulates into slivers - and that rim is where the wave spectrum
        # enters.
        mesh_op("set_rim_size"),
        mesh_op("enforce_mesh_gradation"),
        mesh_op("delete_boundary_faces"),
        mesh_op("delete_faces_connected_to_one_face"),
        mesh_op("make_mesh_boundaries_traversable"),
        mesh_op("fix_mesh", delete_unused=True),
        mesh_op("set_bed", source=DATA.bed),
        # EVERY stretch the library reads as ocean at this depth OPENS, and the
        # sea state is imposed along all of it: a coastal window is open on its
        # seaward side and along both ends, and picking one of them would force
        # a swell through a slot.
        mesh_op("identify_ocean_boundary_sections",
                depth_threshold=P.open_depth_threshold_m),
    ],
)


#: What this question PLACES: the sea state over time at the point the ask gave.
#: Everything the module wrote - the height, the periods, the directions, the
#: breaking band, the forces - is published because its table rows it, not
#: because this template asked.
OUTPUTS = [
    series("HM0", at=_STATION).chart(),
    series("TPD", at=_STATION).chart(),
    series("DMOY", at=_STATION).chart(),
    spectrum(at=_STATION).chart(),
]
CAPTIONS = {"HM0": "significant wave height", "TPD": "peak wave period",
            "DMOY": "mean wave direction", "BETA": "breaking rate",
            "DBR": "breaker dissipation", "wave": "a sea state",
            "level": "a water-surface elevation",
            "spectrum": "wave energy by frequency at the station"}

#: The run's ANSWER, as the numbers a reader has to be able to check: the highest
#: wave anywhere in the water, the three the station stands under when the window
#: closes, and how hard the surf zone is working. WHERE it works is the picture -
#: both breaking rows are drawn as the band the sea makes them in - so the answer
#: carries the magnitude and the layer carries the shape.
ANSWER = {
    "hs_max_m": field("HM0", t=-1).measure("max"),
    "hs_at_station_m": series("HM0", at=_STATION).measure("last"),
    "peak_period_at_station_s": series("TPD", at=_STATION).measure("last"),
    "direction_at_station_deg": series("DMOY", at=_STATION).measure("last"),
    # THE SURF ZONE AT WORK, as a MAGNITUDE. The module publishes both rows as
    # negative quantities - energy leaving the spectrum - so the hardest-working
    # node is the field's MINIMUM, and the number a reader checks is that
    # minimum negated. Read as a maximum, both would answer the untouched water
    # offshore, which is zero.
    "breaking_rate_peak_per_s": field("BETA", t=-1).measure("min").over(-1.0),
    "breaker_dissipation_peak_m2s": (field("DBR", t=-1).measure("min")
                                     .over(-1.0)),
    "mesh_size_m": mesh().measure("size_m"),
}


#: DECLARED mesh_resolution_m range. The solver floor is the finest edge the mesh
#: builder authors regardless of ask; there is no fixed coarse ceiling - the node
#: budget coarsens a long coastal window WITHIN this declaration.
_RES_SPEC = ResolutionSpec(
    param="mesh_resolution_m",
    unit="m",
    min_value=3.0,
    native_hint="edge sized from the water's own width; the bed's own cell is "
                "the matched source's, stated on its row",
    constraint_source="solver",
    rationale=(
        "explicit target edge length; 3 m is the absolute finest the builder "
        "authors and a long window is further coarsened under the mesh node "
        "budget (self-labeled). A wave shoals and then breaks over the SLOPE of "
        "the bed, so the surf zone is only as wide as the elements that resolve "
        "it - a coarse edge averages the bar away and moves the breaking line"
    ),
)

_METADATA = AtomicToolMetadata(
    name="tomawac_nearshore_waves",
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

#: The engine file this run has to write for it to have solved anything.
RESULTS = (RESULT_FILENAME,)

#: What the run directory calls the deck, and where the staged files live.
STEERING_FILE = _STEERING_FILE
PREFIX = "tomawac"

#: The title the card carries when the run is held for review.
REVIEW_TITLE = "Review the sea state, the tide and the water it crosses"


tomawac_nearshore_waves = register_workflow(
    TelemacWorkflow, _METADATA, sys.modules[__name__],
    provenance=(("mesh_resolution_m", "mesh_resolution_note"),),
    # A wave breaks where the depth falls below what it can stand in, so the
    # highest wave the run reports sits one element outside the breaking line -
    # and a coarse element puts that line somewhere else.
    sensitivity=(("hs_max_m", "peak"),),
    coerce=(
        point_arg("seed", tool="tomawac_nearshore_waves",
                  prompt="Click offshore, where the incoming sea state is "
                         "measured",
                  code="TELEMAC_PARAMS_INVALID"),
        point_arg("station", tool="tomawac_nearshore_waves",
                  prompt="Click on the water where the waves should be read",
                  code="TELEMAC_PARAMS_INVALID"),
        event_time(),
    ),
)
