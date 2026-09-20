"""Engine template ``telemac_rain_on_grid`` - a storm over the ground it falls on.

TELEMAC-2D full shallow-water overland flow across the domain this run solves on
- the catchment traced upslope of a pour point, a basin the user draws, a
polygon they own - with SCS curve-number infiltration under it and one outlet
below. APPLICABILITY (Godara, Bruland and Alfredsen 2024, Front. Water
6:1384205): single-storm flash floods in small steep catchments; infiltrated
water is permanently lost, so there is no subsurface return flow and no
baseflow."""

from __future__ import annotations

import sys

from trid3nt_contracts.tool_registry import AtomicToolMetadata, ResolutionSpec

from trid3nt_server.workflows.runtime import (
    Data,
    Ref,
    register_workflow,
    tool,
)
from trid3nt_server.workflows.mesh.tool import mesh_op
from trid3nt_server.inputs import point_arg
from trid3nt_server.workflows.telemac.modules import (
    T2D,
    extent,
    field,
    mass_balance,
    max_over_time,
    mesh,
    series,
)
from trid3nt_server.workflows.telemac.modules.telemac2d import (
    Infiltration,
    Rating,
    Storm,
)
from trid3nt_server.workflows.solver.compute_class import compute_class
from trid3nt_server.workflows.telemac.templates.rain_on_grid.declarations import (
    DOC,
    LANDCOVER_CN_MANNING,
    LANDCOVER_UNMAPPED,
    PARAMS,
    PARAMS as P,
)
from trid3nt_server.workflows.telemac.workflow import (
    Measured, TelemacWorkflow,
)

__all__ = ["ANSWER", "CAPTIONS", "DATA", "MESH", "OUTPUTS", "PARAMS", "STEERING",
           "telemac_rain_on_grid"]


#: The names the run directory holds this run's files under - the deck's own
#: GEOMETRY / BOUNDARY CONDITIONS / RESULTS statements, which the workflow reads
#: back off the deck rather than being told them twice.
_GEOMETRY = "rog.slf"
_BOUNDARY = "rog.cli"
_RESULT = "r2d_rog.slf"

#: How far this question's domain reaches: the half-width of the square DEM
#: window the basin is traced inside. The delineation REFUSES a basin the
#: window clips, so this over-covers one headwater catchment upstream of its
#: outlet; at this cell it stays well inside the tracer's own pre-network budget.
_BASIN_WINDOW_KM = 15.0

#: How fast the mesh edge may grow away from the channel-network band it is
#: sized against.
_MESH_GRADE = 0.20

#: Where the image BAKES the RAINDEF=3 copy of the engine's own
#: ``runoff_scs_cn.f``. The installed source hardcodes ``RAINDEF=1`` as a
#: compile-time PARAMETER, which no steering keyword can reach, so a
#: time-varying gross hyetograph needs the engine's own user-fortran door. The
#: patch is made ONCE at build time and the run STAGES it; a constant-rain run
#: names nothing here and stages nothing.
RAINDEF3_USER_FORTRAN = "/opt/trid3nt/user_fortran/raindef3"


class DATA:
    """The catchment this run stands on, the ground it runs over, the network
    its mesh refines toward, the surface its infiltration is read off, and the
    storm that measured over it, if one did."""

    #: THE CATCHMENT, as a CLASS: a pour point names which basin is modelled at
    #: all, and a watershed IS a water-body extent like any other hydrography
    #: row. The match snaps it onto the traced channel, walks the D8 grid
    #: upslope and returns the divide with the outlet run it drains through - a
    #: basin the user draws or owns supersedes it and carries its own runs, or
    #: none.
    domain = Data.need("hydrography", at=Ref("pour_point"),
                       span_km=_BASIN_WINDOW_KM)
    #: THE GROUND the water runs over, as the whole surface rather than a bed
    #: measured over something else: an OVERLAND domain has no channel bottom
    #: under a hillslope, so nothing is laid over this row.
    bed = Data.need("terrain")
    #: THE MAPPED CHANNEL NETWORK the mesh refines toward - a river's own
    #: geometry is a hydrography row too, ranked by the match against the same
    #: class as the domain. A sizing function measures DISTANCE FROM A LINE, so
    #: the shape is stated: a waterbody polygon is the same class and not this.
    rivers = Data.need("hydrography", geometry="polyline")
    landcover = Data.need("land cover")
    #: THE MEASURED STORM, as the hourly analysis of record published it over
    #: this catchment. CONTEXT: a window nobody stated, a basin outside CONUS or
    #: hours the record has not published yet leave the row absent and the design
    #: storm drives the run, which the sheet says in those words.
    rain = Data.need("precipitation series").context(
        "no hourly rainfall record over this catchment for that window; the "
        "design storm drives the run")


#: The MESH RECIPE this question states for itself, because a catchment is
#: triangulated as a BAND - fine in the channel band, coarsening onto the
#: hillslopes - and the workflow's own recipe resolves one edge over the whole
#: domain. Everything else is the workflow's: the rim at the size word, the
#: library's clean chain, the bed, the runs.
MESH = tool.build_mesh(
    mesher="om2d",
    kind="unstructured_tri",
    extent=DATA.domain,
    resolution_m=P.mesh_resolution_m,
    ops=[
        # Fine along the channel network, coarsening away from it - oceanmesh's
        # own sizing functions under its own names. No sizing function the
        # library has measures the domain's own outline, so the rim is locked at
        # the size word between the sizing and the gradation, and the gradation
        # then holds the whole lattice.
        mesh_op("distance_sizing_from_line_function", line_file=DATA.rivers,
                rate=_MESH_GRADE, max_edge_length=P.mesh_max_edge_m),
        mesh_op("set_rim_size"),
        mesh_op("enforce_mesh_gradation", gradation=_MESH_GRADE),
        mesh_op("delete_boundary_faces"),
        mesh_op("delete_faces_connected_to_one_face"),
        mesh_op("make_mesh_boundaries_traversable"),
        mesh_op("fix_mesh", delete_unused=True),
        # ONE GROUND, CONDITIONED THE SAME WAY. The basin was traced on the
        # pit-filled surface of this product, so the bed the nodes are painted
        # from is conditioned by the same pass: an unfilled sink under an
        # overland solve ponds to its rim and sets the published peak depth from
        # a terrain artifact the routing does not believe in.
        mesh_op("set_bed", source=DATA.bed, condition="pit_fill"),
        # THE OUTLET, as the domain's own match measured it: the stretch of the
        # divide the terrain drains through, cut at the snapped pour point,
        # which rides on the domain row rather than a row of its own. Its
        # type is rating_curve, so the quad prescribes a water LEVEL and the
        # curve beside the deck is what that level is read off - the outlet
        # rises and falls with the hydrograph instead of standing at the
        # boundary file's zero. The all-KSORT free exit is not the alternative:
        # it is well-posed only while the normal velocity leaves, and
        # propin_telemac2d.f refuses an entering one by name.
        mesh_op("set_boundary_roles", runs=DATA.domain),
    ],
)


#: The stage-discharge curve the outlet holds: a normal depth over the section
#: that face cuts, swept over the flow range this storm can produce, at the
#: roughness the deck writes at those same nodes - a level read off another law
#: is a level this run never sits at.
_OUTLET = Measured(
    "outlet", kind="rating",
    asked={"landcover": DATA.landcover,
           "roughness": LANDCOVER_CN_MANNING,
           "unmapped": LANDCOVER_UNMAPPED,
           "mm_per_day": PARAMS.design_storm_mm_per_day,
           "series": PARAMS.rain_series_mm,
           "record": Ref("rain.precip_mm")})


class STEERING(T2D):
    """The deck: rain at every node, infiltration under it, one outlet below."""

    GEOMETRY_FILE = _GEOMETRY
    BOUNDARY_CONDITIONS_FILE = _BOUNDARY
    RESULTS_FILE = _RESULT
    TITLE = Ref("settled.title")

    # HOW OFTEN the result is written, in SOLVER STEPS. The engine's own
    # default is every step, so an unwritten period is a frame per step: at
    # the 40 m default edge the CFL step is 1 s, and the DURATION below is
    # about 43,200 of them - one frame every 900 steps is 48 frames of the
    # storm. A user who wants another cadence sets the keyword by its own name.
    GRAPHIC_PRINTOUT_PERIOD = 900
    # The mass balance the runoff answer is read off is printed in the listing,
    # so it is printed on the same beat the frames are written on.
    LISTING_PRINTOUT_PERIOD = 900
    # TWELVE HOURS, in seconds: longer than the storm below, so the catchment
    # drains inside the window and the recession limb is watched rather than
    # inferred. A window that closes while the discharge is still rising is
    # reported as such and its peak is a LOWER BOUND.
    DURATION = 43200.0
    # The step the catchment is solved at follows the edge the accepted mesh was
    # BUILT at rather than the edge that was asked for: an overland sheet is
    # CFL-tight, and the channel band is the finest ground in the domain.
    TIME_STEP = Ref("settled.time_step_s")

    # The catchment starts DRY, which is the dictionary's own initial condition,
    # and it carries no tracer: the outlet hydrograph is the product.
    TYPE_OF_ADVECTION = [1, 5]
    SUPG_OPTION = [0, 0]
    MASS_LUMPING_ON_H = 1.0
    CONTINUITY_CORRECTION = True
    SOLVER = 1
    SOLVER_ACCURACY = 1.0e-6
    MAXIMUM_NUMBER_OF_ITERATIONS_FOR_SOLVER = 200
    IMPLICITATION_FOR_DEPTH = 0.6
    IMPLICITATION_FOR_VELOCITY = 0.6
    # An overland sheet is thin and its free surface follows the ground, so the
    # gradient the engine reads has to be compatible with the bed it runs over.
    FREE_SURFACE_GRADIENT_COMPATIBILITY = 0.9
    MASS_BALANCE = True

    #: MANNING. The infiltration surface below writes ONE table for both of its
    #: columns, and its roughness column is Manning n, so the law the zones it
    #: writes are read under is the Manning one. The engine's own dictionary
    #: gives this keyword no default, so a deck leaving it unwritten reads a
    #: Manning roughness table under whatever law the build starts at.
    LAW_OF_BOTTOM_FRICTION = 4

    #: The SCS Curve Number method, out of the four rainfall-runoff models the
    #: engine offers, is what the curve-number field below is a field FOR.
    RAINFALL_RUNOFF_MODEL = 1

    #: The storm at every wet node, and the engine's own SCS-CN infiltration
    #: under it. A series the caller states wins; else the hours the record
    #: published. Either drives the run through the block file with a dry tail
    #: past the last simulated instant; with neither, the constant design rate
    #: stops when its own window closes, so the catchment drains and the
    #: recession limb appears.
    storm = Storm(mm_per_day=P.design_storm_mm_per_day,
                  # DURATION OF RAIN OR EVAPORATION IN HOURS: six hours, half
                  # the window above, so the design storm CLOSES inside the run
                  # and the catchment has as long again to drain. The composite
                  # states the keyword only where the window closes, because a
                  # storm outlasting the horizon has no end to write down.
                  hours=6.0,
                  until_s=Ref("settled.until_s"), series=P.rain_series_mm,
                  record=Ref("rain.precip_mm"),
                  tracers=0, fortran=RAINDEF3_USER_FORTRAN)
    #: The infiltration surface, read off the land cover at the accepted mesh's
    #: own nodes when the sheet is filled: the curve number the engine
    #: interpolates and the Manning zones it runs over, one table for both.
    infiltration = Infiltration(
        mesh=Ref("mesh"), landcover=DATA.landcover,
        table=LANDCOVER_CN_MANNING, unmapped=LANDCOVER_UNMAPPED,
        uniform_cn=P.curve_number,
        steep_slope_correction=P.steep_slope_correction,
        # ANTECEDENT MOISTURE CONDITIONS: AMC II, the mid condition the curve
        # numbers in the table are published against, so the field and the
        # condition it is read under come off the same publication.
        antecedent_moisture=2,
        # The standard initial abstraction, Ia/S = 0.2, the ratio the curve
        # numbers in the table were published against.
        initial_abstraction=1)

    #: The DERIVED stage-discharge curve the outlet holds. ``bord.f`` reads it at
    #: every prescribed-depth boundary whose entry is 1, interpolates the
    #: elevation against that boundary's own measured flux and relaxes the depth
    #: toward it, so the outlet level rises and falls with the storm instead of
    #: standing at the boundary file's zero.
    rating = Rating(measured=_OUTLET)


#: What this question PLACES: the flux the engine printed across the outlet the
#: user gave, as the hydrograph - charted, and on the map as the station that
#: carries it.
OUTPUTS = [
    series("FLUX", at=P.pour_point).chart(),
    series("FLUX", at=P.pour_point).station(),
]
CAPTIONS = {"FLUX": "outlet hydrograph"}

#: The run's ANSWER, as the numbers a reader has to be able to check, each a
#: measure of one of the reads above. The volumes are the engine's own final
#: balance: what fell on the meshed catchment, what left through its boundary,
#: and the ratio.
ANSWER = {
    "catchment_area_km2": extent().measure("area_km2"),
    "peak_discharge_m3s": series("FLUX", at=P.pour_point).measure("max"),
    "peak_discharge_time_s": series("FLUX", at=P.pour_point).measure("t_max"),
    "peak_is_window_truncated": series("FLUX", at=P.pour_point).measure("truncated"),
    "rainfall_volume_m3": mass_balance().measure("rain_volume_m3"),
    "runoff_volume_m3": mass_balance().measure("outflow_volume_m3"),
    "runoff_coefficient": mass_balance().measure("runoff_coefficient"),
    "max_depth_peak_m": max_over_time("H").measure("max"),
    "max_depth_p99_m": max_over_time("H").measure("p99"),
    "continuity_rel_error": mass_balance().measure("continuity_rel_error"),
    "n_frames": field("H", t="every").measure("frames"),
    "mesh_size_m": mesh().measure("size_m"),
    "mesh_node_count": mesh().measure("nodes"),
    "mesh_element_count": mesh().measure("elements"),
    "domain_bbox": extent().measure("bbox"),
}


#: DECLARED mesh_resolution_m range. 5 m is the finest the catchment triangulator
#: authors; below it a screening runoff field gains nothing the bed does not
#: already blur. There is no fixed coarse ceiling here - ``mesh_max_edge_m`` is
#: the hillslope end of the same band and is declared separately.
_RES_SPEC = ResolutionSpec(
    param="mesh_resolution_m",
    unit="m",
    min_value=5.0,
    native_hint="USGS 3DEP bare-earth bed (10 m) + the NHDPlus HR channel network",
    constraint_source="solver",
    rationale=(
        "finest triangle edge in the channel band; the hillslopes coarsen toward "
        "mesh_max_edge_m under the declared gradation. Peak depth and flooded "
        "extent are resolution-bound classes, so a coarse mesh reads both low"
    ),
)

_METADATA = AtomicToolMetadata(
    name="telemac_rain_on_grid",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    engine="telemac",
    tier="template",
    resolution_specs=(_RES_SPEC,),
)


#: The title the card carries when the run is held for review.
REVIEW_TITLE = "Review the storm, the catchment and the mesh band"


telemac_rain_on_grid = register_workflow(
    TelemacWorkflow, _METADATA, sys.modules[__name__],
    # The moment a scenario is read at is seated for every template that reads a
    # dated source; this run reads none, so it is not asked for.
    levers=("compute_class",),
    # The overland sheet's deepest point and the hydrograph crest are magnitude
    # maxima that live inside single elements, and a coarse element averages both
    # away. WHEN the crest arrives moves with the elements that route the water
    # to it.
    sensitivity=(("max_depth_peak_m", "peak"),
                 ("peak_discharge_m3s", "peak"),
                 ("peak_discharge_time_s", "location")),
    coerce=(
        # Both routes to a drawn value go through one normalizer: the draw gate
        # seats what the canvas returns and this seats what the model typed, and
        # a point that arrived either way means the same outlet.
        point_arg("pour_point", tool="telemac_rain_on_grid",
                  prompt="Click the catchment outlet the runoff drains to",
                  code="TELEMAC_ROG_PARAMS_INVALID"),
        compute_class(),
    ),
)
