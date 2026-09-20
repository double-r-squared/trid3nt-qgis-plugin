"""Engine template ``artemis_harbor_agitation`` - does the structure shelter the water.

ARTEMIS is the phase-RESOLVING elliptic mild-slope solver, so the answer is a
steady-state agitation coefficient Kd = Hs/H0 in which diffraction fringes and
standing waves are visible rather than averaged away."""

from __future__ import annotations

import sys

from trid3nt_contracts.tool_registry import AtomicToolMetadata, ResolutionSpec

from trid3nt_server.workflows.runtime import (
    Data,
    ParamRef,
    Ref,
    register_workflow,
)
from trid3nt_server.workflows.mesh.tool import mesh_op, tool
from trid3nt_server.workflows.telemac.authoring.assembler import HARBOUR_GEOMETRY
from trid3nt_server.workflows.telemac.modules import field, mesh
from trid3nt_server.workflows.telemac.modules.outputs import profile
from trid3nt_server.workflows.telemac.modules.artemis import (
    ART,
    BOUNDARY_FILENAME,
    IncidentWave,
)
from trid3nt_server.workflows.solver.compute_class import compute_class
from trid3nt_server.workflows.telemac.templates.agitation.declarations import (
    ACCEPTS,
    DOC,
    PARAMS,
    PARAMS as P,
)
from trid3nt_server.workflows.telemac.workflow import (
    Measured, TelemacWorkflow,
)

__all__ = ["ANSWER", "CAPTIONS", "DATA", "MESH", "OUTPUTS", "PARAMS", "STEERING",
           "artemis_harbor_agitation"]


#: What the run directory holds the run's files under - the deck's own GEOMETRY /
#: RESULTS statements. The boundary file is the incident wave's and is named by
#: the composite that writes it.
_RESULT = "res_agitation.slf"
_STEERING_FILE = "art_agitation.cas"


class DATA:
    """What the run consumes from the world: the water, its bed, the structure.

    A harbour basin is the WATER a coastline leaves inside the window the
    question is asked in, so the domain is CUT rather than fetched whole; a user
    who already holds the basin's outline supplies it and the cut never runs.
    The transect is laid through whatever structure the run is handed."""

    #: The window the sheltering question is asked in. Produced on demand, so a
    #: caller who supplies the basin outline below is never asked to draw one.
    box = Data.extent()
    #: The land-water EDGE at survey resolution. OpenStreetMap draws a coastline
    #: way with the land on its LEFT, and that direction is the whole of the
    #: classification the cut below makes.
    coast = Data(tool("fetch_osm_coastline", bbox=Ref("box.bbox")))
    #: THE DOMAIN: the water the coastline leaves inside the box. A polygon is
    #: what a mesh is cut from and a coastline is a line, so this row is the step
    #: between the two; a basin the user outlines supersedes it.
    domain = Data.domain(tool("derive_water_polygon", coastline=coast,
                              extent=Ref("box.bbox")))
    #: TOPOBATHY is the class a bed is defined over and CUDEM's nearshore
    #: collection covers a surveyed harbour, so the bed the wave refracts over is
    #: the surveyed sea floor. The source's own default target is ONE UTM zone,
    #: so a harbour outside it would be warped across ten zones before anything
    #: sampled it; lon/lat is what the nodes are sampled in.
    seafloor = Data(tool("fetch_topobathy", bbox=Ref("domain.bbox"),
                         target_crs="EPSG:4326"))
    #: The LAND-AND-SHORE surface the survey's own footprint stops short of. A
    #: cut water polygon follows the coastline to the metre and a delivered tile
    #: ends on its own grid, so the two disagree by a node at the rim.
    terrain = Data(tool("fetch_dem", bbox=Ref("domain.bbox"),
                        purpose="bed elevation"))
    #: ONE bed: the survey where it sounded, the terrain everywhere else, both on
    #: NAVD88 - and the merge refuses two datums by name rather than writing a
    #: step into the floor a wave then refracts over. Supply a survey raster, a
    #: layer of soundings or a depth in metres and that is the bed instead.
    bed = Data.bed(tool("derive_merge_rasters", primary=seafloor,
                        fallback=terrain))
    structure = Data.supplied(geometry="polyline")
    #: The line the agitation is read along: through the structure's centroid,
    #: along the wave the deck states it is travelling, from the exposed side
    #: into the lee. A propagation direction is what the keyword carries, so the
    #: transect is read in the same trigonometric convention.
    transect = tool("derive_transect", shape=structure, convention="trig",
                    bearing_deg=Ref("sheet.DIRECTION_OF_WAVE_PROPAGATION"),
                    length_m=P.transect_length_m)
    #: The domain as a MESH, when the caller has one already. Unfilled, MESH
    #: below is what the wave is solved on.
    mesh = Data.supplied(geometry="mesh").optional()


#: The structure as a water-removing FOOTPRINT, before the mesh: the domain is
#: the water the structure is subtracted FROM and a centreline bounds no area to
#: subtract.
_FOOTPRINT = Measured(
    "footprint", kind="footprint",
    asked={"value": DATA.structure, "width_m": ParamRef("barrier_width_m"),
           "asked": "the structure this question asks about",
           "code": "ARTEMIS_STRUCTURE_INVALID"})

#: What the accepted harbour mesh measures. The wave the settle stamps onto the
#: boundary file is the wave the deck is solved at, so its period and its
#: direction are read off the deck; the width is the one the footprint above was
#: cut at, because what the mesher removed is what the deck calls solid.
_HARBOUR = Measured(
    "settled", kind="harbour",
    asked={"structure": DATA.structure,
           "structure_width_m": ParamRef("barrier_width_m"),
           "wave_period_s": Ref("stated.WAVE_PERIOD"),
           "wave_height_m": ParamRef("wave_height_m"),
           "wave_direction_deg": Ref("stated.DIRECTION_OF_WAVE_PROPAGATION"),
           "reflection_coef": ParamRef("reflection_coef"),
           "result_basename": _RESULT})


class STEERING(ART):
    """The deck: a monochromatic wave in through the open edge, and what it leaves."""

    TITLE = Ref("settled.title")
    GEOMETRY_FILE = HARBOUR_GEOMETRY
    BOUNDARY_CONDITIONS_FILE = BOUNDARY_FILENAME
    RESULTS_FILE = _RESULT

    # The still water level the harbour is solved at is the dictionary's own zero,
    # so what this states is that the level is CONSTANT rather than what it is.
    INITIAL_CONDITIONS = "CONSTANT ELEVATION"
    # An elliptic solve over tens of thousands of nodes does not converge inside
    # the dictionary's own iteration ceiling for a domain this size.
    MAXIMUM_NUMBER_OF_ITERATIONS_FOR_SOLVER = 4000

    # The swell a harbour of refuge is asked about: an eight-second period, long
    # enough to diffract around the structure and short enough that the basin is
    # many wavelengths across.
    WAVE_PERIOD = 8.0
    # Where it runs TO, counted in the trigonometric sense from +x, so 90 is a
    # wave travelling north; the transect is read along the same bearing.
    DIRECTION_OF_WAVE_PROPAGATION = 90.0

    #: The forcing, which ARTEMIS reads out of the BOUNDARY CONDITIONS FILE and
    #: not out of the deck: the designated liquid stretch carries the incident
    #: height, the structure's own faces reflect, and every other face absorbs.
    incident_wave = IncidentWave(measured=_HARBOUR, height_m=P.wave_height_m,
                                 reflection_coef=P.reflection_coef)


#: The MESH RECIPE, frozen at declaration and building nothing at import. The
#: domain polygon's own edge IS the shoreline the sizing function measures, so
#: the recipe hands the mesher that polygon and nothing restates where the water
#: is. The structure is punched out of it with its outline locked in FIRST, and
#: the shoreline sizing is built over the domain that leaves - so the band around
#: the cut is graded rather than a discontinuity the triangulator has to absorb.
#: The domain's own rim is sized between the two, which is where the one op that
#: measures it belongs: after the sizing, before the gradation that grades it in.
MESH = tool.build_mesh(
    mesher="om2d",
    kind="unstructured_tri",
    extent=DATA.domain,
    resolution_m=P.mesh_resolution_m,
    ops=[
        mesh_op("set_obstacle", geometry=_FOOTPRINT),
        mesh_op("feature_sizing_function"),
        # THE RIM IS THE ASK'S TO SIZE. Nothing else sizes it: a sizing function
        # measures the water's own shape - the feature width, the distance to a
        # line, the wavelength over a depth - and out in the open approach every
        # one of those is coarse, so an undeclared rim comes back an order of
        # magnitude past the size word and the band behind it triangulates into
        # slivers. That rim is the boundary a solver forces its open condition
        # on. No edge is stated, so it is locked at the recipe's own size word.
        mesh_op("set_rim_size"),
        mesh_op("enforce_mesh_gradation", gradation=P.mesh_grade),
        mesh_op("delete_boundary_faces"),
        mesh_op("delete_faces_connected_to_one_face"),
        mesh_op("make_mesh_boundaries_traversable"),
        mesh_op("fix_mesh", delete_unused=True),
        mesh_op("set_bed", source=DATA.bed),
        # EVERY stretch the library reads as ocean at this depth opens. A harbour
        # has more than one mouth, and picking one of them would number a
        # multi-mouth domain as single-mouth.
        mesh_op("identify_ocean_boundary_sections",
                depth_threshold=P.open_depth_threshold_m),
    ],
)


#: What this question PLACES: the agitation coefficient along the transect the
#: user drew through the structure. The profile keeps the nodes within one
#: finest mesh edge of the line - the band the structure's own nodes were laid
#: at - so the read is the line's and not a mean over the whole basin's width.
OUTPUTS = [
    profile("KD", along=DATA.transect, within_m=P.mesh_resolution_m).chart(),
]
CAPTIONS = {"KD": "agitation coefficient"}

#: The run's ANSWER, as the numbers a reader has to be able to check, each a
#: measure of one of the reads above or of the wave height it is a ratio of.
ANSWER = {
    "kd_max": field("KD", t=-1).measure("max"),
    "kd_transect_min": profile("KD", along=DATA.transect,
                               within_m=P.mesh_resolution_m).measure("min"),
    "kd_transect_max": profile("KD", along=DATA.transect,
                               within_m=P.mesh_resolution_m).measure("max"),
    "hs_max_m": field("HS", t=-1).measure("max"),
    "mesh_size_m": mesh().measure("size_m"),
}


_ARTEMIS_RES_SPEC = ResolutionSpec(
    param="mesh_resolution_m",
    unit="m",
    min_value=2.0,
    native_hint="NOAA CUDEM 1/9 arc-second nearshore topobathy (~3 m)",
    constraint_source="solver",
    rationale=(
        "finest triangle edge at the shoreline and around the structure; a "
        "phase-resolving elliptic solve needs several nodes per wavelength, and "
        "Kd peaks inside a diffraction fringe the coarse mesh averages away"
    ),
)

_ARTEMIS_METADATA = AtomicToolMetadata(
    name="artemis_harbor_agitation",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="telemac",
    tier="template",
    resolution_specs=(_ARTEMIS_RES_SPEC,),
)


#: WHAT the mesh is built over, and the DATA slot a caller may hand a built
#: mesh in instead of the recipe. Filled, that mesh is adopted whole.
MESH_ON = "domain"
SUPPLIED_MESH = DATA.mesh

#: The engine files this run has to write for it to have solved
#: anything; unstated, the deck's own RESULTS FILE is the one.
RESULTS = (_RESULT,)

#: What the run directory calls the deck, and where the staged files live.
STEERING_FILE = _STEERING_FILE
PREFIX = "artemis"

#: The title the card carries when the run is held for review.
REVIEW_TITLE = "Review the incident wave, the structure and the mesh"


artemis_harbor_agitation = register_workflow(
    TelemacWorkflow, _ARTEMIS_METADATA, sys.modules[__name__],
    # A harbour is settled by the wave that enters it, and the stages that do it
    # read no runtime lever: no dated source, no frame a level is counted from,
    # and the mesh size is this question's own param.
    levers=(),
    provenance=(("structure", "structure_note"),
                ("mesh_resolution_m", "mesh_edge_note")),
    # A phase-RESOLVING solve is the most mesh-dependent of the family: Kd peaks
    # inside a diffraction fringe the coarse mesh averages away.
    sensitivity=(("kd_max", "peak"),),
    coerce=(compute_class(),),
)
