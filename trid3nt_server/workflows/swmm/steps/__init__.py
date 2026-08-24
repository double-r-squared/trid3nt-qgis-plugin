"""The shared SWMM host-exec step family.

One skeleton the pyswmm templates declare against - site -> real soil properties
(law 9) -> an authored deck -> the native engine in-process - with the deck
writer as the single per-question hook. Nothing here decides what question is
being asked.
"""

from __future__ import annotations

from .errors import (
    SwmmDeckError,
    SwmmPhysicsInputRequired,
    SwmmSolveError,
    SwmmStepError,
)
from .site import resolve_site, site_latlon
from .soil import (
    SOIL_COLUMN_SOURCE,
    conductivity_in_hr,
    field_capacity,
    porosity,
    wilting_point,
)
from .solve import Solve, solve_deck

__all__ = [
    "SOIL_COLUMN_SOURCE", "Solve", "SwmmDeckError", "SwmmPhysicsInputRequired",
    "SwmmSolveError", "SwmmStepError", "conductivity_in_hr", "field_capacity",
    "porosity", "resolve_site", "site_latlon", "solve_deck", "wilting_point",
]
