"""The shared TELEMAC step family: the reach pipeline every river template declares.

One skeleton - geocode -> flowline -> seed -> deck -> solve -> products - with a
per-deliverable serialization hook at each end. A workflow declares which steps
it wants and what feeds them; nothing here decides what question is being asked.
"""

from __future__ import annotations

from .deck import WriteDeck, normalize_bank_source, stage_manifest, write_reach_deck
from .errors import (
    TelemacBanksUnavailableError,
    TelemacDyeScenarioError,
    TelemacDyeScenarioInputError,
    TelemacReachDegenerateError,
    TelemacReleasePointRejectedError,
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
from .mesh_preview import preview_telemac_mesh
from .products import Products, publish_do_products, publish_dye_products
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
    lonlat_point,
    named_watercourse,
    reach_seed,
    slug,
    suggest_mesh_size_m,
    suggest_time_step_s,
)
from .solve import Solve, compute_class, read_run_metrics, solve_reach
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

__all__ = [
    "CarrierDischarge",
    "DEFAULT_RIVER_AOI_HALF_DEG", "GRADATION_PRESETS", "Geocode", "MESH_H_FLOOR_M",
    "MESH_NODE_CAP", "Products", "ReachSeed", "ReviewResolvedInputs",
    "SCOUR_KEYWORDS", "Solve",
    "TelemacBanksUnavailableError", "TelemacDyeScenarioError",
    "TelemacDyeScenarioInputError", "TelemacReachDegenerateError",
    "TelemacReleasePointRejectedError", "WaqtelO2", "WriteDeck",
    "arm_sediment_modules", "classify_substance", "compute_class",
    "coerce_event_time", "coerce_lonlat_point", "do_saturation_mgl",
    "event_time", "lonlat_point",
    "estimate_telemac_solve_seconds",
    "fetch_reach_flowline", "geocode_reach", "named_watercourse",
    "normalize_bank_source", "preview_telemac_mesh", "publish_do_products",
    "publish_dye_products", "reach_seed", "read_run_metrics",
    "resolve_carrier_discharge", "resolve_gradation", "resolve_rain_forcing",
    "review_resolved_inputs",
    "sanitize_substance", "slug", "solve_reach", "stage_manifest",
    "substance_class",
    "suggest_mesh_size_m", "suggest_time_step_s", "upstream_do_mgl",
    "waqtel_o2_process", "write_reach_deck",
]
