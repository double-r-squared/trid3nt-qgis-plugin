"""Engine template ``telemac_rain_on_grid`` - a storm over a delineated watershed.

APPLICABILITY (Godara, Bruland and Alfredsen 2024, Front. Water 6:1384205):
single-storm flash floods in small steep catchments; infiltrated water is
permanently lost, so there is no subsurface return flow and no baseflow."""

from __future__ import annotations

from trid3nt_contracts.telemac_contracts import TELEMAC_MAX_DEPTH_STYLE
from trid3nt_contracts.tool_registry import AtomicToolMetadata, ResolutionSpec

from trid3nt_server.workflows.runtime import (
    ParamRef,
    Ref,
    Step,
    register_workflow,
)
from trid3nt_server.workflows.mesh.tool import mesh_op, tool
from trid3nt_server.inputs import point_arg, user_input
from trid3nt_server.inputs.aoi import AcquireAoi
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
    Hyetograph,
    Infiltration,
    Rain,
    Rating,
)
from trid3nt_server.workflows.solver.compute_class import compute_class
from trid3nt_server.workflows.telemac.templates.rain_on_grid.declarations import (
    DOC,
    LANDCOVER_CN_MANNING,
    LANDCOVER_UNMAPPED,
    NLCD_NATIVE_RESOLUTION_M,
    PARAMS,
    PARAMS as P,
    POUR_POINT_BUFFER_DEG,
)
from trid3nt_server.workflows.telemac.workflow import Door, TelemacWorkflow

__all__ = ["ANSWER", "CAPTIONS", "DATA", "MESH", "OUTPUTS", "PARAMS", "STEERING",
           "telemac_rain_on_grid"]

_AUTHORING = "trid3nt_server.workflows.telemac.authoring"
_TEMPLATE = "trid3nt_server.workflows.telemac.templates.rain_on_grid"
_ENGINE = "trid3nt_server.workflows.telemac.engine"

_CODE = "TELEMAC_ROG_PARAMS_INVALID"

#: The names the run directory holds a catchment's files under - the deck's own
#: GEOMETRY / BOUNDARY CONDITIONS / RESULTS statements.
_GEOMETRY = "rog.slf"
_BOUNDARY = "rog.cli"
_RESULT = "r2d_rog.slf"
_STEERING_FILE = "t2d_rog.cas"

#: Where the image BAKES the RAINDEF=3 copy of the engine's own
#: ``runoff_scs_cn.f``. The installed source hardcodes ``RAINDEF=1`` as a
#: compile-time PARAMETER, which no steering keyword can reach, so a
#: time-varying gross hyetograph needs the engine's own user-fortran door. The
#: patch is made ONCE at build time and the run STAGES it; a constant-rain run
#: names nothing here and stages nothing.
RAINDEF3_USER_FORTRAN = "/opt/trid3nt/user_fortran/raindef3"


#: What the run consumes from the world. Every world-read is declared here rather
#: than performed in a step: the fetcher router's cache, ladders, provenance and
#: typed refusals live once, and a producer is where that middleware is reached
#: from. A mesh the caller SUPPLIES is not among them - that is the mesh router's
#: question, asked once at the build door and never again inside a template.
class DATA:
    # 3DEP is PINNED, not preferred: a DSM (Copernicus GLO-30 includes forest
    # canopy) puts the bed on the tree tops and routes the water down the wrong
    # slopes. A pinned source never switches, so a 3DEP outage surfaces the
    # fetcher's own typed error naming copernicus and the substitution is the
    # user's to make - which is what a cross-dataset swap has to be.
    dem = tool("fetch_dem", bbox=Ref("aoi.bbox"), source="3dep",
               resolution_m=P.bed_dem_resolution_m, purpose="mesh bed")
    rivers = tool("fetch_river_geometry", bbox=Ref("aoi.bbox"),
                  source=P.river_source, purpose="river geometry")
    landcover = tool("fetch_landcover", bbox=Ref("aoi.bbox"),
                     dataset=P.landcover_dataset,
                     resolution_m=NLCD_NATIVE_RESOLUTION_M,
                     purpose="land cover")
    rain = tool(f"{_TEMPLATE}.storm.resolve_rain_event",
                window=P.rain_window,
                intensity_mm_per_hr=P.design_storm_mm_per_hr,
                storm_duration_hr=P.storm_duration_hr,
                sim_duration_hr=P.sim_duration_hr)
    # THE DOMAIN, narrowed by CHAINING tools rather than by a mesher that grew a
    # delineation of its own. The basin is the terrain's answer at the outlet,
    # off the same bare-earth bed the nodes are sampled from - one acquisition,
    # so the delineation and the elevations cannot describe two different grounds.
    # The snap window is the delineation tool's own declared default: how far a
    # clicked outlet may move to reach the channel is a fact about the D8 grid,
    # which is where it is declared.
    basin = tool("delineate_watershed", pour_point=[Ref("aoi.lon"), Ref("aoi.lat")],
                 dem_uri=Ref("dem.uri"))


#: The MESH RECIPE, frozen at declaration and building nothing at import. The
#: extent is the CHAIN's product - the delineated basin - so the mesher
#: triangulates a domain another tool measured rather than delineating one
#: itself, and the channel network the mesh is refined TOWARD is named by the
#: sizing op rather than folded into the domain.
MESH = tool.build_mesh(
    mesher="om2d",
    kind="unstructured_tri",
    extent=Ref("basin"),
    resolution_m=P.mesh_min_edge_m,
    ops=[
        # Fine along the channel network, coarsening away from it, then held to
        # a gradation - oceanmesh's own sizing functions under its own names.
        mesh_op("distance_sizing_from_line_function", line_file=DATA.rivers,
                rate=P.mesh_grade, max_edge_length=P.mesh_max_edge_m),
        mesh_op("enforce_mesh_gradation", gradation=P.mesh_grade),
        mesh_op("delete_boundary_faces"),
        mesh_op("delete_faces_connected_to_one_face"),
        mesh_op("laplacian2"),
        mesh_op("make_mesh_boundaries_traversable"),
        mesh_op("fix_mesh", delete_unused=True),
        # ONE GROUND. The basin above was delineated on the pit-filled surface of
        # this same DEM, so the bed the nodes are painted from is conditioned by
        # the same chain: an unfilled sink under an overland solve ponds to its
        # rim and sets the published peak depth from a terrain artifact the
        # routing does not believe in. A bare-earth DEM is the correct class for
        # an OVERLAND domain - there is no channel bottom under a hillslope.
        mesh_op("set_bed", source=DATA.dem, condition="pit_fill"),
        # THE OUTLET: the delineation's own accumulation-SNAPPED pour point,
        # which is the point on the basin's boundary the terrain drains through.
        # Every boundary node within the mesh's own mean boundary edge of it
        # takes the role, and the hydrograph is the flux across exactly those
        # nodes.
        #
        # A subcritical outlet needs ONE fact from outside, and the RATING CURVE
        # role is where it comes from: the quad prescribes a water LEVEL and the
        # run derives the Z(Q) that level is read off - a normal depth over the
        # section this face cuts, swept over the flow range the storm can
        # produce - so the outlet rises and falls with the hydrograph. The
        # all-KSORT free exit is not the alternative: it is well-posed only while
        # the normal velocity leaves, and propin_telemac2d.f refuses an entering
        # one by name.
        mesh_op("set_boundary_roles",
                rating_curve={"type": "Point",
                              "coordinates": Ref("basin.snapped_pour_point")}),
    ],
)


class STEERING(T2D):
    """The deck: rain at every node, infiltration under it, one outlet below."""

    GEOMETRY_FILE = _GEOMETRY
    BOUNDARY_CONDITIONS_FILE = _BOUNDARY
    RESULTS_FILE = _RESULT
    TITLE = Ref("settled.title")

    VARIABLES_FOR_GRAPHIC_PRINTOUTS = "U,V,H,S,B"
    GRAPHIC_PRINTOUT_PERIOD = Ref("settled.graphic_period")
    LISTING_PRINTOUT_PERIOD = Ref("settled.graphic_period")
    DURATION = Ref("settled.duration_s")
    TIME_STEP = P.time_step_s

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

    #: The SCS Curve Number method, out of the four rainfall-runoff models the
    #: engine offers, is what the curve-number field below is a field FOR.
    RAINFALL_RUNOFF_MODEL = 1

    #: The storm at every wet node, and the engine's own SCS-CN infiltration
    #: under it. A constant design rate stops when the rain window closes so the
    #: catchment drains and the recession limb appears; a real hyetograph brings
    #: its own dry tail and states no window.
    rain = Rain(mm_per_day=Ref("settled.rain_mm_per_day"), tracers=0,
                hours=Ref("settled.rain_hours"))
    #: The infiltration surface, read off the land cover at the accepted mesh's
    #: own nodes when the sheet is filled: the curve number the engine
    #: interpolates and the Manning zones it runs over, one table for both.
    infiltration = Infiltration(
        mesh=Ref("mesh"), landcover=Ref("settled.landcover"),
        table=LANDCOVER_CN_MANNING, unmapped=LANDCOVER_UNMAPPED,
        uniform_cn=P.curve_number,
        steep_slope_correction=P.steep_slope_correction,
        antecedent_moisture=P.antecedent_moisture,
        # The standard initial abstraction, Ia/S = 0.2, the ratio the curve
        # numbers in the table were published against.
        initial_abstraction=1)
    hyetograph = Hyetograph(blocks=Ref("settled.hyetograph_blocks"),
                            until_s=Ref("settled.duration_s"),
                            fortran=RAINDEF3_USER_FORTRAN)

    #: The DERIVED stage-discharge curve the outlet holds. ``bord.f`` reads it at
    #: every prescribed-depth boundary whose entry is 1, interpolates the
    #: elevation against that boundary's own measured flux and relaxes the depth
    #: toward it, so the outlet level rises and falls with the storm instead of
    #: standing at the boundary file's zero.
    rating = Rating(at_boundary=Ref("settled.rating.at_boundary"),
                    of_boundaries=Ref("settled.rating.of_boundaries"),
                    rows=Ref("settled.rating.rows"),
                    note=Ref("settled.rating.note"))


#: What the solved run is read for: the depth over time as the animation, its
#: envelope as the map, and the flux the engine printed across the outlet as the
#: hydrograph - charted, and placed on the map as the station that carries it.
OUTPUTS = [
    field("H", t="every").animate(),
    max_over_time("H").layer(style=TELEMAC_MAX_DEPTH_STYLE),
    series("FLUX", at=P.pour_point).chart(),
    series("FLUX", at=P.pour_point).station(),
]
CAPTIONS = {"H": "water depth", "FLUX": "outlet hydrograph"}

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


#: DECLARED mesh_min_edge_m range. 5 m is the finest the catchment triangulator
#: authors; below it a screening runoff field gains nothing the bed does not
#: already blur. There is no fixed coarse ceiling here - ``mesh_max_edge_m`` is
#: the hillslope end of the same band and is declared separately.
_ROG_RES_SPEC = ResolutionSpec(
    param="mesh_min_edge_m",
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

_ROG_METADATA = AtomicToolMetadata(
    name="telemac_rain_on_grid",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="telemac",
    tier="template",
    resolution_specs=(_ROG_RES_SPEC,),
)


telemac_rain_on_grid = register_workflow(
    TelemacWorkflow, _ROG_METADATA, PARAMS,
    Door(
        steering=STEERING,
        # The OUTLET first, then the analysis window around it: the basin's shape
        # is the terrain's answer rather than the geocoder's, so a place bbox
        # cannot bound it.
        domain=(AcquireAoi(location=P.location, bbox=P.bbox, around=P.pour_point,
                           half_deg=POUR_POINT_BUFFER_DEG,
                           default_name="watershed",
                           code_prefix="TELEMAC_ROG").named("aoi"),),
        mesh=MESH, mesh_on="aoi",
        settle=Step(runner=f"{_AUTHORING}.assembler.settle_catchment",
                    stage="author",
                    kwargs={"catchment": Ref("mesh"),
                            "rain": DATA.rain,
                            "landcover": DATA.landcover,
                            "roughness": LANDCOVER_CN_MANNING,
                            "unmapped": LANDCOVER_UNMAPPED,
                            "time_step_s": ParamRef("time_step_s"),
                            "mesh_resolution_m": ParamRef("mesh_min_edge_m"),
                            "output_interval_min": ParamRef("output_interval_min")}),
        results=(_RESULT,),
        steering_file=_STEERING_FILE, prefix="telemac_rog",
        dispatch=f"{_ENGINE}.solve_case", compute_class=P.compute_class,
        outputs=OUTPUTS, captions=CAPTIONS, answer=ANSWER,
        review_title="Review the storm, the catchment and the mesh band"),
    data=DATA,
    answer=tuple(ANSWER),
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
                  code=_CODE),
        user_input.bbox("bbox", label="analysis AOI", code=_CODE),
        compute_class(),
    ),
    doc=DOC,
)
