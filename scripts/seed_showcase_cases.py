#!/usr/bin/env python3
"""seed_showcase_cases.py -- seed inspectable showcase Cases through the PRODUCT path.

The engine-template proofs have always lived in ``docs/proof/`` renders and ADR
smoke logs -- they never landed in a QGIS profile a human can open. This driver
closes that gap. It is a HEADLESS WS client that drives the live daemon exactly
the way the QGIS plugin does:

  1. auth-token (anonymous) -> auth-ack
  2. session-resume -> session-state
  3. case-command create {title="showcase: <template>"} -> case-open (new Case)
  4. dev-tool-invoke {name, args, case_id, raw_text="!run <tool>(...)"} -- the
     ADR 0114 ``!run`` direct-invocation path: the SAME registry closure, gates,
     layer materialization + Case persistence a model-issued call rides.
  5. collect the turn (auto-confirm the tool-payload-warning / solver-confirm /
     granularity gate; auto-approve a confirmation-request) until turn-complete,
     recording the tool-io status + the emitted ``session-state`` loaded_layers.

After every entry is seeded a SECOND connection reopens each Case (``case-command
select``) and confirms the persisted ``loaded_layers`` survive the reconnect --
the per-Case layer-durability norm, proven end-to-end.

Nothing here fabricates physics: every arg set is a PROVEN demo mined from the
ADR 0141-0174 smoke reports and the ``scripts/run_*_direct.py`` drivers (the
source of each is recorded in the entry ``note``). The reconstructed ``!run``
line each Case records is a line a human can paste into the composer verbatim.

OFFLINE proof (no daemon): ``--dry-run`` prints the planned invocation table and
round-trips every reconstructed ``!run`` line back through the PRODUCT parser
(``trid3nt.net.run_invocation.parse_run_invocation``), asserting the line parses
to the SAME (name, args) -- a hermetic contract check that reuses product code.

This driver NEVER deletes or mutates an existing Case; it only CREATES new
``showcase:``-prefixed Cases. It NEVER touches a template file.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
# Product parser lives in the plugin tree; reuse it for the offline round-trip.
sys.path.insert(0, str(REPO_ROOT / "qgis-plugin"))

WS_URL = "ws://127.0.0.1:8765/ws"
LOG_FILE = Path("/tmp/seed_showcase_cases.log")

# --------------------------------------------------------------------------- #
# Showcase entries -- ordered CHEAPEST-first so a slow tail never starves the
# fast, high-value Cases (quality over completeness). Every ``args`` set is a
# proven demo; ``note`` records its provenance; ``timeout_s`` time-boxes the
# solve.
# --------------------------------------------------------------------------- #
_BOULDER = [-105.37, 39.998, -105.33, 40.032]          # ADR 0141 landlab AOI
_WEST_BIJOU = [-104.33, 39.31, -104.28, 39.35]         # ADR 0184 chi-map escarpment
_PANOLA = [-84.18, 33.60, -84.14, 33.64]               # ADR 0214 groundwater AOI (Panola Mtn GA)
_SF_BAY = [-122.30, 37.70, -122.10, 37.90]             # ADR 0149 PSHA AOI
_EAST_BAY = [-122.30, 37.75, -122.10, 37.95]           # ADR 0164 Hayward AOI
_APALACHEE = [-85.55, 29.70, -85.40, 29.85]            # ADR 0147 SWAN shelf
_CHATTANOOGA = [-85.32, 35.03, -85.28, 35.07]          # ADR 0152 SFINCS pluvial
_MEXBEACH = [-85.5522, 29.6983, -85.3976, 29.8517]     # ADR 0176 SFINCS quadtree (Michael)
_CRESCENT = [-124.24, 41.73, -124.16, 41.78]           # ADR 0148 GeoClaw tsunami
_GALVESTON = [-95.2, 29.0, -94.2, 29.8]                # ADR 0168 surge shelf
_PLATTE = [40.905, -98.42]                              # ADR 0165/0166 well (lat, lon)
_GRAND_ISLAND_REACH = [40.857, -98.412]                 # ADR 0215 Wood River reach AOI (lat, lon)


@dataclass
class Showcase:
    tool: str
    args: dict
    note: str
    timeout_s: float = 300.0
    title_suffix: str = ""  # distinguishes multiple showcases of one template

    @property
    def case_title(self) -> str:
        # Humanize the tool name into a Case label the left rail reads well.
        base = "showcase: " + self.tool.replace("_", " ")
        return f"{base} ({self.title_suffix})" if self.title_suffix else base


SHOWCASE: list[Showcase] = [
    # -- fast closed-form / fixture validators (seconds) ---------------------
    Showcase("pelicun_closed_form_validation", {"check": "damage_state_probability"},
             "ADR 0146 pelicun validation wave (analytic DS-probability identity)", 180),
    Showcase("pelicun_hazus_seismic_dl_run", {},
             "ADR 0160 HAZUS seismic DL harness defaults (C1 Low-Rise Pre-Code)", 240),
    Showcase("elmfire_initial_attack_containment_probability",
             {"head_fire_intensity_kw_m": 2500.0, "attack_delay_min": 30.0},
             "ADR 0190 row 2 ELMFIRE Hirsch (1998) initial-attack probability of "
             "containment closed form: POC vs attack delay across head-fire "
             "intensities (no engine run; chart + scalars).", 120),
    Showcase("modflow_package_validation", {"case": "maw_crossaquifer"},
             "ADR 0153 MODFLOW package validation, MAW cross-aquifer fixture", 300),
    Showcase("modflow_package_validation", {"case": "sfr_stream_depletion"},
             "ADR 0167 MODFLOW SFR stream-depletion fixture", 300),
    Showcase("swmm_rdii_rtk_unit_hydrograph",
             {"R1": 0.12, "T1": 1.0, "K1": 2.0, "R2": 0.15, "T2": 3.0, "K2": 3.0,
              "R3": 0.09, "T3": 8.0, "K3": 3.0, "sewershed_area_ac": 10.0,
              "rainfall_series_in_per_hr": [0.0, 0.25, 0.5, 0.8, 0.4, 0.1, 0.0],
              "cross_check_swmm": True},
             "ADR 0190 row 4 SWMM RTK unit-hydrograph RDII on the EPA SWMM 5 Ch.7 "
             "Table 7-1 worked example (10-acre sewershed, R sum 0.36, published "
             "hourly rainfall): RDII at a node vs direct runoff, closed form "
             "validated against the native SWMM 5 [HYDROGRAPHS]/[RDII] engine "
             "(peak match <1%, ~4% of the published Fig 7-10 peak).", 180),
    Showcase("swmm_snowmelt_degree_day", {},
             "ADR 0218 SWMM Snow Pack degree-day melt (rain-on-snow) on real KBUF "
             "(Buffalo) Jan 2024 ASOS temperature: snowpack accumulates through the "
             "cold spell then melts with the warm rain -- snowmelt vs rain-only "
             "runoff visibly different, plus the snow-removal (plowing) knob. "
             "PHYSICS: peak SWE 4.32 in > 0, total melt 1.21 in > 0, rain-on-snow "
             "peak amplification 1.19x > 1, plowing cuts peak SWE to 1.41 in, "
             "runoff continuity 0.00%. Proof docs/proof/templates/"
             "swmm_snowmelt_degree_day_{swe_series,runoff_snowmelt_vs_rainonly}.png", 180),
    Showcase("swmm_aquifer_baseflow_to_node", {},
             "ADR 0218 SWMM two-zone [AQUIFERS]/[GROUNDWATER] baseflow-to-node: a "
             "shallow aquifer under a pervious subcatchment sustains baseflow to a "
             "drainage node between two storms; the day-12 storm re-recharges it. "
             "PHYSICS: between-storms baseflow 0.930 cfs > 0 with groundwater vs "
             "0.000 cfs surface-only, storm-2 recharge bump +1.60 cfs, recession "
             "tau ~964 h, flow-routing continuity 0.00%. Proof docs/proof/templates/"
             "swmm_aquifer_baseflow_to_node_{node_hydrograph,baseflow_recession}.png", 180),
    Showcase("swmm_subcatchment_runoff_comparison", {"compare": "infiltration_method"},
             "ADR 0151 SWMM mechanism comparison (infiltration method A/B)", 240),
    Showcase("swmm_wetwell_pump_control_comparison", {},
             "ADR 0151 SWMM wet-well pump-control comparison defaults", 240),
    # -- SCHISM shortlist batch 5 (ADR 0189) --------------------------------
    Showcase("schism_baroclinic_circulation",
             {"river_discharge_m3s": 800.0, "sim_days": 4.0},
             "ADR 0189/0191 SCHISM 3D baroclinic estuary: shoreline-clipped mesh (default "
             "Galveston Bay), 4-day spin-up so river+tide restructure the salinity field", 1200),
    Showcase("schism_coupled_waves",
             {"significant_wave_height_m": 4.0, "peak_period_s": 13.0,
              "mean_direction_deg": 70.0, "directional_spread": 25.0, "sim_hours": 0.5},
             "ADR 0189 SCHISM+WWM parametric JONSWAP boundary (Duck FRF, storm sea state)", 1200),
    # -- SCHISM PaHM storm surge (ADR 0217) ----------------------------------
    Showcase("schism_pahm_surge",
             {"bbox": [-95.4, 28.6, -94.2, 29.95], "sim_days": 1.5},
             "ADR 0219 SCHISM PaHM storm surge: published Hurricane Ike (2008) best track "
             "-> Holland-1980 parametric sflux winds -> barotropic surge on the greater "
             "Galveston domain (bay + Bolivar + island + open Gulf shelf) with REAL ETOPO "
             "screening bathymetry (peak surge COG + best-track overlay + gauge hydrograph). "
             "PHYSICS: peak surge 3.18 m > 0 (plausible vs observed Ike ~3-4 m at the coast), "
             "right-of-track lobe on Bolivar Peninsula + upper bay (NE mean 1.28 m >> SW "
             "offshore 0.46 m), gauge setdown -1.45 m then set-up +1.21 m at landfall. "
             "DOMAIN PROVENANCE (NATE ruling, 2026-08-11): synthetic_inputs.domain_provenance "
             "must read REAL with bathymetry traced to the fetched COG source -- fabricated "
             "bathymetry is never a silent fallback; a bathy-fetch failure raises "
             "SCHISM_BATHYMETRY_UNAVAILABLE instead (allow_synthetic_domain=True opts into the "
             "declared idealized-shelf mechanism-demo mode only)", 1800),
    # -- landlab diagnostics on the Boulder AOI (DEM fetch + solve) ----------
    Showcase("landlab_flow_accumulation", {"bbox": _BOULDER},
             "ADR 0141 landlab diagnostic wave, Boulder CO AOI", 360),
    Showcase("landlab_hand_wetness", {"bbox": _BOULDER},
             "ADR 0141 landlab HAND wetness, Boulder CO AOI", 360),
    Showcase("landlab_lake_mapping", {"bbox": _BOULDER},
             "ADR 0141/0145 landlab lake mapping, Boulder CO AOI", 360),
    Showcase("landlab_overland_flow_timeseries", {"bbox": _BOULDER},
             "ADR 0141 landlab overland-flow timeseries, Boulder CO AOI", 420),
    Showcase("landlab_channel_incision_steady_state", {"bbox": _BOULDER},
             "ADR 0184 landlab detachment-limited incision to steady state + "
             "slope-area V&V, Boulder CO foothills (fitted concavity ~0.485 vs "
             "analytical 0.5, K recovered within ~25%)", 480),
    Showcase("landlab_channel_steepness_chi_map", {"bbox": _WEST_BIJOU},
             "ADR 0184 landlab chi / channel-steepness (ksn) knickpoint diagnostic, "
             "West Bijou Creek escarpment CO", 420),
    Showcase("landlab_storm_sequence_generator", {"bbox": _BOULDER},
             "ADR 0184 landlab stochastic storm-sequence generator "
             "(PrecipitationDistribution, in-process), Boulder CO AOI", 180),
    Showcase("landlab_groundwater_water_table",
             {"bbox": _PANOLA, "gw_recharge_mm_yr": 250.0, "gw_aquifer_thickness_m": 15.0},
             "ADR 0214 landlab GroundwaterDupuitPercolator steady water table + seepage "
             "under recharge, Panola Mtn Research Watershed GA (mass-conservation V&V "
             "rel err ~1e-3; depth-to-water shallow along valleys, deep on ridges)", 600),
    Showcase("landlab_groundwater_storm_recession",
             {"bbox": _PANOLA, "gw_storm_aquifer_thickness_m": 6.0,
              "gw_storm_mean_depth_mm": 22.0, "gw_storm_total_days": 120.0},
             "ADR 0214 landlab GroundwaterDupuitPercolator storm-driven seepage/baseflow "
             "hydrograph + recession, Panola Mtn GA (43 storms, first-limb recession "
             "tau ~0.7 d, mass-conservation V&V rel err ~6e-3)", 420),
    # -- MODFLOW georeferenced wellhead/capture-zone -------------------------
    Showcase("modflow_wellhead_protection",
             {"aoi_latlon": _PLATTE, "well_location_latlon": _PLATTE,
              "travel_time_years": [5.0, 10.0, 25.0], "n_particles": 48},
             "ADR 0165/0166 Platte valley nr Grand Island NE, well 40.905/-98.42", 480),
    Showcase("modflow_wellhead_protection",
             {"aoi_latlon": _GRAND_ISLAND_REACH,
              "wells": [
                  {"lon": -98.412, "lat": 40.857, "rate_m3_day": 1600.0, "name": "GI-1"},
                  {"lon": -98.40, "lat": 40.862, "rate_m3_day": 1100.0, "name": "GI-2"},
                  {"lon": -98.425, "lat": 40.85, "rate_m3_day": 800.0, "name": "GI-3"},
              ],
              "transient": True, "sim_years": 10.0, "n_periods": 5,
              "use_nhd_river_boundaries": True,
              "travel_time_years": [1.0, 5.0, 10.0], "n_particles": 24},
             "ADR 0215 wellhead-reeval part 2: 3-well WELLFIELD nr Grand Island NE with "
             "soil-derived K + kriged/measured water-table IC + NHD RIV boundaries all "
             "active on a TRANSIENT solve (steady spin-up + 5x 2-yr storage periods); "
             "per-well capture allocation + 1/5/10-yr isochrones that evolve with the "
             "drawdown (EPA 440/6-87-010; USGS ex-prt-mp7-p03)", 600,
             title_suffix="multi-well transient NHD RIV"),
    # -- OpenQuake seismic ---------------------------------------------------
    Showcase("openquake_psha", {"bbox": _SF_BAY, "logic_tree": "gr_uncertainty"},
             "ADR 0149 PSHA logic-tree GR uncertainty, SF Bay AOI", 480),
    Showcase("openquake_scenario_gmf", {"bbox": _EAST_BAY, "magnitude": 6.9},
             "ADR 0164 scenario GMF, East Bay M6.9 (auto Hayward-fault trace)", 480),
    Showcase("openquake_secondary_perils", {"bbox": _EAST_BAY, "magnitude": 6.9},
             "ADR 0164 secondary perils (liquefaction/landslide), East Bay M6.9", 480),
    Showcase("openquake_disaggregation", {"bbox": _SF_BAY},
             "ADR 0182 hazard disaggregation, SF Bay AOI (dominant M-R-eps at 10%/50yr; "
             "local oq subprocess, ~30s)", 300),
    Showcase("openquake_event_based",
             {"bbox": _SF_BAY, "ses_per_logic_tree_path": 300},
             "ADR 0182 event-based/stochastic PSHA, SF Bay AOI (synthetic catalogue + "
             "classical convergence check; local oq subprocess)", 480),
    Showcase("openquake_psha", {"bbox": _SF_BAY, "vs30_compare": 260.0},
             "ADR 0182 Vs30 site-response A/B fold, SF Bay AOI (rock 760 vs soft 260 m/s "
             "hazard-curve overlay on the classical map path)", 480),
    Showcase("openquake_psha",
             {"bbox": [-112.02, 40.66, -111.80, 40.85], "nehrp_amp_class": "E"},
             "ADR 0220 discrete NEHRP site-class amplification A/B, Salt Lake City valley "
             "(soft basin soil vs Wasatch rock): unamplified 760 rock vs classes C/D/E via "
             "the ASCE 7-22 Fpga AmplificationFunction convolution; PoE monotone rock<C<D<E, "
             "class E ~2.3x rock at 0.556 g PGA; local oq subprocess", 480),
    # -- ELMFIRE wildfire sensitivity ----------------------------------------
    Showcase("elmfire_live_fuel_moisture_sensitivity", {},
             "ADR 0142 ELMFIRE live-fuel-moisture sensitivity defaults (GR2)", 420),
    Showcase("elmfire_transient_wind_schedule_spread", {},
             "ADR 0161 ELMFIRE transient wind-schedule (mid-run direction shift)", 420),
    # -- SWAN wave physics ---------------------------------------------------
    Showcase("swan_physics_sensitivity_sweep", {"bbox": _APALACHEE},
             "ADR 0147 SWAN friction sweep on the Apalachee Bay shelf", 480),
    # -- SFINCS pluvial ------------------------------------------------------
    Showcase("sfincs_flood",
             {"bbox": _CHATTANOOGA, "return_period_yr": 100, "duration_hr": 24,
              "compute_class": "small"},
             "ADR 0152 SFINCS pluvial flood, Chattanooga TN AOI", 600),
    Showcase("sfincs_flood",
             {"bbox": _MEXBEACH, "quadtree": True, "coastal": True,
              "return_period_yr": 100, "duration_hr": 12, "compute_class": "small",
              "quadtree_base_resolution_m": 400.0, "quadtree_coast_refine_level": 3,
              "quadtree_max_refine_level": 3},
             "ADR 0178 SFINCS COAST-FOLLOWING quadtree flood, Mexico Beach FL "
             "(Hurricane Michael lineage). Full PRODUCT path: sfincs_flood(quadtree="
             "True) stages the topobathy DEM + build_spec and dispatches the worker "
             "build+solve (solver=sfincs-quadtree); cht_sfincs authors the grid with "
             "the fine 50 m band hugging the z=0 shoreline (2:1-balanced 400->50 m, "
             "~12k cells), the native sfincs_map.nc mesh carries EPSG:32616.", 900),
    # -- GeoClaw coastal -----------------------------------------------------
    Showcase("geoclaw_inundation",
             {"bbox": _CRESCENT, "scenario": "tsunami", "sim_duration_s": 1800,
              "amr_levels": 2, "output_frames": 6, "fgout_frames": 12},
             "ADR 0187 GeoClaw tsunami inundation with fgout SMOOTH animation, "
             "Crescent City CA: fgout_frames=12 -> 12 evenly-spaced uniform-grid "
             "frames become the scrubber animation (fort.q peak retained)", 600),
    Showcase("geoclaw_thacker_validation",
             {"bowl_a_m": 1.0, "bowl_h0_m": 0.1, "bowl_eta_amp": 0.5,
              "n_periods": 2.5, "amr_levels": 3, "base_cells": 60},
             "ADR 0187 GeoClaw Thacker paraboloid-basin V&V: frictionless closed-wall "
             "bowl vs the 1981 closed form (period ~1.9%, amplitude ~0.1%, mass drift "
             "~5%). Synthetic non-geographic solver verification (charts/scalars).", 300),
    Showcase("geoclaw_storm_surge",
             {"bbox": _GALVESTON, "sim_duration_s": 54000, "output_frames": 12,
              "amr_levels": 2},
             "ADR 0168 GeoClaw storm surge, synthetic demo track on Galveston shelf", 600),
    # -- SWAN nonstationary storm evolution (ADR 0190 row 3) -----------------
    Showcase("swan_wave_field",
             {"bbox": _APALACHEE, "mode": "nonstationary",
              "boundary_hs_m": 1.0, "boundary_side": "S",
              "storm_peak_hs_m": 6.0, "storm_peak_hour": 18.0,
              "sim_duration_s": 129600, "time_step_s": 600, "output_frames": 18},
             "ADR 0190 row 3 SWAN NONSTATIONARY time-marching storm evolution: a "
             "time-varying offshore boundary builds to Hs=6 m at hour 18 then "
             "decays over 36 h (Apalachee Bay FL shelf), producing time-stamped "
             "nearshore Hs frames for the scrubber + a peak-Hs field (native "
             "solver).", 1200, title_suffix="nonstationary storm"),
    # -- TELEMAC water quality / transport -----------------------------------
    Showcase("telemac_do_sag", {"location": "Sacramento River near Colusa, California"},
             "ADR 0169 TELEMAC-WAQTEL DO-sag, real NHDPlus reach nr Colusa CA", 600),
    Showcase("generate_mesh",
             {"location": "Coweeta Creek, North Carolina",
              "pour_point": (-83.40402, 35.05746),
              "min_edge_length_m": 40.0, "max_edge_length_m": 400.0},
             "ADR 0200 standalone mesh builder: watershed mode (pour_point) on the "
             "Coweeta Creek NC catchment. Delineate -> distance-to-river-refined "
             "OceanMesh2D triangulation (GPL-isolated mesh:latest) -> UTM SELAFIN "
             "+ MDAL .2dm display layer + a durable mesh artifact a model template "
             "discovers via the precondition gate. Emits the mesh wireframe as a "
             "layer_type=mesh row (crs_authid=EPSG:32617).",
             1800),
    Showcase("generate_mesh",
             {"mesh_mode": "hecras",
              "bbox": [-83.47, 35.02, -83.36, 35.10],
              "pour_point": (-83.40402, 35.05746),
              "min_edge_length_m": 22.0, "max_edge_length_m": 90.0},
             "ADR 0211 standalone HEC-RAS rain-on-grid mesh: mesh_mode=hecras on the "
             "Coweeta Creek NC catchment. Delineate -> graded Poisson-disk seeds "
             "(22 m channel / 90 m hillslope) + main-stem breaklines -> realized + "
             "validated through the in-container meshprobe (<= 8 sides/cell) -> the "
             "realized Voronoi cell wireframe as a layer_type=vector layer + a PORTABLE "
             "authoring bundle a later hecras_flood_2d rain-on-grid run consumes via "
             "the precondition gate (TryCreateMesh deterministic on the seeds, so the "
             "inspected mesh IS the solved mesh).",
             1800, title_suffix="hecras rain-on-grid"),
    Showcase("telemac_rain_on_grid",
             {"location": "Otto, North Carolina",
              "pour_point": (-83.40402, 35.05746),
              "antecedent_moisture": "normal", "design_storm_mm_per_hr": 25.0,
              "storm_duration_hr": 6.0},
             "ADR 0196 TELEMAC rain-on-grid: an SCS-CN design storm on the "
             "delineated Coweeta Creek NC catchment (steep gauged US replication "
             "site). NLCD-distributed CN + Manning; native RAINFALL-RUNOFF MODEL=1 "
             "with the antecedent-moisture (dry/normal/wet) knob as the dominant "
             "infiltration lever; outlet hydrograph + peak flood-depth COG. Live "
             "V&V: 4854-node catchment, AMC II peak 45.5 vs AMC I (dry) 6.1 m3/s.",
             1800),
    Showcase("telemac_river_dye",
             {"location": "Eel River near Scotia, California",
              "wind_speed_mps": 18.0, "wind_direction_deg": 270.0},
             "ADR 0154 TELEMAC river dye + wind forcing, Eel River nr Scotia CA", 600),
    Showcase("telemac_river_dye",
             {"location": "Eel River near Scotia, California",
              "rainfall_mm_per_day": 150.0, "sim_duration_s": 5400},
             "ADR 0190 row 1 TELEMAC distributed on-mesh rainfall forcing: a real "
             "atmospheric-river daily rate (150 mm/day) applied as a native RAIN OR "
             "EVAPORATION source term at every wet node raises inundation depth "
             "INDEPENDENT of the inflow hydrograph (Eel River nr Scotia CA). gridMET "
             "real-storm auto-source proven live at 158 mm/day for Hurricane Harvey.",
             900, title_suffix="rainfall"),
    Showcase("telemac_river_dye",
             {"location": "Snake River near Twin Falls, Idaho",
              "substance": "scour", "erodible_bed": True,
              "morphological_factor": 5.0, "grain_size_um": 300.0,
              "bed_thickness_m": 5.0, "sim_duration_s": 900},
             "ADR 0216 TELEMAC GAIA v2 ERODIBLE-BED scour morphodynamics: a real "
             "erodible bed + active bedload (Meyer-Peter-Mueller) under flow, so the "
             "bed SCOURS where it steepens and re-deposits where it slackens (Snake "
             "R. nr Twin Falls ID). Signed CUMUL BED EVOL map (scour<0<deposition) + "
             "max_scour_mm. In-image smoke: scour to -0.82 m, mass balance closes. "
             "MORPHOLOGICAL FACTOR is the demo speed-up lever; grain d50 a demo "
             "default (no bed-composition fetcher).",
             1200, title_suffix="erodible-bed-scour"),
    # -- HEC-RAS (bundled Muncie deck; cheap 1D/2D) --------------------------
    Showcase("hecras_flood_2d",
             {"bbox": [-98.115, 29.975, -98.083, 30.000], "target_peak_cfs": 15000,
              "resolution_m": 30, "equation_set": "full_swe", "computation_interval": "1MIN"},
             "ADR 0188 HEC-RAS 2D fresh-AOI flood on the Blanco River canyon nr "
             "Wimberley TX (329 ft relief), exercising the equation_set (full SWE-ELM) "
             "+ computation_interval (1MIN stability step) knobs. DW vs SWE agree on "
             "the peak footprint, separating only at momentum-dominated channel cells; "
             "the coarse-step overshoot converges as the step tightens.", 900),
    Showcase("hecras_flood_2d",
             {"bbox": [-83.47, 35.02, -83.36, 35.10], "design_storm_mm_per_hr": 25.0,
              "storm_duration_hr": 6.0, "resolution_m": 60},
             "ADR 0209 HEC-RAS 2025 RAIN-ON-GRID: a 25 mm/hr x 6 h design storm over "
             "Coweeta Creek NC, authored + solved on the managed CPU engine (the 6.6 "
             "Fortran path could not; no Windows). Rain-only (the 2025 beta has no "
             "infiltration layer) -> water self-organizes into the dendritic drainage, "
             "peak outlet ~195 m3/s.", 1800, title_suffix="rain-on-grid"),
    Showcase("hecras_riverine_flood", {},
             "ADR 0170/0172 HEC-RAS riverine flood, shipped Muncie deck", 480),
    Showcase("hecras_levee_breach", {"breach_enabled": True},
             "ADR 0171/0172 HEC-RAS levee breach, shipped Muncie deck", 480),
]


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(str(LOG_FILE)), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("seed_showcase")


# --------------------------------------------------------------------------- #
# !run line reconstruction (pythonic kwargs form the plugin parser accepts)
# --------------------------------------------------------------------------- #
def _py_literal(v: Any) -> str:
    """Render ``v`` as a Python literal the composer parser can ``literal_eval``."""
    if isinstance(v, bool):
        return "True" if v else "False"
    if v is None:
        return "None"
    if isinstance(v, str):
        return repr(v)  # single-quoted; ast.literal_eval-safe
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_py_literal(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{_py_literal(k)}: {_py_literal(val)}" for k, val in v.items()) + "}"
    raise TypeError(f"unsupported literal: {v!r}")


def run_line(tool: str, args: dict) -> str:
    if not args:
        return f"!run {tool}()"
    inner = ", ".join(f"{k}={_py_literal(v)}" for k, v in args.items())
    return f"!run {tool}({inner})"


# --------------------------------------------------------------------------- #
# WS envelope helper
# --------------------------------------------------------------------------- #
from trid3nt_contracts import new_ulid  # noqa: E402


def mk(type_: str, session_id: str, payload: dict, case_id: str | None = None) -> str:
    return json.dumps({
        "type": type_,
        "id": new_ulid(),
        "ts": "2026-08-07T00:00:00Z",
        "session_id": session_id,
        "case_id": case_id,
        "payload": payload,
    })


# --------------------------------------------------------------------------- #
# Per-entry result record
# --------------------------------------------------------------------------- #
@dataclass
class Result:
    tool: str
    title: str
    args: dict
    note: str
    run_line: str
    case_id: str | None = None
    status: str = "not_run"          # ok | error | blocked | timeout | no_result
    detail: str = ""
    layers: list[dict] = field(default_factory=list)
    tool_status: str | None = None   # status parsed from the function_response
    charts: int = 0                  # chart-dock emissions (validation/report output)
    persisted_layers: int | None = None  # from the reconnect verify

    def as_row(self) -> dict:
        return {
            "case": self.title,
            "case_id": self.case_id,
            "tool": self.tool,
            "args": self.args,
            "status": self.status,
            "detail": self.detail,
            "layers": [{"name": l.get("name"), "type": l.get("layer_type"),
                        "uri": l.get("uri")} for l in self.layers],
            "charts": self.charts,
            "persisted_layers": self.persisted_layers,
            "run_line": self.run_line,
        }


# --------------------------------------------------------------------------- #
# WS client core
# --------------------------------------------------------------------------- #
async def _handshake(ws, session_id: str) -> None:
    await ws.send(mk("auth-token", session_id, {"token": "", "anonymous_user_id": None}))
    ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
    assert ack["type"] == "auth-ack", f"expected auth-ack, got {ack['type']}"
    await ws.send(mk("session-resume", session_id, {"case_id": None}))
    while True:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
        if msg["type"] == "session-state":
            return


async def _create_case(ws, session_id: str, title: str) -> str:
    await ws.send(mk("case-command", session_id,
                     {"command": "create", "args": {"title": title}}))
    while True:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
        if msg["type"] == "case-open":
            ss = msg["payload"].get("session_state")
            if ss:
                return ss["case"]["case_id"]


async def _auto_confirm_warning(ws, session_id: str, msg: dict) -> None:
    import re

    payload = msg["payload"]
    wid = payload.get("warning_id")
    options = payload.get("options") or ["proceed"]
    # Resolution doctrine (ADR 0224): a NATIVE-default heavy fetch (e.g. the surge
    # big-domain showcase) can estimate over the hard cap, so the gate REMOVES
    # ``proceed`` and offers only cancel/narrow_scope with a concrete coarsening
    # suggestion in the recommendation ("coarsen (resolution_m=199)"). An automated
    # seed cannot proceed native there; it accepts the offered coarsening so the demo
    # stays green while exercising the real gate. When ``proceed`` is offered (every
    # other showcase, and any under-cap surge run) the seed proceeds unchanged.
    if "proceed" in options:
        log.info("    auto-confirm tool-payload-warning warning_id=%s -> proceed", wid)
        await ws.send(mk("tool-payload-confirmation", session_id,
                         {"warning_id": wid, "decision": "proceed", "revised_args": None}))
        return
    m = re.search(r"resolution_m=(\d+(?:\.\d+)?)", payload.get("recommendation", ""))
    if m and "narrow_scope" in options:
        res = float(m.group(1))
        # narrow_scope REPLACES params with revised_args (server), so send the FULL
        # original args merged with the coarsening -- not just the delta.
        revised = dict(payload.get("tool_args") or {})
        revised["resolution_m"] = res
        log.info("    auto-confirm tool-payload-warning warning_id=%s -> narrow_scope "
                 "(proceed removed over hard cap; coarsen resolution_m=%s)", wid, res)
        await ws.send(mk("tool-payload-confirmation", session_id,
                         {"warning_id": wid, "decision": "narrow_scope",
                          "revised_args": revised}))
        return
    log.info("    auto-confirm tool-payload-warning warning_id=%s -> cancel "
             "(proceed removed, no coarsening suggestion parseable)", wid)
    await ws.send(mk("tool-payload-confirmation", session_id,
                     {"warning_id": wid, "decision": "cancel", "revised_args": None}))


async def _auto_approve_request(ws, session_id: str, msg: dict) -> None:
    rid = msg["payload"].get("request_id")
    log.info("    auto-approve confirmation-request request_id=%s -> approved", rid)
    await ws.send(mk("confirm-response", session_id,
                     {"request_id": rid, "approved": True}))


_BLOCKING = {
    "spatial-input-request", "disambiguation-request",
    "clarification-request", "recovery-choice",
}


async def _seed_one(ws, session_id: str, sc: Showcase) -> Result:
    res = Result(tool=sc.tool, title=sc.case_title, args=sc.args, note=sc.note,
                 run_line=run_line(sc.tool, sc.args))
    res.case_id = await _create_case(ws, session_id, sc.case_title)
    log.info("[%s] case_id=%s  %s", sc.tool, res.case_id, res.run_line)

    await ws.send(mk("dev-tool-invoke", session_id,
                     {"name": sc.tool, "args": sc.args,
                      "case_id": res.case_id, "raw_text": res.run_line},
                     case_id=res.case_id))

    deadline = time.monotonic() + sc.timeout_s
    activity = False
    tool_io_seen = False
    tool_io_error = False
    charts = 0
    latest_layers: list[dict] = []
    while time.monotonic() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=min(deadline - time.monotonic(), 45))
        except asyncio.TimeoutError:
            continue
        msg = json.loads(raw)
        mtype = msg["type"]
        if mtype == "tool-payload-warning":
            activity = True
            await _auto_confirm_warning(ws, session_id, msg)
        elif mtype == "confirmation-request":
            activity = True
            await _auto_approve_request(ws, session_id, msg)
        elif mtype in _BLOCKING:
            res.status = "blocked"
            res.detail = f"gate needs interactive input ({mtype}); skipped headlessly"
            log.warning("    BLOCKED by %s", mtype)
            return res
        elif mtype in ("pipeline-state", "tool-call-start", "tool-call-progress"):
            activity = True
        elif mtype == "tool-io":
            activity = True
            tool_io_seen = True
            res.tool_status = _parse_tool_status(msg["payload"])
            if msg["payload"].get("is_error"):
                tool_io_error = True
                res.detail = _first_line(msg["payload"].get("function_response", ""))
        elif mtype in ("chart-emission", "chart"):
            activity = True
            charts += 1
        elif mtype in ("tool-call-failed",):
            activity = True
            res.detail = _first_line(json.dumps(msg["payload"]))
        elif mtype == "session-state":
            ll = msg["payload"].get("loaded_layers") or []
            if ll:
                latest_layers = ll
        elif mtype == "error":
            res.status = "error"
            res.detail = f"{msg['payload'].get('error_code')}: {msg['payload'].get('message')}"
            log.error("    ERROR %s", res.detail)
            return res
        elif mtype == "turn-complete":
            if activity:
                break
    else:
        res.status = "timeout"
        res.detail = f"no turn-complete within {sc.timeout_s:.0f}s"
        res.layers = latest_layers
        log.warning("    TIMEOUT")
        return res

    res.layers = latest_layers
    res.charts = charts
    # Honesty floor: is_error on the tool-io is authoritative. Success requires a
    # non-error dispatch that actually EMITTED something inspectable -- a map
    # layer (LayerPanel) or a chart-dock chart (validation/report output) or an
    # explicit status=ok in the function_response.
    if tool_io_error or res.tool_status == "error":
        res.status = "error"
        res.detail = res.detail or "tool-io reported is_error/status=error"
    elif latest_layers:
        res.status = "ok"
        res.detail = f"{len(latest_layers)} layer(s)" + (f" + {charts} chart(s)" if charts else "")
    elif charts or res.tool_status == "ok":
        res.status = "ok"
        res.detail = f"{charts} chart(s), no map layer (validation/report output)"
    elif tool_io_seen:
        res.status = "no_result"
        res.detail = res.detail or "dispatch not error but emitted no layer/chart"
    else:
        res.status = "no_result"
        res.detail = res.detail or "turn completed with no tool dispatch"
    log.info("    -> %s :: %s", res.status.upper(), res.detail)
    return res


def _parse_tool_status(payload: dict) -> str | None:
    raw = payload.get("function_response") or ""
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return "error" if payload.get("is_error") else None
    if isinstance(obj, dict):
        st = obj.get("status")
        if isinstance(st, str):
            return st
    return "error" if payload.get("is_error") else None


def _first_line(s: str, n: int = 240) -> str:
    s = (s or "").strip().replace("\n", " ")
    return s[:n]


async def _verify_persistence(session_id: str, results: list[Result]) -> None:
    """Reopen every seeded Case on a FRESH connection and confirm its layers
    survived the reconnect (per-Case layer-durability norm)."""
    import websockets.asyncio.client as wsc
    async with wsc.connect(WS_URL) as ws:
        await _handshake(ws, session_id)
        for res in results:
            if not res.case_id:
                continue
            await ws.send(mk("case-command", session_id,
                             {"command": "select", "case_id": res.case_id},
                             case_id=res.case_id))
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                except asyncio.TimeoutError:
                    break
                if msg["type"] == "case-open":
                    ss = msg["payload"].get("session_state")
                    if ss and ss["case"]["case_id"] == res.case_id:
                        res.persisted_layers = len(ss.get("loaded_layers") or [])
                        log.info("    reconnect: case %s -> %d persisted layer(s)",
                                 res.case_id, res.persisted_layers)
                        break


async def run_all(only: str | None) -> list[Result]:
    import websockets.asyncio.client as wsc
    entries = [s for s in SHOWCASE if (only is None or only in s.tool or only in s.title_suffix)]
    session_id = new_ulid()
    log.info("=== showcase seeding: %d entries, session=%s ===", len(entries), session_id)
    results: list[Result] = []
    async with wsc.connect(WS_URL, max_size=64 * 1024 * 1024) as ws:
        await _handshake(ws, session_id)
        for sc in entries:
            try:
                results.append(await _seed_one(ws, session_id, sc))
            except Exception as exc:  # noqa: BLE001 -- one bad entry never stops the run
                log.exception("[%s] driver exception", sc.tool)
                r = Result(tool=sc.tool, title=sc.case_title, args=sc.args, note=sc.note,
                           run_line=run_line(sc.tool, sc.args))
                r.status = "error"
                r.detail = f"driver exception: {exc}"
                results.append(r)
    # Second connection: prove durability across a reconnect.
    log.info("=== reconnect: verifying per-Case layer durability ===")
    try:
        await _verify_persistence(session_id, results)
    except Exception:  # noqa: BLE001
        log.exception("persistence verify failed")
    return results


# --------------------------------------------------------------------------- #
# Offline dry-run: plan + product-parser round-trip
# --------------------------------------------------------------------------- #
def dry_run(only: str | None) -> int:
    from trid3nt.net.run_invocation import parse_run_invocation
    entries = [s for s in SHOWCASE if (only is None or only in s.tool or only in s.title_suffix)]
    print(f"planned showcase Cases: {len(entries)}\n")
    failures = 0
    for sc in entries:
        line = run_line(sc.tool, sc.args)
        parsed = parse_run_invocation(line)
        ok = (parsed is not None and parsed.name == sc.tool and parsed.args == sc.args)
        mark = "OK " if ok else "FAIL"
        if not ok:
            failures += 1
        print(f"[{mark}] {sc.case_title}")
        print(f"        tool  = {sc.tool}")
        print(f"        args  = {json.dumps(sc.args)}")
        print(f"        note  = {sc.note}")
        print(f"        !run  = {line}")
        if not ok:
            print(f"        PARSE = {parsed}")
        print()
    print(f"round-trip: {len(entries) - failures}/{len(entries)} !run lines parse "
          f"back to the exact (name, args) via the product parser")
    return 1 if failures else 0


def _print_summary(results: list[Result]) -> None:
    rows = [r.as_row() for r in results]
    print("\n" + "=" * 78)
    print("SHOWCASE SEEDING SUMMARY")
    print("=" * 78)
    by_status: dict[str, int] = {}
    for r in results:
        by_status[r.status] = by_status.get(r.status, 0) + 1
    print("counts: " + ", ".join(f"{k}={v}" for k, v in sorted(by_status.items())))
    print()
    for r in results:
        pl = "" if r.persisted_layers is None else f" persisted={r.persisted_layers}"
        print(f"[{r.status.upper():9}] {r.title}  ({len(r.layers)} layers, {r.charts} charts{pl})")
        print(f"            {r.run_line}")
        if r.detail:
            print(f"            {r.detail}")
    out = Path("/tmp/seed_showcase_results.json")
    out.write_text(json.dumps(rows, indent=2))
    print(f"\nfull JSON: {out}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="offline: print the plan + round-trip !run lines through the product parser")
    ap.add_argument("--only", default=None,
                    help="substring filter on the tool name (e.g. 'landlab')")
    args = ap.parse_args()
    if args.dry_run:
        return dry_run(args.only)
    results = asyncio.run(run_all(args.only))
    _print_summary(results)
    ok = sum(1 for r in results if r.status == "ok")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
