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
    D,
    Data,
    DrawGate,
    Fetch,
    Forcing,
    P,
    Physics,
    Ref,
    RunMode,
    register_workflow,
    user_input,
)
from trid3nt_server.workflows.mesh.tool import tool
from trid3nt_server.workflows.shared.aoi import location_or_bbox
from trid3nt_server.workflows.telemac.do_sag.declarations import (
    COMPATIBLE, DOC, PARAMS,
)
from trid3nt_server.workflows.telemac.steps import (
    ReachMesh,
    ReviewResolvedInputs,
    WaqtelO2,
    event_time,
)
from trid3nt_server.workflows.telemac.workflow import TelemacWorkflow

__all__ = ["ANSWER", "DATA", "PARAMS", "build_sag_chart", "plan", "telemac_do_sag"]

_STEPS = "trid3nt_server.workflows.telemac.steps"


#: The reach's REFERENCE data - fetched fresh for the domain the geocode step
#: binds, never supplied. The carrier discharge is a STEP rather than Data: it reads
#: the resolved mid-reach seed, which is a step result and not something a
#: producer declaration can name.
DATA = (
    Data("rivers", Fetch.tool(f"{_STEPS}.reach.fetch_reach_flowline", prefetched=None)),
)


# -- the binding blocks --------------------------------------------------- #
# What the run IS, declared as frozen values above the recipe that assembles
# them. Every member is a late-bound read (P.<param> / D.<data> / Ref) that the
# interpreter substitutes against the approved sheet, so the blocks are
# process-lifetime constants and the plan is a pure assembly of them.

PHYSICS = Physics("waqtel_o2",
                  do_sag_config=Ref("waqtel"),
                  reach_seed_coords=P.outfall_coords,
                  sim_duration_s=P.sim_duration_s,
                  output_interval_min=P.output_interval_min)

FORCING = Forcing(carrier=Ref("reviewed_discharge"))

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
    """The DO-sag recipe. Pure and STATIC: it reads no value, it names them."""
    return [
        DrawGate(param="outfall_coords", geometry="point",
                 prompt="Click where the discharge enters the river"),
        *ops.acquire_domain(location=P.location, bbox=P.bbox, rivers=D.rivers,
                            discharge=P.discharge_m3s, event_time=P.event_time),
        WaqtelO2(discharge_bod_mgl=P.discharge_bod_mgl,
                 upstream_do_mgl=P.upstream_do_mgl,
                 do_saturation_mgl=P.do_saturation_mgl,
                 water_temp_c=P.water_temp_c, k1_per_day=P.k1_per_day,
                 k2_per_day=P.k2_per_day,
                 do_standard_mgl=P.do_standard_mgl).named("waqtel"),
        ReviewResolvedInputs(carrier_discharge=Ref("carrier_discharge"),
                             bank_source=P.bank_source, workflow=ops.name,
                             input_mode=RunMode).named("reviewed_discharge"),
        ReachMesh.corridor(mesh=MESH, seed=Ref("seed")).named("corridor_mesh"),
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
          "bod_upstream_mgl", "sag_curve_distance_m", "sag_curve_do_mgl",
          "sag_curve_bod_mgl", "mesh_size_m")


def build_sag_chart(*, result: Any, params: Any) -> dict[str, Any] | None:
    """The DO-sag chart SPEC: DO + CBOD vs downstream distance, standard as a rule.

    Honest postprocess scalars off the published layer (the binned centerline
    curve), never a fabricated line; ``None`` when the curve is absent.
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
    vega_lite_spec = {
        "layer": [
            {"mark": {"type": "line", "point": False},
             "data": {"values": do_vals + bod_vals},
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
    return build_chart_payload(
        vega_lite_spec=vega_lite_spec,
        title=title,
        caption=(
            f"Streeter-Phelps DO sag: minimum {dmin} {units} at {dloc} m downstream "
            f"({verdict} the {std:g} {units} standard, dashed). CBOD decay drives the "
            f"sag; reaeration recovers it. Screening/permit grade."
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
    compatible=COMPATIBLE,
    answer=ANSWER,
    # The mesh row is present only when a sizing rule MOVED the user's explicit
    # edge length; on an honoured (or absent) override both fields read null.
    provenance=(("discharge_m3s", "discharge_note"),
                ("mesh_resolution_m", "mesh_resolution_note")),
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
