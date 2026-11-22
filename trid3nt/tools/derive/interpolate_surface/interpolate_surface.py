"""``interpolate_surface`` - the holes of one surface inside a region filled from the cells it measured there.

The algorithm is GDAL FillNodata's inverse-distance weighting or linear interpolation over the triangulation of the
measured cells around a hole, each reaching at most a stated number of pixels from a measured cell; a hole past that
stays unvalued and is counted. Band 2 marks each filled cell FILLED. It reads one raster and fetches nothing.
"""

from __future__ import annotations

from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt.tools import register_tool
from trid3nt.tools.derive import DeriveError

__all__ = ["farthest", "inside", "interpolate_surface", "interpolated"]


def interpolated(values: Any, targets: Any, known: Any, *, algorithm: str, max_distance: float) -> Any:
    """``values`` with the ``targets`` cells interpolated from the ``known`` cells alone, by ``algorithm``, NaN where
    a target lies farther than ``max_distance`` pixels from every known cell."""
    import numpy as np

    from trid3nt.tools.derive._raster_layers import NODATA

    reach = float(max_distance)
    work = np.where(known & ~targets, values, NODATA).astype("float32")
    if algorithm == "linear":
        painted = _linear(work, targets, reach)
    else:
        from rasterio.fill import fillnodata

        painted = fillnodata(work, mask=np.isfinite(work).astype("uint8"),
                             max_search_distance=reach)
    out = values.copy()
    out[targets] = painted[targets]
    return out


def _linear(work: Any, targets: Any, reach: float) -> Any:
    """Linear interpolation over the triangulation of the measured cells bordering the targets; a target
    outside their hull or past ``reach`` pixels from every measured cell stays NaN."""
    import numpy as np
    from scipy.interpolate import griddata
    from scipy.ndimage import binary_dilation, distance_transform_edt

    from trid3nt.tools.derive._raster_layers import NODATA

    known = np.isfinite(work)
    rim = known & binary_dilation(targets, structure=np.ones((3, 3), dtype=bool))
    out = work.copy()
    if int(rim.sum()) < 3:
        return out
    rows, cols = np.nonzero(rim)
    want_rows, want_cols = np.nonzero(targets)
    out[want_rows, want_cols] = griddata((rows, cols), work[rows, cols],
                                         (want_rows, want_cols), method="linear")
    out[targets & (distance_transform_edt(~known) > reach)] = NODATA
    return out


def inside(region: list[Any], crs: Any, transform: Any, shape: tuple[int, int]) -> Any:
    """The cells inside the region's polygons (already on the grid's CRS); every cell without one."""
    import numpy as np
    from rasterio.features import geometry_mask

    if not region:
        return np.ones(shape, dtype=bool)
    return geometry_mask(region, out_shape=shape, transform=transform, invert=True, all_touched=True)


def farthest(holes: Any, valued: Any) -> int:
    """The distance in pixels, rounded up, from the farthest ``holes`` cell to a ``valued`` one; 0 where none is a hole."""
    import math

    from scipy.ndimage import distance_transform_edt

    if not holes.any() or not valued.any():
        return 0
    return int(math.ceil(float(distance_transform_edt(~valued)[holes].max())))


def _layer(layer: Any) -> Any:
    from trid3nt.render.uri_registry import lookup_layer_for_handle

    if isinstance(layer, str):
        return lookup_layer_for_handle(layer) or layer
    return layer


def _said(layer: Any, name: str) -> Any:
    return layer.get(name) if isinstance(layer, dict) else getattr(layer, name, None)


def interpolated_layer(layer: Any, *, region: Any = None, algorithm: str | None = None,
                       max_distance: float | None = None, name: str | None = None) -> Any:
    """The tool's body: ``layer`` read whole, its holes inside ``region`` interpolated and counted -> the layer."""
    import tempfile

    import numpy as np

    from trid3nt_contracts.execution import layer_seed

    from trid3nt.inputs.coverage import INTERPOLATE, interpolation, step
    from trid3nt.inputs.vertical_datum import datum_of
    from trid3nt.tools.derive._raster_layers import (
        FILLED, UNMEASURED, RasterLayerURI, bbox_4326, coverage, label_of, polygons, read, sources_of, staged,
        written)

    try:
        declared = interpolation((step(INTERPOLATE, algorithm=algorithm, max_distance=max_distance),))
    except Exception as refused:  # noqa: BLE001 - the step's own refusal names the argument
        raise DeriveError("INTERPOLATE_ARGUMENT", str(refused)) from None
    held = _layer(layer)
    with tempfile.TemporaryDirectory(prefix="interpolate-") as scratch:
        values, provenance, crs, transform = read(staged(held, "surface", scratch))
    from rasterio.warp import transform_geom

    shapes = [transform_geom("EPSG:4326", crs, g) for g in polygons(_layer(region))] if region else []
    within = inside(shapes, crs, transform, values.shape)
    known = within & np.isfinite(values)
    if not known.any():
        raise DeriveError("INTERPOLATE_NOTHING_MEASURED",
                          "the surface measured no cell inside the region, so there is nothing to interpolate from.")
    holes = within & ~np.isfinite(values)
    painted = interpolated(values, holes, known, algorithm=declared.algorithm, max_distance=declared.max_distance)
    filled = holes & np.isfinite(painted)
    won = (provenance.copy() if provenance is not None
           else np.where(np.isfinite(values), 0, UNMEASURED).astype("uint8"))
    won[filled] = FILLED
    sources = [*(sources_of(held, provenance) if provenance is not None else [label_of(held, 0)])]
    if "filled" not in sources:
        sources.append("filled")
    reached, blank = coverage(won, sources, within)
    seed = layer_seed()
    height, width = painted.shape
    uri = written(painted, won, crs=crs, transform=transform, prefix="interpolated", seed=seed, output_dir=None)
    return RasterLayerURI.published(
        "interpolated", seed=seed, name=name or f"{label_of(held, 0)} interpolated", layer_type="raster", uri=uri,
        role="primary", units=_said(held, "units"), quantity=_said(held, "quantity"),
        bbox=bbox_4326(crs, transform, width, height),
        vertical_datum=datum_of(held) or None, sources=sources, coverage=reached, unmeasured_fraction=blank)


_METADATA = AtomicToolMetadata(name="interpolate_surface", ttl_class="live-no-cache", source_class=None)


@register_tool(_METADATA, read_only_hint=False, open_world_hint=False, destructive_hint=False, idempotent_hint=True)
def interpolate_surface(layer: Any = None, region: Any = None, algorithm: str | None = None,
                        max_distance: float | None = None, name: str | None = None,
                        **_extra_ignored: Any) -> Any:
    """DERIVE: fill the holes of one raster surface (a bed, a DEM, a bathymetry) inside a region by interpolation.

    ROUTING: "fill the holes in this bathymetry", "interpolate the gaps in the bed inside the water", "fill nodata in
    this DEM up to 50 pixels", "interpolate the survey's missing cells linearly". A new case layer; the input is not
    changed.

    `layer` is a case layer id (or uri) of a raster. `region` is a polygon layer id or GeoJSON the fill reads and paints
    inside alone; unset, the whole raster. `algorithm` is inverse_distance (GDAL FillNodata's own weighting) or linear
    (over the triangulation of the measured cells around a hole); `max_distance` is how far, in pixels, a hole's fill
    searches for a measured cell. Each unset takes the setting in force (FILL_ALGORITHM, FILL_MAX_DISTANCE).

    Returns a two-band raster: band 1 the values, band 2 the source of each cell, FILLED for an interpolated one,
    with the share each source and the fill reached over the region and the share nothing reached (a hole past the
    reach stays nodata). A surface measuring nothing inside the region refuses.
    """
    if layer is None:
        raise DeriveError("INTERPOLATE_ARGUMENT", "interpolate_surface takes the raster layer whose holes it fills.")
    return interpolated_layer(layer, region=region, algorithm=algorithm, max_distance=max_distance, name=name)
