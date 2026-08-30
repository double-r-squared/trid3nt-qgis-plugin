"""Engine template ``telemac_rain_on_grid`` - TELEMAC-2D rainfall-runoff on a
delineated watershed.

The recipe on one page: the declared data, the binding blocks, ``plan(ops)``, the
ANSWER fields and the chart function. The declared params and the model-facing
prose are one file over in ``declarations.py``. Everything else - normalizing the
wire args, resolving the doors, walking the plan, persisting the products - is the
skeleton (``workflows/lib/workflow.py``); the catchment mechanism is the TELEMAC
facade's rain-on-grid front (``workflows/telemac/steps/rain_on_grid.py``) over the
shared mesh front's catchment strategy (``workflows/mesh/watershed.py``). See
``docs/design/declarative-workflows.md``.

THE QUESTION: how much RUNOFF a storm produces from a WATERSHED, and where the
water stands while it drains. A design storm or a real hourly hyetograph falls on
a catchment DELINEATED at a pour point and triangulated from a real bare-earth
DEM; it infiltrates by the SCS curve-number method with per-node curve numbers
from land cover, and the excess runs off overland to the outlet - producing an
OUTLET HYDROGRAPH plus a peak flood-depth map.

Applicability envelope (Godara, Bruland and Alfredsen 2024, Front. Water
6:1384205): rain-on-grid reproduces SINGLE-STORM flash-flood events (~10-20 h) in
small steep catchments. Multi-peak and sustained rain-on-snow are NOT reproduced -
infiltrated water is permanently lost, so there is no subsurface return flow and
no inter-peak baseflow, which ``soil_store`` narrows but does not close.
TELEMAC-2D's triangular mesh is stable on steep terrain where a structured grid is
not, which is the paper's own finding against HEC-RAS.
"""

from __future__ import annotations

from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata, ResolutionSpec

from trid3nt_server.workflows.lib import (
    D,
    Data,
    DrawGate,
    Fetch,
    Forcing,
    FormGate,
    P,
    Physics,
    Ref,
    register_workflow,
    user_input,
)
from trid3nt_server.workflows.mesh.tool import tool
from trid3nt_server.workflows.telemac.rain_on_grid.declarations import (
    DOC,
    NLCD_NATIVE_RESOLUTION_M,
    PARAMS,
    POUR_POINT_BUFFER_DEG,
)
from trid3nt_server.workflows.telemac.steps import Catchment, compute_class
from trid3nt_server.workflows.telemac.workflow import TelemacWorkflow

__all__ = ["ANSWER", "DATA", "PARAMS", "build_hydrograph_chart", "plan",
           "telemac_rain_on_grid"]

_STEPS = "trid3nt_server.workflows.telemac.steps"

_CODE = "TELEMAC_ROG_PARAMS_INVALID"


#: What the run consumes from the world, and the ONE mesh it will take instead of
#: building its own. Every world-read is declared here rather than performed in a
#: step: the fetcher router's cache, ladders, provenance and typed refusals live
#: once, and a producer is where that middleware is reached from.
DATA = (
    # THE SLATE. Producer-less on purpose: a catchment mesh is an AUTHORED
    # artifact, and naming a default source for somebody's mesh would be an
    # opinion the question does not carry. Filled by a mesh the caller supplies;
    # unfilled, the run asks whether to adopt a mesh this case already holds and
    # otherwise generates one - a labeled fallback, never a stance.
    Data("mesh").supplied(geometry="mesh").optional(),
    # 3DEP is PINNED, not preferred: a DSM (Copernicus GLO-30 includes forest
    # canopy) puts the bed on the tree tops and routes the water down the wrong
    # slopes. A pinned source never switches, so a 3DEP outage surfaces the
    # fetcher's own typed error naming copernicus and the substitution is the
    # user's to make - which is what a cross-dataset swap has to be.
    Data("bed_dem", Fetch.tool("fetch_dem", bbox=Ref("aoi.bbox"), source="3dep",
                               resolution_m=P.bed_dem_resolution_m,
                               purpose="mesh bed")),
    Data("rivers", Fetch.tool("fetch_river_geometry", bbox=Ref("aoi.bbox"),
                              source=P.river_source, purpose="river geometry")),
    Data("landcover", Fetch.tool("fetch_landcover", bbox=Ref("aoi.bbox"),
                                 dataset=P.landcover_dataset,
                                 resolution_m=NLCD_NATIVE_RESOLUTION_M,
                                 purpose="land cover")),
    Data("rain", Fetch.tool(f"{_STEPS}.rain_on_grid.resolve_rain_event",
                            window=P.rain_window,
                            intensity_mm_per_hr=P.design_storm_mm_per_hr,
                            storm_duration_hr=P.storm_duration_hr,
                            sim_duration_hr=P.sim_duration_hr)
         # A real window fetches the hourly record; without one the constant
         # design storm is the labeled rung, and the run reports which answered.
         .ladder("aorc_hourly", "design_storm")),
)


# -- the binding blocks --------------------------------------------------- #
# What the run IS, declared as frozen values above the recipe that assembles
# them. Every member is a late-bound read (P.<param> / D.<data> / Ref) that the
# interpreter substitutes against the approved sheet, so the blocks are
# process-lifetime constants and the plan is a pure assembly of them.

PHYSICS = Physics("rainfall_runoff",
                  time_step_s=P.time_step_s,
                  outlet_node_count=P.outlet_node_count,
                  output_interval_min=P.output_interval_min,
                  soil_store=P.soil_store,
                  soil_store_capacity_mm=P.soil_store_capacity_mm,
                  soil_recovery_hr=P.soil_recovery_hr,
                  soil_spinup_days=P.soil_spinup_days)

FORCING = Forcing(rain=D.rain)

#: The MESH ASK, frozen at declaration and building nothing at import. The
#: acquired window carries the extent the delineation runs inside and the outlet
#: the basin drains to; the band, the gradation and the outlet-snap window are the
#: ``watershed`` mesher's own declared fields, checked at the router. The deck
#: reads the finest edge only to record what was ASKED for; what the mesh was
#: BUILT at comes back on the mesh step.
MESH = tool.build_mesh(
    mesher="watershed",
    kind="unstructured_tri",
    extent=Ref("aoi"),
    min_edge_length_m=P.mesh_min_edge_m,
    max_edge_length_m=P.mesh_max_edge_m,
    grade=P.mesh_grade,
    max_iter=P.mesh_max_iter,
    snap_search_cells=P.outlet_snap_cells,
)


def plan(ops):  # noqa: ANN001, ANN201 - the declared plan value, per the design doc
    """The rainfall-runoff recipe. Pure and STATIC: it reads no value, it names them.

    The gates come FIRST so every step and every producer downstream of them runs
    on the approved sheet. The pour point in particular: it decides which basin is
    delineated at all, so a run that resolved it after the mesh would have meshed
    a catchment the review never saw.
    """
    return [
        FormGate(title="Review the storm, the catchment and the mesh band"),
        DrawGate(param="pour_point", geometry="point",
                 prompt="Click the catchment OUTLET the runoff drains to"),
        *ops.acquire_domain(location=P.location, bbox=P.bbox, shape="catchment",
                            pour_point=P.pour_point,
                            aoi_half_deg=POUR_POINT_BUFFER_DEG,
                            aoi_name="watershed", code_prefix="TELEMAC_ROG"),
        Catchment.mesh(mesh=MESH, supplied=D.mesh, bed_dem=D.bed_dem,
                       rivers=D.rivers).named("watershed_mesh"),
        Catchment.infiltration(mesh=Ref("watershed_mesh"), landcover=D.landcover,
                               curve_number=P.curve_number,
                               steep_slope_correction=P.steep_slope_correction,
                               antecedent_moisture=P.antecedent_moisture
                               ).named("infiltration"),
        ops.author(mesh=MESH, physics=PHYSICS, forcing=FORCING),
        ops.solve(compute_class=P.compute_class, physics=PHYSICS),
        ops.read(Ref("solve"), physics=PHYSICS, forcing=FORCING)
           .chart("rain_on_grid_outlet_hydrograph", builder=build_hydrograph_chart),
    ]


#: The run's ANSWER, as the numbers a reader has to be able to check. Persisted
#: beside the chart spec so verification cites the run's own figures rather than
#: recomputing them from the raster.
ANSWER = ("catchment_area_km2", "peak_discharge_m3s", "peak_discharge_time_s",
          "rainfall_volume_m3", "runoff_volume_m3", "runoff_coefficient",
          "max_depth_peak_m", "max_velocity_peak_ms", "continuity_rel_error",
          "amc_condition", "rain_intensity_mm_per_hr", "n_frames",
          "mesh_size_m", "mesh_node_count", "mesh_element_count",
          "catchment_provenance", "domain_bbox")


def build_hydrograph_chart(*, result: Any, params: Any) -> dict[str, Any] | None:
    """The OUTLET HYDROGRAPH spec: discharge against time, off the run's own series.

    The hydrograph IS the rainfall-runoff answer - a flood-depth raster says where
    the water stood, and this says how much left the basin and when. The series is
    the worker's own measured outflow through the outlet boundary, never a fitted
    curve. ``None`` when the run measured no series, which is the honest "there is
    no hydrograph to draw".
    """
    times = getattr(result, "outlet_hydrograph_t_s", None)
    flows = getattr(result, "outlet_hydrograph_q_m3s", None)
    if not times or not flows or len(times) != len(flows):
        return None
    from trid3nt_server.tools.processing.charts_common import build_chart_payload

    # Discharge OUT of the basin is what the question asks about, and the outlet
    # boundary's normal points outward - so an outflow arrives negative. The sign
    # is flipped once, here, rather than left for a reader to interpret off an
    # axis that would otherwise run downward for a rising flood.
    values = [{"t_h": float(t) / 3600.0, "q_m3s": -float(q)}
              for t, q in zip(times, flows)]
    where = (params.get("location") or getattr(result, "catchment_name", None)
             or "the catchment")
    peak = getattr(result, "peak_discharge_m3s", None)
    area = getattr(result, "catchment_area_km2", None)
    coefficient = getattr(result, "runoff_coefficient", None)
    intensity = getattr(result, "rain_intensity_mm_per_hr", None)
    return build_chart_payload(
        vega_lite_spec={
            "mark": {"type": "line", "point": True},
            "data": {"values": values},
            "encoding": {
                "x": {"field": "t_h", "type": "quantitative",
                      "title": "Time since storm start (h)"},
                "y": {"field": "q_m3s", "type": "quantitative",
                      "title": "Outlet discharge (m3/s)"},
            },
        },
        title=f"Outlet hydrograph - {where}",
        caption=(
            (f"Peak outflow {abs(float(peak)):.3g} m3/s" if peak is not None
             else "No outflow reached the outlet over this window")
            + (f" from a {float(area):.3g} km2 catchment" if area is not None else "")
            + (f", {float(intensity):g} mm/h storm" if intensity else "")
            + (f", runoff coefficient {float(coefficient):.3g}"
               if coefficient is not None else "")
            + ". Planning-grade single-storm screening: infiltrated water is "
              "permanently lost, so there is no baseflow limb."
        ),
    )


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
    TelemacWorkflow, _ROG_METADATA, PARAMS, plan,
    data=DATA,
    answer=ANSWER,
    provenance=(("rain_event", "rain_event_note"),
                ("sim_duration_hr", "sim_duration_note"),
                ("runoff_path", "runoff_path_note"),
                ("mesh_domain", "mesh_domain_note"),
                ("mesh_bed", "mesh_bed_note")),
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
        user_input.point("pour_point", label="pour_point", code=_CODE),
        user_input.bbox("bbox", label="analysis AOI", code=_CODE),
        compute_class(),
    ),
    doc=DOC,
)
