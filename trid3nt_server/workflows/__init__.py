"""Deterministic, LLM-free workflows that compose atomic tools into runs.

Nothing is re-exported here; the engine packages are imported directly.
"""

from __future__ import annotations

# Side-effect imports: each registers its local-docker solve spec.
from .telemac import engine as _telemac_engine  # noqa: F401
from .modflow import engine as _modflow_engine  # noqa: F401
from .geoclaw import engine as _geoclaw_engine  # noqa: F401

__all__: list[str] = []
