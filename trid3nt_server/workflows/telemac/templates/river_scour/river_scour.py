"""Engine template ``telemac_river_scour`` - a mobile bed under a river reach.

THE QUESTION: where the river bed SCOURS and where it re-deposits - below a dam,
a weir or a bridge contraction, under a flood, and whether a graded mixture SORTS
as it goes. TELEMAC-2D coupled with GAIA over a real reach, with the bed
evolution animated from the native time-stepped mesh; the NESTOR dig/dump rule
layers a channel-maintenance dredge on top of the same bed.

The recipe on one page: the STEERING body of raw keywords, the door that fills
and runs it, the ANSWER fields and the chart. The declared params and the
model-facing prose are one file over in ``declarations.py``; the reach itself is
the shared ``river`` part this body lists.
"""

from __future__ import annotations

from typing import Any

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
from trid3nt_server.workflows.telemac.helpers.substance import SedimentBed
from trid3nt_server.workflows.telemac.modules import T2D
from trid3nt_server.workflows.telemac.modules.gaia import RESULT_FILENAME
from trid3nt_server.workflows.telemac.modules.telemac2d import (
    Boundaries,
    Rain,
    Release,
    TimeOrigin,
    Wind,
)
from trid3nt_server.workflows.telemac.products.products import Products
from trid3nt_server.workflows.telemac.solving.solve import compute_class, solve_reach
from trid3nt_server.workflows.telemac.templates.river_dye.coercions import (
    release_points,
)
from trid3nt_server.workflows.telemac.templates.river_scour.declarations import (
    ACCEPTS, DOC, PARAMS, PARAMS as P,
)
from trid3nt_server.workflows.telemac.templates.shared import river
from trid3nt_server.workflows.telemac.templates.shared.river import (
    PARAMS as S,
    RELEASE as R,
    RIVER,
)
from trid3nt_server.workflows.telemac.workflow import Door, TelemacWorkflow

__all__ = ["ANSWER", "DATA", "PARAMS", "STEERING", "build_bed_chart",
           "telemac_river_scour"]

_HELPERS = "trid3nt_server.workflows.telemac.helpers"

#: What the run directory calls this deck.
_STEERING_FILE = "t2d_river.cas"

#: WHERE along the reach a maintenance dredge acts, and OVER WHAT stretch of the
#: run. A dig at mid-reach with the spoil placed well downstream of it is the
#: shape of a maintenance campaign; the window opens after the flow has
#: established and closes before the run does, so the bed has time to respond to
#: what was dug. The dig RATE is the criterion mode's own, in metres per second
#: of bed lowering.
_DIG_STATION_FRAC = 0.5
_DUMP_STATION_FRAC = 0.85
_DREDGE_ZONE_LEN_M = 200.0
_DREDGE_START_FRAC = 0.15
_DREDGE_END_FRAC = 0.95
_DREDGE_RATE_M_PER_S = 5.0e-4


class _OWN:
    """What this question reads past the reach chain every river run reads."""

    rain = tool(f"{_HELPERS}.forcing.resolve_rain_forcing",
                rainfall_mm_per_day=R.rainfall_mm_per_day,
                evaporation_mm_per_day=R.evaporation_mm_per_day,
                gridmet_window=R.rainfall_gridmet_window
                ).resample(to="1D", max_gap="native*3").normalize(units="mm/day")


DATA = (*river.DATA_ROWS, *data_rows(_OWN))


class STEERING(T2D):
    """The deck: a river, a marker tracer in it, and a BED that moves under it."""

    parts = [RIVER]

    # A coupled run drives the module's own launcher whole rather than the
    # stepped arm, so it writes no restart record and cannot be continued.
    VARIABLES_FOR_GRAPHIC_PRINTOUTS = "U,V,H,S,B,T1"
    GRAPHIC_PRINTOUT_PERIOD = Ref("settled.graphic_period")
    DURATION = P.sim_duration_s

    NUMBER_OF_TRACERS = 1
    NAMES_OF_TRACERS = ["DYE             MG/L"]
    INITIAL_VALUES_OF_TRACERS = [0.0]

    #: The bed is GAIA's; the carrier still runs ONE tracer, so every liquid
    #: boundary carries one clean-river value.
    boundaries = Boundaries(measured=Ref("settled"), tracers=[0.0])

    #: The marker the deposited fraction is measured against, released as a
    #: finite pulse at a mid-reach point source.
    releases = [Release(at=Ref("settled.release_at"), q=R.source_q_m3s,
                        tracers=[P.tracer_concentration_mgl],
                        window_s=R.spill_duration_s,
                        until_s=Ref("settled.until_s"))]

    #: The bed itself: one class or a mixture, bedload on, a real stock to scour
    #: into, and the NESTOR files when a dredge rule was armed.
    coupling = Ref("sediment.coupling")
    #: NESTOR reads absolute DATES; a run without it leaves the dictionary's own
    #: origin, which is what states nothing here.
    time_origin = TimeOrigin(at=Ref("settled.dredging.time_origin"))

    wind = Wind(speed_mps=R.wind_speed_mps, from_deg=R.wind_direction_deg)
    rain = Rain(mm_per_day=Ref("settled.rain_mm_per_day"), tracers=1)


#: The run's ANSWER, as the numbers a reader has to be able to check.
ANSWER = ("max_deposition_mm", "max_scour_mm", "deposited_mass_kg",
          "deposit_fraction", "sediment_surface_d50_range_um", "active_frames",
          "mesh_size_m")


def build_bed_chart(*, result: Any, params: Any) -> dict[str, Any] | None:
    """The marker tracer's HISTORY beside the bed change the run reports.

    The bed evolution is a map, not a curve; what a curve can say is when the
    flow that moved it arrived, which is the tracer's own reach maximum at each
    written frame. ``None`` when the run persisted no history.
    """
    times = getattr(result, "dye_curve_time_s", None)
    values = getattr(result, "dye_curve_cmax_mgl", None)
    if not times or not values:
        return None
    from trid3nt_server.tools.processing.charts_common import build_chart_payload

    where = params.get("location") or getattr(result, "name", None) or "the reach"
    scour = getattr(result, "max_scour_mm", None)
    deposition = getattr(result, "max_deposition_mm", None)
    return build_chart_payload(
        vega_lite_spec={
            "mark": {"type": "line", "point": True},
            "data": {"values": [{"t_s": float(t), "mgl": float(c)}
                                for t, c in zip(times, values)]},
            "encoding": {
                "x": {"field": "t_s", "type": "quantitative", "title": "Time (s)"},
                "y": {"field": "mgl", "type": "quantitative",
                      "title": "Marker concentration (mg/L)"},
            },
        },
        title=f"Marker passage over the moving bed - {where}",
        caption=(f"The marker's reach maximum at each of {len(times)} output "
                 f"times, beside a bed that scoured "
                 f"{'-' if scour is None else f'{float(scour):.3g}'} mm at most "
                 f"and gained "
                 f"{'-' if deposition is None else f'{float(deposition):.3g}'} mm "
                 "at most. The bed evolution itself is the map, not this curve."),
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
    name="telemac_river_scour",
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


telemac_river_scour = register_workflow(
    TelemacWorkflow, _METADATA,
    (*river.PARAM_ROWS, *river.RELEASE_ROWS, *param_rows(PARAMS)),
    Door(
        steering=STEERING,
        domain=river.acquire(seed_coords=R.reach_seed_coords),
        mesh=river.MESH, mesh_on="reach",
        produce=(MeshCoverage(mesh=Ref("mesh"),
                              centerline=river.DATA.centerline),),
        settle=river.settle(
            release_coords=R.release_coords, spill_fraction=R.spill_fraction,
            rain=_OWN.rain,
            dredge={"on": P.dredging,
                    "field": {"bank_offset_m": P.dredge_bank_offset_m,
                              "zone_len_m": _DREDGE_ZONE_LEN_M,
                              "station_frac": _DIG_STATION_FRAC,
                              "disposal_station_frac": _DUMP_STATION_FRAC,
                              "disposal": P.dredge_disposal},
                    "rule": {"mode": P.dredge_mode,
                             "start_frac": _DREDGE_START_FRAC,
                             "end_frac": _DREDGE_END_FRAC,
                             "rate_m_per_s": _DREDGE_RATE_M_PER_S,
                             "volume_m3": P.dredge_volume_m3,
                             "crit_depth_m": P.dredge_crit_depth_m,
                             "dig_depth_m": P.dredge_dig_depth_m}}),
        derive=(SedimentBed(
            gradation=P.sediment_gradation, grain_size_um=P.grain_size_um,
            bed_thickness_m=P.bed_thickness_m,
            bedload_formula=P.bedload_formula,
            morphological_factor=P.morphological_factor,
            dredging=Ref("settled.dredging"),
            injected={"q_m3s": R.source_q_m3s,
                      "concentration_mgl": P.tracer_concentration_mgl,
                      "window_s": R.spill_duration_s}).named("sediment"),),
        slots={"VELOCITY_DIFFUSIVITY": R.velocity_diffusivity,
               "COEFFICIENT_FOR_DIFFUSION_OF_TRACERS": R.tracer_diffusivity},
        results=(river.RESULT, RESULT_FILENAME),
        steering_file=_STEERING_FILE, prefix="telemac",
        dispatch=solve_reach, compute_class=S.compute_class,
        meta={"substance": "sediment", "substance_class": "sediment",
              "sediment_injected_kg": Ref("sediment.injected_kg"),
              "sediment_n_classes": Ref("sediment.n_classes")},
        read=lambda run: Products.dye(
            run=run, solve=run,
            carrier_discharge=Ref("carrier_discharge")).named("bed"),
        chart=("bed_evolution", build_bed_chart),
        review_title="Review the mobile-bed scenario"),
    data=DATA,
    accepts=ACCEPTS,
    answer=ANSWER,
    provenance=(("discharge_m3s", "discharge_note"),),
    # Scour and deposition maxima live inside single elements, so a coarse mesh
    # reads both low.
    sensitivity=(("max_deposition_mm", "peak"),
                 ("max_scour_mm", "peak")),
    coerce=(
        location_or_bbox("telemac_river_scour", code_prefix="TELEMAC",
                         hint="For a natural prompt like 'bed scour below the dam "
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
