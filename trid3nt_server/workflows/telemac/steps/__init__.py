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
    coerce_event_time,
    resolve_carrier_discharge,
    resolve_rain_forcing,
)
from .mesh_preview import preview_telemac_mesh
from .products import Products, build_dye_chart, publish_do_products, publish_dye_products
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
    suggest_mesh_size_m,
    suggest_time_step_s,
)
from .solve import Solve, read_run_metrics, solve_reach
from .substance import (
    GRADATION_PRESETS,
    SCOUR_KEYWORDS,
    arm_sediment_modules,
    classify_substance,
    resolve_gradation,
    sanitize_substance,
)

__all__ = [
    "CarrierDischarge",
    "DEFAULT_RIVER_AOI_HALF_DEG", "GRADATION_PRESETS", "Geocode", "MESH_H_FLOOR_M",
    "MESH_NODE_CAP", "Products", "ReachSeed", "SCOUR_KEYWORDS", "Solve",
    "TelemacBanksUnavailableError", "TelemacDyeScenarioError",
    "TelemacDyeScenarioInputError", "TelemacReachDegenerateError",
    "TelemacReleasePointRejectedError", "WriteDeck",
    "arm_sediment_modules", "build_dye_chart", "classify_substance",
    "coerce_event_time", "coerce_lonlat_point", "estimate_telemac_solve_seconds",
    "fetch_reach_flowline", "geocode_reach", "named_watercourse",
    "normalize_bank_source", "preview_telemac_mesh", "publish_do_products",
    "publish_dye_products", "reach_seed", "read_run_metrics",
    "resolve_carrier_discharge", "resolve_gradation", "resolve_rain_forcing",
    "sanitize_substance", "slug", "solve_reach", "stage_manifest",
    "suggest_mesh_size_m", "suggest_time_step_s", "write_reach_deck",
]
