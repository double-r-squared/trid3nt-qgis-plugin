"""Engine template ``telemac_river_dye`` - a conservative plume down a river reach.

THE QUESTION: how far a DYE / TRACER / CONTAMINANT spill travels DOWNSTREAM in a
river reach, and what its peak concentration is. TELEMAC-2D shallow water over a
real reach, with the plume animated from the native time-stepped mesh.

The recipe on one page: the STEERING body of raw keywords, the data this run
consumes past the shared chain, the door that fills and runs it, the ANSWER
fields and the chart. The declared params and the model-facing prose are one
file over in ``declarations.py``; the reach itself - its chain, its mesh recipe
and everything a river deck states about the water - is the shared ``river``
part this body lists.
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
from trid3nt_server.workflows.telemac.authoring.assembler import settle_reach
from trid3nt_server.workflows.telemac.helpers.forcing import event_time
from trid3nt_server.workflows.telemac.helpers.reach import MeshCoverage
from trid3nt_server.workflows.telemac.helpers.substance import Decay
from trid3nt_server.workflows.telemac.modules import T2D
from trid3nt_server.workflows.telemac.modules.telemac2d import (
    Boundaries,
    Continuation,
    Rain,
    Release,
    Wind,
)
from trid3nt_server.workflows.telemac.products.products import Products
from trid3nt_server.workflows.telemac.solving.solve import compute_class
from trid3nt_server.workflows.telemac.templates.river_dye.coercions import (
    release_points,
)
from trid3nt_server.workflows.telemac.templates.river_dye.declarations import (
    ACCEPTS, DOC, PARAMS, PARAMS as P,
)
from trid3nt_server.workflows.telemac.templates.shared import river
from trid3nt_server.workflows.telemac.templates.shared.river import (
    PARAMS as S,
    RELEASE as R,
    RIVER,
)
from trid3nt_server.workflows.telemac.workflow import Door, TelemacWorkflow

__all__ = ["ANSWER", "DATA", "PARAMS", "STEERING", "build_dye_chart",
           "telemac_river_dye"]

_HELPERS = "trid3nt_server.workflows.telemac.helpers"
_SOLVING = "trid3nt_server.workflows.telemac.solving.solve"

#: What the run directory calls this deck. It is the steering file's own name,
#: so the directory reads as the record of the run it is.
_STEERING_FILE = "t2d_river.cas"


class _OWN:
    """What this question reads past the reach chain every river run reads."""

    # The cadence and units the run receives, stated rather than assumed: the
    # producer answers in daily rates, so this asks for no interpolation - and a
    # sub-daily target would refuse here instead of manufacturing a storm shape
    # gridMET never reported.
    rain = tool(f"{_HELPERS}.forcing.resolve_rain_forcing",
                rainfall_mm_per_day=R.rainfall_mm_per_day,
                evaporation_mm_per_day=R.evaporation_mm_per_day,
                gridmet_window=R.rainfall_gridmet_window
                ).resample(to="1D", max_gap="native*3").normalize(units="mm/day")


#: The reach chain, plus the one row this question adds to it.
DATA = (*river.DATA_ROWS, *data_rows(_OWN))


class STEERING(T2D):
    """The deck: a river, and ONE conservative tracer released into it."""

    parts = [RIVER]

    #: The engine's own last instant, in the double precision a continuation
    #: reads. An uncoupled run is the only one that can be continued, so it is
    #: the only one asked to write this.
    RESTART_FILE = river.RESTART

    VARIABLES_FOR_GRAPHIC_PRINTOUTS = "U,V,H,S,B,T1"
    GRAPHIC_PRINTOUT_PERIOD = Ref("settled.graphic_period")
    DURATION = P.sim_duration_s

    NUMBER_OF_TRACERS = 1
    NAMES_OF_TRACERS = ["DYE             MG/L"]
    INITIAL_VALUES_OF_TRACERS = [0.0]

    #: The carrier's own boundary values. ONE tracer, so every liquid boundary
    #: carries one clean-river value - the arity of this list is what moves when
    #: the question does.
    boundaries = Boundaries(measured=Ref("settled"), tracers=[0.0])

    #: A FINITE pulse at a mid-reach point source, so the slug advects downstream
    #: and dilutes instead of saturating the domain.
    releases = [Release(at=Ref("settled.release_at"), q=R.source_q_m3s,
                        tracers=[P.dye_concentration_mgl],
                        window_s=R.spill_duration_s,
                        until_s=Ref("settled.until_s"))]

    #: The three optional forcings. Each states NOTHING when it was given
    #: nothing: no wind speed is no wind, no resolved rate is no rain, and a run
    #: that continues nothing states its own initial conditions.
    wind = Wind(speed_mps=R.wind_speed_mps, from_deg=R.wind_direction_deg)
    rain = Rain(mm_per_day=Ref("settled.rain_mm_per_day"), tracers=1)
    continue_from = Continuation(previous=Ref("settled.continue_from"))
    #: First-order degradation on the same tracer - no new tracer - when a
    #: decaying substance was named or a half-life stated.
    coupling = Ref("decay.coupling")


#: The run's ANSWER, as the numbers a reader has to be able to check. Persisted
#: beside the chart spec so verification cites the run's own figures rather than
#: recomputing them from the raster.
ANSWER = ("dye_cmax_mgl", "dye_peak_time_s", "plume_reach_m", "active_frames",
          "mesh_size_m")


def _bed_of(result: Any) -> str:
    """The bed the run's mesh was painted from, off the layer's own provenance."""
    for row in getattr(result, "synthetic_inputs", None) or ():
        if getattr(row, "param", None) == "mesh_bed" and row.value:
            return str(row.value)
    return "unrecorded bed"


def build_dye_chart(*, result: Any, params: Any) -> dict[str, Any] | None:
    """The plume's concentration HISTORY: one point per frame the solver wrote.

    Every point is the reach maximum at that output time, measured off the
    postprocessed field, so the curve is the arrival, the peak and the flush-out
    as the run produced them. ``None`` when the run persisted no history, which
    is the honest "there was no curve to draw".
    """
    times = getattr(result, "dye_curve_time_s", None)
    values = getattr(result, "dye_curve_cmax_mgl", None)
    cmax = getattr(result, "dye_cmax_mgl", None)
    peak_t = getattr(result, "dye_peak_time_s", None)
    if not times or not values or cmax is None or peak_t is None:
        return None
    from trid3nt_server.tools.processing.charts_common import build_chart_payload

    where = params.get("location") or getattr(result, "name", None) or "the reach"
    substance = params.get("decaying_substance") or "dye"
    # The layer's own declared units, so the chart's axis and the legend cannot
    # disagree about what this field is measured in.
    units = TELEMAC_DYE_STYLE["units"]
    return build_chart_payload(
        vega_lite_spec={
            "mark": {"type": "line", "point": True},
            "data": {"values": [{"t_s": float(t), "dye_mgl": float(c)}
                                for t, c in zip(times, values)]},
            "encoding": {
                "x": {"field": "t_s", "type": "quantitative", "title": "Time (s)"},
                "y": {"field": "dye_mgl", "type": "quantitative",
                      "title": f"{str(substance).capitalize()} concentration ({units})"},
            },
        },
        title=f"Reach maximum {substance} concentration - {where}",
        caption=(f"The highest {substance} concentration anywhere in the reach at "
                 f"each of {len(times)} output times; peaks at {float(cmax):.3g} "
                 f"{units}, {float(peak_t):.0f} s after release. Bed: "
                 f"{_bed_of(result)}."),
    )


#: DECLARED mesh_resolution_m range. The solver floor is the finest edge the mesh
#: builder authors regardless of ask; below it a screening plume gains nothing.
#: There is no fixed coarse ceiling - the node budget coarsens a long reach WITHIN
#: this declaration, and the effective edge stays >= 2 cells across the channel.
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

#: The mesh gate this template stops at is the STANDARD one - the mesh step opens
#: a session, presents the built reach as an editable layer with its probes, and
#: takes every edit action the ``om2d`` mesher registers - so this template
#: declares no solver gate of its own.
_METADATA = AtomicToolMetadata(
    name="telemac_river_dye",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="telemac",
    tier="template",
    resolution_specs=(_RES_SPEC,),
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
    TelemacWorkflow, _METADATA,
    (*river.PARAM_ROWS, *river.RELEASE_ROWS, *param_rows(PARAMS)),
    Door(
        steering=STEERING,
        domain=river.acquire(seed_coords=R.reach_seed_coords),
        mesh=river.MESH, mesh_on="reach",
        produce=(
            MeshCoverage(mesh=Ref("mesh"), centerline=river.DATA.centerline),
            Decay(substance=P.decaying_substance,
                  half_life_hours=P.decay_half_life_hours,
                  rate_per_day=P.decay_rate_per_day).named("decay"),
        ),
        settle=river.settle(release_coords=R.release_coords,
                            spill_fraction=R.spill_fraction,
                            rain=_OWN.rain, continue_from=P.continue_from),
        # The two constitutive knobs a caller may override. An unset one is not
        # a statement, so the shared part's own value stands.
        slots={"VELOCITY_DIFFUSIVITY": R.velocity_diffusivity,
               "COEFFICIENT_FOR_DIFFUSION_OF_TRACERS": R.tracer_diffusivity},
        results=(river.RESULT, river.RESTART),
        steering_file=_STEERING_FILE, prefix="telemac",
        dispatch=f"{_SOLVING}.solve_reach", compute_class=S.compute_class,
        meta={"substance": "dye", "substance_class": "tracer"},
        read=lambda run: Products.dye(
            run=run, solve=run,
            carrier_discharge=Ref("carrier_discharge")).named("plume"),
        chart=("dye_concentration", build_dye_chart),
        review_title="Review the river-tracer scenario"),
    data=DATA,
    accepts=ACCEPTS,
    answer=ANSWER,
    provenance=(("discharge_m3s", "discharge_note"),),
    # The dye maximum is the canonical peak class: measured 6x LOW on the coarse
    # mesh, because a concentration peak lives inside one element. How far the
    # plume REACHED is a front location and moves with it.
    sensitivity=(("dye_cmax_mgl", "peak"),
                 ("plume_reach_m", "location")),
    coerce=(
        location_or_bbox("telemac_river_dye", code_prefix="TELEMAC",
                         hint="For a natural prompt like 'dye spill in the river "
                              "near <place>', pass location='<place>'."),
        release_points,
        event_time(),
        compute_class(),
        user_input.bearing("wind_direction_deg", label="wind_direction_deg",
                           code="TELEMAC_PARAMS_INVALID"),
    ),
    doc=DOC,
    extra_args=_EXTRA_ARGS,
)
