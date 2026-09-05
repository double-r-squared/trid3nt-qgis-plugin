"""Engine template ``telemac_river_oil_spill`` - an oil slick down a river reach.

THE QUESTION: where an OIL SPILL goes after it enters a river - the floating
slick's drift and the dissolved fraction's plume. TELEMAC-2D shallow water over a
real reach with the engine's own oil-spill module riding on the solve: the module
tracks floating particles and the tracer carries what dissolved.

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
from trid3nt_server.workflows.telemac.modules import T2D
from trid3nt_server.workflows.telemac.modules.telemac2d import (
    Boundaries,
    Oil,
    Rain,
    Release,
    Wind,
)
from trid3nt_server.workflows.telemac.products.products import Products
from trid3nt_server.workflows.telemac.solving.solve import compute_class, solve_reach
from trid3nt_server.workflows.telemac.templates.river_dye.coercions import (
    release_points,
)
from trid3nt_server.workflows.telemac.templates.river_oil_spill.declarations import (
    ACCEPTS, DOC, PARAMS, PARAMS as P,
)
from trid3nt_server.workflows.telemac.templates.shared import river
from trid3nt_server.workflows.telemac.templates.shared.river import (
    PARAMS as S,
    RELEASE as R,
    RIVER,
)
from trid3nt_server.workflows.telemac.workflow import Door, TelemacWorkflow

__all__ = ["ANSWER", "DATA", "PARAMS", "STEERING", "build_slick_chart",
           "telemac_river_oil_spill"]

_HELPERS = "trid3nt_server.workflows.telemac.helpers"

#: What the run directory calls this deck.
_STEERING_FILE = "t2d_river.cas"
#: The particle track the slick reader parses. The slick and the particle
#: snapshots are built from it on the server, so neither is a file the worker
#: writes for itself.
_DROGUES = "drogues.txt"


class _OWN:
    """What this question reads past the reach chain every river run reads."""

    rain = tool(f"{_HELPERS}.forcing.resolve_rain_forcing",
                rainfall_mm_per_day=R.rainfall_mm_per_day,
                evaporation_mm_per_day=R.evaporation_mm_per_day,
                gridmet_window=R.rainfall_gridmet_window
                ).resample(to="1D", max_gap="native*3").normalize(units="mm/day")


DATA = (*river.DATA_ROWS, *data_rows(_OWN))


class STEERING(T2D):
    """The deck: a river, a slick on it, and the fraction that dissolved."""

    parts = [RIVER]

    #: The engine's own last instant, in the double precision a continuation
    #: reads. This run couples nothing, so it is one of the two that can write it.
    RESTART_FILE = river.RESTART

    VARIABLES_FOR_GRAPHIC_PRINTOUTS = "U,V,H,S,B,T1"
    GRAPHIC_PRINTOUT_PERIOD = Ref("settled.graphic_period")
    DURATION = P.sim_duration_s

    NUMBER_OF_TRACERS = 1
    NAMES_OF_TRACERS = ["DYE             MG/L"]
    INITIAL_VALUES_OF_TRACERS = [0.0]

    #: ONE tracer, so every liquid boundary carries one clean-river value.
    boundaries = Boundaries(measured=Ref("settled"), tracers=[0.0])

    #: The dissolved fraction, released as a FINITE pulse at the same point the
    #: floats are compiled to enter at.
    releases = [Release(at=Ref("settled.release_at"), q=R.source_q_m3s,
                        tracers=[P.oil_concentration_mgl],
                        window_s=R.spill_duration_s,
                        until_s=Ref("settled.until_s"))]

    #: The module itself: its own preset file and the per-run source the release
    #: coordinates are compiled into, plus the drogues the slick is drawn from.
    oil = Oil(steering=Ref("settled.oil.steering"),
              fortran=Ref("settled.oil.fortran"),
              release_step=P.oil_release_step, drogues=P.n_drogues,
              drogues_period_steps=Ref("settled.oil.period_steps"))

    wind = Wind(speed_mps=R.wind_speed_mps, from_deg=R.wind_direction_deg)
    rain = Rain(mm_per_day=Ref("settled.rain_mm_per_day"), tracers=1)


#: The run's ANSWER, as the numbers a reader has to be able to check.
ANSWER = ("dye_cmax_mgl", "dye_peak_time_s", "plume_reach_m", "active_frames",
          "mesh_size_m")


def build_slick_chart(*, result: Any, params: Any) -> dict[str, Any] | None:
    """The dissolved fraction's concentration HISTORY, one point per written frame.

    The slick itself is a particle layer on the canvas; what a curve can say is
    how the DISSOLVED fraction arrived, peaked and flushed out, measured off the
    postprocessed field. ``None`` when the run persisted no history.
    """
    times = getattr(result, "dye_curve_time_s", None)
    values = getattr(result, "dye_curve_cmax_mgl", None)
    cmax = getattr(result, "dye_cmax_mgl", None)
    peak_t = getattr(result, "dye_peak_time_s", None)
    if not times or not values or cmax is None or peak_t is None:
        return None
    from trid3nt_server.tools.processing.charts_common import build_chart_payload

    where = params.get("location") or getattr(result, "name", None) or "the reach"
    oil = params.get("oil_type") or "oil"
    units = TELEMAC_DYE_STYLE["units"]
    return build_chart_payload(
        vega_lite_spec={
            "mark": {"type": "line", "point": True},
            "data": {"values": [{"t_s": float(t), "dye_mgl": float(c)}
                                for t, c in zip(times, values)]},
            "encoding": {
                "x": {"field": "t_s", "type": "quantitative", "title": "Time (s)"},
                "y": {"field": "dye_mgl", "type": "quantitative",
                      "title": f"Dissolved {oil} concentration ({units})"},
            },
        },
        title=f"Reach maximum dissolved {oil} concentration - {where}",
        caption=(f"The highest dissolved concentration anywhere in the reach at "
                 f"each of {len(times)} output times; peaks at {float(cmax):.3g} "
                 f"{units}, {float(peak_t):.0f} s after release. The floating "
                 "slick is the particle layer beside this, not this curve."),
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
    name="telemac_river_oil_spill",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="telemac",
    tier="template",
    resolution_specs=(_RES_SPEC,),
)

#: Wire ALIASES the model uses for values PARAMS already declares.
_EXTRA_ARGS: tuple[tuple[str, Any], ...] = (
    ("release_lon", float | None),
    ("release_lat", float | None),
    ("spill_location_latlon", str | None),
)


telemac_river_oil_spill = register_workflow(
    TelemacWorkflow, _METADATA,
    (*river.PARAM_ROWS, *river.RELEASE_ROWS, *param_rows(PARAMS)),
    Door(
        steering=STEERING,
        domain=river.acquire(seed_coords=R.reach_seed_coords),
        mesh=river.MESH, mesh_on="reach",
        produce=(MeshCoverage(mesh=Ref("mesh"),
                              centerline=river.DATA.centerline),),
        settle=river.settle(release_coords=R.release_coords,
                            spill_fraction=R.spill_fraction,
                            rain=_OWN.rain,
                            oil={"preset": P.oil_type,
                                 "release_step": P.oil_release_step,
                                 "drogues_period_s": P.drogues_period_s}),
        # The two constitutive knobs a caller may override. An unset one is not
        # a statement, so the shared part's own value stands.
        slots={"VELOCITY_DIFFUSIVITY": R.velocity_diffusivity,
               "COEFFICIENT_FOR_DIFFUSION_OF_TRACERS": R.tracer_diffusivity},
        results=(river.RESULT, river.RESTART, _DROGUES),
        steering_file=_STEERING_FILE, prefix="telemac",
        dispatch=solve_reach, compute_class=S.compute_class,
        meta={"substance": "oil", "substance_class": "oil"},
        read=lambda run: Products.dye(
            run=run, solve=run,
            carrier_discharge=Ref("carrier_discharge")).named("slick"),
        chart=("dissolved_oil_concentration", build_slick_chart),
        review_title="Review the oil spill scenario"),
    data=DATA,
    accepts=ACCEPTS,
    answer=ANSWER,
    provenance=(("discharge_m3s", "discharge_note"),),
    # The dissolved maximum is the canonical peak class: a concentration peak
    # lives inside one element. How far the slick REACHED is a front location and
    # moves with it.
    sensitivity=(("dye_cmax_mgl", "peak"),
                 ("plume_reach_m", "location")),
    coerce=(
        location_or_bbox("telemac_river_oil_spill", code_prefix="TELEMAC",
                         hint="For a natural prompt like 'oil spill on the river "
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
