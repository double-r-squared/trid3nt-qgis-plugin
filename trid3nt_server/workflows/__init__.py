"""Deterministic, LLM-free workflows that compose atomic tools into runs.

Nothing is re-exported here; the engine packages are imported directly.
"""

from __future__ import annotations

# Imported for the side effect only: the TELEMAC local-docker solve specs
# register at import time into SOLVER_WORKFLOW_REGISTRY and
# LOCAL_SOLVER_SPEC_REGISTRY.
from .telemac.solving import run_telemac as _run_telemac  # noqa: F401

__all__: list[str] = []
