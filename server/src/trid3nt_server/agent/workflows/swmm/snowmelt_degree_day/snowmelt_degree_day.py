"""Engine template ``swmm_snowmelt_degree_day`` - Snow Pack + degree-day melt.

For a cold-climate subcatchment that receives both snow and rain, how does
snowpack ACCUMULATION and degree-day MELT reshape the runoff hydrograph versus
treating all precipitation as immediate rain -- the winter flood-driver question.
The load-bearing case is RAIN-ON-SNOW: a cold spell builds a snowpack, then a
warm storm melts it while adding rain, so the two water sources STACK into a
single amplified, delayed runoff peak that a rain-only (climate-naive) model
both mistimes and under-predicts.

The deck authors a real SWMM 5 [SNOWPACKS] object (PLOWABLE / IMPERVIOUS /
PERVIOUS surfaces) assigned to one subcatchment, forced by a TEMPERATURE time
series (the degree-day driver + the [TEMPERATURE] SNOWMELT rain/snow dividing
temperature) and a rainfall time series, and solves it headless through the
native SWMM 5 snowmelt engine (pyswmm, in-process). THREE variants run on the
SAME forcing:

  1. snowmelt   - Snow Pack + degree-day melt (the physical winter run);
  2. rain_only  - the dividing temperature dropped below all temperatures so
     every drop is rain (the climate-naive control);
  3. removal    - snowmelt PLUS a plow [SNOWPACKS] REMOVAL block that transfers
     plowable-surface snow out of the watershed above a depth threshold (the
     snow-removal / plowing management knob).

Degree-day method (EPA SWMM Reference Manual Vol. I, Snowmelt chapter): while
air temperature T > the base melt temperature Tbase, melt rate = C * (T - Tbase)
with the melt coefficient C ramped seasonally between Cmin and Cmax; precipitation
falls as snow when T <= the dividing temperature and as rain otherwise. The
melt/accumulation the agent narrates comes from the native engine's per-step
snow_depth (SWE) and subcatchment runoff -- no free-generated physics.

Citations (NATE-verified template source):
  * EPA SWMM Reference Manual Volume I - Hydrology (Rossman & Huber),
    Snowmelt chapter (degree-day method; SNOWPACK surfaces; areal depletion).
  * "Example SWMM 5 Snowmelt Model" and "Snowmelt in SWMM5" (swmm5.org, CHI
    re-publication of the EPA SWMM5 snowmelt help) - the worked mechanism deck.
Temperature forcing for the live demonstration is REAL hourly ASOS/METAR air
temperature (fetch_asos_metar ``tmpf``) at a snowbelt station; the default
forcing is a representative Buffalo NY rain-on-snow event (cited climatology).

Chart-first validation class (the RDII template precedent, ADR 0190): the
deliverable is CHARTS (SWE series + runoff hydrograph snowmelt-vs-rain-only) plus
typed scalars, no georeferenced raster. Host-side pyswmm, no worker image.

Determinism boundary (Invariant 1): every number the agent narrates is a typed
field this tool returns - never free-generated.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.agent.tools import register_tool
from trid3nt_server.agent.workflows.swmm._template_card import TemplateCard

logger = logging.getLogger(
    "trid3nt_server.agent.workflows.swmm.snowmelt_degree_day.snowmelt_degree_day"
)

__all__ = [
    "swmm_snowmelt_degree_day",
    "build_snowmelt_inp",
    "solve_snowmelt_deck",
    "default_rain_on_snow_forcing",
    "TEMPLATE_CARD",
]


TEMPLATE_CARD = TemplateCard(
    question=(
        "how does snowpack accumulation and degree-day melt reshape the winter "
        "runoff hydrograph versus treating all precipitation as rain -- the "
        "rain-on-snow flood driver, with an optional snow-removal (plowing) knob"
    ),
    required_inputs=[],
    knobs=(
        "temperature_series_f, rainfall_series_in_hr, dt_min, area_ac, "
        "cmin/cmax (degree-day melt coefficients), base_temp_f, dividing_temp_f, "
        "snow_removal (plowing), plow_threshold_in, plow_fraction"
    ),
)

_METADATA = AtomicToolMetadata(
    name="swmm_snowmelt_degree_day",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="swmm",
    tier="template",
)


# --------------------------------------------------------------------------- #
# Default forcing: a representative Buffalo NY rain-on-snow event.
# A ~5-day window: a cold spell (T < 32 F) with steady light snowfall builds a
# snowpack, then a warm-up (T rising through 32 F to the mid-40s F) with a rain
# burst melts it -- the classic snowbelt rain-on-snow flood driver.
# --------------------------------------------------------------------------- #
def default_rain_on_snow_forcing(
    dt_min: int = 60,
) -> tuple[list[tuple[str, float]], list[tuple[str, float]]]:
    """Representative rain-on-snow forcing: ``(temperature_series, rain_series)``
    each as ``[("H:MM", value), ...]`` (degF, in/hr) at ``dt_min`` spacing.

    Cited climatology: Buffalo NY (KBUF) mid-January - a sub-freezing snowfall
    spell followed by a warm rain. The live proof/showcase overrides the
    temperature series with REAL KBUF ASOS observations."""
    temp: list[tuple[str, float]] = []
    rain: list[tuple[str, float]] = []
    steps = int(round(120 * 60 / dt_min))  # 5 days
    for i in range(steps):
        mins = i * dt_min
        h = mins / 60.0
        clock = f"{mins // 60}:{mins % 60:02d}"
        if h < 48:
            t_f = 20.0                                   # cold spell
        elif h < 60:
            t_f = 20.0 + (h - 48.0) * (45.0 - 20.0) / 12.0  # warm-up ramp
        else:
            t_f = 45.0                                   # warm
        temp.append((clock, round(t_f, 2)))
        if 12 <= h < 36:
            r = 0.05        # steady snowfall through the cold spell (falls as snow)
        elif 60 <= h < 72:
            r = 0.15        # warm rain burst on the ripe snowpack
        else:
            r = 0.0
        rain.append((clock, r))
    return temp, rain


# --------------------------------------------------------------------------- #
# Native SWMM 5 snowmelt deck
# --------------------------------------------------------------------------- #
def build_snowmelt_inp(
    temperature_series_f: list[tuple[str, float]],
    rainfall_series_in_hr: list[tuple[str, float]],
    dt_min: int,
    area_ac: float,
    *,
    cmin: float = 0.001,
    cmax: float = 0.01,
    base_temp_f: float = 32.0,
    dividing_temp_f: float = 32.0,
    percent_impervious: float = 80.0,
    plow_fraction: float = 0.90,
    removal: bool = False,
    plow_threshold_in: float = 0.3,
    plow_out_fraction: float = 1.0,
) -> str:
    """Author a SWMM 5 deck: one subcatchment with a [SNOWPACKS] Snow Pack, a
    [TEMPERATURE] SNOWMELT block (dividing temperature = the rain/snow split), and
    the temperature + rainfall time series. ``removal=True`` adds a plow REMOVAL
    line. Returns the ``.inp`` text (US units: degF, inches, in/hr/degF)."""
    ts_temp = "\n".join(f"TSER_T {clk} {v:.3f}" for clk, v in temperature_series_f)
    ts_rain = "\n".join(f"TSER_R {clk} {v:.4f}" for clk, v in rainfall_series_in_hr)
    snow = (
        f"SP1 PLOWABLE   {cmin} {cmax} {base_temp_f} 0.10 0 0 {plow_fraction}\n"
        f"SP1 IMPERVIOUS {cmin} {cmax} {base_temp_f} 0.10 0 0 2.0\n"
        f"SP1 PERVIOUS   {cmin} {cmax} {base_temp_f} 0.10 0 0 2.0\n"
    )
    if removal:
        # Dplow Fout Fimp Fperv Fimelt Fsub -- transfer plowable snow OUT of the
        # watershed above the plow-trigger depth.
        snow += f"SP1 REMOVAL {plow_threshold_in} {plow_out_fraction} 0 0 0 0\n"
    n_steps = max(len(rainfall_series_in_hr), len(temperature_series_f))
    end_days = int(n_steps * dt_min / 1440) + 2
    return f"""[TITLE]
snowpack degree-day melt (rain-on-snow); removal={removal}

[OPTIONS]
FLOW_UNITS CFS
INFILTRATION HORTON
FLOW_ROUTING KINWAVE
START_DATE 01/01/2020
START_TIME 00:00:00
END_DATE 01/{1 + end_days:02d}/2020
END_TIME 00:00:00
REPORT_STEP 00:{dt_min:02d}:00
WET_STEP 00:{dt_min:02d}:00
DRY_STEP 00:{dt_min:02d}:00
ROUTING_STEP {dt_min * 60}

[EVAPORATION]
CONSTANT 0.0
DRY_ONLY NO

[TEMPERATURE]
TIMESERIES TSER_T
SNOWMELT {dividing_temp_f} 0.5 0.6 500 43.0 0

[RAINGAGES]
RG1 INTENSITY 0:{dt_min:02d} 1.0 TIMESERIES TSER_R

[SUBCATCHMENTS]
S1 RG1 OUT {area_ac} {percent_impervious} 500 0.5 0 SP1

[SUBAREAS]
S1 0.01 0.10 0.05 0.05 25 OUTLET

[INFILTRATION]
S1 3.0 0.5 4 7 0

[SNOWPACKS]
{snow}
[OUTFALLS]
OUT 0.0 FREE NO

[TIMESERIES]
{ts_temp}
{ts_rain}

[REPORT]
INPUT NO
SUBCATCHMENTS ALL
"""


def solve_snowmelt_deck(
    inp_text: str,
) -> tuple[list[float], list[float], list[float], list[float], float]:
    """Solve a snowmelt deck headless (pyswmm, in-process) and return
    ``(hours, swe_in, runoff_cfs, rainfall_in_hr, continuity_error_pct)``.

    ``hours`` is real elapsed time from ``sim.current_time`` (SWMM steps at the
    variable wet/dry step, NOT a fixed count). ``swe_in`` is the subcatchment
    snow depth (snow water equivalent, inches)."""
    import pyswmm

    base = Path(tempfile.mkdtemp(prefix="swmm-snowmelt-"))
    inp = base / "snow.inp"
    inp.write_text(inp_text, encoding="utf-8")
    hours: list[float] = []
    swe: list[float] = []
    runoff: list[float] = []
    rain: list[float] = []
    with pyswmm.Simulation(str(inp)) as sim:
        s1 = pyswmm.Subcatchments(sim)["S1"]
        t0 = None
        for _ in sim:
            now = sim.current_time
            if t0 is None:
                t0 = now
            hours.append((now - t0).total_seconds() / 3600.0)
            swe.append(float(s1.snow_depth))
            runoff.append(float(s1.runoff))
            rain.append(float(s1.rainfall))
        cont = float(sim.runoff_error) * 100.0
    return hours, swe, runoff, rain, cont


def _total_melt_in(swe: list[float]) -> float:
    """Total melted depth (inches): the sum of per-step SWE decreases."""
    return sum(max(0.0, swe[i - 1] - swe[i]) for i in range(1, len(swe)))


def _peak(series: list[float]) -> tuple[float, int]:
    if not series:
        return 0.0, 0
    i = max(range(len(series)), key=lambda k: series[k])
    return series[i], i


# --------------------------------------------------------------------------- #
# Chart specs (Vega-Lite; chart-first validation class, no raster)
# --------------------------------------------------------------------------- #
def _swe_chart_spec(hours: list[float], swe: list[float],
                    swe_removal: list[float] | None) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for t, v in zip(hours, swe):
        rows.append({"t_hr": round(t, 2), "swe_in": round(v, 4),
                     "series": "snowpack (no removal)"})
    if swe_removal is not None:
        for t, v in zip(hours, swe_removal):
            rows.append({"t_hr": round(t, 2), "swe_in": round(v, 4),
                         "series": "snowpack (plowed)"})
    return {
        "title": "snow water equivalent: accumulation then degree-day melt",
        "data": {"values": rows},
        "mark": {"type": "line"},
        "encoding": {
            "x": {"field": "t_hr", "type": "quantitative", "title": "time (hr)"},
            "y": {"field": "swe_in", "type": "quantitative",
                  "title": "snow water equivalent (in)"},
            "color": {"field": "series", "type": "nominal", "title": ""},
        },
    }


def _runoff_chart_spec(hours: list[float], snowmelt: list[float],
                       rain_only: list[float]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for t, v in zip(hours, snowmelt):
        rows.append({"t_hr": round(t, 2), "q_cfs": round(v, 4),
                     "series": "snowmelt physics"})
    for t, v in zip(hours, rain_only):
        rows.append({"t_hr": round(t, 2), "q_cfs": round(v, 4),
                     "series": "rain-only (climate-naive)"})
    return {
        "title": "runoff hydrograph: snowmelt vs rain-only",
        "data": {"values": rows},
        "mark": {"type": "line"},
        "encoding": {
            "x": {"field": "t_hr", "type": "quantitative", "title": "time (hr)"},
            "y": {"field": "q_cfs", "type": "quantitative",
                  "title": "runoff (cfs)"},
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
async def swmm_snowmelt_degree_day(
    temperature_series_f: list[tuple[str, float]] | list[list[Any]] | None = None,
    rainfall_series_in_hr: list[tuple[str, float]] | list[list[Any]] | None = None,
    dt_min: int = 60,
    area_ac: float = 50.0,
    cmin: float = 0.001,
    cmax: float = 0.01,
    base_temp_f: float = 32.0,
    dividing_temp_f: float = 32.0,
    percent_impervious: float = 80.0,
    snow_removal: bool = True,
    plow_threshold_in: float = 0.3,
    plow_fraction: float = 0.90,
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Snowpack degree-day melt (rain-on-snow): how snow accumulation + melt
    reshape the winter runoff hydrograph vs treating all precipitation as rain.

    Authors a native SWMM 5 [SNOWPACKS] deck on one subcatchment forced by a
    temperature + rainfall series and solves it headless (pyswmm, in-process) in
    three variants on the SAME forcing: (1) snowmelt physics, (2) a rain-only
    climate-naive control (dividing temperature dropped below all temperatures),
    and (3) -- when ``snow_removal`` -- a plow REMOVAL variant that transfers
    plowable snow out of the watershed. Emits an SWE-series chart and a
    snowmelt-vs-rain-only runoff chart; returns typed scalars. No engine RASTER
    (chart-first validation class). Degree-day method per the EPA SWMM Reference
    Manual Vol. I snowmelt chapter (module docstring).

    Parameters:
      temperature_series_f: hourly air temperature as ``[("H:MM", degF), ...]``
        (the degree-day driver + rain/snow split). Default = a representative
        Buffalo NY rain-on-snow event; the live proof passes REAL ASOS ``tmpf``.
      rainfall_series_in_hr: rainfall intensity ``[("H:MM", in/hr), ...]``.
        Default pairs with the temperature default (snowfall in the cold spell,
        a warm rain burst on the ripe snowpack).
      dt_min: timestep, minutes. Default 60.
      area_ac: subcatchment area, acres. Default 50.
      cmin, cmax: degree-day melt coefficients (in/hr/degF), seasonally ramped.
      base_temp_f: snow-melt base temperature (degF). Default 32.
      dividing_temp_f: rain/snow dividing temperature (degF). Default 32.
      percent_impervious: subcatchment imperviousness (percent). Default 80.
      snow_removal: also run the plow-removal variant (default True) -- the
        snow-removal / plowing management knob.
      plow_threshold_in: snow depth (in) above which plowing removes snow.
      plow_fraction: fraction of the impervious area that is plowable.

    Returns:
      A dict of scalars: ``peak_swe_in``, ``total_melt_in``,
      ``snowmelt_runoff_peak_cfs`` (+ its ``_hr`` timing),
      ``rain_only_runoff_peak_cfs`` (+ ``_hr``),
      ``rain_on_snow_peak_amplification`` (snowmelt peak / rain-only peak),
      ``cold_period_runoff_fraction_rain_only`` (share of rain-only runoff that
      falls DURING the cold accumulation window -- the climate-naive artifact),
      ``removal_peak_swe_in`` + ``removal_runoff_peak_cfs`` (when snow_removal),
      ``continuity_error_pct``, and ``curves``.
    """
    from trid3nt_server.emission.pipeline_emitter import current_emitter

    def _coerce(series: Any) -> list[tuple[str, float]] | None:
        if not series:
            return None
        try:
            return [(str(c), float(v)) for c, v in series]
        except (TypeError, ValueError):
            return None

    temp = _coerce(temperature_series_f)
    rain = _coerce(rainfall_series_in_hr)
    if temp is None or rain is None:
        dtemp, drain = default_rain_on_snow_forcing(dt_min)
        temp = temp or dtemp
        rain = rain or drain

    try:
        dt_min_i = max(int(dt_min), 1)
        area = max(float(area_ac), 0.01)
    except (TypeError, ValueError) as exc:
        return {"status": "error", "error_code": "SWMM_SNOWMELT_INVALID",
                "error_message": f"bad numeric input: {exc}"}

    common = dict(dt_min=dt_min_i, area_ac=area, cmin=float(cmin),
                  cmax=float(cmax), base_temp_f=float(base_temp_f),
                  percent_impervious=float(percent_impervious),
                  plow_fraction=float(plow_fraction))

    try:
        import asyncio

        # (1) snowmelt physics
        inp_snow = build_snowmelt_inp(temp, rain, dividing_temp_f=float(dividing_temp_f),
                                      removal=False, **common)
        hrs, swe, ro_snow, rn, cont = await asyncio.to_thread(solve_snowmelt_deck, inp_snow)
        # (2) rain-only control: dividing temperature below all temperatures
        inp_rain = build_snowmelt_inp(temp, rain, dividing_temp_f=-99.0,
                                      removal=False, **common)
        _, _, ro_rain, _, _ = await asyncio.to_thread(solve_snowmelt_deck, inp_rain)
        # (3) plow-removal variant
        swe_rem: list[float] | None = None
        ro_rem: list[float] | None = None
        if snow_removal:
            inp_rem = build_snowmelt_inp(temp, rain, dividing_temp_f=float(dividing_temp_f),
                                         removal=True, plow_threshold_in=float(plow_threshold_in),
                                         **common)
            _, swe_rem, ro_rem, _, _ = await asyncio.to_thread(solve_snowmelt_deck, inp_rem)
    except Exception as exc:  # noqa: BLE001
        logger.exception("swmm snowmelt solve failed")
        return {"status": "error", "error_code": "SWMM_SNOWMELT_SOLVE_FAILED",
                "error_message": str(exc)}

    peak_swe, _ = _peak(swe)
    total_melt = _total_melt_in(swe)
    snow_peak, snow_i = _peak(ro_snow)
    rain_peak, rain_i = _peak(ro_rain)
    amplification = (snow_peak / rain_peak) if rain_peak > 0 else 0.0

    # cold-period artifact: share of rain-only runoff volume falling during the
    # cold accumulation window (temperature <= dividing_temp) -- runoff a
    # climate-naive model fabricates because it treats the snowfall as rain.
    cold_flags = [v <= float(dividing_temp_f) for _, v in temp]
    rain_total = sum(ro_rain) or 1.0
    cold_rain_only = sum(q for q, cold in zip(ro_rain, cold_flags[:len(ro_rain)]) if cold)
    cold_frac = cold_rain_only / rain_total

    removal_peak_swe = _peak(swe_rem)[0] if swe_rem else None
    removal_runoff_peak = _peak(ro_rem)[0] if ro_rem else None

    logger.info(
        "swmm snowmelt: peak_SWE=%.3f in total_melt=%.3f in snowmelt_peak=%.3f cfs "
        "rain_only_peak=%.3f cfs amp=%.3f cold_frac_rain_only=%.3f removal_swe=%s cont=%.3f%%",
        peak_swe, total_melt, snow_peak, rain_peak, amplification, cold_frac,
        None if removal_peak_swe is None else round(removal_peak_swe, 3), cont,
    )

    emitter = current_emitter()
    charts_emitted = 0
    if emitter is not None and hasattr(emitter, "emit_chart"):
        try:
            from trid3nt_server.agent.tools.processing.charts_common import build_chart_payload
            for spec, title, cap in (
                (_swe_chart_spec(hrs, swe, swe_rem),
                 "snow water equivalent: accumulation then degree-day melt",
                 f"Snow Pack degree-day melt over {area:.0f} ac: peak SWE {peak_swe:.2f} in, "
                 f"total melt {total_melt:.2f} in"
                 + (f"; plowing cuts peak SWE to {removal_peak_swe:.2f} in."
                    if removal_peak_swe is not None else ".")),
                (_runoff_chart_spec(hrs, ro_snow, ro_rain),
                 "runoff hydrograph: snowmelt vs rain-only (rain-on-snow)",
                 f"Rain-on-snow: snowmelt physics peaks {snow_peak:.2f} cfs at "
                 f"{hrs[snow_i]:.0f} h vs rain-only {rain_peak:.2f} cfs "
                 f"({amplification:.2f}x); the rain-only model also fabricates "
                 f"{cold_frac*100:.0f}% of its runoff during the cold spell "
                 f"(EPA SWMM degree-day snowmelt)."),
            ):
                payload = build_chart_payload(vega_lite_spec=spec, title=title, caption=cap)
                await emitter.emit_chart(payload)
                charts_emitted += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("swmm snowmelt chart emit failed: %s", exc)

    return {
        "status": "ok",
        "model": "swmm_snowpack_degree_day_melt",
        "citation": ("EPA SWMM Reference Manual Vol. I (Hydrology), Snowmelt "
                     "chapter (degree-day method); swmm5.org snowmelt example"),
        "area_ac": area,
        "peak_swe_in": round(peak_swe, 4),
        "total_melt_in": round(total_melt, 4),
        "final_swe_in": round(swe[-1], 4) if swe else 0.0,
        "snowmelt_runoff_peak_cfs": round(snow_peak, 4),
        "snowmelt_runoff_peak_hr": round(hrs[snow_i], 2) if hrs else 0.0,
        "rain_only_runoff_peak_cfs": round(rain_peak, 4),
        "rain_only_runoff_peak_hr": round(hrs[rain_i], 2) if hrs else 0.0,
        "rain_on_snow_peak_amplification": round(amplification, 4),
        "cold_period_runoff_fraction_rain_only": round(cold_frac, 4),
        "removal_peak_swe_in": (round(removal_peak_swe, 4)
                                if removal_peak_swe is not None else None),
        "removal_runoff_peak_cfs": (round(removal_runoff_peak, 4)
                                    if removal_runoff_peak is not None else None),
        "continuity_error_pct": round(cont, 4),
        "curves": {
            "hours": [round(t, 3) for t in hrs],
            "swe_in": [round(v, 4) for v in swe],
            "runoff_snowmelt_cfs": [round(v, 4) for v in ro_snow],
            "runoff_rain_only_cfs": [round(v, 4) for v in ro_rain],
            "swe_removal_in": ([round(v, 4) for v in swe_rem] if swe_rem else None),
        },
        "charts_emitted": charts_emitted,
    }
