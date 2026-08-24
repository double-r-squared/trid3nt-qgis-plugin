"""The snowmelt step family: the forcing, the deck writer, the metrics, the charts.

The forcing is ONE declared step that three decks Ref, which is what makes "three
variants on the SAME forcing" a property of the plan rather than a claim in a
docstring. The deck writer is this template's serialization hook: the
``[SNOWPACKS]`` surfaces, the ``[TEMPERATURE] SNOWMELT`` degree-day block and the
two time series. What stays literal is the schematic scaffolding - the
subcatchment width and slope, the subarea roughness and depression storage, the
single outfall - which is the mechanism that lets a runoff hydrograph be read,
not a property of any site.
"""

from __future__ import annotations

import logging
from typing import Any, Sequence

from trid3nt_server.workflows.lib import Step

from trid3nt_server.workflows.swmm.steps import (
    clock,
    coerce_series,
    line_chart_spec,
    peak,
    timeseries_block,
)

__all__ = [
    "Deck",
    "Forcing",
    "Metrics",
    "RAIN_ONLY_DIVIDING_TEMP_F",
    "SUBCATCHMENT",
    "build_runoff_chart",
    "build_snowmelt_inp",
    "build_swe_chart",
    "rain_on_snow_forcing",
    "snowmelt_metrics",
    "write_snowmelt_deck",
]

logger = logging.getLogger(
    "trid3nt_server.workflows.swmm.snowmelt_degree_day.steps")

_STEPS = "trid3nt_server.workflows.swmm.snowmelt_degree_day.steps"

#: The subcatchment the Snow Pack is assigned to in this schematic deck.
SUBCATCHMENT = "S1"

#: The rain-only CONTROL, defined: a dividing temperature below any real air
#: temperature, so every drop falls as rain and the snowpack never forms. A
#: definition of the control variant, not a scenario value anyone would set.
RAIN_ONLY_DIVIDING_TEMP_F = -99.0


class Forcing:
    """The weather the three variants share."""

    @staticmethod
    def rain_on_snow(**kwargs: Any) -> Step:
        """The declared cold-spell-then-warm-storm temperature and rain series."""
        return Step(runner=f"{_STEPS}.rain_on_snow_forcing", kwargs=kwargs)


class Deck:
    """SWMM deck writers. One constructor per question the family answers."""

    @staticmethod
    def snowmelt(**kwargs: Any) -> Step:
        """Author the ``[SNOWPACKS]`` degree-day deck for one variant."""
        return Step(runner=f"{_STEPS}.write_snowmelt_deck", kwargs=kwargs)


class Metrics:
    """Answer extraction from solved series - never a second physics model."""

    @staticmethod
    def snowmelt(**kwargs: Any) -> Step:
        """What the snowpack held, what it released, and what rain-only missed."""
        return Step(runner=f"{_STEPS}.snowmelt_metrics", kwargs=kwargs)


# --------------------------------------------------------------------------- #
# The forcing
# --------------------------------------------------------------------------- #
async def rain_on_snow_forcing(
    *,
    dt_min: int,
    sim_days: float,
    cold_temp_f: float,
    warm_temp_f: float,
    warmup_start_hr: float,
    warmup_end_hr: float,
    snowfall_start_hr: float,
    snowfall_end_hr: float,
    snowfall_intensity_in_hr: float,
    rain_start_hr: float,
    rain_end_hr: float,
    rain_intensity_in_hr: float,
    temperature_series_f: Sequence[Any] | None = None,
    rainfall_series_in_hr: Sequence[Any] | None = None,
) -> dict[str, Any]:
    """The rain-on-snow forcing, as declared - or the explicit series instead.

    The PATTERN is the question this template asks: a cold spell that accumulates
    a snowpack, a warm-up that ripens it, and a rain burst that melts it while
    adding its own water. Every number in that pattern is a declared param, so
    the shape is editable on the card rather than baked into a demo function.

    Either series may be supplied explicitly and the other still declared - the
    live proof does exactly that, driving REAL ASOS air temperature against the
    declared precipitation phasing.
    """
    explicit_temp = coerce_series(temperature_series_f, what="temperature_series_f")
    explicit_rain = coerce_series(rainfall_series_in_hr, what="rainfall_series_in_hr")

    dt_min = int(dt_min)
    temp: list[tuple[str, float]] = []
    rain: list[tuple[str, float]] = []
    steps = max(int(round(sim_days * 24 * 60 / dt_min)), 1)
    ramp = max(warmup_end_hr - warmup_start_hr, 1e-9)
    for i in range(steps):
        mins = i * dt_min
        hour = mins / 60.0
        when = clock(mins)
        if hour < warmup_start_hr:
            t_f = cold_temp_f
        elif hour < warmup_end_hr:
            t_f = cold_temp_f + (hour - warmup_start_hr) * (warm_temp_f - cold_temp_f) / ramp
        else:
            t_f = warm_temp_f
        temp.append((when, round(t_f, 2)))
        if snowfall_start_hr <= hour < snowfall_end_hr:
            rain.append((when, snowfall_intensity_in_hr))
        elif rain_start_hr <= hour < rain_end_hr:
            rain.append((when, rain_intensity_in_hr))
        else:
            rain.append((when, 0.0))

    return {"temperature": explicit_temp or temp, "rainfall": explicit_rain or rain}


# --------------------------------------------------------------------------- #
# The deck
# --------------------------------------------------------------------------- #
async def write_snowmelt_deck(
    *,
    temperature_series_f: Sequence[Any],
    rainfall_series_in_hr: Sequence[Any],
    dt_min: int,
    area_ac: float,
    cmin: float,
    cmax: float,
    base_temp_f: float,
    dividing_temp_f: float,
    percent_impervious: float,
    plow_fraction: float,
    removal: bool,
    plow_threshold_in: float,
    plow_out_fraction: float,
    free_water_fraction: float,
    initial_snow_depth_in: float,
    initial_free_water_in: float,
    depth_at_full_cover_in: float,
    ati_weight: float,
    negative_melt_ratio: float,
    site_elevation_ft: float,
    site_latitude_deg: float,
    longitude_correction_min: float,
    evaporation_in_day: float,
    horton_max_rate_in_hr: float,
    horton_min_rate_in_hr: float,
    horton_decay_per_hr: float,
    horton_dry_time_days: float,
) -> dict[str, Any]:
    """Author the deck for ONE variant and report which variant it is.

    ``removal=True`` adds the plow ``REMOVAL`` line; a
    ``dividing_temp_f`` below every air temperature is the rain-only control.
    """
    inp = build_snowmelt_inp(
        [(str(w), float(v)) for w, v in temperature_series_f],
        [(str(w), float(v)) for w, v in rainfall_series_in_hr],
        int(dt_min), float(area_ac), cmin=float(cmin), cmax=float(cmax),
        base_temp_f=float(base_temp_f), dividing_temp_f=float(dividing_temp_f),
        percent_impervious=float(percent_impervious),
        plow_fraction=float(plow_fraction), removal=bool(removal),
        plow_threshold_in=float(plow_threshold_in),
        plow_out_fraction=float(plow_out_fraction),
        free_water_fraction=float(free_water_fraction),
        initial_snow_depth_in=float(initial_snow_depth_in),
        initial_free_water_in=float(initial_free_water_in),
        depth_at_full_cover_in=float(depth_at_full_cover_in),
        ati_weight=float(ati_weight),
        negative_melt_ratio=float(negative_melt_ratio),
        site_elevation_ft=float(site_elevation_ft),
        site_latitude_deg=float(site_latitude_deg),
        longitude_correction_min=float(longitude_correction_min),
        evaporation_in_day=float(evaporation_in_day),
        horton_max_rate_in_hr=float(horton_max_rate_in_hr),
        horton_min_rate_in_hr=float(horton_min_rate_in_hr),
        horton_decay_per_hr=float(horton_decay_per_hr),
        horton_dry_time_days=float(horton_dry_time_days),
    )
    return {"inp_text": inp, "removal": bool(removal),
            "dividing_temp_f": float(dividing_temp_f)}


def build_snowmelt_inp(
    temperature_series_f: Sequence[tuple[str, float]],
    rainfall_series_in_hr: Sequence[tuple[str, float]],
    dt_min: int,
    area_ac: float,
    *,
    cmin: float,
    cmax: float,
    base_temp_f: float,
    dividing_temp_f: float,
    percent_impervious: float,
    plow_fraction: float,
    removal: bool,
    plow_threshold_in: float,
    plow_out_fraction: float,
    free_water_fraction: float,
    initial_snow_depth_in: float,
    initial_free_water_in: float,
    depth_at_full_cover_in: float,
    ati_weight: float,
    negative_melt_ratio: float,
    site_elevation_ft: float,
    site_latitude_deg: float,
    longitude_correction_min: float,
    evaporation_in_day: float,
    horton_max_rate_in_hr: float,
    horton_min_rate_in_hr: float,
    horton_decay_per_hr: float,
    horton_dry_time_days: float,
) -> str:
    """One subcatchment with a ``[SNOWPACKS]`` Snow Pack, a ``[TEMPERATURE]``
    SNOWMELT block (the degree-day driver plus the rain/snow dividing
    temperature), and the temperature + rainfall series. US units (degF, in,
    in/hr/degF).

    The scaffolding that carries the answer OUT - the subcatchment width and
    slope, the subarea roughness and depression storage, the single outfall - is
    fixed: it is the schematic that lets the runoff hydrograph be read, not a
    property of the site.
    """
    ts_temp = timeseries_block("TSER_T", temperature_series_f, precision=3)
    ts_rain = timeseries_block("TSER_R", rainfall_series_in_hr, precision=4)
    # Cmin Cmax Tbase FWF SD0 FW0, then SNN0 (plowable fraction) or SD100.
    surface = f"{cmin} {cmax} {base_temp_f} {free_water_fraction:.2f} " \
              f"{initial_snow_depth_in:g} {initial_free_water_in:g}"
    snow = (
        f"SP1 PLOWABLE   {surface} {plow_fraction}\n"
        f"SP1 IMPERVIOUS {surface} {depth_at_full_cover_in}\n"
        f"SP1 PERVIOUS   {surface} {depth_at_full_cover_in}\n"
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
CONSTANT {evaporation_in_day}
DRY_ONLY NO

[TEMPERATURE]
TIMESERIES TSER_T
SNOWMELT {dividing_temp_f} {ati_weight:g} {negative_melt_ratio:g} \
{site_elevation_ft:g} {site_latitude_deg} {longitude_correction_min:g}

[RAINGAGES]
RG1 INTENSITY 0:{dt_min:02d} 1.0 TIMESERIES TSER_R

[SUBCATCHMENTS]
{SUBCATCHMENT} RG1 OUT {area_ac} {percent_impervious} 500 0.5 0 SP1

[SUBAREAS]
{SUBCATCHMENT} 0.01 0.10 0.05 0.05 25 OUTLET

[INFILTRATION]
{SUBCATCHMENT} {horton_max_rate_in_hr} {horton_min_rate_in_hr} \
{horton_decay_per_hr:g} {horton_dry_time_days:g} 0

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


# --------------------------------------------------------------------------- #
# The answer
# --------------------------------------------------------------------------- #
async def snowmelt_metrics(
    *,
    snowmelt: dict[str, Any],
    rain_only: dict[str, Any],
    plowed: dict[str, Any],
    temperature: Sequence[Any],
    subcatchment: str,
    area_ac: float,
    dividing_temp_f: float,
) -> dict[str, Any]:
    """What the snowpack held, what it released, and what a rain-only model misses.

    The rain-only run is the CONTROL that isolates the snowmelt contribution; the
    plowed run is the management variant. Every number is read off a solved
    series - nothing here re-models the physics.
    """
    hours = list(snowmelt["hours"])
    swe = list(snowmelt["subcatchments"][subcatchment]["snow_depth"])
    runoff_snow = list(snowmelt["subcatchments"][subcatchment]["runoff"])
    runoff_rain = list(rain_only["subcatchments"][subcatchment]["runoff"])
    swe_plow = list(plowed["subcatchments"][subcatchment]["snow_depth"])
    runoff_plow = list(plowed["subcatchments"][subcatchment]["runoff"])

    peak_swe, _ = peak(swe)
    total_melt = sum(max(0.0, swe[i - 1] - swe[i]) for i in range(1, len(swe)))
    snow_peak, snow_i = peak(runoff_snow)
    rain_peak, rain_i = peak(runoff_rain)
    amplification = (snow_peak / rain_peak) if rain_peak > 0 else 0.0

    # The climate-naive ARTIFACT: the share of the rain-only run's runoff volume
    # that falls during the cold accumulation window - runoff a model fabricates
    # because it treated the snowfall as rain.
    cold_flags = [float(v) <= dividing_temp_f for _, v in temperature]
    rain_total = sum(runoff_rain) or 1.0
    cold_share = sum(q for q, cold in zip(runoff_rain, cold_flags) if cold) / rain_total

    plow_peak_swe, _ = peak(swe_plow)
    plow_peak_runoff, _ = peak(runoff_plow)
    continuity = float(snowmelt["runoff_error_pct"])

    logger.info(
        "swmm snowmelt: peak_SWE=%.3f in total_melt=%.3f in snowmelt_peak=%.3f cfs "
        "rain_only_peak=%.3f cfs amp=%.3f cold_frac_rain_only=%.3f "
        "removal_swe=%.3f cont=%.3f%%",
        peak_swe, total_melt, snow_peak, rain_peak, amplification, cold_share,
        plow_peak_swe, continuity,
    )
    return {
        "area_ac": float(area_ac),
        "peak_swe_in": round(peak_swe, 4),
        "total_melt_in": round(total_melt, 4),
        "final_swe_in": round(swe[-1], 4) if swe else 0.0,
        "snowmelt_runoff_peak_cfs": round(snow_peak, 4),
        "snowmelt_runoff_peak_hr": round(hours[snow_i], 2) if hours else 0.0,
        "rain_only_runoff_peak_cfs": round(rain_peak, 4),
        "rain_only_runoff_peak_hr": round(hours[rain_i], 2) if hours else 0.0,
        "rain_on_snow_peak_amplification": round(amplification, 4),
        "cold_period_runoff_fraction_rain_only": round(cold_share, 4),
        "removal_peak_swe_in": round(plow_peak_swe, 4),
        "removal_runoff_peak_cfs": round(plow_peak_runoff, 4),
        "continuity_error_pct": round(continuity, 4),
        "curves": {
            "hours": [round(t, 3) for t in hours],
            "swe_in": [round(v, 4) for v in swe],
            "runoff_snowmelt_cfs": [round(v, 4) for v in runoff_snow],
            "runoff_rain_only_cfs": [round(v, 4) for v in runoff_rain],
            "swe_removal_in": [round(v, 4) for v in swe_plow],
        },
    }


def build_swe_chart(*, result: Any, params: Any) -> dict[str, Any] | None:
    """Snow water equivalent: accumulation, then degree-day melt, plowed vs not."""
    curves = (result or {}).get("curves") or {}
    hours = curves.get("hours") or []
    swe = curves.get("swe_in") or []
    plowed = curves.get("swe_removal_in") or []
    if len(hours) < 2 or len(swe) != len(hours):
        return None

    from trid3nt_server.tools.processing.charts_common import build_chart_payload

    series = {"snowpack (no removal)": list(zip(hours, swe))}
    if len(plowed) == len(hours):
        series["snowpack (plowed)"] = list(zip(hours, plowed))
    title = "snow water equivalent: accumulation then degree-day melt"
    spec = line_chart_spec(
        title=title, series=series, x_title="time (hr)",
        y_title="snow water equivalent (in)",
        x_field="t_hr", y_field="swe_in", x_round=2, y_round=None,
    )
    if spec is None:
        return None
    return build_chart_payload(
        vega_lite_spec=spec, title=title,
        caption=(
            f"Snow Pack degree-day melt over {result['area_ac']:.0f} ac: peak SWE "
            f"{result['peak_swe_in']:.2f} in, total melt "
            f"{result['total_melt_in']:.2f} in; plowing cuts peak SWE to "
            f"{result['removal_peak_swe_in']:.2f} in."
        ),
    )


def build_runoff_chart(*, result: Any, params: Any) -> dict[str, Any] | None:
    """The rain-on-snow hydrograph against the climate-naive rain-only control."""
    curves = (result or {}).get("curves") or {}
    hours = curves.get("hours") or []
    snow = curves.get("runoff_snowmelt_cfs") or []
    rain = curves.get("runoff_rain_only_cfs") or []
    if len(hours) < 2 or len(snow) != len(hours) or len(rain) != len(hours):
        return None

    from trid3nt_server.tools.processing.charts_common import build_chart_payload

    title = "runoff hydrograph: snowmelt vs rain-only"
    spec = line_chart_spec(
        title=title,
        series={"snowmelt physics": list(zip(hours, snow)),
                "rain-only (climate-naive)": list(zip(hours, rain))},
        x_title="time (hr)", y_title="runoff (cfs)",
        x_field="t_hr", y_field="q_cfs", x_round=2, y_round=None,
    )
    if spec is None:
        return None
    return build_chart_payload(
        vega_lite_spec=spec,
        title="runoff hydrograph: snowmelt vs rain-only (rain-on-snow)",
        caption=(
            f"Rain-on-snow: snowmelt physics peaks "
            f"{result['snowmelt_runoff_peak_cfs']:.2f} cfs at "
            f"{result['snowmelt_runoff_peak_hr']:.0f} h vs rain-only "
            f"{result['rain_only_runoff_peak_cfs']:.2f} cfs "
            f"({result['rain_on_snow_peak_amplification']:.2f}x); the rain-only "
            f"model also fabricates "
            f"{result['cold_period_runoff_fraction_rain_only'] * 100:.0f}% of its "
            "runoff during the cold spell (EPA SWMM degree-day snowmelt)."
        ),
    )
