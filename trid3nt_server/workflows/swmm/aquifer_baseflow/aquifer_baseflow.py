"""Engine template ``swmm_aquifer_baseflow_to_node`` - two-zone aquifer baseflow.

How much steady BASEFLOW a shallow unconfined aquifer beneath a pervious
subcatchment contributes to a receiving drainage node BETWEEN storms, and how
that groundwater pathway reshapes the node hydrograph versus surface runoff
alone. The deck authors a real SWMM 5 ``[AQUIFERS]`` two-zone moisture column and
a ``[GROUNDWATER]`` link whose lateral outflow follows

    q_gw = A1 * (Hgw - Hstar)^B1 - A2 * (Hsw - Hstar)^B2 + A3 * Hgw * Hsw

and solves it headless through the native engine (pyswmm, in-process). TWO
variants run on the SAME forcing: the baseflow pathway active (A1 > 0), and the
surface-runoff-only control (A1 = 0) that isolates the contribution.

Chart-first validation class: the deliverable is the node-hydrograph chart plus
typed scalars, on a SCHEMATIC deck - there is no georeferenced raster here. The
site is real and so is the soil column derived at it.

Citations (NATE-verified template source):
  * EPA SWMM Reference Manual Volume I - Hydrology (Rossman & Huber), Groundwater
    chapter (the two-zone AQUIFER moisture balance + the GROUNDWATER flow-equation
    coefficients A1/B1/A2/B2/A3).
  * "Aquifer and Groundwater Objects in SWMM 5" (swmm5.org, CHI) - the two-object
    structure and the flow-coefficient editor.

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
from trid3nt_server.workflows.swmm.aquifer_baseflow.steps import (
    Deck,
    Metrics,
    build_baseflow_chart,
)
from trid3nt_server.workflows.swmm.steps import Solve, SwmmStepError

logger = logging.getLogger(
    "trid3nt_server.workflows.swmm.aquifer_baseflow.aquifer_baseflow"
)

__all__ = ["DATA", "PARAMS", "plan", "swmm_aquifer_baseflow_to_node"]

_SHARED = "trid3nt_server.workflows.swmm.steps"
_STEPS = "trid3nt_server.workflows.swmm.aquifer_baseflow.steps"

#: The receiving node the [GROUNDWATER] link discharges to in this deck.
_NODE = "J1"


TEMPLATE_CARD = TemplateCard(
    question=(
        "how much baseflow does a shallow two-zone aquifer contribute to a "
        "drainage node between storms, and how does the groundwater pathway "
        "reshape the node hydrograph versus surface runoff alone"
    ),
    required_inputs=["location (or lat/lon) for the SoilGrids soil-column derivation"],
    knobs=(
        "location/lat/lon (site of the derived aquifer column), a1/b1 (groundwater "
        "flow coefficients), area_ac, storm_intensity_in_hr / storm_duration_hr / "
        "second_storm_day (the two-storm forcing), rainfall_series_in_hr (an "
        "explicit hyetograph), aquifer porosity/wilting/field-capacity/conductivity "
        "(else SoilGrids-derived), initial_water_table_ft, sim_days"
    ),
)


PARAMS: tuple[Param, ...] = (
    # -- the site ----------------------------------------------------------- #
    Param("location", door=doors.QUESTION, optional=True, consequence="aoi",
          desc="Place name for the site whose soil column the deck is built from"),
    Param("lat", door=doors.USER, optional=True, bounds=(-90.0, 90.0),
          units="deg", consequence="aoi",
          desc="Explicit site latitude, instead of a place name"),
    Param("lon", door=doors.USER, optional=True, bounds=(-180.0, 180.0),
          units="deg", consequence="aoi",
          desc="Explicit site longitude, instead of a place name"),
    Param("site_latlon", door=doors.DERIVED, resolve=f"{_SHARED}.site.site_latlon",
          consequence="aoi",
          desc="The (lat, lon) the soil column is sampled at"),

    # -- the two-zone column (law 9: derived from real soil, never invented) -- #
    Param("porosity", door=doors.DERIVED, resolve=f"{_SHARED}.soil.porosity",
          user_lever=True, bounds=(0.05, 0.8), consequence="physics",
          desc="Aquifer porosity (saturated water content); SoilGrids-derived at "
               "the site via the Saxton-Rawls two-zone fit unless supplied - a "
               "SCREENING near-surface proxy, NOT a measured column"),
    Param("wilting_point", door=doors.DERIVED,
          resolve=f"{_SHARED}.soil.wilting_point", user_lever=True,
          bounds=(0.0, 0.5), consequence="physics",
          desc="Soil wilting point (water content at -1500 kPa), from the same "
               "SoilGrids texture fit unless supplied"),
    Param("field_capacity", door=doors.DERIVED,
          resolve=f"{_SHARED}.soil.field_capacity", user_lever=True,
          bounds=(0.0, 0.7), consequence="physics",
          desc="Soil field capacity (water content at -33 kPa), from the same "
               "SoilGrids texture fit unless supplied"),
    Param("conductivity_in_hr", door=doors.DERIVED,
          resolve=f"{_SHARED}.soil.conductivity_in_hr", user_lever=True,
          bounds=(0.001, 50.0), units="in/hr", consequence="physics",
          desc="Aquifer saturated conductivity governing percolation to the water "
               "table, from the same SoilGrids texture fit unless supplied"),

    # -- the groundwater pathway -------------------------------------------- #
    Param("a1", door=doors.SCENARIO, default=0.002, bounds=(0.0, 10.0),
          user_lever=True, consequence="scenario",
          desc="Groundwater-to-node flow coefficient - the baseflow term; 0 is the "
               "surface-runoff-only control"),
    Param("b1", door=doors.SCENARIO, default=1.0, bounds=(0.1, 3.0),
          consequence="scenario",
          desc="Groundwater flow exponent; 1 is a linear reservoir, giving a clean "
               "exponential baseflow recession"),
    Param("initial_water_table_ft", door=doors.SCENARIO, default=4.0,
          bounds=(0.0, 100.0), units="ft", consequence="scenario",
          desc="Initial saturated-zone water-table elevation - the antecedent state "
               "the recession starts from"),

    # -- the storm forcing --------------------------------------------------- #
    Param("rainfall_series_in_hr", door=doors.USER, optional=True,
          consequence="scenario",
          derived_when_absent=(
              "the two declared storms are used: one at storm_start_hr on day 0 and "
              "one on second_storm_day, dry between"),
          desc="An explicit hyetograph [[\"H:MM\", in/hr], ...], superseding the "
               "declared two-storm pattern"),
    Param("storm_intensity_in_hr", door=doors.SCENARIO, default=0.3,
          bounds=(0.01, 10.0), units="in/hr", consequence="scenario",
          desc="Rainfall intensity during each declared storm"),
    Param("storm_start_hr", door=doors.SCENARIO, default=6.0, bounds=(0.0, 23.0),
          units="h", consequence="scenario",
          desc="Hour of the day each declared storm begins"),
    Param("storm_duration_hr", door=doors.SCENARIO, default=8.0, bounds=(0.5, 48.0),
          units="h", consequence="scenario",
          desc="Length of each declared storm"),
    Param("second_storm_day", door=doors.SCENARIO, default=12.0, bounds=(1.0, 60.0),
          units="day", consequence="scenario",
          desc="Day the second storm falls; it re-recharges the receding aquifer"),
    Param("sim_days", door=doors.SCENARIO, default=24, bounds=(2.0, 365.0),
          units="day", consequence="numerical",
          desc="Simulation length; it must outlast the second storm for the "
               "recharge bump to be visible"),

    # -- the subcatchment (the recharge pathway) ----------------------------- #
    Param("area_ac", door=doors.SCENARIO, default=100.0, bounds=(0.01, 1.0e5),
          units="acre", consequence="scenario",
          desc="Pervious subcatchment area draining to the node"),
    Param("imperviousness_pct", door=doors.SCENARIO, default=5.0,
          bounds=(0.0, 100.0), units="%", consequence="scenario",
          desc="Impervious fraction of the modeled subcatchment; the pervious "
               "remainder is what infiltrates and recharges the aquifer"),
    Param("soil_suction_in", door=doors.CONSTANT, default=3.5, bounds=(0.1, 30.0),
          units="in", consequence="numerical",
          desc="Green-Ampt capillary suction head at the wetting front - a typical "
               "medium-textured value for the schematic subcatchment, NOT fitted "
               "to the site texture the aquifer column is derived from"),
    Param("infiltration_ksat_in_hr", door=doors.CONSTANT, default=0.5,
          bounds=(0.001, 50.0), units="in/hr", consequence="numerical",
          desc="Green-Ampt SURFACE saturated conductivity - the infiltration "
               "capacity of the subcatchment surface, deliberately separate from "
               "the aquifer column's own conductivity"),
    Param("initial_moisture_deficit", door=doors.CONSTANT, default=0.30,
          bounds=(0.0, 0.6), consequence="numerical",
          desc="Green-Ampt initial soil-moisture deficit at the storm start - the "
               "antecedent dryness the first storm infiltrates into"),
    Param("aquifer_seepage_in_hr", door=doors.SCENARIO, default=0.002,
          bounds=(0.0, 5.0), units="in/hr", consequence="scenario",
          desc="Deep seepage rate out of the saturated zone - water that leaves "
               "the modeled aquifer rather than reaching the node"),
    Param("evaporation_in_day", door=doors.SCENARIO, default=0.02,
          bounds=(0.0, 1.0), units="in/day", consequence="scenario",
          desc="Constant pan evaporation applied through the simulation"),
    Param("surface_elev_ft", door=doors.CONSTANT, default=10.0, bounds=(0.0, 500.0),
          units="ft", consequence="numerical",
          desc="Ground-surface elevation above the node invert; the head the water "
               "table rises within"),
    Param("dt_min", door=doors.CONSTANT, default=15, bounds=(1.0, 60.0),
          units="min", consequence="numerical",
          desc="Wet-weather timestep the hyetograph is written on"),

    # -- how the answer is measured ------------------------------------------ #
    Param("dry_window_start_day", door=doors.CONSTANT, default=6.0,
          bounds=(0.0, 365.0), units="day", consequence="numerical",
          desc="Start of the dry window the between-storms baseflow is averaged "
               "over; it must sit inside the dry spell between the two storms"),
    Param("dry_window_end_day", door=doors.CONSTANT, default=11.0,
          bounds=(0.0, 365.0), units="day", consequence="numerical",
          desc="End of that dry window, before the second storm arrives"),
)

#: This deck is schematic - one subcatchment, one node - so nothing it consumes is
#: a spatial artifact. Its one real-world input is the soil column, which is a
#: point SAMPLE and therefore resolves through the doors as declared params.
DATA: tuple = ()


def plan(p, d):  # noqa: ANN001, ANN201 - the declared plan value, per the design doc
    """The aquifer-baseflow recipe. Pure: constructs the plan value, executes nothing.

    Two decks, two solves, one comparison. The control run is DECLARED rather than
    hidden inside a composite, so the ledger can replay the expensive half while
    the chart re-executes.
    """
    forcing = dict(
        rainfall_series_in_hr=p.rainfall_series_in_hr, dt_min=p.dt_min,
        sim_days=p.sim_days, storm_intensity_in_hr=p.storm_intensity_in_hr,
        storm_start_hr=p.storm_start_hr, storm_duration_hr=p.storm_duration_hr,
        second_storm_day=p.second_storm_day,
    )
    column = dict(
        porosity=p.porosity, wilting_point=p.wilting_point,
        field_capacity=p.field_capacity, conductivity_in_hr=p.conductivity_in_hr,
    )
    site = dict(
        area_ac=p.area_ac, imperviousness_pct=p.imperviousness_pct,
        soil_suction_in=p.soil_suction_in,
        infiltration_ksat_in_hr=p.infiltration_ksat_in_hr,
        initial_moisture_deficit=p.initial_moisture_deficit,
        aquifer_seepage_in_hr=p.aquifer_seepage_in_hr,
        evaporation_in_day=p.evaporation_in_day,
        surface_elev_ft=p.surface_elev_ft,
        initial_water_table_ft=p.initial_water_table_ft,
    )
    return Plan("swmm_aquifer_baseflow_to_node", "swmm5", (
        FormGate(title="Review the aquifer-baseflow scenario"),
        Deck.aquifer(a1=p.a1, b1=p.b1, **forcing, **column, **site).named("deck_gw"),
        Solve.pyswmm(inp_text=Ref("deck_gw.inp_text"), nodes=(_NODE,),
                     label="aquifer-with-gw").named("solve_gw"),
        Deck.aquifer(a1=0.0, b1=p.b1, **forcing, **column, **site).named("deck_no_gw"),
        Solve.pyswmm(inp_text=Ref("deck_no_gw.inp_text"), nodes=(_NODE,),
                     label="aquifer-no-gw").named("solve_no_gw"),
        Metrics.baseflow(
            with_gw=Ref("solve_gw"), no_gw=Ref("solve_no_gw"), node=_NODE,
            dry_window_start_day=p.dry_window_start_day,
            dry_window_end_day=p.dry_window_end_day,
            second_storm_day=p.second_storm_day, area_ac=p.area_ac,
            a1=p.a1, b1=p.b1,
        ).named("baseflow")
         .chart("node_hydrograph", builder=build_baseflow_chart),
    ))


_METADATA = AtomicToolMetadata(
    name="swmm_aquifer_baseflow_to_node",
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
async def swmm_aquifer_baseflow_to_node(
    location: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    rainfall_series_in_hr: list[list[Any]] | list[tuple[str, float]] | None = None,
    dt_min: int | None = None,
    area_ac: float | None = None,
    a1: float | None = None,
    b1: float | None = None,
    porosity: float | None = None,
    wilting_point: float | None = None,
    field_capacity: float | None = None,
    conductivity_in_hr: float | None = None,
    initial_water_table_ft: float | None = None,
    sim_days: int | None = None,
    storm_intensity_in_hr: float | None = None,
    storm_start_hr: float | None = None,
    storm_duration_hr: float | None = None,
    second_storm_day: float | None = None,
    input_mode: str | None = None,
    restart_clean: bool = False,
    **_extra_ignored: Any,
) -> dict[str, Any]:
    supplied = {k: v for k, v in locals().items()
                if k in {p.name for p in PARAMS} and v is not None}
    try:
        p = await resolve_params(PARAMS, supplied)
        result = await interpret(
            plan(p, None), p, PARAMS, DATA,
            input_mode=input_mode, resume=not restart_clean,
        )
    except asyncio.CancelledError:
        raise
    except DeclarativeError as exc:
        logger.warning("swmm_aquifer_baseflow_to_node %s: %s", exc.error_code, exc)
        return {"status": "error", "error_code": exc.error_code,
                "error_message": str(exc)}
    except SwmmStepError as exc:
        # A DERIVATION refuses before the plan is ever built (the site, the law-9
        # soil column), so its typed code never passes through a step.
        logger.warning("swmm_aquifer_baseflow_to_node %s: %s", exc.error_code, exc)
        return {"status": "error", "error_code": exc.error_code,
                "error_message": str(exc)}
    except Exception as exc:  # noqa: BLE001
        if getattr(exc, "retryable", False):
            raise
        logger.exception("swmm_aquifer_baseflow_to_node unexpected failure")
        return {"status": "error", "error_code": "SWMM_AQUIFER_INTERNAL_ERROR",
                "error_message": str(exc)}

    answer: dict[str, Any] = dict(result.value)
    # The run's OWN sheet, not the one this call resolved: a form gate may have
    # revised it, and what is narrated has to be what ran.
    ran = result.params or p
    return {
        "status": "ok",
        "model": "swmm_two_zone_aquifer_baseflow",
        "citation": ("EPA SWMM Reference Manual Vol. I (Hydrology), Groundwater "
                     "chapter (two-zone aquifer + A1/B1 flow coefficients); "
                     "swmm5.org Aquifer/Groundwater objects"),
        "aquifer_soil_column": {
            name: round(float(ran.get(name)), 4)
            for name in ("porosity", "wilting_point", "field_capacity",
                         "conductivity_in_hr")
        },
        "aquifer_provenance": _column_provenance(ran),
        **answer,
        # The SPEC is the product and the dock is the renderer, so what this
        # reports is what the run BUILT - never a claim about a card it cannot see.
        "chart_specs": sorted(result.charts),
        "notes": result.notes,
    }


def _column_provenance(p: Any) -> str:
    """What the narration says about where the two-zone column came from.

    Read off the run's OWN resolved rows, so the prose cannot drift from the
    machine-readable provenance the run carries.
    """
    row = p.row("porosity")
    if row is None:
        return "the two-zone aquifer column was not resolved."
    source = f" [{row.real_source}]" if row.real_source else f" [basis={row.basis}]"
    return f"two-zone aquifer moisture column {row.note}.{source}"


_DOC = dict(
    summary="BASEFLOW a shallow two-zone AQUIFER adds to a drainage node BETWEEN storms.",
    routing=(
        "THE tool for \"how much baseflow does groundwater add to this node between "
        "storms\", \"does the channel keep flowing after the runoff drains\", \"how "
        "does the water table reshape the node hydrograph\", \"subsurface return "
        "flow to the drainage network\", \"SWMM aquifer and groundwater objects\". "
        "Authors a real SWMM 5 [AQUIFERS] two-zone column + [GROUNDWATER] link on "
        "one pervious subcatchment and solves it headless (pyswmm) in TWO variants "
        "on the same two-storm forcing: baseflow active (A1 > 0) and a "
        "surface-runoff-only control (A1 = 0). SCHEMATIC deck - the product is the "
        "node-hydrograph CHART + typed scalars, never a map. The column is "
        "SoilGrids-derived at a real site or REFUSED (law 9)."
    ),
    not_for=(
        "regional groundwater budgets or drawdown (`modflow_*`); urban pipe or "
        "street flooding (`swmm_urban_flood`); sewer RDII "
        "(`swmm_rdii_rtk_unit_hydrograph`); hillslope water tables "
        "(`landlab_groundwater_water_table`)"
    ),
    params=PARAMS,
    controls=(
        ("input_mode",
         '"user_gated" presents the resolved param sheet - including the '
         "SoilGrids-derived two-zone column - for review/edit before the solves and "
         'WAITS; "auto" (session default) proceeds with every assumption labeled. '
         "Not a physical value."),
        ("restart_clean",
         "True discards the ledger a PREVIOUS FAILED attempt at this same "
         "invocation left behind and re-runs every step from the top. Default "
         "False resumes at the failed step."),
    ),
    returns=(
        "On success a dict of scalars: `baseflow_contribution_cfs` (the "
        "with-minus-without difference), `between_storms_baseflow_with_gw_cfs` and "
        "`_no_gw_cfs`, `peak_node_inflow_with_gw_cfs` (+ `_hr`) and "
        "`peak_node_inflow_no_gw_cfs`, `recession_tau_hr`, "
        "`storm2_recharge_bump_cfs`, `flow_routing_error_pct`, "
        "`aquifer_soil_column` + `aquifer_provenance`, and `curves`. Narrate those "
        "typed numbers. On failure a dict with `status=\"error\"` + `error_code`."
    ),
)

swmm_aquifer_baseflow_to_node.__doc__ = render_docstring(**_DOC)
swmm_aquifer_baseflow_to_node.routing_doc = render_docstring(**_DOC, view="routing")
