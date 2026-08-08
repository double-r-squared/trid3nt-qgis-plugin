"""Door dissolution (ADR 0094): the engine-door concierge tools are DELETED and
each engine template stands alone in the retrieval pool.

Two guarantees are pinned here:

1. CALLABILITY -- every engine template is registered (tier=template,
   source_class=workflow_dispatch) and directly callable; NO tier=door tool
   survives; the 10 deleted door names are gone with no alias.
2. RETRIEVAL -- with templates walked into the index, EACH engine template is
   surfaced in the model-free ``retrieve_visible_tools(query, None, 8)`` top-8
   by at least one of its natural-language corpus queries (the retrieval-corpus-
   first rule -- the doors can die only because discovery works without them).

ASCII only.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import trid3nt_server.main as _main
from trid3nt_server.agent.tools.search.search_tools import search_tools as dd
from trid3nt_server.agent.tools.search.tool_retrieval import retrieve_visible_tools

# The registered engine templates (12 engines; MODFLOW ships 11, HEC-RAS #11 ships
# 2, SCHISM #12 ships 2 -- tidal_hydro + coupled_waves).
EXPECTED_TEMPLATES = {
    "sfincs_flood",
    "sfincs_advanced_numerical_physics_knobs",  # S-tier wave 1: SFINCS numerical solver-settings knob template
    "swmm_urban_flood",
    "swmm_network_import",  # ADR 0124 SWMM network family #1: import a real municipal storm-drain GIS network
    "swmm_dual_drainage_coupling",  # ADR 0124 SWMM network family #2: overland mesh <-> imported pipe coupling
    "swmm_lid_raingarden_wq",  # ADR 0128 published-deck runner: cited rain-garden LID + WQ example
    "swmm_wwtp_detention_ponds",  # ADR 0128 published-deck runner: cited detention-pond storage-routing example
    "swmm_pump_pid_rtc",  # ADR 0128 published-deck runner: cited PID pump real-time-control example
    "swmm_subcatchment_runoff_comparison",  # ADR 0151 SWMM CAND-S: infiltration-method + pre/post-development runoff knob
    "swmm_node_hydraulics_comparison",  # ADR 0151 SWMM CAND-S: outlet-structure family / flow diversion / surcharge-ponding knob
    "swmm_wetwell_pump_control_comparison",  # ADR 0151 SWMM CAND-S: wet-well pump curve + duty/standby + multi-condition RTC
    "swmm_lid_performance_comparison",  # ADR 0151 SWMM CAND-S: green roof / rain barrel vs rooftop disconnect / vegetative swale
    "swmm_wq_buildup_washoff_comparison",  # ADR 0151 SWMM CAND-S: curb-length vs area buildup + EMC vs exp washoff
    "telemac_river_dye",
    "telemac_do_sag",
    "hecras_riverine_flood",  # engine #11 (ADR 0109; renamed ADR 0120): HEC-RAS riverine-flood template (v1 geometry: Muncie)
    "hecras_levee_breach",  # engine #11 second archetype (ADR 0125): HEC-RAS levee-breach template (v1 geometry: Muncie leveed floodplain)
    "hecras_flood_2d",  # engine #11 third archetype (ADR 0140): HEC-RAS fresh-AOI 2D flood (headless-authored geometry from a fetched DEM)
    "schism_tidal_hydro",  # engine #12 (ADR 0118): SCHISM barotropic tidal template
    "schism_coupled_waves",  # engine #12 second archetype (ADR 0131): SCHISM+WWM+GOTM coupled-wave template (Duck FRF)
    "schism_transport_validation",  # SCHISM CAND-S (ADR 0156): transport-scheme numerical-mixing + mass-conservation V&V (Test_HeatConsv / Test_GEN_MassConsv)
    "swan_wave_field",
    "swan_physics_sensitivity_sweep",  # SWAN CAND-S: physics-scheme A-vs-B sensitivity sweep
    "swan_stationary_snapshot_batch",  # SWAN CAND-S: batch of stationary snapshots sampling a storm event (MODE)
    "geoclaw_inundation",
    "elmfire_fire_spread",
    "elmfire_length_to_width_ceiling_sensitivity",  # ELMFIRE CAND-S: MAX_LOW length:width ceiling sweep
    "elmfire_wind_fluctuation_randomization",  # ELMFIRE CAND-S: deterministic-vs-randomized wind-fluctuation ensemble
    "elmfire_live_fuel_moisture_sensitivity",  # ELMFIRE CAND-S: live herbaceous moisture override sweep
    "elmfire_transient_wind_schedule_spread",  # ELMFIRE transient-weather front (ADR 0161): mid-run wind-shift redirection vs constant wind
    "elmfire_dead_fuel_moisture_interpolation_frequency_control",  # ELMFIRE transient-weather front (ADR 0161): DT_INTERPOLATE_M1/M10/M100 accuracy-vs-cost sweep
    "elmfire_crown_fire_initiation_threshold_sweep",  # ELMFIRE crown-fire front (ADR 0161): CRITICAL_CANOPY_COVER initiation + Cruz-rate ceiling folded sweep
    "landlab_susceptibility",
    "landlab_flow_accumulation",  # ADR 0122 hazard-easy-four #1: Landlab flow-accumulation / drainage-area + channel-network template
    "landlab_green_ampt_overland_flow",  # ADR 0123 hazard-easy-four continuation #1: Green-Ampt infiltration/runoff partition
    "landlab_landslide_storm_ensemble",  # Landlab CAND-S: storm/recharge-ensemble landslide susceptibility sweep
    "landlab_overland_flow_timeseries",  # Landlab CAND-S: time-stepped overland-flow depth animation
    "landlab_dem_conditioning",  # Landlab CAND-S: DEM pit-fill conditioning depth
    "landlab_lake_mapping",  # Landlab CAND-S: lake extent + depth mapping
    "landlab_hacks_law_scaling",  # Landlab CAND-S: Hack's-law basin length-area scaling diagnostic
    "landlab_hand_wetness",  # Landlab CAND-S: Height Above Nearest Drainage wetness proxy
    "landlab_channel_incision_steady_state",  # ADR 0184 Landlab shortlist: detachment-limited stream-power incision to steady state + slope-area V&V
    "landlab_channel_steepness_chi_map",  # ADR 0184 Landlab shortlist: ChiFinder + SteepnessFinder chi/ksn knickpoint diagnostic
    "landlab_storm_sequence_generator",  # ADR 0184 Landlab shortlist: PrecipitationDistribution stochastic storm-sequence forcing generator
    "elmfire_verification_elliptical_replication",  # ADR 0123 continuation #2: constant-wind elliptical-spread verification
    "geoclaw_tsunami_gauge_timeseries",  # ADR 0123 continuation #3: coastal gauge water-level time series
    "geoclaw_amr_refinement_regions",  # GeoClaw CAND-S: explicit lat/lon/time AMR region control
    "geoclaw_regional_manning_friction",  # GeoClaw CAND-S: spatially-varying banded Manning friction
    "geoclaw_storm_surge",  # ADR 0168: parametric-Holland tropical-cyclone storm-surge front (wind+pressure from a storm track, drag-law knob)
    "openquake_psha",
    "openquake_scenario_gmf",  # OpenQuake scenario GMF (ADR 0164): single-rupture correlated ground-motion field, mean + realization spread
    "openquake_secondary_perils",  # OpenQuake secondary-perils (ADR 0164): scenario-GMF-driven liquefaction (Zhu 2015) + Newmark landslide screening
    "openquake_disaggregation",  # OpenQuake disaggregation (ADR 0182): which magnitude-distance-epsilon scenario dominates a site's hazard (M-R contribution matrix)
    "openquake_event_based",  # OpenQuake event-based/stochastic PSHA (ADR 0182): synthetic catalogue + event-based hazard map + classical convergence check
    "pelicun_damage_assessment",
    "pelicun_closed_form_validation",  # pelicun CAND-S: Monte-Carlo vs analytic closed form (damage-state prob + loss-function identity)
    "pelicun_mixed_fragility_loss_assessment",  # pelicun CAND-S: mixed fragility+loss-function assessment + EDP correlation spread
    "pelicun_replacement_threshold_override_sweep",  # pelicun CAND-S: RID-triggered irreparable/replacement threshold sweep (RID from PID)
    "pelicun_flood_foundation_depth_damage_sweep",  # pelicun CAND-S: HAZUS flood depth-damage sensitivity to foundation type
    "pelicun_hazus_seismic_dl_run",  # pelicun DL_calculation harness (ADR 0160): auto-populated HAZUS EQ building damage+loss run
    "pelicun_hazus_eq_version_comparison",  # pelicun DL_calculation harness front (ADR 0160): HAZUS EQ v5.1-vs-v6.1 dataset comparison
    "modflow_asr",
    "modflow_capture_zone",
    "modflow_contaminant_plume",
    "modflow_managed_recharge",
    "modflow_mine_dewatering",
    "modflow_regional_water_budget",
    "modflow_river_seepage",
    "modflow_saltwater_intrusion",
    "modflow_sustainable_yield",
    "modflow_wellhead_protection",
    "modflow_wetland_hydroperiod",
    "modflow_package_validation",  # ADR 0153 MODFLOW CAND-S: GWF-NPF Newton (Zaidel) / GWF-MAW (Sokol) / GWF-HFB package V&V benchmarks
}

# The 10 deleted engine-door concierge tools.
DELETED_DOORS = {
    "run_sfincs",
    "run_swmm",
    "run_modflow",
    "run_telemac",
    "run_swan",
    "run_elmfire",
    "run_geoclaw",
    "run_landlab",
    "run_openquake",
    "run_pelicun",
}


def _full_registry():
    _main._import_tools_registry()
    from trid3nt_server.agent.tools import TOOL_REGISTRY

    return TOOL_REGISTRY


# ---------------------------------------------------------------------------
# (1) Callability without doors.
# ---------------------------------------------------------------------------
def test_no_engine_door_survives():
    """No tool carries tier=door, and none of the 10 door names is registered."""
    reg = _full_registry()
    doors = [n for n, e in reg.items() if getattr(e.metadata, "tier", "general") == "door"]
    assert doors == [], f"engine doors must be dissolved; still registered: {doors}"
    still = DELETED_DOORS & set(reg)
    assert still == set(), f"deleted door names must be gone (no alias): {sorted(still)}"


def test_all_templates_registered_and_callable():
    """Every engine template is registered tier=template, workflow_dispatch, and
    directly callable (no door, no gate expansion)."""
    reg = _full_registry()
    registered_templates = {
        n for n, e in reg.items() if getattr(e.metadata, "tier", "general") == "template"
    }
    assert registered_templates == EXPECTED_TEMPLATES, (
        "registered tier=template set drifted from the expected 31: "
        f"missing={sorted(EXPECTED_TEMPLATES - registered_templates)} "
        f"unexpected={sorted(registered_templates - EXPECTED_TEMPLATES)}"
    )
    for name in EXPECTED_TEMPLATES:
        entry = reg[name]
        assert callable(entry.fn), f"{name} is not callable"
        assert getattr(entry.metadata, "engine", None), f"{name} missing engine tag"


# ---------------------------------------------------------------------------
# (2) Retrieval matrix -- every template surfaces top-8 for >=1 natural query.
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def warm_index():
    dd._get_index()  # hashed backend, no network model load
    yield


def _template_corpus() -> dict[str, list[str]]:
    """Load each template's co-located workflows/**/corpus.yaml queries."""
    import trid3nt_server.agent.tools as t

    workflows = Path(t.__file__).resolve().parents[1] / "workflows"
    out: dict[str, list[str]] = {}
    for cp in workflows.rglob("corpus.yaml"):
        data = yaml.safe_load(cp.read_text()) or {}
        for k, v in data.items():
            out.setdefault(k, []).extend(q for q in (v or []) if isinstance(q, str))
    return out


def test_every_template_surfaces_in_top8(warm_index):
    """Model-free retrieve_visible_tools(query, None, 8): for EACH of the 23
    engine templates, at least one of its natural corpus queries surfaces it in
    the top-8. This is the discovery guarantee that lets the doors die."""
    corpus = _template_corpus()
    misses: dict[str, list[str]] = {}
    for tmpl in sorted(EXPECTED_TEMPLATES):
        queries = corpus.get(tmpl, [])
        assert queries, f"{tmpl} has NO corpus queries (retrieval-corpus-first rule)"
        surfaced = any(tmpl in retrieve_visible_tools(q, None, 8) for q in queries)
        if not surfaced:
            misses[tmpl] = queries
    assert not misses, (
        "these templates surface in NO top-8 for any corpus query "
        f"(discovery is broken -- doors cannot die): {sorted(misses)}"
    )
