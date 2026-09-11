"""Engine template ``telemac3d_stratified_flow`` - what a 2D model cannot see.

TELEMAC-3D over sigma layers with active-tracer baroclinic coupling. The run has
NO surface heat exchange, so a falling surface temperature is the warm layer
MIXING DOWNWARD. The basin is CLOSED: it names no liquid boundary."""

from __future__ import annotations

from trid3nt_contracts.telemac_contracts import TELEMAC3D_STRATIFICATION_STYLE
from trid3nt_contracts.tool_registry import AtomicToolMetadata, ResolutionSpec

from trid3nt_server.workflows.runtime import ParamRef, Ref, Step, register_workflow
from trid3nt_server.workflows.mesh.tool import mesh_op, tool
from trid3nt_server.workflows.inputs.aoi import AcquireAoi, location_or_bbox
from trid3nt_server.workflows.telemac.authoring.assembler import (
    BASIN_BOUNDARY,
    BASIN_GEOMETRY,
)
from trid3nt_server.workflows.telemac.modules import column, field, mesh
from trid3nt_server.workflows.telemac.modules.telemac3d import (
    T3D,
    Column,
    VerticalGrid,
    Wind,
)
from trid3nt_server.workflows.telemac.solving.solve import compute_class
from trid3nt_server.workflows.telemac.templates.stratified_flow.declarations import (
    BASIN_HALF_DEG,
    DOC,
    PARAMS,
    PARAMS as P,
)
from trid3nt_server.workflows.telemac.workflow import Door, TelemacWorkflow

__all__ = ["ANSWER", "CAPTIONS", "DATA", "MESH", "OUTPUTS", "PARAMS", "STEERING",
           "telemac3d_stratified_flow"]

_AUTHORING = "trid3nt_server.workflows.telemac.authoring"
_TEMPLATE = "trid3nt_server.workflows.telemac.templates.stratified_flow"
_SOLVING = "trid3nt_server.workflows.telemac.solving.solve"

#: What the run directory holds the run's files under - the deck's own 3D and 2D
#: RESULT FILE statements. The 3D file is the answer; the 2D file is the depth
#: average the question exists to refuse, written because the engine's own
#: printouts read it.
#: The two documents the free surface arithmetic is done between, named once so
#: the rows that fetch them and the step that compares their stated datums cannot
#: drift apart.
_BED = "fetch_greatlakes_bathymetry"
_GAUGE = "fetch_greatlakes_water_level"

_RESULT_3D = "res3d_basin.slf"
_RESULT_2D = "res2d_basin.slf"
_STEERING_FILE = "t3d_basin.cas"


class DATA:
    """What the run consumes from the world: the water body, narrowed to the ask.

    The narrowing is the CHAIN's; the bed is fetched ONCE and read twice."""

    water = tool("fetch_nhd_waterbodies", bbox=Ref("aoi.bbox"))
    mapped = tool("section", polygon=water, within=Ref("aoi.bbox"))
    # THE SUBSTITUTION, declared where a reader can see it. A bed is TOPOBATHY
    # and the coastal CUDEM composite does not reach the Great Lakes at all - its
    # own ladder refuses there - so this row names the NCEI bathymetry the lakes
    # are charted on instead. Same data class, different survey, one stated
    # datum: the basin is solved on the lake's own low water datum, which is
    # where its bed is counted from and where its free surface starts.
    bed = tool(_BED, bbox=Ref("aoi.bbox"))
    # THE DAY the level is read over, and the gauges that watched it. A lake has
    # a level and the run opens at it: the free surface is an OBSERVATION here,
    # the way the carrier discharge is on a river, and it is read on the SAME
    # datum the bed above is charted on.
    day = tool(f"{_TEMPLATE}.lake_level.reading_day",
               event_time=ParamRef("event_time"))
    level = tool(_GAUGE, bbox=Ref("aoi.bbox"), start_date=Ref("day.date"),
                 end_date=Ref("day.date"))


#: The MESH RECIPE, frozen at declaration and building nothing at import. The
#: extent is the CHAIN's product - the mapped water body cut to the AOI and then
#: to the part of it the bed survey actually sounded - so the mesher triangulates
#: a domain another tool measured. No stretch of its boundary is designated
#: liquid, which is what a lake IS.
MESH = tool.build_mesh(
    mesher="om2d",
    kind="unstructured_tri",
    extent=Ref("basin.domain"),
    resolution_m=P.mesh_min_edge_m,
    ops=[
        mesh_op("set_rim_size"),
        mesh_op("delete_boundary_faces"),
        mesh_op("delete_faces_connected_to_one_face"),
        mesh_op("laplacian2"),
        mesh_op("make_mesh_boundaries_traversable"),
        mesh_op("fix_mesh", delete_unused=True),
        # The SAME survey the domain was clipped to, so no node can land where
        # the clip said nothing was measured.
        mesh_op("set_bed", source=DATA.bed),
    ],
)


class STEERING(T3D):
    """The deck: a prescribed column, a steady wind, and the planes to hold them."""

    TITLE = Ref("settled.title")
    GEOMETRY_FILE = BASIN_GEOMETRY
    BOUNDARY_CONDITIONS_FILE = BASIN_BOUNDARY
    RD_RESULT_FILE = _RESULT_3D
    ED_RESULT_FILE = _RESULT_2D
    VARIABLES_FOR_3D_GRAPHIC_PRINTOUTS = "Z,U,V,W,TA1"

    # The step the basin is solved at follows the edge the accepted mesh was
    # BUILT at, through the CFL producer the river part's step comes from.
    TIME_STEP = Ref("settled.time_step_s")
    NUMBER_OF_TIME_STEPS = Ref("settled.n_steps")
    GRAPHIC_PRINTOUT_PERIOD = Ref("settled.graphic_period")
    LISTING_PRINTOUT_PERIOD = Ref("settled.listing_period")
    MASS_BALANCE = True

    NUMBER_OF_HORIZONTAL_LEVELS = P.levels
    # The dictionary's own default here is YES, and a non-hydrostatic solve of a
    # basin-scale column buys nothing the hydrostatic one does not already show.
    NON_HYDROSTATIC_VERSION = False

    # THE FREE SURFACE the basin opens at: flat, and at the level the gauge
    # OBSERVED. The dictionary's own zero is the chart datum, which is where the
    # bed is counted from - a lake left there has no water on its rim at all.
    INITIAL_CONDITIONS = "CONSTANT ELEVATION"
    INITIAL_ELEVATION = Ref("lake_level.elevation_m")

    # The ONE pair that cannot be left to the dictionary, measured both ways:
    # LECDON stops on "THE LAW OF BOTTOM FRICTION 5 IS ASKED / GIVE THE
    # CORRESPONDING FRICTION COEFFICIENT" when only the law is defaulted, and on
    # "NO FRICTION LAW IS PRESCRIBED!" when only the coefficient is written. It
    # reads the two jointly and takes neither half from the dictionary once the
    # other exists. Both values ARE the dictionary's own; what the engine demands
    # is that they be written.
    LAW_OF_BOTTOM_FRICTION = 5
    FRICTION_COEFFICIENT_FOR_THE_BOTTOM = 0.01

    # The diffusivities a screening basin is stable under. The dictionary's own
    # 1e-6 is molecular; a basin at these scales is not solved at molecular
    # viscosity, and the tracer pair has no default at all.
    COEFFICIENT_FOR_HORIZONTAL_DIFFUSION_OF_VELOCITIES = 1.0e-4
    COEFFICIENT_FOR_VERTICAL_DIFFUSION_OF_VELOCITIES = 1.0e-4
    COEFFICIENT_FOR_HORIZONTAL_DIFFUSION_OF_TRACERS = [1.0e-4]
    COEFFICIENT_FOR_VERTICAL_DIFFUSION_OF_TRACERS = [1.0e-4]

    # THE TRACER IS THE PHYSICS: temperature drives the density and the density
    # drives the flow. The engine finds the temperature by the first sixteen
    # characters of its name; the unit rides in the next sixteen, as the
    # dictionary spells a tracer name, and is what the result file carries. The
    # initial values keyword is mandatory even where the Fortran hook overrides
    # it - the solver stops asking for it by name.
    NUMBER_OF_TRACERS = 1
    NAMES_OF_TRACERS = ["TEMPERATURE     DEGC"]
    INITIAL_VALUES_OF_TRACERS = [0.0]
    DENSITY_LAW = 1
    AVERAGE_WATER_DENSITY = 1000.0

    # HOW THE TEMPERATURE IS CARRIED, and the ceiling that carriage runs under.
    # The dictionary gives the tracer scheme no default, so an unstated deck
    # advects the temperature by whatever the VELOCITIES are advected by; the
    # ceiling governs schemes 13 and 14 and nothing else, so the two are one
    # statement and are written together.
    SCHEME_FOR_ADVECTION_OF_TRACERS = [P.tracer_advection_scheme]
    MAXIMUM_NUMBER_OF_ITERATIONS_FOR_ADVECTION_SCHEMES = \
        P.max_advection_iterations
    # AND UNDER WHICH OPTION. The dictionary's own default here is 4, implicit,
    # and murd3d_pos answers to 1 and 2 only - an unstated deck loops "UNKNOWN
    # OPTION IN MURD3D_POS: 4 / OPTION 1 TAKEN INSTEAD" and stops on the explicit
    # option's own iteration ceiling. 2 is the predictor-corrector, which the
    # dictionary's own help calls the faster of the two where there are no tidal
    # flats; a closed lake basin has none.
    SCHEME_OPTION_FOR_ADVECTION_OF_TRACERS = [2]

    #: The sigma grid that can HOLD the declared thermocline over this basin's own
    #: deepest column, or the refusal that says how many planes would.
    vertical_grid = VerticalGrid(levels=P.levels,
                                 max_depth_m=Ref("settled.max_depth_m"),
                                 thermocline_depth_m=P.thermocline_depth_m)
    #: The column the run OPENS with, written into the engine's own initial-
    #: condition hook because no keyword carries a non-uniform tracer field.
    column = Column(levels=P.levels, max_depth_m=Ref("settled.max_depth_m"),
                    thermocline_depth_m=P.thermocline_depth_m,
                    warm_c=P.warm_temp_c, cold_c=P.cold_temp_c,
                    surface_m=Ref("lake_level.elevation_m"))
    #: The wind that decides whether the difference survives. A calm run states
    #: nothing here at all.
    wind = Wind(speed_mps=P.wind_speed_mps, from_deg=P.wind_direction_deg)


#: What the solved run is read for: the temperature on the surface plane and on
#: the bed plane as the pair of maps whose contrast a depth average cannot show,
#: and the column at the deepest node as the chart, the prescribed initial column
#: drawn beside what survived. The run exchanges no heat with the atmosphere, so
#: the two curves enclose the same heat and a surface that fell is the warm
#: layer mixed downward.
OUTPUTS = [
    field("T1", t=-1).layer(style=TELEMAC3D_STRATIFICATION_STYLE),
    field("T1", t=-1, plane=0).layer(style=TELEMAC3D_STRATIFICATION_STYLE),
    column("T1").chart(reference=column("T1", t=0)),
]
CAPTIONS = {"T1": "water temperature"}

#: The run's ANSWER, as the numbers a reader has to be able to check, each a
#: measure of one of the reads above or of the velocity column beside them: the
#: top-to-bottom difference that survived against the one prescribed, the
#: depth-weighted column means whose drift is the numerical error bar on the
#: mixing, and the surface-downwind / return-flow-at-depth pair the same wind
#: drove, whose depth average a 2D model reports as nothing.
ANSWER = {
    "stratification_dt": column("T1").measure("top_minus_bottom"),
    "stratification_dt_init": column("T1", t=0).measure("top_minus_bottom"),
    "column_mean_final_c": column("T1").measure("mean"),
    "column_mean_init_c": column("T1", t=0).measure("mean"),
    "column_depth_m": column("T1").measure("depth_m"),
    "u_surface": column("U").measure("top"),
    "u_bottom": column("U").measure("bottom"),
    "depth_avg_u": column("U").measure("mean"),
    "planes": mesh().measure("planes"),
    "mesh_size_m": mesh().measure("size_m"),
}


_TELEMAC3D_RES_SPEC = ResolutionSpec(
    param="levels",
    unit="planes",
    min_value=5.0,
    native_hint="the thermocline the run declares, which the grid plan must hold",
    constraint_source="solver",
    rationale=(
        "the VERTICAL degree of freedom, which is the one a 2D model has none of. "
        "Too few planes for the declared thermocline over this basin's deepest "
        "column is a refusal naming the count that would work, not a coarser "
        "answer"
    ),
)

_TELEMAC3D_METADATA = AtomicToolMetadata(
    name="telemac3d_stratified_flow",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="telemac",
    tier="template",
    resolution_specs=(_TELEMAC3D_RES_SPEC,),
)


telemac3d_stratified_flow = register_workflow(
    TelemacWorkflow, _TELEMAC3D_METADATA, PARAMS,
    Door(
        steering=STEERING,
        domain=(AcquireAoi(location=P.location, bbox=P.bbox,
                           half_deg=BASIN_HALF_DEG, default_name="basin",
                           code_prefix="TELEMAC3D").named("aoi"),
                # The water the question is about, and then the part of it the
                # bed survey sounded: a water domain has no bed where nobody
                # measured, and meshing the difference builds elements the bed
                # painter has nothing to give.
                Step(runner=f"{_TEMPLATE}.measured_bed.basin_on_measured_bed",
                     stage="prep",
                     kwargs={"polygon": DATA.mapped,
                             "bed": DATA.bed}).named("basin"),),
        mesh=MESH, mesh_on="aoi",
        # The level the basin opens at, before it is settled: the deepest column
        # the vertical grid is planned over is the free surface MINUS the bed,
        # so the surface has to exist before the mesh is measured.
        produce=(Step(runner=f"{_TEMPLATE}.lake_level.observed_lake_level",
                      stage="acquire",
                      kwargs={"level": DATA.level, "gauge_source": _GAUGE,
                              "bed_source": _BED,
                              "aoi": Ref("aoi")}).named("lake_level"),),
        settle=Step(runner=f"{_AUTHORING}.assembler.settle_basin",
                    stage="author",
                    kwargs={"mesh": Ref("mesh"),
                            "warm_temp_c": ParamRef("warm_temp_c"),
                            "cold_temp_c": ParamRef("cold_temp_c"),
                            "thermocline_depth_m": ParamRef("thermocline_depth_m"),
                            "wind_speed_mps": ParamRef("wind_speed_mps"),
                            "wind_direction_deg": ParamRef("wind_direction_deg"),
                            "levels": ParamRef("levels"),
                            "sim_duration_hours": ParamRef("sim_duration_hours"),
                            "time_step_s": ParamRef("time_step_s"),
                            "output_interval_min": ParamRef("output_interval_min"),
                            "surface_m": Ref("lake_level.elevation_m"),
                            "domain_note": Ref("basin.note"),
                            "level_note": Ref("lake_level.note"),
                            "result_basename": _RESULT_3D}),
        results=(_RESULT_3D, _RESULT_2D),
        steering_file=_STEERING_FILE, prefix="telemac3d",
        dispatch=f"{_SOLVING}.solve_case", compute_class=P.compute_class,
        outputs=OUTPUTS, captions=CAPTIONS, answer=ANSWER,
        review_title="Review the prescribed column, the wind and the mesh"),
    data=DATA,
    answer=tuple(ANSWER),
    provenance=(("wind_speed_mps", "wind_note"),
                ("thermocline_depth_m", "thermocline_note"),
                ("levels", "levels_note"),
                ("time_step_s", "time_step_note"),
                ("tracer_advection_scheme", "tracer_advection_note"),
                ("max_advection_iterations", "advection_ceiling_note")),
    # The surface-to-bottom temperature difference is read ACROSS the thermocline,
    # the steepest gradient in the domain, and the planes are what resolve it.
    sensitivity=(("stratification_dt", "gradient"),
                 ("u_surface", "gradient"),
                 ("u_bottom", "gradient")),
    coerce=(
        location_or_bbox("telemac3d_stratified_flow", code_prefix="TELEMAC3D",
                         hint="For a natural prompt like 'does <lake> stratify', "
                              "pass location='<lake>'."),
        compute_class(),
    ),
    doc=DOC,
)
