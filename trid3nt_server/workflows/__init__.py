"""Deterministic, LLM-free workflows that compose atomic tools into runs.

Nothing is re-exported here; the engine packages are imported directly.
"""

from __future__ import annotations

# Side-effect import: registers the TELEMAC local-docker solve specs.
from .telemac import engine as _telemac_engine  # noqa: F401

__all__: list[str] = []
