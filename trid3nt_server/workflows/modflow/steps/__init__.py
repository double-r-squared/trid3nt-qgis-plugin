"""The shared MODFLOW archetype step family.

One skeleton every archetype template declares against - AOI point -> aquifer
properties (law 9) -> deck + mf6 + postprocess -> the archetype's typed layer -
with the run args as the single per-question hook. Nothing here decides what
question is being asked.
"""

from __future__ import annotations

from .aoi import aoi_latlon, location_name
from .aquifer import (
    SOIL_PEDOTRANSFER_SOURCE,
    aquifer_k_ms,
    porosity,
    screening_caveat,
)
from .archetype import RunArchetype, run_archetype, run_id_of
from .errors import (
    ModflowAoiInputError,
    ModflowArchetypeRunError,
    ModflowPhysicsInputRequired,
    ModflowStepError,
)
from .products import build_budget_chart

__all__ = [
    "ModflowAoiInputError", "ModflowArchetypeRunError",
    "ModflowPhysicsInputRequired", "ModflowStepError", "RunArchetype",
    "SOIL_PEDOTRANSFER_SOURCE", "aoi_latlon", "aquifer_k_ms",
    "build_budget_chart", "location_name", "porosity", "run_archetype",
    "run_id_of", "screening_caveat",
]
