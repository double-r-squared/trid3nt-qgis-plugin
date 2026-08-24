"""The aquifer-property derivations (law 9): SoilGrids texture -> K and porosity.

Both properties come off ONE texture read at the AOI, so the pedotransfer call is
memoized on the rounded point: two declared params, one fetch. Declaring them as
DERIVED params rather than resolving them inside a step is what puts the real
derived number - with its texture evidence and its source badge - on the form
card the user reviews, instead of behind the gate.

Unresolvable is a REFUSAL, never a default: there is no invented aquifer here.
"""

from __future__ import annotations

import asyncio
import logging
from functools import lru_cache
from typing import Any

from trid3nt_server.declarative import Derived
from trid3nt_server.workflows.shared.aquifer_resolve import derive_soil_k

from .errors import ModflowPhysicsInputRequired

__all__ = [
    "SOIL_PEDOTRANSFER_SOURCE",
    "aquifer_k_ms",
    "porosity",
    "screening_caveat",
]

logger = logging.getLogger("trid3nt_server.workflows.modflow.steps.aquifer")

#: Named on every derived row so the provenance says which real data answered.
SOIL_PEDOTRANSFER_SOURCE = "fetch_soilgrids (Saxton-Rawls 2006 pedotransfer)"


@lru_cache(maxsize=64)
def _texture_fit(lat: float, lon: float) -> tuple[Any, dict[str, Any]]:
    """The Saxton-Rawls fit at a point. Memoized: soil texture is a fixed fact."""
    return derive_soil_k(lat, lon)


async def _fit_at(params: Any) -> tuple[Any, dict[str, Any]]:
    # The EXACT coordinates, never rounded: the pedotransfer samples a window
    # around the point, so shifting it by a metre moves the mean texture in the
    # third decimal and the solve with it.
    lat, lon = params.aoi_latlon
    return await asyncio.to_thread(_texture_fit, float(lat), float(lon))


def screening_caveat(meta: dict[str, Any]) -> str:
    clamp = " (clamped to the plausible-media span)" if meta.get("clamped") else ""
    return (
        f"DERIVED from SoilGrids texture at the AOI (sand={meta.get('sand_pct')}%, "
        f"clay={meta.get('clay_pct')}%, {meta.get('depth')}) via the Saxton-Rawls "
        f"(2006) pedotransfer function{clamp}"
    )


def _refuse(name: str, what: str, meta: dict[str, Any]) -> ModflowPhysicsInputRequired:
    return ModflowPhysicsInputRequired(
        f"{what} is required and could not be resolved from SoilGrids at this AOI "
        f"({meta.get('reason', 'unavailable')}). No invented default (law 9): "
        f"supply {name} explicitly, or run where SoilGrids has coverage."
    )


async def aquifer_k_ms(params: Any) -> Derived:
    """Saturated hydraulic conductivity (m/s) from SoilGrids texture at the AOI."""
    fit, meta = await _fit_at(params)
    if fit is None:
        raise _refuse("aquifer_k_ms", "aquifer hydraulic conductivity (m/s)", meta)
    logger.info("modflow aquifer K derived %.3g m/s (sand=%s%% clay=%s%%)",
                fit.k_m_s, meta.get("sand_pct"), meta.get("clay_pct"))
    return Derived(value=float(fit.k_m_s), note=screening_caveat(meta),
                   real_source=SOIL_PEDOTRANSFER_SOURCE)


async def porosity(params: Any) -> Derived:
    """Effective porosity from the same SoilGrids texture fit."""
    fit, meta = await _fit_at(params)
    if fit is None:
        raise _refuse("porosity", "effective porosity", meta)
    return Derived(value=float(fit.porosity), note=screening_caveat(meta),
                   real_source=SOIL_PEDOTRANSFER_SOURCE)
