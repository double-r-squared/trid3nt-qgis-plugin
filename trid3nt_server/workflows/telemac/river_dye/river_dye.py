"""Engine template ``telemac_river_dye`` - TELEMAC-2D river surface-tracer engine.

The recipe on one page: the binding blocks, ``plan(ops)``, the ANSWER fields and
the chart function. The declared params and the model-facing prose are one file
over in ``declarations.py``. Everything else - normalizing the wire args,
resolving the doors, walking the plan, persisting the products - is the skeleton
(``workflows/lib/workflow.py``); the reach mechanism is the TELEMAC facade
(``workflows/telemac/workflow.py``). See
``docs/design/declarative-workflows.md``.

THE QUESTION: how far a DYE / TRACER / CONTAMINANT / oil / sewage / sediment
spill travels DOWNSTREAM in a river reach, and what its peak concentration is;
OR where the bed SCOURS and re-deposits under a flood (GAIA erodible-bed
morphodynamics). TELEMAC-2D shallow water over a real reach, with the plume or
the bed evolution animated from the native time-stepped mesh.
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
from trid3nt_server.workflows.shared.aoi import location_or_bbox
from trid3nt_server.workflows.telemac.river_dye.coercions import release_points
from trid3nt_server.workflows.telemac.river_dye.declarations import (
    ACCEPTS, DOC, PARAMS,
)
from trid3nt_server.workflows.telemac.steps import (
    ReachMesh,
    compute_class,
    event_time,
    substance_class,
)
from trid3nt_server.workflows.telemac.workflow import TelemacWorkflow

__all__ = ["ANSWER", "DATA", "PARAMS", "build_dye_chart", "plan", "telemac_river_dye"]

_STEPS = "trid3nt_server.workflows.telemac.steps"


#: The reach pipeline's REFERENCE data - fetched fresh for the domain the geocode
#: step binds, never supplied. The carrier discharge is a STEP rather than Data: it
#: reads the resolved mid-reach seed, which is a step result and not something a
#: producer declaration can name.
DATA = (
    Data("rivers", Fetch.tool(f"{_STEPS}.reach.fetch_reach_flowline",
                              prefetched=P.river_geometry_uri)),
    Data("rain", Fetch.tool(f"{_STEPS}.forcing.resolve_rain_forcing",
                            rainfall_mm_per_day=P.rainfall_mm_per_day,
                            evaporation_mm_per_day=P.evaporation_mm_per_day,
                            gridmet_window=P.rainfall_gridmet_window)
         .ladder("gridmet_domain_mean", "user_rate")
         # The cadence and units the deck receives, stated rather than assumed:
         # both rungs are daily rates, so this asks for no interpolation - and a
         # sub-daily target would refuse here instead of manufacturing a storm
         # shape gridMET never reported.
         .resample(to="1D", max_gap="native*3")
         .normalize(units="mm/day")),
)


# -- the binding blocks --------------------------------------------------- #
# What the run IS, declared as frozen values above the recipe that assembles
# them. Every member is a late-bound read (P.<param> / D.<data> / Ref) that the
# interpreter substitutes against the approved sheet, so the blocks are
# process-lifetime constants and the plan is a pure assembly of them.

PHYSICS = Physics(
    "tracer",
    substance=P.substance, release_coords=P.release_coords,
    reach_seed_coords=P.reach_seed_coords, sim_duration_s=P.sim_duration_s,
    spill_fraction=P.spill_fraction, spill_duration_s=P.spill_duration_s,
    dye_concentration_mgl=P.dye_concentration_mgl, source_q_m3s=P.source_q_m3s,
    output_interval_min=P.output_interval_min,
    friction_coefficient=P.friction_coefficient, friction_law=P.friction_law,
    velocity_diffusivity=P.velocity_diffusivity,
    tracer_diffusivity=P.tracer_diffusivity, erodible_bed=P.erodible_bed,
    sediment_gradation=P.sediment_gradation, dredging=P.dredging,
    decay_half_life_hours=P.decay_half_life_hours,
    decay_rate_per_day=P.decay_rate_per_day, sediment_type=P.sediment_type,
    grain_size_um=P.grain_size_um, bed_thickness_m=P.bed_thickness_m,
    bedload_formula=P.bedload_formula,
    morphological_factor=P.morphological_factor, dredge_mode=P.dredge_mode,
    dredge_volume_m3=P.dredge_volume_m3, dredge_disposal=P.dredge_disposal,
    dredge_crit_depth_m=P.dredge_crit_depth_m,
    dredge_dig_depth_m=P.dredge_dig_depth_m,
)

FORCING = Forcing(carrier=Ref("carrier_discharge"), rain=D.rain,
                  wind_speed_mps=P.wind_speed_mps,
                  wind_direction_deg=P.wind_direction_deg)

#: The MESH ASK, frozen at declaration and building nothing at import. Every field
#: is checked at the router against what the ``corridor_tin`` mesher declares, so a
#: knob it does not read is refused by name rather than ignored.
MESH = tool.build_mesh(
    mesher="corridor_tin",
    kind="unstructured_tri",
    domain=Ref("reach"),
    extent_km=P.reach_length_km,
    width_m=P.channel_width_m,
    banks=P.bank_source,
    refine={"edge_length": P.mesh_resolution_m, "mode": P.mesh_resolution},
)


def plan(ops):  # noqa: ANN001, ANN201 - the declared plan value, per the design doc
    """The river-tracer recipe. Pure and STATIC: it reads no value, it names them.

    The gates come FIRST so every step and every producer downstream of them runs
    on the approved sheet - a step that had already consumed a value the form can
    revise would be exactly the contradiction the review exists to prevent.
    """
    return [
        FormGate(title="Review the river-tracer scenario"),
        DrawGate(param="release_coords", geometry="point",
                 prompt="Click where the substance enters the river"),
        *ops.acquire_domain(location=P.location, bbox=P.bbox, rivers=D.rivers,
                            discharge=P.discharge_m3s, event_time=P.event_time),
        ReachMesh.corridor(mesh=MESH, seed=Ref("seed")).named("corridor_mesh"),
        ops.author(mesh=MESH, physics=PHYSICS, forcing=FORCING),
        ops.solve(compute_class=P.compute_class, physics=PHYSICS),
        ops.read(Ref("solve"), physics=PHYSICS, forcing=FORCING)
           .chart("dye_concentration", builder=build_dye_chart),
    ]


#: The run's ANSWER, as the numbers a reader has to be able to check. Persisted
#: beside the chart spec so verification cites the run's own figures rather than
#: recomputing them from the raster.
ANSWER = ("dye_cmax_mgl", "dye_peak_time_s", "plume_reach_m", "active_frames",
          "max_deposition_mm", "max_scour_mm", "deposited_mass_kg",
          "deposit_fraction", "sediment_surface_d50_range_um", "mesh_size_m",
          "mesh_node_estimate")


def build_dye_chart(*, result: Any, params: Any) -> dict[str, Any] | None:
    """The plume's rise-to-peak chart SPEC: honest tracer scalars, never a fitted curve.

    Two points, both measured off the postprocessed field - zero concentration at
    release, then the peak at its arrival time. ``None`` when the run measured no
    peak, which is the honest "there was no curve to draw".
    """
    cmax = getattr(result, "dye_cmax_mgl", None)
    peak_t = getattr(result, "dye_peak_time_s", None)
    if cmax is None or peak_t is None:
        return None
    from trid3nt_server.emission.styles import preset_units
    from trid3nt_server.tools.processing.charts_common import build_chart_payload

    where = params.get("location") or getattr(result, "name", None) or "the reach"
    substance = params.get("substance") or "dye"
    # The UNITS come from the style contract, so the chart's axis and the layer's
    # legend cannot disagree about what this field is measured in.
    units = preset_units("continuous_plume_concentration") or "mg/L"
    return build_chart_payload(
        vega_lite_spec={
            "mark": {"type": "line", "point": True},
            "data": {"values": [{"t_s": 0.0, "dye_mgl": 0.0},
                                {"t_s": float(peak_t), "dye_mgl": float(cmax)}]},
            "encoding": {
                "x": {"field": "t_s", "type": "quantitative", "title": "Time (s)"},
                "y": {"field": "dye_mgl", "type": "quantitative",
                      "title": f"{str(substance).capitalize()} concentration ({units})"},
            },
        },
        title=f"Peak {substance} concentration - {where}",
        caption=(f"Reach peak {substance} concentration {float(cmax):.3g} {units}, "
                 f"arriving {float(peak_t):.0f} s after release (idealized-bed demo)."),
    )


#: DECLARED mesh_resolution_m range. The solver floor is the finest edge the mesh
#: builder authors regardless of ask; below it a screening plume gains nothing.
#: There is no fixed coarse ceiling - the node budget coarsens a long reach WITHIN
#: this declaration, and the effective edge stays >= 2 cells across the channel.
_TELEMAC_RIVER_DYE_RES_SPEC = ResolutionSpec(
    param="mesh_resolution_m",
    unit="m",
    min_value=3.0,
    native_hint="NHD channel geometry + 3DEP terrain; edge sized from reach width",
    constraint_source="solver",
    rationale=(
        "explicit target edge length; 3 m is the absolute finest the builder "
        "authors, a long reach is further coarsened under the mesh node budget "
        "(self-labeled); no fixed coarse ceiling"
    ),
)

#: The mesh gate this template stops at is the STANDARD one - the mesh step opens
#: a session, presents the built corridor as an editable layer with its probes,
#: and takes every edit action the ``corridor_tin`` mesher registers - so this
#: template declares no solver gate of its own.
_TELEMAC_RIVER_DYE_METADATA = AtomicToolMetadata(
    name="telemac_river_dye",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="telemac",
    tier="template",
    resolution_specs=(_TELEMAC_RIVER_DYE_RES_SPEC,),
)


#: Wire ALIASES the model uses for values PARAMS already declares.
#: ``release_points`` folds them into the declared params before any door.
_EXTRA_ARGS: tuple[tuple[str, Any], ...] = (
    ("contaminant", str | None),
    ("release_lon", float | None),
    ("release_lat", float | None),
    ("spill_location_latlon", str | None),
)


telemac_river_dye = register_workflow(
    TelemacWorkflow, _TELEMAC_RIVER_DYE_METADATA, PARAMS, plan,
    data=DATA,
    accepts=ACCEPTS,
    answer=ANSWER,
    # The mesh row is present only when a sizing rule MOVED the user's explicit
    # edge length; on an honoured (or absent) override both fields read null.
    provenance=(("discharge_m3s", "discharge_note"),
                ("mesh_resolution_m", "mesh_resolution_note")),
    # The dye maximum is the canonical peak class: measured 6x LOW on the coarse
    # mesh, because a concentration peak lives inside one element. How far the
    # plume REACHED is a front location and moves with it.
    sensitivity=(("dye_cmax_mgl", "peak"),
                 ("plume_reach_m", "location"),
                 ("max_deposition_mm", "peak"),
                 ("max_scour_mm", "peak")),
    coerce=(
        location_or_bbox("telemac_river_dye", code_prefix="TELEMAC",
                         hint="For a natural prompt like 'dye spill in the river "
                              "near <place>', pass location='<place>'."),
        release_points,
        event_time(),
        substance_class(),
        compute_class(),
        user_input.bearing("wind_direction_deg", label="wind_direction_deg",
                           code="TELEMAC_PARAMS_INVALID"),
    ),
    doc=DOC,
    extra_args=_EXTRA_ARGS,
)
