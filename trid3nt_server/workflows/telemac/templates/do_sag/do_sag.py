"""Engine template ``telemac_do_sag`` - the dissolved-oxygen sag below an outfall.

THE QUESTION: the DISSOLVED-OXYGEN SAG below a permitted discharge / WWTP outfall
in a river reach (the US TMDL / Clean Water Act permit question). Where does DO
bottom out downstream, and does it VIOLATE the water-quality standard?
TELEMAC-2D + WAQTEL O2 - the Streeter-Phelps oxygen sag - over a real NHDPlus
reach.

The recipe on one page: the STEERING body of raw keywords, the door that fills
and runs it, the ANSWER fields and the chart. The declared params and the
model-facing prose are one file over in ``declarations.py``; the reach itself is
the shared ``river`` part this body lists.
"""

from __future__ import annotations

from typing import Any

from trid3nt_contracts.telemac_contracts import TELEMAC_DO_STYLE
from trid3nt_contracts.tool_registry import AtomicToolMetadata, ResolutionSpec

from trid3nt_server.workflows.runtime import (
    Ref,
    param_rows,
    register_workflow,
    user_input,
)
from trid3nt_server.workflows.shared.aoi import location_or_bbox
from trid3nt_server.workflows.telemac.authoring.assembler import DO_SAG_OUTFALL_FRAC
from trid3nt_server.workflows.telemac.helpers.forcing import event_time
from trid3nt_server.workflows.telemac.helpers.reach import MeshCoverage
from trid3nt_server.workflows.telemac.helpers.water_quality import WaqtelO2
from trid3nt_server.workflows.telemac.modules import T2D, WAQTEL
from trid3nt_server.workflows.telemac.modules.telemac2d import Boundaries, Release
from trid3nt_server.workflows.telemac.products.products import Products
from trid3nt_server.workflows.telemac.templates.do_sag.declarations import (
    ACCEPTS, DOC, PARAMS, PARAMS as P,
)
from trid3nt_server.workflows.telemac.templates.shared import river
from trid3nt_server.workflows.telemac.templates.shared.river import (
    PARAMS as S,
    RIVER,
)
from trid3nt_server.workflows.telemac.workflow import Door, TelemacWorkflow

__all__ = ["ANSWER", "DATA", "PARAMS", "STEERING", "build_sag_chart",
           "telemac_do_sag"]

_SOLVING = "trid3nt_server.workflows.telemac.solving.solve"

#: What the run directory calls this deck.
_STEERING_FILE = "t2d_river.cas"

#: The reach chain, unchanged: a sag reads exactly what a plume reads.
DATA = river.DATA_ROWS


class STEERING(T2D):
    """The deck: a river, an OUTFALL, and the four tracers the O2 process runs."""

    parts = [RIVER]

    # A coupled run drives the module's own launcher whole rather than the
    # stepped arm, so it writes no restart record and cannot be continued.
    VARIABLES_FOR_GRAPHIC_PRINTOUTS = "U,V,H,S,B,T1,T2,T3,T4"
    GRAPHIC_PRINTOUT_PERIOD = Ref("settled.graphic_period")
    DURATION = P.sim_duration_s

    # The carrier declares ONE tracer; WAQTEL's O2 process appends DISSOLVED O2,
    # ORGANIC LOAD and NH4 LOAD behind it, which is why every array sized to the
    # tracer count below carries four values.
    NUMBER_OF_TRACERS = 1
    NAMES_OF_TRACERS = ["DYE             MG/L"]
    INITIAL_VALUES_OF_TRACERS = [0.0, Ref("waqtel.upstream_do_mgl"), 0.0, 0.0]

    #: CLEAN RIVER at every liquid boundary: no organic load, its own oxygen. The
    #: load enters at the source, so which boundary the engine numbers first
    #: cannot decide the answer.
    boundaries = Boundaries(
        measured=Ref("settled"),
        tracers=[0.0, Ref("waqtel.upstream_do_mgl"), 0.0, 0.0])

    #: The OUTFALL: a permitted discharge does not pulse, so the flow and its
    #: concentrations hold flat across the whole run and the reach reaches the
    #: steady-state sag the question is asked about.
    releases = [Release(at=Ref("settled.release_at"),
                        q=Ref("waqtel.effluent_q_m3s"),
                        tracers=[0.0, Ref("waqtel.effluent_do_mgl"),
                                 Ref("waqtel.effluent_bod_mgl"), 0.0],
                        window_s=None, until_s=Ref("settled.until_s"))]

    #: Deoxygenation balanced by surface reaeration, and nothing else: the
    #: modelled curve is the closed form the question is asked against.
    coupling = [WAQTEL.o2(water_temp_c=Ref("waqtel.water_temp_c"),
                          k1_per_day=Ref("waqtel.k1_per_day"),
                          k2_per_day=Ref("waqtel.k2_per_day"),
                          k2_formula=Ref("waqtel.k2_formula"),
                          saturation_mgl=Ref("waqtel.saturation_mgl"))]


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

    from trid3nt_server.tools.processing.charts_common import build_chart_payload

    # The layer's own declared units, so the chart's axis and the legend cannot
    # disagree about what this field is measured in.
    units = TELEMAC_DO_STYLE["units"]

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


_RES_SPEC = ResolutionSpec(
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

_METADATA = AtomicToolMetadata(
    name="telemac_do_sag",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="telemac",
    tier="template",
    resolution_specs=(_RES_SPEC,),
)


telemac_do_sag = register_workflow(
    TelemacWorkflow, _METADATA,
    (*river.PARAM_ROWS, *param_rows(PARAMS)),
    Door(
        steering=STEERING,
        domain=river.acquire(seed_coords=P.outfall_coords),
        mesh=river.MESH, mesh_on="reach",
        produce=(
            MeshCoverage(mesh=Ref("mesh"), centerline=river.DATA.centerline),
            WaqtelO2(effluent_bod_mgl=P.effluent_bod_mgl,
                     effluent_q_m3s=P.effluent_q_m3s,
                     effluent_do_mgl=P.effluent_do_mgl,
                     upstream_do_mgl=P.upstream_do_mgl,
                     do_saturation_mgl=P.do_saturation_mgl,
                     water_temp_c=P.water_temp_c, k1_per_day=P.k1_per_day,
                     k2_per_day=P.k2_per_day,
                     do_standard_mgl=P.do_standard_mgl).named("waqtel"),
        ),
        # The reach was NAVIGATED downstream from its outfall, so the outfall is
        # this reach's chainage zero and the whole modelled stretch is below it.
        # The source sits just inside that top rather than on it: a source node
        # on the prescribed-flowrate face would compete with the boundary
        # condition for the same node.
        settle=river.settle(release_coords=P.outfall_coords,
                            spill_fraction=DO_SAG_OUTFALL_FRAC,
                            marker_label="Outfall"),
        results=(river.RESULT,),
        steering_file=_STEERING_FILE, prefix="telemac",
        dispatch=f"{_SOLVING}.solve_reach", compute_class=S.compute_class,
        meta={"substance": "effluent", "substance_class": "do_sag"},
        read=lambda run: Products.dissolved_oxygen(
            run=run, solve=run, process=Ref("waqtel"),
            carrier_discharge=Ref("carrier_discharge")).named("do_field"),
        chart=("do_sag_curve", build_sag_chart),
        review_title="Review the outfall and the reach it discharges to"),
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
