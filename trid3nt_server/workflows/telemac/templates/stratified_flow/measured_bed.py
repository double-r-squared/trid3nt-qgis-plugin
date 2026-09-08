"""The water domain, narrowed to the part of it anybody has measured a bed for.

A mapped water polygon and a bathymetric survey are two different documents. The
polygon says where the water is; the survey says where its floor was sounded, and
over a harbour arm or a slip the two disagree - the survey stops at its own
coverage and the polygon carries on. Meshing the difference builds elements the
bed painter has nothing to give, and a water domain has no bed where nobody
measured.

The footprint is read off the raster's own MASK, never off a threshold on the
values: a nodata cell and a cell sounded at zero depth are different statements,
and only the mask tells them apart.

Only this question declares the clip, so it lives beside the recipe that does; a
second question asking for it is what earns it a shared home.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(
    "trid3nt_server.workflows.telemac.templates.stratified_flow.measured_bed")

__all__ = ["basin_on_measured_bed"]


async def basin_on_measured_bed(*, polygon: Any, bed: Any) -> dict[str, Any]:
    """The water polygon INTERSECT the bed's measured footprint -> the domain.

    Returns the domain inline, with the two areas it was measured between and the
    sentence the run's journal says them in. A clip that meets nothing REFUSES:
    the answer to "the survey does not reach this water" is to name a bed that
    does, never a domain nobody sounded.
    """
    return await asyncio.to_thread(_clip, polygon, bed)


def _clip(polygon: Any, bed: Any) -> dict[str, Any]:
    import geopandas as gpd
    import rasterio
    from rasterio.features import shapes as raster_shapes
    from shapely.geometry import mapping, shape
    from shapely.ops import unary_union

    from trid3nt_server.workflows.shared.geometry import (
        flatten_geometries,
        read_geometry_doc,
    )
    from trid3nt_server.workflows.mesh.inputs import op_raster
    from trid3nt_server.workflows.runtime import journal_note
    from trid3nt_server.workflows.telemac.helpers.errors import OpenWaterError

    water = unary_union([shape(g) for g in
                         flatten_geometries(read_geometry_doc(polygon))])
    with rasterio.open(op_raster(bed)) as src:
        mask = src.dataset_mask()
        measured = unary_union([
            shape(geom) for geom, value in
            raster_shapes(mask, mask=mask.astype(bool), transform=src.transform)
            if value])
        crs = src.crs
    measured = gpd.GeoSeries([measured], crs=crs).to_crs(4326).union_all()
    domain = water.intersection(measured)
    if domain.is_empty:
        raise OpenWaterError(
            "the bed source measures nothing under this water body, so the "
            "domain would be water with no floor. Name a bed whose survey "
            "reaches this basin.",
            error_code="TELEMAC3D_BED_DOES_NOT_REACH")
    metric = gpd.GeoSeries([domain], crs=4326).estimate_utm_crs()
    kept, whole = (float(gpd.GeoSeries([g], crs=4326).to_crs(metric).area.iloc[0])
                   / 1.0e6 for g in (domain, water))
    note = (f"basin clipped to the bed's measured footprint: {kept:.4f} km2 of "
            f"the {whole:.4f} km2 the water polygon maps ({kept / whole:.0%}); "
            "the remainder is water the survey did not sound.")
    journal_note(note)
    logger.info("stratified basin: %.4f km2 of %.4f km2 measured", kept, whole)
    return {
        "domain": {"type": "FeatureCollection",
                   "features": [{"type": "Feature",
                                 "geometry": mapping(domain),
                                 "properties": {"area_km2": round(kept, 6)}}]},
        "area_km2": round(kept, 6),
        "mapped_area_km2": round(whole, 6),
        "note": note,
    }
