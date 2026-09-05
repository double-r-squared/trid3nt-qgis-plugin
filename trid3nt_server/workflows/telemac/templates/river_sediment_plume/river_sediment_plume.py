"""Engine template ``telemac_river_sediment_plume`` - a settling plume in a reach.

THE QUESTION: where a SUSPENDED SEDIMENT plume released into a river settles out
and deposits. TELEMAC-2D coupled with GAIA over a real reach: ONE settling class
over a bed with NO stock at all, so nothing erodes and only what was injected can
deposit - a supply-limited answer.

The recipe on one page: the STEERING body of raw keywords, the door that fills
and runs it, the ANSWER fields and the chart. The declared params and the
model-facing prose are one file over in ``declarations.py``; the reach itself is
the shared ``river`` part this body lists.
"""

from __future__ import annotations

from typing import Any

from trid3nt_contracts.telemac_contracts import TELEMAC_DYE_STYLE
from trid3nt_contracts.tool_registry import AtomicToolMetadata, ResolutionSpec

from trid3nt_server.workflows.runtime import (
    Ref,
    data_rows,
    param_rows,
    register_workflow,
    user_input,
)
from trid3nt_server.workflows.mesh.tool import tool
from trid3nt_server.workflows.shared.aoi import location_or_bbox
from trid3nt_server.workflows.telemac.helpers.forcing import event_time
from trid3nt_server.workflows.telemac.helpers.reach import MeshCoverage
from trid3nt_server.workflows.telemac.helpers.substance import SuspendedClass
from trid3nt_server.workflows.telemac.modules import T2D
from trid3nt_server.workflows.telemac.modules.gaia import RESULT_FILENAME
from trid3nt_server.workflows.telemac.modules.telemac2d import (
    Boundaries,
    Rain,
    Release,
    Wind,
)
from trid3nt_server.workflows.telemac.products.products import Products
from trid3nt_server.workflows.telemac.solving.solve import compute_class
from trid3nt_server.workflows.telemac.templates.river_dye.coercions import (
    release_points,
)
from trid3nt_server.workflows.telemac.templates.river_sediment_plume.declarations import (
    ACCEPTS, DOC, PARAMS, PARAMS as P,
)
from trid3nt_server.workflows.telemac.templates.shared import river
from trid3nt_server.workflows.telemac.templates.shared.river import (
    PARAMS as S,
    RELEASE as R,
    RIVER,
)
from trid3nt_server.workflows.telemac.workflow import Door, TelemacWorkflow

__all__ = ["ANSWER", "DATA", "PARAMS", "STEERING", "build_plume_chart",
           "telemac_river_sediment_plume"]

_HELPERS = "trid3nt_server.workflows.telemac.helpers"
_SOLVING = "trid3nt_server.workflows.telemac.solving.solve"

#: What the run directory calls this deck.
_STEERING_FILE = "t2d_river.cas"


class _OWN:
    """What this question reads past the reach chain every river run reads."""

    rain = tool(f"{_HELPERS}.forcing.resolve_rain_forcing",
                rainfall_mm_per_day=R.rainfall_mm_per_day,
                evaporation_mm_per_day=R.evaporation_mm_per_day,
                gridmet_window=R.rainfall_gridmet_window
                ).resample(to="1D", max_gap="native*3").normalize(units="mm/day")


DATA = (*river.DATA_ROWS, *data_rows(_OWN))


class STEERING(T2D):
    """The deck: a river, and ONE settling class released into it."""

    parts = [RIVER]

    # A coupled run drives the module's own launcher whole rather than the
    # stepped arm, so it writes no restart record and cannot be continued.
    #: GAIA's suspended class arrives at the carrier as a SECOND tracer, so the
    #: file has to output it and every array sized to the tracer count carries
    #: two values - the visible half of what this fork costs.
    VARIABLES_FOR_GRAPHIC_PRINTOUTS = "U,V,H,S,B,T1,T2"
    GRAPHIC_PRINTOUT_PERIOD = Ref("settled.graphic_period")
    DURATION = P.sim_duration_s

    NUMBER_OF_TRACERS = 1
    NAMES_OF_TRACERS = ["DYE             MG/L"]
    INITIAL_VALUES_OF_TRACERS = [0.0]

    #: TWO values on every liquid boundary - the carrier's own tracer and the
    #: suspended class behind it - or the solver refuses for want of values.
    boundaries = Boundaries(measured=Ref("settled"), tracers=[0.0, 0.0])

    #: The marker the deposited fraction is measured against, released as a
    #: finite pulse at the same source the class is injected at.
    releases = [Release(at=Ref("settled.release_at"), q=R.source_q_m3s,
                        tracers=[P.sediment_concentration_mgl],
                        window_s=R.spill_duration_s,
                        until_s=Ref("settled.until_s"))]

    #: The settling class itself: zero initial thickness, so nothing erodes and
    #: only the injected pulse deposits.
    coupling = Ref("sediment.coupling")

    wind = Wind(speed_mps=R.wind_speed_mps, from_deg=R.wind_direction_deg)
    rain = Rain(mm_per_day=Ref("settled.rain_mm_per_day"), tracers=2)


#: The run's ANSWER, as the numbers a reader has to be able to check.
ANSWER = ("dye_cmax_mgl", "dye_peak_time_s", "plume_reach_m", "active_frames",
          "max_deposition_mm", "deposited_mass_kg", "deposit_fraction",
          "mesh_size_m")


def build_plume_chart(*, result: Any, params: Any) -> dict[str, Any] | None:
    """The suspended concentration's HISTORY: one point per frame the solver wrote.

    Every point is the reach maximum at that output time, so the curve is the
    arrival, the peak and the settling-out as the run produced them. ``None``
    when the run persisted no history.
    """
    times = getattr(result, "dye_curve_time_s", None)
    values = getattr(result, "dye_curve_cmax_mgl", None)
    cmax = getattr(result, "dye_cmax_mgl", None)
    if not times or not values or cmax is None:
        return None
    from trid3nt_server.tools.processing.charts_common import build_chart_payload

    where = params.get("location") or getattr(result, "name", None) or "the reach"
    units = TELEMAC_DYE_STYLE["units"]
    deposited = getattr(result, "deposit_fraction", None)
    return build_chart_payload(
        vega_lite_spec={
            "mark": {"type": "line", "point": True},
            "data": {"values": [{"t_s": float(t), "mgl": float(c)}
                                for t, c in zip(times, values)]},
            "encoding": {
                "x": {"field": "t_s", "type": "quantitative", "title": "Time (s)"},
                "y": {"field": "mgl", "type": "quantitative",
                      "title": f"Suspended concentration ({units})"},
            },
        },
        title=f"Reach maximum suspended sediment - {where}",
        caption=(f"The highest suspended concentration anywhere in the reach at "
                 f"each of {len(times)} output times; peaks at {float(cmax):.3g} "
                 f"{units}"
                 + ("." if deposited is None else
                    f", and {float(deposited):.1%} of what was injected had "
                    "settled onto the bed by the end.")),
    )


_RES_SPEC = ResolutionSpec(
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

_METADATA = AtomicToolMetadata(
    name="telemac_river_sediment_plume",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="telemac",
    tier="template",
    resolution_specs=(_RES_SPEC,),
)

_EXTRA_ARGS: tuple[tuple[str, Any], ...] = (
    ("release_lon", float | None),
    ("release_lat", float | None),
    ("spill_location_latlon", str | None),
)


telemac_river_sediment_plume = register_workflow(
    TelemacWorkflow, _METADATA,
    (*river.PARAM_ROWS, *river.RELEASE_ROWS, *param_rows(PARAMS)),
    Door(
        steering=STEERING,
        domain=river.acquire(seed_coords=R.reach_seed_coords),
        mesh=river.MESH, mesh_on="reach",
        produce=(MeshCoverage(mesh=Ref("mesh"),
                              centerline=river.DATA.centerline),),
        settle=river.settle(release_coords=R.release_coords,
                            spill_fraction=R.spill_fraction, rain=_OWN.rain),
        derive=(SuspendedClass(
            grain_size_um=P.grain_size_um,
            concentration_mgl=P.sediment_concentration_mgl,
            injected={"q_m3s": R.source_q_m3s,
                      "concentration_mgl": P.sediment_concentration_mgl,
                      "window_s": R.spill_duration_s}).named("sediment"),),
        slots={"VELOCITY_DIFFUSIVITY": R.velocity_diffusivity,
               "COEFFICIENT_FOR_DIFFUSION_OF_TRACERS": R.tracer_diffusivity},
        results=(river.RESULT, RESULT_FILENAME),
        steering_file=_STEERING_FILE, prefix="telemac",
        dispatch=f"{_SOLVING}.solve_reach", compute_class=S.compute_class,
        meta={"substance": "suspended sediment", "substance_class": "sediment",
              "sediment_injected_kg": Ref("sediment.injected_kg"),
              "sediment_n_classes": Ref("sediment.n_classes")},
        read=lambda run: Products.dye(
            run=run, solve=run,
            carrier_discharge=Ref("carrier_discharge")).named("plume"),
        chart=("suspended_sediment_concentration", build_plume_chart),
        review_title="Review the sediment-plume scenario"),
    data=DATA,
    accepts=ACCEPTS,
    answer=ANSWER,
    provenance=(("discharge_m3s", "discharge_note"),),
    sensitivity=(("dye_cmax_mgl", "peak"),
                 ("plume_reach_m", "location"),
                 ("max_deposition_mm", "peak")),
    coerce=(
        location_or_bbox("telemac_river_sediment_plume", code_prefix="TELEMAC",
                         hint="For a natural prompt like 'sediment spill in the "
                              "river near <place>', pass location='<place>'."),
        release_points,
        event_time(),
        compute_class(),
        user_input.bearing("wind_direction_deg", label="wind_direction_deg",
                           code="TELEMAC_PARAMS_INVALID"),
    ),
    doc=DOC,
    extra_args=_EXTRA_ARGS,
)
