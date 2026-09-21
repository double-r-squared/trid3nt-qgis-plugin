"""A QGIS layer written out as a file the agent can take.

The one export in the plugin, in both directions across the seam: the case push
sends the user's layer up, and a materialised layer-request sends the provider
layer back. It owns the whole ask - the window (absent = the whole layer) and,
for a raster, the pixel spacing in metres - because only the writer knows the
grid it is laying down. ``kind`` rides along because the ingest route registers
a GeoPackage and a GeoTIFF differently, and only the writer knows which it wrote.
A raster is written through the LAYER's own data provider, so a WCS, WMS or
ArcGIS layer exports the same way a local GeoTIFF does.
"""

from __future__ import annotations

import math
import os
from typing import Any, Optional, Tuple

__all__ = ["LayerExportError", "export_to_tempfile"]

#: Below this many pixels on an axis there is no grid left to write.
_MIN_PX = 1


class LayerExportError(Exception):
    """The layer could not be written out -- carries the writer's own message,
    or an honest local description of why there was nothing to write."""


def export_to_tempfile(
    layer: Any,
    bbox: Optional[Tuple[float, float, float, float]] = None,
    resolution_m: Optional[float] = None,
) -> Tuple[str, str]:
    """Export ``layer`` to a temp file, returning ``(local_path, kind)`` with
    ``kind`` either ``"vector"`` (GeoPackage) or ``"raster"`` (GeoTIFF).
    ``bbox`` is the asked window in EPSG:4326, or ``None`` for the whole layer;
    ``resolution_m`` is the pixel spacing a raster is written at. A vector has
    no pixel grid, so asking one for a spacing is a refusal, never a value
    quietly dropped. A ``None``, unsupported or unexportable layer raises. The
    caller owns the file, including deleting it."""
    from qgis.core import QgsRasterLayer, QgsVectorLayer

    if layer is None:
        raise LayerExportError(
            "no active layer -- select a layer in the QGIS Layers panel first"
        )
    if isinstance(layer, QgsVectorLayer):
        if resolution_m is not None:
            raise LayerExportError(
                f"'{layer.name()}' is a vector layer and has no pixel grid, so "
                f"the asked {resolution_m} m spacing cannot be honoured"
            )
        return _export_vector(layer, bbox), "vector"
    if isinstance(layer, QgsRasterLayer):
        return _export_raster(layer, bbox, resolution_m), "raster"
    raise LayerExportError(
        f"active layer '{layer.name()}' is not a vector or raster layer"
    )


def _export_vector(layer: Any, bbox: Optional[Tuple[float, float, float, float]]) -> str:
    import tempfile

    from qgis.core import QgsProject, QgsVectorFileWriter

    fd, path = tempfile.mkstemp(suffix=".gpkg", prefix="trid3nt_push_")
    os.close(fd)
    os.unlink(path)  # QgsVectorFileWriter creates the file itself

    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "GPKG"
    options.fileEncoding = "UTF-8"
    transform_context = QgsProject.instance().transformContext()
    result = QgsVectorFileWriter.writeAsVectorFormatV3(
        _windowed_vector(layer, bbox), path, transform_context, options
    )
    # writeAsVectorFormatV3 returns (WriterError, errorMessage[, ...]).
    err = result[0] if isinstance(result, tuple) else result
    if err != QgsVectorFileWriter.NoError:
        detail = result[1] if isinstance(result, tuple) and len(result) > 1 else str(err)
        raise LayerExportError(f"could not export vector layer: {detail}")
    return path


def _windowed_vector(layer: Any, bbox: Optional[Tuple[float, float, float, float]]) -> Any:
    """The features inside the asked window: a materialised row carries the AOI,
    never the provider's whole published coverage. A row whose ask IS the whole
    layer carries no bbox, and then there is no window to cut."""
    from qgis.core import QgsProcessing, QgsVectorLayer

    import processing

    if bbox is None:
        return layer
    extracted = processing.run("native:extractbyextent", {
        "INPUT": layer,
        "EXTENT": f"{bbox[0]},{bbox[2]},{bbox[1]},{bbox[3]} [EPSG:4326]",
        "CLIP": False,
        "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT,
    })["OUTPUT"]
    if isinstance(extracted, str):
        return QgsVectorLayer(extracted, layer.name(), "ogr")
    return extracted


def _export_raster(
    layer: Any,
    bbox: Optional[Tuple[float, float, float, float]],
    resolution_m: Optional[float],
) -> str:
    """Write the layer's own pixels over the asked window at the asked spacing.

    The write pulls blocks through the layer's data provider, which is what lets
    a borrowed WCS or WMS layer export at all: a GDAL command line can only read
    a datasource GDAL itself opens, and a provider uri is not one."""
    import tempfile

    from qgis.core import Qgis, QgsRasterFileWriter, QgsRasterPipe

    extent = _window_extent(layer, bbox)
    columns, rows = _grid(layer, extent, resolution_m)
    fd, path = tempfile.mkstemp(suffix=".tif", prefix="trid3nt_push_")
    os.close(fd)
    pipe = QgsRasterPipe()
    provider = layer.dataProvider()
    if not pipe.set(provider.clone()):
        raise LayerExportError(
            f"could not read '{layer.name()}' through its "
            f"{provider.name()} provider"
        )
    writer = QgsRasterFileWriter(path)
    result = writer.writeRaster(pipe, columns, rows, extent, layer.crs())
    if result != Qgis.RasterFileWriterResult.Success:
        raise LayerExportError(
            f"could not export raster layer '{layer.name()}': {result}"
        )
    return path


def _window_extent(layer: Any, bbox: Optional[Tuple[float, float, float, float]]) -> Any:
    """The asked window in the LAYER's own CRS, or the layer's whole extent."""
    from qgis.core import (
        QgsCoordinateReferenceSystem,
        QgsCoordinateTransform,
        QgsProject,
        QgsRectangle,
    )

    if bbox is None:
        return layer.extent()
    asked = QgsRectangle(bbox[0], bbox[1], bbox[2], bbox[3])
    source = QgsCoordinateReferenceSystem("EPSG:4326")
    if layer.crs() == source:
        return asked
    transform = QgsCoordinateTransform(source, layer.crs(), QgsProject.instance())
    return transform.transformBoundingBox(asked)


def _grid(layer: Any, extent: Any, resolution_m: Optional[float]) -> Tuple[int, int]:
    """(columns, rows) over ``extent``: the asked spacing when one was asked for,
    otherwise the layer's own pixel size."""
    if resolution_m is None:
        x_res = layer.rasterUnitsPerPixelX()
        y_res = layer.rasterUnitsPerPixelY()
    else:
        x_res, y_res = _target_resolution(layer, extent, float(resolution_m))
    if x_res <= 0.0 or y_res <= 0.0:
        raise LayerExportError(
            f"'{layer.name()}' reports no pixel size, so there is no grid to write"
        )
    columns = int(round(extent.width() / x_res))
    rows = int(round(extent.height() / y_res))
    if columns < _MIN_PX or rows < _MIN_PX:
        raise LayerExportError(
            f"the asked window over '{layer.name()}' is smaller than one pixel at "
            f"this spacing ({columns} x {rows})"
        )
    return columns, rows


def _target_resolution(
    layer: Any, extent: Any, resolution_m: float
) -> Tuple[float, float]:
    """The asked spacing as ``(x, y)`` in the layer's OWN CRS units. Metres pass
    through on both axes. A geographic CRS converts each axis at the WINDOW's
    centre latitude, through the metres in a degree of longitude and a degree of
    latitude there, so the exported cell is the asked size on the ground rather
    than square in degrees and wrong in metres. Any other unit refuses by name
    rather than passing a number that means something else."""
    from qgis.core import QgsUnitTypes

    crs = layer.crs()
    units = crs.mapUnits()
    if units == QgsUnitTypes.DistanceMeters:
        return resolution_m, resolution_m
    if not crs.isGeographic():
        raise LayerExportError(
            f"'{layer.name()}' is published in {crs.authid() or 'an unnamed CRS'}, "
            f"whose units are {QgsUnitTypes.toString(units)}; a spacing asked in "
            "metres cannot be honoured on that grid"
        )
    phi = math.radians((extent.yMinimum() + extent.yMaximum()) / 2.0)
    per_degree_lat = (
        111132.92
        - 559.82 * math.cos(2 * phi)
        + 1.175 * math.cos(4 * phi)
        - 0.0023 * math.cos(6 * phi)
    )
    per_degree_lon = (
        111412.84 * math.cos(phi)
        - 93.5 * math.cos(3 * phi)
        + 0.118 * math.cos(5 * phi)
    )
    if per_degree_lon <= 0.0:
        raise LayerExportError(
            f"'{layer.name()}' centres on a pole, where a degree of longitude is "
            f"no distance at all; the asked {resolution_m} m has no grid there"
        )
    return resolution_m / per_degree_lon, resolution_m / per_degree_lat
