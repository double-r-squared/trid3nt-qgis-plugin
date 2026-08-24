"""The shared SWMM host-exec step family.

One skeleton the pyswmm templates declare against - site -> real soil properties
(law 9) -> an authored deck -> the native engine in-process -> the series turned
into an answer and a chart - with the deck writer as the single per-question
hook. Nothing here decides what question is being asked.
"""

from __future__ import annotations

from .charts import line_chart_spec
from .errors import (
    SwmmDeckError,
    SwmmPhysicsInputRequired,
    SwmmSolveError,
    SwmmStepError,
)
from .series import clock, coerce_series, peak, timeseries_block
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
    "SwmmSolveError", "SwmmStepError", "clock", "coerce_series",
    "conductivity_in_hr", "field_capacity", "line_chart_spec", "peak",
    "porosity", "resolve_site", "site_latlon", "solve_deck",
    "timeseries_block", "wilting_point",
]
