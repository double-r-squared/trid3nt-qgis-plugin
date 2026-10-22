"""The GeoClaw wrapper: its dictionary as the one module a run starts from."""

from __future__ import annotations

from .module import GeoclawModule, fault, topography

__all__ = ["GEO"]

GEO = GeoclawModule("geoclaw")
GEO.composites(reads={"Topography": ("surface", "offset")}, Topography=topography,
               Fault=fault)
