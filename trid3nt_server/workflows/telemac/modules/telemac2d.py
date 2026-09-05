"""The TELEMAC-2D wrapper: its catalog, and nothing that opines.

The composites and the outputs register here as the templates that need them
arrive; what the wrapper never gains is a default of its own. The engine's
default is the wrapper's whole position, and every opinion above it is a
template's.
"""

from __future__ import annotations

from .module import Module

__all__ = ["T2D"]

T2D = Module("telemac2d")
