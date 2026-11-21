"""``Soil`` - the Green-Ampt parameters of the ground under a run - and ``soil``, its ingestion.

The row matches the sand fraction; the silt, clay and organic carbon arrive beside it on the same grid, and the
ingestion composes the ``green_ampt_parameters`` derive over the four. It fetches nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

__all__ = ["BESIDE", "Soil", "soil"]

#: ``(row, word, geometry)`` of each raster produced beside the matched sand fraction, asked in the source's word.
BESIDE: tuple[tuple[str, str, Any], ...] = (
    ("silt", "silt", None), ("clay", "clay", None), ("organic_carbon", "organic_carbon", None))


@dataclass(frozen=True, slots=True)
class Soil:
    """The derived parameter rasters, by quantity (``green_ampt_hydraulic_conductivity`` and its three siblings)."""

    layers: Mapping[str, Any]

    def layer(self, quantity: str) -> Any:
        """The raster of one parameter; a quantity the soil does not carry refuses by name."""
        if quantity not in self.layers:
            raise KeyError(f"the soil carries {sorted(self.layers)}, not {quantity!r}")
        return self.layers[quantity]


def soil(value: Any, *, label: str = "soil", silt: Any = None, clay: Any = None,
         organic_carbon: Any = None) -> Soil | None:
    """THE ingestion: the sand fraction and the three beside it -> ``Soil``; ``None`` only when nothing came.

    A missing fraction, rasters on different grids or other units refuse as the derive refuses them."""
    if value is None or isinstance(value, Soil):
        return value
    from trid3nt.tools.derive.green_ampt_parameters.green_ampt_parameters import derived

    layers = derived(value, silt, clay, organic_carbon)
    return Soil(MappingProxyType({layer.quantity: layer for layer in layers}))
