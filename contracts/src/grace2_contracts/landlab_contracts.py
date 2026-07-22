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
  ``run_landlab_susceptibility`` tool + ``model_landslide_scenario`` composer.
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
    "DEFAULT_SOIL_TRANSMISSIVITY_M2_DAY",
    "DEFAULT_SOIL_COHESION_PA",
    "DEFAULT_SOIL_INTERNAL_FRICTION_DEG",
    "DEFAULT_SOIL_DENSITY_KG_M3",
    "DEFAULT_SOIL_THICKNESS_M",
    "DEFAULT_RECHARGE_MM_DAY",
    "DEFAULT_RAINFALL_INTENSITY_MM_HR",
    "DEFAULT_STORM_DURATION_HR",
    "DEFAULT_N_MONTE_CARLO",
    "LandlabRunArgs",
    "LandlabSusceptibilityLayerURI",
]


# Which documented Landlab component chain the worker runs.
#   "landslide_probability" — infinite-slope LandslideProbability (DEFAULT;
#       the landslide-susceptibility / factor-of-safety North Star).
#   "overland_flow"         — OverlandFlow (de Almeida shallow-water rainfall
#       routing -> peak surface-water depth).
# Open ``Literal`` so the engine may add component chains without a wire break.
LandlabAnalysis = Literal["landslide_probability", "overland_flow"]


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
