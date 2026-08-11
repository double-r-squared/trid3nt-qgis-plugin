# ADR 0218 - SWMM snowmelt (degree-day, rain-on-snow) + two-zone aquifer baseflow

Date: 2026-08-11
Status: accepted

## Context

The next module-coverage-board batch closes the three SWMM Hydrology CAND-M rows
that the current SWMM surface could not answer: two snowmelt rows (the Snow Pack
object) and one groundwater-baseflow row (the two-zone aquifer):

  * `swmm_snowmelt_degree_day` - snowpack accumulation + degree-day melt reshaping
    the winter runoff hydrograph vs treating all precipitation as rain (the
    rain-on-snow flood driver);
  * `swmm_snow_removal_plowing` - snow actively removed/plowed from part of a
    subcatchment (documentation-only source, no worked example);
  * `swmm_aquifer_baseflow_to_node` - the shallow two-zone aquifer contributing
    baseflow to a drainage node between storms.

Both sections were "Not surfaced": the DEM-derived quasi-2D `swmm_urban_flood`
mesh has no Snow Pack object (all precipitation is rain regardless of climate)
and no Aquifer/Groundwater subsurface pathway (surface runoff only).

## Decision

Two NEW registered chart-first validation-class templates, following the RDII
precedent (ADR 0190): a small deck is authored in-process and solved through the
native SWMM 5 engine (pyswmm, host-side, no worker image), the deliverable is
CHARTS + typed scalars (no georeferenced raster), and every narrated number is a
typed field the tool returns.

Registry 242 -> 244 (+2 CODED tools; both hand-written templates, zero
spec-synthesized). EXPECTED_TEMPLATES 81 -> 83.

### Why NEW chart-first tools, not knobs on `swmm_urban_flood`

`swmm_urban_flood` is the DEM-derived quasi-2D RASTER template (spatial flood
depth COG on a per-cell storage/conduit mesh). Snowmelt and groundwater-baseflow
are HYDROGRAPH-SHAPE questions (chart-first) that require new SWMM object
families the mesh builder does not emit ([SNOWPACKS] + [TEMPERATURE];
[AQUIFERS] + [GROUNDWATER]). The board itself graded both "M, not a pure knob
swap." The RDII template (ADR 0190) already set the precedent for a chart-first
single-subcatchment SWMM validation template driven by real forcing; these ride
the same pattern.

### Why `swmm_snow_removal_plowing` is a KNOB, not a second tool

Snow removal/plowing is the SAME question class as degree-day melt (snowpack ->
runoff timing; removal is a management lever on the same Snow Pack deck), so a
separate registered tool that differs only by whether the [SNOWPACKS] REMOVAL
block is active would be a place/case name, not a question-class name (violating
`names = question class, never place/case` + `prefer knobs where honest`). It is
folded as the `snow_removal` bool knob on `swmm_snowmelt_degree_day` (default
True, so the default showcase exercises it). Both board rows land; one tool.

## Tool 1 - `swmm_snowmelt_degree_day`

A native SWMM 5 [SNOWPACKS] deck (PLOWABLE / IMPERVIOUS / PERVIOUS surfaces) on
one subcatchment, forced by a [TEMPERATURE] TIMESERIES + SNOWMELT block (the
dividing temperature IS the rain/snow split) and a rainfall series. THREE variants
run on the SAME forcing: (1) snowmelt physics, (2) a rain-only climate-naive
control (dividing temperature dropped below all temperatures so every drop is
rain), (3) a plow-removal variant (REMOVAL transfers plowable snow out of the
watershed above a depth threshold). Degree-day method per the EPA SWMM Reference
Manual Vol. I snowmelt chapter.

Temperature source decision: AORC (the wired hourly precip fetcher, ADR 0203) is
precip-only (APCP), so it cannot drive the degree-day method. Of the temperature
options - `fetch_gridmet` (daily tmmn/tmmx) vs `fetch_asos_metar` (hourly tmpf) -
the degree-day method needs SUB-DAILY temperature to resolve the rain/snow split
and the diurnal melt cycle, so `fetch_asos_metar` `tmpf` is the primary source
(gridMET daily min/max is the documented fallback where no ASOS station is in the
AOI). LIVE on REAL KBUF (Buffalo NY) Jan 2024 ASOS: the deep cold spell (Jan
14-21) builds peak SWE 4.32 in, the late-Jan warm-up melts 1.21 in (partial - the
warm-up did not fully ripen the pack, honest real-forcing behavior), rain-on-snow
amplifies the runoff peak to 4.80 cfs vs rain-only 4.03 cfs (1.19x), and the
rain-only model fabricates a mid-winter runoff plateau the snowpack withheld.
Plowing cuts peak SWE 4.32 -> 1.41 in. Runoff continuity 0.00%.

Precip forcing is a representative event PHASED by the real temperature (snowfall
while sub-freezing, a rain burst during the warm-up); AORC hourly extraction over
the 14-day window is the documented upgrade path, not required for the mechanism.

## Tool 2 - `swmm_aquifer_baseflow_to_node`

A native SWMM 5 two-zone [AQUIFERS] column (unsaturated/saturated moisture
balance) + [GROUNDWATER] link (subcatchment -> aquifer -> node) with the SWMM
groundwater flow equation `q = A1*(Hgw-Hstar)^B1 - A2*(Hsw-Hstar)^B2 + A3*Hgw*Hsw`.
Storm infiltration recharges the aquifer; the risen water table discharges the A1
baseflow term to the node and recedes between storms. Solved with-GW vs a
surface-only control (A1=0) on a two-storm forcing. LIVE (100 ac, A1=0.002,
B1=1): groundwater sustains 0.930 cfs baseflow between storms vs 0.000 cfs
surface-only; the day-12 storm re-recharges the water table (+1.60 cfs); the
linear-reservoir tail recedes with tau ~964 h. Flow-routing continuity 0.00%.

The docstring notes the thematic tie to the Landlab GroundwaterDupuitPercolator
templates (ADR 0214) and the TELEMAC rain-on-grid recession tail (ADR 0213) -
the subsurface return-flow theme - WITHOUT overclaiming a shared solver (three
independent engines answering the same question class).

SWMM 5.2 detail (beyond the truncated swmm5.org source page): the [GROUNDWATER]
line requires the Egwt threshold column (11th field; `*` = use surface
elevation) or the engine raises ERROR 203; verified against the native engine.

## Consequence

* 3 board rows LANDED (2 registered tools). Rolling: +2 coded tools, +950 LOC
  tool code, +287 LOC proofs.
* Time-axis lesson baked into both solve helpers: pyswmm iterates at the variable
  wet/dry step, NOT a fixed count, so elapsed time comes from `sim.current_time`
  (a naive per-iteration counter mislabels the hydrograph time axis).
* Bookkeeping: corpus.yaml (10 queries each, model-free top-8 retrieval 10/10),
  categories.py (simulation_modeling), EXPECTED_TEMPLATES + the door-dissolution
  prose de-counted, tools/__init__ imports, seed_showcase_cases.py entries with
  physics assertions. Showcase cases 01KZS2ZEV6M0KW1GA5BXRT3B7Q (snowmelt, 2
  charts) + 01KZS31TC9ETWDQD3F0QM0C0D5 (aquifer, 1 chart), both persisted live.
* Proofs docs/proof/templates/swmm_snowmelt_degree_day_{swe_series,
  runoff_snowmelt_vs_rainonly}.png + swmm_aquifer_baseflow_to_node_{node_hydrograph,
  baseflow_recession}.png.
