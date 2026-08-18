"""Engine template ``swmm_aquifer_baseflow_to_node`` - two-zone aquifer baseflow.

How much steady BASEFLOW does a shallow unconfined aquifer beneath a pervious
subcatchment contribute to a receiving drainage node BETWEEN storms, and how does
adding that groundwater pathway reshape the node's total hydrograph versus surface
runoff alone? This is the SWMM analogue of the subsurface return-flow theme that
the Landlab GroundwaterDupuitPercolator templates and the TELEMAC
rain-on-grid recession tail approach from the surface-hydrology side:
a slow, sustained groundwater discharge that keeps a channel flowing after the
storm runoff has drained -- WITHOUT overclaiming a shared solver (these are
independent engines answering the same question class).

The deck authors a real SWMM 5 [AQUIFERS] object (the two-zone unsaturated /
saturated moisture-balance column: porosity, wilting point, field capacity,
conductivity, seepage, initial water-table elevation) and a [GROUNDWATER] link
(subcatchment -> aquifer -> node) whose lateral outflow follows the SWMM
groundwater flow equation

    q_gw = A1 * (Hgw - Hstar)^B1  -  A2 * (Hsw - Hstar)^B2  +  A3 * Hgw * Hsw

with A1/B1 the groundwater-to-node coefficients (the baseflow term), and solves
it headless through the native SWMM 5 engine (pyswmm, in-process). Storm
infiltration recharges the aquifer; the risen water table then discharges the A1
baseflow term to the node and recedes slowly between storms. TWO variants run on
the SAME two-storm forcing:

  1. with_gw - the [GROUNDWATER] baseflow pathway active (A1 > 0);
  2. no_gw   - A1 = 0 (surface runoff ONLY; the node returns to zero between
     storms), the control that isolates the groundwater contribution.

Citations (NATE-verified template source):
  * EPA SWMM Reference Manual Volume I - Hydrology (Rossman & Huber),
    Groundwater chapter (the two-zone AQUIFER moisture balance + the
    GROUNDWATER flow-equation coefficients A1/B1/A2/B2/A3).
  * "Aquifer and Groundwater Objects in SWMM 5" (swmm5.org, CHI) - the
    two-object structure (Aquifer spans subcatchments; Groundwater is per
    subcatchment) and the flow-coefficient editor.

Chart-first validation class (the RDII template precedent): the
deliverable is CHARTS (node hydrograph with-GW vs no-GW + the baseflow recession)
plus typed scalars, no georeferenced raster. Host-side pyswmm, no worker image.

Determinism boundary (Invariant 1): every number the agent narrates is a typed
field this tool returns - never free-generated.
"""

from __future__ import annotations

import asyncio
import logging
import math
import tempfile
from pathlib import Path
from typing import Any

from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.data import register_tool, TOOL_REGISTRY
from trid3nt_server.gates.input_review import (
    gate_input_review,
    physics_refusal_reason,
)
from trid3nt_server.workflows.shared.aquifer_resolve import derive_soil_column
from trid3nt_server.workflows.swmm._template_card import TemplateCard

logger = logging.getLogger(
    "trid3nt_server.workflows.swmm.aquifer_baseflow.aquifer_baseflow"
)

__all__ = [
    "swmm_aquifer_baseflow_to_node",
    "build_aquifer_inp",
    "solve_aquifer_deck",
    "default_two_storm_forcing",
    "TEMPLATE_CARD",
]


TEMPLATE_CARD = TemplateCard(
    question=(
        "how much baseflow does a shallow two-zone aquifer contribute to a "
        "drainage node between storms, and how does the groundwater pathway "
        "reshape the node hydrograph versus surface runoff alone"
    ),
    required_inputs=["location (or lat/lon) for the SoilGrids soil-column derivation"],
    knobs=(
        "location/lat/lon (AOI for the derived aquifer column), rainfall_series_in_hr, "
        "dt_min, area_ac, a1/b1 (groundwater flow coefficients), aquifer "
        "porosity/wilting/field-capacity/conductivity (else SoilGrids-derived), "
        "initial_water_table_ft, sim_days"
    ),
)

_METADATA = AtomicToolMetadata(
    name="swmm_aquifer_baseflow_to_node",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="swmm",
    tier="template",
)


def _geocode_site(location: str) -> tuple[float, float]:
    """Resolve a place name to ``(lat, lon)`` via the ``geocode_location`` tool.

    Raises on any lookup failure so the caller narrates a typed refusal (never a
    fabricated site). Seam-1: resolves through ``TOOL_REGISTRY``.
    """
    entry = TOOL_REGISTRY.get("geocode_location")
    if entry is None:
        raise RuntimeError("geocode_location not registered")
    r = entry.fn(location)
    lat = r.get("latitude") if isinstance(r, dict) else getattr(r, "latitude", None)
    lon = r.get("longitude") if isinstance(r, dict) else getattr(r, "longitude", None)
    if lat is None or lon is None:
        raise RuntimeError(f"geocode returned no coordinates for {location!r}")
    return float(lat), float(lon)


def default_two_storm_forcing(
    dt_min: int = 15, sim_days: int = 24,
) -> list[tuple[str, float]]:
    """Representative two-storm forcing ``[("H:MM", in/hr), ...]``: an 8-hour
    storm on day 1 and another on day 12, dry between, so the between-storms
    baseflow (and the day-12 recharge bump) are explicit."""
    rain: list[tuple[str, float]] = []
    n = int(round(sim_days * 24 * 60 / dt_min))
    for i in range(n):
        mins = i * dt_min
        h = mins / 60.0
        clock = f"{mins // 60}:{mins % 60:02d}"
        wet = (6 <= h < 14) or (12 * 24 + 6 <= h < 12 * 24 + 14)
        rain.append((clock, 0.3 if wet else 0.0))
    return rain


def build_aquifer_inp(
    rainfall_series_in_hr: list[tuple[str, float]],
    dt_min: int,
    area_ac: float,
    *,
    porosity: float,
    wilting_point: float,
    field_capacity: float,
    conductivity_in_hr: float,
    a1: float = 0.002,
    b1: float = 1.0,
    initial_water_table_ft: float = 4.0,
    surface_elev_ft: float = 10.0,
    sim_days: int = 24,
) -> str:
    """Author a SWMM 5 deck: one pervious subcatchment over an [AQUIFERS] two-zone
    column linked by [GROUNDWATER] (subcatchment -> aquifer -> node J1) with the
    A1/B1 baseflow coefficients, draining to an outfall. Returns the ``.inp`` text
    (US units: feet, inches, in/hr). ``a1=0`` disables the baseflow pathway. The
    two-zone column (porosity / wilting / field-capacity / conductivity) is
    REQUIRED - law 9: the tool derives it from SoilGrids or refuses (no invented
    column here)."""
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
CONSTANT 0.02
DRY_ONLY NO

[RAINGAGES]
RG1 INTENSITY 0:{dt_min:02d} 1.0 TIMESERIES TSER_R

[SUBCATCHMENTS]
S1 RG1 J1 {area_ac} 5 800 0.5 0

[SUBAREAS]
S1 0.02 0.15 0.05 0.05 25 OUTLET

[INFILTRATION]
S1 3.5 0.5 0.30

[AQUIFERS]
;;Name Por WP FC Ksat Kslope Tslope ETu ETs Seep Ebot Egw Umc
AQ1 {porosity} {wilting_point} {field_capacity} {conductivity_in_hr} 10 15 0.35 14 0.002 0 {initial_water_table_ft} 0.30

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


def solve_aquifer_deck(
    inp_text: str,
) -> tuple[list[float], list[float], list[float], float]:
    """Solve an aquifer deck headless (pyswmm, in-process) and return
    ``(hours, node_inflow_cfs, runoff_cfs, flow_routing_error_pct)``.

    ``hours`` is real elapsed time from ``sim.current_time`` (SWMM steps at the
    variable wet/dry step). ``node_inflow_cfs`` is the receiving node J1's total
    inflow (surface runoff + groundwater baseflow)."""
    import pyswmm

    base = Path(tempfile.mkdtemp(prefix="swmm-aquifer-"))
    inp = base / "gw.inp"
    inp.write_text(inp_text, encoding="utf-8")
    hours: list[float] = []
    node_in: list[float] = []
    runoff: list[float] = []
    with pyswmm.Simulation(str(inp)) as sim:
        j1 = pyswmm.Nodes(sim)["J1"]
        s1 = pyswmm.Subcatchments(sim)["S1"]
        t0 = None
        for _ in sim:
            now = sim.current_time
            if t0 is None:
                t0 = now
            hours.append((now - t0).total_seconds() / 3600.0)
            node_in.append(float(j1.total_inflow))
            runoff.append(float(s1.runoff))
        cont = float(sim.flow_routing_error) * 100.0
    return hours, node_in, runoff, cont


def _mean_between(hours: list[float], series: list[float],
                  lo_day: float, hi_day: float) -> float:
    """Mean of ``series`` over the day window ``[lo_day, hi_day)``."""
    window = [q for h, q in zip(hours, series) if lo_day * 24 <= h < hi_day * 24]
    return sum(window) / len(window) if window else 0.0


def _peak(series: list[float]) -> tuple[float, int]:
    if not series:
        return 0.0, 0
    i = max(range(len(series)), key=lambda k: series[k])
    return series[i], i


def _node_chart_spec(hours: list[float], with_gw: list[float],
                     no_gw: list[float]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for t, v in zip(hours, with_gw):
        rows.append({"t_hr": round(t, 2), "q_cfs": round(v, 5),
                     "series": "with groundwater (baseflow)"})
    for t, v in zip(hours, no_gw):
        rows.append({"t_hr": round(t, 2), "q_cfs": round(v, 5),
                     "series": "surface runoff only"})
    return {
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


@register_tool(
    _METADATA,
    read_only_hint=True,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
)
async def swmm_aquifer_baseflow_to_node(
    location: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    rainfall_series_in_hr: list[list[Any]] | list[tuple[str, float]] | None = None,
    dt_min: int = 15,
    area_ac: float = 100.0,
    a1: float = 0.002,
    b1: float = 1.0,
    porosity: float | None = None,
    wilting_point: float | None = None,
    field_capacity: float | None = None,
    conductivity_in_hr: float | None = None,
    initial_water_table_ft: float = 4.0,
    sim_days: int = 24,
    input_mode: str | None = None,
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Two-zone aquifer baseflow to a node: how much sustained groundwater
    baseflow a shallow aquifer contributes between storms, and how it reshapes
    the node hydrograph vs surface runoff alone.

    Authors a native SWMM 5 [AQUIFERS]/[GROUNDWATER] deck on one pervious
    subcatchment and solves it headless (pyswmm, in-process) in two variants on
    the SAME two-storm forcing: (1) with the groundwater baseflow pathway active
    (A1 > 0) and (2) a surface-runoff-only control (A1 = 0). Emits a node
    hydrograph chart (baseflow tail vs none) and returns typed scalars. No engine
    RASTER (chart-first validation class). Two-zone aquifer + groundwater flow
    coefficients per the EPA SWMM Reference Manual Vol. I groundwater chapter
    (module docstring).

    Parameters:
      location: place name (e.g. "Fort Myers, FL") geocoded to the AOI at which
        the two-zone aquifer moisture column is DERIVED from SoilGrids texture
        (law 9). Alternatively pass ``lat``/``lon`` directly.
      lat, lon: explicit AOI coordinates (override ``location``) for the SoilGrids
        soil-column derivation.
      rainfall_series_in_hr: rainfall intensity ``[["H:MM", in/hr], ...]``
        pairs. Default = a representative two-storm sequence (day 1 and day
        12) so the between-storms baseflow and the day-12 recharge bump are
        explicit.
      dt_min: wet-weather timestep, minutes. Default 15.
      area_ac: subcatchment area, acres. Default 100.
      a1: groundwater-to-node flow coefficient (the baseflow term). Default
        0.002. ``a1=0`` is the surface-only control.
      b1: groundwater flow exponent. Default 1.0 (linear reservoir -> a clean
        exponential baseflow recession).
      porosity, wilting_point, field_capacity, conductivity_in_hr: two-zone
        aquifer soil-moisture-balance properties. Unset -> DERIVED from SoilGrids
        texture at the AOI (Saxton-Rawls); the run REFUSES in auto when neither a
        site nor an explicit column is given, or SoilGrids cannot serve (law 9).
      initial_water_table_ft: initial saturated-zone water-table elevation (ft),
        a scenario initial state. Default 4.
      sim_days: simulation length, days. Default 24.

    Returns:
      A dict of scalars: ``peak_node_inflow_with_gw_cfs`` (+ ``_hr``),
      ``peak_node_inflow_no_gw_cfs``, ``between_storms_baseflow_with_gw_cfs``
      and ``_no_gw_cfs`` (mean node inflow over the dry days 6-11),
      ``baseflow_contribution_cfs`` (the with-minus-without difference),
      ``recession_tau_hr`` (exponential recession time constant of the
      between-storms tail), ``storm2_recharge_bump_cfs`` (baseflow rise after
      the day-12 storm re-recharges the aquifer), ``flow_routing_error_pct``,
      and ``curves``.
    """
    from trid3nt_server.emission.pipeline_emitter import current_emitter

    def _coerce(series: Any) -> list[tuple[str, float]] | None:
        if not series:
            return None
        try:
            return [(str(c), float(v)) for c, v in series]
        except (TypeError, ValueError):
            return None

    try:
        dt_min_i = max(int(dt_min), 1)
        area = max(float(area_ac), 0.01)
        days = max(int(sim_days), 2)
    except (TypeError, ValueError) as exc:
        return {"status": "error", "error_code": "SWMM_AQUIFER_INVALID",
                "error_message": f"bad numeric input: {exc}"}

    rain = _coerce(rainfall_series_in_hr) or default_two_storm_forcing(dt_min_i, days)

    # --- law 9: two-zone aquifer soil column DERIVED from SoilGrids or REFUSE ---
    # The [AQUIFERS] moisture column (porosity / wilting / field capacity /
    # conductivity) is a physics-consequential material property that drives the
    # baseflow recession directly. It is DERIVED from SoilGrids texture at the AOI
    # (the Saxton-Rawls two-zone fit) or REFUSES in auto - never an invented column.
    _user_column = (
        porosity is not None and wilting_point is not None
        and field_capacity is not None and conductivity_in_hr is not None
    )
    _lat, _lon, _geo_reason = lat, lon, None
    if not _user_column and (_lat is None or _lon is None) and location:
        try:
            _lat, _lon = await asyncio.to_thread(_geocode_site, location)
        except Exception as exc:  # noqa: BLE001 - a failed geocode -> typed refusal
            _geo_reason = f"geocode failed for {location!r}: {exc}"
    _col = None
    _col_meta: dict[str, Any] = {}
    if not _user_column and _lat is not None and _lon is not None:
        _col, _col_meta = await asyncio.to_thread(derive_soil_column, _lat, _lon)
    elif not _user_column:
        _col_meta = {"reason": _geo_reason or "no site (location or lat/lon) supplied"}

    if _user_column:
        column = dict(
            porosity=float(porosity), wilting_point=float(wilting_point),
            field_capacity=float(field_capacity),
            conductivity_in_hr=float(conductivity_in_hr),
        )
        _col_entry = SyntheticInput(
            param="aquifer_soil_column", basis="user", consequence="physics",
            value=(f"por={column['porosity']}, wp={column['wilting_point']}, "
                   f"fc={column['field_capacity']}, K={column['conductivity_in_hr']} in/hr"),
            note="caller-supplied two-zone aquifer moisture column.",
        )
    elif _col is not None:
        column = dict(
            porosity=_col.porosity, wilting_point=_col.wilting_point,
            field_capacity=_col.field_capacity,
            conductivity_in_hr=_col.conductivity_in_hr,
        )
        _col_entry = SyntheticInput(
            param="aquifer_soil_column", basis="derived", consequence="physics",
            real_source_if_any="fetch_soilgrids (Saxton-Rawls 2006 two-zone column)",
            value=(f"por={_col.porosity}, wp={_col.wilting_point}, "
                   f"fc={_col.field_capacity}, K={_col.conductivity_in_hr} in/hr"),
            note=(f"two-zone aquifer moisture column DERIVED from SoilGrids texture "
                  f"at the AOI (sand={_col.sand_pct}%, clay={_col.clay_pct}%). "
                  "SCREENING near-surface proxy, NOT a measured column."),
        )
    else:
        column = None
        _col_entry = SyntheticInput(
            param="aquifer_soil_column", value=None,
            basis="default_demo", consequence="physics", real_source_if_any=None,
            note=(f"the two-zone aquifer moisture column could not be resolved from "
                  f"SoilGrids ({_col_meta.get('reason', 'unavailable')}). No invented "
                  "default (law 9): supply a location / lat+lon within SoilGrids "
                  "coverage, or explicit porosity/wilting_point/field_capacity/"
                  "conductivity_in_hr."),
        )

    _review = await gate_input_review(
        tool_name="swmm_aquifer_baseflow_to_node", mode=input_mode,
        entries=[_col_entry], params={},
    )
    if _review.cancelled or column is None:
        return {
            "status": "error",
            "error_code": "SWMM_PHYSICS_INPUT_REQUIRED",
            "error_message": (
                _review.cancel_reason
                or physics_refusal_reason(
                    "swmm_aquifer_baseflow_to_node", [_col_entry]
                )
                or str(_col_entry.note)
            ),
        }
    _col_basis = _col_entry.basis
    _col_source = _col_entry.real_source_if_any

    common = dict(dt_min=dt_min_i, area_ac=area, b1=float(b1),
                  initial_water_table_ft=float(initial_water_table_ft),
                  sim_days=days, **column)

    try:
        inp_gw = build_aquifer_inp(rain, a1=float(a1), **common)
        hrs, node_gw, ro, cont = await asyncio.to_thread(solve_aquifer_deck, inp_gw)
        inp_no = build_aquifer_inp(rain, a1=0.0, **common)
        _, node_no, _, _ = await asyncio.to_thread(solve_aquifer_deck, inp_no)
    except Exception as exc:  # noqa: BLE001
        logger.exception("swmm aquifer baseflow solve failed")
        return {"status": "error", "error_code": "SWMM_AQUIFER_SOLVE_FAILED",
                "error_message": str(exc)}

    peak_gw, peak_gw_i = _peak(node_gw)
    peak_no, _ = _peak(node_no)
    # between-storms baseflow = mean node inflow over the dry days 6-11.
    base_gw = _mean_between(hrs, node_gw, 6, 11)
    base_no = _mean_between(hrs, node_no, 6, 11)
    contribution = base_gw - base_no

    # exponential recession time constant of the between-storms tail (days 6-11).
    tail = [(h, q) for h, q in zip(hrs, node_gw) if 6 * 24 <= h < 11 * 24 and q > 1e-6]
    tau = None
    if len(tail) >= 2 and tail[0][1] > tail[-1][1] > 0:
        dt_span = tail[-1][0] - tail[0][0]
        tau = dt_span / math.log(tail[0][1] / tail[-1][1])

    # storm-2 recharge bump: the baseflow rise from just-before day 12 to its
    # post-recharge peak in days 12-14 (recharge revives the receding baseflow).
    pre = _mean_between(hrs, node_gw, 11.5, 12.0)
    post = _peak([q for h, q in zip(hrs, node_gw) if 12 * 24 <= h < 14 * 24])[0]
    bump = post - pre

    logger.info(
        "swmm aquifer baseflow: peak_gw=%.4f cfs peak_no_gw=%.4f cfs "
        "between_storms base_gw=%.4f no_gw=%.4f contrib=%.4f tau=%s bump=%.4f cont=%.3f%%",
        peak_gw, peak_no, base_gw, base_no, contribution,
        None if tau is None else round(tau, 1), bump, cont,
    )

    emitter = current_emitter()
    charts_emitted = 0
    if emitter is not None and hasattr(emitter, "emit_chart"):
        try:
            from trid3nt_server.data.processing.charts_common import build_chart_payload
            spec = _node_chart_spec(hrs, node_gw, node_no)
            payload = build_chart_payload(
                vega_lite_spec=spec,
                title="node hydrograph: groundwater baseflow vs surface runoff only",
                caption=(
                    f"Two-zone aquifer baseflow over {area:.0f} ac: with groundwater the "
                    f"node sustains {base_gw:.3f} cfs baseflow between storms (surface-only "
                    f"{base_no:.3f} cfs)"
                    + (f", receding with tau ~{tau:.0f} h" if tau is not None else "")
                    + f"; the day-12 storm re-recharges the aquifer (+{bump:.3f} cfs). "
                      f"EPA SWMM two-zone [AQUIFERS]/[GROUNDWATER] flow equation."
                ),
            )
            await emitter.emit_chart(payload)
            charts_emitted += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("swmm aquifer chart emit failed: %s", exc)

    return {
        "status": "ok",
        "model": "swmm_two_zone_aquifer_baseflow",
        "citation": ("EPA SWMM Reference Manual Vol. I (Hydrology), Groundwater "
                     "chapter (two-zone aquifer + A1/B1 flow coefficients); "
                     "swmm5.org Aquifer/Groundwater objects"),
        "area_ac": area,
        "a1": float(a1),
        "b1": float(b1),
        "aquifer_soil_column": {k: round(float(v), 4) for k, v in column.items()},
        "aquifer_provenance": (
            str(_col_entry.note)
            + (f" [{_col_source}]" if _col_source else f" [basis={_col_basis}]")
        ),
        "peak_node_inflow_with_gw_cfs": round(peak_gw, 5),
        "peak_node_inflow_with_gw_hr": round(hrs[peak_gw_i], 2) if hrs else 0.0,
        "peak_node_inflow_no_gw_cfs": round(peak_no, 5),
        "between_storms_baseflow_with_gw_cfs": round(base_gw, 5),
        "between_storms_baseflow_no_gw_cfs": round(base_no, 5),
        "baseflow_contribution_cfs": round(contribution, 5),
        "recession_tau_hr": (round(tau, 2) if tau is not None else None),
        "storm2_recharge_bump_cfs": round(bump, 5),
        "flow_routing_error_pct": round(cont, 4),
        "curves": {
            "hours": [round(t, 3) for t in hrs],
            "node_inflow_with_gw_cfs": [round(v, 5) for v in node_gw],
            "node_inflow_no_gw_cfs": [round(v, 5) for v in node_no],
        },
        "charts_emitted": charts_emitted,
    }
