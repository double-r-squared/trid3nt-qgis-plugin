"""Engine template ``artemis_harbor_agitation`` - does the structure shelter the water.

ARTEMIS is the phase-RESOLVING elliptic mild-slope solver, so the answer is a
steady-state agitation coefficient Kd = Hs/H0 in which diffraction fringes and
standing waves are visible rather than averaged away."""

from __future__ import annotations

from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata, ResolutionSpec

from trid3nt_server.workflows.runtime import (
    Data,
    ParamRef,
    Ref,
    Step,
    register_workflow,
)
from trid3nt_server.workflows.mesh.tool import mesh_op, tool
from trid3nt_server.workflows.shared.aoi import AcquireAoi, location_or_bbox
from trid3nt_server.workflows.telemac.authoring.assembler import HARBOUR_GEOMETRY
from trid3nt_server.workflows.telemac.modules.artemis import (
    ART,
    BOUNDARY_FILENAME,
    IncidentWave,
)
from trid3nt_server.workflows.telemac.products.agitation import AgitationProducts
from trid3nt_server.workflows.telemac.solving.solve import compute_class
from trid3nt_server.workflows.telemac.templates.agitation.declarations import (
    ACCEPTS,
    DOC,
    HARBOR_HALF_DEG,
    PARAMS,
    PARAMS as P,
)
from trid3nt_server.workflows.telemac.workflow import Door, TelemacWorkflow

__all__ = ["ANSWER", "DATA", "MESH", "PARAMS", "STEERING",
           "artemis_harbor_agitation", "build_agitation_chart"]

_AUTHORING = "trid3nt_server.workflows.telemac.authoring"
_SOLVING = "trid3nt_server.workflows.telemac.solving.solve"
_TEMPLATE = "trid3nt_server.workflows.telemac.templates.agitation"

#: What the run directory holds the run's files under - the deck's own GEOMETRY /
#: RESULTS statements. The boundary file is the incident wave's and is named by
#: the composite that writes it.
_RESULT = "res_agitation.slf"
_STEERING_FILE = "art_agitation.cas"


class DATA:
    """What the run consumes from the world.

    ONE row, and a SLOT: this template names no default source for a structure."""

    structure = Data.supplied(geometry="polyline")
    #: The domain itself, when the caller has one. Unfilled, MESH below cuts it
    #: from the shoreline; filled, that mesh is what the wave is solved on and
    #: the recipe is not run.
    mesh = Data.supplied(geometry="mesh").optional()


#: The MESH RECIPE, frozen at declaration and building nothing at import. The
#: structure is punched out of the water with its outline locked in FIRST, and
#: the shoreline sizing is built over the domain that leaves - so the band around
#: the cut is graded rather than a discontinuity the triangulator has to absorb.
#: The domain's own rim is sized between the two, which is where the one op that
#: measures it belongs: after the sizing, before the gradation that grades it in.
MESH = tool.build_mesh(
    mesher="om2d",
    kind="unstructured_tri",
    extent=Ref("aoi.bbox"),
    resolution_m=P.mesh_min_edge_m,
    ops=[
        mesh_op("set_obstacle", geometry=Ref("barrier")),
        mesh_op("feature_sizing_function"),
        # THE RIM IS THE ASK'S TO SIZE. Nothing else sizes it: every sizing
        # function measures the shoreline, and the AOI's own box is not one, so
        # an undeclared rim comes back an order of magnitude past the size word
        # and the band where it meets the shoreline triangulates into slivers.
        # No edge is stated, so the rim is locked at the recipe's own size word -
        # the value the basin's rim is sized at.
        mesh_op("set_rim_size"),
        mesh_op("enforce_mesh_gradation", gradation=P.mesh_grade),
        mesh_op("delete_boundary_faces"),
        mesh_op("delete_faces_connected_to_one_face"),
        mesh_op("laplacian2"),
        mesh_op("make_mesh_boundaries_traversable"),
        mesh_op("fix_mesh", delete_unused=True),
        # TOPOBATHY is the class a bed is defined over and CUDEM's nearshore
        # collection covers a surveyed harbour, so no substitution is declared
        # here: the bed the wave refracts over is the surveyed sea floor.
        mesh_op("set_bed", source="fetch_topobathy"),
        # EVERY stretch the library reads as ocean at this depth opens. A harbour
        # has more than one mouth, and picking one of them would number a
        # multi-mouth domain as single-mouth.
        mesh_op("identify_ocean_boundary_sections",
                depth_threshold=P.open_depth_threshold_m),
    ],
)


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

    WAVE_PERIOD = P.wave_period_s
    DIRECTION_OF_WAVE_PROPAGATION = P.wave_direction_deg

    #: The forcing, which ARTEMIS reads out of the BOUNDARY CONDITIONS FILE and
    #: not out of the deck: the designated liquid stretch carries the incident
    #: height, the structure's own faces reflect, and every other face absorbs.
    incident_wave = IncidentWave(cli_text=Ref("settled.cli_text"),
                                 open_nodes=Ref("settled.open_nodes"),
                                 structure_nodes=Ref("settled.structure_nodes"),
                                 height_m=P.wave_height_m,
                                 reflection_coef=P.reflection_coef)


#: The run's ANSWER, as the numbers a reader has to be able to check.
ANSWER = ("kd_max", "hs_max_m", "kd_sheltered", "kd_exposed", "wave_period_s",
          "mesh_size_m", "agitation_curve_m", "agitation_curve_kd",
          "agitation_curve_kind", "boundary_states")


def build_agitation_chart(*, result: Any, params: Any) -> dict[str, Any] | None:
    """The agitation chart SPEC: Kd along the structure's own shadow strip.

    The curve is the RUN's own; ``None`` when the run measured no curve."""
    xs = getattr(result, "agitation_curve_m", None)
    kd = getattr(result, "agitation_curve_kd", None)
    if not xs or not kd or len(xs) != len(kd):
        return None
    from trid3nt_server.emission.charts import build_chart_payload

    sheltered = getattr(result, "kd_sheltered", None)
    exposed = getattr(result, "kd_exposed", None)
    period = getattr(result, "wave_period_s", None)
    where = params.get("location")
    title = (f"Harbour agitation Kd - {where}" if where
             else (getattr(result, "name", None) or "Harbour agitation Kd"))
    return build_chart_payload(
        vega_lite_spec={
            "mark": {"type": "line", "point": False},
            "data": {"values": [{"x": float(xs[i]), "kd": float(kd[i])}
                                for i in range(len(xs))]},
            "encoding": {
                "x": {"field": "x", "type": "quantitative",
                      "title": "Along the incident direction, 0 = the structure (m)"},
                "y": {"field": "kd", "type": "quantitative",
                      "title": "Agitation coefficient Kd = Hs/H0"},
            },
        },
        title=title,
        caption=(
            (f"Forced by a prescribed {float(period):.3g} s incident wave: "
             if period is not None else "")
            + (f"Kd {float(exposed):.3g} on the exposed approach against "
               f"{float(sheltered):.3g} in the lee - the structure cut agitation "
               f"in the strip it shadows by a factor of "
               f"{float(exposed) / float(sheltered):.3g}. "
               if sheltered and exposed else "")
            # WHAT WAS AND WAS NOT MEASURED. Both numbers are means over the
            # structure's own shadow strip inside the MESHED domain; water the
            # shoreline left out of that domain is not in either of them.
            + "Both are means over the meshed domain only. Phase-resolving "
              "screening, not a calibrated hindcast."
        ),
    )


_ARTEMIS_RES_SPEC = ResolutionSpec(
    param="mesh_min_edge_m",
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


artemis_harbor_agitation = register_workflow(
    TelemacWorkflow, _ARTEMIS_METADATA, PARAMS,
    Door(
        steering=STEERING,
        # The AOI, and then the structure as a water-removing FOOTPRINT - both
        # before the mesh, because the domain is the water the structure is
        # subtracted FROM and a centreline bounds no area to subtract.
        domain=(AcquireAoi(location=P.location, bbox=P.bbox,
                           half_deg=HARBOR_HALF_DEG, default_name="harbour",
                           code_prefix="ARTEMIS").named("aoi"),
                Step(runner=f"{_TEMPLATE}.barrier.barrier_footprint",
                     stage="prep",
                     kwargs={"structure": DATA.structure,
                             "width_m": ParamRef("barrier_width_m")}
                     ).named("barrier"),),
        mesh=MESH, mesh_on="aoi", supplied_mesh=DATA.mesh,
        settle=Step(runner=f"{_AUTHORING}.assembler.settle_harbour",
                    stage="author",
                    kwargs={"mesh": Ref("mesh"), "structure": DATA.structure,
                            # The width the footprint above was cut at: what the
                            # mesher removed is what the deck calls solid.
                            "structure_width_m": ParamRef("barrier_width_m"),
                            "wave_period_s": ParamRef("wave_period_s"),
                            "wave_height_m": ParamRef("wave_height_m"),
                            "wave_direction_deg": ParamRef("wave_direction_deg"),
                            "reflection_coef": ParamRef("reflection_coef"),
                            "result_basename": _RESULT}),
        results=(_RESULT,),
        steering_file=_STEERING_FILE, prefix="artemis",
        dispatch=f"{_SOLVING}.solve_case", compute_class=P.compute_class,
        read=lambda run: AgitationProducts.agitation(
            run=run, solve=run).named("agitation"),
        chart=("harbor_agitation", build_agitation_chart),
        review_title="Review the incident wave, the structure and the mesh"),
    data=DATA,
    accepts=ACCEPTS,
    answer=ANSWER,
    provenance=(("wave_period_s", "wave_period_note"),
                ("structure", "structure_note"),
                ("mesh_min_edge_m", "mesh_edge_note")),
    # A phase-RESOLVING solve is the most mesh-dependent of the family: Kd peaks
    # inside a diffraction fringe, and the sheltered/exposed pair is read inside
    # the shadow gradient. The sheltering RATIO between them converges and is not
    # labeled.
    sensitivity=(("kd_max", "peak"),
                 ("kd_sheltered", "gradient"),
                 ("kd_exposed", "gradient")),
    coerce=(
        location_or_bbox("artemis_harbor_agitation", code_prefix="ARTEMIS",
                         hint="For a natural prompt like 'is the marina at <place> "
                              "sheltered', pass location='<place>' and the "
                              "breakwater layer as structure=."),
        compute_class(),
    ),
    doc=DOC,
)
