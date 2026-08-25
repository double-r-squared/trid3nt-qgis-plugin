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
native SWMM 5 snowmelt engine (pyswmm, in-process). THREE variants run on ONE
declared forcing, and the plan says so - one forcing step, three decks:

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
The declared forcing defaults describe a representative Buffalo NY rain-on-snow
event (cited climatology), as LABELED, BOUNDED params rather than a baked demo
series; the live proof supplies REAL hourly ASOS/METAR air temperature
(fetch_asos_metar ``tmpf``) at a snowbelt station instead.

Chart-first validation class (the RDII template precedent): the deliverable is
CHARTS (SWE series + runoff hydrograph snowmelt-vs-rain-only) plus typed scalars,
no georeferenced raster. Host-side pyswmm, no worker image.

Declared as PARAMS + ``plan(p, d)``; see ``docs/design/declarative-workflows.md``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.workflows.lib import (
    DeclarativeError,
    FormGate,
    Param,
    Ref,
    Plan,
    doors,
    interpret,
    render_docstring,
    resolve_params,
)
from trid3nt_server.workflows.swmm._template_card import TemplateCard
from trid3nt_server.workflows.swmm.snowmelt_degree_day.steps import (
    build_runoff_chart,
    build_swe_chart,
    RAIN_ONLY_DIVIDING_TEMP_F,
    SUBCATCHMENT,
    Deck,
    Forcing,
    Metrics,
)
from trid3nt_server.workflows.swmm.steps import Solve, SwmmStepError

logger = logging.getLogger(
    "trid3nt_server.workflows.swmm.snowmelt_degree_day.snowmelt_degree_day")

__all__ = ["DATA", "PARAMS", "plan", "swmm_snowmelt_degree_day"]

_STEPS = "trid3nt_server.workflows.swmm.snowmelt_degree_day.steps"

#: The three subcatchment attributes one solve samples: the snowpack, the runoff
#: it produces, and the precipitation that drove it.
_SAMPLED = ("snow_depth", "runoff", "rainfall")


TEMPLATE_CARD = TemplateCard(
    question=(
        "how does snowpack accumulation and degree-day melt reshape the winter "
        "runoff hydrograph versus treating all precipitation as rain -- the "
        "rain-on-snow flood driver, with the snow-removal (plowing) variant"
    ),
    required_inputs=[],
    knobs=(
        "temperature_series_f, rainfall_series_in_hr (or the declared cold-spell / "
        "warm-up / snowfall / rain-burst forcing shape), dt_min, area_ac, "
        "cmin/cmax (degree-day melt coefficients), base_temp_f, dividing_temp_f, "
        "plow_threshold_in, plow_fraction"
    ),
)


PARAMS: tuple[Param, ...] = (
    # -- the forcing, supplied or declared ----------------------------------- #
    Param("temperature_series_f", door=doors.USER, optional=True,
          consequence="scenario",
          derived_when_absent=(
              "the declared cold-spell / warm-up temperature shape is used: "
              "cold_temp_f until warmup_start_hr, ramping to warm_temp_f by "
              "warmup_end_hr"),
          desc="Explicit hourly air temperature [[\"H:MM\", degF], ...] - the "
               "degree-day driver AND the rain/snow split; the live proof passes "
               "REAL ASOS observations here"),
    Param("rainfall_series_in_hr", door=doors.USER, optional=True,
          consequence="scenario",
          derived_when_absent=(
              "the declared precipitation phasing is used: snowfall through the "
              "cold spell, then a rain burst on the ripe snowpack"),
          desc="Explicit rainfall intensity [[\"H:MM\", in/hr], ...], superseding "
               "the declared snowfall and rain-burst windows"),
    Param("sim_days", door=doors.SCENARIO, default=5.0, bounds=(0.5, 180.0),
          units="day", consequence="numerical",
          desc="Length of the declared forcing window; it must outlast the "
               "warm-up for the melt to complete"),
    Param("cold_temp_f", door=doors.SCENARIO, default=20.0, bounds=(-60.0, 32.0),
          units="degF", consequence="scenario",
          desc="Air temperature through the cold spell that builds the snowpack; "
               "sub-freezing, so precipitation falls as snow"),
    Param("warm_temp_f", door=doors.SCENARIO, default=45.0, bounds=(-20.0, 100.0),
          units="degF", consequence="scenario",
          desc="Air temperature after the warm-up; above the base melt "
               "temperature, which is what drives the degree-day melt"),
    Param("warmup_start_hr", door=doors.SCENARIO, default=48.0,
          bounds=(0.0, 4320.0), units="h", consequence="scenario",
          desc="Hour the warm-up ramp begins - the end of the accumulation spell"),
    Param("warmup_end_hr", door=doors.SCENARIO, default=60.0, bounds=(0.0, 4320.0),
          units="h", consequence="scenario",
          desc="Hour the warm-up ramp reaches warm_temp_f"),
    Param("snowfall_start_hr", door=doors.SCENARIO, default=12.0,
          bounds=(0.0, 4320.0), units="h", consequence="scenario",
          desc="Hour the steady snowfall begins, inside the cold spell"),
    Param("snowfall_end_hr", door=doors.SCENARIO, default=36.0,
          bounds=(0.0, 4320.0), units="h", consequence="scenario",
          desc="Hour the snowfall stops, before the warm-up"),
    Param("snowfall_intensity_in_hr", door=doors.SCENARIO, default=0.05,
          bounds=(0.0, 5.0), units="in/hr", consequence="scenario",
          desc="Precipitation intensity through the snowfall window; it falls as "
               "snow because the air is below the dividing temperature"),
    Param("rain_start_hr", door=doors.SCENARIO, default=60.0, bounds=(0.0, 4320.0),
          units="h", consequence="scenario",
          desc="Hour the warm rain burst begins - the rain-ON-SNOW moment"),
    Param("rain_end_hr", door=doors.SCENARIO, default=72.0, bounds=(0.0, 4320.0),
          units="h", consequence="scenario",
          desc="Hour the rain burst ends"),
    Param("rain_intensity_in_hr", door=doors.SCENARIO, default=0.15,
          bounds=(0.0, 10.0), units="in/hr", consequence="scenario",
          desc="Intensity of the warm rain burst that falls on the ripe snowpack"),

    # -- the subcatchment ----------------------------------------------------- #
    Param("area_ac", door=doors.SCENARIO, default=50.0, bounds=(0.01, 1.0e5),
          units="acre", consequence="scenario",
          desc="Subcatchment area the Snow Pack covers"),
    Param("percent_impervious", door=doors.SCENARIO, default=80.0,
          bounds=(0.0, 100.0), units="%", consequence="scenario",
          desc="Impervious fraction of the subcatchment; the plowable surface is "
               "a share of it, and impervious area is what routes melt fastest"),

    # -- the degree-day melt -------------------------------------------------- #
    Param("cmin", door=doors.SCENARIO, default=0.001, bounds=(0.0, 1.0),
          units="in/hr/degF", user_lever=True, consequence="scenario",
          desc="Minimum degree-day melt coefficient (the winter-solstice end of "
               "the seasonal ramp) - a labeled literature value, not a "
               "site calibration"),
    Param("cmax", door=doors.SCENARIO, default=0.01, bounds=(0.0, 1.0),
          units="in/hr/degF", user_lever=True, consequence="scenario",
          desc="Maximum degree-day melt coefficient (the summer-solstice end of "
               "the seasonal ramp) - a labeled literature value"),
    Param("base_temp_f", door=doors.SCENARIO, default=32.0, bounds=(-20.0, 60.0),
          units="degF", consequence="scenario",
          desc="Base melt temperature: melt runs at C*(T - Tbase) while the air "
               "is above it"),
    Param("dividing_temp_f", door=doors.SCENARIO, default=32.0,
          bounds=(-20.0, 60.0), units="degF", consequence="scenario",
          desc="Rain/snow dividing temperature: precipitation falls as snow at or "
               "below it and as rain above it - the single switch the rain-only "
               "control drops below every temperature"),

    # -- the snow-removal (plowing) variant ----------------------------------- #
    Param("plow_threshold_in", door=doors.SCENARIO, default=0.3,
          bounds=(0.0, 100.0), units="in", consequence="scenario",
          desc="Snow depth above which plowing removes snow from the plowable "
               "surface"),
    Param("plow_fraction", door=doors.SCENARIO, default=0.90, bounds=(0.0, 1.0),
          consequence="scenario",
          desc="Fraction of the impervious area that is plowable (streets and "
               "lots rather than roofs)"),
    Param("plow_out_fraction", door=doors.CONSTANT, default=1.0, bounds=(0.0, 1.0),
          consequence="numerical",
          desc="Share of the plowed snow transferred OUT of the watershed rather "
               "than to another surface; 1.0 is the trucked-away case"),

    # -- the snow pack surfaces ----------------------------------------------- #
    Param("free_water_fraction", door=doors.CONSTANT, default=0.10,
          bounds=(0.0, 1.0), consequence="numerical",
          desc="Free-water holding capacity of the pack as a fraction of its "
               "depth - meltwater the pack retains before it releases any"),
    Param("initial_snow_depth_in", door=doors.SCENARIO, default=0.0,
          bounds=(0.0, 200.0), units="in", consequence="scenario",
          desc="Snow water equivalent already on the ground when the window "
               "opens; zero means the pack is built entirely by the declared "
               "snowfall"),
    Param("initial_free_water_in", door=doors.CONSTANT, default=0.0,
          bounds=(0.0, 50.0), units="in", consequence="numerical",
          desc="Liquid water already held in the initial pack"),
    Param("depth_at_full_cover_in", door=doors.CONSTANT, default=2.0,
          bounds=(0.01, 100.0), units="in", consequence="numerical",
          desc="Snow depth at which the non-plowable surfaces are 100% covered - "
               "the areal-depletion scale"),
    Param("ati_weight", door=doors.CONSTANT, default=0.5, bounds=(0.0, 1.0),
          consequence="numerical",
          desc="Antecedent temperature index weight in the SNOWMELT block - how "
               "much the pack remembers yesterday's air temperature"),
    Param("negative_melt_ratio", door=doors.CONSTANT, default=0.6,
          bounds=(0.0, 1.0), consequence="numerical",
          desc="Negative melt ratio: the rate the pack REFREEZES relative to its "
               "melt rate once the air drops back below base_temp_f"),
    Param("site_elevation_ft", door=doors.SCENARIO, default=500.0,
          bounds=(-300.0, 15000.0), units="ft", consequence="scenario",
          desc="Average elevation above mean sea level, used by the SNOWMELT "
               "block's pressure correction; the default is the Buffalo NY "
               "snowbelt the declared forcing describes"),
    Param("site_latitude_deg", door=doors.SCENARIO, default=43.0,
          bounds=(-90.0, 90.0), units="deg", consequence="aoi",
          desc="Latitude driving the seasonal melt-coefficient ramp between cmin "
               "and cmax; the default is the Buffalo NY snowbelt"),
    Param("longitude_correction_min", door=doors.CONSTANT, default=0.0,
          bounds=(-120.0, 120.0), units="min", consequence="numerical",
          desc="Correction between standard and local time in the SNOWMELT "
               "block"),

    # -- the surface the melt runs over --------------------------------------- #
    Param("evaporation_in_day", door=doors.SCENARIO, default=0.0,
          bounds=(0.0, 1.0), units="in/day", consequence="scenario",
          desc="Constant pan evaporation through the window; zero is the winter "
               "case this template is about"),
    Param("horton_max_rate_in_hr", door=doors.CONSTANT, default=3.0,
          bounds=(0.0, 50.0), units="in/hr", consequence="numerical",
          desc="Horton maximum infiltration rate on the pervious area - a typical "
               "medium-textured literature value for the schematic subcatchment, "
               "NOT fitted to any site"),
    Param("horton_min_rate_in_hr", door=doors.CONSTANT, default=0.5,
          bounds=(0.0, 50.0), units="in/hr", consequence="numerical",
          desc="Horton minimum (saturated) infiltration rate; melt above it "
               "becomes runoff"),
    Param("horton_decay_per_hr", door=doors.CONSTANT, default=4.0,
          bounds=(0.0, 20.0), units="1/hr", consequence="numerical",
          desc="Horton decay constant - how fast infiltration capacity falls "
               "toward the minimum during a wet spell"),
    Param("horton_dry_time_days", door=doors.CONSTANT, default=7.0,
          bounds=(0.0, 100.0), units="day", consequence="numerical",
          desc="Days of dry weather needed for the infiltration capacity to "
               "recover fully"),

    # -- how the answer is computed -------------------------------------------- #
    Param("dt_min", door=doors.CONSTANT, default=60, bounds=(1.0, 60.0),
          units="min", consequence="numerical",
          desc="Timestep the forcing series are written on and the engine reports "
               "at; the degree-day method is an hourly method. The upper bound is "
               "the deck's own MM field - a longer step cannot be written"),
)

#: One subcatchment, one outfall, forcing declared as values - nothing this plan
#: consumes is a spatial artifact.
DATA: tuple = ()


def plan(p, d):  # noqa: ANN001, ANN201 - the declared plan value, per the design doc
    """The rain-on-snow recipe. Pure: constructs the plan value, executes nothing.

    ONE forcing step and THREE decks: "three variants on the same forcing" is a
    shape of the plan rather than a promise in prose, and the rain-only control
    differs from the physical run in exactly one declared argument.
    """
    forcing = dict(
        temperature_series_f=Ref("forcing.temperature"),
        rainfall_series_in_hr=Ref("forcing.rainfall"),
    )
    pack = dict(
        dt_min=p.dt_min, area_ac=p.area_ac, cmin=p.cmin, cmax=p.cmax,
        base_temp_f=p.base_temp_f, percent_impervious=p.percent_impervious,
        plow_fraction=p.plow_fraction, plow_threshold_in=p.plow_threshold_in,
        plow_out_fraction=p.plow_out_fraction,
        free_water_fraction=p.free_water_fraction,
        initial_snow_depth_in=p.initial_snow_depth_in,
        initial_free_water_in=p.initial_free_water_in,
        depth_at_full_cover_in=p.depth_at_full_cover_in,
        ati_weight=p.ati_weight, negative_melt_ratio=p.negative_melt_ratio,
        site_elevation_ft=p.site_elevation_ft,
        site_latitude_deg=p.site_latitude_deg,
        longitude_correction_min=p.longitude_correction_min,
        evaporation_in_day=p.evaporation_in_day,
        horton_max_rate_in_hr=p.horton_max_rate_in_hr,
        horton_min_rate_in_hr=p.horton_min_rate_in_hr,
        horton_decay_per_hr=p.horton_decay_per_hr,
        horton_dry_time_days=p.horton_dry_time_days,
    )
    solve = dict(subcatchments=(SUBCATCHMENT,), subcatchment_attrs=_SAMPLED)
    return Plan("swmm_snowmelt_degree_day", "swmm5", (
        FormGate(title="Review the rain-on-snow snowmelt scenario"),
        Forcing.rain_on_snow(
            dt_min=p.dt_min, sim_days=p.sim_days, cold_temp_f=p.cold_temp_f,
            warm_temp_f=p.warm_temp_f, warmup_start_hr=p.warmup_start_hr,
            warmup_end_hr=p.warmup_end_hr,
            snowfall_start_hr=p.snowfall_start_hr,
            snowfall_end_hr=p.snowfall_end_hr,
            snowfall_intensity_in_hr=p.snowfall_intensity_in_hr,
            rain_start_hr=p.rain_start_hr, rain_end_hr=p.rain_end_hr,
            rain_intensity_in_hr=p.rain_intensity_in_hr,
            temperature_series_f=p.temperature_series_f,
            rainfall_series_in_hr=p.rainfall_series_in_hr,
        ).named("forcing"),

        Deck.snowmelt(**forcing, **pack, dividing_temp_f=p.dividing_temp_f,
                      removal=False).named("deck_snow"),
        Solve.pyswmm(inp_text=Ref("deck_snow.inp_text"), label="snowmelt",
                     **solve).named("solve_snow"),

        # The climate-naive CONTROL: one argument different, so what it isolates
        # is unambiguous.
        Deck.snowmelt(**forcing, **pack,
                      dividing_temp_f=RAIN_ONLY_DIVIDING_TEMP_F,
                      removal=False).named("deck_rain"),
        Solve.pyswmm(inp_text=Ref("deck_rain.inp_text"), label="rain-only",
                     **solve).named("solve_rain"),

        Deck.snowmelt(**forcing, **pack, dividing_temp_f=p.dividing_temp_f,
                      removal=True).named("deck_plow"),
        Solve.pyswmm(inp_text=Ref("deck_plow.inp_text"), label="plowed",
                     **solve).named("solve_plow"),

        Metrics.snowmelt(
            snowmelt=Ref("solve_snow"), rain_only=Ref("solve_rain"),
            plowed=Ref("solve_plow"), temperature=Ref("forcing.temperature"),
            subcatchment=SUBCATCHMENT, area_ac=p.area_ac,
            dividing_temp_f=p.dividing_temp_f,
        ).named("snowmelt")
         .chart("swe_series", builder=build_swe_chart)
         .chart("runoff_snowmelt_vs_rain_only",
                builder=build_runoff_chart),
    ))


_METADATA = AtomicToolMetadata(
    name="swmm_snowmelt_degree_day",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="swmm",
    tier="template",
)


@register_tool(
    _METADATA,
    read_only_hint=True,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
)
async def swmm_snowmelt_degree_day(
    temperature_series_f: list[list[Any]] | None = None,
    rainfall_series_in_hr: list[list[Any]] | None = None,
    sim_days: float | None = None,
    dt_min: int | None = None,
    area_ac: float | None = None,
    percent_impervious: float | None = None,
    cmin: float | None = None,
    cmax: float | None = None,
    base_temp_f: float | None = None,
    dividing_temp_f: float | None = None,
    cold_temp_f: float | None = None,
    warm_temp_f: float | None = None,
    warmup_start_hr: float | None = None,
    warmup_end_hr: float | None = None,
    snowfall_start_hr: float | None = None,
    snowfall_end_hr: float | None = None,
    snowfall_intensity_in_hr: float | None = None,
    rain_start_hr: float | None = None,
    rain_end_hr: float | None = None,
    rain_intensity_in_hr: float | None = None,
    plow_threshold_in: float | None = None,
    plow_fraction: float | None = None,
    initial_snow_depth_in: float | None = None,
    site_elevation_ft: float | None = None,
    site_latitude_deg: float | None = None,
    input_mode: str | None = None,
    restart_clean: bool = False,
    **_extra_ignored: Any,
) -> dict[str, Any]:
    supplied = {k: v for k, v in locals().items()
                if k in {q.name for q in PARAMS} and v is not None}
    try:
        p = await resolve_params(PARAMS, supplied)
        result = await interpret(
            plan(p, None), p, PARAMS, DATA,
            input_mode=input_mode, resume=not restart_clean,
        )
    except asyncio.CancelledError:
        raise
    except DeclarativeError as exc:
        logger.warning("swmm_snowmelt_degree_day %s: %s", exc.error_code, exc)
        return {"status": "error", "error_code": exc.error_code,
                "error_message": str(exc)}
    except SwmmStepError as exc:
        logger.warning("swmm_snowmelt_degree_day %s: %s", exc.error_code, exc)
        return {"status": "error", "error_code": exc.error_code,
                "error_message": str(exc)}
    except Exception as exc:  # noqa: BLE001
        if getattr(exc, "retryable", False):
            raise
        logger.exception("swmm_snowmelt_degree_day unexpected failure")
        return {"status": "error", "error_code": "SWMM_SNOWMELT_INTERNAL_ERROR",
                "error_message": str(exc)}

    return {
        "status": "ok",
        "model": "swmm_snowpack_degree_day_melt",
        "citation": ("EPA SWMM Reference Manual Vol. I (Hydrology), Snowmelt "
                     "chapter (degree-day method); swmm5.org snowmelt example"),
        **dict(result.value),
        # The SPEC is the product and the dock is the renderer, so what this
        # reports is what the run BUILT - never a claim about a card it cannot see.
        "chart_specs": sorted(result.charts),
        "notes": result.notes,
    }


_DOC = dict(
    summary="SNOWPACK accumulation + degree-day MELT reshaping a winter runoff hydrograph.",
    routing=(
        "THE tool for \"rain-on-snow flooding\", \"how much runoff comes from "
        "snowmelt\", \"snowpack accumulation and melt\", \"winter/spring melt "
        "flood driver\", \"does plowing change the melt runoff\", \"SWMM snow pack "
        "degree-day method\". Authors a real SWMM 5 [SNOWPACKS] + [TEMPERATURE] "
        "SNOWMELT deck on one subcatchment and solves it headless (pyswmm) in "
        "THREE variants on ONE declared forcing: snowmelt physics, a rain-only "
        "climate-naive control, and a plow-removal variant. SCHEMATIC deck - the "
        "product is the SWE and runoff CHARTS + typed scalars, never a map."
    ),
    not_for=(
        "snow WATER SUPPLY or basin SWE mapping from remote sensing; urban pipe or "
        "street flooding (`swmm_urban_flood`); groundwater baseflow "
        "(`swmm_aquifer_baseflow_to_node`); sewer RDII "
        "(`swmm_rdii_rtk_unit_hydrograph`); landscape snow-driven erosion "
        "(`landlab_*`)"
    ),
    params=PARAMS,
    controls=(
        ("input_mode",
         '"user_gated" presents the resolved param sheet - the forcing shape, the '
         "degree-day coefficients and the pack surfaces - for review/edit before "
         'the three solves, and WAITS; "auto" (session default) proceeds with '
         "every assumption labeled. Not a physical value."),
        ("restart_clean",
         "True discards the ledger a PREVIOUS FAILED attempt at this same "
         "invocation left behind and re-runs every step from the top. Default "
         "False resumes at the failed step."),
    ),
    returns=(
        "On success a dict of scalars: `peak_swe_in`, `total_melt_in`, "
        "`final_swe_in`, `snowmelt_runoff_peak_cfs` (+ `_hr`), "
        "`rain_only_runoff_peak_cfs` (+ `_hr`), "
        "`rain_on_snow_peak_amplification` (snowmelt peak / rain-only peak), "
        "`cold_period_runoff_fraction_rain_only` (the runoff a climate-naive model "
        "fabricates during the cold spell), `removal_peak_swe_in` + "
        "`removal_runoff_peak_cfs` (the plowed variant), `continuity_error_pct` "
        "and `curves`. Narrate those typed numbers. On failure a dict with "
        "`status=\"error\"` + `error_code`."
    ),
)

swmm_snowmelt_degree_day.__doc__ = render_docstring(**_DOC)
swmm_snowmelt_degree_day.routing_doc = render_docstring(**_DOC, view="routing")
