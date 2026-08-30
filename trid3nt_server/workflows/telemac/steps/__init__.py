"""The shared TELEMAC step families: the pipelines every TELEMAC template declares.

Two fronts, one skeleton each. The REACH front - geocode -> flowline -> seed ->
deck -> solve -> products - meshes a corridor along a flowline. The OPEN-WATER
front - AOI -> deck -> solve -> products - lays a regular grid over an extent and
serves the coastal, wave, harbour and stratified domains, which differ only in
which worker section their deck names. Both have a per-deliverable serialization
hook at each end. A workflow declares which steps it wants and what feeds them;
nothing here decides what question is being asked.
"""

from __future__ import annotations

from .agitation import (
    Agitation,
    publish_agitation_products,
    write_agitation_deck,
)
from .coastal import Coastal, publish_coastal_products, write_coastal_deck
from .deck import WriteDeck, stage_manifest, write_reach_deck
from .open_water import (
    OpenWaterError,
    SolveOpenWater,
    download_open_water_result,
    solve_open_water,
    fetch_domain_bed,
    great_lake_for,
    real_lake_bathy_label,
    solved_domain_bbox,
    solves_on_real_bed,
    stage_open_water_manifest,
    staged_bed_inputs,
)
from .errors import (
    ReachBanksUnmapped,
    TelemacDyeScenarioError,
    TelemacDyeScenarioInputError,
    TelemacReachDegenerateError,
    TelemacReleaseOutsideDomainError,
)
from .forcing import (
    CarrierDischarge,
    ReviewResolvedInputs,
    coerce_event_time,
    event_time,
    resolve_carrier_discharge,
    resolve_rain_forcing,
    review_resolved_inputs,
)
from .products import Products, publish_do_products, publish_dye_products
from .rain_on_grid import (
    AcquireCatchment,
    Infiltration,
    RainOnGrid,
    RainOnGridError,
    SolveRainOnGrid,
    acquire_catchment,
    catchment_aoi,
    node_infiltration_fields,
    publish_rain_on_grid_products,
    resolve_rain_event,
    solve_rain_on_grid,
    write_rain_on_grid_deck,
)
from .reach import (
    DEFAULT_RIVER_AOI_HALF_DEG,
    Geocode,
    MESH_H_FLOOR_M,
    MESH_NODE_CAP,
    ReachSeed,
    coerce_lonlat_point,
    estimate_telemac_solve_seconds,
    fetch_reach_flowline,
    geocode_reach,
    named_watercourse,
    reach_seed,
    slug,
    suggest_time_step_s,
)
from .solve import Solve, compute_class, read_run_metrics, solve_reach
from .stratified import (
    Stratified,
    publish_stratified_products,
    write_stratified_deck,
)
from .substance import (
    GRADATION_PRESETS,
    SCOUR_KEYWORDS,
    arm_sediment_modules,
    classify_substance,
    resolve_gradation,
    sanitize_substance,
    substance_class,
)
from .water_quality import (
    WaqtelO2,
    do_saturation_mgl,
    upstream_do_mgl,
    waqtel_o2_process,
)
from .wave import Wave, publish_wave_products, write_wave_deck

__all__ = [
    "AcquireCatchment", "Infiltration", "RainOnGrid",
    "RainOnGridError", "SolveRainOnGrid", "acquire_catchment",
    "catchment_aoi", "node_infiltration_fields",
    "publish_rain_on_grid_products", "resolve_rain_event", "solve_rain_on_grid",
    "write_rain_on_grid_deck",
    "Agitation", "CarrierDischarge", "Coastal",
    "DEFAULT_RIVER_AOI_HALF_DEG", "GRADATION_PRESETS", "Geocode", "MESH_H_FLOOR_M",
    "MESH_NODE_CAP", "OpenWaterError", "Products", "ReachSeed",
    "ReviewResolvedInputs",
    "SCOUR_KEYWORDS", "Solve", "SolveOpenWater", "Stratified",
    "download_open_water_result", "publish_coastal_products",
    "solve_open_water", "solved_domain_bbox", "stage_open_water_manifest",
    "staged_bed_inputs", "solves_on_real_bed", "fetch_domain_bed",
    "great_lake_for", "real_lake_bathy_label",
    "Wave", "write_coastal_deck",
    "ReachBanksUnmapped", "TelemacDyeScenarioError",
    "TelemacDyeScenarioInputError", "TelemacReachDegenerateError",
    "TelemacReleaseOutsideDomainError", "WaqtelO2", "WriteDeck",
    "arm_sediment_modules", "classify_substance", "compute_class",
    "coerce_event_time", "coerce_lonlat_point", "do_saturation_mgl",
    "event_time",
    "estimate_telemac_solve_seconds",
    "fetch_reach_flowline", "geocode_reach", "named_watercourse",
    "publish_do_products",
    "publish_dye_products", "reach_seed", "read_run_metrics",
    "resolve_carrier_discharge", "resolve_gradation", "resolve_rain_forcing",
    "review_resolved_inputs",
    "sanitize_substance", "slug", "solve_reach", "stage_manifest",
    "substance_class",
    "suggest_time_step_s", "upstream_do_mgl",
    "publish_agitation_products",
    "publish_stratified_products",
    "publish_wave_products", "waqtel_o2_process", "write_agitation_deck",
    "write_stratified_deck",
    "write_reach_deck",
    "write_wave_deck",
]
