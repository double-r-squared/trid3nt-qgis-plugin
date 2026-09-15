"""``derive_measured_extent`` - where a raster actually measured anything.

A survey covers the ground it sounded and no more; the file still spans a whole
rectangle. The footprint is read off the raster's own MASK, never off a
threshold on values, so a real zero is inside it and an untouched cell is not.
Handed a polygon, the footprint is intersected with it: meshing water the survey
never reached builds elements the bed painter has nothing to give.
"""
from __future__ import annotations

import logging
import tempfile
import uuid
from typing import Any

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.inputs.geometry import (
    flatten_geometries,
    read_geometry_doc,
    source_uri,
)
from trid3nt_server.tools import register_tool
from trid3nt_server.tools.derive._hydrology_common import (
    _stage_uri_local,
    _write_geojson,
)

__all__ = ["MeasuredExtentError", "MeasuredExtentLayerURI", "derive_measured_extent"]

logger = logging.getLogger(
    "trid3nt_server.tools.derive.derive_measured_extent.derive_measured_extent")

#: The label the footprint travels under: a polygon the tool MEASURED off a
#: raster's mask, whose meaning is whatever the raster measured.
_STYLE = {"kind": "reference", "geometry": "polygon"}

_METADATA = AtomicToolMetadata(
    name="derive_measured_extent",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)


class MeasuredExtentLayerURI(LayerURI):
    """The measured footprint, with its area, the area handed in, and notes."""

    area_km2: float = 0.0
    within_area_km2: float | None = None
    covered_fraction: float | None = None
    notes: list[str] = []


class MeasuredExtentError(RuntimeError):
    """A typed refusal: ``DERIVE_MEASURED_EXTENT_EMPTY`` (the raster measures
    nothing), ``_DISJOINT`` (it measures nothing under the polygon) or
    ``_SOURCE_UNREADABLE``."""

    error_code: str
    retryable: bool = False

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def _measured(path: str) -> Any:
    """The raster's valid-data mask as one geometry, in EPSG:4326."""
    import geopandas as gpd
    import rasterio
    from rasterio.features import shapes as raster_shapes
    from shapely.geometry import shape as _shape
    from shapely.ops import unary_union

    with rasterio.open(path) as src:
        mask = src.dataset_mask()
        parts = [_shape(geom) for geom, value in
                 raster_shapes(mask, mask=mask.astype(bool), transform=src.transform)
                 if value]
        crs = src.crs
    if not parts:
        raise MeasuredExtentError(
            "DERIVE_MEASURED_EXTENT_EMPTY",
            "every cell of this raster is nodata, so it measured nothing "
            "anywhere and there is no footprint to take.")
    return gpd.GeoSeries([unary_union(parts)], crs=crs).to_crs(4326).union_all()


def _km2(geometry: Any, metric: Any) -> float:
    import geopandas as gpd

    return float(
        gpd.GeoSeries([geometry], crs=4326).to_crs(metric).area.iloc[0]) / 1.0e6


@register_tool(
    _METADATA,
    read_only_hint=False,
    # Reads the raster it is handed and writes its own artifact.
    open_world_hint=False,
)
def derive_measured_extent(
    raster: Any,
    within: Any = None,
    *,
    _output_dir: str | None = None,
    # absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> MeasuredExtentLayerURI:
    """Take the footprint a raster ACTUALLY measured -> a polygon layer.

    ROUTING: "where does this survey actually have data", "clip the lake to the
    part the bathymetry sounded", "the real coverage of this DEM, not its
    rectangle". Use it before meshing or clipping over a survey that covers less
    ground than its file spans: what it returns is a DOMAIN, ready for
    ``build_mesh(extent=<this uri>)``.

    Do NOT use for: a threshold on VALUES (a depth or an elevation band is a
    classification, not coverage), or the raster's bounding box
    (``compute_layer_bounds``).

    Params:
        raster: the raster whose measured footprint is taken - a fetched raster
            layer or its uri.
        within: OPTIONAL polygons to intersect the footprint with (a vector
            layer or inline GeoJSON) - the water body, the AOI, the domain so
            far. Omitted, the footprint stands on its own.

    Returns the footprint as a polygon layer with its ``area_km2``, the
    ``within_area_km2`` handed in and the ``covered_fraction`` of it. A raster
    that measures nothing under the polygon refuses rather than returning an
    empty domain.
    """
    from shapely.geometry import mapping, shape as _shape
    from shapely.ops import unary_union

    import geopandas as gpd

    from trid3nt_server.workflows.runtime import journal_note

    uri = str(source_uri(raster) or "").strip()
    if not uri:
        raise MeasuredExtentError(
            "DERIVE_MEASURED_EXTENT_SOURCE_UNREADABLE",
            f"the raster {raster!r} names no file to read.")
    with tempfile.TemporaryDirectory(prefix="measured-extent-") as tmpdir:
        try:
            path = _stage_uri_local(uri, tmpdir, "raster")
        except Exception as exc:  # noqa: BLE001 - every reader fault, named by source
            raise MeasuredExtentError(
                "DERIVE_MEASURED_EXTENT_SOURCE_UNREADABLE",
                f"the raster {uri!r} could not be read ({exc}).") from exc
        footprint = _measured(path)

    notes: list[str] = []
    whole: float | None = None
    if within is not None:
        polys = [_shape(g) for g in flatten_geometries(read_geometry_doc(within))
                 if str(g.get("type") or "") in ("Polygon", "MultiPolygon")]
        if not polys:
            raise MeasuredExtentError(
                "DERIVE_MEASURED_EXTENT_DISJOINT",
                f"the area {within!r} carries no polygon to intersect the "
                "measured footprint with.")
        asked = unary_union(polys)
        footprint = footprint.intersection(asked)
        if footprint.is_empty:
            raise MeasuredExtentError(
                "DERIVE_MEASURED_EXTENT_DISJOINT",
                "this raster measures nothing under the area given, so the "
                "domain would be ground with no data beneath it. Name a survey "
                "whose coverage reaches this place.")
        metric = gpd.GeoSeries([asked], crs=4326).estimate_utm_crs()
        whole = _km2(asked, metric)
    else:
        metric = gpd.GeoSeries([footprint], crs=4326).estimate_utm_crs()
    kept = _km2(footprint, metric)
    fraction = (kept / whole) if whole else None
    note = (f"measured footprint: {kept:.4f} km2"
            + (f" of the {whole:.4f} km2 asked for ({fraction:.0%}); the "
               "remainder is ground this raster never measured."
               if whole else " read off the raster's own mask."))
    notes.append(note)
    journal_note(note)

    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": mapping(footprint),
         "properties": {"area_km2": round(kept, 6)}}]}
    seed = uuid.uuid4().hex[:8]
    out_uri = _write_geojson(fc, "measured_extent", seed, _output_dir)
    logger.info("derive_measured_extent: %.4f km2 measured", kept)
    return MeasuredExtentLayerURI(
        layer_id=f"measured-extent-{seed}",
        name=f"Measured footprint ({kept:.2f} km2)",
        layer_type="vector",
        uri=out_uri,
        style=_STYLE,
        role="primary",
        crs_authid="EPSG:4326",
        bbox=tuple(float(v) for v in footprint.bounds),
        area_km2=round(kept, 6),
        within_area_km2=round(whole, 6) if whole else None,
        covered_fraction=round(fraction, 6) if fraction is not None else None,
        notes=notes)
