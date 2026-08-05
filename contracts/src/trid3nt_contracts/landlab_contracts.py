"""Landlab surface-process engine contracts (sprint-17 — NEW engine).

Landlab (CSDMS, MIT) is a pure-Python landscape/surface-process modeling
library: you build a ``RasterModelGrid`` over a DEM and SNAP TOGETHER documented
``Component`` objects (the CSDMS-BMI snap-together pattern) — infinite-slope
landslide stability, overland flow, hillslope diffusion, stream power, etc. The
sprint-17 Landlab North Star is a landslide-susceptibility / factor-of-safety
hazard layer: a hazard CLASS the system does not yet have.

Two shapes back the Landlab demo path (AOI -> DEM COG -> RasterModelGrid ->
component chain -> susceptibility/FoS field -> COG):

- ``LandlabRunArgs``  — the forcing/structure parameters the agent confirms with
  the user before submitting a Landlab run. Consumed by the engine worker
  (``services/workers/landlab/entrypoint.py``) which builds the grid from the
  AOI DEM and runs the documented component chain, and by the agent-side
  ``landlab_susceptibility`` template tool + ``model_landslide_scenario`` composer.
- ``LandlabSusceptibilityLayerURI`` — the postprocess output layer. Extends
  ``LayerURI`` field-for-field (so it still maps onto ``map-command load-layer``
  with no translation, like every other layer) and adds the narration scalars
  the agent cites: the unstable-area fraction + min factor-of-safety + mean
  probability of failure.

Design notes
------------
- ``bbox`` is the project ``BBox`` convention: ``(min_lon, min_lat, max_lon,
  max_lat)`` in EPSG:4326 (lon-first), range-validated by the shared ``BBox``
  type. A landslide-susceptibility AOI is an *area* (a hillslope / catchment),
  so it is a bbox — same shape as ``SWMMRunArgs.bbox``.
- ``analysis`` selects the documented Landlab component chain (EXPLICIT, never
  silently hardcoded — the cross-check improvement carried from the SWMM
  contract):
    * ``"landslide_probability"`` (DEFAULT) — the infinite-slope landslide
      stability model: Landlab's ``LandslideProbability`` component computes a
      relative wetness + a Monte-Carlo probability-of-failure field and (in the
      single-recharge mode) a factor-of-safety field, driven by topographic
      slope + specific contributing area + soil cohesion / internal-friction /
      transmissivity. The canonical Landlab landslide tutorial chain
      (FlowAccumulator -> LandslideProbability).
    * ``"overland_flow"`` — the ``OverlandFlow`` component (de Almeida 2012
      shallow-water): routes a rainfall pulse over the DEM and reports peak
      surface-water depth. The other documented surface-process North-Star
      chain; selectable so the same worker serves overland-flow runs.
- ``soil_*`` / ``rainfall_*`` / ``recharge_*`` are EXPLICIT engine parameters
  (demo defaults, narrated as demo values by the composer, not site-calibrated
  geotechnical parameters). The infinite-slope chain consumes the soil
  parameters; the overland-flow chain consumes the rainfall parameters.
- ``LandlabSusceptibilityLayerURI`` is a structured numeric carrier (invariant 1
  / Decision H / FR-AS-7): the agent narrates ``unstable_area_fraction`` /
  ``min_factor_of_safety`` / ``mean_probability_of_failure`` from these typed
  fields rather than inventing them.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator

from .common import BBox, EngineRunArgsMixin, GraceModel
from .execution import LayerURI

__all__ = [
    "LandlabAnalysis",
    "LandlabDepressionHandler",
    "DEFAULT_SOIL_TRANSMISSIVITY_M2_DAY",
    "DEFAULT_SOIL_COHESION_PA",
    "DEFAULT_SOIL_INTERNAL_FRICTION_DEG",
    "DEFAULT_SOIL_DENSITY_KG_M3",
    "DEFAULT_SOIL_THICKNESS_M",
    "DEFAULT_RECHARGE_MM_DAY",
    "DEFAULT_RAINFALL_INTENSITY_MM_HR",
    "DEFAULT_STORM_DURATION_HR",
    "DEFAULT_N_MONTE_CARLO",
    "DEFAULT_CHANNEL_THRESHOLD_CELLS",
    "DEFAULT_GREEN_AMPT_K_M_S",
    "DEFAULT_INITIAL_SOIL_MOISTURE",
    "DEFAULT_GREEN_AMPT_SOIL_TYPE",
    "DEFAULT_MEAN_STORM_DURATION_HR",
    "DEFAULT_MEAN_INTERSTORM_DURATION_HR",
    "DEFAULT_MEAN_STORM_DEPTH_MM",
    "DEFAULT_N_RECHARGE_SCENARIOS",
    "DEFAULT_OUTPUT_INTERVAL_S",
    "LandlabRunArgs",
    "LandlabSusceptibilityLayerURI",
    "LandlabFlowAccumulationLayerURI",
    "LandlabGreenAmptLayerURI",
    "LandlabStormEnsembleLayerURI",
    "LandlabOverlandTimeseriesLayerURI",
    "LandlabDemConditioningLayerURI",
    "LandlabLakeMappingLayerURI",
    "LandlabHacksLawLayerURI",
    "LandlabHandLayerURI",
]


# Which documented Landlab component chain the worker runs.
#   "landslide_probability" — infinite-slope LandslideProbability (DEFAULT;
#       the landslide-susceptibility / factor-of-safety North Star).
#   "overland_flow"         — OverlandFlow (de Almeida shallow-water rainfall
#       routing -> peak surface-water depth).
# Open ``Literal`` so the engine may add component chains without a wire break.
#   "flow_accumulation"     — FlowAccumulator drainage-area + channel-network
#       extraction (the canonical the_FlowAccumulator tutorial chain): route flow
#       over the DEM, accumulate contributing drainage area, extract the channel
#       network by a drainage-area threshold, and compare routing directors.
#   "green_ampt_overland_flow" - SoilInfiltrationGreenAmpt coupled to the
#       OverlandFlow chain: partition a design storm into infiltration-depth vs
#       runoff-depth (rainfall excess) rasters (the canonical
#       infilt_green_ampt_with_overland_flow tutorial chain).
#   "landslide_storm_ensemble" - the infinite-slope LandslideProbability chain
#       swept across a storm/recharge ENSEMBLE (recharge scenarios drawn from a
#       landlab PrecipitationDistribution) instead of one fixed recharge: emits
#       the ensemble-mean probability field + a susceptibility-vs-recharge series.
#   "overland_flow_timeseries" - the de Almeida OverlandFlow chain sampled at N
#       intervals (output_interval_s) so inundation depth is written frame by
#       frame (the time-stepped animation output), plus the peak-depth field.
#   "dem_pit_fill" - LakeMapperBarnes depression filling: the per-cell fill depth
#       (where the DEM needed filling to be routable) as its own field.
#   "lake_mapping" - the same LakeMapperBarnes plumbing with lake tracking on:
#       lake extent (mask) + per-cell lake depth.
#   "hacks_law" - a HackCalculator diagnostic: the longest-flow-path vs
#       drainage-area scaling exponent per basin (chart-led + basin vector).
#   "hand" - HeightAboveDrainageCalculator (Nobre et al. 2011): height above the
#       nearest drainage channel (a wetness / relative-elevation proxy field).
LandlabAnalysis = Literal[
    "landslide_probability",
    "overland_flow",
    "flow_accumulation",
    "green_ampt_overland_flow",
    "landslide_storm_ensemble",
    "overland_flow_timeseries",
    "dem_pit_fill",
    "lake_mapping",
    "hacks_law",
    "hand",
]

# How the flow-accumulation chain handles closed depressions before routing:
#   "fill"           — Landlab DepressionFinderAndRouter (D8 single-flow only;
#       a multi-flow director runs without it — noted honestly).
#   "priority_flood" — the PriorityFloodFlowRouter (fills/breaches + routes in one
#       pass, valid for every director metric). The folded row-9 component.
LandlabDepressionHandler = Literal["fill", "priority_flood"]

#: Default channel-head drainage-area threshold as a MULTIPLE of the grid cell
#: area (contributing cells). Mirrors the worker
#: ``component_chain.DEFAULT_CHANNEL_THRESHOLD_CELLS``.
DEFAULT_CHANNEL_THRESHOLD_CELLS: int = 100

#: Green-Ampt infiltration demo defaults (labeled demo values, not
#: SSURGO-calibrated - no soil fetcher yet). Mirrors the worker
#: ``component_chain`` Green-Ampt constants.
DEFAULT_GREEN_AMPT_K_M_S: float = 1.0e-5
DEFAULT_INITIAL_SOIL_MOISTURE: float = 0.15
DEFAULT_GREEN_AMPT_SOIL_TYPE: str = "sandy loam"


# TENTATIVE demo defaults (sprint-17). Narrated as demo values, NOT
# site-calibrated geotechnical / hydrologic parameters, by the composer.
#
# Infinite-slope LandslideProbability soil parameters (Landlab tutorial values):
DEFAULT_SOIL_TRANSMISSIVITY_M2_DAY: float = 20.0  # saturated soil transmissivity
DEFAULT_SOIL_COHESION_PA: float = 10_000.0  # effective soil cohesion, Pa
DEFAULT_SOIL_INTERNAL_FRICTION_DEG: float = 35.0  # internal angle of friction, deg
DEFAULT_SOIL_DENSITY_KG_M3: float = 2000.0  # wet soil bulk density, kg/m^3
DEFAULT_SOIL_THICKNESS_M: float = 1.0  # soil mantle thickness over bedrock, m
DEFAULT_RECHARGE_MM_DAY: float = 30.0  # groundwater recharge driving wetness
DEFAULT_N_MONTE_CARLO: int = 250  # Monte-Carlo draws for probability of failure
# OverlandFlow rainfall design-storm parameters:
DEFAULT_RAINFALL_INTENSITY_MM_HR: float = 50.0  # rainfall intensity, mm/hr
DEFAULT_STORM_DURATION_HR: float = 2.0  # storm duration, hours

# Storm-ensemble (PrecipitationDistribution) draw parameters for the
# landslide_storm_ensemble chain. Poisson storm generator means; each drawn
# storm depth (mm) becomes one triggering-recharge scenario (mm/day pulse).
DEFAULT_MEAN_STORM_DURATION_HR: float = 2.0  # mean storm duration, hours
DEFAULT_MEAN_INTERSTORM_DURATION_HR: float = 48.0  # mean interstorm duration, hours
DEFAULT_MEAN_STORM_DEPTH_MM: float = 15.0  # mean storm depth, mm
DEFAULT_N_RECHARGE_SCENARIOS: int = 8  # recharge scenarios swept in the ensemble
# Time-stepped OverlandFlow output cadence (seconds between depth snapshots).
DEFAULT_OUTPUT_INTERVAL_S: float = 300.0  # depth snapshot interval, seconds


class LandlabRunArgs(EngineRunArgsMixin):
    """Forcing + structure parameters for a Landlab surface-process run.

    Adopts ``EngineRunArgsMixin`` (levers STEP 3): ``advanced_physics`` keys are
    validated against ``physics_registry.PHYSICS_REGISTRY["landlab"]``
    (overland_alpha / mannings_n / flow_director) and applied at the
    ``OverlandFlow`` / ``FlowAccumulator`` component-build seam in the worker
    chain; ``None`` => byte-identical component chain.

    Returned/assembled by the landslide composer after agent-confirmed parameter
    extraction; consumed by the Landlab worker / adapter. The agent confirms
    these with the user before submission (confirmation-before-consequence,
    invariant 9).

    Use this when:
        Building the input to a Landlab run over an AOI — landslide
        susceptibility / factor-of-safety (infinite-slope) or rainfall overland
        flow — driven by an AOI DEM + soil / rainfall parameters.

    Do NOT use this for:
        Surface-water riverine/coastal flooding (that is SFINCS ``ModelSetup``),
        urban pluvial drainage (that is ``SWMMRunArgs``), or groundwater
        contamination (that is ``MODFLOWRunArgs``); nor for carrying solver
        output (that is ``LandlabSusceptibilityLayerURI``).

    Fields:
        schema_version: contract version pin (additive growth only).
        bbox: AOI as ``(min_lon, min_lat, max_lon, max_lat)`` EPSG:4326. The
            worker fetches a DEM within it and builds the ``RasterModelGrid``.
        analysis: which documented Landlab component chain to run, EXACTLY one of
            {"landslide_probability", "overland_flow"} (EXPLICIT, never
            hardcoded). ``"landslide_probability"`` (DEFAULT) runs the
            infinite-slope ``LandslideProbability`` chain (susceptibility / FoS);
            ``"overland_flow"`` runs the ``OverlandFlow`` rainfall chain.
        target_resolution_m: requested grid cell size, m (> 0). The DEM is
            resampled to this resolution before the grid is built. Demo default
            30 m (a hillslope-scale grid; finer for small AOIs).
        soil_transmissivity_m2_day: saturated soil transmissivity, m^2/day (> 0)
            (LandslideProbability). Demo default.
        soil_cohesion_pa: effective soil cohesion, Pa (>= 0)
            (LandslideProbability). Demo default.
        soil_internal_friction_deg: soil internal angle of friction, degrees in
            (0, 90) (LandslideProbability). Demo default.
        soil_density_kg_m3: wet soil bulk density, kg/m^3 (> 0)
            (LandslideProbability). Demo default.
        soil_thickness_m: soil mantle thickness over bedrock, m (> 0)
            (LandslideProbability). Demo default.
        recharge_mm_day: groundwater recharge driving the relative-wetness term,
            mm/day (>= 0) (LandslideProbability). Demo default.
        n_monte_carlo: Monte-Carlo draws for the probability-of-failure field
            (>= 1) (LandslideProbability). Demo default.
        rainfall_intensity_mm_hr: rainfall intensity, mm/hr (> 0)
            (OverlandFlow). Demo default.
        storm_duration_hr: storm duration, hours (> 0) (OverlandFlow). Demo
            default.
    """

    schema_version: Literal["v1"] = "v1"

    bbox: BBox

    analysis: LandlabAnalysis = "landslide_probability"

    target_resolution_m: float = Field(default=30.0, gt=0.0)

    # --- infinite-slope LandslideProbability soil parameters ---
    soil_transmissivity_m2_day: float = Field(
        default=DEFAULT_SOIL_TRANSMISSIVITY_M2_DAY, gt=0.0
    )
    soil_cohesion_pa: float = Field(default=DEFAULT_SOIL_COHESION_PA, ge=0.0)
    soil_internal_friction_deg: float = Field(
        default=DEFAULT_SOIL_INTERNAL_FRICTION_DEG, gt=0.0, lt=90.0
    )
    soil_density_kg_m3: float = Field(default=DEFAULT_SOIL_DENSITY_KG_M3, gt=0.0)
    soil_thickness_m: float = Field(default=DEFAULT_SOIL_THICKNESS_M, gt=0.0)
    recharge_mm_day: float = Field(default=DEFAULT_RECHARGE_MM_DAY, ge=0.0)
    n_monte_carlo: int = Field(default=DEFAULT_N_MONTE_CARLO, ge=1)

    # --- OverlandFlow rainfall parameters ---
    rainfall_intensity_mm_hr: float = Field(
        default=DEFAULT_RAINFALL_INTENSITY_MM_HR, gt=0.0
    )
    storm_duration_hr: float = Field(default=DEFAULT_STORM_DURATION_HR, gt=0.0)

    # --- flow_accumulation chain parameters ---
    #: Depression handling before routing (``"fill"`` DepressionFinderAndRouter,
    #: D8-only; ``"priority_flood"`` PriorityFloodFlowRouter, any director).
    depression_handler: LandlabDepressionHandler = "fill"
    #: Channel-head drainage-area threshold as a MULTIPLE of the grid cell area
    #: (contributing cells) for channel-network extraction (>= 1).
    channel_threshold_cells: int = Field(
        default=DEFAULT_CHANNEL_THRESHOLD_CELLS, ge=1
    )

    # --- green_ampt_overland_flow chain parameters ---
    #: Green-Ampt saturated hydraulic conductivity, m/s (> 0). Demo default
    #: (sandy-loam K); not SSURGO-calibrated.
    soil_hydraulic_conductivity_m_s: float = Field(
        default=DEFAULT_GREEN_AMPT_K_M_S, gt=0.0
    )
    #: Green-Ampt initial soil moisture content, volumetric fraction in [0, 1)
    #: (sets the moisture deficit). Demo default.
    initial_soil_moisture_content: float = Field(
        default=DEFAULT_INITIAL_SOIL_MOISTURE, ge=0.0, lt=1.0
    )
    #: Green-Ampt soil texture class (selects Landlab's tabulated capillary-head
    #: + porosity). Demo default "sandy loam".
    green_ampt_soil_type: str = Field(default=DEFAULT_GREEN_AMPT_SOIL_TYPE)

    # --- landslide_storm_ensemble chain parameters ---
    #: Mean storm duration (hours) for the PrecipitationDistribution recharge
    #: draws (> 0).
    mean_storm_duration_hr: float = Field(
        default=DEFAULT_MEAN_STORM_DURATION_HR, gt=0.0
    )
    #: Mean interstorm duration (hours) for the storm generator (> 0).
    mean_interstorm_duration_hr: float = Field(
        default=DEFAULT_MEAN_INTERSTORM_DURATION_HR, gt=0.0
    )
    #: Mean storm depth (mm) for the storm generator; each drawn depth is a
    #: triggering-recharge scenario (mm/day pulse) (> 0).
    mean_storm_depth_mm: float = Field(default=DEFAULT_MEAN_STORM_DEPTH_MM, gt=0.0)
    #: Number of recharge scenarios swept in the ensemble (>= 2).
    n_recharge_scenarios: int = Field(default=DEFAULT_N_RECHARGE_SCENARIOS, ge=2, le=64)

    # --- overland_flow_timeseries chain parameters ---
    #: Seconds between surface-water-depth snapshots for the time-stepped output
    #: (> 0). Snapshots are subsampled to the animation frame cap downstream.
    output_interval_s: float = Field(default=DEFAULT_OUTPUT_INTERVAL_S, gt=0.0)

    # --- dem_pit_fill / lake_mapping chain parameters ---
    #: LakeMapperBarnes fill mode: True fills each depression to a flat surface;
    #: False fills to a slight downslope incline (so flow routes over the lake).
    fill_flat: bool = Field(default=True)

    @field_validator("depression_handler", mode="before")
    @classmethod
    def _normalize_depression_handler(cls, value: Any) -> Any:
        """Map common synonyms onto the canonical depression handler BEFORE the
        ``Literal`` check (no self-correcting retry). Unknown passes through."""
        if not isinstance(value, str):
            return value
        key = value.strip().lower().replace("-", "_").replace(" ", "_")
        aliases = {
            "fill": "fill",
            "filled": "fill",
            "depression_finder": "fill",
            "depressionfinderandrouter": "fill",
            "sink_fill": "fill",
            "priority_flood": "priority_flood",
            "priorityflood": "priority_flood",
            "priority": "priority_flood",
            "breach": "priority_flood",
            "pf": "priority_flood",
        }
        return aliases.get(key, key)

    @field_validator("analysis", mode="before")
    @classmethod
    def _normalize_analysis(cls, value: Any) -> Any:
        """Map common LLM synonyms onto the canonical analysis BEFORE the
        ``Literal`` check, so the FIRST attempt succeeds (no self-correcting
        retry loop). An unknown string passes through UNCHANGED so a
        genuinely-invalid value still raises the honest ``Literal`` error."""
        if not isinstance(value, str):
            return value
        key = value.strip().lower().replace("-", "_").replace(" ", "_")
        aliases = {
            # landslide_probability
            "landslide": "landslide_probability",
            "landslides": "landslide_probability",
            "landslide_susceptibility": "landslide_probability",
            "susceptibility": "landslide_probability",
            "slope_stability": "landslide_probability",
            "stability": "landslide_probability",
            "factor_of_safety": "landslide_probability",
            "fos": "landslide_probability",
            "infinite_slope": "landslide_probability",
            # overland_flow
            "overland": "overland_flow",
            "overlandflow": "overland_flow",
            "runoff": "overland_flow",
            "surface_flow": "overland_flow",
            "shallow_water": "overland_flow",
            # flow_accumulation
            "flow_accumulation": "flow_accumulation",
            "flowaccumulation": "flow_accumulation",
            "flow_accumulator": "flow_accumulation",
            "flowaccumulator": "flow_accumulation",
            "drainage_area": "flow_accumulation",
            "drainage": "flow_accumulation",
            "flow_routing": "flow_accumulation",
            "channel_network": "flow_accumulation",
            "channel_extraction": "flow_accumulation",
            "accumulation": "flow_accumulation",
            # green_ampt_overland_flow
            "green_ampt": "green_ampt_overland_flow",
            "green_ampt_overland_flow": "green_ampt_overland_flow",
            "greenampt": "green_ampt_overland_flow",
            "infiltration": "green_ampt_overland_flow",
            "infiltration_runoff": "green_ampt_overland_flow",
            "runoff_partition": "green_ampt_overland_flow",
            "rainfall_partition": "green_ampt_overland_flow",
            "infiltration_excess": "green_ampt_overland_flow",
            # landslide_storm_ensemble
            "landslide_storm_ensemble": "landslide_storm_ensemble",
            "storm_ensemble": "landslide_storm_ensemble",
            "rainfall_ensemble": "landslide_storm_ensemble",
            "landslide_sensitivity": "landslide_storm_ensemble",
            "susceptibility_sensitivity": "landslide_storm_ensemble",
            "recharge_sweep": "landslide_storm_ensemble",
            # overland_flow_timeseries
            "overland_flow_timeseries": "overland_flow_timeseries",
            "overland_timeseries": "overland_flow_timeseries",
            "depth_timeseries": "overland_flow_timeseries",
            "flood_animation": "overland_flow_timeseries",
            "inundation_animation": "overland_flow_timeseries",
            "time_stepped_depth": "overland_flow_timeseries",
            # dem_pit_fill
            "dem_pit_fill": "dem_pit_fill",
            "pit_fill": "dem_pit_fill",
            "fill_depth": "dem_pit_fill",
            "dem_conditioning": "dem_pit_fill",
            "sink_fill_depth": "dem_pit_fill",
            "depression_filling": "dem_pit_fill",
            # lake_mapping
            "lake_mapping": "lake_mapping",
            "lake_extent": "lake_mapping",
            "lake_depth": "lake_mapping",
            "lakes": "lake_mapping",
            "ponding": "lake_mapping",
            # hacks_law
            "hacks_law": "hacks_law",
            "hack": "hacks_law",
            "hacks": "hacks_law",
            "basin_scaling": "hacks_law",
            "length_area_scaling": "hacks_law",
            # hand
            "hand": "hand",
            "height_above_drainage": "hand",
            "height_above_nearest_drainage": "hand",
            "hand_wetness": "hand",
            "wetness_proxy": "hand",
        }
        return aliases.get(key, key)


class LandlabSusceptibilityLayerURI(LayerURI):
    """A ``LayerURI`` for a Landlab landslide-susceptibility / FoS layer, plus
    narration scalars.

    Extends ``LayerURI`` field-for-field so it still maps onto
    ``map-command load-layer`` with no translation (same as every other layer).
    Adds the structured numbers the agent narrates about the hazard so the LLM
    cites typed fields, never invents them (invariant 1, FR-AS-7):

        unstable_area_fraction: fraction of the AOI flagged unstable
            (probability-of-failure above the unstable threshold for the
            landslide chain; wet/inundated cell fraction for the overland-flow
            chain), dimensionless in [0, 1].
        min_factor_of_safety: minimum factor-of-safety over the AOI (the
            landslide chain; <= 1.0 means at-failure). For the overland-flow
            chain this carries the peak surface-water depth in metres as a
            structured scalar (the layer's ``units`` field disambiguates).
        mean_probability_of_failure: mean probability of failure over the AOI,
            dimensionless in [0, 1] (the landslide chain; 0.0 for overland flow).

    ``layer_type`` for a susceptibility / FoS / depth field is ``"raster"`` (a
    single-band COG); the base contract's vocabulary is inherited unchanged.
    """

    unstable_area_fraction: float = Field(ge=0.0, le=1.0)
    min_factor_of_safety: float = Field(ge=0.0)
    mean_probability_of_failure: float = Field(ge=0.0, le=1.0)

    # Input provenance for narration: which triggering-rainfall source was used
    # (NOAA Atlas-14 design storm) and that the soil block is demo-defaulted (no
    # SSURGO/POLARIS fetcher yet). Set by the composer/tool. None preserves prior
    # behaviour. A fuller structured assumptions list is a wave-2 addition; this
    # is the honest prose surface so nothing regresses meanwhile.
    source_note: str | None = Field(default=None)


class LandlabFlowAccumulationLayerURI(LayerURI):
    """A ``LayerURI`` for a Landlab flow-accumulation drainage-area layer, plus
    the drainage-network narration scalars.

    Extends ``LayerURI`` field-for-field so it still maps onto
    ``map-command load-layer`` with no translation. The primary raster is the
    log-styled ``drainage_area`` (m^2) field; the channel network is a companion
    vector layer. Adds the structured numbers the agent narrates (invariant 1,
    FR-AS-7 -- typed fields, never invented):

        max_drainage_area_km2: the maximum contributing drainage area over the
            AOI (km^2) -- the size of the largest accumulated flow path (the
            basin outlet). > 0.
        mean_drainage_area_km2: the mean contributing drainage area over the
            active AOI cells (km^2). > 0.
        channelized_area_fraction: fraction of active cells whose drainage area
            meets the channel-head threshold (the extracted channel network),
            dimensionless in [0, 1].

    ``layer_type`` for the drainage-area field is ``"raster"`` (a single-band
    COG); the base contract's vocabulary is inherited unchanged.
    """

    max_drainage_area_km2: float = Field(ge=0.0)
    mean_drainage_area_km2: float = Field(ge=0.0)
    channelized_area_fraction: float = Field(ge=0.0, le=1.0)

    #: Input-provenance prose (DEM source; the routing/depression/threshold knobs
    #: are deterministic engine settings, not synthetic data). None preserves the
    #: prior behaviour.
    source_note: str | None = Field(default=None)


class LandlabGreenAmptLayerURI(LayerURI):
    """A ``LayerURI`` for a Landlab Green-Ampt infiltration-depth layer, plus the
    storm-partition narration scalars.

    Extends ``LayerURI`` field-for-field so it still maps onto
    ``map-command load-layer`` with no translation. The primary raster is the
    per-cell cumulative infiltration depth (m); a companion runoff-depth
    (rainfall-excess) raster is emitted alongside. Adds the structured numbers
    the agent narrates (invariant 1, FR-AS-7 -- typed fields, never invented):

        infiltrated_fraction: domain-mean share of the storm rainfall that
            infiltrated, dimensionless in [0, 1].
        runoff_fraction: domain-mean share that became runoff (rainfall excess),
            dimensionless in [0, 1].
        mean_infiltration_mm: domain-mean cumulative infiltration depth (mm).
        mean_runoff_mm: domain-mean runoff (rainfall-excess) depth (mm).
        total_rainfall_mm: the design-storm total depth (mm) the partition is of.

    ``layer_type`` for the infiltration-depth field is ``"raster"`` (a
    single-band COG); the base contract's vocabulary is inherited unchanged.
    """

    infiltrated_fraction: float = Field(ge=0.0, le=1.0)
    runoff_fraction: float = Field(ge=0.0, le=1.0)
    mean_infiltration_mm: float = Field(ge=0.0)
    mean_runoff_mm: float = Field(ge=0.0)
    total_rainfall_mm: float = Field(ge=0.0)

    #: Input-provenance prose (triggering rainfall = NOAA Atlas-14 design storm;
    #: the soil hydraulic block is demo-defaulted). None preserves prior behaviour.
    source_note: str | None = Field(default=None)


class LandlabStormEnsembleLayerURI(LayerURI):
    """A ``LayerURI`` for the storm-ensemble landslide-susceptibility field, plus
    the ensemble-sensitivity narration scalars.

    The primary raster is the ENSEMBLE-MEAN probability-of-failure field (m of the
    per-scenario probability grids); the sensitivity of the unstable-area fraction
    to the swept recharge scenarios is the companion chart. Adds the structured
    numbers the agent narrates (typed fields, never invented):

        unstable_area_fraction: ensemble-mean fraction of the AOI flagged
            unstable, dimensionless in [0, 1].
        mean_probability_of_failure: ensemble-mean probability of failure over
            active cells, dimensionless in [0, 1].
        min_recharge_mm_day / max_recharge_mm_day: the recharge span swept.
        n_recharge_scenarios: number of storm/recharge scenarios in the sweep.
        sensitivity_slope: change in unstable-area fraction per mm/day of
            recharge (least-squares slope over the ensemble).
    """

    unstable_area_fraction: float = Field(ge=0.0, le=1.0)
    mean_probability_of_failure: float = Field(ge=0.0, le=1.0)
    min_recharge_mm_day: float = Field(ge=0.0)
    max_recharge_mm_day: float = Field(ge=0.0)
    n_recharge_scenarios: int = Field(ge=1)
    sensitivity_slope: float = Field(default=0.0)

    source_note: str | None = Field(default=None)


class LandlabOverlandTimeseriesLayerURI(LayerURI):
    """A ``LayerURI`` for the time-stepped overland-flow depth output, plus the
    hydrograph narration scalars.

    The primary raster is the PEAK surface-water-depth field; per-interval depth
    frames are emitted as a time-stepped animation group, and the depth-vs-time
    series at the maximum-depth cell is the companion chart. Adds the structured
    numbers the agent narrates (typed fields, never invented):

        wet_area_fraction: fraction of active cells at/above the wet-depth floor
            at peak, dimensionless in [0, 1].
        max_depth_m: peak surface-water depth over the AOI and the storm (m).
        n_frames: number of emitted time-step depth frames.
        time_to_peak_s: elapsed seconds to the maximum-depth-cell peak.
    """

    wet_area_fraction: float = Field(ge=0.0, le=1.0)
    max_depth_m: float = Field(ge=0.0)
    n_frames: int = Field(ge=0)
    time_to_peak_s: float = Field(ge=0.0)

    source_note: str | None = Field(default=None)


class LandlabDemConditioningLayerURI(LayerURI):
    """A ``LayerURI`` for the DEM pit-fill conditioning depth field, plus the
    routability narration scalars.

    The primary raster is the per-cell FILL DEPTH (metres the DEM had to rise to
    become routable, via LakeMapperBarnes). Adds the structured numbers the agent
    narrates (typed fields, never invented):

        max_fill_depth_m: deepest fill applied anywhere in the AOI (m).
        filled_area_fraction: fraction of active cells that required any fill,
            dimensionless in [0, 1].
        n_depressions: number of distinct filled depressions.
    """

    max_fill_depth_m: float = Field(ge=0.0)
    filled_area_fraction: float = Field(ge=0.0, le=1.0)
    n_depressions: int = Field(ge=0)

    source_note: str | None = Field(default=None)


class LandlabLakeMappingLayerURI(LayerURI):
    """A ``LayerURI`` for the lake extent + depth field, plus the ponding
    narration scalars.

    The primary raster is the per-cell LAKE DEPTH within mapped lakes (via
    LakeMapperBarnes with lake tracking); the lake extent is a companion vector.
    Adds the structured numbers the agent narrates (typed fields, never invented):

        n_lakes: number of distinct lakes mapped.
        total_lake_area_km2: summed lake surface area (km^2).
        total_lake_volume_m3: summed lake storage volume (m^3).
        max_lake_depth_m: deepest lake depth in the AOI (m).
    """

    n_lakes: int = Field(ge=0)
    total_lake_area_km2: float = Field(ge=0.0)
    total_lake_volume_m3: float = Field(ge=0.0)
    max_lake_depth_m: float = Field(ge=0.0)

    source_note: str | None = Field(default=None)


class LandlabHacksLawLayerURI(LayerURI):
    """A ``LayerURI`` for the Hack's-law basin-scaling diagnostic, plus the fit
    narration scalars.

    The primary raster is the log-styled drainage-area backdrop; the fitted basin
    footprint is a companion vector and the length-vs-area log-log scatter with
    the fitted exponent is the companion chart. Adds the structured numbers the
    agent narrates (typed fields, never invented):

        hack_exponent: the fitted Hack exponent ``h`` in ``L = C * A**h`` for the
            largest basin (the classic value is ~0.5-0.6).
        hack_coefficient: the fitted coefficient ``C``.
        largest_basin_area_km2: drainage area of the largest fitted basin (km^2).
        n_basins: number of basins the diagnostic fitted.
    """

    hack_exponent: float = Field(ge=0.0)
    hack_coefficient: float = Field(ge=0.0)
    largest_basin_area_km2: float = Field(ge=0.0)
    n_basins: int = Field(ge=0)

    source_note: str | None = Field(default=None)


class LandlabHandLayerURI(LayerURI):
    """A ``LayerURI`` for the Height Above Nearest Drainage (HAND) field, plus the
    narration scalars.

    The primary raster is the per-cell height above the nearest drainage channel
    (Nobre et al. 2011) - a relative-elevation / wetness proxy. Adds the
    structured numbers the agent narrates (typed fields, never invented):

        mean_hand_m: mean HAND over active cells (m).
        max_hand_m: maximum HAND over the AOI (m).
        channel_area_fraction: fraction of active cells classed as channel (HAND
            ~ 0), dimensionless in [0, 1].
        lowland_area_fraction: fraction of active cells with HAND below the
            lowland threshold (near-drainage, flood-prone), in [0, 1].
    """

    mean_hand_m: float = Field(ge=0.0)
    max_hand_m: float = Field(ge=0.0)
    channel_area_fraction: float = Field(ge=0.0, le=1.0)
    lowland_area_fraction: float = Field(ge=0.0, le=1.0)

    source_note: str | None = Field(default=None)
