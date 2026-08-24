"""The aquifer-baseflow step family: the deck writer, the metrics, the chart.

The deck is this template's serialization hook - the ``[AQUIFERS]`` /
``[GROUNDWATER]`` objects and the storm forcing that recharges them. Every value
it writes that changes the answer arrives as a declared argument; what stays
literal is the schematic drainage scaffolding (one junction, one outfall, one
pipe), which is the mechanism, not the question.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Sequence

from trid3nt_server.declarative import Step

from trid3nt_server.workflows.swmm.steps.errors import SwmmDeckError

__all__ = [
    "Deck",
    "Metrics",
    "build_aquifer_inp",
    "build_baseflow_chart",
    "baseflow_metrics",
    "two_storm_forcing",
    "write_aquifer_deck",
]

logger = logging.getLogger("trid3nt_server.workflows.swmm.aquifer_baseflow.steps")

_STEPS = "trid3nt_server.workflows.swmm.aquifer_baseflow.steps"

#: How the storm-2 recharge bump is MEASURED: the baseflow rise from the half-day
#: before the second storm to its peak in the two days after. A definition of the
#: statistic, not a physical value.
_PRE_STORM_WINDOW_D = 0.5
_RECHARGE_WINDOW_D = 2.0


class Deck:
    """SWMM deck writers. One constructor per question the family answers."""

    @staticmethod
    def aquifer(**kwargs: Any) -> Step:
        """Author the two-zone [AQUIFERS] / [GROUNDWATER] deck for one variant."""
        return Step(runner=f"{_STEPS}.write_aquifer_deck", kwargs=kwargs)


class Metrics:
    """Answer extraction from solved series - never a second physics model."""

    @staticmethod
    def baseflow(**kwargs: Any) -> Step:
        """The between-storms baseflow contribution and its recession."""
        return Step(runner=f"{_STEPS}.baseflow_metrics", kwargs=kwargs)


def two_storm_forcing(
    *,
    dt_min: int,
    sim_days: int,
    intensity_in_hr: float,
    storm_start_hr: float,
    storm_duration_hr: float,
    second_storm_day: float,
) -> list[tuple[str, float]]:
    """The declared two-storm hyetograph ``[("H:MM", in/hr), ...]``.

    Two wet windows with a long dry spell between them: the dry spell is where
    the between-storms baseflow is read, and the second storm is what shows the
    water table being re-recharged. Every number is a declared param.
    """
    rain: list[tuple[str, float]] = []
    dt_min, sim_days = int(dt_min), int(sim_days)
    steps = int(round(sim_days * 24 * 60 / dt_min))
    second_start = second_storm_day * 24 + storm_start_hr
    for i in range(steps):
        mins = i * dt_min
        hour = mins / 60.0
        clock = f"{mins // 60}:{mins % 60:02d}"
        wet = (storm_start_hr <= hour < storm_start_hr + storm_duration_hr
               or second_start <= hour < second_start + storm_duration_hr)
        rain.append((clock, intensity_in_hr if wet else 0.0))
    return rain


async def write_aquifer_deck(
    *,
    a1: float,
    b1: float,
    area_ac: float,
    dt_min: int,
    sim_days: int,
    porosity: float,
    wilting_point: float,
    field_capacity: float,
    conductivity_in_hr: float,
    initial_water_table_ft: float,
    surface_elev_ft: float,
    imperviousness_pct: float,
    soil_suction_in: float,
    infiltration_ksat_in_hr: float,
    initial_moisture_deficit: float,
    aquifer_seepage_in_hr: float,
    evaporation_in_day: float,
    storm_intensity_in_hr: float,
    storm_start_hr: float,
    storm_duration_hr: float,
    second_storm_day: float,
    rainfall_series_in_hr: Sequence[Any] | None = None,
) -> dict[str, Any]:
    """Author the deck for ONE variant and return it with the forcing it used.

    ``a1=0`` is the surface-runoff-only control: the same deck with the
    groundwater-to-node pathway switched off, which is what isolates the
    baseflow contribution.
    """
    rain = _coerce_series(rainfall_series_in_hr) or two_storm_forcing(
        dt_min=dt_min, sim_days=sim_days, intensity_in_hr=storm_intensity_in_hr,
        storm_start_hr=storm_start_hr, storm_duration_hr=storm_duration_hr,
        second_storm_day=second_storm_day,
    )
    inp = build_aquifer_inp(
        rain, dt_min=dt_min, area_ac=area_ac, porosity=porosity,
        wilting_point=wilting_point, field_capacity=field_capacity,
        conductivity_in_hr=conductivity_in_hr, a1=a1, b1=b1,
        initial_water_table_ft=initial_water_table_ft,
        surface_elev_ft=surface_elev_ft, sim_days=sim_days,
        imperviousness_pct=imperviousness_pct, soil_suction_in=soil_suction_in,
        infiltration_ksat_in_hr=infiltration_ksat_in_hr,
        initial_moisture_deficit=initial_moisture_deficit,
        aquifer_seepage_in_hr=aquifer_seepage_in_hr,
        evaporation_in_day=evaporation_in_day,
    )
    return {"inp_text": inp, "a1": float(a1), "rain_steps": len(rain)}


def _coerce_series(series: Any) -> list[tuple[str, float]] | None:
    if not series:
        return None
    try:
        return [(str(clock), float(value)) for clock, value in series]
    except (TypeError, ValueError) as exc:
        raise SwmmDeckError(
            f"rainfall_series_in_hr is not a list of [\"H:MM\", in/hr] pairs: {exc}"
        ) from exc


def build_aquifer_inp(
    rainfall_series_in_hr: Sequence[tuple[str, float]],
    *,
    dt_min: int,
    area_ac: float,
    porosity: float,
    wilting_point: float,
    field_capacity: float,
    conductivity_in_hr: float,
    a1: float,
    b1: float,
    initial_water_table_ft: float,
    surface_elev_ft: float,
    sim_days: int,
    imperviousness_pct: float,
    soil_suction_in: float,
    infiltration_ksat_in_hr: float,
    initial_moisture_deficit: float,
    aquifer_seepage_in_hr: float,
    evaporation_in_day: float,
) -> str:
    """One pervious subcatchment over a two-zone [AQUIFERS] column, linked by
    [GROUNDWATER] to node J1 and drained to an outfall. US units (ft, in, in/hr).

    The scaffolding that carries the answer OUT - the junction, the outfall, the
    pipe, the subarea roughness - is fixed: it is the schematic that lets the
    node hydrograph be read, not a property of the site.
    """
    # The deck's clock fields are integers; the declared params carry bounds, so
    # the resolver hands them over as floats.
    dt_min, sim_days = int(dt_min), int(sim_days)
    ts_rain = "\n".join(f"TSER_R {clk} {v:.4f}" for clk, v in rainfall_series_in_hr)
    end_date = 1 + int(sim_days)
    dd = end_date // 30 + 1
    mm = end_date % 30 or 30
    return f"""[TITLE]
two-zone aquifer groundwater baseflow-to-node; A1={a1}

[OPTIONS]
FLOW_UNITS CFS
INFILTRATION GREEN_AMPT
FLOW_ROUTING KINWAVE
START_DATE 01/01/2020
START_TIME 00:00:00
END_DATE {dd:02d}/{mm:02d}/2020
END_TIME 00:00:00
REPORT_STEP 01:00:00
WET_STEP 00:{dt_min:02d}:00
DRY_STEP 01:00:00
ROUTING_STEP 300

[EVAPORATION]
CONSTANT {evaporation_in_day}
DRY_ONLY NO

[RAINGAGES]
RG1 INTENSITY 0:{dt_min:02d} 1.0 TIMESERIES TSER_R

[SUBCATCHMENTS]
S1 RG1 J1 {area_ac} {imperviousness_pct:g} 800 0.5 0

[SUBAREAS]
S1 0.02 0.15 0.05 0.05 25 OUTLET

[INFILTRATION]
S1 {soil_suction_in:g} {infiltration_ksat_in_hr:g} {initial_moisture_deficit:g}

[AQUIFERS]
;;Name Por WP FC Ksat Kslope Tslope ETu ETs Seep Ebot Egw Umc
AQ1 {porosity} {wilting_point} {field_capacity} {conductivity_in_hr} 10 15 0.35 14 {aquifer_seepage_in_hr:g} 0 {initial_water_table_ft} 0.30

[GROUNDWATER]
;;Subcat Aquifer Node Esurf A1 B1 A2 B2 A3 Dsw Egwt
S1 AQ1 J1 {surface_elev_ft} {a1} {b1} 0 0 0 0 *

[JUNCTIONS]
J1 0.0 0 0 0 0

[OUTFALLS]
OUT -1.0 FREE NO

[CONDUITS]
C1 J1 OUT 400 0.01 0 0 0 0

[XSECTIONS]
C1 CIRCULAR 3 0 0 0 1

[TIMESERIES]
{ts_rain}

[REPORT]
INPUT NO
SUBCATCHMENTS ALL
NODES ALL
"""


async def baseflow_metrics(
    *,
    with_gw: dict[str, Any],
    no_gw: dict[str, Any],
    node: str,
    dry_window_start_day: float,
    dry_window_end_day: float,
    second_storm_day: float,
    area_ac: float,
    a1: float,
    b1: float,
) -> dict[str, Any]:
    """The physical answer: what the groundwater pathway added, and how it recedes.

    Every number is read off the two solved hydrographs; the ``no_gw`` run is the
    control that isolates the contribution.
    """
    hours = list(with_gw["hours"])
    gw = list(with_gw["nodes"][node])
    dry = list(no_gw["nodes"][node])

    peak_gw, peak_index = _peak(gw)
    peak_no, _ = _peak(dry)
    base_gw = _mean_between(hours, gw, dry_window_start_day, dry_window_end_day)
    base_no = _mean_between(hours, dry, dry_window_start_day, dry_window_end_day)

    tail = [(h, q) for h, q in zip(hours, gw)
            if dry_window_start_day * 24 <= h < dry_window_end_day * 24 and q > 1e-6]
    tau = None
    if len(tail) >= 2 and tail[0][1] > tail[-1][1] > 0:
        tau = (tail[-1][0] - tail[0][0]) / math.log(tail[0][1] / tail[-1][1])

    pre = _mean_between(hours, gw, second_storm_day - _PRE_STORM_WINDOW_D,
                        second_storm_day)
    post = _peak([q for h, q in zip(hours, gw)
                  if second_storm_day * 24 <= h
                  < (second_storm_day + _RECHARGE_WINDOW_D) * 24])[0]

    logger.info("swmm aquifer baseflow: peak_gw=%.4f no_gw=%.4f base_gw=%.4f "
                "base_no=%.4f contrib=%.4f tau=%s bump=%.4f cont=%.3f%%",
                peak_gw, peak_no, base_gw, base_no, base_gw - base_no,
                None if tau is None else round(tau, 1), post - pre,
                with_gw["flow_routing_error_pct"])
    return {
        "area_ac": float(area_ac),
        "a1": float(a1),
        "b1": float(b1),
        "peak_node_inflow_with_gw_cfs": round(peak_gw, 5),
        "peak_node_inflow_with_gw_hr": round(hours[peak_index], 2),
        "peak_node_inflow_no_gw_cfs": round(peak_no, 5),
        "between_storms_baseflow_with_gw_cfs": round(base_gw, 5),
        "between_storms_baseflow_no_gw_cfs": round(base_no, 5),
        "baseflow_contribution_cfs": round(base_gw - base_no, 5),
        "recession_tau_hr": (round(tau, 2) if tau is not None else None),
        "storm2_recharge_bump_cfs": round(post - pre, 5),
        "flow_routing_error_pct": round(float(with_gw["flow_routing_error_pct"]), 4),
        "curves": {
            "hours": [round(h, 3) for h in hours],
            "node_inflow_with_gw_cfs": [round(v, 5) for v in gw],
            "node_inflow_no_gw_cfs": [round(v, 5) for v in dry],
        },
    }


def _mean_between(hours: Sequence[float], series: Sequence[float],
                  lo_day: float, hi_day: float) -> float:
    window = [q for h, q in zip(hours, series) if lo_day * 24 <= h < hi_day * 24]
    return sum(window) / len(window) if window else 0.0


def _peak(series: Sequence[float]) -> tuple[float, int]:
    if not series:
        return 0.0, 0
    index = max(range(len(series)), key=lambda k: series[k])
    return series[index], index


def build_baseflow_chart(*, result: Any, params: Any) -> dict[str, Any] | None:
    """The node hydrograph with the groundwater pathway against the control.

    Honest engine output only: the two solved series the metrics step already
    reported. ``None`` when there is no hydrograph to draw.
    """
    curves = (result or {}).get("curves") or {}
    hours = curves.get("hours") or []
    gw = curves.get("node_inflow_with_gw_cfs") or []
    dry = curves.get("node_inflow_no_gw_cfs") or []
    if len(hours) < 2 or len(gw) != len(hours) or len(dry) != len(hours):
        return None

    from trid3nt_server.data.processing.charts_common import build_chart_payload

    rows = [{"t_hr": round(h, 2), "q_cfs": v,
             "series": "with groundwater (baseflow)"} for h, v in zip(hours, gw)]
    rows += [{"t_hr": round(h, 2), "q_cfs": v, "series": "surface runoff only"}
             for h, v in zip(hours, dry)]
    spec = {
        "title": "node hydrograph: groundwater baseflow vs surface runoff only",
        "data": {"values": rows},
        "mark": {"type": "line"},
        "encoding": {
            "x": {"field": "t_hr", "type": "quantitative", "title": "time (hr)"},
            "y": {"field": "q_cfs", "type": "quantitative",
                  "title": "node inflow (cfs)"},
            "color": {"field": "series", "type": "nominal", "title": ""},
        },
    }
    base_gw = result["between_storms_baseflow_with_gw_cfs"]
    base_no = result["between_storms_baseflow_no_gw_cfs"]
    tau = result.get("recession_tau_hr")
    where = params.get("location")
    return build_chart_payload(
        vega_lite_spec=spec,
        title="node hydrograph: groundwater baseflow vs surface runoff only",
        caption=(
            f"Two-zone aquifer baseflow over {result['area_ac']:.0f} ac"
            + (f" at {where}" if where else "")
            + f": with groundwater the node sustains {base_gw:.3f} cfs baseflow "
              f"between storms (surface-only {base_no:.3f} cfs)"
            + (f", receding with tau ~{tau:.0f} h" if tau is not None else "")
            + f"; the second storm re-recharges the aquifer "
              f"(+{result['storm2_recharge_bump_cfs']:.3f} cfs). EPA SWMM two-zone "
              "[AQUIFERS]/[GROUNDWATER] flow equation."
        ),
    )
