"""Engine template ``telemac_do_sag`` - TELEMAC-2D WAQTEL dissolved-oxygen sag.

The recipe on one page: the binding blocks, ``plan(ops)``, the ANSWER fields and
the chart function. The declared params and the model-facing prose are one file
over in ``declarations.py``. Everything else - normalizing the wire args,
resolving the doors, walking the plan, persisting the products - is the skeleton
(``workflows/lib/workflow.py``); the reach mechanism is the TELEMAC facade
(``workflows/telemac/workflow.py``). See
``docs/design/declarative-workflows.md``.

THE QUESTION: the DISSOLVED-OXYGEN SAG below a permitted discharge / WWTP outfall
in a river reach (the US TMDL / Clean Water Act permit question). Where does DO
bottom out downstream, and does it VIOLATE the water-quality standard? TELEMAC-2D
+ WAQTEL O2 - the Streeter-Phelps oxygen sag - over a real NHDPlus reach.
"""

from __future__ import annotations

from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata, ResolutionSpec

from trid3nt_server.workflows.lib import (
    DrawGate,
    Forcing,
    Physics,
    Ref,
    RunMode,
    register_workflow,
    user_input,
)
from trid3nt_server.workflows.mesh.step import MeshStep
from trid3nt_server.workflows.mesh.tool import mesh_op, tool
from trid3nt_server.workflows.shared.aoi import location_or_bbox
from trid3nt_server.workflows.telemac.do_sag.declarations import (
    ACCEPTS, DOC, PARAMS, PARAMS as P,
)
from trid3nt_server.workflows.telemac.steps import (
    MeshCoverage,
    ReviewResolvedInputs,
    WaqtelO2,
    event_time,
)
from trid3nt_server.workflows.telemac.workflow import TelemacWorkflow

__all__ = ["ANSWER", "DATA", "PARAMS", "build_sag_chart", "plan", "telemac_do_sag"]

_HELPERS = "trid3nt_server.workflows.telemac.helpers"


#: The reach's data, one row per artifact, in the order the chain reads them. The
#: carrier discharge is a STEP rather than a row: it reads the resolved mid-reach
#: seed, which is a step result and not something a producer declaration can name.
class DATA:
    rivers = tool(f"{_HELPERS}.reach.fetch_reach_flowline", prefetched=None)
    # THE REACH, narrowed by CHAINING tools rather than by a mesher that grew a
    # corridor of its own. The navigated mainstem names the stretch, its two ends
    # name where the stretch stops, and the cut through the MAPPED water is the
    # domain - so the two end faces are the transects the inflow and the outflow
    # are prescribed on, measured off real geometry rather than a ribbon.
    centerline = tool("fetch_nhdplus_nldi_navigate",
                      seed_point=[Ref("seed.lon"), Ref("seed.lat")],
                      direction="DM",
                      distance_km=P.reach_length_km)
    ends = tool("endpoints", line=centerline)
    # The QUERY WINDOW the water is asked for: the centerline's own extent grown
    # by a stated distance, because the water that belongs to this reach reaches
    # past the line - a far channel behind a mid-river island is three km off it
    # and is still the same river. The pad widens the QUESTION, never the meshed
    # domain: what returns is real mapped water, and the section cut below keeps
    # only the stretch between the reach's two ends.
    window = tool("compute_layer_bounds", layer_uri=centerline, pad_m=3000.0,
                  fit_map=False)
    water = tool("fetch_nhd_area_water", bbox=Ref("window.bbox"))
    # HOW MUCH of the reach the returned polygons actually map, measured before
    # the cut so an unmapped reach refuses on its own cause instead of arriving
    # at the section as an empty geometry.
    mapped_water = tool(f"{_HELPERS}.reach.measure_water_coverage",
                        water=water, centerline=centerline)
    reach_polygon = tool("section", polygon=mapped_water,
                         between=Ref("ends.between"))
    # THE SUBSTITUTION, declared where a reader can see it. A bed is TOPOBATHY -
    # the channel bottom - and no topobathy survey covers an inland reach, so
    # this row is a surface DEM and the recipe below says so by painting the bed
    # from it BY NAME. The consequence travels with it: a surface measures the
    # water top, so the modelled channel is shallower than the real one and the
    # journal names this row as what the bed came from. GLO-30 is asked for on
    # its OWN 1-arcsecond lattice, so the raster the nodes are sampled from
    # carries the source pixels rather than a resample of them.
    dem = tool("fetch_copernicus_dem", bbox=Ref("window.bbox"), px_per_deg=3600.0,
               purpose="river bed elevation")


# -- the binding blocks --------------------------------------------------- #
# What the run IS, declared as frozen values above the recipe that assembles
# them. Every member is a late-bound read (P.<param> / DATA.<row> / Ref) that the
# interpreter substitutes against the approved sheet, so the blocks are
# process-lifetime constants and the plan is a pure assembly of them.

#: The reach's own extent rides HERE rather than on the mesh ask: the mesher is
#: handed a measured polygon, while the deck still states the stretch it wrote for.
PHYSICS = Physics("waqtel_o2",
                  reach_length_km=P.reach_length_km,
                  do_sag_config=Ref("waqtel"),
                  sim_duration_s=P.sim_duration_s,
                  output_interval_min=P.output_interval_min)

FORCING = Forcing(carrier=Ref("reviewed_discharge"))

#: The MESH RECIPE, frozen at declaration and building nothing at import. Three
#: agnostic params and the ordered program that produces the mesh. The extent is
#: the CHAIN's product - the stretch of mapped water the section cut between the
#: centerline's two ends - so the mesher triangulates a domain other tools
#: measured rather than growing a corridor of its own. The ops are oceanmesh's
#: own clean passes under its own names, then the two things we impose: the bed,
#: painted from the substitution this template declared above and named in the
#: journal, and the roles, prescribed across the two end transects the section
#: cut.
MESH = tool.build_mesh(
    mesher="om2d",
    kind="unstructured_tri",
    extent=Ref("reach_polygon"),
    resolution_m=P.mesh_resolution_m,
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


def plan(ops):  # noqa: ANN001, ANN201 - the declared plan value, per the design doc
    """The DO-sag recipe. Pure and STATIC: it reads no value, it names them."""
    return [
        DrawGate(param="outfall_coords", geometry="point",
                 prompt="Click where the discharge enters the river"),
        *ops.acquire_domain(location=P.location, bbox=P.bbox, rivers=DATA.rivers,
                            discharge=P.discharge_m3s, event_time=P.event_time,
                            seed_coords=P.outfall_coords),
        WaqtelO2(effluent_bod_mgl=P.effluent_bod_mgl,
                 effluent_q_m3s=P.effluent_q_m3s,
                 effluent_do_mgl=P.effluent_do_mgl,
                 upstream_do_mgl=P.upstream_do_mgl,
                 do_saturation_mgl=P.do_saturation_mgl,
                 water_temp_c=P.water_temp_c, k1_per_day=P.k1_per_day,
                 k2_per_day=P.k2_per_day,
                 do_standard_mgl=P.do_standard_mgl).named("waqtel"),
        ReviewResolvedInputs(carrier_discharge=Ref("carrier_discharge"),
                             workflow=ops.name,
                             input_mode=RunMode).named("reviewed_discharge"),
        MeshStep.build(mesh=MESH, name=Ref("reach")).named("mesh"),
        MeshCoverage(mesh=Ref("mesh"), centerline=DATA.centerline),
        ops.author(mesh=MESH, physics=PHYSICS, forcing=FORCING),
        ops.solve(compute_class=P.compute_class, physics=PHYSICS),
        ops.read(Ref("solve"), physics=PHYSICS, forcing=FORCING)
           .chart("do_sag_curve", builder=build_sag_chart),
    ]


#: The run's ANSWER, as the numbers a reader has to be able to check. Persisted
#: beside the chart spec so verification cites the run's own figures rather than
#: recomputing them from the raster.
ANSWER = ("do_min_mgl", "do_min_distance_m", "do_standard_mgl",
          "do_violates_standard", "do_upstream_mgl", "do_saturation_mgl",
          "bod_mixed_mgl", "mean_velocity_mps", "sag_curve_distance_m",
          "sag_curve_do_mgl", "sag_curve_bod_mgl", "sp_curve_distance_m",
          "sp_curve_do_mgl", "sp_rms_mgl", "sp_sag_deviation_mgl", "sp_note",
          "mesh_size_m")


def build_sag_chart(*, result: Any, params: Any) -> dict[str, Any] | None:
    """The DO-sag chart SPEC: DO + CBOD vs distance, against the closed form.

    Honest postprocess scalars off the published layer (the binned centerline
    curve), never a fabricated line; ``None`` when the curve is absent. The
    Streeter-Phelps profile rides as a DASHED second series computed in the read
    from the run's own mix point, load and velocity, and the caption states how
    far the solve sits from it - the deterministic grading, not a judgement.
    """
    xs = getattr(result, "sag_curve_distance_m", None)
    do = getattr(result, "sag_curve_do_mgl", None)
    bod = getattr(result, "sag_curve_bod_mgl", None)
    if not xs or not do or len(xs) != len(do):
        return None
    std = float(getattr(result, "do_standard_mgl", None) or 5.0)

    from trid3nt_server.emission.styles import preset_units
    from trid3nt_server.tools.processing.charts_common import build_chart_payload

    # The UNITS come from the style contract, so the chart's axis and the DO
    # layer's legend cannot disagree about what this field is measured in.
    units = preset_units("continuous_plume_concentration") or "mg/L"

    do_vals = [{"x_km": round(xs[i] / 1000.0, 4), "v": do[i], "series": "Dissolved O2"}
               for i in range(len(xs))]
    bod_vals = ([{"x_km": round(xs[i] / 1000.0, 4), "v": bod[i], "series": "CBOD"}
                 for i in range(len(xs))] if bod and len(bod) == len(xs) else [])
    sp_x = getattr(result, "sp_curve_distance_m", None)
    sp_do = getattr(result, "sp_curve_do_mgl", None)
    # The closed form rides as its OWN NAMED SERIES rather than as a second
    # layer: the dock draws one colour and one legend entry per series, so a
    # reader can tell the solved profile from the analytical one - which is the
    # whole point of drawing them together.
    sp_vals = ([{"x_km": round(sp_x[i] / 1000.0, 4), "v": sp_do[i],
                 "series": "Streeter-Phelps closed form"}
                for i in range(len(sp_x))]
               if sp_x and sp_do and len(sp_x) == len(sp_do) else [])
    vega_lite_spec = {
        "layer": [
            {"mark": {"type": "line", "point": False},
             "data": {"values": do_vals + sp_vals + bod_vals},
             "encoding": {
                 "x": {"field": "x_km", "type": "quantitative",
                       "title": "Downstream distance (km)"},
                 "y": {"field": "v", "type": "quantitative",
                       "title": f"Concentration ({units})"},
                 "color": {"field": "series", "type": "nominal", "title": None}}},
            {"mark": {"type": "rule", "strokeDash": [6, 4], "color": "#c0392b"},
             "data": {"values": [{"y": std}]},
             "encoding": {"y": {"field": "y", "type": "quantitative"}}},
        ]
    }
    dmin = getattr(result, "do_min_mgl", None)
    dloc = getattr(result, "do_min_distance_m", None)
    verdict = "violates" if getattr(result, "do_violates_standard", False) else "meets"
    # With no location words the LAYER's own name is the title: it already reads
    # "Dissolved oxygen sag (<reach>)", so prefixing it would say it twice.
    where = params.get("location")
    title = (f"Dissolved-oxygen sag - {where}" if where
             else (getattr(result, "name", None) or "Dissolved-oxygen sag"))
    rms = getattr(result, "sp_rms_mgl", None)
    dev = getattr(result, "sp_sag_deviation_mgl", None)
    overlay = (
        f" The Streeter-Phelps closed form is drawn beside it on this run's own mix "
        f"point, load and velocity - whole-profile RMS {rms:g} {units}, sag minimum "
        f"{abs(dev):g} {units} {'below' if dev < 0 else 'above'} it."
        if sp_vals and rms is not None and dev is not None else
        f" No analytical overlay ({getattr(result, 'sp_note', None) or 'not computed'})."
    )
    return build_chart_payload(
        vega_lite_spec=vega_lite_spec,
        title=title,
        caption=(
            f"DO sag: minimum {dmin} {units} at {dloc} m downstream "
            f"({verdict} the {std:g} {units} standard, red dashed). CBOD decay drives "
            f"the sag; reaeration recovers it." + overlay + " Screening/permit grade."
        ),
    )


_TELEMAC_DO_SAG_RES_SPEC = ResolutionSpec(
    param="mesh_resolution_m",
    unit="m",
    min_value=3.0,
    native_hint="NHD channel geometry + 3DEP terrain; edge sized from reach width",
    constraint_source="solver",
    rationale=(
        "explicit target edge length; 3 m is the absolute finest the TELEMAC mesh "
        "builder authors, a long reach is coarsened under the node budget "
        "(self-labeled); no fixed coarse ceiling"
    ),
)

_TELEMAC_DO_SAG_METADATA = AtomicToolMetadata(
    name="telemac_do_sag",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="telemac",
    tier="template",
    resolution_specs=(_TELEMAC_DO_SAG_RES_SPEC,),
)


telemac_do_sag = register_workflow(
    TelemacWorkflow, _TELEMAC_DO_SAG_METADATA, PARAMS, plan,
    data=DATA,
    accepts=ACCEPTS,
    answer=ANSWER,
    provenance=(("discharge_m3s", "discharge_note"),),
    # WHERE the sag sits is a local-feature LOCATION and moves with the element
    # that resolves it. The DO minimum itself is a saturated maximum - a
    # converged class - so it carries no label.
    sensitivity=(("do_min_distance_m", "location"),),
    coerce=(
        location_or_bbox("telemac_do_sag", code_prefix="TELEMAC"),
        user_input.point("outfall_coords", label="outfall_coords",
                         code="TELEMAC_PARAMS_INVALID"),
        event_time(),
    ),
    doc=DOC,
)
