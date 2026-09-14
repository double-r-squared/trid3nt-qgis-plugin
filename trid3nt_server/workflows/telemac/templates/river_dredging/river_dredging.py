"""Engine template ``telemac_river_dredging`` - a maintenance dredge of a reach.

TELEMAC-2D coupled with GAIA, the dredger driven by NESTOR on the sediment deck:
how much material comes out of the fairway to hold it at grade, where the spoil
goes, and what the bed does around both."""

from __future__ import annotations

from trid3nt_contracts.tool_registry import AtomicToolMetadata, ResolutionSpec

from trid3nt_server.workflows.runtime import (
    Data,
    ParamRef,
    Ref,
    Step,
    register_workflow,
)
from trid3nt_server.workflows.mesh.tool import mesh_op, tool
from trid3nt_server.inputs.aoi import location_or_bbox
from trid3nt_server.workflows.telemac.modules import (
    GAIA,
    T2D,
    field,
    mass_balance,
    mesh,
)
from trid3nt_server.workflows.telemac.modules.gaia import Dig, Dredging, RESULT_FILENAME
from trid3nt_server.workflows.telemac.modules.telemac2d import Boundaries, TimeOrigin
from trid3nt_server.workflows.solver.compute_class import compute_class
from trid3nt_server.workflows.telemac.templates.river_dredging.declarations import (
    ACCEPTS, DOC, PARAMS, PARAMS as P,
)
from trid3nt_server.workflows.telemac.templates import reach
from trid3nt_server.workflows.telemac.workflow import Door, TelemacWorkflow

__all__ = ["ANSWER", "CAPTIONS", "DATA", "MESH", "OUTPUTS", "PARAMS", "STEERING",
           "telemac_river_dredging"]

_AUTHORING = "trid3nt_server.workflows.telemac.authoring"
_REACH = "trid3nt_server.workflows.telemac.templates.reach"
_ENGINE = "trid3nt_server.workflows.telemac.engine"

#: The names the run directory holds this run's files under - the deck's own
#: STEERING / GEOMETRY / BOUNDARY CONDITIONS / RESULTS statements.
_STEERING_FILE = "t2d_river.cas"
_GEOMETRY = "river.slf"
_BOUNDARY = "river.cli"
_RESULT = "r2d_river.slf"

#: How far past the centerline the mapped banks are ASKED for: the water that
#: belongs to this reach reaches past the line, and the pad widens the QUESTION
#: rather than the meshed domain - the section cut keeps only the stretch
#: between the reach's two ends.
_BANK_QUERY_PAD_M = 3000.0

#: Which reference surface every action reads its levels from: the profile file
#: this run authors, which carries the water surface the reach opens at. The
#: alternatives the engine offers - a ZRL variable on the geometry, a level a
#: Save_water_level action captured - are not what this question measures a
#: design depth against.
_REFERENCE_LEVEL = "SECTIONS"


class DATA:
    """The reach chain, one row per artifact, in the order it is read, and the
    two areas the dredge works on, which this template names no source for."""

    rivers = tool(f"{_REACH}.fetch_reach_flowline",
                  prefetched=ParamRef("river_geometry_uri"))
    # THE REACH, narrowed by CHAINING tools rather than by a mesher that grew a
    # corridor of its own. The navigated mainstem names the stretch, its two ends
    # name where the stretch stops, and the cut through the MAPPED banks is the
    # domain - so the two end faces are the transects the inflow and the outflow
    # are prescribed on, measured off real geometry rather than a ribbon.
    centerline = tool("fetch_nhdplus_nldi_navigate",
                      seed_point=[Ref("seed.lon"), Ref("seed.lat")],
                      direction="DM",
                      distance_km=ParamRef("reach_length_km"))
    ends = tool("endpoints", line=centerline)
    window = tool("compute_layer_bounds", layer_uri=centerline,
                  pad_m=_BANK_QUERY_PAD_M, fit_map=False)
    water = tool("fetch_nhd_area_water", bbox=Ref("window.bbox"))
    # HOW MUCH of the reach the returned polygons actually map, measured before
    # the cut so an unmapped reach refuses on its own cause instead of arriving
    # at the section as an empty geometry.
    mapped_water = tool(f"{_REACH}.measure_water_coverage",
                        water=water, centerline=centerline)
    reach_polygon = tool("section", polygon=mapped_water,
                         between=Ref("ends.between"))
    # THE SUBSTITUTION, declared where a reader can see it. A bed is TOPOBATHY -
    # the channel bottom - and no topobathy survey covers an inland reach, so
    # this row is a surface DEM and the recipe below says so by painting the bed
    # from it BY NAME. The consequence travels with it: a surface measures the
    # water top, so the modelled channel is shallower than the real one, the
    # design depth is measured against the surface the run opens at rather than
    # a chart datum, and the journal names this row as what the bed came from.
    dem = tool("fetch_copernicus_dem", bbox=Ref("window.bbox"), px_per_deg=3600.0,
               purpose="river bed elevation")
    #: WHERE the dredger works, and where the spoil goes. Two SLOTS: the channel
    #: a port keeps at grade and the disposal ground it is licensed to use are
    #: both administrative areas, and no dataset knows either - they are drawn,
    #: or handed over as the port's own layers.
    dredge_area = Data.supplied(geometry="polygon")
    dump_area = Data.supplied(geometry="polygon")


#: The MESH RECIPE, frozen at declaration and building nothing at import. The
#: extent is the CHAIN's product - the stretch of mapped water the section cut
#: between the centerline's two ends - so the mesher triangulates a domain other
#: tools measured rather than growing a corridor of its own. The ops are
#: oceanmesh's own clean passes under its own names, then the two things we
#: impose: the bed, painted from the substitution declared above and named in the
#: journal, and the roles, prescribed across the two end transects the section cut.
MESH = tool.build_mesh(
    mesher="om2d",
    kind="unstructured_tri",
    extent=Ref("reach_polygon"),
    resolution_m=ParamRef("mesh_resolution_m"),
    ops=[
        mesh_op("delete_boundary_faces"),
        mesh_op("delete_faces_connected_to_one_face"),
        mesh_op("laplacian2"),
        mesh_op("make_mesh_boundaries_traversable"),
        mesh_op("fix_mesh", delete_unused=True),
        mesh_op("set_bed", source=DATA.dem, interp="nearest"),
        mesh_op("set_boundary_roles",
                inflow=Ref("reach_polygon.face_start"),
                outflow=Ref("reach_polygon.face_end")),
    ],
)


class STEERING(T2D):
    """The deck: a river over a mobile bed, with a dredger working in it."""

    GEOMETRY_FILE = _GEOMETRY
    BOUNDARY_CONDITIONS_FILE = _BOUNDARY
    RESULTS_FILE = _RESULT
    TITLE = Ref("settled.title")

    # The step the reach is solved at follows the edge the accepted mesh was
    # BUILT at rather than the edge that was asked for.
    TIME_STEP = Ref("settled.time_step_s")
    LISTING_PRINTOUT_PERIOD = 500

    # The run OPENS at the derived normal depth, laid bed-parallel. Not a
    # constant elevation at the outflow stage: the stage is derived only where
    # the reach FALLS, so a horizontal surface at the outlet's level leaves every
    # node upstream of it dry - the flowrate face among them - and the engine
    # refuses a discharge it has no water to impose. It is also the surface the
    # dredge's own reference profiles are laid at.
    INITIAL_CONDITIONS = "CONSTANT DEPTH"
    INITIAL_DEPTH = Ref("settled.depth_m")

    # The roughness the outflow stage was DERIVED at, written back out as the
    # roughness the run is solved at. One number, stated once: a stage derived at
    # one and written at another is a level the run never sits at.
    LAW_OF_BOTTOM_FRICTION = Ref("settled.friction_law")
    FRICTION_COEFFICIENT = Ref("settled.friction_coefficient")

    # The advection of momentum and depth, and the SUPG the reach is stable
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

    # The engine accounts for its own water volume and prints one flux per liquid
    # boundary; the sediment side of the same closure is GAIA's, and the dredge's
    # own volumes are printed into the same listing.
    MASS_BALANCE = True

    VARIABLES_FOR_GRAPHIC_PRINTOUTS = "U,V,H,S,B"
    GRAPHIC_PRINTOUT_PERIOD = Ref("settled.graphic_period")
    DURATION = P.sim_duration_s

    #: The clock every dredging action is dated against. NESTOR reads absolute
    #: dates and differences them against THIS origin, so the deck states it
    #: rather than inheriting the dictionary's own.
    time_origin = TimeOrigin(at=P.time_origin)

    #: No tracer: a dredge is a question about the bed, so every liquid boundary
    #: carries the measured flowrate and stage and nothing else.
    boundaries = Boundaries(measured=Ref("settled"), tracers=[])

    #: The bed the dredger cuts into, and the dredger itself. One class, bedload
    #: on, a real stock: the material the criterion dig takes out of the fairway
    #: and the rate dump lays into the spoil ground both go through GAIA's own
    #: per-class mass evolution, which is what its bed evolution and its sediment
    #: balance are computed from.
    coupling = [GAIA.bed(
        geometry=_GEOMETRY, boundary=_BOUNDARY, mass_balance=True,
        gradation=None, presets={}, d50_um=P.grain_size_um,
        thickness_m=P.bed_thickness_m, formula=P.bedload_formula,
        hiding_factor_formula=1, morphological_factor=P.morphological_factor,
        printouts="B,E", mixture_printouts="B,E,D50",
        dredging=Dredging(
            actions=[Dig(field=Ref("dredge.dredge_area"),
                         level=_REFERENCE_LEVEL,
                         start=P.dredge_start_s, end=P.dredge_end_s,
                         repeat=P.dredge_repeat_s,
                         rate=P.dig_rate_m_per_s,
                         depth=P.design_depth_m,
                         crit_depth=P.trigger_depth_m,
                         min_volume=P.min_volume_m3,
                         # The radius a minimum volume is gathered over is the
                         # mesh's own measured edge: it is the length below which
                         # this run resolves nothing anyway.
                         min_volume_radius=Ref("settled.mesh_size_m"),
                         dump=Ref("dredge.dump_area"),
                         dump_rate=P.dump_rate_m_per_s)],
            reference=Ref("dredge.profiles"),
            origin=P.time_origin))]


#: The GAIA bed-evolution field, in the metres the module writes: deposition
#: positive, the dredged cut negative, so the ramp diverges about zero and the
#: legend is ranged symmetrically about that centre.
BED_EVOLUTION_STYLE = {"kind": "mesh", "ramp": "rdbu", "units": "m",
                       "center": 0.0}
#: The bed the run ends on, as terrain rather than as change.
BED_STYLE = {"kind": "mesh", "ramp": "terrain", "units": "m"}

#: What the solved run is read for: the bed's cumulative evolution off GAIA's own
#: result as the map and, over time, as the animation - the CHANGE is the answer,
#: where the bed's absolute relief is terrain the run did not make - and the
#: host's own bottom beside them, which is the channel the dredge left.
OUTPUTS = [
    field("E", t=-1, module="gaia").layer(style=BED_EVOLUTION_STYLE),
    field("E", t="every", module="gaia").animate(),
    field("B", t=-1).layer(style=BED_STYLE),
]
CAPTIONS = {"E": "bed evolution", "B": "bed elevation"}

#: The run's ANSWER. The two volumes are the engine's OWN report lines, summed
#: over the maintenance passes it printed; the two bed changes are the evolution
#: field read inside each area, so the cut is the dredged area's minimum and the
#: heap is the spoil ground's maximum. A pass that does not finish inside the
#: run's clock prints no volume at all, so the report states why the two volumes
#: read as they do rather than leaving a reader with an unexplained blank.
ANSWER = {
    "dug_volume_m3": mass_balance(module="gaia").measure("dug_volume_m3"),
    "dumped_volume_m3": mass_balance(module="gaia").measure("dumped_volume_m3"),
    "dredge_report": mass_balance(module="gaia").measure("dredge_report"),
    "dredged_bed_change_m": field("E", t=-1, module="gaia",
                                  over=DATA.dredge_area).measure("min"),
    "dumped_bed_change_m": field("E", t=-1, module="gaia",
                                 over=DATA.dump_area).measure("max"),
    "net_bed_mass_kg": mass_balance(module="gaia").measure("sediment_net_bed_mass_kg"),
    "mesh_size_m": mesh().measure("size_m"),
}


#: DECLARED mesh_resolution_m range. The solver floor is the finest edge the mesh
#: builder authors regardless of ask; below it a screening run gains nothing.
_RES_SPEC = ResolutionSpec(
    param="mesh_resolution_m",
    unit="m",
    min_value=3.0,
    native_hint="NHD channel geometry + Copernicus GLO-30 terrain; edge sized "
                "from reach width",
    constraint_source="solver",
    rationale=(
        "explicit target edge length; 3 m is the absolute finest the builder "
        "authors, a long reach is further coarsened under the mesh node budget "
        "(self-labeled); the dredged volume is summed over the nodes inside the "
        "field, so a fairway a few cells wide reads it coarsely"
    ),
)

_METADATA = AtomicToolMetadata(
    name="telemac_river_dredging",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="telemac",
    tier="template",
    resolution_specs=(_RES_SPEC,),
)


telemac_river_dredging = register_workflow(
    TelemacWorkflow, _METADATA,
    PARAMS,
    Door(
        steering=STEERING,
        domain=(reach.Geocode(location=P.location, bbox=P.bbox),
                reach.ReachSeed(reach=Ref("reach"), rivers=DATA.rivers),
                reach.CarrierDischarge(seed=Ref("seed"), explicit=P.discharge_m3s,
                                       event_time=P.event_time)),
        mesh=MESH, mesh_on="reach",
        produce=(reach.MeshCoverage(mesh=Ref("mesh"), centerline=DATA.centerline),),
        settle=Step(runner=f"{_AUTHORING}.assembler.settle_reach", stage="author",
                    kwargs={"reach": Ref("reach"), "seed": Ref("seed"),
                            "mesh": Ref("mesh"), "centerline": DATA.centerline,
                            "carrier_discharge": Ref("carrier_discharge"),
                            "sim_duration_s": P.sim_duration_s,
                            "mesh_resolution_m": P.mesh_resolution_m,
                            "output_interval_min": P.output_interval_min,
                            "friction_law": P.friction_law,
                            "friction_coefficient": P.friction_coefficient}),
        # The two areas and the reference surface are measured AFTER the reach is
        # settled: the surface the design depth is read from is the water surface
        # the settled normal depth puts the run at.
        derive=(Step(runner=f"{_AUTHORING}.assembler.settle_dredge",
                     stage="author",
                     kwargs={"mesh": Ref("mesh"), "centerline": DATA.centerline,
                             "seed": Ref("seed"), "settled": Ref("settled"),
                             "areas": {"dredge_area": DATA.dredge_area,
                                       "dump_area": DATA.dump_area}}
                     ).named("dredge"),),
        results=(_RESULT, RESULT_FILENAME),
        steering_file=_STEERING_FILE, prefix="telemac",
        dispatch=f"{_ENGINE}.solve_case", compute_class=P.compute_class,
        outputs=OUTPUTS, captions=CAPTIONS, answer=ANSWER,
        review_title="Review the dredge, the bed and the mesh"),
    data=DATA,
    accepts=ACCEPTS,
    answer=tuple(ANSWER),
    provenance=(("discharge_m3s", "discharge_note"),
                ("mesh_resolution_m", "mesh_resolution_note")),
    # The dredged volume is a sum over the nodes inside the field, so a coarse
    # mesh resolves a narrow fairway - and the volume it holds - badly.
    sensitivity=(("dug_volume_m3", "peak"),
                 ("dredged_bed_change_m", "peak")),
    coerce=(
        location_or_bbox("telemac_river_dredging", code_prefix="TELEMAC",
                         hint="For a natural prompt like 'how much do we have to "
                              "dredge out of the channel at <place>', pass "
                              "location='<place>' and the two areas as polygons."),
        reach.event_time(),
        compute_class(),
    ),
    doc=DOC,
)
