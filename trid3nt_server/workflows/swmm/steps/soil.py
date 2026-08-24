"""The two-zone soil-column derivations (law 9): SoilGrids texture -> [AQUIFERS].

The four moisture-column properties SWMM's ``[AQUIFERS]`` object needs come off
ONE texture read at the site, so the pedotransfer call is memoized: four declared
params, one fetch. Each is its own row on the form card, carrying the texture it
was fitted from, and any of them can be pinned by hand.

Unresolvable is a REFUSAL, never a default: there is no invented column here.
"""

from __future__ import annotations

import asyncio
import logging
from functools import lru_cache
from typing import Any

from trid3nt_server.declarative import Derived
from trid3nt_server.workflows.shared.aquifer_resolve import derive_soil_column
from trid3nt_server.workflows.shared.site_resolve import SiteUnresolvedError

from .errors import SwmmPhysicsInputRequired
from .site import resolve_site

__all__ = [
    "SOIL_COLUMN_SOURCE",
    "conductivity_in_hr",
    "field_capacity",
    "porosity",
    "wilting_point",
]

logger = logging.getLogger("trid3nt_server.workflows.swmm.steps.soil")

#: Named on every derived row so the provenance says which real data answered.
SOIL_COLUMN_SOURCE = "fetch_soilgrids (Saxton-Rawls 2006 two-zone column)"


@lru_cache(maxsize=64)
def _column(lat: float, lon: float) -> tuple[Any, dict[str, Any]]:
    """The two-zone column at a point. Memoized: soil texture is a fixed fact.

    The coordinates are used EXACTLY as resolved - the pedotransfer samples a
    window around the point, so rounding the key would move the fit.
    """
    return derive_soil_column(lat, lon)


_NO_SITE = (
    "the two-zone aquifer column this deck needs is DERIVED from SoilGrids at a "
    "site, and no site was given. Supply a location (a place name) or lat + lon, "
    "or supply the whole column explicitly (porosity, wilting_point, "
    "field_capacity, conductivity_in_hr). It is never invented (law 9)."
)


async def _column_at(params: Any) -> tuple[Any, dict[str, Any]]:
    try:
        site = await asyncio.to_thread(resolve_site, params)
    except SiteUnresolvedError as exc:
        raise SwmmPhysicsInputRequired(
            f"the two-zone aquifer column is DERIVED at the site and the site "
            f"could not be resolved: {exc} Supply lat + lon, or the explicit column."
        ) from exc
    if site is None:
        raise SwmmPhysicsInputRequired(_NO_SITE)
    return await asyncio.to_thread(_column, float(site[0]), float(site[1]))


def _caveat(meta: dict[str, Any]) -> str:
    return (
        f"DERIVED from SoilGrids texture at the site (sand={meta.get('sand_pct')}%, "
        f"clay={meta.get('clay_pct')}%) via the Saxton-Rawls (2006) two-zone fit; "
        "a SCREENING near-surface proxy, NOT a measured column"
    )


def _refuse(name: str, what: str, meta: dict[str, Any]) -> SwmmPhysicsInputRequired:
    return SwmmPhysicsInputRequired(
        f"the two-zone aquifer {what} could not be resolved from SoilGrids at this "
        f"site ({meta.get('reason', 'unavailable')}). No invented default (law 9): "
        f"supply {name}, or run at a site within SoilGrids coverage."
    )


async def _property(params: Any, name: str, what: str) -> Derived:
    column, meta = await _column_at(params)
    if column is None:
        raise _refuse(name, what, meta)
    return Derived(value=float(getattr(column, name)), note=_caveat(meta),
                   real_source=SOIL_COLUMN_SOURCE)


async def porosity(params: Any) -> Derived:
    """Saturated water content theta_s - the [AQUIFERS] porosity."""
    column, meta = await _column_at(params)
    if column is None:
        raise _refuse("porosity", "porosity", meta)
    logger.info("swmm two-zone column derived por=%s wp=%s fc=%s K=%s in/hr "
                "(sand=%s%% clay=%s%%)", column.porosity, column.wilting_point,
                column.field_capacity, column.conductivity_in_hr,
                meta.get("sand_pct"), meta.get("clay_pct"))
    return Derived(value=float(column.porosity), note=_caveat(meta),
                   real_source=SOIL_COLUMN_SOURCE)


async def wilting_point(params: Any) -> Derived:
    """Water content at -1500 kPa - the lower bound of the unsaturated zone."""
    return await _property(params, "wilting_point", "wilting point")


async def field_capacity(params: Any) -> Derived:
    """Water content at -33 kPa - where drainage to the saturated zone begins."""
    return await _property(params, "field_capacity", "field capacity")


async def conductivity_in_hr(params: Any) -> Derived:
    """Saturated conductivity (in/hr) governing percolation to the water table."""
    return await _property(params, "conductivity_in_hr", "conductivity")
